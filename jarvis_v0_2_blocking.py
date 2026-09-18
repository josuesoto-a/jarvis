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

# 20 ms de audio.
BLOCK_MS = 20
BLOCKSIZE = int(SAMPLE_RATE * BLOCK_MS / 1000)


SYSTEM_INSTRUCTIONS = """
Tu nombre es Jarvis.

Eres un asistente personal de voz ejecutándose en la computadora
del usuario.

Esta es tu versión de conversación Live.

Comportamiento:

- Habla principalmente en español.
- Mantén una conversación natural.
- Sé directo, preciso y relativamente breve.
- Escucha atentamente al usuario.
- El usuario puede interrumpirte mientras hablas.
- Si te interrumpe, atiende inmediatamente su nueva intervención.
- Tolera pausas naturales al hablar.
- Si no entiendes algo importante, pregunta.
- No inventes información.
- No afirmes tener memoria persistente todavía.
- No afirmes controlar la computadora todavía.
- No afirmes ejecutar herramientas todavía.
- No delegues tareas externas en esta versión.
- Para conversación normal, responde tú mismo.
"""


# ============================================================
# ESTADO
# ============================================================

stop_event = threading.Event()

input_audio_queue = queue.Queue(maxsize=500)
output_audio_queue = queue.Queue(maxsize=1000)

console_lock = threading.Lock()

current_console_speaker = None

usage_seconds = 0.0


# ============================================================
# CONSOLA
# ============================================================

def print_transcript(speaker: str, text: str) -> None:
    global current_console_speaker

    if not text:
        return

    with console_lock:

        if current_console_speaker != speaker:

            print()

            if speaker == "user":
                print("Tú > ", end="", flush=True)

            elif speaker == "jarvis":
                print("Jarvis > ", end="", flush=True)

            current_console_speaker = speaker

        print(text, end="", flush=True)


def console_message(message: str) -> None:
    global current_console_speaker

    with console_lock:
        print()
        print(message, flush=True)
        current_console_speaker = None


# ============================================================
# MICRÓFONO
# ============================================================

def microphone_callback(indata, frames, time_info, status) -> None:

    if status:
        console_message(f"[MICRÓFONO] {status}")

    if stop_event.is_set():
        return

    try:
        input_audio_queue.put_nowait(bytes(indata))

    except queue.Full:
        # En tiempo real es mejor descartar un bloque viejo
        # que detener todo el micrófono.
        pass


# ============================================================
# ENVÍO DE AUDIO A GPT-LIVE
# ============================================================

def microphone_sender(connection) -> None:

    while not stop_event.is_set():

        try:
            audio_chunk = input_audio_queue.get(timeout=0.1)

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
                    f"[ERROR enviando audio]\n{error}"
                )

            stop_event.set()
            break


# ============================================================
# REPRODUCCIÓN DE AUDIO
# ============================================================

def audio_player(output_stream) -> None:

    while not stop_event.is_set():

        try:
            audio_chunk = output_audio_queue.get(
                timeout=0.1
            )

        except queue.Empty:
            continue

        if audio_chunk is None:
            break

        try:
            output_stream.write(audio_chunk)

        except Exception as error:

            if not stop_event.is_set():
                console_message(
                    f"[ERROR reproduciendo audio]\n{error}"
                )

            stop_event.set()
            break


# ============================================================
# EVENTOS RECIBIDOS
# ============================================================

def receiver(connection) -> None:
    global usage_seconds

    try:

        while not stop_event.is_set():

            event = connection.recv()

            event_type = event.type


            # ------------------------------------------------
            # AUDIO GENERADO POR JARVIS
            # ------------------------------------------------

            if event_type == "session.output_audio.delta":

                raw_audio = base64.b64decode(
                    event.delta
                )

                try:
                    output_audio_queue.put(
                        raw_audio,
                        timeout=0.5,
                    )

                except queue.Full:
                    console_message(
                        "[ADVERTENCIA] Buffer de salida lleno."
                    )

                continue


            # ------------------------------------------------
            # TRANSCRIPCIÓN DE LO QUE DICES
            # ------------------------------------------------

            if event_type == "session.input_transcript.delta":

                print_transcript(
                    "user",
                    event.delta,
                )

                continue


            # ------------------------------------------------
            # TRANSCRIPCIÓN DE JARVIS
            # ------------------------------------------------

            if event_type == "session.output_transcript.delta":

                print_transcript(
                    "jarvis",
                    event.delta,
                )

                continue


            # ------------------------------------------------
            # CONSUMO
            # ------------------------------------------------

            if event_type == "session.usage.updated":

                seconds = getattr(
                    event.usage,
                    "seconds",
                    None,
                )

                if seconds is not None:
                    usage_seconds = float(seconds)

                continue


            # ------------------------------------------------
            # DELEGACIÓN
            # ------------------------------------------------

            if event_type == "session.delegation.created":

                target = getattr(
                    event.delegation,
                    "target",
                    "desconocido",
                )

                console_message(
                    f"[DELEGACIÓN solicitada: {target}]\n"
                    "Todavía no conectamos herramientas en v0.2."
                )

                continue


            # ------------------------------------------------
            # INFORMACIÓN
            # ------------------------------------------------

            if event_type == "info":
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
                            f"Tipo: {getattr(error, 'type', None)}",
                            f"Código: {getattr(error, 'code', None)}",
                            f"Mensaje: {getattr(error, 'message', None)}",
                        ]
                    )
                )

                stop_event.set()
                break


            # ------------------------------------------------
            # SESIÓN CERRADA
            # ------------------------------------------------

            if event_type == "session.closed":

                final_seconds = getattr(
                    event.usage,
                    "seconds",
                    usage_seconds,
                )

                console_message(
                    "\n".join(
                        [
                            "",
                            "=" * 60,
                            "SESIÓN LIVE FINALIZADA",
                            "=" * 60,
                            f"Motivo: {event.reason}",
                            f"Audio acumulado: {final_seconds} segundos",
                        ]
                    )
                )

                stop_event.set()
                break


    except Exception as error:

        if not stop_event.is_set():

            console_message(
                f"[ERROR recibiendo eventos]\n{error}"
            )

            stop_event.set()


