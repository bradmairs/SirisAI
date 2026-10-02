FROM python:3.12-slim

WORKDIR /app

# ssh client for the cron overview (app/cron/), which reads other machines'
# crontabs over plain `ssh`; ffmpeg for the optional TTS effect
# (SIRISAI_TTS_EFFECT, app/voice/effects.py); git for the second brain's
# optional sync (SIRISAI_BRAIN_GIT_SYNC, app/second_brain/).
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Speaker recognition weights (app/voice/speaker.py), converted to numpy at
# build time -- under /app/models, not /app/data, which is a mounted volume.
RUN python scripts/fetch_voice_encoder.py /app/models/voice_encoder.npz

RUN mkdir -p /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health').read()"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
