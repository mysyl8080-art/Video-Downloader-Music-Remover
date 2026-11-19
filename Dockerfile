FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CUDA_VISIBLE_DEVICES="" \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1

WORKDIR /app

# CPU-only PyTorch/torchaudio are pinned as a compatible pair.
# The default htdemucs model does not require the optional DiffQ extension.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && pip install --upgrade pip setuptools wheel \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch==2.4.1 torchaudio==2.4.1 \
    && pip install "numpy<2" \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements before installing them (required for Docker build context).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py ./

RUN useradd --create-home --uid 10001 botuser \
    && mkdir -p /home/botuser/.cache/torch \
    && chown -R botuser:botuser /app /home/botuser

ENV TORCH_HOME=/home/botuser/.cache/torch \
    XDG_CACHE_HOME=/home/botuser/.cache

USER botuser

CMD ["python", "bot.py"]
