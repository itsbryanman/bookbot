# syntax=docker/dockerfile:1.4

FROM python:3.11-slim AS runtime

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system packages required for audio processing and build steps
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ffmpeg \
        libffi-dev \
        libssl-dev \
        git \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency definitions first for better caching
COPY pyproject.toml README.md /app/

# Install project dependencies (runtime + dev for testing)
RUN pip install --upgrade pip \
    && pip install .[dev]

# Copy the remainder of the source tree
COPY . /app

# Default command launches the CLI help; override as needed
ENTRYPOINT ["bookbot"]
CMD ["--help"]
