"""Health & operations collectors.

Service checks, container details, pressure/OOM, journal (errors, SSH, failed units),
voice-service usage, NVMe SMART, ping and energy attribution per AI reply.
All read-only and unprivileged.
"""
from __future__ import annotations

import asyncio
import bisect
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone

import httpx

log = logging.getLogger("health")

DOCKER_SOCK = "/var/run/docker.sock"


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def _journal(args: list[str], timeout: int = 60):
    """Run journalctl -o json and yield records with decoded MESSAGE and _ts (epoch seconds)."""
    out = subprocess.run(["journalctl", "--no-pager", "-q", "-o", "json", *args],
                         capture_output=True, text=True, timeout=timeout)
    for line in out.stdout.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg = rec.get("MESSAGE")
        if isinstance(msg, list):
            try:
                msg = bytes(msg).decode("utf-8", "replace")
            except Exception:
                msg = ""
        rec["MESSAGE"] = msg or ""
        rec["_ts"] = int(rec.get("__REALTIME_TIMESTAMP", 0) or 0) / 1e6
        yield rec


_dns: dict[str, str] = {}


def resolve(ip: str) -> str:
    if ip not in _dns:
        try:
            _dns[ip] = socket.gethostbyaddr(ip)[0]
        except Exception:
            _dns[ip] = ""
    return _dns[ip]


class Periodic:
    """Runs fn() every `interval` seconds in a daemon thread; latest result in .data."""

    def __init__(self, name: str, interval: float, fn, initial: dict | None = None) -> None:
        self.name, self.interval, self.fn = name, interval, fn
        self.data: dict = initial or {}
        threading.Thread(target=self._run, daemon=True, name=name).start()

    def _run(self) -> None:
        while True:
            try:
                self.data = self.fn()
            except Exception as e:  # noqa: BLE001
                log.exception("%s failed", self.name)
                self.data = {**self.data, "error": str(e)}
            time.sleep(self.interval)


# --------------------------------------------------------------------------- service checks
class ServiceChecker:
    """HTTP health of Ollama / Open WebUI and Wyoming `describe` round-trip for Whisper/Piper."""

    def __init__(self, cfg, http: httpx.AsyncClient) -> None:
        self.cfg, self.http = cfg, http
        self.cache: list = []
        self.ts = 0.0
        self.since: dict[str, tuple[bool, float]] = {}

    async def _http(self, url: str) -> dict:
        t0 = time.perf_counter()
        try:
            r = await self.http.get(url, timeout=5)
            return {"ok": r.status_code < 400, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "info": f"HTTP {r.status_code}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "latency_ms": None, "info": str(e)[:160] or type(e).__name__}

    @staticmethod
    async def _wyoming(host: str, port: int) -> dict:
        t0 = time.perf_counter()
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 3)
            try:
                writer.write(b'{"type": "describe"}\n')
                await writer.drain()
                hdr = json.loads(await asyncio.wait_for(reader.readline(), 5))
                data = hdr.get("data") or {}
                dl = int(hdr.get("data_length") or 0)
                if dl:
                    data.update(json.loads(await asyncio.wait_for(reader.readexactly(dl), 5)))
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            lat = round((time.perf_counter() - t0) * 1000, 1)
            models = [m.get("name") for p in data.get("asr") or [] for m in p.get("models") or [] if m.get("name")]
            voices = [v for p in data.get("tts") or [] for v in p.get("voices") or []]
            info = ", ".join(models[:3]) if models else f"{len(voices)} Stimmen" if voices else ""
            return {"ok": hdr.get("type") == "info", "latency_ms": lat, "info": info or "Wyoming"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "latency_ms": None, "info": str(e)[:160] or type(e).__name__}

    async def collect(self) -> list:
        now = time.time()
        if now - self.ts < 30 and self.cache:
            return self.cache
        self.ts = now
        checks = [
            ("ollama", "Ollama API", f"{self.cfg['ollama_url'].rstrip('/')}/api/version", self._http(f"{self.cfg['ollama_url'].rstrip('/')}/api/version")),
            ("openwebui", "Open WebUI", f"{self.cfg['openwebui_url'].rstrip('/')}/health", self._http(f"{self.cfg['openwebui_url'].rstrip('/')}/health")),
        ]
        for v in self.cfg["voice_services"]:
            checks.append((v["key"], v["name"], f"tcp://{v.get('host', '127.0.0.1')}:{v['port']}",
                           self._wyoming(v.get("host", "127.0.0.1"), int(v["port"]))))
        results = await asyncio.gather(*(c[3] for c in checks))
        out = []
        for (key, name, target, _), r in zip(checks, results):
            prev = self.since.get(key)
            if not prev or prev[0] != r["ok"]:
                self.since[key] = (r["ok"], now)
            out.append({"key": key, "name": name, "target": target, **r, "since": self.since[key][1]})
        self.cache = out
        return out


