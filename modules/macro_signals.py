"""
MacroSignalAgent — environmental, seasonal, and economic non-financial signals.

Scores each trading day on:
  1. Seasonal patterns   — January Effect, Sell in May, quarter-end, day-of-week
  2. Economic calendar   — FOMC dates, Jobs Report Fridays, CPI window
  3. NOAA weather alerts — severe weather → sector bias (energy, insurance, ag)
  4. Civil unrest proxy  — news headline scan for volatility-driving keywords

Score interpretation:
  > +10  Macro environment favorable — lean in
    0    Neutral — normal sizing
  < -10  Adverse macro — reduce position size, tighten stops
"""

import threading
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from collections import defaultdict


HEADERS = {'User-Agent': 'StopTrading buckkeely@gmail.com'}

# 2026 FOMC meeting dates (day-2 to day+1 window treated as high-uncertainty)
FOMC_2026 = [(1,28),(3,18),(5,7),(6,17),(7,29),(9,16),(11,4),(12,16)]

# Keywords → (sector, score_delta)
WEATHER_SECTOR_MAP = [
    (['hurricane','tropical storm','typhoon'],   [('energy',+12),('insurance',-15),('construction',+10)]),
    (['winter storm','blizzard','ice storm'],    [('energy',+10),('retail',-6)]),
    (['drought','heat wave','excessive heat'],   [('utility',+12),('agriculture',-12),('water',+15)]),
    (['flood','flash flood'],                    [('insurance',-10),('construction',+8)]),
    (['tornado','severe thunderstorm'],          [('insurance',-8)]),
    (['wildfire','red flag warning'],            [('insurance',-10),('utility',-5)]),
]

# Civil/economic unrest → headline keywords → score delta
UNREST_KEYWORDS = {
    'inflation':    -5,
    'recession':    -8,
    'layoffs':      -6,
    'strike':       -4,
    'protest':      -3,
    'shutdown':     -7,
    'tariff':       -5,
    'sanctions':    -4,
    'rate hike':    -6,
    'rate cut':     +5,
    'stimulus':     +8,
    'jobs report':  +4,
    'gdp growth':   +6,
}


