# Deployment & Production Hardening

This document outlines deployment options and production security recommendations.

---

## Docker Deployment (Recommended)

The repository provides a multi-stage, non-root Docker setup optimized for minimal footprint and security.

### 1. Build and Run via Docker Compose

```bash
docker compose up -d --build
```

### 2. Inspect Running Container & Logs

```bash
docker compose logs -f bot
```

### 3. File Permissions (Linux Bind-Mounts)

The container runs as non-root user `botuser` with UID `10001` and GID `10001`.
If using a bind-mount directory for `./data`, ensure ownership matches:

```bash
mkdir -p data
sudo chown -R 10001:10001 data
```

---

## Standalone Systemd Service (Linux Host)

To run the bot as a background systemd service on Ubuntu/Debian:

Create `/etc/systemd/system/ai-tg-bot.service`:

```ini
[Unit]
Description=AI Telegram Bot Service
After=network.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/opt/ai-tg-bot
EnvironmentFile=/opt/ai-tg-bot/.env
ExecStart=/opt/ai-tg-bot/venv/bin/python -m app.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable ai-tg-bot
sudo systemctl start ai-tg-bot
sudo systemctl status ai-tg-bot
```

---

## Production Security Checklist

- [x] Run container as non-root (`UID 10001`).
- [x] Never commit `.env` or sensitive API tokens to git.
- [x] Mount `./data` with restricted file permissions (`chmod 700`).
- [x] Dependency vulnerability scanning enabled via `pip-audit`.
- [x] Static application security testing (SAST) enabled via `bandit`.