# --------------------------------------------------------------------------- pressure / OOM
def read_pressure() -> dict:
    out: dict = {}
    for res in ("cpu", "memory", "io"):
        d = {}
        for line in _read(f"/proc/pressure/{res}").splitlines():
            parts = line.split()
            if not parts:
                continue
            vals = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
            d[parts[0]] = {k: float(v) for k, v in vals.items() if k != "total"}
        out[res] = d
    oom = None
    for line in _read("/proc/vmstat").splitlines():
        if line.startswith("oom_kill "):
            oom = int(line.split()[1])
    out["oom_kills_boot"] = oom
    out["available"] = bool(out["memory"])
    return out


# --------------------------------------------------------------------------- journal
SSH_ACCEPT = re.compile(r"Accepted (\S+) for (\S+) from (\S+) port")
SSH_FAIL = re.compile(r"Failed (\S+) for (?:invalid user )?(\S*) from (\S+) port")
SSH_INVALID = re.compile(r"Invalid user (\S*) from (\S+) port")
OOM_RE = re.compile(r"Killed process (\d+) \(([^)]*)\)")
MID_STARTED = "39f53479d3a045ac8e11786248231fbf"
MID_JOB_FAILED = "be02cf6855d2428ba40df7e9d022f03d"
MID_RESULT = "d9b373ed55a64feb8242e02dbe79a49c"
MID_SUCCESS = "7ad2d189f7e94e70a38c781354912448"
MID_STOPPED = "9d1aaa27d60140bd96365438aad20286"
PRIO = {0: "emerg", 1: "alert", 2: "crit", 3: "err"}


class JournalCollector:
    """Errors (24 h), SSH logins (7 d), OOM kills (7 d), failed units (current boot)."""

    def collect(self) -> dict:
        now = time.time()
        out: dict = {"checked": now}

        # errors, priority err and worse
        errs = list(_journal(["--since=-24h", "-p", "3", "-n", "20000",
                              "--output-fields=MESSAGE,SYSLOG_IDENTIFIER,_COMM,PRIORITY"]))
        ident = Counter((r.get("SYSLOG_IDENTIFIER") or r.get("_COMM") or "?") for r in errs)
        out["errors"] = {
            "count_24h": len(errs),
            "count_1h": sum(1 for r in errs if r["_ts"] >= now - 3600),
            "by_source": [{"source": k, "count": v} for k, v in ident.most_common(8)],
            "recent": [{"ts": r["_ts"], "source": r.get("SYSLOG_IDENTIFIER") or r.get("_COMM") or "?",
                        "prio": PRIO.get(int(r.get("PRIORITY") or 3), "err"), "msg": r["MESSAGE"][:300]}
                       for r in reversed(errs[-12:])],
        }

        # SSH
        acc, fails = [], []
        for r in _journal(["--since=-7d", "-t", "sshd", "-t", "sshd-session", "--output-fields=MESSAGE"]):
            m = r["MESSAGE"]
            if (a := SSH_ACCEPT.search(m)):
                acc.append({"ts": r["_ts"], "method": a.group(1), "user": a.group(2), "ip": a.group(3)})
            elif (f := SSH_FAIL.search(m)):
                fails.append({"ts": r["_ts"], "user": f.group(2), "ip": f.group(3)})
            elif (i := SSH_INVALID.search(m)):
                fails.append({"ts": r["_ts"], "user": i.group(1), "ip": i.group(2)})
        f24 = [f for f in fails if f["ts"] >= now - 86400]
        out["ssh"] = {
            "failed_24h": len(f24), "failed_7d": len(fails), "logins_7d": len(acc),
            "fail_sources": [{"ip": ip, "name": resolve(ip), "count": c} for ip, c in Counter(f["ip"] for f in fails).most_common(6)],
            "recent_logins": [{**a, "name": resolve(a["ip"])} for a in reversed(acc[-8:])],
            "last_login": acc[-1] if acc else None,
        }

        # OOM kills (kernel)
        ooms = []
        for r in _journal(["--since=-7d", "_TRANSPORT=kernel", "--output-fields=MESSAGE"]):
            if "Killed process" in r["MESSAGE"] and (o := OOM_RE.search(r["MESSAGE"])):
                ooms.append({"ts": r["_ts"], "pid": int(o.group(1)), "process": o.group(2),
                             "cgroup": "memory cgroup" in r["MESSAGE"].lower()})
        out["oom"] = {"kills_24h": sum(1 for o in ooms if o["ts"] >= now - 86400), "kills_7d": len(ooms),
                      "recent": list(reversed(ooms[-8:]))}

        # failed units: last lifecycle event per unit (current boot) is a failure
        last: dict[str, dict] = {}
        for r in _journal(["-b", "_PID=1", "--output-fields=MESSAGE_ID,UNIT,MESSAGE",
                           *(f"MESSAGE_ID={m}" for m in (MID_STARTED, MID_JOB_FAILED, MID_RESULT, MID_SUCCESS, MID_STOPPED))]):
            unit = r.get("UNIT")
            if unit:
                last[unit] = {"mid": r.get("MESSAGE_ID"), "ts": r["_ts"], "msg": r["MESSAGE"][:200]}
        failed = [{"unit": u, "ts": e["ts"], "msg": e["msg"]} for u, e in last.items()
                  if e["mid"] in (MID_JOB_FAILED, MID_RESULT)]
        out["failed_units"] = sorted(failed, key=lambda x: -x["ts"])
        out["available"] = True
        return out


