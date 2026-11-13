"""metis-monitor: FastAPI app, collector loop, WebSocket push, MQTT."""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import socket
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import collectors as C
from . import health as H
from .config import DATA_DIR, Config, stored_broker_password, validate_broker
from .mqtt import VERSION, MqttManager, test_connection
from .sensors import HISTORY_KEYS, SENSORS
from .store import EnergyLedger, History

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("main")

MIN_ADMIN_PASSWORD = 12
WEAK_PASSWORDS = {"change-me", "changeme", "admin", "password", "passwort"}


def _admin_password() -> str:
    """Editing is only enabled with a non-trivial ADMIN_PASSWORD (never the shipped placeholder)."""
    pw = os.environ.get("ADMIN_PASSWORD", "")
    if not pw:
        return ""
    if pw.lower() in WEAK_PASSWORDS or len(pw) < MIN_ADMIN_PASSWORD:
        log.error("ADMIN_PASSWORD ist zu schwach (Platzhalter oder < %d Zeichen) – Bearbeiten deaktiviert",
                  MIN_ADMIN_PASSWORD)
        return ""
    return pw


ADMIN_PASSWORD = _admin_password()
STATIC = os.path.join(os.path.dirname(__file__), "static")
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400}
MAX_WS_CLIENTS = 50
WS_SEND_TIMEOUT = 3.0


def _allowed_hosts() -> set[str]:
    """Host names under which the dashboard may be addressed (DNS-rebinding protection).
    IP literals are always accepted; names must be listed (ALLOWED_HOSTS) or be this machine's name."""
    names = {"localhost"}
    for raw in (os.environ.get("ALLOWED_HOSTS", "") or "").split(","):
        if raw.strip():
            names.add(raw.strip().lower())
    pub = urlsplit(os.environ.get("PUBLIC_URL", "") or "").hostname
    if pub:
        names.add(pub.lower())
    for path in ("/host/etc/hostname", "/etc/hostname"):
        try:
            with open(path, encoding="utf-8") as f:
                h = f.read().strip().lower()
            if h:
                names |= {h, f"{h}.local", f"{h}.lan", f"{h}.fritz.box", f"{h}.home.arpa"}
        except OSError:
            pass
    names.add(socket.gethostname().lower())
    return names


ALLOWED_HOSTS = _allowed_hosts()


def _host_ok(hostport: str | None) -> bool:
    if not hostport:
        return False
    host = urlsplit("//" + hostport).hostname or ""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host.lower() in ALLOWED_HOSTS


def _origin_ok(origin: str | None, host: str | None) -> bool:
    """Browsers always send Origin on WebSocket and cross-site requests; it must match the page's host."""
    if not origin:
        return True            # non-browser client (curl, Home Assistant)
    o = urlsplit(origin)
    return bool(o.netloc) and host is not None and o.netloc.lower() == host.lower()


class AdminThrottle:
    """Per-IP backoff after failed admin logins (makes online guessing impractical)."""

    def __init__(self) -> None:
        self.fails: dict[str, list[float]] = {}

    def check(self, ip: str) -> None:
        now = time.time()
        f = [t for t in self.fails.get(ip, []) if t > now - 900]
        self.fails[ip] = f
        if len(f) >= 5 and now - f[-1] < min(300, 2 ** (len(f) - 4)):
            raise HTTPException(429, "Zu viele Fehlversuche – bitte später erneut versuchen")

    def fail(self, ip: str) -> None:
        if len(self.fails) > 10_000:
            self.fails.clear()
        self.fails.setdefault(ip, []).append(time.time())

    def ok(self, ip: str) -> None:
        self.fails.pop(ip, None)


throttle = AdminThrottle()


