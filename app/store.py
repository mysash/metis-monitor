"""SQLite history: averaged samples, pruned after N days."""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
from datetime import date, timedelta

from .sensors import HISTORY_KEYS

log = logging.getLogger("store")


def finite(v) -> float | None:
    """Return v as float if it is a real, finite number (bools/strings/NaN/inf → None)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except (OverflowError, ValueError):
        return None
    return f if math.isfinite(f) else None


MAX_METER_WH = 1e12   # plausibility bound for a cumulative meter reading


class EnergyLedger:
    """Per-day energy from a cumulative meter (Wh). Survives meter resets; kept forever."""

    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.lock = threading.Lock()
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.execute("create table if not exists energy_daily (day text primary key, start_wh real, last_wh real)")
        self.con.commit()
        self._last_write = 0.0
        self._cache: dict[str, list[float]] = {}

    def update(self, total_wh: float) -> dict:
        total_wh = finite(total_wh)
        if total_wh is None or not (0 <= total_wh < MAX_METER_WH):
            raise ValueError("Zählerstand ungültig")
        today = date.today().isoformat()
        now = time.time()
        with self.lock:
            row = self._cache.get(today)
            if row is None:
                for d, (s, l) in self._cache.items():   # flush the finished day
                    self.con.execute("update energy_daily set start_wh=?, last_wh=? where day=?", (s, l, d))
                r = self.con.execute("select start_wh, last_wh from energy_daily where day=?", (today,)).fetchone()
                if r and finite(r[0]) is not None and finite(r[1]) is not None:
                    row = [float(r[0]), float(r[1])]
                else:
                    y = self.con.execute("select last_wh from energy_daily where day=?",
                                         ((date.today() - timedelta(days=1)).isoformat(),)).fetchone()
                    yl = finite(y[0]) if y else None
                    start = yl if yl is not None and yl <= total_wh else total_wh
                    row = [start, total_wh]
                    if r:   # repair a corrupt row for today instead of failing forever
                        log.warning("energy_daily %s: ungültige Werte ersetzt", today)
                    self.con.execute("insert or replace into energy_daily values (?,?,?)", (today, start, total_wh))
                    self.con.commit()
                self._cache = {today: row}
            if total_wh < row[1] - 1:          # meter reset: keep today's consumption
                row[0] = total_wh - (row[1] - row[0])
            row[1] = total_wh
            if now - self._last_write > 60:
                self.con.execute("update energy_daily set start_wh=?, last_wh=? where day=?", (row[0], row[1], today))
                self.con.commit()
                self._last_write = now
        return self.summary()

    def summary(self) -> dict:
        today = date.today()
        with self.lock:
            rows = self.con.execute("select day, start_wh, last_wh from energy_daily order by day").fetchall()
            cache = dict(self._cache)
        per_day = {}
        for d, s, l in rows:
            if d in cache:
                s, l = cache[d]
            s, l = finite(s), finite(l)
            if s is None or l is None:
                continue          # skip corrupt rows instead of breaking every summary
            per_day[d] = max(0.0, l - s) / 1000
        if not per_day:
            return {}
        since = min(per_day)

        def period(start: date) -> float:
            return round(sum(v for d, v in per_day.items() if d >= start.isoformat()), 3)

        week_start = today - timedelta(days=today.weekday())
        month_start = today.replace(day=1)
        year_start = today.replace(month=1, day=1)
        days = [(today - timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
        return {
            "since": since,
            "today_kwh": period(today),
            "yesterday_kwh": round(per_day.get((today - timedelta(days=1)).isoformat(), 0.0), 3),
            "week_kwh": period(week_start),
            "month_kwh": period(month_start),
            "year_kwh": period(year_start),
            "tracked_kwh": round(sum(per_day.values()), 3),
            "week_partial": since > week_start.isoformat(),
            "month_partial": since > month_start.isoformat(),
            "year_partial": since > year_start.isoformat(),
            "daily": [{"day": d, "kwh": round(per_day[d], 3) if d in per_day else None} for d in days],
        }


class History:
    def __init__(self, path: str, interval: int, days: int) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.interval = interval
        self.days = days
        self.lock = threading.Lock()
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.execute("pragma journal_mode=wal")
        self.con.execute("create table if not exists samples (ts integer primary key, data text not null)")
        self.con.commit()
        self._acc: dict[str, list[float]] = {}
        self._acc_start = time.time()
        self._last_prune = 0.0

    def add(self, flat: dict) -> None:
        for k in HISTORY_KEYS:
            v = finite(flat.get(k))
            if v is not None:
                self._acc.setdefault(k, []).append(v)
        now = time.time()
        if now - self._acc_start < self.interval:
            return
        row = {k: round(sum(v) / len(v), 2) for k, v in self._acc.items() if v}
        self._acc, self._acc_start = {}, now
        with self.lock:
            self.con.execute("insert or replace into samples(ts, data) values (?, ?)",
                             (int(now), json.dumps(row, allow_nan=False)))
            if now - self._last_prune > 3600:
                self.con.execute("delete from samples where ts < ?", (int(now - self.days * 86400),))
                self._last_prune = now
            self.con.commit()

    def series(self, key: str, since: float) -> list[tuple[int, float]]:
        with self.lock:
            rows = self.con.execute("select ts, json_extract(data, ?) from samples where ts >= ? order by ts",
                                    (f"$.{key}", int(since))).fetchall()
        return [(t, f) for t, v in rows if (f := finite(v)) is not None]

    def query(self, seconds: int, keys: list[str], points: int = 360) -> dict:
        # allowlisted + de-duplicated: at most len(HISTORY_KEYS) aggregate columns per query
        keys = list(dict.fromkeys(k for k in keys if k in HISTORY_KEYS)) or HISTORY_KEYS
        bucket = max(self.interval, seconds // points)
        cols = ", ".join(f"avg(json_extract(data, '$.{k}'))" for k in keys)
        since = int(time.time() - seconds)
        with self.lock:
            rows = self.con.execute(
                f"select (ts / ?) * ? as t, {cols} from samples where ts >= ? group by t order by t",
                (bucket, bucket, since),
            ).fetchall()
        return {
            "bucket": bucket,
            "ts": [r[0] for r in rows],
            "series": {k: [None if r[i + 1] is None else round(r[i + 1], 2) for r in rows] for i, k in enumerate(keys)},
        }
