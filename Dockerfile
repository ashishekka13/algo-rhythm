# algo-rhythm player image. The player plays through the speakers of the machine it runs on, so run it on the venue
# machine: natively on macOS (Docker cannot reach Mac speakers), or in this image on Linux with --device /dev/snd.
# The same image runs the one-off jobs (import into the database, Gemini enrichment) on Cloud Run Jobs.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        rubberband-cli libsndfile1 libportaudio2 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY algo_rhythm ./algo_rhythm
COPY tools ./tools
RUN pip install --no-cache-dir . && useradd --create-home dj
USER dj

ENV LIBRARY_DIR=/data/library PORT=8765 HOST=0.0.0.0
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
CMD ["sh", "-c", "python -m algo_rhythm.server \"$LIBRARY_DIR\" --host \"$HOST\" --port \"$PORT\""]
