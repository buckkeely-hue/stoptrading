"""
Catalyst Engine — SEC EDGAR real-time 8-K / S-3 / 424B5 detection.

Polls SEC EDGAR every 30 seconds for new filings. Classifies each one by
keyword NLP scoring. Maintains:
  - catalyst_scores: {ticker -> score}  (positive = bullish, negative = avoid)
  - dilution_guard:  set of tickers with active dilutive filings — never buy

Catalyst scoring scale:
  +80  FDA approval / Phase 3 success / major acquisition
  +60  Large government contract / uplisting to NASDAQ/NYSE
  +50  Strategic partnership / licensing deal / patent grant
  +30  Revenue/guidance raise / positive clinical data
  +10  Minor PR / officer change (positive)
   0   Neutral / unclear
  -30  SEC inquiry / class action lawsuit / CEO resignation
  -60  S-3 shelf registration (imminent dilution likely)
  -100 424B5 prospectus supplement (dilution HAPPENING NOW — sell immediately)

A ticker with score < 0 is added to the dilution_guard blacklist.
"""

import threading
import time
import re
import requests
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timedelta

EDGAR_BASE = 'https://www.sec.gov/cgi-bin/browse-edgar'
EFTS_BASE  = 'https://efts.sec.gov/LATEST/search-index'   # full-text search, 2-10 min lag
HEADERS    = {'User-Agent': 'StopTrading buckkeely@gmail.com'}

# Keyword → point value (checked against filing title/description, case-insensitive)
BULLISH_KEYWORDS = [
    ('fda approval',          80), ('fda approved',           80),
    ('phase 3 results',       80), ('phase iii results',      80),
    ('breakthrough therapy',  70), ('accelerated approval',   70),
    ('pdufa',                 85), ('pdufa date',             90),
    ('nda approval',          80), ('bla approval',           80),
    ('orphan drug',           60), ('fast track designation', 65),
    ('acquisition',           60), ('merger agreement',       60),
    ('uplisting',             60), ('nasdaq listing',         60),
    ('government contract',   55), ('department of defense',  55),
    ('dod contract',          55), ('strategic partnership',  50),
    ('licensing agreement',   50), ('patent granted',         45),
    ('patent issued',         45), ('revenue guidance',       35),
    ('raises guidance',       35), ('positive results',       30),
    ('clinical data',         25), ('fda clearance',          70),
    ('510(k)',                 65), ('pma approval',           70),
    ('record revenue',        40), ('profitable',             30),
    ('stock repurchase',      25), ('buyback',                25),
    ('dea schedule',          40), ('controlled substance',   35),
    ('insider purchase',      30), ('director purchase',      30),
]

BEARISH_KEYWORDS = [
    ('424b5',                -100), ('prospectus supplement', -100),
    ('shelf registration',    -60), ('s-3',                   -60),
    ('at-the-market',         -70), ('atm offering',          -70),
    ('registered direct',     -80), ('pipe transaction',      -75),
    ('going concern',         -80), ('bankruptcy',            -100),
    ('chapter 11',           -100), ('default',               -70),
    ('sec investigation',     -65), ('class action',          -50),
    ('delisting',             -90), ('reverse split',         -40),
    ('restatement',           -55), ('material weakness',     -45),
    ('ceo resignation',       -35), ('cfo resignation',       -30),
]

# Filing types that are always bearish / dilutive
DILUTION_TYPES = {'S-3', 'S-3/A', '424B5', '424B4', '424B3', 'S-1/A', 'POS AM'}


