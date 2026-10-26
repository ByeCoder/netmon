"""
SQLite storage — two resolutions, integer-only, WITHOUT ROWID tables.

    *_fine    full detail, kept `retention_fine_hours` (24h)
              traffic: one row per poll (5s); ports/countries/protocols: per minute
    *_hourly  hour buckets, kept `retention_hourly_days` (30d)

Both are written on every flush (hourly via upsert-add), so long-range
queries always include the most recent data. Typical size: a few MB.
"""
import os
import sqlite3
import threading
import time

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS traffic_fine(
  ts INTEGER PRIMARY KEY, rx INTEGER, tx INTEGER, rx_peak INTEGER, tx_peak INTEGER,
  conns INTEGER, new_in INTEGER, new_out INTEGER) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS traffic_hourly(
  ts INTEGER PRIMARY KEY, rx INTEGER, tx INTEGER, rx_peak INTEGER, tx_peak INTEGER,
  conns INTEGER, new_in INTEGER, new_out INTEGER) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS proto_fine(
  ts INTEGER, p INTEGER, rx INTEGER, tx INTEGER, rx_pk INTEGER, tx_pk INTEGER,
  rx_peak INTEGER, tx_peak INTEGER, req_in INTEGER, req_out INTEGER,
  PRIMARY KEY(ts, p)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS proto_hourly(
  ts INTEGER, p INTEGER, rx INTEGER, tx INTEGER, rx_pk INTEGER, tx_pk INTEGER,
  rx_peak INTEGER, tx_peak INTEGER, req_in INTEGER, req_out INTEGER,
  PRIMARY KEY(ts, p)) WITHOUT ROWID;

-- role 0 = server port on this machine (inbound), 1 = remote port (outbound)
CREATE TABLE IF NOT EXISTS port_fine(
  ts INTEGER, port INTEGER, role INTEGER, p INTEGER, rx INTEGER, tx INTEGER,
  rx_peak INTEGER, tx_peak INTEGER, req INTEGER,
  PRIMARY KEY(ts, port, role, p)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS port_hourly(
  ts INTEGER, port INTEGER, role INTEGER, p INTEGER, rx INTEGER, tx INTEGER,
  rx_peak INTEGER, tx_peak INTEGER, req INTEGER,
  PRIMARY KEY(ts, port, role, p)) WITHOUT ROWID;

-- dir 0 = inbound (source country), 1 = outbound (destination country)
CREATE TABLE IF NOT EXISTS country_fine(
  ts INTEGER, cc TEXT, dir INTEGER, req INTEGER,
  PRIMARY KEY(ts, cc, dir)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS country_hourly(
  ts INTEGER, cc TEXT, dir INTEGER, req INTEGER,
  PRIMARY KEY(ts, cc, dir)) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS records(k TEXT PRIMARY KEY, v INTEGER, ts INTEGER) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fw_log(ts INTEGER, action TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS fw_log_ts ON fw_log(ts);
"""


def conn(db_path):
    c = getattr(_local, "c", None)
    if c is None or getattr(_local, "path", None) != db_path:
        d = os.path.dirname(db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        c = sqlite3.connect(db_path, timeout=30, isolation_level=None,
                            check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA auto_vacuum=INCREMENTAL")   # only takes effect on a new file
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA temp_store=MEMORY")
        c.execute("PRAGMA cache_size=-2000")
        c.execute("PRAGMA journal_size_limit=4194304")
        _local.c, _local.path = c, db_path
    return c


def init(db_path):
    c = conn(db_path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(traffic_fine)")}
    if cols and "rx" not in cols:
        # database from NetMon 1.x: different layout, keep it aside and start fresh
        c.close()
        _local.c = None
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(db_path + suf):
                os.replace(db_path + suf, db_path + ".v1" + suf)
        print(f"[db] old 1.x database moved to {db_path}.v1", flush=True)
        c = conn(db_path)
    c.executescript(SCHEMA)
    return c


def _upsert(c, table, keys, cols, rows, peaks=(), replace=()):
    if not rows:
        return
    allc = keys + cols
    sets = []
    for x in cols:
        if x in peaks:
            sets.append(f"{x}=MAX({x}, excluded.{x})")
        elif x in replace:
            sets.append(f"{x}=MAX({x}, excluded.{x})")
        else:
            sets.append(f"{x}={x}+excluded.{x}")
    sql = (f"INSERT INTO {table}({','.join(allc)}) VALUES({','.join('?' * len(allc))}) "
           f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {', '.join(sets)}")
    c.executemany(sql, rows)


T_COLS = ["rx", "tx", "rx_peak", "tx_peak", "conns", "new_in", "new_out"]
P_COLS = ["rx", "tx", "rx_pk", "tx_pk", "rx_peak", "tx_peak", "req_in", "req_out"]
PO_COLS = ["rx", "tx", "rx_peak", "tx_peak", "req"]
PEAKS = ("rx_peak", "tx_peak")


def _hour(rows, keylen):
    out = {}
    for r in rows:
        k = ((r[0] // 3600) * 3600,) + tuple(r[1:keylen])
        out.setdefault(k, []).append(r[keylen:])
    return out


def flush(db_path, traffic, protos, ports, countries, records):
    """Write one batch. Each argument is a list of tuples in column order
    (keys first). `records` = {key: (value, ts, mode)} mode 'max' | 'add'."""
    c = conn(db_path)
    c.execute("BEGIN")
    try:
        _upsert(c, "traffic_fine", ["ts"], T_COLS, traffic, PEAKS, ("conns",))
        _upsert(c, "proto_fine", ["ts", "p"], P_COLS, protos, PEAKS)
        _upsert(c, "port_fine", ["ts", "port", "role", "p"], PO_COLS, ports, PEAKS)
        _upsert(c, "country_fine", ["ts", "cc", "dir"], ["req"], countries)

        for table, fine, keylen, cols in (
                ("traffic_hourly", traffic, 1, T_COLS),
                ("proto_hourly", protos, 2, P_COLS),
                ("port_hourly", ports, 4, PO_COLS),
                ("country_hourly", countries, 3, ["req"])):
            agg = []
            for k, vals in _hour(fine, keylen).items():
                merged = []
                for i, col in enumerate(cols):
                    col_vals = [v[i] for v in vals]
                    merged.append(max(col_vals) if col in PEAKS or col == "conns" else sum(col_vals))
                agg.append(k + tuple(merged))
            keys = {1: ["ts"], 2: ["ts", "p"], 3: ["ts", "cc", "dir"],
                    4: ["ts", "port", "role", "p"]}[keylen]
            _upsert(c, table, keys, cols, agg, PEAKS, ("conns",))

        for k, (v, ts, mode) in records.items():
            if mode == "add":
                c.execute("INSERT INTO records(k,v,ts) VALUES(?,?,?) ON CONFLICT(k) "
                          "DO UPDATE SET v=v+excluded.v, ts=excluded.ts", (k, v, ts))
            else:
                c.execute("INSERT INTO records(k,v,ts) VALUES(?,?,?) ON CONFLICT(k) "
                          "DO UPDATE SET v=excluded.v, ts=excluded.ts WHERE excluded.v>v",
                          (k, v, ts))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def prune(db_path, fine_hours, hourly_days):
    c = conn(db_path)
    now = int(time.time())
    fc, hc = now - fine_hours * 3600, now - hourly_days * 86400
    for t in ("traffic_fine", "proto_fine", "port_fine", "country_fine"):
        c.execute(f"DELETE FROM {t} WHERE ts < ?", (fc,))
    for t in ("traffic_hourly", "proto_hourly", "port_hourly", "country_hourly"):
        c.execute(f"DELETE FROM {t} WHERE ts < ?", (hc,))
    c.execute("DELETE FROM fw_log WHERE ts < ?", (now - 90 * 86400,))
    c.execute("PRAGMA incremental_vacuum(500)")
    c.execute("PRAGMA wal_checkpoint(PASSIVE)")


def records(db_path):
    c = conn(db_path)
    return {r["k"]: (r["v"], r["ts"]) for r in c.execute("SELECT k, v, ts FROM records")}


def fw_log(db_path, action, detail):
    try:
        conn(db_path).execute("INSERT INTO fw_log(ts,action,detail) VALUES(?,?,?)",
                              (int(time.time()), action, detail[:200]))
    except sqlite3.Error as e:
        print(f"[db] fw_log: {e}", flush=True)


def size(db_path):
    tot = 0
    for suf in ("", "-wal", "-shm"):
        try:
            tot += os.path.getsize(db_path + suf)
        except OSError:
            pass
    return tot
