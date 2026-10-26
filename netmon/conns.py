"""
Connection sampler: who is connecting, from where, to which port.

Preferred source is the kernel conntrack table (`conntrack -L`): it sees every
flow, including NAT / forwarded tunnel traffic, and keeps closed TCP flows for
a while (TIME_WAIT), so short connections are rarely missed. Fallback is
/proc/net/{tcp,tcp6,udp,udp6} (local sockets only).

Each sample is diffed against the previous one; flows not seen before are
"new requests". Keys are stored as hashes to keep memory tiny.
"""
import ipaddress
import os
import socket
import struct
import time
from collections import Counter

from .shell import have, run

PNUM = {"tcp": 6, "udp": 17, "icmp": 1, "icmpv6": 1, "gre": 47, "esp": 50, "sctp": 132}


def _is_public(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_global


def local_addresses():
    addrs = {"127.0.0.1", "::1"}
    if have("ip"):
        rc, out, _ = run(["ip", "-o", "addr", "show"], timeout=5)
        if rc == 0:
            for ln in out.splitlines():
                p = ln.split()
                for i, t in enumerate(p):
                    if t in ("inet", "inet6") and i + 1 < len(p):
                        addrs.add(p[i + 1].split("/")[0])
            return addrs
    # fallbacks without iproute2
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 53))
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        with open("/proc/net/if_inet6") as f:
            for ln in f:
                h = ln.split()[0]
                addrs.add(socket.inet_ntop(socket.AF_INET6, bytes.fromhex(h)))
    except OSError:
        pass
    try:
        with open("/proc/net/fib_trie") as f:
            lines = f.read().splitlines()
        for i, ln in enumerate(lines):
            if "/32 host LOCAL" in ln and i > 0:
                addrs.add(lines[i - 1].split()[-1])
    except OSError:
        pass
    return addrs


# ---------------------------------------------------------------- /proc parsing

def _hex_ip(hx):
    if len(hx) == 8:
        return socket.inet_ntoa(struct.pack("<I", int(hx, 16)))
    b = bytes.fromhex(hx)
    w = struct.unpack("<IIII", b)
    ip = socket.inet_ntop(socket.AF_INET6, struct.pack(">IIII", *w))
    if ip.startswith("::ffff:"):
        return ip[7:]
    return ip


def _proc(path):
    try:
        with open(path) as f:
            next(f)
            for ln in f:
                p = ln.split()
                if len(p) < 4:
                    continue
                lh, lp = p[1].rsplit(":", 1)
                rh, rp = p[2].rsplit(":", 1)
                yield lh, int(lp, 16), rh, int(rp, 16), p[3]
    except (OSError, StopIteration, ValueError):
        return


def ephemeral_low():
    try:
        with open("/proc/sys/net/ipv4/ip_local_port_range") as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 32768


def listening():
    """{(proto_code, port)} for sockets reachable from outside (not loopback)."""
    out = set()
    eph = ephemeral_low()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        for lh, lp, rh, rp, st in _proc(path):
            if st == "0A" and not _hex_ip(lh).startswith(("127.", "::1")):
                out.add((6, lp))
    for path in ("/proc/net/udp", "/proc/net/udp6"):
        for lh, lp, rh, rp, st in _proc(path):
            if rp == 0 and lp < eph and not _hex_ip(lh).startswith(("127.", "::1")):
                out.add((17, lp))
    return out


# ---------------------------------------------------------------- sampler

class Sampler:
    def __init__(self):
        self.use_ct = have("conntrack")
        self.prev = None
        self.local = local_addresses()
        self.local_ts = time.time()
        self.listen = listening()
        self.last_cost = 0.0
        # live info for the UI
        self.active = 0
        self.top_ips = []
        self.source = "conntrack" if self.use_ct else "proc"

    def _flows_conntrack(self):
        for fam in ("ipv4", "ipv6"):
            rc, out, _ = run(["conntrack", "-L", "-f", fam], timeout=20)
            if rc != 0:
                if fam == "ipv4":
                    self.use_ct = False
                    self.source = "proc"
                    return None
                continue
            for ln in out.splitlines():
                p = ln.split()
                if len(p) < 4:
                    continue
                proto = PNUM.get(p[0], 0)
                src = dst = None
                sport = dport = 0
                for t in p[2:]:
                    if t.startswith("src=") and src is None:
                        src = t[4:]
                    elif t.startswith("dst=") and dst is None:
                        dst = t[4:]
                    elif t.startswith("sport=") and not sport:
                        sport = int(t[6:])
                    elif t.startswith("dport=") and not dport:
                        dport = int(t[6:])
                        break
                if src and dst:
                    yield proto, src, sport, dst, dport
        return None

    def _flows_proc(self):
        lp_listen = {p for c, p in self.listen}
        for path, proto in (("/proc/net/tcp", 6), ("/proc/net/tcp6", 6),
                            ("/proc/net/udp", 17), ("/proc/net/udp6", 17)):
            for lh, lport, rh, rport, st in _proc(path):
                if (proto == 6 and st == "0A") or rport == 0:
                    continue
                l, r = _hex_ip(lh), _hex_ip(rh)
                if lport in lp_listen:      # remote -> us
                    yield proto, r, rport, l, lport
                else:                       # us -> remote
                    yield proto, l, lport, r, rport

    def sample(self, geo_lookup):
        """Returns dict with new-flow tallies since the previous sample:
             countries: Counter{(cc, dir)}   dir 0 = inbound, 1 = outbound
             in_ports:  Counter{(proto, port)}  new inbound flows per local port
             out_ports: Counter{(proto, port)}  new outbound flows per remote port
             protos:    Counter{(proto, dir)}
        """
        t0 = time.time()
        if t0 - self.local_ts > 300:
            self.local = local_addresses()
            self.listen = listening()
            self.local_ts = t0
        flows = None
        if self.use_ct:
            flows = list(self._flows_conntrack() or [])
            if not self.use_ct:
                flows = None
        if flows is None:
            self.listen = listening()
            flows = list(self._flows_proc())

        local = self.local
        seen = set()
        countries, in_ports, out_ports, protos = Counter(), Counter(), Counter(), Counter()
        ip_now = Counter()
        prev = self.prev
        for proto, src, sport, dst, dport in flows:
            if src in local and dst in local:
                continue
            if dst in local:
                d, remote = 0, src
            elif src in local:
                d, remote = 1, dst
            elif _is_public(src):          # forwarded / DNAT'd inbound
                d, remote = 0, src
            else:                          # forwarded client going out
                d, remote = 1, dst
            if d == 0:
                ip_now[remote] += 1
            k = hash((proto, src, sport, dst, dport))
            seen.add(k)
            if prev is None or k in prev:
                continue
            protos[(proto, d)] += 1
            if d == 0:
                if dport:
                    in_ports[(proto, dport)] += 1
            elif dport:
                out_ports[(proto, dport)] += 1
            if _is_public(remote):
                countries[(geo_lookup(remote), d)] += 1
        self.prev = seen
        self.active = len(seen)
        self.top_ips = [(ip, n, geo_lookup(ip)) for ip, n in ip_now.most_common(25)]
        self.last_cost = time.time() - t0
        return {"countries": countries, "in_ports": in_ports,
                "out_ports": out_ports, "protos": protos}
