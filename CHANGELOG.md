# Changelog

## 2.0.0

Complete rewrite.

- No Flask: standard-library HTTP server; one systemd service instead of two (~25 MB RAM).
- Exact kernel counters per protocol (TCP, UDP, ICMP, GRE, ESP, SCTP, other) and per port,
  download and upload separated, with packets, requests (new connections) and peak speeds.
- IPv6 and forwarded / NAT tunnel traffic are counted.
- 5-second history for 24 h, hourly for 30 days; long-range views now include the last 24 h.
- Countries from the conntrack table, incoming and outgoing, with flags and rankings.
- Firewall: blocks run before NAT, never cut established connections or replies; single-port
  exceptions inside ranges; pause/resume rules; IP blocking; SSH switch that keeps open sessions.
- Well-known ports grouped by purpose with listening indicators.
- New panel: live 1-second scope, zoomable charts with exact read-outs, day/night themes, mobile layout.
- Installer: frees a stuck apt lock, downloads the country database in the background,
  upgrades 1.x in place.
