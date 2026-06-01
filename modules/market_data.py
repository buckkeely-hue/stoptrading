"""
Unified market data adapter — Polygon.io → yfinance fallback.

Priority chain for every data type:
  1. Polygon.io REST (if polygon_key configured) — real-time, stable, full OTC coverage
  2. yfinance — free, 15-min delayed, unofficial scraper (fragile)

Usage — drop-in yfinance replacement:
    from modules.market_data import MarketData
    md = MarketData(config)
    price = md.Ticker('CNDT').fast_info.last_price
    df    = md.download('CNDT MVIS', period='2d', interval='1h')
"""

import time
import threading
from datetime import date, timedelta, datetime

import requests
import pandas as pd
import yfinance as _yf

_POLY_BASE  = 'https://api.polygon.io'
_POLY_HDRS  = {'User-Agent': 'StopTrading/1.0 buckkeely@gmail.com'}

_PERIOD_DAYS = {
    '1d': 1, '2d': 2, '3d': 3, '5d': 5, '7d': 7,
    '14d': 14, '30d': 30, '60d': 60, '90d': 90,
    '1mo': 30, '3mo': 90, '6mo': 180, '1y': 365,
}
_INTERVAL_POLY = {
    '1m': (1, 'minute'),  '2m': (2, 'minute'),  '5m': (5, 'minute'),
    '15m': (15, 'minute'), '30m': (30, 'minute'),
    '1h': (1, 'hour'),    '2h': (2, 'hour'),    '4h': (4, 'hour'),
    '1d': (1, 'day'),     '1wk': (1, 'week'),
}


def _period_to_dates(period):
    days = _PERIOD_DAYS.get(period, 5)
    end   = date.today()
    start = end - timedelta(days=days + 2)   # +2 buffer for weekends
    return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')


def _poly_bars_to_df(results):
    """Convert Polygon aggs result list to a yfinance-style OHLCV DataFrame."""
    if not results:
        return pd.DataFrame(columns=['Open', 'High', 'Low', 'Close', 'Volume'])
    df = pd.DataFrame(results)
    df['t'] = pd.to_datetime(df['t'], unit='ms', utc=True)
    df = df.rename(columns={'o': 'Open', 'h': 'High', 'l': 'Low', 'c': 'Close', 'v': 'Volume'})
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].set_index(df['t'])
    df.index.name = None
    return df


class _FakeFastInfo:
    def __init__(self, price, avg_vol=1_000_000):
        self.last_price                 = float(price) if price else 0.0
        self.three_month_average_volume = avg_vol


class MarketDataTicker:
    """Wraps a symbol for per-ticker queries — mirrors yfinance.Ticker interface."""

    def __init__(self, symbol, md):
        self.symbol = symbol.upper().strip()
        self._md    = md
        self._info_cache    = None
        self._info_cache_ts = 0

    def history(self, period='1mo', interval='1d', **kwargs):
        return self._md._history(self.symbol, period, interval)

    @property
    def fast_info(self):
        price   = self._md.last_price(self.symbol)
        avg_vol = self._md._avg_vol(self.symbol)
        return _FakeFastInfo(price, avg_vol)

    @property
    def info(self):
        if self._info_cache and time.time() - self._info_cache_ts < 14400:
            return self._info_cache
        result = self._md._info(self.symbol)
        self._info_cache    = result
        self._info_cache_ts = time.time()
        return result

    @property
    def news(self):
        return self._md._news(self.symbol)

    @property
    def institutional_holders(self):
        try:
            return _yf.Ticker(self.symbol).institutional_holders
        except Exception:
            return None

    @property
    def recommendations(self):
        try:
            return _yf.Ticker(self.symbol).recommendations
        except Exception:
            return None


