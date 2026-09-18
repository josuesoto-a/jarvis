
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
    raise RuntimeError(
        "No se encontró OPENAI_API_KEY en el archivo .env"
    )


MODEL = "gpt-live-1"
VOICE = "marin"

SAMPLE_RATE = 24000
CHANNELS = 1
DTYPE = "int16"

BYTES_PER_SAMPLE = 2

BLOCK_MS = 20
BLOCKSIZE = int(
    SAMPLE_RATE * BLOCK_MS / 1000
)


SYSTEM_INSTRUCTIONS = """
Tu nombre es Jarvis.

Eres un asistente personal de voz ejecutándose en la computadora
del usuario.

Esta es tu versión de conversación Live.

Reglas:

- Habla principalmente en español.
- Mantén una conversación natural.
- Sé directo, preciso y relativamente breve.
- Escucha atentamente al usuario.
- El usuario puede interrumpirte.
- Tolera pausas naturales.
- Si no entiendes algo importante, pregunta.
- No inventes información.
- No afirmes tener memoria persistente todavía.
- No afirmes controlar la computadora todavía.
- No afirmes ejecutar herramientas todavía.

Si el usuario pide información actual o una acción que requiere una
herramienta externa que todavía no está conectada, indícale brevemente
que esa herramienta aún no está disponible.

No digas que estás comprobando información externa si realmente no
puedes hacerlo.
"""


# ============================================================
# ESTADO
# ============================================================

stop_event = threading.Event()

input_audio_queue = queue.Queue(
    maxsize=500
)

console_lock = threading.Lock()

current_console_speaker = None

usage_seconds = 0.0


# ============================================================
# BUFFER DE REPRODUCCIÓN
# ============================================================

class PlaybackBuffer:
    """
    El thread receptor deposita aquí audio de GPT-Live.

    PortAudio consume ese audio desde su propio callback.

    Nadie llama output_stream.write().
    """

    def __init__(self):
        self.queue = queue.SimpleQueue()

        # Este bytearray solamente es manipulado
        # desde el callback de salida.
        self.pending = bytearray()

        self.underflows = 0


    def put(self, data: bytes) -> None:
        if data:
            self.queue.put(data)


    def callback(
        self,
        outdata,
        frames,
        time_info,
        status,
    ) -> None:

        if status:
            # No imprimimos desde el callback de audio.
            # Solo registramos que hubo falta de datos.
            self.underflows += 1

        bytes_required = (
            frames
            * CHANNELS
            * BYTES_PER_SAMPLE
        )

        # Recoger todos los chunks disponibles hasta
        # disponer de suficientes bytes.
        while len(self.pending) < bytes_required:

            try:
                chunk = self.queue.get_nowait()

            except queue.Empty:
                break

            if chunk is None:
                break

            self.pending.extend(chunk)


        available = min(
            bytes_required,
            len(self.pending),
        )


        # Siempre debemos llenar completamente
        # el buffer que PortAudio nos entrega.
        outdata[:] = b"\x00" * len(outdata)


        if available:

            outdata[:available] = self.pending[
                :available
            ]

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

            elif speaker == "jarvis":
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


    if status:

        # Evitamos hacer trabajo pesado en el callback.
        pass


    try:

        input_audio_queue.put_nowait(
            bytes(indata)
        )

    except queue.Full:

        # Mejor descartar un bloque que bloquear
        # el callback de PortAudio.
        pass


# ============================================================
# ENVÍO DEL MICRÓFONO A GPT-LIVE
# ============================================================

def microphone_sender(
    connection,
) -> None:

    while not stop_event.is_set():

        try:

            audio_chunk = input_audio_queue.get(
                timeout=0.1
            )

        except queue.Empty:
            continue


        if audio_chunk is None:
            break


        encoded_audio = base64.b64encode(
            audio_chunk
        ).decode("ascii")


        try:

            connection.session.input_audio.append(
                audio=encoded_audio
            )

        except Exception as error:

            if not stop_event.is_set():

                console_message(
                    "[ERROR enviando audio]\n"
                    f"{error}"
                )

            stop_event.set()

            break


# ============================================================
# RECEPCIÓN DE GPT-LIVE
# ============================================================

def receiver(
    connection,
) -> None:

    global usage_seconds


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
            # TRANSCRIPCIÓN DEL USUARIO
            # ------------------------------------------------

            if (
                event_type
                == "session.input_transcript.delta"
            ):

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
            # USO
            # ------------------------------------------------

            if (
                event_type
                == "session.usage.updated"
            ):

                seconds = getattr(
                    event.usage,
                    "seconds",
                    None,
                )

                if seconds is not None:

                    usage_seconds = float(
                        seconds
                    )

                continue


            # ------------------------------------------------
            # DELEGACIÓN
            # ------------------------------------------------

            if (
                event_type
                == "session.delegation.created"
            ):

                target = getattr(
                    event.delegation,
                    "target",
                    "client",
                )

                console_message(
                    "\n"
                    f"[DELEGACIÓN: {target}]\n"
                    "Herramientas externas todavía "
                    "no están conectadas."
                )

                continue


            # ------------------------------------------------
            # INFO
            # ------------------------------------------------

            if event_type == "info":
                continue


            # ------------------------------------------------
            # ERROR DE API
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

                usage = getattr(
                    event,
                    "usage",
                    None,
                )

                final_seconds = (
                    getattr(
                        usage,
                        "seconds",
                        usage_seconds,
                    )
                    if usage is not None
                    else usage_seconds
                )


                console_message(
                    "\n".join(
                        [
                            "",
                            "=" * 60,
                            "SESIÓN LIVE FINALIZADA",
                            "=" * 60,
                            (
                                "Motivo: "
                                f"{getattr(event, 'reason', None)}"
                            ),
                            (
                                "Audio acumulado: "
                                f"{final_seconds} segundos"
                            ),
                        ]
                    )
                )

                stop_event.set()

                break


    except Exception as error:

        if not stop_event.is_set():

            console_message(
                "[ERROR recibiendo eventos]\n"
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
    print("JARVIS v0.2.1")
    print("GPT-Live + audio callback")
    print("=" * 60)
    print()


    # --------------------------------------------------------
    # AUDIO LOCAL
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
                "🎙️ Habla normalmente."
            )
            print(
                "🔊 Audio Live por callback."
            )
            print(
                "🛑 Ctrl+C para terminar."
            )
            print()


            # ------------------------------------------------
            # SALIDA: PORTAUDIO CALLBACK
            # ------------------------------------------------

            with sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=BLOCKSIZE,
                callback=playback.callback,
            ):


                # --------------------------------------------
                # ENVÍO DEL MICRÓFONO
                # --------------------------------------------

                sender_thread = threading.Thread(
                    target=microphone_sender,
                    args=(connection,),
                    daemon=True,
                )

                sender_thread.start()


                # --------------------------------------------
                # RECEPCIÓN DE GPT-LIVE
                # --------------------------------------------

                receiver_thread = threading.Thread(
                    target=receiver,
                    args=(connection,),
                    daemon=True,
                )

                receiver_thread.start()


                # --------------------------------------------
                # ENTRADA DE MICRÓFONO
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
            # CIERRE
            # ------------------------------------------------

            if not stop_event.is_set():
                stop_event.set()


            try:

                connection.session.close()

                time.sleep(
                    0.2
                )

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
PY