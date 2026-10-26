"""Well-known ports, grouped by what they're used for."""
import socket

# (category, [(port, proto, service, description)])   proto: tcp | udp | both
CATALOG = [
    ("Remote access", [
        (22, "tcp", "SSH", "Secure shell, server management"),
        (23, "tcp", "Telnet", "Unencrypted remote shell"),
        (3389, "both", "RDP", "Windows remote desktop"),
        (5900, "tcp", "VNC", "Remote desktop / screen sharing"),
        (10000, "tcp", "Webmin", "Web-based server admin"),
        (9090, "tcp", "Cockpit", "Web-based server admin"),
    ]),
    ("Web", [
        (80, "tcp", "HTTP", "Websites, plain"),
        (443, "both", "HTTPS", "Websites over TLS, HTTP/3 on UDP"),
        (8080, "tcp", "HTTP alt", "Alternate web / this panel"),
        (8443, "tcp", "HTTPS alt", "Alternate TLS web"),
        (8880, "tcp", "HTTP alt", "Alternate web (CDN-friendly)"),
        (2053, "tcp", "HTTPS alt", "Alternate TLS (CDN-friendly)"),
        (2083, "tcp", "HTTPS alt", "Alternate TLS (CDN-friendly)"),
        (2087, "tcp", "HTTPS alt", "Alternate TLS (CDN-friendly)"),
        (2096, "tcp", "HTTPS alt", "Alternate TLS (CDN-friendly)"),
    ]),
    ("VPN and tunnels", [
        (1194, "udp", "OpenVPN", "OpenVPN default"),
        (51820, "udp", "WireGuard", "WireGuard default"),
        (500, "udp", "IKE", "IPsec key exchange"),
        (4500, "udp", "IPsec NAT-T", "IPsec through NAT"),
        (1701, "udp", "L2TP", "Layer-2 tunnelling"),
        (1723, "tcp", "PPTP", "Legacy VPN"),
        (8388, "both", "Shadowsocks", "Shadowsocks default"),
        (2408, "udp", "WARP", "Cloudflare WARP"),
    ]),
    ("Proxy", [
        (1080, "tcp", "SOCKS", "SOCKS proxy"),
        (3128, "tcp", "Squid", "HTTP proxy"),
        (8118, "tcp", "Privoxy", "HTTP proxy"),
        (9050, "tcp", "Tor", "Tor SOCKS"),
    ]),
    ("Mail", [
        (25, "tcp", "SMTP", "Mail relay between servers"),
        (465, "tcp", "SMTPS", "Mail submission over TLS"),
        (587, "tcp", "Submission", "Mail submission"),
        (110, "tcp", "POP3", "Mail download"),
        (995, "tcp", "POP3S", "Mail download over TLS"),
        (143, "tcp", "IMAP", "Mailbox access"),
        (993, "tcp", "IMAPS", "Mailbox access over TLS"),
    ]),
    ("DNS and network services", [
        (53, "both", "DNS", "Name resolution"),
        (853, "tcp", "DNS over TLS", "Encrypted DNS"),
        (123, "udp", "NTP", "Time sync"),
        (67, "udp", "DHCP", "Address assignment"),
        (161, "udp", "SNMP", "Device monitoring"),
        (5353, "udp", "mDNS", "Local discovery"),
    ]),
    ("File transfer and sharing", [
        (21, "tcp", "FTP", "File transfer"),
        (69, "udp", "TFTP", "Trivial file transfer"),
        (139, "tcp", "NetBIOS", "Windows file sharing (legacy)"),
        (445, "tcp", "SMB", "Windows file sharing"),
        (2049, "both", "NFS", "Network file system"),
        (873, "tcp", "rsync", "File sync"),
    ]),
    ("Databases and caches", [
        (3306, "tcp", "MySQL", "MySQL / MariaDB"),
        (5432, "tcp", "PostgreSQL", "PostgreSQL"),
        (1433, "tcp", "MSSQL", "Microsoft SQL Server"),
        (27017, "tcp", "MongoDB", "MongoDB"),
        (6379, "tcp", "Redis", "Redis"),
        (11211, "both", "Memcached", "Memcached"),
        (9200, "tcp", "Elasticsearch", "Elasticsearch HTTP"),
    ]),
    ("Messaging and queues", [
        (1883, "tcp", "MQTT", "IoT messaging"),
        (5672, "tcp", "AMQP", "RabbitMQ"),
        (9092, "tcp", "Kafka", "Kafka broker"),
        (6667, "tcp", "IRC", "Chat"),
    ]),
    ("Containers and monitoring", [
        (2375, "tcp", "Docker API", "Docker, unencrypted"),
        (2376, "tcp", "Docker TLS", "Docker over TLS"),
        (6443, "tcp", "Kubernetes", "Kubernetes API"),
        (10250, "tcp", "Kubelet", "Kubernetes node agent"),
        (3000, "tcp", "Grafana", "Dashboards"),
        (9100, "tcp", "Node exporter", "Prometheus metrics"),
        (19999, "tcp", "Netdata", "Monitoring"),
    ]),
]

_KNOWN = {}
for _cat, _items in CATALOG:
    for _p, _proto, _svc, _desc in _items:
        _KNOWN.setdefault(_p, _svc)

_cache = {}


def service(port, proto=6):
    """Human name for a port."""
    if port in _KNOWN:
        return _KNOWN[port]
    key = (port, proto)
    if key not in _cache:
        try:
            _cache[key] = socket.getservbyport(port, "udp" if proto == 17 else "tcp")
        except (OSError, OverflowError):
            _cache[key] = ""
    return _cache[key]
