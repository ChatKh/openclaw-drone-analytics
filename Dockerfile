# One image for every service (API, pipeline, dashboard, agent CLI).
# Python 3.10 on purpose: the same version as the development machine and CI.
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: this layer is reused until requirements.txt changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pytest.ini ./
COPY src ./src
COPY bin ./bin
COPY tests ./tests

# Run as an ordinary user. /data is the shared volume (database, CSV, model, logs).
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown app:app /data
USER app

ENV DRONE_DB_PATH=/data/drone.db \
    FLEET_AUDIT_LOG=/data/logs/agent_queries.jsonl

EXPOSE 8000 8501
CMD ["python", "-m", "src.pipeline", "--help"]
