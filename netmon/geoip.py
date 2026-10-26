"""
Offline IP -> country lookup from a compact binary file.

The installer downloads the public-domain (CC0) "geo-whois-asn-country"
CSVs from sapics/ip-location-db and squeezes them into `geoip.bin`:
adjacent ranges of the same country are merged and only range *starts* are
stored (uint32 for IPv4, upper 64 bits for IPv6) plus a 2-byte country code.
Result: ~1-2 MB on disk and in RAM, O(log n) lookups, no network at runtime.
"""
import bisect
import functools
import os
import socket
import struct
import urllib.request
from array import array

from .countries import NAMES

MAGIC = b"NMG1"
_v4s, _v4c, _v6s, _v6c = array("I"), b"", array("Q"), b""
_ready = False

_PKG = "@ip-location-db/geo-whois-asn-country/geo-whois-asn-country-ipv{v}-num.csv"
_RAW = "https://raw.githubusercontent.com/sapics/ip-location-db/main/geo-whois-asn-country/geo-whois-asn-country-ipv{v}-num.csv"
_MIRRORS = ["https://cdn.jsdelivr.net/npm/", "https://fastly.jsdelivr.net/npm/",
            "https://gcore.jsdelivr.net/npm/", "https://unpkg.com/"]
URLS = {f"v{v}": [m + _PKG.format(v=v) for m in _MIRRORS] + [_RAW.format(v=v)] for v in (4, 6)}


def load(path):
    global _v4s, _v4c, _v6s, _v6c, _ready
    if not path or not os.path.exists(path):
        return 0
    try:
        with open(path, "rb") as f:
            if f.read(4) != MAGIC:
                return 0
            n4, n6 = struct.unpack("<II", f.read(8))
            a4 = array("I"); a4.frombytes(f.read(4 * n4))
            c4 = f.read(2 * n4)
            a6 = array("Q"); a6.frombytes(f.read(8 * n6))
            c6 = f.read(2 * n6)
        _v4s, _v4c, _v6s, _v6c = a4, c4, a6, c6
        _ready = True
        lookup.cache_clear()
        return n4 + n6
    except Exception as e:  # noqa: BLE001
        print(f"[geoip] load failed: {e}", flush=True)
        return 0


def ready():
    return _ready


@functools.lru_cache(maxsize=8192)
def lookup(ip):
    """ISO-2 country code, or '??'."""
    if not _ready:
        return "??"
    try:
        if ":" in ip:
            if ip.startswith("::ffff:") and "." in ip:
                return lookup(ip[7:])
            n = int.from_bytes(socket.inet_pton(socket.AF_INET6, ip)[:8], "big")
            starts, codes = _v6s, _v6c
        else:
            n = struct.unpack("!I", socket.inet_aton(ip))[0]
            starts, codes = _v4s, _v4c
    except OSError:
        return "??"
    i = bisect.bisect_right(starts, n) - 1
    if i < 0:
        return "??"
    cc = codes[2 * i:2 * i + 2].decode("ascii", "replace")
    return "??" if cc == "--" else cc


def name(cc):
    return NAMES.get((cc or "").upper(), "Unknown" if cc in ("??", "", None) else cc)


def flag(cc):
    cc = (cc or "").upper()
    if len(cc) != 2 or not cc.isalpha():
        return "🏳"
    return chr(0x1F1E6 + ord(cc[0]) - 65) + chr(0x1F1E6 + ord(cc[1]) - 65)


# ---------------------------------------------------------------- building

def _compact(rows, shift):
    """rows: iterable of (start, end, cc) sorted by start -> (starts, codes)."""
    starts, codes = [], []
    prev_end = -1
    for s, e, cc in rows:
        s >>= shift
        e >>= shift
        if s <= prev_end:           # overlap after shifting (tiny v6 ranges)
            s = prev_end + 1
            if s > e:
                continue
        if s > prev_end + 1:
            if not codes or codes[-1] != "--":
                starts.append(prev_end + 1); codes.append("--")
        if codes and codes[-1] == cc:
            pass                    # merge into previous range
        else:
            starts.append(s); codes.append(cc)
        prev_end = e
    starts.append(prev_end + 1); codes.append("--")
    return starts, codes


def _read_csv(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            p = ln.strip().split(",")
            if len(p) < 3:
                continue
            try:
                rows.append((int(p[0]), int(p[1]), (p[2].strip().upper() + "--")[:2]))
            except ValueError:
                continue
    rows.sort()
    return rows


def build(v4_csv, v6_csv, out_path):
    s4, c4 = _compact(_read_csv(v4_csv), 0) if v4_csv and os.path.exists(v4_csv) else ([], [])
    s6, c6 = _compact(_read_csv(v6_csv), 64) if v6_csv and os.path.exists(v6_csv) else ([], [])
    s4 = [x for x in s4 if x <= 0xFFFFFFFF]
    c4 = c4[:len(s4)]
    s6 = [x for x in s6 if x <= 0xFFFFFFFFFFFFFFFF]
    c6 = c6[:len(s6)]
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<II", len(s4), len(s6)))
        f.write(array("I", s4).tobytes())
        f.write("".join(c4).encode("ascii", "replace"))
        f.write(array("Q", s6).tobytes())
        f.write("".join(c6).encode("ascii", "replace"))
    os.replace(tmp, out_path)
    return len(s4), len(s6)


def _download(urls, dest, deadline=150, quiet=False):
    """Try each mirror; gzip transfer, live progress, hard time limit per mirror
    so a throttled or stalled connection never hangs forever."""
    import gzip
    import sys
    import time
    tty = sys.stdout.isatty() and not quiet
    for u in urls:
        host = u.split("/")[2]
        t0 = time.time()
        got = 0
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "netmon",
                                                     "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=15) as r, open(dest, "wb") as f:
                src = gzip.GzipFile(fileobj=r) if r.headers.get("Content-Encoding") == "gzip" else r
                while True:
                    chunk = src.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    el = time.time() - t0
                    if tty:
                        print(f"\r    {host}: {got / 1048576:5.1f} MB  ({got / 1024 / max(el, .1):6.0f} KB/s) ",
                              end="", flush=True)
                    if el > deadline:
                        raise TimeoutError(f"too slow, gave up after {deadline}s")
            if tty:
                print()
            if os.path.getsize(dest) > 100_000:
                if not quiet:
                    print(f"    ✓ {host}: {got / 1048576:.1f} MB in {time.time() - t0:.0f}s", flush=True)
                return True
        except Exception as e:  # noqa: BLE001
            if tty:
                print()
            if not quiet:
                print(f"    ! {host}: {e}", flush=True)
    return False


def update(out_path, ipv6=True, quiet=False):
    """Download fresh CSVs and rebuild geoip.bin. Returns (n4, n6) or None."""
    d = os.path.dirname(out_path) or "."
    os.makedirs(d, exist_ok=True)
    v4, v6 = os.path.join(d, ".geo4.csv"), os.path.join(d, ".geo6.csv")
    try:
        if not quiet:
            print("  IPv4 ranges:", flush=True)
        ok4 = _download(URLS["v4"], v4, quiet=quiet)
        ok6 = False
        if ipv6:
            if not quiet:
                print("  IPv6 ranges:", flush=True)
            ok6 = _download(URLS["v6"], v6, quiet=quiet)
        if not ok4 and not ok6:
            return None
        return build(v4 if ok4 else None, v6 if ok6 else None, out_path)
    finally:
        for p in (v4, v6):
            try:
                os.remove(p)
            except OSError:
                pass
