FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY bot.py ./
RUN useradd --create-home --uid 10001 botuser \
    && mkdir -p /home/botuser/.cache/torch /home/botuser/.cache/demucs \
    && chown -R botuser:botuser /app /home/botuser
USER botuser

CMD ["python", "bot.py"]