class Monitor:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.http = httpx.AsyncClient()
        self.system = C.SystemCollector(cfg)
        self.gpu = C.GpuCollector()
        self.ollama = C.OllamaCollector(cfg, self.http)
        self.ollama_log = C.OllamaLogTailer(cfg["ollama_unit"])
        self.owui = C.OpenWebUICollector(cfg, self.http)
        self.docker = C.DockerCollector()
        self.history = History(os.path.join(DATA_DIR, "history.db"), cfg["history_interval"], cfg["history_days"])
        self.ledger = EnergyLedger(os.path.join(DATA_DIR, "energy.db"))
        self.shelly = C.ShellyCollector(cfg, self.http, self.ledger)
        self.apt = C.AptCollector(cfg)
        self.updates: dict = {"available": False}
        self._apt_running = False
        self.services = H.ServiceChecker(cfg, self.http)
        self.voice = H.VoiceUsage(cfg)
        self.journal = H.Periodic("journal", 60, H.JournalCollector().collect)
        self.nvme = H.Periodic("nvme", 300, H.NvmeCollector(cfg).collect)
        self.ping = H.Periodic("ping", 30, H.PingCollector(cfg).collect)
        self.energy_attr = H.Periodic("energy-attr", 300, H.EnergyAttribution(self.history, self.owui, self.ollama_log).collect,
                                      initial={"available": False, "reason": "wird berechnet…"})
        self.owui_clients = H.Periodic("owui-clients", 30, H.OpenWebUIClients(cfg, self.ollama_log).collect,
                                       initial={"available": False, "clients": []})
        self.mqtt = MqttManager(cfg)
        self.snapshot: dict = {}
        self.clients: set[WebSocket] = set()

    @staticmethod
    def _ok(name: str, res, fallback: dict):
        """One failing collector must not abort the whole snapshot."""
        if isinstance(res, BaseException):
            log.warning("collector %s: %s", name, res)
            return {**fallback, "error": str(res)[:200]}
        return res

    async def collect(self) -> dict:
        system, gpu = await asyncio.gather(
            asyncio.to_thread(self.system.collect), asyncio.to_thread(self.gpu.collect)
        )
        res = await asyncio.gather(
            self.ollama.collect(), self.owui.collect(asyncio.to_thread), self.docker.collect(),
            self.shelly.collect(asyncio.to_thread), self.services.collect(), return_exceptions=True,
        )
        ollama = self._ok("ollama", res[0], {"up": False, "loaded": []})
        owui = self._ok("openwebui", res[1], {"up": False})
        docker = self._ok("docker", res[2], {"available": False, "containers": []})
        power = self._ok("power", res[3], {"configured": True, "up": False})
        services = res[4]
        if isinstance(services, BaseException):
            log.warning("collector services: %s", services)
            services = []
        ollama["requests"] = self.ollama_log.stats()
        names = self.ollama.blob_names
        ollama["loads"] = [{**e, "model": names.get(e["sha"]) or (e["sha"][:12] or "?")} for e in self.ollama_log.load_events()]
        if not self._apt_running:          # apt may take a few seconds: run in background
            self._apt_running = True
            asyncio.create_task(self._refresh_apt())
        snap = {"ts": time.time(), "version": VERSION, "system": system, "gpu": gpu,
                "ollama": ollama, "openwebui": owui, "docker": docker, "power": power,
                "updates": self.updates, "services": services, "pressure": H.read_pressure(),
                "journal": self.journal.data, "voice": self.voice.stats(), "nvme": self.nvme.data,
                "ping": self.ping.data, "energy_attr": self.energy_attr.data, "owui_clients": self.owui_clients.data}
        snap["metrics"] = {}
        for name, fn in (("flatten", C.flatten), ("health-flatten", H.flatten)):
            try:
                snap["metrics"].update(fn(snap))
            except Exception:  # noqa: BLE001 – keep the rest of the metrics
                log.exception("%s failed", name)
        try:
            snap["problems"] = H.problems(snap)
        except Exception:  # noqa: BLE001
            log.exception("problems failed")
            snap["problems"] = []
        return snap

    async def _refresh_apt(self) -> None:
        try:
            self.updates = await asyncio.to_thread(self.apt.collect)
        finally:
            self._apt_running = False

    async def loop(self) -> None:
        interval = self.cfg["collect_interval"]
        while True:
            t0 = time.monotonic()
            try:
                snap = await self.collect()
                self.snapshot = snap
                # off the event loop: the history lock may be held by a running /api/history query
                await asyncio.to_thread(self.history.add, snap["metrics"])
                await asyncio.to_thread(self.mqtt.publish, snap["metrics"], snap)
                snap["mqtt"] = self.mqtt.status()
                await self.broadcast(snap)
            except Exception:
                log.exception("collect loop error")
            await asyncio.sleep(max(0.5, interval - (time.monotonic() - t0)))

    async def _send(self, ws: WebSocket, text: str) -> WebSocket | None:
        try:
            await asyncio.wait_for(ws.send_text(text), WS_SEND_TIMEOUT)
            return None
        except Exception:  # noqa: BLE001 – slow or gone: drop it
            return ws

    async def broadcast(self, snap: dict) -> None:
        if not self.clients:
            return
        text = dumps(snap)                       # serialise once, not per client
        dead = await asyncio.gather(*(self._send(ws, text) for ws in list(self.clients)))
        for ws in dead:
            if ws is not None:
                self.clients.discard(ws)
                try:
                    await asyncio.wait_for(ws.close(code=1013), 1)
                except Exception:  # noqa: BLE001
                    pass


