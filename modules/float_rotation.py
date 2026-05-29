"""
FloatRotationAgent — live float turnover tracker.

Float rotation = today's total volume / shares in float.
This metric identifies when a stock is being genuinely re-distributed
vs. thin-tape noise:

  0.0 – 0.1  (0-10% of float traded)  → low activity, no signal
  0.1 – 0.5  (10-50%)                 → elevated conviction (+5)
  0.5 – 1.0  (50-100%)                → high conviction (+12)
  1.0 – 1.5  (100-150%)               → float fully turned over (+18)
  >1.5       (>150%)                  → likely exhaustion / distribution (−10)

Float data sourced from the StockHarvester float cache (shared reference).
Volume data sourced from VelocityTracker cache, falling back to yfinance.
"""

import threading
import time
import yfinance as yf
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo('America/New_York')
except Exception:
    _ET = None


def rotation_score(ratio: float) -> int:
    if ratio <= 0:
        return 0
    if ratio < 0.1:
        return 0
    if ratio < 0.5:
        return 5
    if ratio < 1.0:
        return 12
    if ratio < 1.5:
        return 18
    return -10   # exhaustion penalty above 1.5×


class FloatRotationAgent:

    def __init__(self, universe, float_cache_ref: dict, engine=None):
        """
        universe:        DynamicUniverse or list
        float_cache_ref: direct reference to StockHarvester._float_cache
                         {symbol: (float_shares, timestamp)}
        engine:          RealtimeEngine for velocity cache reads
        """
        self.universe    = universe
        self._float_ref  = float_cache_ref
        self.engine      = engine
        self._lock       = threading.Lock()
        self._rotation   = {}   # symbol -> (ratio, today_vol, timestamp)
        self._date       = ''
        self.running     = False

    def start(self):
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                et = datetime.now(_ET) if _ET else datetime.now(timezone.utc) + timedelta(hours=-4)
                m  = et.hour * 60 + et.minute
                # Only compute during trading hours
                if 570 <= m <= 960:
                    self._refresh()
            except Exception:
                pass
            time.sleep(300)   # 5-minute cycle

    def _refresh(self):
        syms = self.universe.get() if hasattr(self.universe, 'get') else list(self.universe)
        today_str = datetime.now().strftime('%Y-%m-%d')
        new_rot = {}

        for sym in syms[:80]:
            try:
                fl_entry = self._float_ref.get(sym, (0, 0))
                float_shares = fl_entry[0] if fl_entry else 0
                if float_shares <= 0:
                    continue

                # Try velocity cache first (no network)
                today_vol = 0
                if self.engine and hasattr(self.engine, 'cache'):
                    vel = self.engine.cache.velocity.get(sym, {})
                    cached_vol = vel.get('today_volume', 0)
                    if cached_vol > 0:
                        today_vol = int(cached_vol)

                # Fall back to yfinance
                if today_vol == 0:
                    hist = yf.Ticker(sym).history(period='1d', interval='1d')
                    if not hist.empty:
                        today_vol = int(hist['Volume'].iloc[-1])

                if today_vol <= 0:
                    continue

                ratio = today_vol / float_shares
                new_rot[sym] = (round(ratio, 4), today_vol, time.time())
                time.sleep(0.1)
            except Exception:
                continue

        with self._lock:
            self._rotation = new_rot
            self._date = today_str

    # ── Public API ────────────────────────────────────────────────────────────

    def get_rotation(self, symbol: str) -> float:
        with self._lock:
            entry = self._rotation.get(symbol.upper())
        return entry[0] if entry else 0.0

    def get_rotation_score(self, symbol: str) -> int:
        return rotation_score(self.get_rotation(symbol))

    def get_high_rotation_tickers(self, min_ratio: float = 0.5) -> list:
        """Return tickers with float rotation above threshold, sorted by ratio."""
        today_str = datetime.now().strftime('%Y-%m-%d')
        with self._lock:
            if self._date != today_str:
                return []
            items = [(s, r[0], r[1]) for s, r in self._rotation.items() if r[0] >= min_ratio]
        return [
            {'ticker': s, 'rotation': rat, 'today_vol': vol, 'score': rotation_score(rat)}
            for s, rat, vol in sorted(items, key=lambda x: x[1], reverse=True)
        ]

    def get_summary(self) -> dict:
        with self._lock:
            rot = dict(self._rotation)
            date = self._date
        top = sorted(
            [{'ticker': s, 'rotation': v[0], 'score': rotation_score(v[0])}
             for s, v in rot.items()],
            key=lambda x: x['rotation'], reverse=True
        )[:20]
        return {
            'date':     date,
            'tracked':  len(rot),
            'top':      top,
            'high_conviction': [t for t in top if 0.5 <= t['rotation'] < 1.5],
            'exhausted': [t for t in top if t['rotation'] >= 1.5],
        }
