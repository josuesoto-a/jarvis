import base64
import os
import queue
import threading
import time

import sounddevice as sd
from dotenv import load_dotenv
from openai import OpenAI
from websockets.exceptions import ConnectionClosedOK

from integrations.action_result_display import (
    extract_copyable_urls,
)
from integrations.local_approval_defaults import (
    build_default_local_approval_registry,
)

from core.action_worker import ActionWorker
from core.bootstrap import build_default_jarvis

from integrations.openai_live import (
    PendingPermissionUpdate,
    TerminalFunctionOutput,
    perform_action_argument_schema,
)
from integrations.openai_live_voice import (
    VoiceActionBridge,
)
from integrations.openai_live_voice_session import (
    RearmingVoiceActionSession,
)


LOCAL_APPROVAL_REGISTRY = (
    build_default_local_approval_registry()
)


# ============================================================
# CONFIGURACIÓN
# ============================================================

load_dotenv(".env")

API_KEY = os.getenv("OPENAI_API_KEY")

if not API_KEY:
    raise RuntimeError("No se encontró OPENAI_API_KEY en .env")


MODEL = "gpt-live-1"
VOICE = "marin"

SAMPLE_RATE = 24000
CHANNELS = 1
DTYPE = "int16"
BYTES_PER_SAMPLE = 2

BLOCK_MS = 20
BLOCKSIZE = int(SAMPLE_RATE * BLOCK_MS / 1000)


SYSTEM_INSTRUCTIONS = """
Tu nombre es Jarvis.

Eres un asistente personal de voz ejecutándose en la computadora
del usuario.

Reglas:

- Habla principalmente en español.
- Mantén una conversación natural.
- Sé directo, preciso y relativamente breve.
- Escucha mientras hablas.
- El usuario puede interrumpirte en cualquier momento.
- Si el usuario empieza a hablar mientras tú estás hablando,
  deja de desarrollar la respuesta anterior y atiende la nueva
  intervención.
- Tolera pausas naturales.
- Si no entiendes algo importante, pregunta.
- No inventes información.
- No afirmes tener memoria persistente todavía.
- Puedes delegar tareas al motor de acciones cuando necesites
  información pública actual o una capacidad externa disponible.
- Mientras el backend trabaja, la conversación de voz puede continuar.
- No anuncies que una acción tuvo éxito hasta recibir su resultado.
- Nunca apruebes permisos en nombre del usuario.
- Si una acción requiere confirmación, espera la autorización
  proporcionada por la aplicación.
- No inventes resultados de herramientas ni acciones.
"""


# ============================================================
# ACTION ENGINE / RESPONSES DELEGATION
# ============================================================

ACTION_BACKEND_MODEL = "gpt-5.6-luna"

ACTION_BACKEND_INSTRUCTIONS = """
You are the action-intent backend for Jarvis, a live voice assistant.

The conversation transcript may contain speech-recognition mistakes,
unfinished phrases, pauses, and later corrections.

When GPT-Live delegates work to you:

- Translate the user's requested task into exactly one perform_action call.
- Preserve the user's real goal and relevant constraints.
- Use the conversation context to resolve obvious references.
- Do not invent authority or permissions.
- Never place confirmed_steps, permission overrides, or request IDs
  inside tool arguments.
- Jarvis' local Action Engine is responsible for planning, permissions,
  execution, verification, and capability selection.
- Return useful factual results after the application returns the tool result.
- Never invent that an action succeeded.

The application, not the model, controls authorization.
""".strip()

ACTION_TOOL_CHOICE = {
    "type": "function",
    "name": "perform_action",
}

ACTION_POLL_INTERVAL = 0.02



# ============================================================
# ESTADO GLOBAL
# ============================================================

stop_event = threading.Event()

input_audio_queue = queue.Queue(maxsize=500)

console_lock = threading.Lock()

current_console_speaker = None


# ============================================================
# BUFFER DE AUDIO DE JARVIS
# ============================================================

