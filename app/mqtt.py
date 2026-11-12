"""MQTT publishing to any number of brokers, incl. Home Assistant discovery."""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time

import paho.mqtt.client as mqtt

from .sensors import SENSORS

log = logging.getLogger("mqtt")
VERSION = "1.1.0"


def _make_client(b: dict, client_id: str) -> mqtt.Client:
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=True)
    if b.get("username"):
        c.username_pw_set(b["username"], b.get("password") or None)
    if b.get("tls"):
        if b.get("tls_insecure"):
            c.tls_set(cert_reqs=ssl.CERT_NONE)
            c.tls_insecure_set(True)
        else:
            c.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    return c


class BrokerPublisher:
    def __init__(self, b: dict, cfg) -> None:
        self.b = b
        self.cfg = cfg
        self.base = b["base_topic"]
        self.connected = False
        self.last_error: str | None = None
        self.last_publish: float | None = None
        self.last_connect: float | None = None
        self.sent = 0
        self._last_state = 0.0
        node = cfg["node_id"]
        self.client = _make_client(b, f"{node}-monitor-{b['id']}")
        self.client.will_set(f"{self.base}/status", "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.reconnect_delay_set(1, 60)

    # -- lifecycle
    def start(self) -> None:
        try:
            self.client.connect_async(self.b["host"], self.b["port"], keepalive=60)
            self.client.loop_start()
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            log.warning("[%s] start failed: %s", self.b["name"], e)

    def stop(self) -> None:
        try:
            if self.connected:
                self.client.publish(f"{self.base}/status", "offline", qos=1, retain=True).wait_for_publish(2)
            self.client.disconnect()
        except Exception:
            pass
        self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            self.connected = False
            self.last_error = f"Verbindung abgelehnt: {reason_code}"
            log.warning("[%s] %s", self.b["name"], self.last_error)
            return
        self.connected = True
        self.last_error = None
        self.last_connect = time.time()
        log.info("[%s] verbunden mit %s:%s", self.b["name"], self.b["host"], self.b["port"])
        client.publish(f"{self.base}/status", "online", qos=1, retain=True)
        if self.b.get("discovery"):
            self.publish_discovery()
        self._last_state = 0.0

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected = False
        if reason_code and reason_code.is_failure:
            self.last_error = f"Getrennt: {reason_code}"

    # -- discovery
    def _device(self) -> dict:
        dev = {
            "identifiers": [self.cfg["node_id"]],
            "name": self.cfg["device_name"],
            "manufacturer": self.cfg["device_manufacturer"],
            "model": self.cfg["device_model"],
        }
        if self.cfg.get("public_url"):
            dev["configuration_url"] = self.cfg["public_url"]
        return dev

    def publish_discovery(self) -> None:
        node = self.cfg["node_id"]
        prefix = self.b["discovery_prefix"]
        dev = self._device()
        for s in SENSORS:
            comp, key = s["component"], s["key"]
            p = {
                "name": s["name"],
                "unique_id": f"{node}_{key}",
                "default_entity_id": f"{comp}.{node}_{key}",
                "state_topic": f"{self.base}/state",
                "availability_topic": f"{self.base}/status",
                "device": dev,
                "origin": {"name": "metis-monitor", "sw_version": VERSION},
            }
            if comp == "binary_sensor":
                p["value_template"] = "{{ 'ON' if value_json.get('%s') else 'OFF' }}" % key
            else:
                p["value_template"] = "{{ value_json.get('%s') }}" % key
            for src, dst in (("unit", "unit_of_measurement"), ("device_class", "device_class"),
                             ("state_class", "state_class"), ("icon", "icon"),
                             ("category", "entity_category"), ("precision", "suggested_display_precision")):
                if s.get(src) is not None:
                    p[dst] = s[src]
            if s.get("attributes"):
                p["json_attributes_topic"] = f"{self.base}/{s['attributes']}"
            self.client.publish(f"{prefix}/{comp}/{node}/{key}/config", json.dumps(p), qos=1, retain=True)

    # -- state
    def maybe_publish(self, flat: dict, snapshot: dict, force: bool = False) -> None:
        if not self.connected:
            return
        now = time.time()
        if not force and now - self._last_state < self.b["interval"] - 0.5:
            return
        self._last_state = now
        try:
            self.client.publish(f"{self.base}/state", json.dumps(flat), qos=0, retain=True)
            ol = snapshot.get("ollama", {})
            self.client.publish(f"{self.base}/ollama", json.dumps({
                "loaded": [{k: m.get(k) for k in ("name", "size_vram", "expires_at", "context_length")}
                           for m in ol.get("loaded", [])],
                "installed": [m.get("name") for m in ol.get("installed", [])],
                "version": ol.get("version"),
            }), qos=0, retain=True)
            probs = snapshot.get("problems") or []
            self.client.publish(f"{self.base}/problems", json.dumps({"problems": probs, "count": len(probs)}),
                                qos=0, retain=True)
            self.sent += 3
            self.last_publish = now
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)

    def status(self) -> dict:
        return {
            "id": self.b["id"], "connected": self.connected, "last_error": self.last_error,
            "last_publish": self.last_publish, "last_connect": self.last_connect, "sent": self.sent,
            "state_topic": f"{self.base}/state",
        }


class MqttManager:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.lock = threading.Lock()
        self.pubs: dict[str, BrokerPublisher] = {}

    def apply(self) -> None:
        with self.lock:
            for p in self.pubs.values():
                p.stop()
            self.pubs = {}
            for b in self.cfg["brokers"]:
                if b["enabled"]:
                    p = BrokerPublisher(b, self.cfg)
                    p.start()
                    self.pubs[b["id"]] = p

    def publish(self, flat: dict, snapshot: dict) -> None:
        with self.lock:
            for p in self.pubs.values():
                p.maybe_publish(flat, snapshot)

    def status(self) -> dict:
        with self.lock:
            return {bid: p.status() for bid, p in self.pubs.items()}

    def shutdown(self) -> None:
        with self.lock:
            for p in self.pubs.values():
                p.stop()
            self.pubs = {}


def test_connection(b: dict, timeout: float = 6.0) -> dict:
    """Synchronous one-shot connection test."""
    result = {"ok": False, "message": "Timeout"}
    done = threading.Event()
    c = _make_client(b, f"metis-monitor-test-{int(time.time())}")

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            result.update(ok=False, message=f"Abgelehnt: {reason_code}")
        else:
            result.update(ok=True, message="Verbindung erfolgreich")
        done.set()

    c.on_connect = on_connect
    try:
        c.connect(b["host"], int(b["port"]), keepalive=10)
        c.loop_start()
        done.wait(timeout)
    except Exception as e:  # noqa: BLE001
        result.update(ok=False, message=str(e))
    finally:
        try:
            c.disconnect()
            c.loop_stop()
        except Exception:
            pass
    return result
