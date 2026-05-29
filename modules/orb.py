"""
ORBAgent — Opening Range Breakout detector.

Tracks the first 5-minute candle (9:30-9:35 ET) for each universe symbol.
Returns tickers where current price has broken above the ORB high.

Academic basis: The first 5-minute candle is the most widely watched level
by retail and institutional day traders. A clean break above the ORB high
with elevated volume has historically shown 55-65% follow-through rate
on gap-up setups (Toby Crabel, "Day Trading with Short Term Price Patterns").

Usage:
    orb = ORBAgent(universe)
    orb.start()   # begin background refresh loop
    breakouts = orb.get_breakouts()   # [{symbol, price, orb_high, pct}, ...]
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


class ORBAgent:

    def __init__(self, universe):
        self.universe = universe   # DynamicUniverse instance or list
        self._lock    = threading.Lock()
        self._orb     = {}   # symbol -> {high, low, open, date}
        self._date    = ''
        self.running  = False
        self._thread  = None

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def _et_now(self):
        return datetime.now(_ET) if _ET else datetime.now(timezone.utc) + timedelta(hours=-4)

    def _loop(self):
        while self.running:
            try:
                et = self._et_now()
                m  = et.hour * 60 + et.minute
                today_str = et.strftime('%Y-%m-%d')

                # Capture ORB levels once per day starting at 9:36am
                if 576 <= m <= 600 and self._date != today_str:
                    self._capture_orb_levels(today_str)

                # Clear levels after market close
                if m >= 960:
                    with self._lock:
                        self._orb.clear()
                    self._date = ''

            except Exception:
                pass
            time.sleep(30)

    def _capture_orb_levels(self, today_str: str):
        """Fetch first-5m candle high/low for entire universe."""
        syms = self.universe.get() if hasattr(self.universe, 'get') else list(self.universe)
        captured = {}
        for sym in syms[:80]:
            try:
                hist = yf.Ticker(sym).history(period='1d', interval='5m')
                if hist.empty:
                    continue
                first = hist.iloc[0]
                captured[sym] = {
                    'high':  float(first['High']),
                    'low':   float(first['Low']),
                    'open':  float(first['Open']),
                    'date':  today_str,
                }
                time.sleep(0.1)   # gentle rate limiting
            except Exception:
                continue
        with self._lock:
            self._orb = captured
        self._date = today_str

    def get_breakouts(self, current_prices: dict = None) -> list:
        """
        Return symbols where current price > ORB high + 0.5% buffer.
        Optionally accepts a {symbol: price} dict to avoid extra yfinance calls.
        """
        today_str = self._et_now().strftime('%Y-%m-%d')
        with self._lock:
            orb = dict(self._orb)
        breakouts = []
        for sym, lvl in orb.items():
            if lvl.get('date') != today_str:
                continue
            orb_high = lvl['high']
            if current_prices:
                price = current_prices.get(sym, 0)
            else:
                try:
                    price = float(yf.Ticker(sym).fast_info.last_price)
                except Exception:
                    continue
            if price <= 0:
                continue
            if price > orb_high * 1.005:
                breakouts.append({
                    'symbol':       sym,
                    'price':        round(price, 4),
                    'orb_high':     round(orb_high, 4),
                    'orb_low':      round(lvl['low'], 4),
                    'breakout_pct': round((price / orb_high - 1) * 100, 2),
                })
        return sorted(breakouts, key=lambda x: x['breakout_pct'], reverse=True)

    def get_orb_level(self, symbol: str) -> dict:
        """Return ORB levels for a single symbol, or empty dict if not captured."""
        today_str = self._et_now().strftime('%Y-%m-%d')
        with self._lock:
            lvl = self._orb.get(symbol.upper(), {})
        return lvl if lvl.get('date') == today_str else {}

    def get_summary(self) -> dict:
        today_str = self._et_now().strftime('%Y-%m-%d')
        with self._lock:
            today_levels = {s: v for s, v in self._orb.items() if v.get('date') == today_str}
        return {
            'date':          today_str,
            'symbols_tracked': len(today_levels),
            'levels':        today_levels,
        }
