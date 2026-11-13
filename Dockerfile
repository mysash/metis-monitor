# Base image pinned by digest (update deliberately: docker pull python:3.12-slim, take the new RepoDigest)
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# journalctl: Ollama log, errors, SSH, OOM; tzdata: local day boundaries for energy; ping: network latency
RUN apt-get update \
 && apt-get install -y --no-install-recommends systemd tzdata iputils-ping \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY app/ ./app/

ENV DATA_DIR=/data PORT=8088 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8088
CMD ["python", "-m", "app.main"]
