"""Regression tests for the run-1 security audit fixes (stdlib only: python -m unittest)."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from app import collectors as C  # noqa: E402
from app import health as H  # noqa: E402
from app.config import same_broker_target, stored_broker_password, validate_broker  # noqa: E402
from app.sensors import HISTORY_KEYS  # noqa: E402
from app.store import EnergyLedger, History  # noqa: E402


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "energy.db")

    def test_rejects_non_numeric_and_non_finite(self):
        led = EnergyLedger(self.path)
        for bad in ("x", "123", float("inf"), float("nan"), True, None, [1], -5, 1e13):
            with self.assertRaises(ValueError, msg=repr(bad)):
                led.update(bad)
        self.assertEqual(led.con.execute("select count(*) from energy_daily").fetchone()[0], 0)
        self.assertEqual(led.summary(), {})

    def test_corrupt_persisted_row_is_skipped_and_repaired(self):
        con = sqlite3.connect(self.path)
        con.execute("create table energy_daily (day text primary key, start_wh real, last_wh real)")
        con.execute("insert into energy_daily values ('2020-01-01', 'x', 'x')")
        con.execute("insert into energy_daily values (date('now','localtime'), 'x', 'y')")
        con.commit()
        con.close()
        led = EnergyLedger(self.path)
        led.summary()                      # must not raise
        s = led.update(1000.0)
        self.assertEqual(s["today_kwh"], 0.0)
        s = led.update(1500.0)
        self.assertAlmostEqual(s["today_kwh"], 0.5)


class HistoryTests(unittest.TestCase):
    def test_non_finite_dropped_and_keys_deduplicated(self):
        h = History(os.path.join(tempfile.mkdtemp(), "h.db"), interval=0, days=1)
        h.add({"power_w": float("inf"), "load_1": 1.0})
        h.add({"power_w": 5.0, "load_1": float("nan")})
        rows = [json.loads(r[0]) for r in h.con.execute("select data from samples")]
        self.assertTrue(all("Infinity" not in json.dumps(r) and "NaN" not in json.dumps(r) for r in rows))
        q = h.query(3600, ["load_1"] * 2000 + ["nope"])
        self.assertEqual(list(q["series"]), ["load_1"])


class ConfigTests(unittest.TestCase):
    def test_password_not_replayed_to_new_destination(self):
        prev = {"host": "ha.local", "port": 1883, "username": "u", "tls": False, "password": "secret"}
        self.assertEqual(stored_broker_password(prev, dict(prev, password=None)), "secret")
        for change in ({"host": "evil.lan"}, {"port": 1884}, {"username": "v"}, {"tls": True}):
            self.assertFalse(same_broker_target(prev, {**prev, **change}))
            self.assertEqual(stored_broker_password(prev, {**prev, **change}), "")

    def test_topic_validation(self):
        base = {"host": "ha.local", "port": 1883, "base_topic": "metis", "discovery_prefix": "homeassistant"}
        self.assertIsNone(validate_broker(base))
        for bad in ({"base_topic": "$SYS"}, {"base_topic": "homeassistant/x"}, {"base_topic": "a\x00b"},
                    {"host": "a b"}):
            self.assertIsNotNone(validate_broker({**base, **bad}), bad)


class ParserTests(unittest.TestCase):
    def test_go_duration_and_gin(self):
        self.assertAlmostEqual(C.parse_go_duration("1m2.5s"), 62.5)
        self.assertEqual(C.parse_go_duration("."), 0)            # no ValueError any more
        self.assertIsInstance(C.parse_go_duration("1.2.3s"), float)   # odd input: number, no exception
        m = C.GIN_RE.search('[GIN] 2026/11/13 - 10:00:00 | 200 |  1.5s |  10.0.0.5 | POST "/api/chat"')
        self.assertEqual(m.group(5), "/api/chat")

    def test_num(self):
        self.assertIsNone(C._num(float("inf")))
        self.assertIsNone(C._num("1"))
        self.assertIsNone(C._num(True))
        self.assertEqual(C._num(2), 2.0)


class WyomingTests(unittest.TestCase):
    def test_oversized_data_length_rejected(self):
        async def run():
            async def handler(reader, writer):
                await reader.readline()
                writer.write(json.dumps({"type": "info", "data_length": 1 << 32}).encode() + b"\n")
                await writer.drain()
                writer.close()
            srv = await asyncio.start_server(handler, "127.0.0.1", 0)
            port = srv.sockets[0].getsockname()[1]
            async with srv:
                return await H.ServiceChecker._wyoming("127.0.0.1", port)
        r = asyncio.run(run())
        self.assertFalse(r["ok"])
        self.assertIn("Limit", r["info"])


class ClientsTests(unittest.TestCase):
    def test_invalid_ips_ignored_and_rows_capped(self):
        class Log:
            import threading
            lock = threading.Lock()
            events = []
        oc = H.OpenWebUIClients.__new__(H.OpenWebUIClients)
        oc.container, oc.ollama_log = "x", Log()
        from collections import deque
        oc.events, oc.web_ips, oc.matched = deque(maxlen=100_000), {}, {}
        import time as _t
        oc.since = _t.time() - 7 * 86400
        lines = []
        now = _t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime())
        for i in range(200):
            lines.append(f'{now}.000000000Z INFO - 10.9.{i // 250}.{i % 250}:1 - "POST /api/chat/completions HTTP/1.1" 401')
        lines.append(f'{now}.000000000Z INFO - ::::.:::1:1 - "POST /api/chat/completions HTTP/1.1" 401')
        body = ("\n".join(lines)).encode()

        class Resp:
            status_code = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def iter_bytes(self): yield body

        class Http:
            def stream(self, *a, **k): return Resp()
        oc.http = Http()
        H.resolve = lambda ip: ""                       # no DNS in tests
        out = oc.collect()
        self.assertEqual(out["clients_total"], 200)     # the bogus '::::.:::1' token is rejected
        self.assertEqual(len(out["clients"]), H.OpenWebUIClients.MAX_CLIENTS)


if __name__ == "__main__":
    unittest.main()