def _clean(o):
    """Replace non-finite floats by None so every consumer gets strict JSON."""
    if isinstance(o, float):
        return o if o == o and o not in (float("inf"), float("-inf")) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def dumps(o) -> str:
    try:
        return json.dumps(o, allow_nan=False, separators=(",", ":"), default=str)
    except ValueError:
        return json.dumps(_clean(o), allow_nan=False, separators=(",", ":"), default=str)


cfg = Config()
mon = Monitor(cfg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    mon.mqtt.apply()
    task = asyncio.create_task(mon.loop())
    yield
    task.cancel()
    mon.mqtt.shutdown()


app = FastAPI(title="metis-monitor", version=VERSION, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


class StrictJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return dumps(content).encode("utf-8")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def require_admin(pw: str | None, request: Request) -> None:
    if not ADMIN_PASSWORD:
        raise HTTPException(403, "Bearbeiten deaktiviert: ADMIN_PASSWORD fehlt oder ist zu schwach "
                                 f"(mind. {MIN_ADMIN_PASSWORD} Zeichen)")
    ip = _client_ip(request)
    throttle.check(ip)
    if not pw or not hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
        throttle.fail(ip)
        raise HTTPException(401, "Admin-Passwort falsch")
    throttle.ok(ip)


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


_first_snapshot = asyncio.Lock()


@app.get("/api/snapshot", response_class=StrictJSONResponse)
async def snapshot():
    if not mon.snapshot:
        async with _first_snapshot:              # single flight until the loop has produced one
            if not mon.snapshot:
                mon.snapshot = await mon.collect()
    return {**mon.snapshot, "mqtt": mon.mqtt.status()}


@app.get("/api/metrics", response_class=StrictJSONResponse)
async def metrics():
    """Flat metrics – handy for HA REST sensors or scripts."""
    return mon.snapshot.get("metrics", {})


@app.get("/api/history")
async def history(range: str = "1h", keys: str = ""):
    seconds = RANGES.get(range)
    if not seconds:
        raise HTTPException(400, f"range muss einer von {list(RANGES)} sein")
    klist = list(dict.fromkeys(k for k in keys[:2000].split(",") if k in HISTORY_KEYS)) or HISTORY_KEYS
    return await asyncio.to_thread(mon.history.query, seconds, klist)


@app.get("/api/sensors")
async def sensors():
    return SENSORS


@app.get("/api/brokers")
async def get_brokers():
    return {
        "editable": bool(ADMIN_PASSWORD),
        "brokers": [Config.public_broker(b) for b in cfg["brokers"]],
        "status": mon.mqtt.status(),
        "node_id": cfg["node_id"],
    }


@app.put("/api/brokers")
async def put_brokers(request: Request, x_admin_password: str | None = Header(default=None)):
    require_admin(x_admin_password, request)
    body = await request.json()
    if not isinstance(body, list):
        raise HTTPException(400, "Liste von Brokern erwartet")
    try:
        cfg.set_brokers(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await asyncio.to_thread(mon.mqtt.apply)
    return await get_brokers()


@app.post("/api/brokers/test")
async def test_broker(request: Request, x_admin_password: str | None = Header(default=None)):
    require_admin(x_admin_password, request)
    raw = await request.json()
    if not isinstance(raw, dict):
        raise HTTPException(400, "Objekt erwartet")
    if raw.get("password") is None:
        prev = next((b for b in cfg["brokers"] if b["id"] == raw.get("id")), None)
        raw["password"] = stored_broker_password(prev, raw)   # only for the same host/port/user/TLS
    b = Config.normalize_broker(raw)
    err = validate_broker(b)
    if err:
        return {"ok": False, "message": err}
    return await asyncio.to_thread(test_connection, b)


@app.get("/api/power")
async def get_power():
    return {"editable": bool(ADMIN_PASSWORD), "settings": cfg.public_power()}


@app.put("/api/power")
async def put_power(request: Request, x_admin_password: str | None = Header(default=None)):
    require_admin(x_admin_password, request)
    try:
        cfg.set_power(await request.json())
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from e
    mon.shelly.info_host = None
    return await get_power()


@app.post("/api/admin/check")
async def admin_check(request: Request, x_admin_password: str | None = Header(default=None)):
    require_admin(x_admin_password, request)
    return {"ok": True}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    host = websocket.headers.get("host")
    # cross-site WebSocket hijacking / DNS rebinding: only same-origin pages on an allowed host
    if not _host_ok(host) or not _origin_ok(websocket.headers.get("origin"), host):
        await websocket.close(code=1008)
        return
    if len(mon.clients) >= MAX_WS_CLIENTS:
        await websocket.close(code=1013)
        return
    await websocket.accept()
    mon.clients.add(websocket)
    try:
        if mon.snapshot:
            await asyncio.wait_for(websocket.send_text(dumps({**mon.snapshot, "mqtt": mon.mqtt.status()})),
                                   WS_SEND_TIMEOUT)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        mon.clients.discard(websocket)


SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'self'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}


@app.middleware("http")
async def guard(request: Request, call_next):
    if not _host_ok(request.headers.get("host")):          # DNS rebinding
        return JSONResponse({"detail": "Host nicht erlaubt (ALLOWED_HOSTS)"}, status_code=421)
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _origin_ok(request.headers.get("origin"),
                                                                       request.headers.get("host")):
        return JSONResponse({"detail": "Origin nicht erlaubt"}, status_code=403)
    if (request.headers.get("range") or "").count(",") > 4:   # multi-range abuse (CVE-2025-62727 class)
        return JSONResponse({"detail": "Range nicht unterstützt"}, status_code=416)
    resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"   # revalidate via ETag → updates show up immediately
    return resp


@app.exception_handler(HTTPException)
async def http_exc(_req, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    tls = {}
    if os.environ.get("SSL_CERTFILE") and os.environ.get("SSL_KEYFILE"):
        tls = {"ssl_certfile": os.environ["SSL_CERTFILE"], "ssl_keyfile": os.environ["SSL_KEYFILE"]}
    elif os.environ.get("HOST", "0.0.0.0") not in ("127.0.0.1", "::1", "localhost"):
        log.warning("Kein TLS: Admin- und Gerätepasswörter gehen im Klartext übers Netz. "
                    "SSL_CERTFILE/SSL_KEYFILE setzen oder HOST=127.0.0.1 hinter einem HTTPS-Proxy.")
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8088")),
                log_level="warning", ws_max_size=64 * 1024, limit_concurrency=200,
                h11_max_incomplete_event_size=32 * 1024, server_header=False, **tls)
