"""Host information for the System page (all from /proc, `ss` if present)."""
import os
import platform
import re
import socket
import time

from .shell import have, run

_proc_cache = {"ts": 0, "map": {}, "list": []}


def _meminfo():
    m = {}
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                k, v = ln.split(":", 1)
                m[k] = int(v.split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return m


def _os_name():
    try:
        with open("/etc/os-release") as f:
            for ln in f:
                if ln.startswith("PRETTY_NAME="):
                    return ln.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def _self_rss():
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return 0


def _ifaces():
    out = []
    addrs = {}
    if have("ip"):
        rc, txt, _ = run(["ip", "-o", "addr", "show"], timeout=5)
        if rc == 0:
            for ln in txt.splitlines():
                p = ln.split()
                if len(p) > 3 and p[2] in ("inet", "inet6"):
                    addrs.setdefault(p[1], []).append(p[3])
    try:
        with open("/proc/net/dev") as f:
            for ln in f.readlines()[2:]:
                name, _, data = ln.partition(":")
                v = data.split()
                name = name.strip()
                out.append({"name": name, "rx": int(v[0]), "tx": int(v[8]),
                            "addrs": addrs.get(name, [])})
    except (OSError, ValueError, IndexError):
        pass
    return out


def listening_procs(max_age=20):
    """[(proto_code, port, addr, process)] and {(proto, port): process}."""
    now = time.time()
    if now - _proc_cache["ts"] < max_age:
        return _proc_cache["list"], _proc_cache["map"]
    lst = []
    if have("ss"):
        rc, out, _ = run(["ss", "-H", "-tulpn"], timeout=8)
        if rc == 0:
            for ln in out.splitlines():
                p = ln.split()
                if len(p) < 5:
                    continue
                proto = 6 if p[0] == "tcp" else 17
                local = p[4]
                addr, _, port = local.rpartition(":")
                try:
                    port = int(port)
                except ValueError:
                    continue
                m = re.search(r'users:\(\("([^"]+)"', ln)
                lst.append((proto, port, addr, m.group(1) if m else ""))
    if not lst:
        from .conns import listening
        lst = [(c, p, "*", "") for c, p in sorted(listening())]
    mp = {}
    for proto, port, addr, proc in lst:
        if proc and not addr.startswith(("127.", "[::1]")):
            mp.setdefault((proto, port), proc)
    _proc_cache.update(ts=now, list=lst, map=mp)
    return lst, mp


def info(db_size=0, cpu=0.0):
    m = _meminfo()
    try:
        st = os.statvfs("/")
        disk = {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}
    except OSError:
        disk = {"total": 0, "free": 0}
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
    except (OSError, ValueError):
        up = 0
    try:
        load = os.getloadavg()
    except OSError:
        load = (0, 0, 0)
    lst, _ = listening_procs()
    seen, svc = set(), []
    for proto, port, addr, proc in sorted(lst, key=lambda x: (x[1], x[0])):
        k = (proto, port, proc)
        if k in seen:
            continue
        seen.add(k)
        svc.append({"proto": "TCP" if proto == 6 else "UDP", "port": port,
                    "addr": addr, "process": proc,
                    "local_only": addr.startswith(("127.", "[::1]", "localhost"))})
    return {
        "hostname": socket.gethostname(), "os": _os_name(), "kernel": platform.release(),
        "cpus": os.cpu_count() or 1, "cpu": cpu, "load": [round(x, 2) for x in load],
        "uptime": int(up),
        "mem": {"total": m.get("MemTotal", 0), "available": m.get("MemAvailable", 0)},
        "swap": {"total": m.get("SwapTotal", 0), "free": m.get("SwapFree", 0)},
        "disk": disk, "db_size": db_size, "rss": _self_rss(),
        "ifaces": _ifaces(), "listening": svc,
    }
