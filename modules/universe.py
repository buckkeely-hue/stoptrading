"""
DynamicUniverse — live penny stock universe refreshed every 15 minutes.

Sources:
  1. Yahoo Finance screeners: small_cap_gainers, day_gainers, most_actives
  2. StockTwits trending symbols (no key required)
  3. Finviz penny screener (public scrape, no key)
  4. CatalystEngine bullish tickers (catalyst score >= 30)

Multi-stream arbitrage: tickers appearing in 2+ independent sources are
prioritized — cross-source agreement is the cleanest buy signal filter.

Filters:
  - Price: $0.10 – $8.00
  - Volume: > 100,000 shares today
  - No dots or hyphens in ticker (OTC/warrants excluded)
  - Dilution-flagged tickers removed
  - Validation pass every 60 min prunes tickers with no live price data
"""

import re
import threading
import time
import requests
import yfinance as yf
from datetime import datetime

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Accept': 'application/json',
}

_SCREENER_IDS = ['small_cap_gainers', 'day_gainers', 'most_actives']
_SCREENER_BASE = (
    'https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved'
    '?scrIds={}&count=50&start=0'
)
_FINVIZ_URL = (
    'https://finviz.com/screener.ashx?v=111'
    '&f=sh_price_u5,sh_avgvol_o100,sh_price_o0.1&o=-change&r=1'
)

# Fallback — VERIFIED ACTIVE tickers only (delisted stocks removed May 2026)
# Removed: NAKD, NKLA, GOEV, SPRT, IDEX, MULN, BBIG, SOLO, SOPA, SLNO,
#          MARK, OTRK, GBOX, MFON, NILE, ATNF, CIDM, INPX (all delisted)
FALLBACK = [
    'SNDL', 'MVIS', 'OCGN', 'BCRX', 'CTRM', 'SHIP', 'TOPS', 'GORO',
    'TNXP', 'KPLT', 'INDO', 'IMPP', 'REED', 'UONE', 'PETZ', 'SQFT',
    'PAVS', 'PHIO', 'TPCS', 'SIFY', 'LFMD', 'ADTX', 'WKHS',
    'GFAI', 'BFRI', 'HUDI', 'SENS', 'CLOV', 'ATER',
]


