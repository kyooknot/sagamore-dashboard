# Sagamore — a state dashboard for a house.
#
# 🚧 UNVERIFIED: this image has never been built or run. It is derived from a
# working systemd deployment and checked only for internal consistency — the
# paths it COPY's exist, and docker-compose.yml is valid YAML. The most likely
# failure is bind-mount ownership: see "If Docker fails" in the README.
# Bug reports very welcome; they are the only way this gets verified.
#
# Build:  docker build -t sagamore .
# Run:    docker compose up -d      (see docker-compose.yml)
#
# Two mount points matter:
#   /config   your sagamore.yaml lives here — edit it and restart
#   /data     the SQLite file lives here — this is the only state

FROM python:3.13-slim

# No build step, no compilers needed: this is an HTTP client with a SQLite file.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_FILE=/config/sagamore.yaml \
    DB_PATH=/data/sagamore.db

WORKDIR /app

# Dependencies first, so edits to the app don't invalidate the wheel cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as a non-root user. The image creates the mount points so a bind mount
# from the host inherits sane ownership even on first run.
RUN useradd --system --uid 10001 --create-home --home-dir /home/sagamore sagamore \
    && mkdir -p /config /data \
    && chown -R sagamore:sagamore /config /data /app

USER sagamore
EXPOSE 8092

# A dead dashboard should fail its healthcheck rather than sit there looking fine
# — the same principle the app applies to sensors.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8092/api/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8092"]