class PlaybackBuffer:

    def __init__(self):
        self.queue = queue.SimpleQueue()

        # Solo el callback de PortAudio modifica este bytearray.
        self.pending = bytearray()

        # Otro thread puede pedir un corte inmediato.
        self.interrupt_requested = threading.Event()


    def put(self, data: bytes) -> None:

        if data:
            self.queue.put(data)


    def request_interrupt(self) -> None:
        """
        Indica al callback de audio que descarte inmediatamente
        todo el audio pendiente de Jarvis.
        """

        self.interrupt_requested.set()


    def _drain_queue(self) -> None:

        while True:

            try:
                self.queue.get_nowait()

            except queue.Empty:
                break


    def callback(
        self,
        outdata,
        frames,
        time_info,
        status,
    ) -> None:

        bytes_required = (
            frames
            * CHANNELS
            * BYTES_PER_SAMPLE
        )


        # ----------------------------------------------------
        # INTERRUPCIÓN
        # ----------------------------------------------------

        if self.interrupt_requested.is_set():

            self.pending.clear()

            self._drain_queue()

            outdata[:] = b"\x00" * len(outdata)

            self.interrupt_requested.clear()

            return


        # ----------------------------------------------------
        # OBTENER AUDIO DISPONIBLE
        # ----------------------------------------------------

        while len(self.pending) < bytes_required:

            try:
                chunk = self.queue.get_nowait()

            except queue.Empty:
                break

            self.pending.extend(chunk)


        # ----------------------------------------------------
        # PORTAUDIO SIEMPRE DEBE RECIBIR UN BUFFER COMPLETO
        # ----------------------------------------------------

        outdata[:] = b"\x00" * len(outdata)

        available = min(
            bytes_required,
            len(self.pending),
        )


        if available:

            outdata[:available] = self.pending[:available]

            del self.pending[:available]


playback = PlaybackBuffer()


# ============================================================
# CONSOLA
# ============================================================

def print_transcript(
    speaker: str,
    text: str,
) -> None:

    global current_console_speaker

    if not text:
        return


    with console_lock:

        if current_console_speaker != speaker:

            print()

            if speaker == "user":

                print(
                    "Tú > ",
                    end="",
                    flush=True,
                )

            else:

                print(
                    "Jarvis > ",
                    end="",
                    flush=True,
                )


            current_console_speaker = speaker


        print(
            text,
            end="",
            flush=True,
        )


def console_message(
    message: str,
) -> None:

    global current_console_speaker


    with console_lock:

        print()

        print(
            message,
            flush=True,
        )

        current_console_speaker = None


# ============================================================
# MICRÓFONO
# ============================================================

def microphone_callback(
    indata,
    frames,
    time_info,
    status,
) -> None:

    if stop_event.is_set():
        return


    try:

        input_audio_queue.put_nowait(
            bytes(indata)
        )

    except queue.Full:

        # Nunca bloqueamos PortAudio.
        pass


# ============================================================
# ENVÍO DE MICRÓFONO A GPT-LIVE
# ============================================================

def microphone_sender(
    connection,
) -> None:

    while not stop_event.is_set():

        try:

            chunk = input_audio_queue.get(
                timeout=0.1
            )

        except queue.Empty:

            continue


        if chunk is None:
            break


        encoded = base64.b64encode(
            chunk
        ).decode("ascii")


        try:

            connection.session.input_audio.append(
                audio=encoded
            )


        except Exception as error:

            if not stop_event.is_set():

                console_message(
                    "[ERROR ENVIANDO AUDIO]\n"
                    f"{error}"
                )


            stop_event.set()

            break


# ============================================================
# POLLING NO BLOQUEANTE DEL ACTION ENGINE
# ============================================================

