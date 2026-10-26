"""
Exact byte / packet / new-connection accounting with iptables counters.

Pure counting rules in the mangle table (no ACCEPT / DROP), hooked only on the
monitored interface(s), so it can never change how traffic flows.

    NM_ACC_IN  (PREROUTING  -i <wan>)  download side
    NM_ACC_OUT (POSTROUTING -o <wan>)  upload side

Per protocol: bytes, packets and NEW connections (= requests) per direction.
Per port, two roles are tracked separately:
    server (s): a port on this machine that clients connect to
                download = --dport P in, upload = --sport P out
    client (c): a remote port this machine (or a forwarded client) connects to
                upload = --dport P out, download = --sport P in
"""
from . import ipt

PROTOS = [(6, "tcp"), (17, "udp"), (1, "icmp"), (47, "gre"), (50, "esp"), (132, "sctp")]
PROTO_NAMES = {6: "TCP", 17: "UDP", 1: "ICMP", 47: "GRE", 50: "ESP", 132: "SCTP", 0: "Other"}


def _pnum(fam, code):
    return 58 if (fam == "6" and code == 1) else code


def build_rules(fam, server_ports, client_ports):
    """server_ports / client_ports: iterables of (proto_code, port)."""
    I, O = ipt.ACC_IN, ipt.ACC_OUT
    L = []
    for ch, d in ((I, "i"), (O, "o")):
        L.append(f"-A {ch} -m comment --comment nm:a{d}")
        L.append(f"-A {ch} -m conntrack --ctstate NEW -m comment --comment nm:na{d}")
        for code, _ in PROTOS:
            n = _pnum(fam, code)
            L.append(f"-A {ch} -p {n} -m comment --comment nm:p{d}:{code}")
            L.append(f"-A {ch} -p {n} -m conntrack --ctstate NEW -m comment --comment nm:n{d}:{code}")
    L.append(f"-A {I} -p tcp -j NM_PI_TCP")
    L.append(f"-A {I} -p udp -j NM_PI_UDP")
    L.append(f"-A {O} -p tcp -j NM_PO_TCP")
    L.append(f"-A {O} -p udp -j NM_PO_UDP")

    for code, pname, pin, pout in ((6, "tcp", "NM_PI_TCP", "NM_PO_TCP"),
                                   (17, "udp", "NM_PI_UDP", "NM_PO_UDP")):
        srv = sorted(p for c, p in server_ports if c == code)
        cli = sorted(p for c, p in client_ports if c == code)
        for p in srv:
            L.append(f"-A {pin} -p {pname} --dport {p} -m conntrack --ctstate NEW "
                     f"-m comment --comment nm:q:s:{code}:{p}")
            L.append(f"-A {pin} -p {pname} --dport {p} -m comment --comment nm:b:i:s:{code}:{p} -j RETURN")
            L.append(f"-A {pout} -p {pname} --sport {p} -m comment --comment nm:b:o:s:{code}:{p} -j RETURN")
        for p in cli:
            L.append(f"-A {pout} -p {pname} --dport {p} -m conntrack --ctstate NEW "
                     f"-m comment --comment nm:q:c:{code}:{p}")
            L.append(f"-A {pout} -p {pname} --dport {p} -m comment --comment nm:b:o:c:{code}:{p} -j RETURN")
            L.append(f"-A {pin} -p {pname} --sport {p} -m comment --comment nm:b:i:c:{code}:{p} -j RETURN")
    return L


CHAINS = (ipt.ACC_IN, ipt.ACC_OUT) + ipt.ACC_SUB


class Accounting:
    def __init__(self, cfg, ifaces):
        self.ifaces = list(ifaces)
        self.fams = ipt.families() if cfg.get("use_iptables_accounting", True) else []
        self.enabled = bool(self.fams)
        self.prev = {}
        self.server = set()
        self.client = set()
        self.error = ""

    def setup(self, server=(), client=()):
        if not self.fams:
            self.enabled = False
            return False
        self.server, self.client = set(server), set(client)
        ok_any = False
        for fam in self.fams:
            ok, err = ipt.restore(fam, "mangle", CHAINS,
                                  build_rules(fam, self.server, self.client))
            if ok:
                ok_any = True
                ipt.STATE["acc"][fam] = True
            else:
                self.error = err
                print(f"[acct] v{fam} setup failed: {err}", flush=True)
        ipt.STATE["ifaces"] = self.ifaces
        ipt.sync_hooks()
        self.prev = {}
        self.enabled = ok_any
        return ok_any

    def read(self):
        """{tag: (d_packets, d_bytes)} since the previous read."""
        if not self.enabled:
            return {}
        cur = {}
        for fam in self.fams:
            for tag, (p, b) in ipt.read_counters(fam, "mangle").items():
                op, ob = cur.get(tag, (0, 0))
                cur[tag] = (op + p, ob + b)
        delta = {}
        for tag, (p, b) in cur.items():
            pp, pb = self.prev.get(tag, (0, 0))
            dp, db = p - pp, b - pb
            if dp < 0 or db < 0:   # counters were reset underneath us
                dp, db = p, b
            if dp or db:
                delta[tag] = (dp, db)
        self.prev = cur
        return delta

    def rebuild(self, server, client):
        """Swap the per-port rule set. Returns the counter delta captured just
        before the swap, so nothing is lost."""
        pending = self.read()
        self.setup(server, client)
        return pending

    def teardown(self):
        for fam in ipt.families():
            ipt.STATE["acc"][fam] = False
            for c in CHAINS:
                ipt.drop_chain(fam, "mangle", c)
        ipt.sync_hooks()