# ============================================================
# ESPERAR SESSION.STARTED
# ============================================================

def wait_until_started(connection):

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

    global current_console_speaker

    print("=" * 60)
    print("JARVIS v0.2")
    print("Conversación GPT-Live full-duplex")
    print("=" * 60)
    print()

    print("Python Live API + audio PCM 24 kHz")
    print()

    # --------------------------------------------------------
    # VALIDAR AUDIO LOCAL
    # --------------------------------------------------------

    print("Comprobando dispositivos de audio...")

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


    client = OpenAI(
        api_key=API_KEY
    )


    try:

        # ----------------------------------------------------
        # CONEXIÓN LIVE
        # ----------------------------------------------------

        print("Conectando con GPT-Live-1...")

        with client.live.connect() as connection:

            connection.session.start(
                session={
                    "model": MODEL,

                    "instructions": SYSTEM_INSTRUCTIONS,

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


            # ------------------------------------------------
            # ESPERAR CONFIRMACIÓN DEL SERVIDOR
            # ------------------------------------------------

            session = wait_until_started(connection)

            print()
            print("=" * 60)
            print("JARVIS LIVE ACTIVO")
            print("=" * 60)
            print(f"Session ID: {session.id}")
            print(f"Modelo: {session.model}")
            print()
            print("🎙️ Habla normalmente.")
            print("🔊 Jarvis responderá en streaming.")
            print("🛑 Ctrl+C para terminar.")
            print()


            # ------------------------------------------------
            # STREAM DE SALIDA
            # ------------------------------------------------

            with sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=BLOCKSIZE,
            ) as output_stream:


                # --------------------------------------------
                # THREAD: REPRODUCCIÓN
                # --------------------------------------------

                player_thread = threading.Thread(
                    target=audio_player,
                    args=(output_stream,),
                    daemon=True,
                )

                player_thread.start()


                # --------------------------------------------
                # THREAD: ENVÍO MICRÓFONO
                # --------------------------------------------

                sender_thread = threading.Thread(
                    target=microphone_sender,
                    args=(connection,),
                    daemon=True,
                )

                sender_thread.start()


                # --------------------------------------------
                # THREAD: RECEPCIÓN OPENAI
                # --------------------------------------------

                receiver_thread = threading.Thread(
                    target=receiver,
                    args=(connection,),
                    daemon=True,
                )

                receiver_thread.start()


                # --------------------------------------------
                # MICRÓFONO CONTINUO
                # --------------------------------------------

                with sd.RawInputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype=DTYPE,
                    blocksize=BLOCKSIZE,
                    callback=microphone_callback,
                ):

                    while not stop_event.is_set():
                        time.sleep(0.05)


            # ------------------------------------------------
            # CIERRE LIVE
            # ------------------------------------------------

            if not stop_event.is_set():
                stop_event.set()

            try:
                connection.session.close()

                # Permitimos recibir session.closed.
                time.sleep(0.3)

            except Exception:
                pass


    except KeyboardInterrupt:

        print()
        print()

        console_message(
            "Cerrando Jarvis..."
        )

        stop_event.set()


    except Exception as error:

        console_message(
            f"[JARVIS ERROR]\n{error}"
        )

        stop_event.set()


    finally:

        try:
            input_audio_queue.put_nowait(None)
        except queue.Full:
            pass

        try:
            output_audio_queue.put_nowait(None)
        except queue.Full:
            pass

        print()
        print("Jarvis cerrado.")


if __name__ == "__main__":
    main()
