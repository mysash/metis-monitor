"""Regression tests for the run-2 security audit fixes (stdlib only: python -m unittest)."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from app import collectors as C  # noqa: E402
from app import health as H  # noqa: E402
from app.config import same_broker_target, validate_broker  # noqa: E402
from app.store import EnergyLedger, History  # noqa: E402


class GinTests(unittest.TestCase):
    LINE = '[GIN] 2026/11/16 - 10:00:00 | 200 |    1.234s |       127.0.0.1 | POST     "/api/chat"'

    def test_normal_line(self):
        m = C.GIN_RE.match(self.LINE)
        self.assertEqual(m.groups(), ("200", "1.234s", "127.0.0.1", "POST", "/api/chat"))

    def test_backtracking_payload_is_fast(self):
        evil = ('[GIN] 2026/11/16 - 10:00:00 | 404 |  1.2us |  10.0.0.9 | GET      "/' + "a" * 2100
                + "[GIN]" * 50 + "|200|x" + " " * 1500 + 'y"')
        t0 = time.perf_counter()
        self.assertIsNone(C.GIN_RE.match(evil))
        self.assertLess(time.perf_counter() - t0, 0.05)


class SshTests(unittest.TestCase):
    def test_last_from_is_the_peer(self):
        m = H.SSH_FAIL.match("Failed password for invalid user x from 6.6.6.6 port 1 ssh2 from 192.0.2.7 port 22 ssh2")
        self.assertEqual(m.group(3), "192.0.2.7")

    def test_forged_accept_inside_username_not_matched(self):
        msg = "Invalid user Accepted publickey for root from 6.6.6.6 port 22 ssh2 from 192.0.2.7 port 5000"
        self.assertIsNone(H.SSH_ACCEPT.match(msg))
        self.assertEqual(H.SSH_INVALID.match(msg).group(2), "192.0.2.7")

    def test_accept_with_key_suffix(self):
        m = H.SSH_ACCEPT.match("Accepted publickey for root from 192.0.2.7 port 5000 ssh2: ED25519 SHA256:abc")
        self.assertEqual((m.group(2), m.group(3)), ("root", "192.0.2.7"))

    def test_multivalue_journal_fields(self):
        self.assertEqual(H._jfield(["sshd", "x"]), "sshd")
        self.assertEqual(H._jfield([104, 105]), "hi")
        self.assertIsNone(H._jfield([]))


class ConfigTests(unittest.TestCase):
    def test_tls_insecure_change_drops_password(self):
        prev = {"host": "ha.local", "port": 8883, "username": "u", "tls": True, "tls_insecure": False}
        self.assertFalse(same_broker_target(prev, {**prev, "tls_insecure": True}))

    def test_lone_surrogate_rejected(self):
        base = {"host": "ha.local", "port": 1883, "base_topic": "metis", "discovery_prefix": "homeassistant",
                "name": "x", "username": "", "password": ""}
        self.assertIsNone(validate_broker(base))
        self.assertIsNotNone(validate_broker({**base, "password": "\ud800"}))


class EnergyTests(unittest.TestCase):
    def test_meter_jump_not_counted(self):
        led = EnergyLedger(os.path.join(tempfile.mkdtemp(), "e.db"))
        led.update(1000.0)
        led.update(1010.0)
        s = led.update(1e9)                    # forged jump
        self.assertLess(s["today_kwh"], 0.1)
        s = led.update(1000.0 + 20)            # real meter again (looks like a reset)
        self.assertLess(s["today_kwh"], 0.1)

    def test_history_aggregate_overflow(self):
        h = History(os.path.join(tempfile.mkdtemp(), "h.db"), interval=3600, days=1)
        h.add({"power_w": 1.7e308, "cpu_percent": 5})
        h._acc_start = time.time() - 7200
        h.add({"power_w": 1.7e308, "cpu_percent": 5})
        rows = [json.loads(r[0]) for r in h.con.execute("select data from samples")]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("Infinity", json.dumps(rows))

    def test_shelly_range(self):
        self.assertIsNone(C._rng(1.7e308, -50_000, 50_000))
        self.assertEqual(C._rng(120.5, -50_000, 50_000), 120.5)


class ManifestTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX only")
    def test_fifo_skipped(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "fifo")
        os.mkfifo(p)
        self.assertIsNone(C._read_manifest(p))


if __name__ == "__main__":
    unittest.main()
