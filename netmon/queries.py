"""Read-side aggregation for the API. Every function returns plain JSON-able data."""
import time

from . import catalog, db, geoip
from .accounting import PROTO_NAMES

WINDOWS = {"15m": 900, "1h": 3600, "6h": 6 * 3600, "12h": 12 * 3600, "24h": 86400,
           "7d": 7 * 86400, "30d": 30 * 86400}
AUTO_STEP = {"15m": 5, "1h": 5, "6h": 15, "12h": 30, "24h": 60, "today": 60,
             "yesterday": 300, "7d": 3600, "30d": 3600}


class Q:
    def __init__(self, db_path, poll=5, bucket=60, fine_hours=24):
        self.db_path = db_path
        self.poll = poll
        self.bucket = bucket
        self.fine_hours = fine_hours

    @property
    def c(self):
        return db.conn(self.db_path)

    # ------------------------------------------------------------ windows
    def bounds(self, window, tz=0):
        now = int(time.time())
        off = int(tz) * 60
        if window in ("today", "yesterday"):
            start = ((now + off) // 86400) * 86400 - off
            if window == "yesterday":
                return start - 86400, start
            return start, now
        return now - WINDOWS.get(window, 86400), now

    def fine(self, start):
        return start >= int(time.time()) - self.fine_hours * 3600 - 60

    def _step(self, window, step, base):
        try:
            step = int(step)
        except (TypeError, ValueError):
            step = AUTO_STEP.get(window, 60)
        step = max(base, step)
        return (step // base) * base

    @staticmethod
    def _grp(step, tz):
        off = int(tz) * 60 if step >= 86400 else 0
        return f"(((ts + {off}) / {step}) * {step} - {off})", off

    @staticmethod
    def _axis(start, end, step, off):
        first = ((start + off) // step) * step - off
        return list(range(first, end + 1, step))

    # ------------------------------------------------------------ summary
    def _sum(self, tbl, start, end=None):
        end = end or int(time.time()) + 1
        r = self.c.execute(
            f"SELECT COALESCE(SUM(rx),0) rx, COALESCE(SUM(tx),0) tx, "
            f"COALESCE(SUM(new_in),0) ni, COALESCE(SUM(new_out),0) no "
            f"FROM {tbl} WHERE ts >= ? AND ts < ?", (start, end)).fetchone()
        return dict(r)

    def _peak(self, tbl, col, start, end):
        r = self.c.execute(f"SELECT ts, {col} v FROM {tbl} WHERE ts >= ? AND ts < ? "
                           f"ORDER BY {col} DESC LIMIT 1", (start, end + 1)).fetchone()
        return [r["v"], r["ts"]] if r and r["v"] else [0, 0]

    def summary(self, tz=0):
        out = {}
        for w in ("today", "yesterday", "24h", "7d", "30d"):
            s, e = self.bounds(w, tz)
            tbl = "traffic_fine" if self.fine(s) else "traffic_hourly"
            out[w] = self._sum(tbl, s, e)
            out[w]["peak_rx"] = self._peak(tbl, "rx_peak", s, e)
            out[w]["peak_tx"] = self._peak(tbl, "tx_peak", s, e)
        rec = db.records(self.db_path)
        out["alltime"] = {"rx": rec.get("total_rx", (0, 0))[0],
                          "tx": rec.get("total_tx", (0, 0))[0],
                          "since": min(rec.get("total_rx", (0, 0))[1] or 0,
                                       self._first_ts() or 0) or None,
                          "peak_rx": list(rec.get("peak_rx", (0, 0))),
                          "peak_tx": list(rec.get("peak_tx", (0, 0)))}
        return out

    def _first_ts(self):
        r = self.c.execute("SELECT MIN(ts) m FROM traffic_hourly").fetchone()
        return r["m"] if r else None

    # ------------------------------------------------------------ traffic
    def traffic(self, window="24h", step=None, tz=0):
        start, end = self.bounds(window, tz)
        fine = self.fine(start)
        tbl, base = ("traffic_fine", self.poll) if fine else ("traffic_hourly", 3600)
        step = self._step(window, step, base)
        g, off = self._grp(step, tz)
        rows = self.c.execute(
            f"SELECT {g} b, SUM(rx) rx, SUM(tx) tx, MAX(rx_peak) rp, MAX(tx_peak) tp, "
            f"MAX(conns) cn, SUM(new_in) ni, SUM(new_out) no FROM {tbl} "
            f"WHERE ts >= ? AND ts < ? GROUP BY b ORDER BY b", (start, end + 1)).fetchall()
        idx = {r["b"]: r for r in rows}
        axis = self._axis(start, end, step, off)
        cols = {k: [] for k in ("rx", "tx", "rp", "tp", "cn", "ni", "no")}
        for t in axis:
            r = idx.get(t)
            for k in cols:
                cols[k].append(r[k] if r else None)
        # trim a leading run of empty buckets so a fresh install isn't a flat line
        first = next((i for i, v in enumerate(cols["rx"]) if v is not None), len(axis))
        first = max(0, first - 1) if window not in ("today", "yesterday") else 0
        return {"step": step, "fine": fine, "ts": axis[first:],
                "rx": cols["rx"][first:], "tx": cols["tx"][first:],
                "rx_peak": cols["rp"][first:], "tx_peak": cols["tp"][first:],
                "conns": cols["cn"][first:], "new_in": cols["ni"][first:],
                "new_out": cols["no"][first:]}

    def daily(self, days=30, tz=0):
        now = int(time.time())
        off = int(tz) * 60
        start = ((now + off) // 86400) * 86400 - off - (days - 1) * 86400
        g = f"(((ts + {off}) / 86400) * 86400 - {off})"
        rows = self.c.execute(
            f"SELECT {g} b, SUM(rx) rx, SUM(tx) tx, MAX(rx_peak) rp, MAX(tx_peak) tp, "
            f"SUM(new_in) ni FROM traffic_hourly WHERE ts >= ? GROUP BY b ORDER BY b",
            (start,)).fetchall()
        idx = {r["b"]: r for r in rows}
        axis = list(range(start, now + 1, 86400))
        return {"ts": axis,
                "rx": [idx[t]["rx"] if t in idx else 0 for t in axis],
                "tx": [idx[t]["tx"] if t in idx else 0 for t in axis],
                "rx_peak": [idx[t]["rp"] if t in idx else 0 for t in axis],
                "tx_peak": [idx[t]["tp"] if t in idx else 0 for t in axis],
                "req": [idx[t]["ni"] if t in idx else 0 for t in axis]}

    # ------------------------------------------------------------ protocols
    def protocols(self, window="24h", tz=0):
        start, end = self.bounds(window, tz)
        tbl = "proto_fine" if self.fine(start) else "proto_hourly"
        rows = self.c.execute(
            f"SELECT p, SUM(rx) rx, SUM(tx) tx, SUM(rx_pk) rx_pk, SUM(tx_pk) tx_pk, "
            f"MAX(rx_peak) rx_peak, MAX(tx_peak) tx_peak, SUM(req_in) req_in, "
            f"SUM(req_out) req_out FROM {tbl} WHERE ts >= ? AND ts < ? GROUP BY p",
            (start, end + 1)).fetchall()
        got = {r["p"]: dict(r) for r in rows}
        out = []
        for code, name in PROTO_NAMES.items():
            r = got.get(code) or {"p": code, "rx": 0, "tx": 0, "rx_pk": 0, "tx_pk": 0,
                                  "rx_peak": 0, "tx_peak": 0, "req_in": 0, "req_out": 0}
            r["name"] = name
            out.append(r)
        return out

    def proto_series(self, window="24h", step=None, tz=0):
        start, end = self.bounds(window, tz)
        fine = self.fine(start)
        tbl, base = ("proto_fine", self.bucket) if fine else ("proto_hourly", 3600)
        step = self._step(window, step if step else (self.bucket if fine else 3600), base)
        g, off = self._grp(step, tz)
        rows = self.c.execute(
            f"SELECT {g} b, p, SUM(rx) rx, SUM(tx) tx, SUM(req_in)+SUM(req_out) req "
            f"FROM {tbl} WHERE ts >= ? AND ts < ? GROUP BY b, p", (start, end + 1)).fetchall()
        axis = self._axis(start, end - step, step, off)  # skip the unfinished bucket
        pos = {t: i for i, t in enumerate(axis)}
        series = {}
        for r in rows:
            i = pos.get(r["b"])
            if i is None:
                continue
            s = series.setdefault(r["p"], {"rx": [0] * len(axis), "tx": [0] * len(axis),
                                           "req": [0] * len(axis)})
            s["rx"][i], s["tx"][i], s["req"][i] = r["rx"], r["tx"], r["req"]
        return {"step": step, "ts": axis,
                "series": {PROTO_NAMES.get(p, str(p)): v for p, v in series.items()}}

    # ------------------------------------------------------------ ports
    def ports(self, window="24h", role=None, sort="total", limit=50, tz=0, procs=None):
        start, end = self.bounds(window, tz)
        tbl = "port_fine" if self.fine(start) else "port_hourly"
        where, args = "ts >= ? AND ts < ?", [start, end + 1]
        if role in (0, 1):
            where += " AND role = ?"
            args.append(role)
        order = {"total": "rx+tx", "rx": "rx", "tx": "tx", "req": "req",
                 "peak": "MAX(rx_peak, tx_peak)"}.get(sort, "rx+tx")
        rows = self.c.execute(
            f"SELECT port, role, p, SUM(rx) rx, SUM(tx) tx, SUM(req) req, "
            f"MAX(rx_peak) rx_peak, MAX(tx_peak) tx_peak FROM {tbl} WHERE {where} "
            f"GROUP BY port, role, p ORDER BY {order} DESC, req DESC LIMIT ?",
            (*args, int(limit))).fetchall()
        procs = procs or {}
        out = []
        for r in rows:
            d = dict(r)
            d["proto"] = "TCP" if r["p"] == 6 else "UDP" if r["p"] == 17 else str(r["p"])
            d["service"] = catalog.service(r["port"], r["p"])
            d["process"] = procs.get((r["p"], r["port"]), "") if r["role"] == 0 else ""
            out.append(d)
        return out

    def port_series(self, port, role, p, window="24h", step=None, tz=0):
        start, end = self.bounds(window, tz)
        fine = self.fine(start)
        tbl, base = ("port_fine", self.bucket) if fine else ("port_hourly", 3600)
        step = self._step(window, step if step else base, base)
        g, off = self._grp(step, tz)
        rows = self.c.execute(
            f"SELECT {g} b, SUM(rx) rx, SUM(tx) tx, SUM(req) req, MAX(rx_peak) rp, "
            f"MAX(tx_peak) tp FROM {tbl} WHERE port=? AND role=? AND p=? AND ts >= ? "
            f"AND ts < ? GROUP BY b", (int(port), int(role), int(p), start, end + 1)).fetchall()
        idx = {r["b"]: r for r in rows}
        axis = self._axis(start, end - step, step, off)
        res = {"step": step, "ts": axis}
        for k, col in (("rx", "rx"), ("tx", "tx"), ("req", "req"),
                       ("rx_peak", "rp"), ("tx_peak", "tp")):
            res[k] = [idx[t][col] if t in idx else 0 for t in axis]
        return res

    # ------------------------------------------------------------ countries
    def countries(self, window="24h", direction=0, limit=250, tz=0):
        start, end = self.bounds(window, tz)
        tbl = "country_fine" if self.fine(start) else "country_hourly"
        rows = self.c.execute(
            f"SELECT cc, SUM(req) req FROM {tbl} WHERE ts >= ? AND ts < ? AND dir = ? "
            f"GROUP BY cc ORDER BY req DESC LIMIT ?",
            (start, end + 1, int(direction), int(limit))).fetchall()
        total = self.c.execute(f"SELECT COALESCE(SUM(req),0) FROM {tbl} WHERE ts >= ? AND ts < ? AND dir = ?",
                               (start, end + 1, int(direction))).fetchone()[0] or 1
        return [{"cc": r["cc"], "name": geoip.name(r["cc"]), "flag": geoip.flag(r["cc"]),
                 "req": r["req"], "share": r["req"] / total} for r in rows]

    def country_series(self, ccs, window="24h", direction=0, step=None, tz=0):
        start, end = self.bounds(window, tz)
        fine = self.fine(start)
        tbl, base = ("country_fine", self.bucket) if fine else ("country_hourly", 3600)
        step = self._step(window, step if step else (base if fine and window != "24h" else
                                                     (300 if fine else 3600)), base)
        g, off = self._grp(step, tz)
        ccs = [c for c in ccs if isinstance(c, str) and len(c) == 2][:8]
        if not ccs:
            return {"step": step, "ts": [], "series": {}}
        q = ",".join("?" * len(ccs))
        rows = self.c.execute(
            f"SELECT {g} b, cc, SUM(req) req FROM {tbl} WHERE ts >= ? AND ts < ? AND dir=? "
            f"AND cc IN ({q}) GROUP BY b, cc", (start, end + 1, int(direction), *ccs)).fetchall()
        axis = self._axis(start, end - step, step, off)  # skip the unfinished bucket
        pos = {t: i for i, t in enumerate(axis)}
        series = {c: [0] * len(axis) for c in ccs}
        for r in rows:
            i = pos.get(r["b"])
            if i is not None:
                series[r["cc"]][i] = r["req"]
        return {"step": step, "ts": axis, "series": series}

    # ------------------------------------------------------------ highlights
    def highlights(self, window="24h", tz=0, procs=None):
        by_bytes = self.ports(window, None, "total", 1, tz, procs)
        by_req = self.ports(window, None, "req", 1, tz, procs)
        by_peak = self.ports(window, None, "peak", 1, tz, procs)
        cc = self.countries(window, 0, 1, tz)
        pr = sorted(self.protocols(window, tz), key=lambda r: -(r["req_in"] + r["req_out"]))
        return {"port_bytes": by_bytes[0] if by_bytes else None,
                "port_req": by_req[0] if by_req else None,
                "port_peak": by_peak[0] if by_peak else None,
                "country": cc[0] if cc else None,
                "proto": pr[0] if pr and (pr[0]["req_in"] + pr[0]["req_out"]) else None}

    def fw_log(self, limit=60):
        return [dict(r) for r in self.c.execute(
            "SELECT ts, action, detail FROM fw_log ORDER BY ts DESC LIMIT ?", (limit,))]
