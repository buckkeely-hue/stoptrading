"""
CongressAgent — Congressional stock trade monitor via Quiver Quantitative.

US Congress members must disclose stock trades within 45 days under the STOCK Act.
Quiver Quantitative aggregates these disclosures and exposes a free API.

Why it matters for penny stocks:
  - Congress members have access to non-public information about regulations,
    contracts, and legislation that disproportionately affect small/micro-caps
  - Cluster buying (multiple members buying same stock) is the strongest signal
  - 45-day lag limits real-time utility, but recent disclosures still carry edge

Scoring:
  3+ members bought recently : +20
  2 members bought            : +12
  1 member bought             : +6
  Any recent selling          : -5 (mild — insiders sell for many reasons)

Free API endpoint: https://api.quiverquant.com/beta/live/congresstrading
No API key required for free tier (rate-limited to ~30 req/hour).
"""

import threading
import time
import requests
from datetime import datetime, date, timedelta
from collections import defaultdict

API_URL = 'https://api.quiverquant.com/beta/live/congresstrading'
HEADERS = {
    'User-Agent':  'StopTrading buckkeely@gmail.com',
    'Accept':      'application/json',
}
LOOKBACK_DAYS = 45   # max disclosure lag under STOCK Act


class CongressAgent:

    def __init__(self, config):
        self.config    = config
        self._lock     = threading.Lock()
        self.running   = False
        self._buys     = defaultdict(list)   # ticker -> [{member, date, amount}, ...]
        self._sells    = defaultdict(list)   # ticker -> [{member, date, amount}, ...]
        self._recent   = []                  # last 100 trades
        self._last_fetch = None

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
                self._fetch()
            except Exception:
                pass
            time.sleep(3600)   # refresh every hour (free tier rate limit)

    # ── Data Fetching ─────────────────────────────────────────────────────────

    def _fetch(self):
        try:
            r = requests.get(API_URL, headers=HEADERS, timeout=12)
            if r.status_code != 200:
                return

            trades = r.json()
            if not isinstance(trades, list):
                return

            cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
            buys   = defaultdict(list)
            sells  = defaultdict(list)
            recent = []

            for trade in trades:
                ticker   = (trade.get('Ticker') or '').upper().strip()
                tx_type  = (trade.get('Transaction') or '').lower()
                member   = trade.get('Representative') or trade.get('Senator') or 'Unknown'
                tx_date  = trade.get('Date') or trade.get('TransactionDate') or ''
                amount   = trade.get('Range') or trade.get('Amount') or ''

                if not ticker or len(ticker) > 5:
                    continue

                try:
                    tx_dt = date.fromisoformat(tx_date[:10]) if tx_date else date.today()
                except ValueError:
                    tx_dt = date.today()

                if tx_dt < cutoff:
                    continue

                entry = {
                    'ticker':  ticker,
                    'member':  member,
                    'date':    tx_dt.isoformat(),
                    'amount':  amount,
                    'type':    tx_type,
                }

                if 'purchase' in tx_type or 'buy' in tx_type:
                    buys[ticker].append(entry)
                elif 'sale' in tx_type or 'sell' in tx_type:
                    sells[ticker].append(entry)

                recent.append(entry)

            if len(recent) > 0:
                with self._lock:
                    self._buys       = buys
                    self._sells      = sells
                    self._recent     = recent[-100:]
                    self._last_fetch = datetime.now()

        except Exception:
            pass

    # ── Public API ────────────────────────────────────────────────────────────

    def get_score(self, ticker):
        """Congress buy signal score for ticker. 0-20."""
        ticker = ticker.upper()
        with self._lock:
            buys  = len(self._buys.get(ticker, []))
            sells = len(self._sells.get(ticker, []))

        if buys == 0:
            return max(0, -5 if sells > 0 else 0)
        if buys == 1:   return 6
        if buys == 2:   return 12
        return 20       # cluster buy across multiple members

    def get_buying_tickers(self, min_members=1):
        """Tickers being bought by congress — universe expansion signal."""
        with self._lock:
            return [
                {'ticker': t, 'members': len(e), 'trades': e}
                for t, e in self._buys.items()
                if len(e) >= min_members
            ]

    def get_recent(self, limit=30):
        with self._lock:
            return self._recent[-limit:]

    def get_summary(self):
        with self._lock:
            return {
                'buy_tickers':  dict(self._buys),
                'sell_tickers': dict(self._sells),
                'cluster_buys': [t for t, e in self._buys.items() if len(e) >= 2],
                'recent':       self._recent[-20:],
                'last_fetch':   self._last_fetch.isoformat() if self._last_fetch else None,
                'lookback_days': LOOKBACK_DAYS,
            }
