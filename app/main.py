"""metis-monitor: FastAPI app, collector loop, WebSocket push, MQTT."""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import collectors as C
from . import health as H
from .config import DATA_DIR, Config, validate_broker
from .mqtt import VERSION, MqttManager, test_connection
from .sensors import HISTORY_KEYS, SENSORS
from .store import EnergyLedger, History

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("main")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
STATIC = os.path.join(os.path.dirname(__file__), "static")
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400}


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
        self.mqtt = MqttManager(cfg)
        self.snapshot: dict = {}
        self.clients: set[WebSocket] = set()

    async def collect(self) -> dict:
        system, gpu = await asyncio.gather(
            asyncio.to_thread(self.system.collect), asyncio.to_thread(self.gpu.collect)
        )
        ollama, owui, docker, power, services = await asyncio.gather(
            self.ollama.collect(), self.owui.collect(asyncio.to_thread), self.docker.collect(),
            self.shelly.collect(asyncio.to_thread), self.services.collect(),
        )
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
                "ping": self.ping.data, "energy_attr": self.energy_attr.data}
        snap["metrics"] = C.flatten(snap)
        snap["problems"] = H.problems(snap)
        snap["metrics"].update(H.flatten(snap))
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
                self.history.add(snap["metrics"])
                await asyncio.to_thread(self.mqtt.publish, snap["metrics"], snap)
                snap["mqtt"] = self.mqtt.status()
                await self.broadcast(snap)
            except Exception:
                log.exception("collect loop error")
            await asyncio.sleep(max(0.5, interval - (time.monotonic() - t0)))

    async def broadcast(self, snap: dict) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(snap)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


cfg = Config()
mon = Monitor(cfg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    mon.mqtt.apply()
    task = asyncio.create_task(mon.loop())
    yield
    task.cancel()
    mon.mqtt.shutdown()


app = FastAPI(title="metis-monitor", version=VERSION, lifespan=lifespan)


def require_admin(pw: str | None) -> None:
    if not ADMIN_PASSWORD:
        raise HTTPException(403, "Bearbeiten deaktiviert: ADMIN_PASSWORD ist nicht gesetzt")
    if not pw or not hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "Admin-Passwort falsch")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/api/snapshot")
async def snapshot():
    if not mon.snapshot:
        mon.snapshot = await mon.collect()
    return {**mon.snapshot, "mqtt": mon.mqtt.status()}


@app.get("/api/metrics")
async def metrics():
    """Flat metrics – handy for HA REST sensors or scripts."""
    return mon.snapshot.get("metrics", {})


@app.get("/api/history")
async def history(range: str = "1h", keys: str = ""):
    seconds = RANGES.get(range)
    if not seconds:
        raise HTTPException(400, f"range muss einer von {list(RANGES)} sein")
    klist = [k for k in keys.split(",") if k] or HISTORY_KEYS
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
    require_admin(x_admin_password)
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
    require_admin(x_admin_password)
    raw = await request.json()
    if raw.get("password") is None:
        prev = next((b for b in cfg["brokers"] if b["id"] == raw.get("id")), None)
        raw["password"] = prev["password"] if prev else ""
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
    require_admin(x_admin_password)
    try:
        cfg.set_power(await request.json())
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from e
    mon.shelly.info_host = None
    return await get_power()


@app.post("/api/admin/check")
async def admin_check(x_admin_password: str | None = Header(default=None)):
    require_admin(x_admin_password)
    return {"ok": True}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    mon.clients.add(websocket)
    try:
        if mon.snapshot:
            await websocket.send_json({**mon.snapshot, "mqtt": mon.mqtt.status()})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        mon.clients.discard(websocket)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"   # revalidate via ETag → updates show up immediately
    return resp


@app.exception_handler(HTTPException)
async def http_exc(_req, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8088")),
                log_level="warning")
