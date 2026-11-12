"""Configuration handling: /data/config.yaml, editable through the web UI."""
from __future__ import annotations

import copy
import os
import re
import secrets
import threading

import yaml

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.yaml")

DEFAULTS: dict = {
    "node_id": "metis",
    "device_name": "metis",
    "device_manufacturer": "Gigabyte",
    "device_model": "DGX Spark (NVIDIA GB10)",
    "public_url": os.environ.get("PUBLIC_URL", ""),   # e.g. http://<host>:8088 (HA configuration_url)
    "collect_interval": 5,
    "history_interval": 10,
    "history_days": 7,
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_unit": "ollama.service",
    "openwebui_url": "http://127.0.0.1:8080",
    "openwebui_db": "/owui/webui.db",
    "host_root": "/host",
    "disks": ["/"],
    "power": {"host": "", "channel": 0, "password": "", "price_eur_kwh": None},
    "ollama_models_dir": "/host/usr/share/ollama/.ollama/models",
    "voice_services": [
        {"key": "whisper", "name": "Whisper (STT)", "container": "wyoming-whisper", "host": "127.0.0.1", "port": 10300},
        {"key": "piper", "name": "Piper (TTS)", "container": "wyoming-piper", "host": "127.0.0.1", "port": 10200},
    ],
    "ping_targets": [
        {"key": "gateway", "name": "Router", "host": "gateway"},
        {"key": "internet", "name": "Internet", "host": "1.1.1.1"},
    ],
    "smart_file": "/data/smart.json",
    "brokers": [],
}

BROKER_DEFAULTS: dict = {
    "id": "",
    "name": "",
    "enabled": True,
    "host": "",
    "port": 1883,
    "username": "",
    "password": "",
    "tls": False,
    "tls_insecure": False,
    "base_topic": "metis",
    "discovery": True,
    "discovery_prefix": "homeassistant",
    "interval": 10,
}

_lock = threading.Lock()


def _normalize_broker(b: dict) -> dict:
    out = copy.deepcopy(BROKER_DEFAULTS)
    out.update({k: v for k, v in (b or {}).items() if k in BROKER_DEFAULTS})
    out["id"] = re.sub(r"[^a-zA-Z0-9_-]", "", str(out["id"] or "")) or secrets.token_hex(4)
    out["host"] = str(out["host"] or "").strip()
    out["name"] = str(out["name"] or out["host"]).strip()
    out["port"] = int(out["port"] or 1883)
    out["interval"] = max(1, int(out["interval"] or 10))
    out["username"] = str(out["username"] or "")
    out["password"] = str(out["password"] or "")
    out["base_topic"] = str(out["base_topic"] or "metis").strip().strip("/")
    out["discovery_prefix"] = str(out["discovery_prefix"] or "homeassistant").strip().strip("/")
    for k in ("enabled", "tls", "tls_insecure", "discovery"):
        out[k] = bool(out[k])
    return out


def validate_broker(b: dict) -> str | None:
    if not b["host"]:
        return "Host fehlt"
    if not (1 <= b["port"] <= 65535):
        return "Port ungültig"
    for k in ("base_topic", "discovery_prefix"):
        if not b[k] or any(c in b[k] for c in "#+ "):
            return f"{k} ungültig (keine Leerzeichen, # oder +)"
    return None


class Config:
    def __init__(self) -> None:
        self.data = self._load()

    def _load(self) -> dict:
        data = copy.deepcopy(DEFAULTS)
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            data.update({k: v for k, v in loaded.items() if k in DEFAULTS})
        data["brokers"] = [_normalize_broker(b) for b in data.get("brokers") or []]
        data["power"] = {**DEFAULTS["power"], **(data.get("power") or {})}
        if not os.path.exists(CONFIG_PATH):
            self._write(data)
        return data

    def _write(self, data: dict) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set_brokers(self, brokers: list[dict]) -> list[dict]:
        """Replace broker list. A missing/None password keeps the stored one."""
        with _lock:
            old = {b["id"]: b for b in self.data["brokers"]}
            new = []
            for raw in brokers:
                raw = dict(raw)
                prev = old.get(raw.get("id") or "")
                if raw.get("password") is None:
                    raw["password"] = prev["password"] if prev else ""
                b = _normalize_broker(raw)
                err = validate_broker(b)
                if err:
                    raise ValueError(f"{b['name'] or b['host'] or 'Broker'}: {err}")
                new.append(b)
            ids = [b["id"] for b in new]
            if len(ids) != len(set(ids)):
                raise ValueError("Doppelte Broker-ID")
            self.data["brokers"] = new
            self._write(self.data)
            return new

    def set_power(self, p: dict) -> dict:
        """Update Shelly settings. password None keeps the stored one."""
        with _lock:
            cur = self.data["power"]
            host = str(p.get("host") or "").strip()
            host = re.sub(r"^https?://", "", host).strip("/")
            if host and not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host):
                raise ValueError("Host ungültig")
            price = p.get("price_eur_kwh")
            if price in ("", None):
                price = None
            else:
                price = float(str(price).replace(",", "."))
                if not (0 <= price < 10):
                    raise ValueError("Strompreis ungültig")
            new = {
                "host": host,
                "channel": max(0, int(p.get("channel") or 0)),
                "password": cur["password"] if p.get("password") is None else str(p["password"]),
                "price_eur_kwh": price,
            }
            self.data["power"] = new
            self._write(self.data)
            return new

    def public_power(self) -> dict:
        p = self.data["power"]
        return {**{k: v for k, v in p.items() if k != "password"}, "has_password": bool(p.get("password"))}

    @staticmethod
    def public_broker(b: dict) -> dict:
        out = {k: v for k, v in b.items() if k != "password"}
        out["has_password"] = bool(b.get("password"))
        return out

    @staticmethod
    def normalize_broker(b: dict) -> dict:
        return _normalize_broker(b)
