"""
Firewall — isolated, reversible, open by default.

NetMon never edits your own rules. Everything lives in chains it owns and is
rebuilt atomically from config.json on every change and on boot:

  mangle/NM_FW_IN   (PREROUTING)        inbound: runs before NAT, so it also
                                        covers DNAT'd / forwarded tunnel ports
  filter/NM_FW_OUT  (OUTPUT + FORWARD)  outbound blocks

Order inside NM_FW_IN (first match wins):
  loopback, established/related   -> pass (existing sessions & replies never cut)
  panel port                      -> pass (you can't lock yourself out of the panel)
  whitelisted IPs                 -> pass
  blocked IPs                     -> drop
  single-port exceptions          -> pass  (open one port inside a blocked range)
  port / range blocks             -> drop
  tunnel ports                    -> pass  (never rate-limited)
  rate-limit (optional)           -> drop excess NEW connections per source IP
Anything not matched simply continues to your normal rules: nothing is closed
unless you close it.
"""
import ipaddress
import time

from . import catalog, config, db, ipt
from .shell import have, run


def _protos(p):
    return ("tcp", "udp") if p in ("both", "all", "any", "", None) else (p,)


def _spec(b):
    return f"{b['lo']}:{b['hi']}" if b.get("kind") == "range" else str(b["port"])


def _fam_of(cidr):
    try:
        return str(ipaddress.ip_network(cidr, strict=False).version)
    except ValueError:
        return None


def build_rules(fam, cfg):
    I, O = ipt.FW_IN, ipt.FW_OUT
    panel = int(cfg.get("web_port", 8080))
    fin = [f"-A {I} -i lo -j RETURN",
           f"-A {I} -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN",
           f"-A {I} -p tcp --dport {panel} -j RETURN"]
    fout = [f"-A {O} -o lo -j RETURN",
            f"-A {O} -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"]

    for ip in cfg.get("whitelist", []):
        if _fam_of(ip) == fam:
            fin.append(f"-A {I} -s {ip} -m comment --comment nm:wl -j RETURN")
            fout.append(f"-A {O} -d {ip} -j RETURN")
    for e in cfg.get("ip_blocks", []):
        if e.get("enabled", True) and _fam_of(e["cidr"]) == fam:
            fin.append(f"-A {I} -s {e['cidr']} -m comment --comment nm:fw:ip:{e['id']} -j DROP")
            fout.append(f"-A {O} -d {e['cidr']} -m comment --comment nm:fw:ipo:{e['id']} -j REJECT")
    for a in cfg.get("allows", []):
        for p in _protos(a.get("proto")):
            fin.append(f"-A {I} -p {p} --dport {a['port']} -j RETURN")
            fout.append(f"-A {O} -p {p} --dport {a['port']} -j RETURN")
    for b in cfg.get("blocks", []):
        if not b.get("enabled", True):
            continue
        d = b.get("direction", "in")
        for p in _protos(b.get("proto")):
            if d in ("in", "both"):
                fin.append(f"-A {I} -p {p} --dport {_spec(b)} "
                           f"-m comment --comment nm:fw:b:{b['id']} -j DROP")
            if d in ("out", "both"):
                rej = "--reject-with tcp-reset" if p == "tcp" else ""
                fout.append(f"-A {O} -p {p} --dport {_spec(b)} "
                            f"-m comment --comment nm:fw:bo:{b['id']} -j REJECT {rej}".rstrip())
    for port in cfg.get("tunnel_ports", []):
        for p in ("tcp", "udp"):
            fin.append(f"-A {I} -p {p} --dport {int(port)} -j RETURN")

    if cfg.get("ratelimit_enabled"):
        rate = int(cfg.get("ratelimit_new_conn_per_sec", 60))
        burst = int(cfg.get("ratelimit_burst", 120))
        cmax = int(cfg.get("ratelimit_conn_max", 300))
        mask = "32" if fam == "4" else "64"
        fin.append(f"-A {I} -p tcp --syn -m hashlimit --hashlimit-above {rate}/sec "
                   f"--hashlimit-burst {burst} --hashlimit-mode srcip "
                   f"--hashlimit-srcmask {mask} --hashlimit-name nm_rl{fam} "
                   f"-m comment --comment nm:fw:rl -j DROP")
        if cmax > 0:
            fin.append(f"-A {I} -p tcp --syn -m connlimit --connlimit-above {cmax} "
                       f"--connlimit-mask {mask} -m comment --comment nm:fw:cl -j DROP")
    return fin, fout


