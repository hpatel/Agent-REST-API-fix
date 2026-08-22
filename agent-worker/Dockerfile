FROM python:3.12-slim

# Git is required for repo cloning
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /service

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./agent/
COPY worker.py .
COPY config/ ./config/

RUN adduser --disabled-password --gecos "" appuser \
    && mkdir -p /tmp/autofix-clones \
    && chown appuser /tmp/autofix-clones

USER appuser

CMD ["python", "worker.py"]
