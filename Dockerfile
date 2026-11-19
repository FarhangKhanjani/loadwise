# LoadWise: API + Telegram bot + 08:00 daily check in one process (python -m src.main).
FROM python:3.12-slim

# No .pyc files in the image; logs appear immediately in `docker compose logs`.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: this layer is cached and only rebuilt when requirements.txt changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ src/
# The synthetic sample data, for trying it out with --db data/demo.db (real data is mounted).
COPY data/sample_sessions.csv data/sample_wellness.csv data/

# Run as a normal user, not root: a bug in the app can't change the container's system files.
RUN useradd --create-home --uid 1000 loadwise \
    && mkdir -p /app/data /app/logs \
    && chown -R loadwise:loadwise /app
USER loadwise

# Inside the container the API must listen on all interfaces to be reachable at all.
# docker-compose.yml publishes the port on 127.0.0.1 only, so it is still local to your PC.
ENV API_HOST=0.0.0.0 \
    API_PORT=8000 \
    DB_PATH=/app/data/loadwise.db
EXPOSE 8000

# The slim image has no curl, so the health check uses Python's standard library.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "src.main"]
