import base64
import os
import queue
import threading
import time

import sounddevice as sd
from dotenv import load_dotenv
from openai import OpenAI


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
- No afirmes controlar la computadora todavía.
- No afirmes ejecutar herramientas todavía.

Si el usuario pide información actual o alguna acción que requiere
una herramienta externa aún no conectada, dilo claramente.

No digas que estás comprobando información externa si realmente
no puedes hacerlo.
"""


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
# RECEPCIÓN DESDE GPT-LIVE
# ============================================================

def receiver(
    connection,
) -> None:

    try:

        while not stop_event.is_set():

            event = connection.recv()

            event_type = event.type


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
                    "La tarea requiere una herramienta "
                    "externa que todavía no está conectada."
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
    print("JARVIS v0.2.2")
    print("GPT-Live + interrupción real")
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

                    "store": False,
                }
            )


            session = wait_until_started(
                connection
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
                    args=(connection,),
                    daemon=True,
                )

                receiver_thread.start()


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

                        time.sleep(
                            0.05
                        )


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


        print()

        print(
            "Jarvis cerrado."
        )


if __name__ == "__main__":
    main()
