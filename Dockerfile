FROM python:3.11-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cache dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application and worker source code
COPY app/ ./app/
COPY worker/ ./worker/

# Ensure transit directory exists with proper permissions
RUN mkdir -p /tmp/riva_transit && chmod 777 /tmp/riva_transit

ENV PYTHONPATH=/app

EXPOSE 8000
