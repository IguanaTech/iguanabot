"""Voz: entrada (nota de voz → texto con Whisper) y salida (texto → nota de voz con TTS).

Es una capa de I/O: el cerebro (LangGraph) sigue trabajando en texto. Opt-in por mensaje.
WhatsApp entrega/espera OGG/Opus para que se vea como nota de voz → transcodificamos con ffmpeg.
"""
import re
import subprocess
import sys
import tempfile
from functools import lru_cache

from .config import config


# ── ¿el cliente pidió la respuesta en VOZ? (on-demand) ────────────────────────
_FRASES_VOZ = (
    "en voz", "por voz", "de voz", "nota de voz", "en audio", "por audio", "un audio",
    "hablado", "hablada", "háblame", "hablame", "dímelo hablando", "dilo hablando",
    "respóndeme con voz", "contéstame con voz", "manda audio", "mándame audio",
    "mándame la voz", "mándamelo en voz", "responde en voz", "contesta en voz",
)


def pidio_voz(texto: str) -> bool:
    """True si el mensaje pide explícitamente la respuesta en nota de voz."""
    t = (texto or "").lower()
    return any(f in t for f in _FRASES_VOZ)


_FRASES_PDF = ("en pdf", "un pdf", "el pdf", "a pdf", "en documento", "un documento", "en un pdf",
               "generame un pdf", "genérame un pdf", "mándame un pdf", "mandame un pdf",
               "pásalo a pdf", "pasalo a pdf", "en archivo", "expórtalo", "exportalo", "en excel")


def pidio_pdf(texto: str) -> bool:
    """True si el mensaje pide la respuesta como documento PDF."""
    t = (texto or "").lower()
    return any(f in t for f in _FRASES_PDF)


def limpiar_para_voz(texto: str) -> str:
    """Prepara el texto para el TTS: quita markdown/emojis, arregla montos (el punto de miles se
    lee como decimal) y pone 'pesos' después del número. Así la voz suena natural."""
    t = texto or ""
    t = re.sub(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]", "", t)  # emojis
    t = re.sub(r"[*_`#>|]", " ", t)                       # símbolos markdown + pipes de tabla
    t = re.sub(r"(?m)^\s*[-•]\s*", "", t)                 # viñetas
    while re.search(r"\d\.\d{3}\b", t):                   # 137.925 → 137925 (evita lectura decimal)
        t = re.sub(r"(\d)\.(\d{3}\b)", r"\1\2", t)
    t = re.sub(r"RD\$\s*(\d+)", r"\1 pesos", t)           # RD$ 137925 → 137925 pesos
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ── Voz → texto (Whisper, self-host en CPU) ───────────────────────────────────
@lru_cache(maxsize=1)
def _whisper():
    from faster_whisper import WhisperModel  # import perezoso: solo si se usa voz
    # int8 en CPU: liviano y suficiente para español dominicano en notas de voz cortas.
    return WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")


def transcribir(audio_ogg: bytes) -> str:
    """Nota de voz de WhatsApp (OGG/Opus) → texto."""
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=True) as f:
        f.write(audio_ogg)
        f.flush()
        segmentos, _ = _whisper().transcribe(f.name, language="es")
        return " ".join(s.text for s in segmentos).strip()


# ── Texto → voz (TTS) ─────────────────────────────────────────────────────────
def sintetizar(texto: str) -> bytes:
    """Texto → nota de voz OGG/Opus lista para WhatsApp."""
    if config.TTS_MOTOR == "piper":
        return _tts_piper(texto)
    if config.TTS_MOTOR == "polly":
        return _tts_polly(texto)
    if config.TTS_MOTOR == "elevenlabs":
        return _tts_elevenlabs(texto)
    raise RuntimeError(f"TTS_MOTOR desconocido: {config.TTS_MOTOR}")


def _a_ogg_opus(wav_o_mp3: bytes, formato_entrada: str) -> bytes:
    """Transcodifica a OGG/Opus con ffmpeg (para que WhatsApp lo muestre como nota de voz)."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", formato_entrada, "-i", "pipe:0",
         "-c:a", "libopus", "-b:a", "24k", "-f", "ogg", "pipe:1"],
        input=wav_o_mp3, stdout=subprocess.PIPE, check=True,
    )
    return proc.stdout


def _tts_piper(texto: str) -> bytes:
    """Piper (self-host, offline): texto → WAV → OGG/Opus. El modelo es_MX está en el contenedor.
    CLI de piper-tts 1.5: `python -m piper -m <modelo> -f <salida.wav>`, texto por stdin."""
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        subprocess.run(
            [sys.executable, "-m", "piper", "-m", config.PIPER_MODEL, "-f", f.name],
            input=texto.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
        )
        f.seek(0)
        wav = f.read()
    return _a_ogg_opus(wav, "wav")


def _tts_polly(texto: str) -> bytes:
    import boto3  # import perezoso
    polly = boto3.client("polly")
    r = polly.synthesize_speech(Text=texto, OutputFormat="mp3", VoiceId="Mia", Engine="neural")
    return _a_ogg_opus(r["AudioStream"].read(), "mp3")


def _tts_elevenlabs(texto: str) -> bytes:
    import os
    import httpx
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "")
    key = os.getenv("ELEVENLABS_API_KEY", "")
    r = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": key},
        json={"text": texto, "model_id": "eleven_multilingual_v2"},
        timeout=30,
    )
    r.raise_for_status()
    return _a_ogg_opus(r.content, "mp3")
