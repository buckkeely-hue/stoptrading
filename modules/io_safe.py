"""Crash-safe JSON persistence.

All state files (config, paper trades, autopilot, accounting, notify log) are
written via atomic_write_json so a crash / SIGKILL mid-write can never leave a
truncated file. We write to a temp file in the same directory, fsync it, then
os.replace() onto the target — replace is atomic on POSIX.
"""
import json
import os
import tempfile
import time


def append_jsonl(path, obj, max_bytes=None):
    """Crash-safe single-line append to a JSONL recorder.

    Serializes `obj` to one JSON line, appends it, then flush+fsync so a crash / SIGKILL can
    never leave a torn partial line that would break the readers (model_eval, adaptive_policy).
    If `max_bytes` is set and the file already exceeds it, the live file is first rotated to a
    timestamped archive (atomic rename) so the active file stays bounded — archives are kept on
    disk as cold history. Returns True on success, False on any failure (never raises: recording
    must never break trading)."""
    try:
        line = json.dumps(obj) + '\n'
    except Exception:
        return False
    try:
        if max_bytes and os.path.exists(path) and os.path.getsize(path) >= max_bytes:
            try:
                os.replace(path, '%s.%s' % (path, time.strftime('%Y%m%d-%H%M%S')))
            except Exception:
                pass
        with open(path, 'a') as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception:
        return False


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
