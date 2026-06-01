"""Crash-safe JSON persistence.

All state files (config, paper trades, autopilot, accounting, notify log) are
written via atomic_write_json so a crash / SIGKILL mid-write can never leave a
truncated file. We write to a temp file in the same directory, fsync it, then
os.replace() onto the target — replace is atomic on POSIX.
"""
import json
import os
import tempfile


def atomic_write_json(path, data, indent=2):
    """Write `data` as JSON to `path` atomically. Returns True on success."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix='.tmp-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
