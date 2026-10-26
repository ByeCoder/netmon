"""
Collector thread.

  every 1 s   /proc/net/dev  -> live speed, 1-second peaks
  every 5 s   iptables counters -> per-protocol / per-port bytes, packets, requests
  every 10 s  connection table  -> countries, port candidates, live top IPs
  every 30 s  one SQLite transaction (fine + hourly)
  every 60 s  re-pick which ports get their own counters
  every 10 m  prune old rows
"""
import os
import threading
import time
from collections import Counter, deque

from . import accounting, config, conns, db, geoip
from .accounting import PROTOS

PCODES = [c for c, _ in PROTOS]


def read_dev():
    out = {}
    try:
        with open("/proc/net/dev") as f:
            for ln in f.readlines()[2:]:
                name, _, data = ln.partition(":")
                v = data.split()
                if len(v) >= 9:
                    out[name.strip()] = (int(v[0]), int(v[8]))
    except OSError:
        pass
    return out


def default_ifaces():
    found = []
    try:
        with open("/proc/net/route") as f:
            for ln in f.readlines()[1:]:
                p = ln.split()
                if len(p) > 2 and p[1] == "00000000" and p[0] not in found:
                    found.append(p[0])
    except OSError:
        pass
    if not found:
        try:
            with open("/proc/net/ipv6_route") as f:
                for ln in f:
                    p = ln.split()
                    if len(p) >= 10 and p[0] == "0" * 32 and p[1] == "00" and p[9] != "lo":
                        if p[9] not in found:
                            found.append(p[9])
        except OSError:
            pass
    if not found:
        found = [k for k in read_dev() if k != "lo"][:1] or ["eth0"]
    return found


def _cpu_times():
    try:
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        return sum(v), idle
    except (OSError, ValueError, IndexError):
        return 0, 0


