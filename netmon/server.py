"""
Web panel + JSON API on the standard library's threaded HTTP server.
No Flask, no external dependencies; static files are served from memory,
pre-gzipped, with ETags.
"""
import gzip
import hashlib
import ipaddress
import json
import mimetypes
import os
import signal
import socket
import sys
import threading
import time
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, auth, config, db, firewall, geoip, queries, sysinfo
from .collector import Collector

WEB = os.path.join(os.path.dirname(__file__), "web")
COOKIE = "nm_s"
WINDOWS = set(queries.WINDOWS) | {"today", "yesterday"}

STATE = {"collector": None, "q": None, "lock": auth.Lockout()}
_static = {}


def _load_static(name):
    path = os.path.realpath(os.path.join(WEB, name))
    if not path.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(path):
        return None
    st = os.stat(path)
    hit = _static.get(name)
    if hit and hit["mtime"] == st.st_mtime:
        return hit
    with open(path, "rb") as f:
        raw = f.read()
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if name.endswith(".woff2"):
        ctype = "font/woff2"
    if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
        ctype += "; charset=utf-8"
    gz = gzip.compress(raw, 9) if not name.endswith((".woff2", ".png", ".jpg")) else None
    hit = {"raw": raw, "gz": gz, "ctype": ctype, "mtime": st.st_mtime,
           "etag": '"' + hashlib.sha1(raw).hexdigest()[:16] + '"'}
    _static[name] = hit
    return hit


