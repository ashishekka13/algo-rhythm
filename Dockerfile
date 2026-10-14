# algo-rhythm web server (Cloud Run): stateless; the host's browser plays and mixes. The server plans each join, which
# renders it with numpy and rubberband to check it, so rubberband-cli is the one system tool it needs.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends rubberband-cli libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY algo_rhythm ./algo_rhythm
RUN pip install --no-cache-dir . && useradd --create-home dj
USER dj

# LIBRARY_DIR: where audio fetched from the bucket is cached (a Cloud Run instance's disk is its memory)
ENV LIBRARY_DIR=/tmp/library PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD exec uvicorn --factory algo_rhythm.server:asgi --host 0.0.0.0 --port "$PORT" \
    --ws-ping-interval 20 --ws-ping-timeout 20 --timeout-keep-alive 75 --proxy-headers --forwarded-allow-ips '*'
