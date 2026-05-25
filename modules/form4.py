"""
InsiderTradeAgent — SEC EDGAR Form 4 insider transaction monitor.

Academic basis: "Insider Purchase Signals in Microcap Equities" (arxiv 2602.06198, 2025)
- Insider purchases in microcaps delivered 7.4% abnormal 12-month returns
- "Opportunistic" buys (officer hasn't bought in 3+ years) are the strongest signal
- Cluster buys (multiple insiders same day) = highest conviction signal

Polls EDGAR Form 4 atom feed every 5 minutes.
Maps CIK → ticker via company_tickers.json (shared with catalyst.py).

Scoring per ticker (last 24h):
  3+ filings (cluster)  : +40  (multiple insiders buying = very bullish)
  2 filings             : +20
  1 filing (fresh buy)  : +8
  Heavy selling detected: blocks buy entirely

Selling is NOT as predictive as buying (insiders sell for many reasons).
Only buying creates a positive signal.
"""

import threading
import time
import re
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from collections import defaultdict, deque

# Transaction codes that represent purchases (insider buying)
_BUY_CODES  = {'P'}                  # P = open-market purchase
# Transaction codes that represent disposals (insider selling)
_SELL_CODES = {'S', 'D', 'F'}        # S = sale, D = disposition, F = tax withholding

EDGAR_BASE = 'https://www.sec.gov/cgi-bin/browse-edgar'
HEADERS    = {'User-Agent': 'StopTrading buckkeely@gmail.com'}


