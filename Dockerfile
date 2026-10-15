# algo-rhythm web server (Cloud Run): stateless; the host's browser plays and mixes. The server plans each join, which
# renders it with numpy and rubberband to check it, so rubberband-cli is the one system tool it needs.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends rubberband-cli libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY algo_rhythm ./algo_rhythm
RUN pip install --no-cache-dir . && useradd --create-home dj
# where the host is, for the MC ("What's up Bengaluru"): the free DB-IP City Lite database (CC BY 4.0, db-ip.com),
# fetched at build time; city and country only, and the address it is looked up from is not kept
COPY tools/fetch_geoip.py /tmp/fetch_geoip.py
RUN python /tmp/fetch_geoip.py /app/geo/dbip-city-lite.mmdb && rm /tmp/fetch_geoip.py
USER dj

# LIBRARY_DIR: where audio fetched from the bucket is cached (a Cloud Run instance's disk is its memory)
ENV LIBRARY_DIR=/tmp/library PORT=8080 PYTHONUNBUFFERED=1 GEOIP_DB=/app/geo/dbip-city-lite.mmdb
EXPOSE 8080
CMD exec uvicorn --factory algo_rhythm.server:asgi --host 0.0.0.0 --port "$PORT" \
    --ws-ping-interval 20 --ws-ping-timeout 20 --timeout-keep-alive 75 --proxy-headers --forwarded-allow-ips '*'
