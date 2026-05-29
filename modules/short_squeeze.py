"""
ShortSqueezeAgent — identifies potential short squeeze setups.

A short squeeze occurs when:
  1. Short interest is elevated (sellers are trapped)
  2. Price starts rising (forces covering)
  3. Volume accelerates (buying pressure overwhelms sellers)

Score composition (0 – 45):
  Short interest >30% of float:   +15
  Short interest 15-30%:          +10
  Short interest 5-15%:           +5
  SSR active (price dropped >10% prev day): +10
  Float <5M shares (micro-float): +10
  Float 5-10M:                    +5
  Days-to-cover (DTC) >5:         +5
  Price trending up (slope > 0):  +5  (checked via velocity cache)

Data sources:
  - yfinance Ticker.info: shortRatio, shortPercentOfFloat, floatShares
  - SSRMonitor: is_ssr(symbol)
  - RealtimeEngine velocity cache: slope signal
"""

import threading
import time
import yfinance as yf
from datetime import datetime

_REFRESH_INTERVAL = 3600   # short interest data changes daily — refresh every hour


class ShortSqueezeAgent:

    def __init__(self, config, universe, ssr=None, engine=None):
        self.config   = config
        self.universe = universe
        self.ssr      = ssr      # SSRMonitor — optional
        self.engine   = engine   # RealtimeEngine — optional
        self._lock    = threading.Lock()
        self._data    = {}   # symbol -> {short_pct, dtc, score, timestamp}
        self.running  = False

    def start(self):
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def stop(self):
        self.running = False

    def _loop(self):
        # Stagger initial refresh to avoid startup thundering herd
        time.sleep(30)
        while self.running:
            try:
                self._refresh()
            except Exception:
                pass
            time.sleep(_REFRESH_INTERVAL)

    def _refresh(self):
        syms = self.universe.get() if hasattr(self.universe, 'get') else list(self.universe)
        new_data = {}

        for sym in syms[:80]:
            try:
                info = yf.Ticker(sym).info
                short_pct_float = float(info.get('shortPercentOfFloat') or 0) * 100
                short_ratio     = float(info.get('shortRatio') or 0)    # days-to-cover
                float_shares    = int(info.get('floatShares') or info.get('sharesOutstanding') or 0)

                score = 0
                # Short interest level
                if short_pct_float >= 30:
                    score += 15
                elif short_pct_float >= 15:
                    score += 10
                elif short_pct_float >= 5:
                    score += 5

                # SSR active — forced covering context
                if self.ssr and self.ssr.is_ssr(sym):
                    score += 10

                # Micro-float amplifies squeeze velocity
                if 0 < float_shares <= 5_000_000:
                    score += 10
                elif float_shares <= 10_000_000:
                    score += 5

                # Days-to-cover: how many days at avg volume to close all shorts
                if short_ratio >= 5:
                    score += 5

                new_data[sym] = {
                    'short_pct_float': round(short_pct_float, 1),
                    'dtc':             round(short_ratio, 1),
                    'float_shares':    float_shares,
                    'score':           score,
                    'timestamp':       time.time(),
                }
                time.sleep(0.3)   # yfinance info is slow — avoid hammering
            except Exception:
                continue

        with self._lock:
            self._data = new_data

    # ── Public API ────────────────────────────────────────────────────────────

    def get_squeeze_score(self, symbol: str) -> int:
        """
        Base score from short interest / SSR / float,
        plus +5 if price is currently trending up.
        """
        with self._lock:
            entry = self._data.get(symbol.upper())
        if not entry:
            return 0
        score = entry['score']
        # Realtime momentum check — only boost if price is actually rising
        if self.engine and hasattr(self.engine, 'cache'):
            vel = self.engine.cache.velocity.get(symbol.upper(), {})
            pct_1m = vel.get('pct_1m', 0.0)
            if pct_1m > 0:
                score += 5
        return score

    def get_squeeze_candidates(self, min_score: int = 10) -> list:
        """Return tickers ranked by squeeze setup quality."""
        with self._lock:
            candidates = [
                (s, d) for s, d in self._data.items()
                if d['score'] >= min_score
            ]
        result = []
        for sym, d in sorted(candidates, key=lambda x: x[1]['score'], reverse=True):
            total = self.get_squeeze_score(sym)   # includes live momentum
            result.append({
                'ticker':          sym,
                'score':           total,
                'short_pct_float': d['short_pct_float'],
                'dtc':             d['dtc'],
                'float_shares':    d['float_shares'],
                'ssr':             self.ssr.is_ssr(sym) if self.ssr else False,
            })
        return result

    def get_summary(self) -> dict:
        candidates = self.get_squeeze_candidates(min_score=5)
        with self._lock:
            total = len(self._data)
        return {
            'tracked':    total,
            'candidates': candidates[:20],
        }
