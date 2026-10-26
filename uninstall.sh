#!/usr/bin/env bash
# Remove NetMon and every iptables rule it added.   sudo bash uninstall.sh
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root: sudo bash uninstall.sh"; exit 1; }

echo "Stopping service…"
systemctl disable --now netmon.service netmon-geoip.timer >/dev/null 2>&1 || true
rm -f /etc/systemd/system/netmon.service /etc/systemd/system/netmon-geoip.service /etc/systemd/system/netmon-geoip.timer
systemctl daemon-reload 2>/dev/null || true

echo "Removing NetMon firewall chains (your own rules are untouched)…"
if [ -d /opt/netmon/netmon ]; then
  (cd /opt/netmon && python3 -m netmon teardown) || true
fi

read -rp "Also delete history, settings and blocks (/var/lib/netmon, /etc/netmon)? [y/N] " a </dev/tty || a=n
if [ "${a,,}" = "y" ]; then
  rm -rf /var/lib/netmon /etc/netmon
  echo "Data and settings removed."
else
  echo "Kept /var/lib/netmon and /etc/netmon — reinstalling picks them up again."
fi
rm -rf /opt/netmon /usr/local/bin/netmon
echo "NetMon uninstalled."