class CatalystEngine:

    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self.catalyst_scores  = {}   # ticker -> {'score', 'title', 'type', 'time'}
        self.dilution_guard   = {}   # ticker -> datetime added (pruned after 30 days)
        self.recent_filings   = []   # last 100 filings seen
        self.running          = False
        self._seen_ids        = deque(maxlen=2000)
        self._cik_to_ticker   = {}
        self._load_cik_map()

    # ── CIK → Ticker Map ─────────────────────────────────────────────────────

    def _load_cik_map(self):
        """Download EDGAR company_tickers.json once to map CIK → ticker."""
        try:
            r = requests.get(
                'https://www.sec.gov/files/company_tickers.json',
                headers=HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                for entry in data.values():
                    cik = str(entry.get('cik_str', '')).zfill(10)
                    ticker = str(entry.get('ticker', '')).upper()
                    if cik and ticker:
                        self._cik_to_ticker[cik] = ticker
        except Exception:
            pass

    def _extract_cik(self, link_href):
        m = re.search(r'CIK=(\d+)', link_href or '', re.IGNORECASE)
        if m:
            return m.group(1).zfill(10)
        return None

    # ── Background Polling ────────────────────────────────────────────────────

    def start(self):
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def stop(self):
        self.running = False

    def _prune_dilution_guard(self):
        cutoff = datetime.now() - timedelta(days=30)
        with self._lock:
            expired = [t for t, ts in self.dilution_guard.items() if ts <= cutoff]
            for t in expired:
                del self.dilution_guard[t]

    def _loop(self):
        cycle = 0
        while self.running:
            for filing_type in ['8-K', 'S-3', '424B5', 'S-1']:
                try:
                    self._poll(filing_type)
                except Exception:
                    pass
                time.sleep(2)
            try:
                self._poll_efts()
            except Exception:
                pass
            cycle += 1
            if cycle % 20 == 0:   # prune every ~10 minutes
                self._prune_dilution_guard()
            time.sleep(25)

    def _poll(self, filing_type):
        url = (EDGAR_BASE +
               '?action=getcurrent&type={}&dateb=&owner=include'
               '&count=40&search_text=&output=atom').format(filing_type)
        r = requests.get(url, headers=HEADERS, timeout=12)
        if r.status_code != 200:
            return

        root = ET.fromstring(r.content)
        ns   = {'atom': 'http://www.w3.org/2005/Atom'}

        for entry in root.findall('atom:entry', ns):
            entry_id = entry.findtext('atom:id', '', ns)
            if entry_id in self._seen_ids:
                continue
            self._seen_ids.append(entry_id)

            title   = entry.findtext('atom:title', '', ns)
            updated = entry.findtext('atom:updated', '', ns)
            link_el = entry.find('atom:link', ns)
            link    = link_el.get('href', '') if link_el is not None else ''

            cik    = self._extract_cik(link)
            ticker = self._cik_to_ticker.get(cik, '') if cik else ''

            if not ticker:
                # Try extracting ticker from title like "8-K - COMPANY (TICKER)"
                m = re.search(r'\(([A-Z]{1,5})\)', title)
                if m:
                    ticker = m.group(1)

            if not ticker:
                continue

            score = self._score(title, filing_type)
            is_dilutive = filing_type in DILUTION_TYPES or score <= -60

            with self._lock:
                existing = self.catalyst_scores.get(ticker, {})
                # Keep the most recent score
                self.catalyst_scores[ticker] = {
                    'score':  score,
                    'title':  title[:120],
                    'type':   filing_type,
                    'time':   updated[:16] if updated else datetime.now().strftime('%Y-%m-%d %H:%M'),
                    'ticker': ticker,
                    'dilutive': is_dilutive,
                }
                if is_dilutive:
                    self.dilution_guard[ticker] = datetime.now()
                elif ticker in self.dilution_guard and score >= 50:
                    del self.dilution_guard[ticker]

                self.recent_filings.insert(0, {
                    'ticker':   ticker,
                    'type':     filing_type,
                    'score':    score,
                    'title':    title[:100],
                    'time':     updated[:16] if updated else '',
                    'dilutive': is_dilutive,
                })
                self.recent_filings = self.recent_filings[:100]

    def _poll_efts(self):
        """
        EDGAR Full-Text Search — searches 8-K body text for bullish keywords.
        Catches catalysts 2-10 minutes faster than atom feed title-only matching
        because it searches inside the full document, not just the filing title.
        """
        today = datetime.now().strftime('%Y-%m-%d')
        # Search for high-value catalyst keywords across all 8-Ks filed today
        keywords = ['FDA approval', 'FDA approved', 'breakthrough therapy',
                    'government contract', 'strategic partnership', 'uplisting',
                    'acquisition', 'merger agreement', 'record revenue']
        for kw in keywords[:4]:   # limit to top 4 to stay within rate limits
            try:
                url = ('{}?q="{}"&forms=8-K&dateRange=custom'
                       '&startdt={}&hits.hits._source.period_of_report=true').format(
                    EFTS_BASE, kw.replace(' ', '+'), today)
                r = requests.get(url, headers=HEADERS, timeout=8)
                if r.status_code != 200:
                    continue
                hits = r.json().get('hits', {}).get('hits', [])
                for hit in hits[:5]:
                    src     = hit.get('_source', {})
                    efts_id = hit.get('_id', '')
                    if efts_id in self._seen_ids:
                        continue
                    self._seen_ids.append(efts_id)

                    entity  = src.get('entity_name', '') or src.get('display_names', [''])[0]
                    tickers = src.get('file_date', '')
                    # Extract ticker from entity name or form data
                    ticker  = ''
                    m = re.search(r'\b([A-Z]{1,5})\b', src.get('period_of_report', ''))
                    if not m:
                        m = re.search(r'\(([A-Z]{1,5})\)', entity)
                    if m:
                        ticker = m.group(1)
                    if not ticker:
                        continue

                    title = '{} — EFTS: {}'.format(entity[:60], kw)
                    score = self._score(kw, '8-K') + 10   # EFTS hit = confirmed keyword in body
                    with self._lock:
                        if ticker not in self.catalyst_scores or score > self.catalyst_scores[ticker].get('score', 0):
                            self.catalyst_scores[ticker] = {
                                'score':    score,
                                'title':    title[:120],
                                'type':     '8-K/EFTS',
                                'time':     datetime.now().strftime('%Y-%m-%d %H:%M'),
                                'ticker':   ticker,
                                'dilutive': False,
                            }
                            self.recent_filings.insert(0, {
                                'ticker':   ticker,
                                'type':     '8-K/EFTS',
                                'score':    score,
                                'title':    title[:100],
                                'time':     datetime.now().strftime('%Y-%m-%d %H:%M'),
                                'dilutive': False,
                            })
                        self.recent_filings = self.recent_filings[:100]
            except Exception:
                continue

    def _score(self, text, filing_type):
        text_lower = text.lower()
        score = 0

        # Filing type baseline
        if filing_type in DILUTION_TYPES:
            score -= 70
        elif filing_type == '8-K':
            score += 5  # slight positive prior — 8-K means something happened

        for keyword, pts in BULLISH_KEYWORDS:
            if keyword in text_lower:
                score += pts

        for keyword, pts in BEARISH_KEYWORDS:
            if keyword in text_lower:
                score += pts   # pts are negative

        return max(-100, min(100, score))

    # ── Public API ────────────────────────────────────────────────────────────

    def get_score(self, ticker):
        """Return catalyst score for ticker. 0 if no recent filing."""
        with self._lock:
            return self.catalyst_scores.get(ticker.upper(), {}).get('score', 0)

    def get_catalyst(self, ticker):
        with self._lock:
            return self.catalyst_scores.get(ticker.upper(), {})

    def is_diluting(self, ticker):
        """True if ticker has an active dilutive filing within the last 30 days."""
        cutoff = datetime.now() - timedelta(days=30)
        with self._lock:
            ts = self.dilution_guard.get(ticker.upper())
            return ts is not None and ts > cutoff

    def get_recent(self, limit=30):
        with self._lock:
            return self.recent_filings[:limit]

    def get_bullish_tickers(self, min_score=30):
        """Return tickers with strong positive catalysts — for universe expansion."""
        with self._lock:
            return [
                t for t, d in self.catalyst_scores.items()
                if d.get('score', 0) >= min_score and not d.get('dilutive')
            ]

    def get_all_scores(self):
        with self._lock:
            return dict(self.catalyst_scores)
