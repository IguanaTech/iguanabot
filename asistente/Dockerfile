FROM python:3.11-slim

# ffmpeg: transcodificar audio a OGG/Opus (notas de voz). libpango/cairo/gdk-pixbuf: que WeasyPrint
# renderice el PDF del reporte (HTML→PDF, sin navegador headless).
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg curl \
      libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libffi-dev fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Piper TTS: modelo de voz español (self-host, offline). Voz de RESPUESTA cuando el cliente la pide.
# Voz FEMENINA es_MX de alta calidad (`claude`, 1 hablante, 22kHz) — mucho más natural que la anterior
# es_MX-ald-medium (masculina, calidad media, sonaba robótica). El .onnx.json va al lado del .onnx
# (piper lo busca solo). Si aún se quiere una voz casi humana, ver _tts_polly/_tts_elevenlabs (neural,
# requieren cuenta): se prende cambiando TTS_MOTOR en .env.
RUN mkdir -p /voces \
    && curl -fsSL -o /voces/es_MX-claude-high.onnx \
       "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_MX/claude/high/es_MX-claude-high.onnx" \
    && curl -fsSL -o /voces/es_MX-claude-high.onnx.json \
       "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_MX/claude/high/es_MX-claude-high.onnx.json"

# Modelo de embeddings HORNEADO en la imagen (igual que la voz de Piper). Si se bajara en la
# primera consulta, el primer mensaje tras cada deploy pagaría ~220 MB de descarga y, sin red
# saliente, la memoria semántica quedaría apagada para siempre sin decir por qué.
ENV FASTEMBED_CACHE_PATH=/modelos
RUN mkdir -p /modelos && python -c "\
from fastembed import TextEmbedding; \
TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

# El código del paquete se monta bajo /app/asistente (ver COPY). Se corre como módulo.
COPY . /app/asistente

EXPOSE 8000
CMD ["uvicorn", "asistente.app:app", "--host", "0.0.0.0", "--port", "8000"]
