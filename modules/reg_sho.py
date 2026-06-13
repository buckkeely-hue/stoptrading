"""
RegSHOMonitor — daily Reg SHO Threshold Securities list (free, no key).

Threshold securities have persistent failures-to-deliver and are hard to borrow — a
short-pressure / squeeze-prone signal. The exchanges publish the list each trading
morning; Nasdaq Trader hosts the pipe-delimited files for every market center.

Exposes:
  is_threshold(symbol) -> bool      # on today's (most recent) threshold list
  count() -> int
  get_summary() -> dict

Wired as a modest squeeze/borrow tailwind bonus in the buy scoring (capped, config
`reg_sho_bonus`), and a flag other modules can read. Fails safe to False on any error.
"""

import threading
import time
import requests
from datetime import datetime, timedelta

HEADERS = {'User-Agent': 'StopTrading buckkeely@gmail.com'}

# Nasdaq Trader hosts daily Reg SHO threshold files per market center (free, pipe-delimited):
#   nasdaqth<YYYYMMDD>.txt  (Nasdaq)   nyseth<YYYYMMDD>.txt (NYSE)
#   nasdaqbxth<YYYYMMDD>.txt (BX)      psxth<YYYYMMDD>.txt  (PSX)
_FILES = ['nasdaqth', 'nyseth', 'nasdaqbxth', 'psxth']
_BASE  = 'https://www.nasdaqtrader.com/dynamic/symdir/regsho/'


class RegSHOMonitor:
    def __init__(self, config=None):
        self.config       = config or {}
        self._lock        = threading.Lock()
        self._symbols     = set()
        self.running      = False
        self.last_refresh = None
        self.as_of        = None

    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self.refresh()
            except Exception:
                pass
            time.sleep(3 * 3600)   # published each morning; a few checks/day is plenty

    def refresh(self):
        # Today's file may not be posted pre-open, so walk back a few business days.
        for back in range(0, 5):
            d = datetime.utcnow().date() - timedelta(days=back)
            ymd = d.strftime('%Y%m%d')
            day_syms = set()
            for f in _FILES:
                try:
                    r = requests.get('%s%s%s.txt' % (_BASE, f, ymd), headers=HEADERS, timeout=8)
                    if r.status_code != 200 or '|' not in r.text:
                        continue
                    for line in r.text.splitlines()[1:]:        # skip header row
                        sym = line.split('|')[0].strip().upper()
                        if sym and sym.isupper() and 1 <= len(sym) <= 6 and sym.replace('.', '').isalnum():
                            day_syms.add(sym)
                except Exception:
                    continue
            if day_syms:
                with self._lock:
                    self._symbols = day_syms
                    self.last_refresh = datetime.now()
                    self.as_of = ymd
                return

    def is_threshold(self, symbol):
        if not symbol:
            return False
        with self._lock:
            return symbol.upper().strip() in self._symbols

    def count(self):
        with self._lock:
            return len(self._symbols)

    def get_summary(self):
        with self._lock:
            return {'running': self.running, 'count': len(self._symbols), 'as_of': self.as_of,
                    'last_refresh': self.last_refresh.isoformat() if self.last_refresh else None}