class Handler(BaseHTTPRequestHandler):
    server_version = "NetMon"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet access log
        pass

    # ------------------------------------------------------------ helpers
    def client_ip(self):
        ip = self.client_address[0]
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        if STATE["cfg"].get("trust_proxy"):
            fwd = self.headers.get("X-Forwarded-For")
            if fwd:
                ip = fwd.split(",")[0].strip()
        return ip

    def _headers(self, code, ctype, length, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self'; font-src 'self' data:; connect-src 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def send_bytes(self, code, body, ctype, extra=None, compress=True):
        extra = dict(extra or {})
        if compress and len(body) > 1200 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, 5)
            extra["Content-Encoding"] = "gzip"
            extra["Vary"] = "Accept-Encoding"
        self._headers(code, ctype, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, code=200, extra=None):
        extra = dict(extra or {})
        extra.setdefault("Cache-Control", "no-store")
        body = json.dumps(obj, separators=(",", ":"), default=str).encode()
        self.send_bytes(code, body, "application/json", extra)

    def redirect(self, to, extra=None):
        h = {"Location": to, "Cache-Control": "no-store"}
        h.update(extra or {})
        self._headers(303, "text/plain", 0, h)

    def user(self):
        c = cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return None
        m = c.get(COOKIE)
        return auth.check_token(STATE["cfg"], m.value) if m else None

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            return None
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return None

    def args(self):
        return {k: v[-1] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    def page(self, name, code=200):
        st = _load_static(name)
        self.send_bytes(code, st["raw"], st["ctype"], {"Cache-Control": "no-store"})

    # ------------------------------------------------------------ dispatch
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path.startswith("/static/"):
                return self.static(path[8:])
            if path == "/favicon.ico":
                return self.static("favicon.svg")
            if path == "/login":
                if self.user():
                    return self.redirect("/")
                return self.page("login.html")
            if path == "/logout":
                return self.logout()
            if not self.user():
                if path.startswith("/api/"):
                    return self.send_json({"error": "Sign in required"}, 401)
                return self.redirect("/login")
            if path in ("/", "/index.html"):
                return self.page("index.html")
            fn = GET_API.get(path)
            if fn:
                return self.send_json(fn(self, self.args()))
            self.send_json({"error": "Not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            print(f"[web] GET {path}: {e!r}", flush=True)
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/login":
                return self.login()
            if path == "/logout":
                return self.logout()
            if not self.user():
                return self.send_json({"error": "Sign in required"}, 401)
            if self.headers.get("X-NetMon") != "1":
                return self.send_json({"error": "Missing request header"}, 403)
            data = self.body_json()
            if data is None:
                return self.send_json({"error": "Body must be JSON"}, 400)
            fn = POST_API.get(path)
            if not fn:
                return self.send_json({"error": "Not found"}, 404)
            res = fn(self, data)
            if res is None:          # handler already responded
                return
            if isinstance(res, tuple):
                return self.send_json(res[0], res[1])
            self.send_json(res)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            print(f"[web] POST {path}: {e!r}", flush=True)
            self.send_json({"error": str(e)}, 500)

    def static(self, name):
        st = _load_static(name)
        if not st:
            return self.send_json({"error": "Not found"}, 404)
        if self.headers.get("If-None-Match") == st["etag"]:
            self._headers(304, st["ctype"], 0, {"ETag": st["etag"]})
            return
        extra = {"ETag": st["etag"], "Cache-Control": "public, max-age=3600"}
        body = st["raw"]
        if st["gz"] and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = st["gz"]
            extra["Content-Encoding"] = "gzip"
            extra["Vary"] = "Accept-Encoding"
        self._headers(200, st["ctype"], len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    # ------------------------------------------------------------ auth
    def login(self):
        ip = self.client_ip()
        ctype = self.headers.get("Content-Type", "")
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 8192)).decode("utf-8", "replace") if n else ""
        if "json" in ctype:
            try:
                d = json.loads(raw or "{}")
            except ValueError:
                d = {}
        else:
            d = {k: v[-1] for k, v in urllib.parse.parse_qs(raw).items()}
        lk = STATE["lock"]
        if lk.locked(ip):
            return self.send_json({"error": "Too many attempts. Wait 10 minutes and try again."}, 429)
        cfg = config.load()
        STATE["cfg"] = cfg
        user = (d.get("user") or "").strip()
        pw = d.get("password") or ""
        if cfg.get("pw_hash") and user == cfg.get("admin_user") and \
                auth.verify_password(pw, cfg["pw_hash"]):
            lk.clear(ip)
            tok = auth.make_token(cfg, user)
            ttl = int(cfg.get("session_ttl_seconds", 7 * 86400))
            ck = f"{COOKIE}={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age={ttl}"
            return self.send_json({"ok": True}, 200, {"Set-Cookie": ck})
        lk.fail(ip)
        time.sleep(0.6)
        if not cfg.get("pw_hash"):
            return self.send_json({"error": "No admin account yet. Run: sudo netmon passwd"}, 401)
        return self.send_json({"error": "Wrong username or password"}, 401)

    def logout(self):
        ck = f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        self.redirect("/login", {"Set-Cookie": ck})


# ================================================================ API (GET)

def _win(a, default="24h"):
    w = a.get("window", default)
    return w if w in WINDOWS else default


def _int(a, k, default=0, lo=None, hi=None):
    try:
        v = int(a.get(k, default))
    except (TypeError, ValueError):
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _tz(a):
    return _int(a, "tz", 0, -900, 900)


def g_live(h, a):
    col = STATE["collector"]
    snap = col.snapshot(_int(a, "seconds", 120, 10, 600))
    snap["top_ips"] = [{"ip": ip, "n": n, "cc": cc, "flag": geoip.flag(cc),
                        "name": geoip.name(cc)} for ip, n, cc in col.sampler.top_ips]
    return snap


def g_overview(h, a):
    q = STATE["q"]
    tz = _tz(a)
    _, procs = sysinfo.listening_procs()
    return {"summary": q.summary(tz), "highlights": q.highlights(_win(a), tz, procs),
            "info": STATE["collector"].info(), "version": __version__}


def g_traffic(h, a):
    return STATE["q"].traffic(_win(a), a.get("step"), _tz(a))


def g_daily(h, a):
    return STATE["q"].daily(_int(a, "days", 30, 1, 31), _tz(a))


def g_protocols(h, a):
    return {"totals": STATE["q"].protocols(_win(a), _tz(a)),
            "live": STATE["collector"].snapshot(5)["proto_rate"]}


def g_proto_series(h, a):
    return STATE["q"].proto_series(_win(a), a.get("step"), _tz(a))


def g_ports(h, a):
    role = a.get("role", "")
    role = int(role) if role in ("0", "1") else None
    _, procs = sysinfo.listening_procs()
    return STATE["q"].ports(_win(a), role, a.get("sort", "total"),
                            _int(a, "limit", 50, 1, 500), _tz(a), procs)


def g_port_series(h, a):
    return STATE["q"].port_series(_int(a, "port", 0, 0, 65535), _int(a, "role", 0, 0, 1),
                                  _int(a, "p", 6), _win(a), a.get("step"), _tz(a))


def g_countries(h, a):
    return STATE["q"].countries(_win(a), _int(a, "dir", 0, 0, 1), 250, _tz(a))


def g_country_series(h, a):
    ccs = [c.strip().upper() for c in a.get("cc", "").split(",") if c.strip()]
    return STATE["q"].country_series(ccs, _win(a), _int(a, "dir", 0, 0, 1), a.get("step"), _tz(a))


def g_firewall(h, a):
    lst, _ = sysinfo.listening_procs()
    st = firewall.status({(p, port) for p, port, addr, proc in lst})
    st["me"] = h.client_ip()
    st["me_whitelisted"] = any(_ip_in(st["me"], w) for w in st["whitelist"])
    ssh_ports = sorted({port for p, port, addr, proc in lst if proc == "sshd" and p == 6})
    st["ssh_ports"] = ssh_ports or [22]
    return st


def _ip_in(ip, net):
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(net, strict=False)
    except ValueError:
        return False


def g_fwlog(h, a):
    return STATE["q"].fw_log(_int(a, "limit", 60, 1, 500))


def g_system(h, a):
    col = STATE["collector"]
    d = sysinfo.info(db.size(STATE["cfg"]["db_path"]), col.cpu)
    d["netmon"] = col.info()
    d["netmon"]["version"] = __version__
    return d


GET_API = {
    "/api/live": g_live, "/api/overview": g_overview, "/api/traffic": g_traffic,
    "/api/daily": g_daily, "/api/protocols": g_protocols, "/api/proto_series": g_proto_series,
    "/api/ports": g_ports, "/api/port_series": g_port_series, "/api/countries": g_countries,
    "/api/country_series": g_country_series, "/api/firewall": g_firewall,
    "/api/fwlog": g_fwlog, "/api/system": g_system,
}


# ================================================================ API (POST)

def _port(v, what="port"):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"Enter a {what} between 1 and 65535")
    if not 0 < v < 65536:
        raise ValueError(f"Enter a {what} between 1 and 65535")
    return v


def _proto(v):
    v = (v or "tcp").lower()
    if v not in ("tcp", "udp", "both"):
        raise ValueError("Protocol must be TCP, UDP or both")
    return v


def _label(v):
    return "".join(ch for ch in str(v or "")[:40] if ch.isprintable() and ch not in "\"'\\`$")


def _res(ok, msg, extra=None):
    out = {"ok": bool(ok), "msg": msg if isinstance(msg, str) else "ok"}
    if extra:
        out.update(extra)
    if not ok:
        out["error"] = out["msg"]
        return out, 500
    return out


def p_block(h, d):
    proto, label = _proto(d.get("proto")), _label(d.get("label"))
    direction = d.get("direction", "in")
    if direction not in ("in", "out", "both"):
        raise ValueError("Direction must be in, out or both")
    kill = d.get("kill")
    kill = None if kill is None else bool(kill)
    if d.get("lo") not in (None, "") or d.get("hi") not in (None, ""):
        lo, hi = _port(d.get("lo"), "start port"), _port(d.get("hi"), "end port")
        if lo > hi:
            lo, hi = hi, lo
        ok, e = firewall.add_block(lo=lo, hi=hi, proto=proto, direction=direction,
                                   label=label, kill=kill)
    else:
        ok, e = firewall.add_block(port=_port(d.get("port")), proto=proto,
                                   direction=direction, label=label, kill=kill)
    return _res(ok, "Blocked" if ok else "Could not apply rules", {"entry": e})


def p_block_toggle(h, d):
    return _res(*firewall.set_block_enabled(d.get("id"), bool(d.get("enabled"))))


def p_block_delete(h, d):
    return _res(*firewall.remove_block(d.get("id")))


def p_port(h, d):
    ok, msg = firewall.set_port(_port(d.get("port")), _proto(d.get("proto")),
                                bool(d.get("open")), _label(d.get("label")))
    return _res(ok, ("Opened" if d.get("open") else "Closed") if ok else msg)


def p_allow_delete(h, d):
    return _res(*firewall.remove_allow(d.get("id")))


def p_ip(h, d):
    cidr = str(d.get("cidr", "")).strip()
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        raise ValueError("Enter an IP address or CIDR, like 203.0.113.7 or 203.0.113.0/24")
    me = h.client_ip()
    if _ip_in(me, str(net)):
        raise ValueError(f"That range includes your own IP ({me}); you would lock yourself out")
    if net.prefixlen < (8 if net.version == 4 else 16):
        raise ValueError("That range is too wide")
    ok, e = firewall.add_ip_block(cidr, _label(d.get("label")))
    return _res(ok, "Blocked" if ok else "Could not apply rules", {"entry": e})


def p_ip_delete(h, d):
    return _res(*firewall.remove_ip_block(d.get("id")))


def p_ratelimit(h, d):
    return _res(*firewall.set_ratelimit(bool(d.get("enabled")), d.get("rate"),
                                        d.get("burst"), d.get("conn_max")))


def p_whitelist(h, d):
    ips = d.get("ips")
    if ips is not None:
        clean = []
        for ip in ips:
            try:
                clean.append(str(ipaddress.ip_network(str(ip).strip(), strict=False))
                             if "/" in str(ip) else str(ipaddress.ip_address(str(ip).strip())))
            except ValueError:
                raise ValueError(f"Not an IP or CIDR: {ip}")
        ips = clean
    tp = d.get("tunnel_ports")
    if tp is not None:
        tp = [_port(p, "tunnel port") for p in tp]
    return _res(*firewall.set_whitelist(ips, tp))


def p_whitelist_me(h, d):
    me = h.client_ip()
    cfg = config.load()
    return _res(*firewall.set_whitelist(list(cfg.get("whitelist", [])) + [me]))


def p_settings(h, d):
    def mut(c):
        if "kill_on_block" in d:
            c["kill_on_block"] = bool(d["kill_on_block"])
    config.update(mut)
    return {"ok": True, "msg": "Saved"}


def p_password(h, d):
    cfg = config.load()
    if not auth.verify_password(d.get("current", ""), cfg.get("pw_hash", "")):
        raise ValueError("Current password is wrong")
    new = d.get("new", "")
    if len(new) < 8:
        raise ValueError("Use at least 8 characters")
    user = (d.get("user") or cfg.get("admin_user")).strip()
    if not user or len(user) > 32:
        raise ValueError("Username must be 1–32 characters")

    def mut(c):
        c["pw_hash"] = auth.hash_password(new)
        c["admin_user"] = user
    cfg, _ = config.update(mut)
    STATE["cfg"] = cfg
    tok = auth.make_token(cfg, user)
    ttl = int(cfg.get("session_ttl_seconds", 7 * 86400))
    db.fw_log(cfg["db_path"], "account", "password changed")
    h_cookie = f"{COOKIE}={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age={ttl}"
    h.send_json({"ok": True, "msg": "Password updated"}, 200, {"Set-Cookie": h_cookie})
    return None


POST_API = {
    "/api/fw/block": p_block, "/api/fw/block/toggle": p_block_toggle,
    "/api/fw/block/delete": p_block_delete, "/api/fw/port": p_port,
    "/api/fw/allow/delete": p_allow_delete, "/api/fw/ip": p_ip,
    "/api/fw/ip/delete": p_ip_delete, "/api/fw/ratelimit": p_ratelimit,
    "/api/fw/whitelist": p_whitelist, "/api/fw/whitelist/me": p_whitelist_me,
    "/api/fw/settings": p_settings, "/api/account/password": p_password,
}


# ================================================================ server

class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass
        super().server_bind()


def make_server(host, port):
    if host in ("0.0.0.0", "", "::") and socket.has_ipv6:
        try:
            Server.address_family = socket.AF_INET6
            return Server(("::", port), Handler)
        except OSError:
            pass
    Server.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return Server((host, port), Handler)


def main():
    cfg = config.load()
    STATE["cfg"] = cfg
    if not cfg.get("secret_key"):
        cfg["secret_key"] = os.urandom(32).hex()
        config.save(cfg)
    STATE["lock"] = auth.Lockout(int(cfg.get("login_max_fails", 5)),
                                 int(cfg.get("login_lockout_seconds", 600)))
    if not cfg.get("pw_hash"):
        print("[web] no admin account configured — run: sudo netmon passwd", flush=True)

    port = int(cfg.get("web_port", 8080))
    try:
        firewall.open_panel_port(port)
        ok, msg = firewall.apply(cfg)
        print(f"[fw] {msg}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[fw] apply on start failed: {e}", flush=True)

    col = Collector(cfg)
    STATE["collector"] = col
    STATE["q"] = queries.Q(cfg["db_path"], col.poll, col.bucket,
                           int(cfg.get("retention_fine_hours", 24)))
    col.start()

    srv = make_server(cfg.get("web_host", "0.0.0.0"), port)
    print(f"[web] NetMon {__version__} listening on {cfg.get('web_host')}:{port}", flush=True)

    def stop(*_):
        try:
            col._flush()
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    srv.serve_forever(poll_interval=1)
    sys.exit(0)


if __name__ == "__main__":
    main()
