"""
netmon command line.

  netmon run            start panel + collector (what the service runs)
  netmon passwd         set admin username / password
  netmon status         show settings and what is being measured
  netmon geoip-update   download a fresh country database (--no-ipv6 = smaller)
  netmon geoip-import   build it from CSV files you downloaded yourself
  netmon fw-apply       re-apply firewall rules from config
  netmon unblock-all    emergency: remove every NetMon block (keeps whitelist)
  netmon teardown       remove all NetMon iptables chains (used by uninstall)
"""
import getpass
import os
import sys

from . import __version__, auth, config


def _passwd():
    user = os.environ.get("NETMON_USER")
    pw = os.environ.get("NETMON_PASS")
    cfg = config.load()
    if not user:
        cur = cfg.get("admin_user") or "admin"
        user = input(f"  Admin username [{cur}]: ").strip() or cur
    if not pw:
        while True:
            pw = getpass.getpass("  Admin password (min 8 chars): ")
            if len(pw) < 8:
                print("  ! Too short, use at least 8 characters")
                continue
            if pw != getpass.getpass("  Repeat password: "):
                print("  ! Passwords don't match")
                continue
            break
    if len(pw) < 8:
        sys.exit("password must be at least 8 characters")

    def mut(c):
        c["admin_user"] = user
        c["pw_hash"] = auth.hash_password(pw)
        if not c.get("secret_key"):
            c["secret_key"] = os.urandom(32).hex()
    config.update(mut)
    print(f"  ✓ Admin account '{user}' saved")


def _status():
    from . import geoip, ipt
    from .collector import default_ifaces
    cfg = config.load()
    print(f"NetMon {__version__}")
    print(f"  panel        http://{cfg['web_host']}:{cfg['web_port']}  user: {cfg['admin_user']}")
    print(f"  interfaces   {cfg.get('interfaces') or default_ifaces()} (auto if empty)")
    print(f"  iptables     v4={'yes' if ipt.available('4') else 'no'} v6={'yes' if ipt.available('6') else 'no'}")
    print(f"  geoip        {geoip.load(cfg['geoip_path'])} ranges ({cfg['geoip_path']})")
    print(f"  database     {cfg['db_path']}")
    print(f"  blocks       {len(cfg.get('blocks', []))} ports/ranges, {len(cfg.get('ip_blocks', []))} IPs")
    print(f"  whitelist    {', '.join(cfg.get('whitelist', [])) or '—'}")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "run"
    if cmd == "run":
        from . import server
        server.main()
    elif cmd == "passwd":
        _passwd()
    elif cmd == "status":
        _status()
    elif cmd == "geoip-update":
        from . import geoip
        cfg = config.load()
        print("Downloading the country database (about 3-6 MB compressed)…", flush=True)
        r = geoip.update(cfg["geoip_path"], ipv6="--no-ipv6" not in argv)
        if not r:
            sys.exit("✗ every mirror failed. Download the CSV on another machine and run:\n"
                     "  sudo netmon geoip-import geo-whois-asn-country-ipv4-num.csv [ipv6.csv]")
        print(f"✓ {r[0]} IPv4 + {r[1]} IPv6 ranges → {cfg['geoip_path']} "
              f"({os.path.getsize(cfg['geoip_path']) // 1024} KB). Restart: systemctl restart netmon")
    elif cmd == "geoip-import":
        from . import geoip
        if len(argv) < 2:
            sys.exit("usage: netmon geoip-import <ipv4-num.csv> [ipv6-num.csv]")
        cfg = config.load()
        r = geoip.build(argv[1], argv[2] if len(argv) > 2 else None, cfg["geoip_path"])
        print(f"✓ {r[0]} IPv4 + {r[1]} IPv6 ranges → {cfg['geoip_path']}. Restart: systemctl restart netmon")
    elif cmd == "fw-apply":
        from . import firewall
        cfg = config.load()
        firewall.open_panel_port(int(cfg.get("web_port", 8080)))
        print(firewall.apply(cfg)[1])
    elif cmd == "unblock-all":
        from . import firewall

        def mut(c):
            c["blocks"], c["allows"], c["ip_blocks"] = [], [], []
            c["ratelimit_enabled"] = False
        cfg, _ = config.update(mut)
        print("All NetMon blocks removed:", firewall.apply(cfg)[1])
    elif cmd == "teardown":
        from . import firewall
        from .accounting import Accounting
        firewall.teardown()
        Accounting(config.load(), []).teardown()
        print("NetMon chains removed")
    elif cmd in ("-v", "--version", "version"):
        print(__version__)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
