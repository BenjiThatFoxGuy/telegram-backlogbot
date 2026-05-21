FROM python:3.10-slim

WORKDIR /app

# Keep system deps minimal. ffmpeg is used for media handling.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      ffmpeg \
    ; \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip setuptools wheel; \
    python -m pip install -r requirements.txt

COPY backlogbot.py ./
COPY async_pymongo.py ./

CMD ["python", "backlogbot.py"]
