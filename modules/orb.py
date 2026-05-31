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
from datetime import datetime, timezone, timedelta
from modules.market_data import MarketData

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo('America/New_York')
except Exception:
    _ET = None


class ORBAgent:

    def __init__(self, universe, config=None):
        self.universe = universe   # DynamicUniverse instance or list
        self._lock    = threading.Lock()
        self._orb     = {}   # symbol -> {high, low, open, date}
        self._date    = ''
        self.running  = False
        self._thread  = None
        self._md      = MarketData(config or {})

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
                hist = self._md.Ticker(sym).history(period='1d', interval='5m')
                if hist.empty:
                    continue
                # Validate the first bar is the 9:30 ET candle, not a pre-market bar
                bar_ts = hist.index[0]
                try:
                    bar_et = bar_ts.tz_convert(_ET) if _ET and bar_ts.tzinfo else bar_ts
                    if bar_et.hour != 9 or bar_et.minute != 30:
                        # Skip to the first bar at 9:30
                        market_open = hist[
                            (hist.index.hour == 9) & (hist.index.minute == 30)
                            if hasattr(hist.index, 'hour') else []
                        ]
                        if market_open.empty:
                            continue
                        first = market_open.iloc[0]
                    else:
                        first = hist.iloc[0]
                except Exception:
                    first = hist.iloc[0]
                captured[sym] = {
                    'high':   float(first['High']),
                    'low':    float(first['Low']),
                    'open':   float(first['Open']),
                    'volume': int(first['Volume']) if first['Volume'] > 0 else 0,
                    'date':   today_str,
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
                    price = self._md.last_price(sym)
                except Exception:
                    continue
            if price <= 0:
                continue
            if price > orb_high * 1.005:
                # Volume confirmation: current 5m bar must have ≥ 1.5× the ORB candle volume
                orb_vol = lvl.get('volume', 0)
                if orb_vol > 0:
                    try:
                        recent_bars = self._md.Ticker(sym).history(period='1d', interval='5m')
                        curr_vol = int(recent_bars['Volume'].iloc[-1]) if not recent_bars.empty else 0
                        if curr_vol < orb_vol * 1.5:
                            continue  # insufficient volume — not a confirmed breakout
                    except Exception:
                        pass  # fail open — don't block if volume fetch fails
                breakouts.append({
                    'symbol':       sym,
                    'price':        round(price, 4),
                    'orb_high':     round(orb_high, 4),
                    'orb_low':      round(lvl['low'], 4),
                    'orb_volume':   orb_vol,
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