def available():
    return ipt.available("4")


def apply(cfg=None):
    """Rebuild our chains from config. Returns (ok, message)."""
    if not available():
        return False, "iptables is not installed"
    cfg = cfg or config.load()
    errs = []
    for fam in ipt.families():
        fin, fout = build_rules(fam, cfg)
        ok1, e1 = ipt.restore(fam, "mangle", (ipt.FW_IN,), fin)
        ok2, e2 = ipt.restore(fam, "filter", (ipt.FW_OUT,), fout)
        ipt.STATE["fw"][fam] = True
        if not ok1:
            errs.append(f"v{fam} in: {e1}")
        if not ok2:
            errs.append(f"v{fam} out: {e2}")
    ipt.sync_hooks()
    return (not errs), ("; ".join(errs) or "applied")


def teardown():
    for fam in ipt.families():
        ipt.STATE["fw"][fam] = False
        ipt.drop_chain(fam, "mangle", ipt.FW_IN)
        ipt.drop_chain(fam, "filter", ipt.FW_OUT)
        while ipt.ipt(fam, "-D", "INPUT", "-p", "tcp", "--dport",
                      str(config.load().get("web_port", 8080)), "-m", "comment",
                      "--comment", "nm:panel", "-j", "ACCEPT")[0] == 0:
            pass
    ipt.sync_hooks()


def open_panel_port(port):
    """Make sure the panel port is reachable even on a default-deny server."""
    for fam in ipt.families():
        args = ["-p", "tcp", "--dport", str(port), "-m", "comment",
                "--comment", "nm:panel", "-j", "ACCEPT"]
        if ipt.ipt(fam, "-C", "INPUT", *args)[0] != 0:
            ipt.ipt(fam, "-I", "INPUT", "1", *args)
    if have("ufw"):
        rc, out, _ = run(["ufw", "status"], timeout=10)
        if rc == 0 and "Status: active" in out and f"{port}/tcp" not in out:
            run(["ufw", "allow", f"{port}/tcp"], timeout=15)


# ---------------------------------------------------------------- helpers

def _kill_flows(proto, port=None, ip=None):
    """Drop existing conntrack entries so a new block bites immediately."""
    if not have("conntrack"):
        return
    for p in _protos(proto):
        if ip:
            run(["conntrack", "-D", "-s", ip], timeout=10)
            break
        run(["conntrack", "-D", "-p", p, "--orig-port-dst", str(port)], timeout=10)


def _next_id(items):
    ids = [int(x["id"]) for x in items if str(x.get("id", "")).isdigit()]
    return str(max(ids) + 1 if ids else 1)


def _log(cfg, action, detail):
    db.fw_log(cfg["db_path"], action, detail)


def _covers(b, port, proto):
    if not b.get("enabled", True) or b.get("direction", "in") not in ("in", "both"):
        return False
    if proto not in _protos(b.get("proto")):
        return False
    if b.get("kind") == "range":
        return int(b["lo"]) <= port <= int(b["hi"])
    return int(b["port"]) == port


def port_open(cfg, port, proto="tcp"):
    """True if inbound `port/proto` is NOT blocked by NetMon."""
    if any(int(a["port"]) == port and proto in _protos(a.get("proto"))
           for a in cfg.get("allows", [])):
        return True
    return not any(_covers(b, port, proto) for b in cfg.get("blocks", []))


def _finish(cfg, action, detail):
    ok, msg = apply(cfg)
    _log(cfg, action, detail)
    return ok, msg