# --------------------------------------------------------------------------- voice usage (Wyoming)
TCP_LISTEN, TCP_TIME_WAIT = "0A", "06"


def _hex_ip(h: str) -> str:
    b = bytes.fromhex(h)
    if len(b) == 4:
        return socket.inet_ntoa(b[::-1])
    b = b"".join(b[i:i + 4][::-1] for i in range(0, 16, 4))
    ip = socket.inet_ntop(socket.AF_INET6, b)
    return ip[7:] if ip.startswith("::ffff:") else ip


class VoiceUsage:
    """Counts client connections to Wyoming services by polling the container's TCP table.

    Whisper/Piper do not log requests; every Home Assistant STT/TTS call opens one TCP connection.
    Loopback and the Docker gateway (our own probe, healthchecks) are ignored.
    """

    def __init__(self, cfg) -> None:
        self.services = cfg["voice_services"]
        self.http = httpx.Client(transport=httpx.HTTPTransport(uds=DOCKER_SOCK), base_url="http://docker")
        self.info: dict[str, dict] = {}
        self.open: dict[tuple, list] = {}
        self.seen: dict[tuple, float] = {}
        self.events = {s["key"]: deque(maxlen=50_000) for s in self.services}
        self.lock = threading.Lock()
        self.error: str | None = None
        threading.Thread(target=self._run, daemon=True, name="voice-usage").start()

    def _container(self, svc: dict) -> dict:
        i = self.info.get(svc["key"])
        if i and time.time() - i["ts"] < 60 and i["pid"] and os.path.exists(f"/proc/{i['pid']}/net/tcp"):
            return i
        ins = self.http.get(f"/containers/{svc['container']}/json", timeout=5).json()
        st = ins.get("State") or {}
        nets = ((ins.get("NetworkSettings") or {}).get("Networks") or {}).values()
        i = {"pid": st.get("Pid") or 0, "running": bool(st.get("Running")), "ts": time.time(),
             "ignore": {n.get("Gateway") for n in nets if n.get("Gateway")} | {n.get("IPv6Gateway") for n in nets if n.get("IPv6Gateway")}}
        self.info[svc["key"]] = i
        return i

    def _poll(self, svc: dict, now: float) -> None:
        i = self._container(svc)
        if not i["pid"]:
            return
        port = int(svc.get("container_port") or svc["port"])
        cur: dict[tuple, str] = {}
        for fn in ("tcp", "tcp6"):
            for line in _read(f"/proc/{i['pid']}/net/{fn}").splitlines()[1:]:
                p = line.split()
                if len(p) < 4 or p[3] == TCP_LISTEN:
                    continue
                lip, lport = p[1].rsplit(":", 1)
                if int(lport, 16) != port:
                    continue
                rip, rport = p[2].rsplit(":", 1)
                ip = _hex_ip(rip)
                if ip.startswith("127.") or ip == "::1" or ip in i["ignore"]:
                    continue
                cur[(svc["key"], ip, int(rport, 16))] = p[3]
        with self.lock:
            for key, st in cur.items():
                if key in self.open:
                    if st == TCP_TIME_WAIT:
                        self._finish(key, now)
                    else:
                        self.open[key][1] = now
                        self.seen[key] = now + 180
                elif key not in self.seen:
                    self.seen[key] = now + 180
                    if st == TCP_TIME_WAIT:          # whole connection happened between two polls
                        self.events[key[0]].append((now, 0.0, key[1]))
                    else:
                        self.open[key] = [now, now]
            for key in [k for k in self.open if k[0] == svc["key"] and k not in cur]:
                self._finish(key, now)

    def _finish(self, key: tuple, now: float) -> None:
        first, last = self.open.pop(key)
        self.seen[key] = now + 180
        self.events[key[0]].append((first, max(0.0, last - first), key[1]))

    def _run(self) -> None:
        while True:
            now = time.time()
            try:
                for svc in self.services:
                    self._poll(svc, now)
                self.error = None
            except Exception as e:  # noqa: BLE001
                if str(e) != self.error:
                    log.warning("voice usage: %s", e)
                self.error = str(e)
                self.info.clear()
            if int(now) % 60 == 0:
                with self.lock:
                    self.seen = {k: v for k, v in self.seen.items() if v > now}
            time.sleep(1)

    def stats(self) -> dict:
        now = time.time()
        out = {}
        with self.lock:
            for svc in self.services:
                ev = self.events[svc["key"]]
                while ev and ev[0][0] < now - 7 * 86400:
                    ev.popleft()
                evs = list(ev)
                active = sum(1 for k in self.open if k[0] == svc["key"])
                d24 = [e for e in evs if e[0] >= now - 86400]
                hourly = [0] * 24
                for e in d24:
                    idx = 23 - int((now - e[0]) // 3600)
                    if 0 <= idx < 24:
                        hourly[idx] += 1
                durs = [e[1] for e in d24 if e[1] > 0]
                out[svc["key"]] = {
                    "name": svc["name"], "container": svc["container"], "active": active,
                    "requests_1h": sum(1 for e in evs if e[0] >= now - 3600), "requests_24h": len(d24),
                    "requests_7d": len(evs), "avg_duration_24h": round(sum(durs) / len(durs), 1) if durs else None,
                    "last_use": evs[-1][0] if evs else None, "hourly": hourly,
                    "clients": [{"ip": ip, "name": resolve(ip), "count": c} for ip, c in Counter(e[2] for e in evs).most_common(5)],
                }
        return {"available": self.error is None, "error": self.error, "services": out}


# --------------------------------------------------------------------------- NVMe SMART
class NvmeCollector:
    """Identity from sysfs (unprivileged); SMART health from a smartctl JSON file written on the host."""

    def __init__(self, cfg) -> None:
        self.file = cfg["smart_file"]

    def collect(self) -> dict:
        ctrls = []
        base = "/sys/class/nvme"
        try:
            names = sorted(os.listdir(base))
        except OSError:
            names = []
        for n in names:
            ctrls.append({"name": n, "model": _read(f"{base}/{n}/model"), "firmware": _read(f"{base}/{n}/firmware_rev"),
                          "serial": _read(f"{base}/{n}/serial")})
        out: dict = {"controllers": ctrls, "smart": None, "smart_file": self.file}
        try:
            st = os.stat(self.file)
            with open(self.file, encoding="utf-8") as f:
                j = json.load(f)
        except FileNotFoundError:
            out["smart_error"] = "missing"
            return out
        except Exception as e:  # noqa: BLE001
            out["smart_error"] = str(e)
            return out
        h = j.get("nvme_smart_health_information_log") or {}
        if not h:
            msgs = [m.get("string") for m in (j.get("smartctl") or {}).get("messages") or []]
            out["smart_error"] = "; ".join(m for m in msgs if m) or "keine SMART-Daten in der Datei"
            return out
        unit = 512 * 1000
        out["smart"] = {
            "age": time.time() - st.st_mtime, "updated": st.st_mtime,
            "device": (j.get("device") or {}).get("name"), "model": j.get("model_name"),
            "capacity": (j.get("user_capacity") or {}).get("bytes") or ((j.get("nvme_namespaces") or [{}])[0].get("size") or {}).get("bytes"),
            "passed": (j.get("smart_status") or {}).get("passed"),
            "critical_warning": h.get("critical_warning"), "temperature": h.get("temperature"),
            "available_spare": h.get("available_spare"), "spare_threshold": h.get("available_spare_threshold"),
            "percentage_used": h.get("percentage_used"),
            "data_read": (h.get("data_units_read") or 0) * unit, "data_written": (h.get("data_units_written") or 0) * unit,
            "power_on_hours": h.get("power_on_hours"), "power_cycles": h.get("power_cycles"),
            "unsafe_shutdowns": h.get("unsafe_shutdowns"), "media_errors": h.get("media_errors"),
            "error_log_entries": h.get("num_err_log_entries"),
        }
        return out


# --------------------------------------------------------------------------- ping
PING_LOSS = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
PING_RTT = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms")


def default_gateway() -> str | None:
    for line in _read("/proc/net/route").splitlines()[1:]:
        p = line.split()
        if len(p) > 2 and p[1] == "00000000":
            return socket.inet_ntoa(bytes.fromhex(p[2])[::-1])
    return None


class PingCollector:
    def __init__(self, cfg) -> None:
        self.targets = cfg["ping_targets"]

    @staticmethod
    def ping(host: str, count: int = 5) -> dict:
        try:
            r = subprocess.run(["ping", "-n", "-q", "-c", str(count), "-i", "0.2", "-W", "1", host],
                               capture_output=True, text=True, timeout=count + 10)
        except FileNotFoundError:
            return {"ok": False, "error": "ping nicht installiert"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "loss": 100.0, "error": "Timeout"}
        m, rtt = PING_LOSS.search(r.stdout), PING_RTT.search(r.stdout)
        if not m:
            return {"ok": False, "loss": 100.0, "error": (r.stderr or r.stdout).strip()[:160]}
        sent, recv = int(m.group(1)), int(m.group(2))
        return {"ok": recv > 0, "loss": round((sent - recv) / sent * 100, 1) if sent else 100.0,
                "min": float(rtt.group(1)) if rtt else None, "avg": float(rtt.group(2)) if rtt else None,
                "max": float(rtt.group(3)) if rtt else None, "jitter": float(rtt.group(4)) if rtt else None}

    def collect(self) -> dict:
        out = []
        for t in self.targets:
            host = default_gateway() if t["host"] == "gateway" else t["host"]
            res = self.ping(host) if host else {"ok": False, "error": "kein Default-Gateway"}
            out.append({"key": t["key"], "name": t["name"], "host": host, **res})
        return {"targets": out, "checked": time.time()}


# --------------------------------------------------------------------------- energy attribution
class EnergyAttribution:
    """Extra energy above idle during each AI reply.

    Baseline = 10th percentile of plug power over the last 24 h. For each Open WebUI reply the excess
    power (plug minus baseline) is integrated over [created_at, created_at + total_duration] using the
    averaged history samples. Overlapping requests are counted once per request.
    """

    def __init__(self, history, owui, tailer) -> None:
        self.history, self.owui, self.tailer = history, owui, tailer

    def collect(self) -> dict:
        now = time.time()
        samples = self.history.series("power_w", now - 7 * 86400)
        day = sorted(v for t, v in samples if t >= now - 86400)
        if len(day) < 60:
            return {"available": False, "reason": "Noch zu wenig Leistungsmesswerte (Shelly)."}
        baseline = day[int(len(day) * 0.10)]
        ts = [t for t, _ in samples]
        iv = self.history.interval
        spans = []
        for k, (t, v) in enumerate(samples):
            prev = samples[k - 1][0] if k else t - iv
            spans.append((prev if t - prev <= 3 * iv else t - iv, t, v))

        def excess(a: float, b: float) -> tuple[float, float]:
            i = bisect.bisect_left(ts, a)
            wh = cov = 0.0
            while i < len(spans) and spans[i][0] < b:
                s0, s1, v = spans[i]
                ov = min(s1, b) - max(s0, a)
                if ov > 0:
                    wh += max(0.0, v - baseline) * ov / 3600
                    cov += ov
                i += 1
            return wh, cov

        per: dict[str, dict] = {}
        first = ts[0]
        for model, start, dur, out_t in getattr(self.owui, "recent", []):
            if dur <= 0 or start < first:
                continue
            wh, cov = excess(start, start + dur)
            if cov < 0.5 * dur:
                continue
            m = per.setdefault(model, {"model": model, "replies": 0, "wh": 0.0, "out_tokens": 0, "seconds": 0.0})
            m["replies"] += 1
            m["wh"] += wh
            m["out_tokens"] += out_t
            m["seconds"] += dur
        models = []
        for m in per.values():
            models.append({**m, "wh": round(m["wh"], 3), "wh_per_reply": round(m["wh"] / m["replies"], 3),
                           "wh_per_1k_tokens": round(m["wh"] / m["out_tokens"] * 1000, 3) if m["out_tokens"] else None,
                           "avg_seconds": round(m["seconds"] / m["replies"], 1),
                           "avg_w": round(m["wh"] * 3600 / m["seconds"], 1) if m["seconds"] else None})
        models.sort(key=lambda x: -x["replies"])
        # all Ollama inference requests in the last 24 h (any client), from the GIN log
        req_wh = 0.0
        req_n = 0
        with self.tailer.lock:
            evs = [e for e in self.tailer.events if e[0] >= now - 86400]
        for e in evs:
            if e[4] <= 0 or e[0] - e[4] < first:
                continue
            wh, cov = excess(e[0] - e[4], e[0])
            if cov >= 0.5 * e[4]:
                req_wh += wh
                req_n += 1
        replies = sum(m["replies"] for m in models)
        total = sum(m["wh"] for m in models)
        return {
            "available": True, "baseline_w": round(baseline, 1), "since": first, "models": models,
            "replies": replies, "wh_total": round(total, 3),
            "wh_per_reply": round(total / replies, 3) if replies else None,
            "ollama_requests_24h": req_n, "ollama_wh_24h": round(req_wh, 3),
            "checked": now,
        }


# --------------------------------------------------------------------------- problems + flat metrics
def problems(s: dict) -> list[str]:
    p: list[str] = []
    for sv in s.get("services") or []:
        if not sv["ok"]:
            p.append(f"{sv['name']} nicht erreichbar")
    for c in (s.get("docker") or {}).get("containers", []):
        if c.get("health") == "unhealthy":
            p.append(f"Container {c['name']} unhealthy")
        elif c.get("state") == "restarting":
            p.append(f"Container {c['name']} startet ständig neu")
        elif c.get("state") == "exited" and c.get("exit_code") not in (0, None):
            p.append(f"Container {c['name']} beendet (Code {c['exit_code']})")
    j = s.get("journal") or {}
    for u in j.get("failed_units") or []:
        p.append(f"Unit {u['unit']} fehlgeschlagen")
    if (j.get("oom") or {}).get("kills_24h"):
        p.append(f"{j['oom']['kills_24h']} OOM-Kill(s) in 24 h")
    if ((j.get("ssh") or {}).get("failed_24h") or 0) >= 20:
        p.append(f"{j['ssh']['failed_24h']} fehlgeschlagene SSH-Logins (24 h)")
    mem = (s.get("pressure") or {}).get("memory") or {}
    if (mem.get("full") or {}).get("avg60", 0) >= 5 or (mem.get("some") or {}).get("avg60", 0) >= 20:
        p.append("Hoher Speicherdruck")
    th = (s.get("gpu") or {}).get("throttle") or {}
    if th.get("critical"):
        p.append(f"GPU gedrosselt: {', '.join(th.get('reasons_text') or [])}")
    m = s.get("metrics") or {}
    if (m.get("cpu_temp") or 0) >= 90:
        p.append(f"CPU-Temperatur {m['cpu_temp']:.0f} °C")
    if (m.get("gpu_temp") or 0) >= 85:
        p.append(f"GPU-Temperatur {m['gpu_temp']:.0f} °C")
    if (m.get("nvme_temp") or 0) >= 70:
        p.append(f"NVMe-Temperatur {m['nvme_temp']:.0f} °C")
    if (m.get("disk_used_percent") or 0) >= 90:
        p.append(f"Disk zu {m['disk_used_percent']:.0f} % voll")
    sm = (s.get("nvme") or {}).get("smart") or {}
    if sm:
        if sm.get("critical_warning"):
            p.append(f"NVMe kritische Warnung ({sm['critical_warning']})")
        if sm.get("media_errors"):
            p.append(f"NVMe {sm['media_errors']} Medienfehler")
        if (sm.get("percentage_used") or 0) >= 90:
            p.append(f"NVMe zu {sm['percentage_used']} % verschlissen")
    for t in (s.get("ping") or {}).get("targets") or []:
        if t.get("loss") is not None and t["loss"] >= 50:
            p.append(f"{t['name']} ({t['host']}): {t['loss']:.0f} % Paketverlust")
    pw = s.get("power") or {}
    if pw.get("configured") and not pw.get("up"):
        p.append("Steckdose (Shelly) nicht erreichbar")
    return p


def flatten(s: dict) -> dict:
    def r(v, n=1):
        return round(v, n) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    svc = {x["key"]: x for x in s.get("services") or []}
    pr = s.get("pressure") or {}
    j = s.get("journal") or {}
    sm = (s.get("nvme") or {}).get("smart") or {}
    ping = {t["key"]: t for t in (s.get("ping") or {}).get("targets") or []}
    voice = (s.get("voice") or {}).get("services") or {}
    th = (s.get("gpu") or {}).get("throttle") or {}
    loads = (s.get("ollama") or {}).get("loads") or []
    ea = s.get("energy_attr") or {}
    dk = (s.get("docker") or {}).get("containers") or []
    probs = s.get("problems") or []
    last_login = (j.get("ssh") or {}).get("last_login")

    def psi(res, kind="some"):
        return r(((pr.get(res) or {}).get(kind) or {}).get("avg60"))

    out = {
        "problem": bool(probs),
        "problem_count": len(probs),
        "problem_text": ("; ".join(probs) or "OK")[:250],
        "failed_units": len(j["failed_units"]) if "failed_units" in j else None,
        "containers_unhealthy": sum(1 for c in dk if c.get("health") == "unhealthy" or c.get("state") == "restarting"
                                    or (c.get("state") == "exited" and c.get("exit_code") not in (0, None))),
        "psi_cpu_some": psi("cpu"), "psi_mem_some": psi("memory"), "psi_mem_full": psi("memory", "full"),
        "psi_io_some": psi("io"),
        "oom_kills": pr.get("oom_kills_boot"),
        "oom_kills_24h": (j.get("oom") or {}).get("kills_24h"),
        "gpu_throttled": bool(th.get("active")),
        "gpu_throttle_reason": ", ".join(th.get("reasons_text") or []) or "keine",
        "nvme_wear_percent": sm.get("percentage_used"),
        "nvme_data_written_tb": r(sm["data_written"] / 1e12, 3) if sm.get("data_written") is not None else None,
        "nvme_media_errors": sm.get("media_errors"),
        "nvme_spare_percent": sm.get("available_spare"),
        "nvme_power_on_hours": sm.get("power_on_hours"),
        "journal_errors_24h": (j.get("errors") or {}).get("count_24h"),
        "ssh_failed_24h": (j.get("ssh") or {}).get("failed_24h"),
        "ssh_last_login": datetime.fromtimestamp(last_login["ts"], tz=timezone.utc).isoformat(timespec="seconds") if last_login else None,
        "ollama_last_load_s": r(loads[0]["seconds"]) if loads else None,
        "ai_energy_per_reply_wh": r(ea.get("wh_per_reply"), 3),
    }
    for key, v in svc.items():           # ollama/openwebui already exposed as *_up elsewhere
        if key not in ("ollama", "openwebui"):
            out[f"{key}_up"] = bool(v["ok"])
            out[f"{key}_latency_ms"] = r(v.get("latency_ms"))
    for key, v in voice.items():
        out[f"{key}_requests_24h"] = v.get("requests_24h")
    for key, t in ping.items():
        out[f"ping_{key}_ms"] = r(t.get("avg"), 2)
        out[f"ping_{key}_loss"] = r(t.get("loss"), 0)
    return out
