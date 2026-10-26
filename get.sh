#!/usr/bin/env bash
# NetMon one-line installer.
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/ByeCoder/netmon/main/get.sh)"
# Downloads the repository and runs install.sh. Same env vars as install.sh
# (NETMON_USER, NETMON_PASS, NETMON_PORT) work here too.
set -euo pipefail

REPO="${NETMON_REPO:-ByeCoder/netmon}"
BRANCH="${NETMON_BRANCH:-main}"

[ "$(id -u)" = 0 ] || { echo "Run as root:  sudo bash -c \"\$(curl -fsSL .../get.sh)\""; exit 1; }
command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "Downloading NetMon from github.com/$REPO ($BRANCH)…"
for url in "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" \
           "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"; do
  if curl -fsSL --connect-timeout 15 --max-time 120 "$url" -o "$TMP/netmon.tgz"; then break; fi
done
[ -s "$TMP/netmon.tgz" ] || { echo "Download failed. Check the repo name or your connection."; exit 1; }
tar xzf "$TMP/netmon.tgz" -C "$TMP"
cd "$TMP"/*/
bash install.sh </dev/tty