class MarketData:
    """
    Unified market data layer.
    Configure polygon_key in settings to enable real-time data.
    Without it, transparently falls back to yfinance.
    """

    def __init__(self, config=None):
        self.config   = config or {}
        self._lock    = threading.Lock()
        self._cache   = {}    # (symbol, period, interval) → (df, timestamp)
        self._price_c = {}    # symbol → (price, timestamp)
        self._info_c  = {}    # symbol → (info_dict, timestamp)

    @property
    def _key(self):
        return self.config.get('polygon_key', '') or self.config.get('polygon_api_key', '')

    # ── Public API ────────────────────────────────────────────────────────────

    def Ticker(self, symbol):
        return MarketDataTicker(symbol, self)

    def download(self, tickers, period='1mo', interval='1d',
                 group_by='ticker', auto_adjust=True,
                 progress=False, threads=True, timeout=20, **kwargs):
        """Batch download — mirrors yfinance.download() signature."""
        if isinstance(tickers, str):
            sym_list = tickers.split()
        else:
            sym_list = list(tickers)

        if len(sym_list) == 1:
            return self._history(sym_list[0], period, interval)

        frames = {}
        for sym in sym_list:
            df = self._history(sym, period, interval)
            if not df.empty:
                frames[sym] = df

        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=1, keys=list(frames.keys()))

    def last_price(self, symbol, allow_stale=True):
        """Real-time last trade price (Polygon) or latest close (yfinance).

        When allow_stale=False, a price that could only be sourced from the PRIOR day's
        close (no trade/day data today) returns 0 instead — so stops/exits never transact
        on yesterday's price. Callers that only mark/display can keep allow_stale=True.
        """
        key = symbol.upper()
        with self._lock:
            cached = self._price_c.get(key)
            if cached and (time.time() - cached[1]) < 15:
                price, _, is_stale = cached
                return 0.0 if (is_stale and not allow_stale) else price

        price, is_stale = self._fetch_last_price(key)
        with self._lock:
            self._price_c[key] = (price, time.time(), is_stale)
        return 0.0 if (is_stale and not allow_stale) else price

    # ── Internal fetchers ─────────────────────────────────────────────────────

    def _history(self, symbol, period, interval):
        cache_key = (symbol, period, interval)
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and (time.time() - cached[1]) < 60:
                return cached[0]

        df = self._fetch_bars(symbol, period, interval)
        with self._lock:
            self._cache[cache_key] = (df, time.time())
        return df

    def _fetch_bars(self, symbol, period, interval):
        key = self._key
        if key:
            try:
                mult, span = _INTERVAL_POLY.get(interval, (1, 'day'))
                from_dt, to_dt = _period_to_dates(period)
                url = ('{}/v2/aggs/ticker/{}/range/{}/{}/{}/{}?adjusted=true'
                       '&sort=asc&limit=5000&apiKey={}').format(
                    _POLY_BASE, symbol, mult, span, from_dt, to_dt, key)
                r = requests.get(url, headers=_POLY_HDRS, timeout=15)
                if r.status_code == 200:
                    results = r.json().get('results', [])
                    if results:
                        return _poly_bars_to_df(results)
            except Exception:
                pass

        # yfinance fallback
        try:
            return _yf.Ticker(symbol).history(period=period, interval=interval,
                                               auto_adjust=True)
        except Exception:
            return pd.DataFrame(columns=['Open', 'High', 'Low', 'Close', 'Volume'])

    def _fetch_last_price(self, symbol):
        """Returns (price, is_stale). is_stale=True means the only price available was
        the prior day's close (no live trade/day data) — not safe to transact on."""
        key = self._key
        if key:
            try:
                url = '{}/v2/snapshot/locale/us/markets/stocks/tickers/{}?apiKey={}'.format(
                    _POLY_BASE, symbol, key)
                r = requests.get(url, headers=_POLY_HDRS, timeout=8)
                if r.status_code == 200:
                    snap = r.json().get('ticker', {})
                    # Today's trade or today's bar close = fresh; prevDay close = stale.
                    fresh = (snap.get('lastTrade', {}).get('p') or
                             snap.get('day', {}).get('c') or 0)
                    if fresh:
                        return float(fresh), False
                    prev = snap.get('prevDay', {}).get('c') or 0
                    if prev:
                        return float(prev), True
            except Exception:
                pass

        try:
            info = _yf.Ticker(symbol).fast_info
            return float(info.last_price or 0), False
        except Exception:
            return 0.0, False

    def _avg_vol(self, symbol):
        try:
            df = self._history(symbol, '30d', '1d')
            if not df.empty and 'Volume' in df.columns:
                return int(df['Volume'].mean())
        except Exception:
            pass
        return 1_000_000

    def _info(self, symbol):
        key = self._key
        result = {}

        if key:
            try:
                url = '{}/v3/reference/tickers/{}?apiKey={}'.format(_POLY_BASE, symbol, key)
                r = requests.get(url, headers=_POLY_HDRS, timeout=8)
                if r.status_code == 200:
                    d = r.json().get('results', {})
                    result['floatShares']       = d.get('share_class_shares_outstanding',
                                                         d.get('weighted_shares_outstanding', 0))
                    result['sharesOutstanding'] = d.get('weighted_shares_outstanding', 0)
                    result['marketCap']         = d.get('market_cap', 0)
                    result['sector']            = d.get('sic_description', '')
                    result['industry']          = d.get('sic_description', '')
            except Exception:
                pass

        # Merge yfinance info for fields Polygon doesn't cover
        try:
            yf_info = _yf.Ticker(symbol).info or {}
            for k in ('shortPercentOfFloat', 'shortRatio', 'beta',
                      'trailingPE', 'forwardPE', 'dividendYield'):
                result.setdefault(k, yf_info.get(k, 0))
            result.setdefault('floatShares', yf_info.get('floatShares', 0))
            result.setdefault('sharesOutstanding', yf_info.get('sharesOutstanding', 0))
        except Exception:
            pass

        return result

    def _news(self, symbol):
        key = self._key
        benzinga_key = self.config.get('benzinga_key', '')

        # Benzinga first (fastest human-curated)
        if benzinga_key:
            try:
                url = ('https://api.benzinga.com/api/v2/news?token={}'
                       '&tickers={}&pageSize=5&displayOutput=headline').format(
                    benzinga_key, symbol)
                r = requests.get(url, timeout=8)
                if r.status_code == 200:
                    articles = r.json() if isinstance(r.json(), list) else r.json().get('news', [])
                    if articles:
                        return [{'title': a.get('title', ''),
                                 'providerPublishTime': 0,
                                 'publisher': 'Benzinga'} for a in articles[:5]]
            except Exception:
                pass

        # Polygon news
        if key:
            try:
                url = '{}/v2/reference/news?ticker={}&limit=5&apiKey={}'.format(
                    _POLY_BASE, symbol, key)
                r = requests.get(url, timeout=8)
                if r.status_code == 200:
                    arts = r.json().get('results', [])
                    if arts:
                        return [{'title': a.get('title', ''),
                                 'providerPublishTime': 0,
                                 'publisher': a.get('publisher', {}).get('name', 'Polygon')}
                                for a in arts]
            except Exception:
                pass

        # yfinance fallback
        try:
            return _yf.Ticker(symbol).news or []
        except Exception:
            return []