def action_poller(
    action_session,
    orchestrator,
    approval_queue,
) -> None:
    """Poll Jarvis without blocking the Live receiver thread."""

    last_notice = None

    try:

        while not stop_event.is_set():

            projection = (
                action_session.poll_once()
            )

            if isinstance(
                projection,
                PendingPermissionUpdate,
            ):

                notice = (
                    "permission",
                    projection.request_id,
                    projection.pending_confirmation_steps,
                )

                if notice != last_notice:

                    console_message(
                        "\n".join(
                            [
                                "",
                                "=" * 60,
                                "PERMISO REQUERIDO",
                                "=" * 60,
                                (
                                    "Request ID: "
                                    f"{projection.request_id}"
                                ),
                                (
                                    "Pasos pendientes: "
                                    f"{projection.pending_confirmation_steps}"
                                ),
                                (
                                    projection.message
                                    or (
                                        "La acción requiere "
                                        "confirmación local."
                                    )
                                ),
                                (
                                    "La aprobación por voz todavía "
                                    "no está habilitada; usa el teclado local."
                                ),
                            ]
                        )
                    )

                    approval = LOCAL_APPROVAL_REGISTRY.resolve(
                        orchestrator,
                        projection,
                    )

                    if approval is not None:
                        console_message(
                            LOCAL_APPROVAL_REGISTRY.render(approval)
                        )

                        try:
                            approval_queue.put_nowait(approval)
                        except queue.Full:
                            console_message(
                                "[PERMISO] La cola local está ocupada. "
                                "No se concedió autorización."
                            )

                    else:
                        console_message(
                            "[PERMISO NO AUTORIZABLE EN v0.2.6] "
                            "Solo se admite browser de riesgo bajo "
                            "con URL literal o una URL concreta "
                            "congelada en checkpoint. "
                            "El navegador seguirá detenido."
                        )

                    last_notice = notice

            elif isinstance(
                projection,
                TerminalFunctionOutput,
            ):

                notice = (
                    "terminal",
                    projection.request_id,
                    projection.status,
                )

                if notice != last_notice:

                    console_message(
                        "\n".join(
                            [
                                "",
                                (
                                    "[ACCIÓN JARVIS] "
                                    f"{projection.status}"
                                ),
                                (
                                    "Request ID: "
                                    f"{projection.request_id}"
                                ),
                            ]
                        )
                    )

                    copyable_urls = (
                        extract_copyable_urls(
                            projection.output_json
                        )
                    )

                    if copyable_urls:

                        console_message(
                            "\n".join(
                                [
                                    "",
                                    "[RESULTADO COPIABLE]",
                                    *copyable_urls,
                                ]
                            )
                        )

                    last_notice = notice

            time.sleep(
                ACTION_POLL_INTERVAL
            )

    except Exception as error:

        if not stop_event.is_set():

            console_message(
                "[ERROR ACTION ENGINE]\n"
                f"{error}"
            )

            stop_event.set()


# ============================================================
# RECEPCIÓN DESDE GPT-LIVE
# ============================================================

def receiver(
    connection,
    action_session,
) -> None:

    try:

        while not stop_event.is_set():

            event = connection.recv()

            event_type = event.type


            # ------------------------------------------------
            # ACTION ENGINE / RESPONSES
            # ------------------------------------------------

            if (
                event_type == "response.event"
                or event_type == "session.updated"
            ):

                action_session.handle_event(
                    event
                )

                continue


            # ------------------------------------------------
            # AUDIO DE JARVIS
            # ------------------------------------------------

            if (
                event_type
                == "session.output_audio.delta"
            ):

                raw_audio = base64.b64decode(
                    event.delta
                )

                playback.put(
                    raw_audio
                )

                continue


            # ------------------------------------------------
            # USUARIO EMPEZÓ / SIGUE HABLANDO
            # ------------------------------------------------

            if (
                event_type
                == "session.input_transcript.delta"
            ):

                # Ésta es la pieza nueva de v0.2.2.
                #
                # Si hay audio viejo de Jarvis pendiente,
                # PortAudio lo elimina inmediatamente.
                playback.request_interrupt()


                print_transcript(
                    "user",
                    event.delta,
                )

                continue


            # ------------------------------------------------
            # TRANSCRIPCIÓN DE JARVIS
            # ------------------------------------------------

            if (
                event_type
                == "session.output_transcript.delta"
            ):

                print_transcript(
                    "jarvis",
                    event.delta,
                )

                continue


            # ------------------------------------------------
            # DELEGACIÓN
            # ------------------------------------------------

            if (
                event_type
                == "session.delegation.created"
            ):

                console_message(
                    "[DELEGACIÓN]\n"
                    "GPT-Live delegó la tarea al "
                    "Action Engine de Jarvis."
                )

                continue


            # ------------------------------------------------
            # EVENTOS INFORMATIVOS
            # ------------------------------------------------

            if (
                event_type
                == "info"
                or event_type
                == "session.usage.updated"
            ):

                continue


            # ------------------------------------------------
            # ERROR
            # ------------------------------------------------

            if event_type == "error":

                error = event.error


                console_message(
                    "\n".join(
                        [
                            "",
                            "=" * 60,
                            "ERROR DE GPT-LIVE",
                            "=" * 60,
                            (
                                "Tipo: "
                                f"{getattr(error, 'type', None)}"
                            ),
                            (
                                "Código: "
                                f"{getattr(error, 'code', None)}"
                            ),
                            (
                                "Mensaje: "
                                f"{getattr(error, 'message', None)}"
                            ),
                        ]
                    )
                )


                stop_event.set()

                break


            # ------------------------------------------------
            # SESIÓN CERRADA
            # ------------------------------------------------

            if event_type == "session.closed":

                stop_event.set()

                break


    except ConnectionClosedOK:

        # WebSocket close code 1000 is a normal shutdown.
        stop_event.set()

    except Exception as error:

        if not stop_event.is_set():

            console_message(
                "[ERROR RECIBIENDO EVENTOS]\n"
                f"{error}"
            )

            stop_event.set()


