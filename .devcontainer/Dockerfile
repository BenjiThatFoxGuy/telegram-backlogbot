FROM mcr.microsoft.com/devcontainers/python:1-3.10-bullseye

# Keep system deps minimal. ffmpeg is used for media handling.
# Also install Tailscale for optional network access back to your tailnet.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Tailscale (Debian bullseye)
RUN set -eux; \
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bullseye.noarmor.gpg \
      -o /usr/share/keyrings/tailscale-archive-keyring.gpg; \
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bullseye.tailscale-keyring.list \
      -o /etc/apt/sources.list.d/tailscale.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends tailscale; \
    rm -rf /var/lib/apt/lists/*

# Install python dependencies (cached at image build time)
COPY requirements.txt /tmp/pip-tmp/
RUN pip install --no-cache-dir -r /tmp/pip-tmp/requirements.txt \
    && rm -rf /tmp/pip-tmp
