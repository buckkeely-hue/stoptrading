"""
SSR (Short Sale Restriction / Rule 201) Monitor.

When a stock drops >10% from its prior close, the SEC's Rule 201 circuit
breaker activates for the rest of that day and the next, preventing short
sellers from hitting the bid (they can only short on an uptick).

For penny stocks already under pressure, SSR removes the bear's main weapon
and creates asymmetric bounce setups. SSR stocks are BUY candidates, not sells.

Data source: Nasdaq publishes daily SSR files (refreshed intraday).
  https://www.nasdaqtrader.com/dynamic/symdir/shortsalerestrictions.txt
"""

import threading
import time
import requests
from datetime import datetime, date

_SSR_URL = 'https://www.nasdaqtrader.com/dynamic/symdir/shortsalerestrictions.txt'
_HEADERS  = {'User-Agent': 'StopTrading/1.0 buckkeely@gmail.com'}


class SSRMonitor:
    """
    Downloads the Nasdaq SSR list once at startup, then refreshes every 30 min.
    Stocks on SSR get a +10 scoring bonus in AutoPilot buy logic.
    """

    def __init__(self):
        self._lock      = threading.Lock()
        self._ssr_set   = set()
        self.running    = False
        self.last_fetch = None
        self.count      = 0

    def start(self):
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def stop(self):
        self.running = False

    def _loop(self):
        self._fetch()
        while self.running:
            time.sleep(1800)   # refresh every 30 minutes
            try:
                self._fetch()
            except Exception:
                pass

    def _fetch(self):
        try:
            r = requests.get(_SSR_URL, headers=_HEADERS, timeout=12)
            if r.status_code != 200:
                return
            symbols = set()
            for line in r.text.splitlines():
                line = line.strip()
                if not line or line.startswith('#') or line.lower().startswith('symbol'):
                    continue
                parts = line.split('|')
                if parts:
                    sym = parts[0].strip().upper()
                    if sym and '.' not in sym and '-' not in sym:
                        symbols.add(sym)
            with self._lock:
                self._ssr_set   = symbols
                self.last_fetch = datetime.now().isoformat()
                self.count      = len(symbols)
        except Exception:
            pass

    def is_ssr(self, symbol):
        with self._lock:
            return symbol.upper() in self._ssr_set

    def get_score(self, symbol):
        """SSR active = shorts can't hit the bid = asymmetric bounce setup → +10."""
        return 10 if self.is_ssr(symbol) else 0

    def status(self):
        with self._lock:
            return {
                'count':      self.count,
                'last_fetch': self.last_fetch,
                'sample':     sorted(self._ssr_set)[:10],
            }