class Collector(threading.Thread):
    def __init__(self, cfg=None):
        super().__init__(daemon=True, name="collector")
        self.cfg = cfg or config.load()
        self.db_path = self.cfg["db_path"]
        db.init(self.db_path)
        n = geoip.load(self.cfg.get("geoip_path"))
        self.ifaces = self.cfg.get("interfaces") or default_ifaces()
        self.poll = max(2, int(self.cfg.get("poll_interval", 5)))
        self.bucket = max(self.poll, int(self.cfg.get("bucket_seconds", 60)))
        self.conn_every = max(5, int(self.cfg.get("conn_sample_interval", 10)))
        self.flush_every = max(self.poll, int(self.cfg.get("flush_interval", 30)))

        self.acct = accounting.Accounting(self.cfg, self.ifaces)
        self.sampler = conns.Sampler()
        self.lock = threading.Lock()
        self.started = time.time()

        # live state
        self.live = deque(maxlen=600)       # (ts, rx_bps, tx_bps)
        self.proto_rate = {}                # code -> (rx_bps, tx_bps)
        self.new_rate = (0.0, 0.0)          # new conns/s in, out
        self.cpu = 0.0
        self._cpu_prev = _cpu_times()

        # buffers waiting for the next flush
        self.b_traffic = []
        self.b_proto = {}
        self.b_port = {}
        self.b_country = Counter()
        self.peak = {"rx": (0, 0), "tx": (0, 0)}
        self.tot = [0, 0]

        # port selection state
        self.cand_in = Counter()
        self.cand_out = Counter()
        self.pending = {}
        self.last_error = ""

        rec = db.records(self.db_path)
        self.alltime_peak = {"rx": rec.get("peak_rx", (0, 0)), "tx": rec.get("peak_tx", (0, 0))}
        print(f"[collector] ifaces={self.ifaces} poll={self.poll}s "
              f"iptables={'on' if self.acct.fams else 'off'} "
              f"conn-source={self.sampler.source} geoip={n} ranges", flush=True)

    # ------------------------------------------------------------ helpers
    def _merge_delta(self, delta):
        for k, (p, b) in delta.items():
            op, ob = self.pending.get(k, (0, 0))
            self.pending[k] = (op + p, ob + b)

    def _pick_ports(self, initial=False):
        cfg = config.load()
        listen = self.sampler.listen
        tunnel = {(c, int(p)) for p in cfg.get("tunnel_ports", []) for c in (6, 17)}
        score = Counter()
        for k in listen:
            score[k] += 1000
        for k in tunnel:
            score[k] += 10 ** 7
        for k, v in self.cand_in.items():
            if v >= 2:
                score[k] += v
        for k in self.acct.server:
            score[k] += 5
        server = {k for k, _ in score.most_common(int(cfg.get("max_server_ports", 64)))}

        cs = Counter({(6, 443): 50, (6, 80): 20, (17, 53): 20, (17, 443): 10})
        for k, v in self.cand_out.items():
            if v >= 2:
                cs[k] += v
        for k in self.acct.client:
            cs[k] += 5
        client = {k for k, _ in cs.most_common(int(cfg.get("max_client_ports", 32)))}

        for c in (self.cand_in, self.cand_out):
            for k in list(c):
                c[k] //= 2
                if not c[k]:
                    del c[k]

        if initial:
            self.acct.setup(server, client)
            return
        if (server - self.acct.server) or (client - self.acct.client) or \
                len(self.acct.server) > len(server) or len(self.acct.client) > len(client):
            self._merge_delta(self.acct.rebuild(server, client))

    # ------------------------------------------------------------ steps
    def _interval(self, ts, acc_rx, acc_tx, pk_rx, pk_tx, new_fallback):
        self._merge_delta(self.acct.read())
        d, self.pending = self.pending, {}
        poll = self.poll
        bkt = ts - ts % self.bucket

        def g(tag, idx=1):
            v = d.get(tag)
            return v[idx] if v else 0

        prates = {}
        if self.acct.enabled:
            s = [0, 0, 0, 0, 0, 0, 0, 0]
            for code in PCODES:
                row = [g(f"nm:pi:{code}"), g(f"nm:po:{code}"),
                       g(f"nm:pi:{code}", 0), g(f"nm:po:{code}", 0),
                       0, 0, g(f"nm:ni:{code}", 0), g(f"nm:no:{code}", 0)]
                for i in range(8):
                    s[i] += row[i]
                self._add_proto(bkt, code, row)
            other = [max(0, g("nm:ai") - s[0]), max(0, g("nm:ao") - s[1]),
                     max(0, g("nm:ai", 0) - s[2]), max(0, g("nm:ao", 0) - s[3]), 0, 0,
                     max(0, g("nm:nai", 0) - s[6]), max(0, g("nm:nao", 0) - s[7])]
            self._add_proto(bkt, 0, other)
            new_in, new_out = g("nm:nai", 0), g("nm:nao", 0)

            for tag, (p, b) in d.items():
                parts = tag.split(":")
                if parts[1] == "b" and len(parts) == 6:
                    _, _, dr, role, code, port = parts
                    k = (bkt, int(port), 0 if role == "s" else 1, int(code))
                    r = self.b_port.setdefault(k, [0, 0, 0, 0, 0])
                    if dr == "i":
                        r[0] += b
                        r[2] = max(r[2], b // poll)
                    else:
                        r[1] += b
                        r[3] = max(r[3], b // poll)
                elif parts[1] == "q" and len(parts) == 5:
                    _, _, role, code, port = parts
                    k = (bkt, int(port), 0 if role == "s" else 1, int(code))
                    self.b_port.setdefault(k, [0, 0, 0, 0, 0])[4] += p
            prates = {code: (g(f"nm:pi:{code}") / poll, g(f"nm:po:{code}") / poll)
                      for code in PCODES}
            prates[0] = (other[0] / poll, other[1] / poll)
        else:
            new_in, new_out = new_fallback

        with self.lock:
            self.proto_rate = prates
            self.new_rate = (new_in / poll, new_out / poll)
        self.b_traffic.append((ts, acc_rx, acc_tx, pk_rx, pk_tx,
                               self.sampler.active, new_in, new_out))
        self.tot[0] += acc_rx
        self.tot[1] += acc_tx

    def _add_proto(self, bkt, code, row):
        r = self.b_proto.get((bkt, code))
        poll = self.poll
        if r is None:
            r = self.b_proto[(bkt, code)] = [0] * 8
        for i in (0, 1, 2, 3, 6, 7):
            r[i] += row[i]
        r[4] = max(r[4], row[0] // poll)
        r[5] = max(r[5], row[1] // poll)

    def _sample(self, now):
        res = self.sampler.sample(geoip.lookup)
        bkt = int(now) - int(now) % self.bucket
        for (cc, dr), n in res["countries"].items():
            self.b_country[(bkt, cc, dr)] += n
        self.cand_in.update(res["in_ports"])
        self.cand_out.update(res["out_ports"])
        if not self.acct.enabled:
            for (code, port), n in res["in_ports"].items():
                self.b_port.setdefault((bkt, port, 0, code), [0, 0, 0, 0, 0])[4] += n
            for (code, port), n in res["out_ports"].items():
                self.b_port.setdefault((bkt, port, 1, code), [0, 0, 0, 0, 0])[4] += n
            for (code, dr), n in res["protos"].items():
                code = code if code in PCODES else 0
                r = self.b_proto.setdefault((bkt, code), [0] * 8)
                r[6 + dr] += n
        return sum(n for (c, d), n in res["protos"].items() if d == 0), \
            sum(n for (c, d), n in res["protos"].items() if d == 1)

    def _flush(self):
        if not (self.b_traffic or self.b_proto or self.b_port or self.b_country):
            return
        recs = {}
        for k in ("rx", "tx"):
            v, t = self.peak[k]
            if v:
                recs[f"peak_{k}"] = (v, t, "max")
        if self.tot[0] or self.tot[1]:
            recs["total_rx"] = (self.tot[0], int(time.time()), "add")
            recs["total_tx"] = (self.tot[1], int(time.time()), "add")
        try:
            db.flush(self.db_path, self.b_traffic,
                     [k + tuple(v) for k, v in self.b_proto.items()],
                     [k + tuple(v) for k, v in self.b_port.items()],
                     [k + (v,) for k, v in self.b_country.items()],
                     recs)
            self.b_traffic, self.b_proto, self.b_port = [], {}, {}
            self.b_country = Counter()
            self.peak = {"rx": (0, 0), "tx": (0, 0)}
            self.tot = [0, 0]
            self.last_error = ""
        except Exception as e:  # noqa: BLE001
            self.last_error = f"db write: {e}"
            print(f"[collector] {self.last_error}", flush=True)
            if len(self.b_traffic) > 2000:      # never grow unbounded
                self.b_traffic = self.b_traffic[-1000:]

    # ------------------------------------------------------------ main loop
    def run(self):
        try:
            self.sampler.sample(geoip.lookup)    # baseline, nothing counted
        except Exception as e:  # noqa: BLE001
            print(f"[collector] sampler: {e}", flush=True)
        try:
            self._pick_ports(initial=True)
        except Exception as e:  # noqa: BLE001
            print(f"[collector] accounting setup: {e}", flush=True)
        prev = read_dev()
        t = time.time()
        next_poll = t - t % self.poll + self.poll
        next_conn = t + self.conn_every
        next_flush = t + self.flush_every
        next_pick = t + 30
        next_prune = t + 60
        acc_rx = acc_tx = pk_rx = pk_tx = 0
        fb_new = [0, 0]
        conn_every = self.conn_every
        while True:
            time.sleep(max(0.05, 1 - (time.time() % 1)))
            now = time.time()
            cur = read_dev()
            rx = tx = 0
            for i in self.ifaces:
                if i in cur and i in prev:
                    rx += max(0, cur[i][0] - prev[i][0])
                    tx += max(0, cur[i][1] - prev[i][1])
            prev = cur
            acc_rx += rx
            acc_tx += tx
            pk_rx, pk_tx = max(pk_rx, rx), max(pk_tx, tx)
            its = int(now)
            with self.lock:
                self.live.append((its, rx, tx))
            if rx > self.peak["rx"][0]:
                self.peak["rx"] = (rx, its)
            if tx > self.peak["tx"][0]:
                self.peak["tx"] = (tx, its)
            for k, v in (("rx", rx), ("tx", tx)):
                if v > self.alltime_peak[k][0]:
                    self.alltime_peak[k] = (v, its)
            try:
                if now >= next_conn:
                    a, b = self._sample(now)
                    fb_new[0] += a
                    fb_new[1] += b
                    # back off on huge conntrack tables
                    conn_every = self.conn_every * (3 if self.sampler.last_cost > 1.5 else 1)
                    next_conn = now + conn_every
                if now >= next_poll:
                    ts = int(next_poll) - self.poll
                    self._interval(ts, acc_rx, acc_tx, pk_rx, pk_tx, tuple(fb_new))
                    acc_rx = acc_tx = pk_rx = pk_tx = 0
                    fb_new = [0, 0]
                    next_poll += self.poll
                    if next_poll < now:
                        next_poll = now - now % self.poll + self.poll
                    t0, i0 = self._cpu_prev
                    t1, i1 = _cpu_times()
                    if t1 > t0:
                        self.cpu = 100.0 * (1 - (i1 - i0) / (t1 - t0))
                    self._cpu_prev = (t1, i1)
                if now >= next_pick:
                    self._pick_ports()
                    next_pick = now + 60
                if now >= next_flush:
                    self._flush()
                    next_flush = now + self.flush_every
                if now >= next_prune:
                    db.prune(self.db_path, int(self.cfg.get("retention_fine_hours", 24)),
                             int(self.cfg.get("retention_hourly_days", 30)))
                    next_prune = now + 600
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                print(f"[collector] {e}", flush=True)

    # ------------------------------------------------------------ for the API
    def snapshot(self, seconds=120):
        with self.lock:
            live = list(self.live)[-seconds:]
            prates = dict(self.proto_rate)
            new_rate = self.new_rate
        last = live[-1] if live else (int(time.time()), 0, 0)
        return {
            "ts": last[0], "rx": last[1], "tx": last[2],
            "series": live,
            "proto_rate": {str(k): v for k, v in prates.items()},
            "new_in": round(new_rate[0], 2), "new_out": round(new_rate[1], 2),
            "active": self.sampler.active,
            "peak_rx": self.alltime_peak["rx"], "peak_tx": self.alltime_peak["tx"],
            "cpu": round(self.cpu, 1),
        }

    def info(self):
        return {
            "ifaces": self.ifaces, "poll": self.poll, "bucket": self.bucket,
            "accounting": self.acct.enabled, "accounting_error": self.acct.error,
            "ipv6": "6" in self.acct.fams,
            "conn_source": self.sampler.source, "geoip": geoip.ready(),
            "watched_server": len(self.acct.server), "watched_client": len(self.acct.client),
            "uptime": int(time.time() - self.started), "error": self.last_error,
            "pid": os.getpid(),
        }
