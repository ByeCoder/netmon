"""
Settings loader / saver.

Everything lives in /etc/netmon/config.json (written by install.sh). Missing
keys fall back to DEFAULTS so an old config never crashes a new version.
Paths can be redirected with NETMON_BASE / NETMON_DATA for testing.
"""
import json
import os
import threading

BASE_DIR = os.environ.get("NETMON_BASE", "/etc/netmon")
DATA_DIR = os.environ.get("NETMON_DATA", "/var/lib/netmon")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

_lock = threading.RLock()

DEFAULTS = {
    # ---- web panel
    "web_host": "0.0.0.0",
    "web_port": 8080,
    "secret_key": "",
    "admin_user": "admin",
    "pw_hash": "",
    "trust_proxy": False,            # honour X-Forwarded-For (only behind your own proxy)
    "session_ttl_seconds": 7 * 86400,
    "login_max_fails": 5,
    "login_lockout_seconds": 600,

    # ---- storage
    "db_path": os.path.join(DATA_DIR, "netmon.db"),
    "geoip_path": os.path.join(DATA_DIR, "geoip.bin"),
    "retention_fine_hours": 24,      # fine-grained history
    "retention_hourly_days": 30,     # hourly history
    "flush_interval": 30,            # seconds between DB writes (fewer = less disk wear)

    # ---- collector
    "interfaces": [],                # [] = auto (default-route interface)
    "poll_interval": 5,              # seconds per fine traffic sample
    "bucket_seconds": 60,            # resolution of port / country / protocol history
    "conn_sample_interval": 10,      # seconds between connection-table samples
    "use_iptables_accounting": True,
    "max_server_ports": 64,          # per-port counters for listening / inbound ports
    "max_client_ports": 32,          # per-port counters for outbound destination ports

    # ---- firewall (state, re-applied on boot)
    "blocks": [],                    # port / range blocks
    "allows": [],                    # single-port exceptions inside blocked ranges
    "ip_blocks": [],                 # blocked IPs / CIDRs
    "whitelist": [],                 # IPs that are never blocked or rate-limited
    "tunnel_ports": [],              # ports that are never rate-limited
    "kill_on_block": True,           # drop existing connections when a port is blocked

    # ---- soft anti-DDoS (off by default)
    "ratelimit_enabled": False,
    "ratelimit_new_conn_per_sec": 60,
    "ratelimit_burst": 120,
    "ratelimit_conn_max": 300,
}


def load():
    with _lock:
        cfg = json.loads(json.dumps(DEFAULTS))
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            print(f"[config] could not parse {CONFIG_PATH}: {e}", flush=True)
        if os.environ.get("NETMON_DB"):
            cfg["db_path"] = os.environ["NETMON_DB"]
        if os.environ.get("NETMON_PORT"):
            cfg["web_port"] = int(os.environ["NETMON_PORT"])
        return cfg


def save(cfg):
    with _lock:
        os.makedirs(BASE_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, CONFIG_PATH)


def update(fn):
    """Atomically load -> mutate -> save. `fn(cfg)` may return a value."""
    with _lock:
        cfg = load()
        result = fn(cfg)
        save(cfg)
        return cfg, result
