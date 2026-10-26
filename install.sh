#!/usr/bin/env bash
# NetMon installer — Ubuntu / Debian.   sudo bash install.sh
# Non-interactive:  sudo NETMON_USER=admin NETMON_PASS='secret123' bash install.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=/opt/netmon
CFG=/etc/netmon
VAR=/var/lib/netmon
PORT="${NETMON_PORT:-8080}"

c()   { printf "\033[%sm%s\033[0m" "$1" "$2"; }
ok()  { echo " $(c '1;32' '✓') $1"; }
inf() { echo " $(c '1;36' '›') $1"; }
warn(){ echo " $(c '1;33' '!') $1"; }
die() { echo " $(c '1;31' '✗') $1" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "Run as root:  sudo bash install.sh"
[ -d "$SRC/netmon" ] || die "Run this from the unpacked netmon folder"

echo
echo "$(c '1;36' '   ┌─────────────────────────────────────┐')"
echo "$(c '1;36' '   │')  $(c '1;37' 'NetMon')  traffic monitor & firewall  $(c '1;36' '│')"
echo "$(c '1;36' '   └─────────────────────────────────────┘')"
echo

# ---------------------------------------------------------------- apt lock
APT_LOCKS="/var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock /var/cache/apt/archives/lock"

lock_holders() {  # PIDs that have an apt/dpkg lock file open (no fuser/lsof needed)
  local pid fd t
  for pid in /proc/[0-9]*; do
    pid=${pid#/proc/}
    [ "$pid" = "$$" ] && continue
    for fd in /proc/"$pid"/fd/*; do
      t=$(readlink "$fd" 2>/dev/null) || continue
      case " $APT_LOCKS " in *" $t "*) echo "$pid"; break ;; esac
    done
  done | sort -u
}

free_apt_lock() {
  local pids i
  pids=$(lock_holders)
  [ -z "$pids" ] && return 0
  warn "apt is locked by: $(for p in $pids; do printf '%s(%s) ' "$p" "$(cat /proc/$p/comm 2>/dev/null)"; done)"
  # give a running install a short chance to finish cleanly
  for i in $(seq 1 15); do
    sleep 2
    pids=$(lock_holders); [ -z "$pids" ] && { ok "apt lock released"; return 0; }
  done
  warn "Still locked — stopping automatic updates and killing the lock holders"
  systemctl stop unattended-upgrades.service apt-daily.service apt-daily-upgrade.service >/dev/null 2>&1 || true
  pkill -TERM -f unattended-upgr 2>/dev/null || true
  for p in $(lock_holders); do kill -TERM "$p" 2>/dev/null || true; done
  sleep 5
  for p in $(lock_holders); do kill -KILL "$p" 2>/dev/null || true; done
  sleep 1
  if [ -n "$(lock_holders)" ]; then
    die "Couldn't free the apt lock. Reboot the server and run the installer again."
  fi
  rm -f $APT_LOCKS
  inf "Repairing any half-finished package installs…"
  dpkg --configure -a >/dev/null 2>&1 || warn "dpkg --configure -a reported problems"
  ok "apt lock freed"
}

# ---------------------------------------------------------------- packages
if command -v apt-get >/dev/null 2>&1; then
  inf "Installing packages (python3, iptables, conntrack, iproute2)…"
  export DEBIAN_FRONTEND=noninteractive
  NEED=""
  for p in python3 iptables conntrack ip curl; do command -v "$p" >/dev/null 2>&1 || NEED=1; done
  if [ -z "$NEED" ]; then
    ok "Everything needed is already installed, skipping apt"
  else
    free_apt_lock
    APT="apt-get -o DPkg::Lock::Timeout=60 -y -qq"
    $APT update || warn "apt update had errors, continuing"
    $APT install python3 iptables conntrack iproute2 curl ca-certificates >/dev/null \
      || $APT install python3 iptables iproute2 curl ca-certificates >/dev/null \
      || die "Package install failed — see the message above"
  fi
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 iptables conntrack-tools iproute curl >/dev/null || die "Package install failed"
else
  warn "Unknown package manager — make sure python3, iptables and conntrack are installed"
fi
command -v python3 >/dev/null || die "python3 is missing"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' || die "Python 3.7+ required"
command -v iptables >/dev/null || warn "iptables not found — firewall and exact counters will be off"
modprobe nf_conntrack 2>/dev/null || true
ok "Packages ready (no pip packages needed)"

# ---------------------------------------------------------------- upgrade from 1.x
if systemctl list-unit-files 2>/dev/null | grep -q '^netmon-web.service'; then
  inf "Removing NetMon 1.x services…"
  systemctl disable --now netmon-web.service netmon-collector.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/netmon-web.service /etc/systemd/system/netmon-collector.service
fi
for spec in "mangle PREROUTING NETMON_IN" "mangle POSTROUTING NETMON_OUT" "filter INPUT NETMON_FW" "filter OUTPUT NETMON_FWO"; do
  set -- $spec
  if iptables -t "$1" -n -L "$3" >/dev/null 2>&1; then
    while iptables -t "$1" -D "$2" -j "$3" 2>/dev/null; do :; done
    iptables -t "$1" -F "$3" 2>/dev/null || true
    iptables -t "$1" -X "$3" 2>/dev/null || true
  fi
done
systemctl stop netmon.service >/dev/null 2>&1 || true

# ---------------------------------------------------------------- files
inf "Installing to $APP…"
mkdir -p "$APP" "$CFG" "$VAR"
rm -rf "$APP/netmon.new"
cp -r "$SRC/netmon" "$APP/netmon.new"
if [ -d "$APP/netmon/web/fonts" ]; then cp -n "$APP"/netmon/web/fonts/*.woff2 "$APP/netmon.new/web/fonts/" 2>/dev/null || true; fi
rm -rf "$APP/netmon"
mv "$APP/netmon.new" "$APP/netmon"
find "$APP" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
chmod -R go-w "$APP"
chmod 700 "$CFG" "$VAR"
cat > /usr/local/bin/netmon <<EOF
#!/bin/sh
cd $APP && exec python3 -m netmon "\$@"
EOF
chmod 755 /usr/local/bin/netmon
ok "Files installed, command 'netmon' available"

# ---------------------------------------------------------------- config + admin
NEW=0
[ -f "$CFG/config.json" ] || NEW=1
PYTHONPATH="$APP" python3 - "$PORT" <<'PY'
import os, sys
from netmon import config
cfg = config.load()
if not cfg.get("secret_key"):
    cfg["secret_key"] = os.urandom(32).hex()
if not os.path.exists(config.CONFIG_PATH):
    cfg["web_port"] = int(sys.argv[1])
config.save(cfg)
PY
HAS_PW=$(PYTHONPATH="$APP" python3 -c "from netmon import config; print(1 if config.load().get('pw_hash') else 0)")
echo
if [ "$HAS_PW" = 1 ] && [ -z "${NETMON_PASS:-}" ]; then
  read -rp " $(c '1;36' '›') An admin account already exists. Keep it? [Y/n] " KEEP </dev/tty || KEEP=y
  case "${KEEP,,}" in n|no) netmon passwd </dev/tty ;; *) ok "Keeping existing admin account" ;; esac
else
  inf "Create the admin login for the web panel"
  if [ -n "${NETMON_PASS:-}" ]; then netmon passwd; else netmon passwd </dev/tty; fi
fi

# ---------------------------------------------------------------- whitelist (first install)
if [ "$NEW" = 1 ] && [ -t 0 -o -e /dev/tty ] && [ -z "${NETMON_PASS:-}" ]; then
  echo
  ME=$(echo "${SSH_CLIENT:-}" | awk '{print $1}')
  inf "Optional: IPs that must never be blocked (your tunnel server, your own IP)."
  [ -n "$ME" ] && echo "    You are connected from $(c '1;37' "$ME")"
  read -rp "    IPs/CIDRs, comma separated (Enter to skip): " WL </dev/tty || WL=""
  read -rp "    Tunnel ports to exempt from flood protection (Enter to skip): " TP </dev/tty || TP=""
  PYTHONPATH="$APP" python3 - "$WL" "$TP" <<'PY'
import ipaddress, sys
from netmon import config
wl, tp = sys.argv[1], sys.argv[2]
def mut(c):
    ips = []
    for x in wl.split(","):
        x = x.strip()
        try:
            ipaddress.ip_network(x, strict=False); ips.append(x)
        except ValueError:
            pass
    c["whitelist"] = sorted(set(c.get("whitelist", []) + ips))
    ports = [int(p) for p in tp.replace(" ", ",").split(",") if p.strip().isdigit() and 0 < int(p) < 65536]
    c["tunnel_ports"] = sorted(set(c.get("tunnel_ports", []) + ports))
config.update(mut)
PY
fi

# ---------------------------------------------------------------- fonts (optional, offline afterwards)
FONTDIR="$APP/netmon/web/fonts"
mkdir -p "$FONTDIR"
cat > "$VAR/fonts.sh" <<'FONTS'
#!/usr/bin/env bash
# downloads the panel fonts once; the panel uses system fonts until they arrive
D="$1"
fetch() {  # fetch <dest> <npm path>
  [ -s "$1" ] && return 0
  for base in https://cdn.jsdelivr.net/npm https://fastly.jsdelivr.net/npm https://unpkg.com; do
    if curl -fsSL --connect-timeout 8 --max-time 30 "$base/$2" -o "$1.tmp" 2>/dev/null && [ -s "$1.tmp" ]; then
      mv "$1.tmp" "$1"; return 0
    fi
  done
  rm -f "$1.tmp"; return 1
}
fetch "$D/chakra-petch-700.woff2" "@fontsource/chakra-petch@5/files/chakra-petch-latin-700-normal.woff2" || { echo "font mirrors unreachable"; exit 1; }
for w in 500 600; do fetch "$D/chakra-petch-$w.woff2" "@fontsource/chakra-petch@5/files/chakra-petch-latin-$w-normal.woff2"; done
for w in 400 500 600; do fetch "$D/plex-sans-$w.woff2" "@fontsource/ibm-plex-sans@5/files/ibm-plex-sans-latin-$w-normal.woff2"; done
for w in 400 500; do fetch "$D/plex-mono-$w.woff2" "@fontsource/ibm-plex-mono@5/files/ibm-plex-mono-latin-$w-normal.woff2"; done
fetch "$D/flags.woff2" "country-flag-emoji-polyfill@0.1/dist/TwemojiCountryFlags.woff2"
echo "fonts: $(ls "$D"/*.woff2 2>/dev/null | wc -l)/9"
FONTS
chmod 700 "$VAR/fonts.sh"
if [ "$(ls "$FONTDIR"/*.woff2 2>/dev/null | wc -l)" -ge 9 ]; then
  ok "Fonts already present"
else
  setsid nohup bash "$VAR/fonts.sh" "$FONTDIR" >"$VAR/fonts.log" 2>&1 < /dev/null &
  inf "Fonts are downloading in the background (the panel uses system fonts until then)"
fi

# ---------------------------------------------------------------- systemd
inf "Installing the service…"
install -m 644 "$SRC/systemd/netmon.service" /etc/systemd/system/netmon.service
install -m 644 "$SRC/systemd/netmon-geoip.service" /etc/systemd/system/netmon-geoip.service
install -m 644 "$SRC/systemd/netmon-geoip.timer" /etc/systemd/system/netmon-geoip.timer
systemctl daemon-reload
systemctl enable --now netmon.service >/dev/null 2>&1
systemctl enable --now netmon-geoip.timer >/dev/null 2>&1 || true
PORT=$(PYTHONPATH="$APP" python3 -c "from netmon import config; print(config.load()['web_port'])")

# country database: downloaded in the background so the install never waits on it
if [ -s "$VAR/geoip.bin" ] && [ -z "$(find "$VAR/geoip.bin" -mtime +25 2>/dev/null)" ]; then
  ok "Country database is recent, keeping it"
else
  systemctl start --no-block netmon-geoip.service >/dev/null 2>&1 || true
  inf "Country database is downloading in the background (flags appear when it's done)"
  echo "    progress:  journalctl -u netmon-geoip -f      manual:  sudo netmon geoip-update"
fi

# wait for the panel to answer
for i in $(seq 1 20); do
  curl -fsS -o /dev/null "http://127.0.0.1:$PORT/login" 2>/dev/null && break
  sleep 0.5
done
if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/login" 2>/dev/null; then
  ok "NetMon is running"
else
  warn "The service didn't answer yet. Check:  journalctl -u netmon -n 50"
fi

# ---------------------------------------------------------------- done
IP=$(curl -4 -fsS --max-time 4 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
USER_NAME=$(PYTHONPATH="$APP" python3 -c "from netmon import config; print(config.load()['admin_user'])")
echo
echo "$(c '1;32' '   ══════════════  Ready  ══════════════')"
echo
echo "   Panel     $(c '1;36' "http://${IP:-<server-ip>}:$PORT")"
echo "   Login     $(c '1;37' "$USER_NAME")"
echo "   Port $PORT is open in the firewall; nothing else was closed."
echo
echo "   netmon status          what is being measured"
echo "   netmon passwd          change the admin password"
echo "   netmon unblock-all     emergency: remove every block"
echo "   journalctl -u netmon   logs"
echo
warn "The panel controls your firewall and is reachable from the internet."
warn "Use a strong password, or put it behind HTTPS (nginx / Caddy)."
echo