# ---------------------------------------------------------------- operations

def add_block(port=None, lo=None, hi=None, proto="tcp", direction="in", label="",
              wellknown=False, kill=None):
    def mut(cfg):
        blocks = cfg.setdefault("blocks", [])
        e = {"id": _next_id(blocks), "proto": proto, "direction": direction,
             "label": label, "enabled": True, "wellknown": wellknown, "ts": int(time.time())}
        if lo is not None and hi is not None:
            e.update(kind="range", lo=int(lo), hi=int(hi))
        else:
            e.update(kind="single", port=int(port))
            # blocking a single port removes any exception for it
            cfg["allows"] = [a for a in cfg.get("allows", []) if int(a["port"]) != int(port)]
        blocks.append(e)
        return e
    cfg, e = config.update(mut)
    what = f"{e['lo']}-{e['hi']}" if e["kind"] == "range" else str(e["port"])
    ok, msg = _finish(cfg, "block", f"{what}/{proto} {direction}" + (f" ({label})" if label else ""))
    if kill is None:
        kill = cfg.get("kill_on_block", True)
    if ok and kill and direction in ("in", "both"):
        if e["kind"] == "single" and e["port"] != 22:
            _kill_flows(proto, port=e["port"])
        elif e["kind"] == "range" and e["hi"] - e["lo"] < 64:
            for p in range(e["lo"], e["hi"] + 1):
                _kill_flows(proto, port=p)
    return ok, e


def remove_block(block_id):
    cfg, gone = config.update(lambda c: _pop(c, "blocks", block_id))
    if not gone:
        return False, "not found"
    return _finish(cfg, "unblock", _desc(gone))


def set_block_enabled(block_id, enabled):
    def mut(c):
        for b in c.get("blocks", []):
            if str(b["id"]) == str(block_id):
                b["enabled"] = bool(enabled)
                return b
        return None
    cfg, b = config.update(mut)
    if not b:
        return False, "not found"
    return _finish(cfg, "resume" if enabled else "pause", _desc(b))


def _pop(cfg, key, item_id):
    items = cfg.get(key, [])
    for x in items:
        if str(x.get("id")) == str(item_id):
            items.remove(x)
            return x
    return None


def _desc(b):
    if "cidr" in b:
        return b["cidr"]
    if b.get("kind") == "range":
        return f"{b['lo']}-{b['hi']}/{b.get('proto')}"
    return f"{b.get('port')}/{b.get('proto')}"


def set_port(port, proto, enabled, label=""):
    """Open (enabled=True) or close one port. Opening a port that sits inside a
    blocked range adds a single-port exception; closing adds a single block."""
    port = int(port)
    want = set(_protos(proto))

    def mut(cfg):
        if enabled:
            keep = []
            for b in cfg.get("blocks", []):
                if b.get("kind") == "single" and int(b["port"]) == port \
                        and b.get("direction", "in") in ("in", "both"):
                    rest = set(_protos(b.get("proto"))) - want
                    if not rest:
                        continue
                    b["proto"] = rest.pop()
                keep.append(b)
            cfg["blocks"] = keep
            still = [p for p in want if not port_open(cfg, port, p)]
            if still:
                allows = cfg.setdefault("allows", [])
                allows.append({"id": _next_id(allows), "port": port,
                               "proto": "both" if len(still) == 2 else still[0],
                               "label": label})
        else:
            cfg["allows"] = [a for a in cfg.get("allows", []) if int(a["port"]) != port]
            missing = [p for p in want if port_open(cfg, port, p)]
            if missing:
                blocks = cfg.setdefault("blocks", [])
                blocks.append({"id": _next_id(blocks), "kind": "single", "port": port,
                               "proto": "both" if len(missing) == 2 else missing[0],
                               "direction": "in", "label": label, "enabled": True,
                               "wellknown": True, "ts": int(time.time())})
    cfg, _ = config.update(mut)
    ok, msg = _finish(cfg, "open" if enabled else "close",
                      f"{port}/{proto}" + (f" ({label})" if label else ""))
    if ok and not enabled and port != 22 and cfg.get("kill_on_block", True):
        _kill_flows(proto, port=port)
    return ok, msg


