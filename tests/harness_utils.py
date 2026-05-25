"""
Shared utilities for the StopTrading test harness.
Import this in every test module.
"""
import sys, os, json, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── ANSI colours ──────────────────────────────────────────────────────────────
PASS  = '\033[92m✓\033[0m'
FAIL  = '\033[91m✗\033[0m'
WARN  = '\033[93m⚠\033[0m'
HEAD  = '\033[94m'
BOLD  = '\033[1m'
RESET = '\033[0m'


class Suite:
    """Lightweight test suite — tracks results, prints formatted output."""

    def __init__(self, name: str):
        self.name    = name
        self.results = []   # [(label, ok, detail)]
        print(f'\n{BOLD}{HEAD}{"═"*60}')
        print(f'  {name}')
        print(f'{"═"*60}{RESET}')

    def section(self, title: str):
        pad = max(0, 52 - len(title))
        print(f'\n{HEAD}── {title} {"─"*pad}{RESET}')

    def check(self, label: str, ok: bool, detail: str = '') -> bool:
        mark = PASS if ok else FAIL
        line = f'  {mark}  {label}'
        if detail:
            line += f'  \033[2m({detail})\033[0m'
        print(line)
        self.results.append((label, ok, detail))
        return ok

    def warn(self, label: str, detail: str = ''):
        line = f'  {WARN}  {label}'
        if detail:
            line += f'  \033[2m({detail})\033[0m'
        print(line)
        self.results.append((label, None, detail))   # None = warning, not counted

    def summary(self) -> tuple:
        passed  = sum(1 for _, ok, _ in self.results if ok is True)
        failed  = sum(1 for _, ok, _ in self.results if ok is False)
        warned  = sum(1 for _, ok, _ in self.results if ok is None)
        total   = passed + failed
        color   = '\033[92m' if failed == 0 else '\033[91m'
        print(f'\n{color}  {passed}/{total} passed', end='')
        if failed:
            print(f'  ·  {failed} failed', end='')
        if warned:
            print(f'  ·  {warned} warnings', end='')
        print(f'\033[0m\n')
        return passed, failed, total


class TempAuth:
    """Context manager: patches modules.auth to use a throw-away .auth file."""

    def __init__(self):
        self._dir  = None
        self._path = None

    def __enter__(self):
        import modules.auth as a
        self._dir      = tempfile.mkdtemp(prefix='st_test_')
        self._path     = os.path.join(self._dir, '.auth_test')
        self._orig_path   = a.AUTH_FILE
        self._orig_creds  = a._creds
        a.AUTH_FILE = self._path
        a._creds    = None
        return a

    def __exit__(self, *_):
        import modules.auth as a
        a.AUTH_FILE = self._orig_path
        a._creds    = self._orig_creds
        shutil.rmtree(self._dir, ignore_errors=True)


def load_config_copy() -> dict:
    """Return a copy of the current config — safe to mutate in tests."""
    from config import load_config
    return dict(load_config())
