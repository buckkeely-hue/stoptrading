"""
NewsAgent — real-time news catalyst scoring via yfinance + SEC EDGAR.

Sources:
  1. yfinance .news — aggregated headlines (Yahoo Finance, Benzinga, etc.)
  2. SEC EDGAR ATOM feed — all 8-K filings filed today, filtered to universe

Scoring table:
  FDA approval / breakthrough therapy:      +30
  Earnings beat / revenue beat:             +20
  Revenue raised / guidance raised:         +18
  Major contract / partnership / deal:      +15
  Buyout / acquisition target:              +15
  Product launch / FDA clearance:           +12
  New hire / leadership appointment:        +5
  General positive press release:           +5
  Earnings miss / revenue miss:            -15
  Guidance lowered:                        -12
  SEC investigation / subpoena / lawsuit:  -20
  Bankruptcy / default / going concern:    -25
  Dilution / offering / ATM sale:          -25 (also handled by CatalystEngine)

Recency weight: score × max(0.1, 1 - age_hours / 3)
  → news from 0h ago = 100%, 1.5h ago = 50%, 3h ago = 0%
"""

import re
import threading
import time
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

try:
    import yfinance as yf
    _HAS_YF = True
except ImportError:
    _HAS_YF = False

HEADERS = {'User-Agent': 'StopTrading/NewsAgent 1.0 (buckkeely@gmail.com)'}

_POSITIVE_KW = [
    (r'\bfda\s+(?:approved?|clearance|breakthrough|designation)\b', 30),
    (r'\bearnings?\s+beat\b', 20),
    (r'\brevenue?\s+beat\b', 20),
    (r'\braises?\s+(?:guidance|outlook|forecast)\b', 18),
    (r'\b(?:major\s+)?(?:contract|partnership|collaboration|deal|agreement)\b', 15),
    (r'\bacquisition|buyout|merger\b', 15),
    (r'\bfda\s+clearance\b', 12),
    (r'\bproduct\s+launch\b', 10),
    (r'\bsecured?\s+(?:funding|financing|grant|award)\b', 10),
    (r'\bnew\s+(?:ceo|cfo|coo|president|chief)\b', 5),
    (r'\bannounces?\b', 3),
]

_NEGATIVE_KW = [
    (r'\bbankruptcy|chapter\s+11|chapter\s+7|going\s+concern\b', -25),
    (r'\boffering|dilut|at-the-market|atm\s+facilit\b', -25),
    (r'\bsec\s+(?:investigation|subpoena|charges|enforcement)\b', -20),
    (r'\blawsuit|litigation|class\s+action\b', -15),
    (r'\bearnings?\s+miss\b', -15),
    (r'\blowers?\s+guidance\b', -12),
    (r'\bdefault|delinquent|notice\s+of\s+default\b', -20),
]


def _score_headline(title: str) -> int:
    t = title.lower()
    score = 0
    for pat, pts in _POSITIVE_KW:
        if re.search(pat, t):
            score += pts
    for pat, pts in _NEGATIVE_KW:
        if re.search(pat, t):
            score += pts
    return score


def _recency_factor(publish_ts: float) -> float:
    age_h = (time.time() - publish_ts) / 3600
    return max(0.0, 1.0 - age_h / 3.0)


class NewsAgent:

    def __init__(self, config, universe):
        self.config    = config
        self.universe  = universe
        self._lock     = threading.Lock()
        self._cache    = defaultdict(list)  # symbol -> [{title, raw_score, publish_ts}]
        self._scores   = {}   # symbol -> weighted composite score
        self._last_run = 0
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
                self._refresh()
            except Exception:
                pass
            time.sleep(300)   # 5-minute refresh cycle

    def _refresh(self):
        syms = self.universe.get() if hasattr(self.universe, 'get') else list(self.universe)
        new_cache = defaultdict(list)

        for sym in syms[:60]:
            try:
                if not _HAS_YF:
                    break
                news_items = yf.Ticker(sym).news
                for item in (news_items or [])[:8]:
                    title = item.get('title', '')
                    pts   = _score_headline(title)
                    ts    = float(item.get('providerPublishTime', 0))
                    if ts == 0:
                        ts = time.time()
                    age_h = (time.time() - ts) / 3600
                    if age_h > 24:
                        continue   # ignore stale news
                    new_cache[sym].append({
                        'title':     title,
                        'raw_score': pts,
                        'publish_ts': ts,
                        'source':    item.get('publisher', ''),
                    })
                time.sleep(0.15)
            except Exception:
                continue

        self._fetch_edgar(new_cache, syms)

        scores = {}
        for sym, items in new_cache.items():
            weighted = sum(
                item['raw_score'] * _recency_factor(item['publish_ts'])
                for item in items
            )
            scores[sym] = round(weighted, 1)

        with self._lock:
            self._cache  = new_cache
            self._scores = scores
            self._last_run = time.time()

    def _fetch_edgar(self, cache: dict, universe: list):
        """Pull today's 8-K filings from EDGAR ATOM feed and match to universe."""
        try:
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            url   = (
                'https://www.sec.gov/cgi-bin/browse-edgar'
                '?action=getcurrent&type=8-K&dateb=&owner=include&count=40&output=atom'
            )
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                return
            text = r.text.upper()
            sym_set = set(universe)
            for sym in sym_set:
                if sym not in text:
                    continue
                # Simple pattern match — 8-K filed by this company today
                import xml.etree.ElementTree as ET
                try:
                    root = ET.fromstring(r.text)
                    ns   = {'atom': 'http://www.w3.org/2005/Atom'}
                    for entry in root.findall('atom:entry', ns):
                        title_el = entry.find('atom:title', ns)
                        upd_el   = entry.find('atom:updated', ns)
                        if title_el is None:
                            continue
                        title = title_el.text or ''
                        if sym.upper() not in title.upper():
                            continue
                        upd = upd_el.text[:10] if upd_el is not None and upd_el.text else ''
                        if upd != today:
                            continue
                        pts = _score_headline(title) or 10   # at least +10 for filing
                        cache[sym].append({
                            'title':     title,
                            'raw_score': pts,
                            'publish_ts': time.time(),
                            'source':    'SEC-EDGAR',
                        })
                except Exception:
                    pass
        except Exception:
            pass

    # ── Public API ────────────────────────────────────────────────────────────

    def get_news_score(self, symbol: str) -> int:
        with self._lock:
            return int(self._scores.get(symbol.upper(), 0))

    def get_hot_tickers(self, min_score: int = 8) -> list:
        """Return tickers with recent positive news, sorted by score."""
        with self._lock:
            items = [(s, sc) for s, sc in self._scores.items() if sc >= min_score]
        return [{'ticker': s, 'score': sc} for s, sc in sorted(items, key=lambda x: x[1], reverse=True)]

    def get_recent_headlines(self, symbol: str) -> list:
        with self._lock:
            return list(self._cache.get(symbol.upper(), []))[:5]

    def get_summary(self) -> dict:
        with self._lock:
            hot = sorted(
                [(s, sc) for s, sc in self._scores.items() if sc != 0],
                key=lambda x: abs(x[1]), reverse=True
            )[:20]
        return {
            'last_refresh': datetime.fromtimestamp(self._last_run).isoformat() if self._last_run else None,
            'tracked':      len(self._scores),
            'positive':     sum(1 for _, sc in self._scores.items() if sc > 0),
            'negative':     sum(1 for _, sc in self._scores.items() if sc < 0),
            'hot':          [{'ticker': s, 'score': sc} for s, sc in hot],
        }
