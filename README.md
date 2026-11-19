# Video Downloader & Music Remover Bot

Telegram bot for downloading publicly accessible videos with **yt-dlp** and removing background music from uploaded videos using **Demucs** while retaining the vocal stem.

## Features

- Download supported public video URLs through yt-dlp.
- Upload a video to Telegram and run `/remove` to isolate vocals.
- FFmpeg handles audio extraction and remuxing.
- Docker-based deployment, including Railway configuration.
- Secrets are supplied through environment variables.

## Commands

- `/start` — show instructions.
- `/download <url>` — download a video.
- Send a video, then use `/remove` — create a version with the isolated vocal stem.

You can also send a supported URL as a normal text message.

## Local setup

Requirements: Python 3.11+, FFmpeg, and a Telegram bot token from BotFather.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `BOT_TOKEN` in your environment (or configure your deployment variables), then run:

```bash
python bot.py
```

## Docker

```bash
docker build -t video-downloader-music-remover .
docker run --rm -e BOT_TOKEN="YOUR_TOKEN" video-downloader-music-remover
```

## Railway deployment

1. Create a Railway project and deploy this GitHub repository.
2. Railway detects the Dockerfile automatically.
3. Add `BOT_TOKEN` in the service Variables tab.
4. Deploy and inspect the service logs.

Other optional variables are documented in `.env.example`.

## Important notes

- Only download content you own or are authorized to download, and respect each platform's terms and applicable copyright law.
- The bot does not bypass private access controls, DRM, or authentication restrictions.
- Demucs is compute-intensive; first-time startup may download model weights and processing can take several minutes.
- Vocal isolation is AI-based and may leave musical artifacts. It isolates the vocal stem; it cannot perfectly reconstruct every original voice.
- Telegram upload/download limits depend on the Bot API and deployment configuration. Large files may fail.
- Never commit `.env`, bot tokens, cookies, or other credentials.

## License

Choose and add a license before distributing this project.
