"""Data collectors: host, GPU, Ollama (API + journal), Open WebUI (DB), Docker."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import socket
import sqlite3
import subprocess
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone

import httpx
import psutil

log = logging.getLogger("collectors")

GiB = 1024 ** 3
ARM_PARTS = {"0xd85": "X925", "0xd87": "A725", "0xd8e": "N3", "0xd4f": "V2", "0xd81": "A720", "0xd82": "X4"}


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _ts_norm(v) -> float:
    """Normalise s / ms / µs / ns epoch values to seconds."""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    if v > 1e17:
        return v / 1e9
    if v > 1e14:
        return v / 1e6
    if v > 1e11:
        return v / 1e3
    return v


class Rates:
    def __init__(self) -> None:
        self.prev: dict[str, tuple[float, float]] = {}

    def __call__(self, key: str, value: float, now: float) -> float:
        p = self.prev.get(key)
        self.prev[key] = (value, now)
        if not p or now <= p[1] or value < p[0]:
            return 0.0
        return (value - p[0]) / (now - p[1])


# --------------------------------------------------------------------------- host
class SystemCollector:
    def __init__(self, cfg) -> None:
        self.root = cfg["host_root"]
        self.disks = cfg["disks"]
        self.rates = Rates()
        psutil.cpu_percent(percpu=True)
        self.static = self._static()

    def _static(self) -> dict:
        osr = {}
        for line in _read(os.path.join(self.root, "etc/os-release")).splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                osr[k] = v.strip('"')
        hostname = _read(os.path.join(self.root, "etc/hostname")) or socket.gethostname()
        cores = []
        for i in range(psutil.cpu_count() or 0):
            midr = _read(f"/sys/devices/system/cpu/cpu{i}/regs/identification/midr_el1")
            part = hex((int(midr, 16) >> 4) & 0xFFF) if midr else ""
            cores.append(ARM_PARTS.get(part, part or "?"))
        model = ", ".join(f"{n}× {t}" for t, n in Counter(cores).items()) if cores else platform.processor()
        return {
            "hostname": hostname,
            "os": osr.get("PRETTY_NAME", ""),
            "kernel": platform.release(),
            "arch": platform.machine(),
            "cpu_model": model,
            "core_types": cores,
            "cpu_count": psutil.cpu_count(),
        }

    def _temps(self) -> dict:
        zones = []
        base = "/sys/class/thermal"
        try:
            names = sorted(n for n in os.listdir(base) if n.startswith("thermal_zone"))
        except OSError:
            names = []
        for n in names:
            t = _read(f"{base}/{n}/temp")
            if t.lstrip("-").isdigit():
                zones.append({"name": f"{_read(f'{base}/{n}/type')} {n[12:]}", "temp": int(t) / 1000})
        hw = {}
        base = "/sys/class/hwmon"
        try:
            for h in os.listdir(base):
                name = _read(f"{base}/{h}/name")
                t = _read(f"{base}/{h}/temp1_input")
                if t.lstrip("-").isdigit() and not name.startswith("acpitz"):
                    hw[name] = int(t) / 1000
        except OSError:
            pass
        return {
            "cpu_max": max((z["temp"] for z in zones), default=None),
            "zones": zones,
            "nvme": hw.get("nvme"),
            "other": [{"name": k, "temp": v} for k, v in hw.items() if k != "nvme"],
        }

    @staticmethod
    def _default_iface() -> str | None:
        for line in _read("/proc/net/route").splitlines()[1:]:
            parts = line.split()
            if len(parts) > 2 and parts[1] == "00000000":
                return parts[0]
        return None

    def collect(self) -> dict:
        now = time.time()
        per = psutil.cpu_percent(percpu=True)
        try:
            freqs = [round(f.current) for f in psutil.cpu_freq(percpu=True)]
        except Exception:
            freqs = []
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        load = os.getloadavg()

        disks = []
        for mnt in self.disks:
            try:
                u = psutil.disk_usage(os.path.join(self.root, mnt.lstrip("/")))
                disks.append({"mount": mnt, "total": u.total, "used": u.used, "free": u.free, "percent": u.percent})
            except OSError:
                pass

        rd = wr = 0
        try:
            for name, c in psutil.disk_io_counters(perdisk=True).items():
                if re.fullmatch(r"nvme\d+n\d+|sd[a-z]+|vd[a-z]+", name):
                    rd += c.read_bytes
                    wr += c.write_bytes
        except Exception:
            pass

        iface = self._default_iface()
        net = {"iface": iface, "rx_bps": 0.0, "tx_bps": 0.0, "rx_total": 0, "tx_total": 0, "ip": None, "speed": None}
        try:
            c = psutil.net_io_counters(pernic=True).get(iface) if iface else None
            if c:
                net.update(rx_total=c.bytes_recv, tx_total=c.bytes_sent,
                           rx_bps=self.rates("rx", c.bytes_recv, now), tx_bps=self.rates("tx", c.bytes_sent, now))
            for a in psutil.net_if_addrs().get(iface, []):
                if a.family == socket.AF_INET:
                    net["ip"] = a.address
            st = psutil.net_if_stats().get(iface)
            net["speed"] = st.speed if st else None
        except Exception:
            pass

        return {
            **self.static,
            "boot_time": psutil.boot_time(),
            "uptime": now - psutil.boot_time(),
            "cpu": {
                "percent": round(sum(per) / len(per), 1) if per else 0,
                "per_core": per,
                "freq_mhz": freqs,
                "load": [round(x, 2) for x in load],
            },
            "memory": {
                "total": vm.total, "used": vm.total - vm.available, "available": vm.available,
                "percent": vm.percent, "cached": getattr(vm, "cached", 0),
                "swap_total": sw.total, "swap_used": sw.used, "swap_percent": sw.percent,
            },
            "temps": self._temps(),
            "disks": disks,
            "disk_io": {"read_bps": self.rates("rd", rd, now), "write_bps": self.rates("wr", wr, now)},
            "net": net,
        }


# --------------------------------------------------------------------------- GPU
class GpuCollector:
    FIELDS = ["name", "driver_version", "temperature.gpu", "utilization.gpu", "power.draw",
              "clocks.sm", "clocks.max.sm", "pstate", "memory.used", "memory.total"]

    THROTTLE = "clocks_event_reasons.active"
    # nvidia-smi clocks event reason bits; "critical" ones indicate real slowdowns (not idle / power cap)
    REASONS = {0x1: "Leerlauf", 0x2: "App-Takt", 0x4: "Leistungslimit", 0x8: "HW-Slowdown", 0x10: "Sync Boost",
               0x20: "Temperatur (SW)", 0x40: "Temperatur (HW)", 0x80: "Power Brake", 0x100: "Display-Takt"}
    ACTIVE_MASK = 0x4 | 0x8 | 0x20 | 0x40 | 0x80
    CRITICAL_MASK = 0x8 | 0x20 | 0x40 | 0x80

    def __init__(self) -> None:
        self.error = None
        self.throttle_ok = True
        self.throttle_hist: deque = deque()

    @staticmethod
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _throttle(self, raw: str | None) -> dict:
        try:
            mask = int(raw, 16)
        except (TypeError, ValueError):
            return {"available": False}
        now = time.time()
        active = bool(mask & self.ACTIVE_MASK)
        self.throttle_hist.append((now, active))
        while self.throttle_hist and self.throttle_hist[0][0] < now - 3600:
            self.throttle_hist.popleft()
        share = sum(a for _, a in self.throttle_hist) / len(self.throttle_hist) * 100
        return {"available": True, "mask": mask, "active": active, "critical": bool(mask & self.CRITICAL_MASK),
                "reasons_text": [t for b, t in self.REASONS.items() if mask & b and b & self.ACTIVE_MASK],
                "flags_text": [t for b, t in self.REASONS.items() if mask & b],
                "share_1h": round(share, 1)}

    def collect(self) -> dict:
        try:
            fields = self.FIELDS + ([self.THROTTLE] if self.throttle_ok else [])
            out = subprocess.run(
                ["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode != 0 and self.throttle_ok:   # older driver: field unknown
                self.throttle_ok = False
                return self.collect()
            if out.returncode != 0:
                raise RuntimeError(out.stderr.strip() or out.stdout.strip())
            vals = [v.strip() for v in out.stdout.splitlines()[0].split(",")]
            d = dict(zip(fields, vals))
            procs = []
            p = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            for line in p.stdout.splitlines():
                parts = [x.strip() for x in line.split(",")]
                if not parts or not parts[0].isdigit():
                    continue
                pid = int(parts[0])
                try:
                    pr = psutil.Process(pid)
                    name = pr.name()
                    rss = pr.memory_info().rss
                except Exception:
                    name, rss = "?", None
                procs.append({"pid": pid, "name": name, "gpu_mem_mib": self._num(parts[1]) if len(parts) > 1 else None, "rss": rss})
            self.error = None
            return {
                "available": True,
                "name": d["name"], "driver": d["driver_version"],
                "temp": self._num(d["temperature.gpu"]), "util": self._num(d["utilization.gpu"]),
                "power": self._num(d["power.draw"]), "clock_mhz": self._num(d["clocks.sm"]),
                "clock_max_mhz": self._num(d["clocks.max.sm"]), "pstate": d["pstate"],
                "mem_used_mib": self._num(d["memory.used"]), "mem_total_mib": self._num(d["memory.total"]),
                "processes": procs,
                "throttle": self._throttle(d.get(self.THROTTLE)),
            }
        except Exception as e:  # noqa: BLE001
            if str(e) != self.error:
                log.warning("GPU collect failed: %s", e)
            self.error = str(e)
            return {"available": False, "error": self.error}


# --------------------------------------------------------------------------- Ollama log
INFER_PATHS = ("/api/chat", "/api/generate", "/api/embed", "/api/embeddings",
               "/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/v1/responses", "/v1/messages")
GIN_RE = re.compile(r'\[GIN\][^|]*\|\s*(\d{3})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(\w+)\s+"([^"]+)"')
DUR_RE = re.compile(r"([\d.]+)(h|ms|µs|us|ns|m|s)")
DUR_MULT = {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "µs": 1e-6, "us": 1e-6, "ns": 1e-9}
BLOB_RE = re.compile(r"sha256-([0-9a-f]{64})")
RUNNER_RE = re.compile(r"llama runner started in ([\d.]+) seconds")


def parse_go_duration(s: str) -> float:
    return sum(float(n) * DUR_MULT[u] for n, u in DUR_RE.findall(s))


class OllamaLogTailer:
    """Follows `journalctl -u ollama` and keeps inference request events (24h)."""

    def __init__(self, unit: str) -> None:
        self.unit = unit
        self.events: deque = deque(maxlen=200_000)
        self.lock = threading.Lock()
        self.available = None
        self.error = None
        self.names: dict[str, str] = {}
        self.total_seen = 0
        self.pending_loads: deque = deque(maxlen=20)   # (ts, blob sha) of "loading model" lines
        self.loads: deque = deque(maxlen=50)           # (ts done, seconds, blob sha)
        threading.Thread(target=self._run, daemon=True, name="ollama-log").start()

    def _resolve(self, ip: str) -> str:
        if ip not in self.names:
            name = ""
            if ip in ("127.0.0.1", "::1"):
                name = "localhost"
            else:
                try:
                    name = socket.gethostbyaddr(ip)[0]
                except Exception:
                    name = ""
            self.names[ip] = name
        return self.names[ip]

    def _run(self) -> None:
        while True:
            try:
                proc = subprocess.Popen(
                    ["journalctl", "-u", self.unit, "-o", "json", "--output-fields=MESSAGE",
                     "--since=-7d", "-f", "--no-pager"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                )
                self.available = True
                self.error = None
                for line in proc.stdout:
                    self._handle(line)
                err = proc.stderr.read().strip()
                proc.wait()
                self.error = err or f"journalctl beendet ({proc.returncode})"
            except FileNotFoundError:
                self.available = False
                self.error = "journalctl nicht verfügbar"
                return
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
            log.warning("ollama log tailer: %s – restart in 15s", self.error)
            self.available = False
            time.sleep(15)

    def _handle(self, line: str) -> None:
        try:
            rec = json.loads(line)
        except ValueError:
            return
        msg = rec.get("MESSAGE")
        if isinstance(msg, list):  # binary messages come as byte arrays
            try:
                msg = bytes(msg).decode("utf-8", "replace")
            except Exception:
                return
        if not msg:
            return
        ts = int(rec.get("__REALTIME_TIMESTAMP", 0)) / 1e6 or time.time()
        if "[GIN]" not in msg:
            self._handle_load(msg, ts)
            return
        if ts < time.time() - 86400:          # requests: only the last 24 h
            return
        m = GIN_RE.search(msg)
        if not m:
            return
        status, dur, client, method, path = m.groups()
        path = path.split("?", 1)[0]
        self.total_seen += 1
        if not path.startswith(INFER_PATHS):
            return
        client = client.strip()
        ev = (ts, method, path, int(status), parse_go_duration(dur), client, self._resolve(client))
        with self.lock:
            self.events.append(ev)

    def _handle_load(self, msg: str, ts: float) -> None:
        if 'msg="loading model via llama-server"' in msg or 'msg="starting llama server"' in msg:
            m = BLOB_RE.search(msg)
            self.pending_loads.append((ts, m.group(1) if m else ""))
        elif "server_run_i: model loaded" in msg or "llama runner started in" in msg:
            started = RUNNER_RE.search(msg)
            while self.pending_loads and self.pending_loads[0][0] < ts - 1800:   # stale (failed) loads
                self.pending_loads.popleft()
            t0, sha = self.pending_loads.popleft() if self.pending_loads else (None, "")
            secs = float(started.group(1)) if started else (ts - t0 if t0 else None)
            if secs is not None:
                with self.lock:
                    self.loads.append((ts, round(secs, 2), sha))

    def load_events(self) -> list:
        with self.lock:
            return [{"ts": t, "seconds": s, "sha": sha} for t, s, sha in reversed(self.loads)]

    def stats(self) -> dict:
        now = time.time()
        with self.lock:
            while self.events and self.events[0][0] < now - 86400:
                self.events.popleft()
            evs = list(self.events)
        h1 = [e for e in evs if e[0] >= now - 3600]
        m5 = [e for e in evs if e[0] >= now - 300]
        clients = Counter((e[5], e[6]) for e in evs)
        paths = Counter(e[2] for e in evs)
        durs = [e[4] for e in h1 if e[3] < 400]
        hourly = [0] * 24
        for e in evs:
            idx = 23 - int((now - e[0]) // 3600)
            if 0 <= idx < 24:
                hourly[idx] += 1
        return {
            "available": bool(self.available),
            "error": self.error,
            "requests_24h": len(evs),
            "requests_1h": len(h1),
            "requests_per_min": round(len(m5) / 5, 2),
            "errors_24h": sum(1 for e in evs if e[3] >= 400),
            "avg_duration_1h": round(sum(durs) / len(durs), 2) if durs else None,
            "max_duration_1h": round(max(durs), 2) if durs else None,
            "last_request": evs[-1][0] if evs else None,
            "hourly": hourly,
            "clients": [{"ip": ip, "name": n, "count": c} for (ip, n), c in clients.most_common(10)],
            "paths": [{"path": p, "count": c} for p, c in paths.most_common()],
            "recent": [
                {"ts": e[0], "method": e[1], "path": e[2], "status": e[3], "duration": round(e[4], 3),
                 "client": e[5], "client_name": e[6]}
                for e in reversed(evs[-15:])
            ],
        }


# --------------------------------------------------------------------------- Ollama API
class OllamaCollector:
    def __init__(self, cfg, client: httpx.AsyncClient) -> None:
        self.url = cfg["ollama_url"].rstrip("/")
        self.http = client
        self.tags: list = []
        self.tags_ts = 0.0
        self.version = None
        self.manifests = os.path.join(cfg["ollama_models_dir"], "manifests")
        self.blob_names: dict[str, str] = {}

    def _scan_manifests(self) -> None:
        """Model blob sha256 -> model name (for load-time log lines)."""
        names = {}
        for root, _dirs, files in os.walk(self.manifests):
            for fn in files:
                path = os.path.join(root, fn)
                try:
                    with open(path, encoding="utf-8") as f:
                        man = json.load(f)
                except Exception:
                    continue
                rel = os.path.relpath(path, self.manifests).replace(os.sep, "/")
                parts = rel.split("/")
                if len(parts) < 3:
                    continue
                repo = "/".join(parts[1:-1])
                repo = repo[len("library/"):] if repo.startswith("library/") else repo
                name = f"{repo}:{parts[-1]}"
                for layer in man.get("layers") or []:
                    if layer.get("mediaType") == "application/vnd.ollama.image.model":
                        names[str(layer.get("digest", "")).replace("sha256:", "")] = name
        self.blob_names = names

    async def collect(self) -> dict:
        try:
            r = await self.http.get(f"{self.url}/api/ps", timeout=5)
            r.raise_for_status()
            loaded = []
            for m in r.json().get("models", []):
                det = m.get("details") or {}
                loaded.append({
                    "name": m.get("name"), "size": m.get("size"), "size_vram": m.get("size_vram"),
                    "expires_at": m.get("expires_at"), "context_length": m.get("context_length"),
                    "parameter_size": det.get("parameter_size"), "quantization": det.get("quantization_level"),
                    "family": det.get("family"),
                })
            now = time.time()
            if now - self.tags_ts > 60 or self.version is None:
                v = await self.http.get(f"{self.url}/api/version", timeout=5)
                self.version = v.json().get("version")
                t = await self.http.get(f"{self.url}/api/tags", timeout=10)
                self.tags = []
                for m in t.json().get("models", []):
                    det = m.get("details") or {}
                    self.tags.append({
                        "name": m.get("name"), "size": m.get("size"), "modified_at": m.get("modified_at"),
                        "parameter_size": det.get("parameter_size"), "quantization": det.get("quantization_level"),
                        "family": det.get("family"), "context_length": det.get("context_length"),
                        "capabilities": m.get("capabilities") or [],
                    })
                self.tags.sort(key=lambda x: x.get("size") or 0, reverse=True)
                self.tags_ts = now
                await asyncio.to_thread(self._scan_manifests)
            return {
                "up": True, "version": self.version, "loaded": loaded, "installed": self.tags,
                "installed_size": sum(m.get("size") or 0 for m in self.tags),
            }
        except Exception as e:  # noqa: BLE001
            return {"up": False, "error": str(e), "loaded": [], "installed": self.tags, "version": self.version}


# --------------------------------------------------------------------------- Open WebUI
class OpenWebUICollector:
    def __init__(self, cfg, client: httpx.AsyncClient) -> None:
        self.url = cfg["openwebui_url"].rstrip("/")
        self.db = cfg["openwebui_db"]
        self.http = client
        self.db_stats: dict = {}
        self.db_ts = 0.0
        self.version = None
        self.recent: list[tuple] = []   # (model, start ts, duration s, output tokens) – last 7 days

    def _query_db(self) -> dict:
        if not os.path.exists(self.db):
            return {"db_available": False, "db_error": f"{self.db} nicht gefunden"}
        # mode=ro (not immutable): immutable ignores the WAL and misses recent writes (e.g. new users)
        try:
            con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=5)
            con.execute("select 1 from user limit 1")
        except sqlite3.Error:
            con = sqlite3.connect(f"file:{self.db}?immutable=1", uri=True, timeout=5)
        try:
            now = time.time()
            users = con.execute("select last_active_at from user").fetchall()
            chats = con.execute("select updated_at, archived from chat").fetchall()
            msgs = con.execute(
                "select model_id, usage, created_at from chat_message where role='assistant'"
            ).fetchall()
        finally:
            con.close()
        per_model: dict[str, dict] = {}
        tok24 = tok_total = msg24 = 0
        last = None
        hourly = [0] * 24
        recent = []
        for model, usage, created in msgs:
            ts = _ts_norm(created)
            try:
                u = json.loads(usage) if usage else {}
            except ValueError:
                u = {}
            u = u or {}
            out_t = int(u.get("output_tokens") or u.get("eval_count") or u.get("completion_tokens") or 0)
            in_t = int(u.get("input_tokens") or u.get("prompt_eval_count") or u.get("prompt_tokens") or 0)
            tps = u.get("response_token/s")
            pm = per_model.setdefault(model or "?", {"model": model or "?", "messages": 0, "out_tokens": 0,
                                                     "in_tokens": 0, "_tps": [], "last": 0})
            pm["messages"] += 1
            pm["out_tokens"] += out_t
            pm["in_tokens"] += in_t
            pm["last"] = max(pm["last"], ts)
            if isinstance(tps, (int, float)) and tps > 0:
                pm["_tps"].append(tps)
            tok_total += out_t + in_t
            dur_ns = u.get("total_duration")
            if ts >= now - 7 * 86400 and isinstance(dur_ns, (int, float)) and dur_ns > 0:
                recent.append((model or "?", ts, dur_ns / 1e9, out_t))   # created_at = start of the reply
            if ts >= now - 86400:
                msg24 += 1
                tok24 += out_t + in_t
                idx = 23 - int((now - ts) // 3600)
                if 0 <= idx < 24:
                    hourly[idx] += 1
            if last is None or ts >= last[0]:
                last = (ts, model, tps)
        models = []
        for pm in per_model.values():
            t = pm.pop("_tps")
            pm["avg_tps"] = round(sum(t) / len(t), 1) if t else None
            models.append(pm)
        models.sort(key=lambda x: x["messages"], reverse=True)
        self.recent = recent
        return {
            "db_available": True,
            "users": len(users),
            "active_users_24h": sum(1 for (la,) in users if _ts_norm(la) >= now - 86400),
            "chats": len(chats),
            "chats_active_24h": sum(1 for (up, _a) in chats if _ts_norm(up) >= now - 86400),
            "messages_total": len(msgs),
            "messages_24h": msg24,
            "tokens_24h": tok24,
            "tokens_total": tok_total,
            "hourly": hourly,
            "per_model": models,
            "last_response": {"ts": last[0], "model": last[1], "tps": last[2]} if last else None,
        }

    async def collect(self, to_thread) -> dict:
        up = False
        try:
            r = await self.http.get(f"{self.url}/health", timeout=5)
            up = r.status_code == 200
            if self.version is None and up:
                v = await self.http.get(f"{self.url}/api/version", timeout=5)
                self.version = v.json().get("version")
        except Exception:
            up = False
        now = time.time()
        if now - self.db_ts > 30:
            try:
                self.db_stats = await to_thread(self._query_db)
            except Exception as e:  # noqa: BLE001
                self.db_stats = {"db_available": False, "db_error": str(e)}
            self.db_ts = now
        return {"up": up, "version": self.version, "url": self.url, **self.db_stats}


# --------------------------------------------------------------------------- apt updates
APT_RE = re.compile(r"^(\S+?)/(\S+)\s+(\S+)\s+(\S+)\s+\[upgradable from: ([^\]]+)\]")


class AptCollector:
    """Pending host updates via `apt list --upgradable` against the read-only host root."""

    def __init__(self, cfg) -> None:
        self.root = cfg["host_root"]
        self.lists = os.path.join(self.root, "var/lib/apt/lists")
        self.status = os.path.join(self.root, "var/lib/dpkg/status")
        self.cache: dict = {"available": False}
        self.key = None
        self.ts = 0.0

    def _mtime(self, p: str) -> float:
        try:
            return os.stat(p).st_mtime
        except OSError:
            return 0.0

    def collect(self) -> dict:
        lists_m, status_m = self._mtime(self.lists), self._mtime(self.status)
        key = (lists_m, status_m)
        reboot = os.path.exists("/hostrun/reboot-required")
        reboot_pkgs = []
        if reboot:
            reboot_pkgs = _read("/hostrun/reboot-required.pkgs").split()
        if key == self.key and time.time() - self.ts < 1800:
            return {**self.cache, "reboot_required": reboot, "reboot_pkgs": reboot_pkgs}
        try:
            out = subprocess.run(
                ["apt", "list", "--upgradable",
                 "-o", f"Dir={self.root}", "-o", "Dir::Cache=/tmp/apt-cache",
                 "-o", "Dir::Cache::pkgcache=", "-o", "Dir::Cache::srcpkgcache=",
                 "-o", "Debug::NoLocking=1", "-o", "Dir::Log=/tmp"],
                capture_output=True, text=True, timeout=120, env={**os.environ, "LANG": "C", "LC_ALL": "C"},
            )
            pkgs = []
            for line in out.stdout.splitlines():
                m = APT_RE.match(line.strip())
                if m:
                    name, suite, new, arch, old = m.groups()
                    pkgs.append({"name": name, "suite": suite, "new": new, "old": old,
                                 "security": "-security" in suite})
            if out.returncode != 0 and not pkgs:
                raise RuntimeError(out.stderr.strip()[:300] or f"apt exit {out.returncode}")
            pkgs.sort(key=lambda p: (not p["security"], p["name"]))
            self.cache = {
                "available": True, "count": len(pkgs), "security": sum(p["security"] for p in pkgs),
                "packages": pkgs, "lists_updated": lists_m or None, "checked": time.time(),
            }
            self.key, self.ts = key, time.time()
        except Exception as e:  # noqa: BLE001
            self.cache = {"available": False, "error": str(e)}
            self.ts = time.time()
        return {**self.cache, "reboot_required": reboot, "reboot_pkgs": reboot_pkgs}


# --------------------------------------------------------------------------- Shelly plug
class ShellyCollector:
    """Shelly Gen2+ (RPC). Read-only: never switches the output."""

    def __init__(self, cfg, client: httpx.AsyncClient, ledger) -> None:
        self.cfg = cfg
        self.http = client
        self.ledger = ledger
        self.info: dict = {}
        self.info_host = None

    async def collect(self, to_thread) -> dict:
        p = self.cfg["power"]
        if not p.get("host"):
            return {"configured": False}
        base = f"http://{p['host']}"
        auth = httpx.DigestAuth("admin", p["password"]) if p.get("password") else None
        ch = int(p.get("channel") or 0)
        try:
            if self.info_host != p["host"]:
                r = await self.http.get(f"{base}/shelly", timeout=5)
                self.info = r.json()
                self.info_host = p["host"]
            st = None
            for method in ("Switch.GetStatus", "PM1.GetStatus"):
                r = await self.http.get(f"{base}/rpc/{method}", params={"id": ch}, auth=auth, timeout=5)
                if r.status_code == 401:
                    raise RuntimeError("Authentifizierung fehlgeschlagen (Passwort?)")
                if r.status_code == 200:
                    st = r.json()
                    break
            if st is None:
                raise RuntimeError(f"Keine Messdaten (HTTP {r.status_code})")
            total_wh = (st.get("aenergy") or {}).get("total")
            energy = await to_thread(self.ledger.update, total_wh) if total_wh is not None else {}
            price = p.get("price_eur_kwh")
            return {
                "configured": True, "up": True, "host": p["host"],
                "name": self.info.get("name") or self.info.get("id"), "model": self.info.get("model"),
                "gen": self.info.get("gen"), "fw": self.info.get("ver"),
                "output": st.get("output"), "power": st.get("apower"), "voltage": st.get("voltage"),
                "current": st.get("current"), "freq": st.get("freq"),
                "temp": (st.get("temperature") or {}).get("tC"),
                "meter_total_kwh": total_wh / 1000 if total_wh is not None else None,
                "price_eur_kwh": price,
                **energy,
            }
        except Exception as e:  # noqa: BLE001
            self.info_host = None
            return {"configured": True, "up": False, "host": p["host"], "error": str(e),
                    **(await to_thread(self.ledger.summary))}


# --------------------------------------------------------------------------- Docker
class DockerCollector:
    def __init__(self, sock: str = "/var/run/docker.sock") -> None:
        self.sock = sock
        self.http = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=sock), base_url="http://docker")
        self.cache: dict = {"available": False, "containers": []}
        self.ts = 0.0
        self.prev: dict[str, tuple] = {}

    async def _detail(self, c: dict) -> dict:
        cid, out = c["Id"], {}
        try:
            ins = (await self.http.get(f"/containers/{cid}/json", timeout=5)).json()
            st = ins.get("State") or {}
            out.update(restarts=ins.get("RestartCount", 0), started_at=st.get("StartedAt"), finished_at=st.get("FinishedAt"),
                       exit_code=st.get("ExitCode"), oom_killed=bool(st.get("OOMKilled")),
                       restart_policy=((ins.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name"))
            h = st.get("Health") or {}
            if h.get("Status") == "unhealthy":
                out["health_log"] = ((h.get("Log") or [{}])[-1].get("Output") or "").strip()[:200]
        except Exception:
            pass
        if c.get("State") != "running":
            return out
        try:
            s = (await self.http.get(f"/containers/{cid}/stats", params={"stream": "false", "one-shot": "true"}, timeout=8)).json()
            cpu = s.get("cpu_stats") or {}
            tot, sysu = (cpu.get("cpu_usage") or {}).get("total_usage"), cpu.get("system_cpu_usage")
            prev = self.prev.get(cid)
            if prev and tot is not None and sysu and prev[1] and sysu > prev[1] and tot >= prev[0]:
                out["cpu_percent"] = round((tot - prev[0]) / (sysu - prev[1]) * 100, 2)   # share of the whole host
            self.prev[cid] = (tot, sysu)
            mem = s.get("memory_stats") or {}
            if mem.get("usage") is not None:
                out["mem"] = mem["usage"] - (mem.get("stats") or {}).get("inactive_file", 0)
            nets = s.get("networks") or {}
            if nets:
                out["net_rx"] = sum(n.get("rx_bytes", 0) for n in nets.values())
                out["net_tx"] = sum(n.get("tx_bytes", 0) for n in nets.values())
            rd = wr = 0
            for e in (s.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []:
                op = (e.get("op") or "").lower()
                rd += e.get("value", 0) if op == "read" else 0
                wr += e.get("value", 0) if op == "write" else 0
            out.update(blk_read=rd, blk_write=wr, pids=(s.get("pids_stats") or {}).get("current"))
        except Exception:
            pass
        return out

    async def collect(self) -> dict:
        if time.time() - self.ts < 10:
            return self.cache
        self.ts = time.time()
        if not os.path.exists(self.sock):
            self.cache = {"available": False, "error": "docker.sock nicht gemountet", "containers": []}
            return self.cache
        try:
            r = await self.http.get("/containers/json", params={"all": "true"}, timeout=5)
            r.raise_for_status()
            raw = r.json()
            details = await asyncio.gather(*(self._detail(c) for c in raw))
            ids = {c["Id"] for c in raw}
            self.prev = {k: v for k, v in self.prev.items() if k in ids}
            cs = []
            for c, det in zip(raw, details):
                status = c.get("Status", "")
                health = None
                for h in ("healthy", "unhealthy", "health: starting"):
                    if f"({h})" in status:
                        health = h.replace("health: ", "")
                cs.append({
                    "name": (c.get("Names") or ["?"])[0].lstrip("/"), "image": c.get("Image"),
                    "state": c.get("State"), "status": status, "health": health,
                    "project": (c.get("Labels") or {}).get("com.docker.compose.project"),
                    **det,
                })
            cs.sort(key=lambda x: (x["state"] != "running", x["name"]))
            self.cache = {"available": True, "containers": cs}
        except Exception as e:  # noqa: BLE001
            self.cache = {"available": False, "error": str(e), "containers": self.cache.get("containers", [])}
        return self.cache


# --------------------------------------------------------------------------- flatten
def flatten(s: dict) -> dict:
    """Snapshot -> flat dict of SENSORS keys."""
    sysd, gpu, ol, ow, dk = s["system"], s["gpu"], s["ollama"], s["openwebui"], s["docker"]
    pw = s.get("power") or {}
    price = pw.get("price_eur_kwh")
    mem, temps, net = sysd["memory"], sysd["temps"], sysd["net"]
    root = next((d for d in sysd["disks"] if d["mount"] == "/"), sysd["disks"][0] if sysd["disks"] else None)
    freqs = sysd["cpu"]["freq_mhz"]
    loaded = ol.get("loaded") or []
    logs = ol.get("requests") or {}
    lr = ow.get("last_response") or {}

    def r(v, n=1):
        return round(v, n) if isinstance(v, (int, float)) else None

    return {
        "cpu_percent": r(sysd["cpu"]["percent"]),
        "load_1": r(sysd["cpu"]["load"][0], 2),
        "cpu_temp": r(temps["cpu_max"]),
        "cpu_freq_mhz": r(sum(freqs) / len(freqs), 0) if freqs else None,
        "mem_used_percent": r(mem["used"] / mem["total"] * 100 if mem["total"] else None),
        "mem_used_gib": r(mem["used"] / GiB, 2),
        "mem_available_gib": r(mem["available"] / GiB, 2),
        "swap_used_percent": r(mem["swap_percent"]),
        "disk_used_percent": r(root["percent"]) if root else None,
        "disk_free_gb": r(root["free"] / 1e9, 1) if root else None,
        "disk_read_kbps": r(sysd["disk_io"]["read_bps"] / 1000),
        "disk_write_kbps": r(sysd["disk_io"]["write_bps"] / 1000),
        "nvme_temp": r(temps["nvme"]),
        "net_rx_kbps": r(net["rx_bps"] / 1000),
        "net_tx_kbps": r(net["tx_bps"] / 1000),
        "boot_time": _iso(sysd["boot_time"]),
        "gpu_util": gpu.get("util"),
        "gpu_temp": gpu.get("temp"),
        "gpu_power": r(gpu.get("power"), 2),
        "gpu_clock_mhz": gpu.get("clock_mhz"),
        "ollama_up": bool(ol.get("up")),
        "ollama_loaded_count": len(loaded),
        "ollama_loaded_model": ", ".join(m["name"] for m in loaded) if loaded else "keins",
        "ollama_loaded_gib": r(sum((m.get("size_vram") or m.get("size") or 0) for m in loaded) / GiB, 2),
        "ollama_installed_count": len(ol.get("installed") or []),
        "ollama_requests_1h": logs.get("requests_1h"),
        "ollama_requests_24h": logs.get("requests_24h"),
        "ollama_avg_duration_1h": logs.get("avg_duration_1h"),
        "ollama_last_request": _iso(logs.get("last_request")),
        "openwebui_up": bool(ow.get("up")),
        "openwebui_users": ow.get("users"),
        "openwebui_active_users_24h": ow.get("active_users_24h"),
        "openwebui_chats": ow.get("chats"),
        "openwebui_messages_24h": ow.get("messages_24h"),
        "openwebui_tokens_24h": ow.get("tokens_24h"),
        "openwebui_tokens_total": ow.get("tokens_total"),
        "openwebui_last_tps": r(lr.get("tps")),
        "updates_pending": (s.get("updates") or {}).get("count"),
        "updates_security": (s.get("updates") or {}).get("security"),
        "reboot_required": bool((s.get("updates") or {}).get("reboot_required")),
        "power_w": r(pw.get("power")) if pw.get("up") else None,
        "power_voltage": r(pw.get("voltage")) if pw.get("up") else None,
        "power_current": r(pw.get("current"), 3) if pw.get("up") else None,
        "energy_today_kwh": r(pw.get("today_kwh"), 3),
        "energy_week_kwh": r(pw.get("week_kwh"), 3),
        "energy_month_kwh": r(pw.get("month_kwh"), 3),
        "energy_meter_kwh": r(pw.get("meter_total_kwh"), 3) if pw.get("up") else None,
        "energy_cost_today_eur": r(pw["today_kwh"] * price, 2) if price is not None and pw.get("today_kwh") is not None else None,
        "energy_cost_month_eur": r(pw["month_kwh"] * price, 2) if price is not None and pw.get("month_kwh") is not None else None,
        "containers_running": sum(1 for c in dk.get("containers", []) if c["state"] == "running") if dk.get("available") else None,
    }
