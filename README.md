# telegram-backlogbot

A small, standalone Telegram backlog poster.

It scans one or more folders for media files and posts them to configured Telegram targets at a fixed cadence (per-target or global). State is stored in MongoDB.

## Docker

Build locally:

```bash
docker build -t backlogbot .
```

Run (example):

```bash
docker run --rm \
  -e TG_API_ID=... \
  -e TG_API_HASH=... \
  -e MONGO_URL=mongodb://mongo:27017 \
  -e BACKLOG_ROOT=/backlog \
  -e BACKLOG_TARGETS=@mychannel \
  -v /path/to/backlog:/backlog \
  backlogbot
```

## Codespaces (development)

This repo includes a devcontainer configuration under `.devcontainer/` that works in GitHub Codespaces (and locally with the Dev Containers extension).

What you get:

- A development container with Python deps + `ffmpeg` installed.
- A MongoDB service via `docker-compose` (the devcontainer defaults `MONGO_URL` to `mongodb://mongo:27017`).
- Optional Tailscale installed and auto-connected on start **if** you provide a Codespaces secret `TAILSCALE_AUTHKEY` (recommend using an ephemeral key).

After creating/rebuilding the Codespace, run the bot from the integrated terminal:

```bash
python backlogbot.py
```

## Configuration (environment variables)

### Required

- `TG_API_ID` / `TG_API_HASH`: Telegram API credentials.
- `MONGO_URL`: Mongo connection string.

### Backlog input/output

- `BACKLOG_ENABLE` (default: `true`)
- `BACKLOG_ROOT` (default: `/backlog`)
- `BACKLOG_ROOT_1..BACKLOG_ROOT_50` (optional): if set, these override `BACKLOG_ROOT` and allow multiple input roots.
- `BACKLOG_ARCHIVE_ROOT` (default: `/backlog_archive`)
- `BACKLOG_TARGETS` (default: empty): comma-separated allowlist of targets.

### Timing / cadence

- `BACKLOG_SCAN_EVERY_SECONDS` (default: `30`)
- `BACKLOG_SETTLE_SECONDS` (default: `30`)
- `BACKLOG_INTERVAL_SECONDS` (default: `21600`)
- `BACKLOG_SCOPE` (default: `per_target`, allowed: `per_target|global`)
- `BACKLOG_OVERDUE` (default: `post_once`, allowed: `post_once|wait`)

### Post behavior

- `BACKLOG_SUCCESS_ACTION` (default: `delete`, allowed: `delete|archive`)
- `BACKLOG_ALLOW_UNKNOWN_AS_DOCUMENT` (default: `false`)
- `BACKLOG_SKIP_QUARANTINE_UNMAPPED_TARGETS` (default: `false`)
- `BACKLOG_IMMEDIATE_POST_ON_START` (default: `false`)

### Telegram scheduler mode (optional)

- `BACKLOG_USE_TELEGRAM_SCHEDULER` (default: `false`)
- `BACKLOG_SCHEDULER_MODE` (default: `fixed_cadence`, allowed: `fixed_cadence|legacy`)
- `BACKLOG_SCHEDULE_AHEAD` (default: `7d`) — only used when scheduler is enabled.
- `BACKLOG_MIN_SCHEDULE_DELAY_SECONDS` (default: `120`)

### Reliability

- `BACKLOG_MAX_FAILURES` (default: `5`)

### Logging

- `BACKLOG_LOG_LEVEL` (default: `INFO`)
- `BACKLOG_LIB_LOG_LEVEL` (default: `WARNING`)

### Timezone

- `TZ` (default: `Europe/Warsaw`)

## License

MIT.
