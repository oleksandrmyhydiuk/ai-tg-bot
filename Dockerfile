# ==========================================
# Етап 1: Builder (Встановлення залежностей)
# ==========================================
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# ==========================================
# Етап 2: Runtime (Фінальний легкий образ)
# ==========================================
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=Europe/Kyiv

WORKDIR /app

RUN groupadd -g 10001 botgroup && \
    useradd -u 10001 -g botgroup -s /sbin/nologin -M botuser && \
    mkdir -p /app/data && \
    chown -R botuser:botgroup /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=botuser:botgroup ./app /app/app

USER botuser

CMD ["python", "-m", "app.main"]
