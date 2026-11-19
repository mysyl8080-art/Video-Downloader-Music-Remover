FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CUDA_VISIBLE_DEVICES=""

WORKDIR /app

# FFmpeg is required at runtime. gcc is needed only to compile DiffQ's
# small native extension, which Demucs' quantized mdx_q model requires.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates gcc \
    && pip install --upgrade pip \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch torchaudio \
    && pip install -r requirements.txt \
    && apt-get purge -y --auto-remove gcc \
    && rm -rf /var/lib/apt/lists/*

COPY bot.py ./
RUN useradd --create-home --uid 10001 botuser \
    && mkdir -p /home/botuser/.cache/torch /home/botuser/.cache/demucs \
    && chown -R botuser:botuser /app /home/botuser
USER botuser

CMD ["python", "bot.py"]