class MacroSignalAgent:

    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self.running = False
        self._seasonal_score   = 0
        self._seasonal_reasons = []
        self._econ_score       = 0
        self._econ_events      = []
        self._weather_alerts   = []
        self._sector_bias      = {}
        self._unrest_score     = 0
        self._unrest_items     = []
        self._last_refresh     = None

    def start(self):
        self.running = True
        self._refresh()
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self._refresh()
            except Exception:
                pass
            time.sleep(3600)

    def _refresh(self):
        self._calc_seasonal()
        self._calc_econ_calendar()
        self._fetch_weather()
        self._fetch_unrest()
        with self._lock:
            self._last_refresh = datetime.now()

    # ── Seasonal ──────────────────────────────────────────────────────────────

    def _calc_seasonal(self):
        now   = datetime.now()
        month = now.month
        dow   = now.weekday()   # 0=Mon 4=Fri
        dom   = now.day
        score = 0
        reasons = []

        # Monthly effects
        if month == 1:
            score += 15; reasons.append('January Effect: small caps +3-5% historically')
        elif month == 12 and dom >= 15:
            score += 10; reasons.append('Santa Claus rally window')
        elif month == 10:
            score += 5;  reasons.append('October recovery — small caps historically rebound')
        elif month in (3, 4):
            score += 5;  reasons.append('Spring rally season')
        elif month in (5, 6, 7, 8, 9):
            score -= 5;  reasons.append('Sell in May / summer doldrums')
        elif month == 11:
            score -= 8;  reasons.append('Tax-loss selling season')

        # Day-of-week (penny stock specific — 2025 research: Wednesday strongest)
        if dow == 2:
            score += 6; reasons.append('Wednesday: strongest day for penny stocks (2025 data)')
        elif dow == 4:
            score += 3; reasons.append('Friday: historically positive close')
        elif dow == 0:
            score -= 3; reasons.append('Monday effect: historically slightly negative')

        # Start-of-month inflow
        if dom <= 5:
            score += 5; reasons.append('Start of month: fresh capital allocation')

        # Quarter-end window dressing (last week of Mar/Jun/Sep/Dec)
        if month in (3, 6, 9, 12) and dom >= 24:
            score += 8; reasons.append('Quarter-end window dressing: funds buy winners')

        with self._lock:
            self._seasonal_score   = score
            self._seasonal_reasons = reasons

    # ── Economic Calendar ─────────────────────────────────────────────────────

    def _calc_econ_calendar(self):
        now   = datetime.now()
        month = now.month
        dom   = now.day
        dow   = now.weekday()
        score = 0
        events = []

        # Jobs Report: first Friday of each month
        if dow == 4 and dom <= 7:
            events.append('NFP Jobs Report Friday — elevated volatility')
            score += 6  # volatility = opportunity for penny traders

        # CPI: typically 2nd Tue/Wed of month
        if dow in (1, 2) and 8 <= dom <= 14:
            events.append('CPI release window — inflation-sensitive stocks in play')
            score -= 2

        # FOMC window
        for m, d in FOMC_2026:
            if month == m and abs(dom - d) <= 1:
                events.append('FOMC meeting window — rate uncertainty, avoid low-liquidity names')
                score -= 8
                break

        # Options expiration: 3rd Friday of each month
        if dow == 4 and 15 <= dom <= 21:
            events.append('Monthly OpEx Friday — pin risk, unusual volume')
            score += 4  # pinning creates momentum plays

        with self._lock:
            self._econ_score  = score
            self._econ_events = events

    # ── NOAA Weather ─────────────────────────────────────────────────────────

    def _fetch_weather(self):
        try:
            r = requests.get('https://alerts.weather.gov/cap/us.php?x=1',
                             headers=HEADERS, timeout=10)
            if r.status_code != 200:
                return
            root = ET.fromstring(r.content)
            ns   = {'a': 'http://www.w3.org/2005/Atom'}

            sector_scores = defaultdict(int)
            alerts = []

            for entry in root.findall('a:entry', ns)[:30]:
                title = entry.findtext('a:title', '', ns).lower()
                for keywords, impacts in WEATHER_SECTOR_MAP:
                    if any(k in title for k in keywords):
                        for sector, delta in impacts:
                            sector_scores[sector] += delta
                        alerts.append(title[:80])
                        break

            with self._lock:
                self._weather_alerts = list(set(alerts))[:10]
                self._sector_bias    = dict(sector_scores)
        except Exception:
            pass

    # ── Civil / Economic Unrest ───────────────────────────────────────────────

    def _fetch_unrest(self):
        """Scan Reuters/AP RSS for macro-unrest keywords."""
        feeds = [
            'https://feeds.reuters.com/reuters/businessNews',
            'https://rss.app/feeds/v1.1/nytimes-business.json',  # fallback ignored on error
        ]
        score = 0
        items = []
        try:
            r = requests.get(feeds[0], headers=HEADERS, timeout=8)
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall('.//item')[:30]:
                    title = (item.findtext('title') or '').lower()
                    for kw, pts in UNREST_KEYWORDS.items():
                        if kw in title:
                            score += pts
                            items.append('{} ({:+d})'.format(kw, pts))
        except Exception:
            pass

        with self._lock:
            self._unrest_score = max(-20, min(20, score))
            self._unrest_items = items[:10]

    # ── Public API ────────────────────────────────────────────────────────────

    def get_score(self, sector='general'):
        """Composite macro score. Positive = buy-favorable. Negative = caution."""
        with self._lock:
            base = self._seasonal_score + self._econ_score + self._unrest_score
            if sector != 'general':
                base += self._sector_bias.get(sector, 0)
            return base

    def sector_bias(self, sector):
        with self._lock:
            return self._sector_bias.get(sector, 0)

    def is_favorable(self):
        return self.get_score() >= -8

    def get_summary(self):
        with self._lock:
            return {
                'seasonal_score':   self._seasonal_score,
                'seasonal_reasons': self._seasonal_reasons,
                'econ_score':       self._econ_score,
                'econ_events':      self._econ_events,
                'weather_alerts':   self._weather_alerts,
                'sector_bias':      self._sector_bias,
                'unrest_score':     self._unrest_score,
                'unrest_items':     self._unrest_items,
                'total_score':      self._seasonal_score + self._econ_score + self._unrest_score,
                'favorable':        (self._seasonal_score + self._econ_score + self._unrest_score) >= -8,
                'last_refresh':     self._last_refresh.isoformat() if self._last_refresh else None,
            }