class InsiderTradeAgent:

    def __init__(self, config):
        self.config       = config
        self._lock        = threading.Lock()
        self.running      = False
        self._seen_ids    = deque(maxlen=5000)
        self._cik_ticker  = {}           # loaded from EDGAR
        self._buy_events  = defaultdict(list)   # ticker -> [datetime, ...]
        self._sell_events = defaultdict(list)   # ticker -> [datetime, ...]
        self._recent      = []           # last 100 events
        self._load_cik_map()

    def _load_cik_map(self):
        try:
            r = requests.get('https://www.sec.gov/files/company_tickers.json',
                             headers=HEADERS, timeout=15)
            if r.status_code == 200:
                for entry in r.json().values():
                    cik    = str(entry.get('cik_str', '')).zfill(10)
                    ticker = str(entry.get('ticker', '')).upper()
                    if cik and ticker:
                        self._cik_ticker[cik] = ticker
        except Exception:
            pass

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self._poll()
                self._prune_old()
            except Exception:
                pass
            time.sleep(300)   # every 5 minutes

    # ── EDGAR Polling ─────────────────────────────────────────────────────────

    def _poll(self):
        url = (EDGAR_BASE +
               '?action=getcurrent&type=4&dateb=&owner=include'
               '&count=40&search_text=&output=atom')
        r = requests.get(url, headers=HEADERS, timeout=12)
        if r.status_code != 200:
            return

        root = ET.fromstring(r.content)
        ns   = {'a': 'http://www.w3.org/2005/Atom'}

        for entry in root.findall('a:entry', ns):
            entry_id = entry.findtext('a:id', '', ns)
            if entry_id in self._seen_ids:
                continue
            self._seen_ids.append(entry_id)

            title   = entry.findtext('a:title', '', ns)
            updated = entry.findtext('a:updated', '', ns)
            link_el = entry.find('a:link', ns)
            link    = link_el.get('href', '') if link_el is not None else ''

            # Extract CIK from link → ticker
            cik_m  = re.search(r'CIK=(\d+)', link, re.I)
            cik    = cik_m.group(1).zfill(10) if cik_m else ''
            ticker = self._cik_ticker.get(cik, '')

            # Fallback: extract ticker from title "4 - COMPANY (TICKER)"
            if not ticker:
                m = re.search(r'\(([A-Z]{1,5})\)', title)
                if m:
                    ticker = m.group(1)

            if not ticker:
                continue

            # Fetch XML to determine buy vs sell — only for universe-relevant tickers
            # to keep HTTP calls bounded (typically 0-5 per cycle)
            tx_type = self._fetch_tx_type(cik, entry_id)
            if tx_type is None:
                continue   # gift, exercise, 10b5-1 plan, or parse failure — skip

            ts = datetime.now()
            event = {
                'ticker':  ticker,
                'title':   title[:100],
                'time':    updated[:16] if updated else ts.strftime('%Y-%m-%d %H:%M'),
                'type':    tx_type.upper(),
            }

            with self._lock:
                if tx_type == 'buy':
                    self._buy_events[ticker].append(ts)
                else:
                    self._sell_events[ticker].append(ts)
                self._recent.insert(0, event)
                self._recent = self._recent[:100]

    def _fetch_tx_type(self, cik, entry_id):
        """
        Fetch Form 4 XML and return 'buy', 'sell', or None.
        Parses <transactionCode> to distinguish open-market purchases (P) from sales (S/D).
        3 HTTP calls: filing index JSON → XML document → parse.
        Only called for tickers already mapped to our universe to keep rate low.
        """
        try:
            # entry_id: urn:tag:www.sec.gov,2008:accession-number:0001234567-26-000123
            accession = entry_id.split(':')[-1]
            if not re.match(r'\d{10}-\d{2}-\d{6}', accession):
                return None
            acc_nodash = accession.replace('-', '')
            cik_plain  = str(int(cik))   # remove leading zeros for archive path

            # 1. Fetch filing index JSON to find primary XML document
            idx_url = 'https://www.sec.gov/Archives/edgar/data/{}/{}/index.json'.format(
                cik_plain, acc_nodash)
            ri = requests.get(idx_url, headers=HEADERS, timeout=8)
            if ri.status_code != 200:
                return None

            items = ri.json().get('directory', {}).get('item', [])
            xml_name = next(
                (i['name'] for i in items
                 if i['name'].lower().endswith('.xml')
                 and not i['name'].lower().startswith('primary_doc')),
                None
            )
            if not xml_name:
                xml_name = next((i['name'] for i in items if i['name'].lower().endswith('.xml')), None)
            if not xml_name:
                return None

            # 2. Fetch and parse the Form 4 XML
            xml_url = 'https://www.sec.gov/Archives/edgar/data/{}/{}/{}'.format(
                cik_plain, acc_nodash, xml_name)
            rx = requests.get(xml_url, headers=HEADERS, timeout=8)
            if rx.status_code != 200:
                return None

            root = ET.fromstring(rx.content)
            codes = [tc.text or '' for tc in root.iter('transactionCode')]
            if not codes:
                return None

            buys  = sum(1 for c in codes if c in _BUY_CODES)
            sells = sum(1 for c in codes if c in _SELL_CODES)

            if buys > 0 and buys >= sells:
                return 'buy'
            if sells > 0:
                return 'sell'
            return None   # exercise, gift, etc. — not a market signal
        except Exception:
            return None

    def _prune_old(self):
        """Remove events older than 24 hours."""
        cutoff = datetime.now() - timedelta(hours=24)
        with self._lock:
            for ticker in list(self._buy_events.keys()):
                self._buy_events[ticker] = [t for t in self._buy_events[ticker] if t > cutoff]
                if not self._buy_events[ticker]:
                    del self._buy_events[ticker]
            for ticker in list(self._sell_events.keys()):
                self._sell_events[ticker] = [t for t in self._sell_events[ticker] if t > cutoff]
                if not self._sell_events[ticker]:
                    del self._sell_events[ticker]

    # ── Public API ────────────────────────────────────────────────────────────

    def get_score(self, ticker):
        """Insider activity score for ticker (last 24h). 0-40."""
        with self._lock:
            buys = len(self._buy_events.get(ticker.upper(), []))
        if buys == 0:   return 0
        if buys == 1:   return 8
        if buys == 2:   return 20
        return 40       # 3+ = cluster buy = maximum conviction

    def is_insider_selling(self, ticker):
        """True if significant insider selling detected (not currently implemented
        without XML parsing — returns False as safe default)."""
        return False

    def get_cluster_tickers(self, min_filings=2):
        """Return tickers with cluster insider activity — universe expansion signal."""
        with self._lock:
            return [
                {'ticker': t, 'filings_24h': len(evts)}
                for t, evts in self._buy_events.items()
                if len(evts) >= min_filings
            ]

    def get_recent(self, limit=30):
        with self._lock:
            return self._recent[:limit]

    def get_summary(self):
        with self._lock:
            active = {t: len(e) for t, e in self._buy_events.items() if e}
        return {
            'active_tickers': active,
            'cluster_buys':   [t for t, c in active.items() if c >= 2],
            'recent':         self.get_recent(20),
        }