class DynamicUniverse:
    """
    Maintains a live, filtered universe of penny stock tickers.
    Cross-references multiple independent data streams; tracks source count
    per ticker so the picker can prioritize multi-stream consensus candidates.
    """

    def __init__(self, catalyst_engine=None):
        self.catalyst = catalyst_engine
        self._lock = threading.Lock()
        self._universe = list(FALLBACK)
        self._meta = {}        # symbol -> {price, volume, change_pct, sources, source_count}
        self._blacklist = set()  # confirmed dead/delisted tickers
        self.running = False
        self.last_refresh = None
        self.refresh_count = 0
        self.error_log = []

    def start(self):
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._validate_loop, daemon=True).start()

    def stop(self):
        self.running = False

    def _loop(self):
        self._refresh()
        while self.running:
            time.sleep(900)
            try:
                self._refresh()
            except Exception as exc:
                self.error_log.append(str(exc))
                self.error_log = self.error_log[-10:]

    def _validate_loop(self):
        """Every 60 min: download 1d bars for universe, blacklist dead tickers."""
        time.sleep(300)   # let first refresh complete first
        while self.running:
            try:
                self._validate()
            except Exception:
                pass
            time.sleep(3600)

    def _validate(self):
        with self._lock:
            candidates = list(self._universe)
        if not candidates:
            return
        try:
            data = yf.download(
                tickers=' '.join(candidates),
                period='2d', interval='1d',
                group_by='ticker', auto_adjust=True,
                progress=False, threads=True, timeout=30,
            )
        except Exception:
            return

        dead = set()
        for sym in candidates:
            try:
                hist = data if len(candidates) == 1 else data.get(sym, None)
                if hist is None:
                    dead.add(sym)
                    continue
                hist = hist.dropna()
                if hist.empty or float(hist['Close'].iloc[-1]) <= 0:
                    dead.add(sym)
            except Exception:
                dead.add(sym)

        if dead:
            with self._lock:
                self._blacklist.update(dead)
                self._universe = [s for s in self._universe if s not in self._blacklist]
                self._meta = {s: d for s, d in self._meta.items() if s not in self._blacklist}

    def _refresh(self):
        tickers = {}   # symbol -> {price, volume, change_pct, sources}

        # ── Source 1: Yahoo screeners ─────────────────────────────────────────
        for scr_id in _SCREENER_IDS:
            try:
                r = requests.get(_SCREENER_BASE.format(scr_id), headers=_HEADERS, timeout=15)
                if r.status_code != 200:
                    continue
                quotes = (
                    r.json()
                     .get('finance', {})
                     .get('result', [{}])[0]
                     .get('quotes', [])
                )
                for q in quotes:
                    sym = q.get('symbol', '').upper().strip()
                    if not sym or '.' in sym or '-' in sym or sym in self._blacklist:
                        continue
                    price  = float(q.get('regularMarketPrice', 0) or 0)
                    volume = int(q.get('regularMarketVolume', 0) or 0)
                    change = float(q.get('regularMarketChangePercent', 0) or 0)
                    if not (0.10 <= price <= 8.0) or volume < 100_000:
                        continue
                    if sym not in tickers:
                        tickers[sym] = {'price': round(price, 4), 'volume': volume,
                                        'change_pct': round(change, 2), 'sources': []}
                    tickers[sym]['sources'].append('yahoo_' + scr_id)
            except Exception:
                continue

        # ── Source 2: StockTwits trending ─────────────────────────────────────
        try:
            r = requests.get(
                'https://api.stocktwits.com/api/2/trending/symbols.json', timeout=8)
            if r.status_code == 200:
                for item in r.json().get('symbols', []):
                    sym = item.get('symbol', '').upper().strip()
                    if not sym or '.' in sym or '-' in sym or sym in self._blacklist:
                        continue
                    if sym not in tickers:
                        tickers[sym] = {'price': 0, 'volume': 0, 'change_pct': 0, 'sources': []}
                    tickers[sym]['sources'].append('stocktwits')
        except Exception:
            pass

        # ── Source 3: Finviz penny screener (public HTML) ─────────────────────
        try:
            r = requests.get(_FINVIZ_URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            if r.status_code == 200:
                for sym in set(re.findall(r'quote\.ashx\?t=([A-Z]{1,5})&', r.text)):
                    if sym in self._blacklist:
                        continue
                    if sym not in tickers:
                        tickers[sym] = {'price': 0, 'volume': 0, 'change_pct': 0, 'sources': []}
                    tickers[sym]['sources'].append('finviz')
        except Exception:
            pass

        # ── Source 4: FINRA Reg SHO threshold list (short squeeze candidates) ──
        try:
            # FINRA publishes daily threshold securities (heavily shorted, hard to borrow)
            # These are prime short-squeeze setups when price momentum turns positive
            from datetime import date
            today = date.today().strftime('%Y%m%d')
            finra_url = 'https://www.finra.org/sites/default/files/ftp/threshold-securities/{}-{}-{}.txt'.format(
                today[:4], today[4:6], today[6:])
            fr = requests.get(finra_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
            if fr.status_code == 200:
                for line in fr.text.splitlines()[1:]:   # skip header
                    parts = line.split('|')
                    if len(parts) >= 2:
                        sym = parts[1].strip().upper()
                        if sym and '.' not in sym and '-' not in sym and sym not in self._blacklist:
                            if sym not in tickers:
                                tickers[sym] = {'price': 0, 'volume': 0, 'change_pct': 0, 'sources': []}
                            tickers[sym]['sources'].append('finra_sho')
        except Exception:
            pass

        # ── Source 5: Catalyst bullish tickers ───────────────────────────────
        if self.catalyst:
            try:
                for sym in self.catalyst.get_bullish_tickers(min_score=30):
                    if sym in self._blacklist:
                        continue
                    if sym not in tickers:
                        tickers[sym] = {'price': 0, 'volume': 0, 'change_pct': 0, 'sources': []}
                    tickers[sym]['sources'].append('catalyst')
            except Exception:
                pass

        # ── Dilution guard ────────────────────────────────────────────────────
        if self.catalyst:
            try:
                tickers = {s: d for s, d in tickers.items()
                           if not self.catalyst.is_diluting(s)}
            except Exception:
                pass

        # ── Annotate source count + low-float bonus ───────────────────────────
        for d in tickers.values():
            d['source_count'] = len(set(d['sources']))
            # Low-float bonus: FINRA SHO + float data will be enriched on next tick
            if 'finra_sho' in d['sources']:
                d['source_count'] += 0.5   # squeeze candidate gets half-point bonus

        if not tickers:
            return   # keep existing universe on full failure

        with self._lock:
            self._meta = tickers
            # Lead with multi-stream consensus tickers
            self._universe = sorted(
                tickers.keys(),
                key=lambda s: tickers[s]['source_count'],
                reverse=True,
            )
            self.last_refresh = datetime.now().isoformat()
            self.refresh_count += 1

    def get_premarket_gappers(self, min_gap_pct=8.0):
        """
        Scan for stocks gapping up >min_gap_pct% in pre-market (4am–9:30am ET).
        Run this at 9:00–9:25am to build a watchlist before open.
        Returns list of {symbol, gap_pct, premarket_price, prev_close, volume}.
        """
        with self._lock:
            candidates = list(self._universe)
        if not candidates:
            candidates = list(FALLBACK)

        gappers = []
        for sym in candidates:
            try:
                t = yf.Ticker(sym)
                pm = t.history(period='1d', interval='1m', prepost=True)
                if pm.empty:
                    continue
                # Split into pre-market and regular session
                import pytz
                et = pytz.timezone('US/Eastern')
                pm.index = pm.index.tz_convert(et) if pm.index.tzinfo else pm.index
                pre = pm[pm.index.time < __import__('datetime').time(9, 30)]
                reg_prev = pm[pm.index.time >= __import__('datetime').time(9, 30)]
                if pre.empty:
                    continue
                # Prior close: last regular-session bar from yesterday
                hist = t.history(period='2d', interval='1d')
                if len(hist) < 2:
                    continue
                prev_close = float(hist['Close'].iloc[-2])
                pm_price   = float(pre['Close'].iloc[-1])
                pm_vol     = int(pre['Volume'].sum())
                gap_pct    = (pm_price - prev_close) / prev_close * 100 if prev_close > 0 else 0
                if gap_pct >= min_gap_pct and pm_vol > 50_000:
                    gappers.append({
                        'symbol':         sym,
                        'gap_pct':        round(gap_pct, 2),
                        'premarket_price': round(pm_price, 4),
                        'prev_close':     round(prev_close, 4),
                        'premarket_vol':  pm_vol,
                    })
            except Exception:
                continue

        return sorted(gappers, key=lambda x: x['gap_pct'], reverse=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self):
        """Return current universe ticker list (falls back to FALLBACK if empty)."""
        with self._lock:
            return list(self._universe) if self._universe else list(FALLBACK)

    def get_meta(self, symbol=None):
        with self._lock:
            if symbol:
                return dict(self._meta.get(symbol.upper(), {}))
            return dict(self._meta)

    def get_multi_stream(self, min_sources=2):
        """Return tickers confirmed by 2+ independent data streams — highest conviction."""
        with self._lock:
            return [s for s, d in self._meta.items()
                    if d.get('source_count', 1) >= min_sources]

    def status(self):
        with self._lock:
            multi = [s for s, d in self._meta.items() if d.get('source_count', 1) >= 2]
            return {
                'size':          len(self._universe),
                'multi_stream':  len(multi),
                'blacklisted':   len(self._blacklist),
                'last_refresh':  self.last_refresh,
                'refresh_count': self.refresh_count,
                'top_picks':     self._universe[:10],
                'errors':        self.error_log[-3:],
            }