# ============================================================
# ESPERAR SESSION.STARTED
# ============================================================

def wait_until_started(
    connection,
):

    while True:

        event = connection.recv()


        if event.type == "session.started":

            return event.session


        if event.type == "error":

            error = event.error

            raise RuntimeError(
                f"{getattr(error, 'code', 'error')}: "
                f"{getattr(error, 'message', error)}"
            )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    print("=" * 60)
    print("JARVIS v0.2.6")
    print("GPT-Live + Action Engine + interrupción real")
    print("=" * 60)
    print()


    # --------------------------------------------------------
    # VALIDAR HARDWARE
    # --------------------------------------------------------

    print(
        "Comprobando dispositivos de audio..."
    )


    sd.check_input_settings(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
    )


    sd.check_output_settings(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
    )


    print("✅ Micrófono")
    print("✅ Parlantes")
    print()


    # --------------------------------------------------------
    # OPENAI
    # --------------------------------------------------------

    client = OpenAI(
        api_key=API_KEY
    )


    # --------------------------------------------------------
    # JARVIS PRODUCTION ACTION ENGINE
    # --------------------------------------------------------

    app = build_default_jarvis(
        client=client,
        planner_model="gpt-5.6-luna",
        web_search_model="gpt-5.6-luna",
    )

    worker = ActionWorker(
        app.orchestrator.run,
        capacity=8,
    )

    worker.start()


    try:

        print(
            "Conectando con GPT-Live-1..."
        )


        with client.live.connect() as connection:

            connection.session.start(
                session={
                    "model": MODEL,

                    "instructions":
                        SYSTEM_INSTRUCTIONS,

                    "audio": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": SAMPLE_RATE,
                        },

                        "output": {
                            "voice": VOICE,
                        },
                    },

                    "delegation": {
                        "type": "responses",

                        "responses": {
                            "model": ACTION_BACKEND_MODEL,

                            "instructions":
                                ACTION_BACKEND_INSTRUCTIONS,

                            "tools": [
                                {
                                    "type": "function",
                                    "name": "perform_action",
                                    "description": (
                                        "Submit one user-requested "
                                        "task to the local Jarvis "
                                        "Action Engine."
                                    ),
                                    "parameters":
                                        perform_action_argument_schema(),
                                    "strict": False,
                                }
                            ],

                            "tool_choice":
                                ACTION_TOOL_CHOICE,

                            "parallel_tool_calls":
                                False,

                            "max_output_tokens":
                                256,
                        },
                    },

                    "store": False,
                }
            )


            session = wait_until_started(
                connection
            )


            # ------------------------------------------------
            # LIVE ↔ ACTION ENGINE BRIDGE
            # ------------------------------------------------

            voice_action_bridge = (
                VoiceActionBridge(
                    connection=connection,
                    worker=worker,
                )
            )

            action_session = (
                RearmingVoiceActionSession(
                    connection=connection,
                    bridge=voice_action_bridge,
                    action_instructions=(
                        ACTION_BACKEND_INSTRUCTIONS
                    ),
                    action_tool_choice=(
                        ACTION_TOOL_CHOICE
                    ),
                )
            )


            print()
            print("=" * 60)
            print("JARVIS LIVE ACTIVO")
            print("=" * 60)

            print(
                f"Session ID: {session.id}"
            )

            print(
                f"Modelo: {session.model}"
            )

            print()

            print(
                "🎙️ Conversación continua."
            )

            print(
                "✋ Puedes interrumpir a Jarvis."
            )

            print(
                "🛑 Ctrl+C para terminar."
            )

            print()


            # ------------------------------------------------
            # AUDIO DE SALIDA
            # ------------------------------------------------

            approval_queue = queue.Queue(maxsize=1)

            with sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=BLOCKSIZE,
                callback=playback.callback,
            ):


                # --------------------------------------------
                # ENVÍO
                # --------------------------------------------

                sender_thread = threading.Thread(
                    target=microphone_sender,
                    args=(connection,),
                    daemon=True,
                )

                sender_thread.start()


                # --------------------------------------------
                # RECEPCIÓN
                # --------------------------------------------

                receiver_thread = threading.Thread(
                    target=receiver,
                    args=(
                        connection,
                        action_session,
                    ),
                    daemon=True,
                )

                receiver_thread.start()


                # --------------------------------------------
                # ACTION ENGINE POLLER
                # --------------------------------------------

                action_thread = threading.Thread(
                    target=action_poller,
                    args=(
                        action_session,
                        app.orchestrator,
                        approval_queue,
                    ),
                    daemon=True,
                )

                action_thread.start()


                # --------------------------------------------
                # MICRÓFONO
                # --------------------------------------------

                with sd.RawInputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype=DTYPE,
                    blocksize=BLOCKSIZE,
                    callback=microphone_callback,
                ):

                    while not stop_event.is_set():

                        try:
                            approval = approval_queue.get_nowait()

                        except queue.Empty:
                            time.sleep(0.05)
                            continue

                        while not stop_event.is_set():

                            answer = input(
                                "\\nEscribe AUTORIZAR y Enter "
                                "(Ctrl+C para cerrar): "
                            )

                            if answer.strip() != "AUTORIZAR":
                                console_message(
                                    "No autorizado. La acción sigue "
                                    "detenida. Escribe AUTORIZAR "
                                    "solo si deseas permitirla."
                                )
                                continue

                            if not LOCAL_APPROVAL_REGISTRY.is_current(
                                app.orchestrator,
                                action_session,
                                approval,
                            ):
                                console_message(
                                    "[PERMISO RECHAZADO] La solicitud "
                                    "o su URL ya no coinciden. "
                                    "No se ejecutó el navegador."
                                )
                                break

                            try:
                                confirmed_id = (
                                    action_session.confirm_pending(
                                        confirmed_steps=frozenset(
                                            {approval.step_number}
                                        )
                                    )
                                )

                            except Exception as error:
                                console_message(
                                    "[PERMISO RECHAZADO] "
                                    f"{type(error).__name__}: {error}"
                                )

                            else:
                                console_message(
                                    "[AUTORIZACIÓN LOCAL ACEPTADA] "
                                    f"Request ID: {confirmed_id}"
                                )

                            break


            # ------------------------------------------------
            # CIERRE DE SESIÓN
            # ------------------------------------------------

            try:

                connection.session.close()

            except Exception:

                pass


    except KeyboardInterrupt:

        console_message(
            "\nCerrando Jarvis..."
        )

        stop_event.set()


    except Exception as error:

        console_message(
            "[JARVIS ERROR]\n"
            f"{error}"
        )

        stop_event.set()


    finally:

        try:

            input_audio_queue.put_nowait(
                None
            )

        except queue.Full:

            pass


        try:

            worker.shutdown(
                wait=True,
                timeout=3,
            )

        except Exception:

            pass


        print()

        print(
            "Jarvis cerrado."
        )


if __name__ == "__main__":
    main()
