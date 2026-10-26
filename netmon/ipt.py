"""
Shared iptables / ip6tables plumbing.

NetMon only ever touches chains it owns (all prefixed NM_) plus the single
jump rules that hook them into the built-in chains. Rule sets are loaded with
`iptables-restore --noflush`, which replaces a whole chain atomically in one
call instead of dozens of fork/exec round-trips.
"""
import re
import shlex
import threading

from .shell import have, run

# family -> (iptables, iptables-save, iptables-restore)
FAMILIES = {
    "4": ("iptables", "iptables-save", "iptables-restore"),
    "6": ("ip6tables", "ip6tables-save", "ip6tables-restore"),
}

ACC_IN, ACC_OUT = "NM_ACC_IN", "NM_ACC_OUT"
ACC_SUB = ("NM_PI_TCP", "NM_PI_UDP", "NM_PO_TCP", "NM_PO_UDP")
FW_IN, FW_OUT = "NM_FW_IN", "NM_FW_OUT"

LOCK = threading.RLock()

# hook state, shared by accounting + firewall
STATE = {"ifaces": [], "acc": {"4": False, "6": False}, "fw": {"4": False, "6": False}}


def available(fam="4"):
    return have(FAMILIES[fam][0])


def families():
    return [f for f in FAMILIES if available(f)]


def ipt(fam, *args, table="filter", timeout=15):
    return run([FAMILIES[fam][0], "-w", "5", "-t", table, *args], timeout=timeout)


def chain_exists(fam, table, chain):
    return ipt(fam, "-n", "-L", chain, table=table)[0] == 0


def ensure_chain(fam, table, chain):
    if not chain_exists(fam, table, chain):
        ipt(fam, "-N", chain, table=table)


def restore(fam, table, chains, lines):
    """Flush `chains` and load `lines` (each like '-A CHAIN ...') atomically.
    Falls back to one command per rule if iptables-restore is unhappy."""
    with LOCK:
        for c in chains:
            ensure_chain(fam, table, c)
        payload = [f"*{table}"] + [f"-F {c}" for c in chains] + list(lines) + ["COMMIT", ""]
        rc, _, err = run([FAMILIES[fam][2], "-w", "5", "-n"], stdin="\n".join(payload))
        if rc == 0:
            return True, ""
        # slow path
        for c in chains:
            ipt(fam, "-F", c, table=table)
        bad = 0
        for ln in lines:
            if ipt(fam, *shlex.split(ln), table=table)[0] != 0:
                bad += 1
        return bad == 0, (err.strip() or f"{bad} rules failed")


def drop_chain(fam, table, chain):
    with LOCK:
        for hook in _hooks_of(table):
            delete_jumps(fam, table, hook, chain)
        ipt(fam, "-F", chain, table=table)
        ipt(fam, "-X", chain, table=table)


def _hooks_of(table):
    return {"mangle": ("PREROUTING", "POSTROUTING", "INPUT", "OUTPUT", "FORWARD"),
            "filter": ("INPUT", "OUTPUT", "FORWARD")}.get(table, ())


def list_rules(fam, table, chain):
    rc, out, _ = ipt(fam, "-S", chain, table=table)
    if rc != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.startswith("-A ")]


def delete_jumps(fam, table, hook, chain):
    for r in list_rules(fam, table, hook):
        if re.search(rf"-j {re.escape(chain)}(\s|$)", r):
            ipt(fam, *shlex.split("-D" + r[2:]), table=table)


def _norm(rule):
    return " ".join(rule.replace('"', "").split())


def _sync(fam, table, hook, desired, owned):
    """Ensure `desired` jump specs sit at the very top of `hook`, in order."""
    want = [_norm(f"-A {hook} {d}") for d in desired]
    cur = [_norm(r) for r in list_rules(fam, table, hook)]
    if cur[:len(want)] == want and not any(
            any(f"-j {o}" in r for o in owned) for r in cur[len(want):]):
        return
    for o in owned:
        delete_jumps(fam, table, hook, o)
    for i, d in enumerate(desired, 1):
        ipt(fam, "-I", hook, str(i), *shlex.split(d), table=table)


def sync_hooks():
    """(Re)hook our chains in the right order: accounting first, then firewall."""
    with LOCK:
        for fam in families():
            acc = STATE["acc"].get(fam)
            fw = STATE["fw"].get(fam)
            ifs = STATE["ifaces"] or []
            pre = ([f"-i {i} -j {ACC_IN}" for i in ifs] if acc else []) + \
                  ([f"-j {FW_IN}"] if fw else [])
            post = [f"-o {i} -j {ACC_OUT}" for i in ifs] if acc else []
            _sync(fam, "mangle", "PREROUTING", pre, (ACC_IN, FW_IN))
            _sync(fam, "mangle", "POSTROUTING", post, (ACC_OUT,))
            outj = [f"-j {FW_OUT}"] if fw else []
            _sync(fam, "filter", "OUTPUT", outj, (FW_OUT,))
            _sync(fam, "filter", "FORWARD", outj, (FW_OUT,))


COUNTER_RE = re.compile(r'^\[(\d+):(\d+)\]\s+-A\s+(\S+)\s.*?--comment\s+"?(nm:[^\s"]+)')


def read_counters(fam, table="mangle"):
    """{tag: (packets, bytes)} for every rule carrying an nm: comment."""
    rc, out, _ = run([FAMILIES[fam][1], "-c", "-t", table], timeout=20)
    res = {}
    if rc != 0:
        return res
    for line in out.splitlines():
        if "nm:" not in line or not line.startswith("["):
            continue
        m = COUNTER_RE.match(line)
        if m:
            p, b = int(m.group(1)), int(m.group(2))
            tag = m.group(4)
            if tag in res:
                op, ob = res[tag]
                res[tag] = (op + p, ob + b)
            else:
                res[tag] = (p, b)
    return res
