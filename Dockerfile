# syntax=docker/dockerfile:1.4

# ---- Build stage ----
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /build/
COPY bookbot /build/bookbot

RUN pip install --upgrade pip \
    && pip install --prefix=/install .

# ---- Runtime stage ----
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="BookBot" \
      org.opencontainers.image.description="A cross-platform TUI audiobook renamer and organizer" \
      org.opencontainers.image.version="0.3.0" \
      org.opencontainers.image.authors="itsbryanman" \
      org.opencontainers.image.url="https://github.com/itsbryanman/BookBot" \
      org.opencontainers.image.source="https://github.com/itsbryanman/BookBot" \
      org.opencontainers.image.licenses="MIT"

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy source for reference (scripts, configs, etc.)
COPY . /app

ENTRYPOINT ["bookbot"]
CMD ["--help"]
