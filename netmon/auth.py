"""
Auth: PBKDF2-SHA256 passwords (stdlib), HMAC-signed session tokens that
survive restarts and are invalidated by a password change, and per-IP lockout.
"""
import base64
import hashlib
import hmac
import os
import threading
import time

ITER = 240_000


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITER)
    return f"pbkdf2_sha256${ITER}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, it, salt, h = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), h)
    except (ValueError, AttributeError):
        return False


def _key(cfg):
    return (cfg.get("secret_key", "") + "|" + cfg.get("pw_hash", "")[-24:]).encode()


def make_token(cfg, user):
    exp = int(time.time()) + int(cfg.get("session_ttl_seconds", 7 * 86400))
    body = f"{user}|{exp}|{os.urandom(6).hex()}"
    sig = hmac.new(_key(cfg), body.encode(), hashlib.sha256).hexdigest()[:40]
    return base64.urlsafe_b64encode(f"{body}|{sig}".encode()).decode().rstrip("=")


def check_token(cfg, tok):
    if not tok or len(tok) > 400:
        return None
    try:
        raw = base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)).decode()
        user, exp, nonce, sig = raw.split("|")
    except (ValueError, UnicodeDecodeError):
        return None
    body = f"{user}|{exp}|{nonce}"
    good = hmac.new(_key(cfg), body.encode(), hashlib.sha256).hexdigest()[:40]
    if not hmac.compare_digest(good, sig):
        return None
    if int(exp) < time.time() or user != cfg.get("admin_user"):
        return None
    return user


class Lockout:
    def __init__(self, max_fails=5, window=600):
        self.max, self.window = max_fails, window
        self.fails = {}
        self.lock = threading.Lock()

    def locked(self, ip):
        with self.lock:
            n, first = self.fails.get(ip, (0, 0))
            if time.time() - first > self.window:
                self.fails.pop(ip, None)
                return False
            return n >= self.max

    def fail(self, ip):
        with self.lock:
            n, first = self.fails.get(ip, (0, time.time()))
            if time.time() - first > self.window:
                n, first = 0, time.time()
            self.fails[ip] = (n + 1, first)
            if len(self.fails) > 5000:
                self.fails.clear()

    def clear(self, ip):
        with self.lock:
            self.fails.pop(ip, None)
