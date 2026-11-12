FROM python:3.12-slim

# journalctl: Ollama log, errors, SSH, OOM; tzdata: local day boundaries for energy; ping: network latency
RUN apt-get update \
 && apt-get install -y --no-install-recommends systemd tzdata iputils-ping \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/

ENV DATA_DIR=/data PORT=8088 PYTHONUNBUFFERED=1
EXPOSE 8088
CMD ["python", "-m", "app.main"]
