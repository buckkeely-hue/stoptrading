"""
SectorMomentumAgent — ETF sector rotation signals for penny stock tailwinds.

Small-cap penny stocks are often beta plays on their parent sector ETF.
When the sector ETF outperforms SPY on 5m bars with accelerating volume,
constituent small-caps frequently follow within 30-60 minutes.

ETF → constituent penny stock connection:
  ARKK  — disruptive tech/innovation (MMAT, NKLA, SNDL adjacent)
  XBI   — small-cap biotech (ATNF, BFRI, ABVC, GOVX, most biotech pennies)
  KARS  — EV/lithium/battery (WKHS, GOEV, SOLO, NKLA, RIDE)
  BETZ  — online gaming (HOOK, GMBL)
  SOXS/SOXL sector — semiconductors (downstream small-caps)
  SPY   — broad market regime (baseline)

Scoring:
  Sector ETF 1h return vs SPY:
    +3% or more outperformance:  strong tailwind (+15)
    +1.5% to +3%:                moderate tailwind (+8)
    +0.5% to +1.5%:              mild tailwind (+4)
    -0.5% to +0.5%:              neutral (0)
    -1.5% to -0.5%:              mild headwind (-4)
    below -1.5%:                 strong headwind (-10)
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

# ETF → universe tag mapping
SECTOR_ETFS = {
    'ARKK':  ['MMAT', 'NKLA', 'SNDL', 'CLOV', 'GFAI'],
    'XBI':   ['ATNF', 'BFRI', 'ABVC', 'GOVX', 'GTBP', 'XTLB', 'COCP', 'CELZ'],
    'KARS':  ['WKHS', 'GOEV', 'RIDE', 'ZEV', 'NKLA', 'CENN', 'DPRO'],
    'BETZ':  ['HOOK', 'GMBL', 'ACMR'],
    'BITO':  ['BTBT', 'MARA', 'RIOT', 'CIFR', 'WULF'],
}
ALL_TRACKED_ETFS = list(SECTOR_ETFS.keys()) + ['SPY']


def _etf_return_vs_spy(etf_hist, spy_hist) -> float:
    """Compute ETF 1-bar (5m) return minus SPY 1-bar return."""
    try:
        if etf_hist.empty or spy_hist.empty:
            return 0.0
        etf_chg = (etf_hist['Close'].iloc[-1] - etf_hist['Close'].iloc[-2]) / etf_hist['Close'].iloc[-2] * 100
        spy_chg = (spy_hist['Close'].iloc[-1] - spy_hist['Close'].iloc[-2]) / spy_hist['Close'].iloc[-2] * 100
        return round(etf_chg - spy_chg, 3)
    except Exception:
        return 0.0


def _sector_score(rel_return: float) -> int:
    if rel_return >= 3.0:   return 15
    if rel_return >= 1.5:   return 8
    if rel_return >= 0.5:   return 4
    if rel_return >= -0.5:  return 0
    if rel_return >= -1.5:  return -4
    return -10


class SectorMomentumAgent:

    def __init__(self, config):
        self.config    = config
        self._lock     = threading.Lock()
        self._etf_data = {}   # etf -> {rel_return, score, timestamp}
        self._spy_hist = None
        self.running   = False

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
                if 570 <= m <= 970:   # during market hours only
                    self._refresh()
            except Exception:
                pass
            time.sleep(300)   # 5-minute refresh

    def _refresh(self):
        try:
            spy_h = yf.Ticker('SPY').history(period='1d', interval='5m')
            if spy_h.empty or len(spy_h) < 3:
                return
        except Exception:
            return

        new_data = {}
        for etf in SECTOR_ETFS:
            try:
                etf_h = yf.Ticker(etf).history(period='1d', interval='5m')
                rel   = _etf_return_vs_spy(etf_h, spy_h)
                new_data[etf] = {
                    'rel_return': rel,
                    'score':      _sector_score(rel),
                    'timestamp':  time.time(),
                }
                time.sleep(0.2)
            except Exception:
                continue

        with self._lock:
            self._etf_data = new_data
            self._spy_hist = spy_h

    # ── Public API ────────────────────────────────────────────────────────────

    def get_sector_score(self, symbol: str) -> int:
        """
        Return the strongest sector tailwind/headwind score for a given ticker.
        Checks all ETFs that contain this ticker and returns the max positive
        (or most negative) score.
        """
        sym = symbol.upper()
        scores = []
        with self._lock:
            for etf, members in SECTOR_ETFS.items():
                if sym in [m.upper() for m in members]:
                    entry = self._etf_data.get(etf)
                    if entry:
                        scores.append(entry['score'])
        if not scores:
            return 0
        # Return max by absolute value — strongest signal wins
        return max(scores, key=abs)

    def get_broad_market_bias(self) -> int:
        """
        Overall market tone based on SPY recent bars.
        Returns int: +5 (bullish), 0 (neutral), -5 (bearish).
        """
        with self._lock:
            spy_h = self._spy_hist
        if spy_h is None or spy_h.empty or len(spy_h) < 6:
            return 0
        try:
            recent = spy_h['Close'].tail(6).values
            chg = (recent[-1] - recent[0]) / recent[0] * 100 if recent[0] > 0 else 0
            if chg > 0.5:   return 5
            if chg < -0.5:  return -5
            return 0
        except Exception:
            return 0

    def get_strongest_sectors(self) -> list:
        """Return ETFs sorted by rel_return, with their constituent penny tickers."""
        with self._lock:
            data = dict(self._etf_data)
        result = []
        for etf, d in sorted(data.items(), key=lambda x: x[1]['rel_return'], reverse=True):
            result.append({
                'etf':        etf,
                'rel_return': d['rel_return'],
                'score':      d['score'],
                'members':    SECTOR_ETFS.get(etf, []),
            })
        return result

    def get_summary(self) -> dict:
        return {
            'sectors':            self.get_strongest_sectors(),
            'broad_market_bias':  self.get_broad_market_bias(),
        }
