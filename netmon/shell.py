"""Small, safe subprocess helpers (argument lists only, never a shell)."""
import shutil
import subprocess

_have = {}


def have(cmd):
    if cmd not in _have:
        _have[cmd] = shutil.which(cmd) is not None
    return _have[cmd]


def run(args, timeout=15, stdin=None):
    """Run a command. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(args, input=stdin, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as e:
        return 126, "", str(e)