def remove_allow(allow_id):
    cfg, gone = config.update(lambda c: _pop(c, "allows", allow_id))
    if not gone:
        return False, "not found"
    return _finish(cfg, "remove exception", f"{gone['port']}/{gone.get('proto')}")


def add_ip_block(cidr, label=""):
    cidr = str(ipaddress.ip_network(cidr, strict=False)) if "/" in cidr else \
        str(ipaddress.ip_address(cidr))

    def mut(c):
        items = c.setdefault("ip_blocks", [])
        e = {"id": _next_id(items), "cidr": cidr, "label": label, "enabled": True,
             "ts": int(time.time())}
        items.append(e)
        return e
    cfg, e = config.update(mut)
    ok, msg = _finish(cfg, "block ip", cidr + (f" ({label})" if label else ""))
    if ok and "/" not in cidr:
        _kill_flows("tcp", ip=cidr)
    return ok, e


def remove_ip_block(item_id):
    cfg, gone = config.update(lambda c: _pop(c, "ip_blocks", item_id))
    if not gone:
        return False, "not found"
    return _finish(cfg, "unblock ip", gone["cidr"])


def set_ratelimit(enabled, rate=None, burst=None, cmax=None):
    def mut(c):
        c["ratelimit_enabled"] = bool(enabled)
        if rate is not None:
            c["ratelimit_new_conn_per_sec"] = max(1, int(rate))
        if burst is not None:
            c["ratelimit_burst"] = max(1, int(burst))
        if cmax is not None:
            c["ratelimit_conn_max"] = max(0, int(cmax))
    cfg, _ = config.update(mut)
    return _finish(cfg, "rate-limit", f"{'on' if enabled else 'off'} "
                   f"{cfg['ratelimit_new_conn_per_sec']}/s burst {cfg['ratelimit_burst']}")


def set_whitelist(ips=None, tunnel_ports=None):
    def mut(c):
        if ips is not None:
            c["whitelist"] = sorted({s.strip() for s in ips if s.strip()})
        if tunnel_ports is not None:
            c["tunnel_ports"] = sorted({int(p) for p in tunnel_ports})
    cfg, _ = config.update(mut)
    return _finish(cfg, "whitelist", f"{len(cfg['whitelist'])} IPs, "
                   f"{len(cfg['tunnel_ports'])} tunnel ports")


# ---------------------------------------------------------------- status

def status(listening=None):
    cfg = config.load()
    listening = listening or set()
    lports = {p for _, p in listening}
    groups = []
    for cat, items in catalog.CATALOG:
        rows = []
        for port, proto, svc, desc in items:
            protos = _protos(proto)
            rows.append({
                "port": port, "proto": proto, "service": svc, "note": desc,
                "open": all(port_open(cfg, port, p) for p in protos),
                "listening": port in lports,
            })
        groups.append({"name": cat, "ports": rows})
    return {
        "available": available(),
        "ipv6": ipt.available("6"),
        "panel_port": cfg.get("web_port", 8080),
        "ssh_open": port_open(cfg, 22, "tcp"),
        "ratelimit": {
            "enabled": cfg.get("ratelimit_enabled", False),
            "rate": cfg.get("ratelimit_new_conn_per_sec"),
            "burst": cfg.get("ratelimit_burst"),
            "conn_max": cfg.get("ratelimit_conn_max"),
        },
        "kill_on_block": cfg.get("kill_on_block", True),
        "whitelist": cfg.get("whitelist", []),
        "tunnel_ports": cfg.get("tunnel_ports", []),
        "blocks": cfg.get("blocks", []),
        "allows": cfg.get("allows", []),
        "ip_blocks": cfg.get("ip_blocks", []),
        "groups": groups,
    }
