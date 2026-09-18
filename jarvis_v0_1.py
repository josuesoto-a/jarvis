from pathlib import Path
import os
import wave

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from openai import OpenAI


# ============================================================
# CONFIGURACIÓN
# ============================================================

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"

INPUT_AUDIO_FILE = BASE_DIR / "user_audio.wav"
OUTPUT_AUDIO_FILE = BASE_DIR / "jarvis_reply.wav"

SAMPLE_RATE = 44100
CHANNELS = 1
RECORD_SECONDS = 5

TEXT_MODEL = "gpt-5.6-luna"
TRANSCRIPTION_MODEL = "gpt-transcribe"
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "coral"


# ============================================================
# CARGAR API KEY
# ============================================================

load_dotenv(dotenv_path=ENV_FILE)

if not os.getenv("OPENAI_API_KEY"):
    raise RuntimeError(
        "No se encontró OPENAI_API_KEY en el archivo .env"
    )

client = OpenAI()


# ============================================================
# PERSONALIDAD INICIAL DE JARVIS
# ============================================================

SYSTEM_INSTRUCTIONS = """
Tu nombre es Jarvis.

Eres un asistente personal de voz ejecutándose desde la computadora
del usuario.

Reglas:

- Responde principalmente en español.
- Sé directo, natural y preciso.
- Tus respuestas habladas deben ser relativamente breves.
- No inventes información.
- Si no entiendes una instrucción importante, pregunta.
- No afirmes haber realizado acciones que todavía no puedes realizar.
- Actualmente puedes escuchar al usuario y responderle por voz.
"""


# ============================================================
# GRABACIÓN DEL MICRÓFONO
# ============================================================

def record_audio() -> None:
    print()
    print(f"🎙️ Escuchando durante {RECORD_SECONDS} segundos...")

    frames = int(RECORD_SECONDS * SAMPLE_RATE)

    recording = sd.rec(
        frames,
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=np.int16,
    )

    sd.wait()

    with wave.open(str(INPUT_AUDIO_FILE), "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(recording.tobytes())

    print("✅ Audio capturado.")


# ============================================================
# TRANSCRIPCIÓN
# ============================================================

def transcribe_audio() -> str:
    print("📝 Transcribiendo...")

    with open(INPUT_AUDIO_FILE, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model=TRANSCRIPTION_MODEL,
            file=audio_file,
        )

    return transcription.text.strip()


# ============================================================
# CEREBRO
# ============================================================

def ask_jarvis(message: str) -> str:
    print("🧠 Pensando...")

    response = client.responses.create(
        model=TEXT_MODEL,
        instructions=SYSTEM_INSTRUCTIONS,
        input=message,
    )

    return response.output_text.strip()


# ============================================================
# GENERACIÓN DE VOZ
# ============================================================

def generate_speech(text: str) -> None:
    print("🔊 Generando voz...")

    with client.audio.speech.with_streaming_response.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=text,
        instructions=(
            "Habla en español con una voz natural, tranquila, "
            "segura y clara. Ritmo conversacional."
        ),
        response_format="wav",
    ) as response:
        response.stream_to_file(OUTPUT_AUDIO_FILE)


# ============================================================
# REPRODUCCIÓN DE VOZ
# ============================================================

def play_speech() -> None:
    with wave.open(str(OUTPUT_AUDIO_FILE), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_rate = wav_file.getframerate()
        sample_width = wav_file.getsampwidth()
        frames = wav_file.readframes(wav_file.getnframes())

    if sample_width != 2:
        raise RuntimeError(
            f"Formato de audio inesperado: {sample_width * 8} bits"
        )

    audio = np.frombuffer(
        frames,
        dtype=np.int16,
    )

    if channels > 1:
        audio = audio.reshape(-1, channels)

    print("🗣️ Jarvis hablando...")

    sd.play(
        audio,
        sample_rate,
    )

    sd.wait()


# ============================================================
# CICLO PRINCIPAL
# ============================================================

def main() -> None:
    print("=" * 60)
    print("JARVIS v0.1")
    print("Asistente personal por voz")
    print("=" * 60)

    print()
    print("ENTER  -> hablar con Jarvis")
    print("s      -> salir")
    print()
    print("La voz que escucharás es generada por inteligencia artificial.")

    while True:

        command = input(
            "\n[ENTER = hablar | s = salir] > "
        ).strip().lower()

        if command in {"s", "salir", "exit", "quit"}:
            print("\nJarvis > Cerrando sistema.")
            break

        try:
            # 1. Escuchar
            record_audio()

            # 2. Convertir voz a texto
            transcript = transcribe_audio()

            if not transcript:
                print("No detecté ninguna frase.")
                continue

            print()
            print(f"Tú > {transcript}")

            # 3. Pensar
            answer = ask_jarvis(transcript)

            print()
            print(f"Jarvis > {answer}")

            # 4. Generar voz
            generate_speech(answer)

            # 5. Hablar
            play_speech()

        except KeyboardInterrupt:
            print("\n\nJarvis > Cerrando sistema.")
            break

        except Exception as error:
            print()
            print("[JARVIS ERROR]")
            print(error)


if __name__ == "__main__":
    main()
