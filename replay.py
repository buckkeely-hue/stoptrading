#!/usr/bin/env python3
"""
StopTrading Market Replay
─────────────────────────
Downloads real 1-minute bar data from the last trading session and plays it
back through the live AutoPilot algorithm at configurable speed.

Usage:
    python3 replay.py                      # 60× speed (~6.5 min for full day)
    python3 replay.py --speed 30           # 30× speed (~13 min)
    python3 replay.py --speed 120          # 120× speed (~3 min)
    python3 replay.py --date 2026-05-22    # specific date
    python3 replay.py --balance 500        # starting balance
    python3 replay.py --symbols SNDL,MVIS  # override universe
"""

import sys, os, time, argparse, threading
from datetime import datetime, timedelta, date, timezone
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

# ── CLI ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description='StopTrading market replay')
parser.add_argument('--speed',   type=float, default=60.0,
                    help='Replay speed multiplier (default 60×)')
parser.add_argument('--date',    type=str,   default=None,
                    help='YYYY-MM-DD to replay (default: most recent trading day)')
parser.add_argument('--balance', type=float, default=500.0,
                    help='Starting paper balance (default $500)')
parser.add_argument('--symbols', type=str,   default=None,
                    help='Comma-separated symbol override instead of universe')
parser.add_argument('--daily-limit', type=float, default=None,
                    help='Daily spend cap in dollars (default: from config)')
parser.add_argument('--seed-model', action='store_true',
                    help='Persist predictor training (warm-up mode) instead of ephemeral')
parser.add_argument('--min-rvol', type=float, default=None,
                    help='Override the candidate vol_ratio gate (sweep/measurement)')
parser.add_argument('--min-change', type=float, default=None,
                    help='Override the candidate change_1h%% gate (sweep/measurement)')
parser.add_argument('--refresh-cache', action='store_true',
                    help='Ignore any cached bars for this date and re-download from yfinance')
args = parser.parse_args()

SPEED     = args.speed
START_BAL = args.balance


def _last_trading_day():
    d = date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


if args.date:
    replay_date = datetime.strptime(args.date, '%Y-%m-%d').date()
else:
    replay_date = _last_trading_day()

DATE_STR  = replay_date.strftime('%Y-%m-%d')
DATE_NEXT = (replay_date + timedelta(days=1)).strftime('%Y-%m-%d')
DATE_PREV = (replay_date - timedelta(days=5)).strftime('%Y-%m-%d')

_OPEN  = datetime.combine(replay_date, datetime.strptime('09:30', '%H:%M').time())
_CLOSE = datetime.combine(replay_date, datetime.strptime('16:00', '%H:%M').time())
_TOTAL_MINS = int((_CLOSE - _OPEN).total_seconds() / 60)

print(f'\n{"="*64}')
print(f'  StopTrading Market Replay — {DATE_STR}')
print(f'  Speed   : {SPEED:.0f}×  ({_TOTAL_MINS / SPEED:.1f} real-minutes for full session)')
print(f'  Balance : ${START_BAL:.2f}')
print(f'{"="*64}\n')

# ── Download historical data ──────────────────────────────────────────────────

import numpy as np
import pandas as pd
import yfinance as _yf_real

from modules.harvester import TOP_PENNY_UNIVERSE
from modules.universe import DynamicUniverse as _DU, FALLBACK as _FALLBACK

def _tz_strip(df):
    """Convert timezone-aware DatetimeIndex to naive ET."""
    if df is None or df.empty:
        return df
    try:
        if df.index.tzinfo is not None:
            df = df.copy()
            df.index = df.index.tz_convert('US/Eastern').tz_localize(None)
    except Exception:
        pass
    return df


def _bars_have_signal(df, min_range_pct=0.5):
    """Reject degenerate 1-min data. yfinance rate-limits/serves stale intraday bars on
    repeated or bulk pulls, returning a flat single value (e.g. CMTG pinned at one price all
    day). Flat bars make every triple-barrier resolve at ret=0 → a fake loss that poisons the
    predictor's training corpus. Require enough bars AND real intraday range so only genuine
    price paths feed the model."""
    if df is None or len(df) <= 30 or 'Close' not in df:
        return False
    c = df['Close']
    mean = float(c.mean())
    if mean <= 0:
        return False
    return (float(c.max()) - float(c.min())) / mean * 100.0 >= float(min_range_pct)


_bars_1m = {}        # sym → DataFrame, 1-min bars for replay date
_bars_1h = {}        # sym → DataFrame, 1-hour bars last 2 days (for scan_movers)
_bars_1d = {}        # sym → DataFrame, daily bars last 5 days (for _get_rvol)
_bars_5m_prev = {}   # sym → DataFrame, 5m bars from DATE_PREV up to (not incl.) replay date

# ── Bar-data cache: download each date's validated bars ONCE, then reuse. yfinance intraday
# data is unreliable under repeated/bulk pulls (flat/stale bars that poison training); the
# cache makes warm-up runs deterministic and stops re-triggering rate limits. --refresh-cache
# forces a fresh download.
import pickle
_CACHE_DIR = BASE / 'bar_cache'
_cache_fp  = _CACHE_DIR / f'{DATE_STR}.pkl'
_use_cache = _cache_fp.exists() and not args.refresh_cache

if _use_cache:
    try:
        with open(_cache_fp, 'rb') as _cf:
            _cached = pickle.load(_cf)
        UNIVERSE      = _cached.get('universe', [])
        _bars_1m      = _cached.get('bars_1m', {})
        _bars_1h      = _cached.get('bars_1h', {})
        _bars_1d      = _cached.get('bars_1d', {})
        _bars_5m_prev = _cached.get('bars_5m_prev', {})
        VALID = list(_bars_1m)
        print(f'[Data] Loaded cached bars for {DATE_STR}: {len(VALID)} symbols '
              f'({_cache_fp.name}) — no yfinance calls')
        if not VALID:
            print('[ERROR] Cached bar set is empty — delete the cache file and retry.')
            sys.exit(1)
    except Exception as _ce:
        print(f'[Data] Cache load failed ({_ce}) — re-downloading')
        _use_cache = False

if not _use_cache:
    if args.symbols:
        UNIVERSE = [s.strip().upper() for s in args.symbols.split(',')]
    else:
        # Try to pull a live universe snapshot (no background thread — one sync refresh)
        print('[Data] Fetching live universe from Yahoo/StockTwits/Finviz…')
        try:
            _du = _DU()
            _du._refresh()
            _live = _du.get()
            if len(_live) >= 10:
                UNIVERSE = _live[:80]
                print(f'[Data] Live universe: {len(UNIVERSE)} symbols')
            else:
                raise ValueError('too few')
        except Exception as _e:
            print(f'[Data] Live fetch failed ({_e}) — using fallback list')
            UNIVERSE = list(_FALLBACK)

    print(f'[Data] Downloading 1-min bars for up to {len(UNIVERSE)} symbols on {DATE_STR}…')

    _flat_rejected = []
    for sym in UNIVERSE:
        try:
            t = _yf_real.Ticker(sym)
            m1 = _tz_strip(t.history(start=DATE_STR, end=DATE_NEXT,
                                       interval='1m', prepost=False))
            if _bars_have_signal(m1):
                _bars_1m[sym] = m1
            elif m1 is not None and len(m1) > 30:
                _flat_rejected.append(sym)   # had bars but they're flat → rate-limit artifact
            d1 = _tz_strip(t.history(start=DATE_PREV, end=DATE_NEXT, interval='1d'))
            if not d1.empty:
                _bars_1d[sym] = d1
        except Exception:
            pass

    VALID = list(_bars_1m)
    print(f'[Data] {len(VALID)} symbols with usable 1-min data'
          + (f' ({len(_flat_rejected)} rejected as flat/degenerate — likely yfinance rate-limit)'
             if _flat_rejected else ''))

    if not VALID:
        print('[ERROR] No usable 1-min data — not a trading day, or yfinance is rate-limiting '
              '(all bars flat). Retry later or space out runs.')
        sys.exit(1)

    # 1-hour bars in batch (for scan_movers)
    try:
        h1_date_from = (replay_date - timedelta(days=3)).strftime('%Y-%m-%d')
        h1_raw = _yf_real.download(
            tickers=' '.join(VALID),
            start=h1_date_from, end=DATE_NEXT,
            interval='1h', group_by='ticker',
            auto_adjust=True, progress=False, threads=True, timeout=30,
        )
        for sym in VALID:
            try:
                if len(VALID) == 1:
                    df = h1_raw.dropna()
                else:
                    if sym not in h1_raw.columns.get_level_values(0):
                        continue
                    df = h1_raw[sym].dropna()
                _bars_1h[sym] = _tz_strip(df)
            except Exception:
                pass
    except Exception as e:
        print(f'[Data] 1-hour batch failed: {e} — scan_movers will use 1m data')

    # SPY for market-regime check
    try:
        spy = _tz_strip(_yf_real.Ticker('SPY').history(start=DATE_PREV,
                                                        end=DATE_NEXT, interval='1d'))
        _bars_1d['SPY'] = spy
    except Exception:
        pass

    # Historical 5m bars for prior 5 days — gives scan_movers() the multi-day fallback
    # context it needs for change_5d and average-volume baselines.
    try:
        m5_raw = _yf_real.download(
            tickers=' '.join(VALID),
            start=DATE_PREV, end=DATE_STR,
            interval='5m', group_by='ticker',
            auto_adjust=True, progress=False, threads=True, timeout=30,
        )
        for sym in VALID:
            try:
                if len(VALID) == 1:
                    df = m5_raw.dropna()
                else:
                    if sym not in m5_raw.columns.get_level_values(0):
                        continue
                    df = m5_raw[sym].dropna()
                if not df.empty:
                    _bars_5m_prev[sym] = _tz_strip(df)
            except Exception:
                pass
        print(f'[Data] Historical 5m bars: {len(_bars_5m_prev)} symbols (prior {DATE_PREV}–{DATE_STR})')
    except Exception as e:
        print(f'[Data] Historical 5m batch failed: {e} — early-session signal quality reduced')

    # Persist validated bars so later runs are deterministic and skip yfinance entirely.
    try:
        _CACHE_DIR.mkdir(exist_ok=True)
        with open(_cache_fp, 'wb') as _cf:
            pickle.dump({'universe': VALID, 'bars_1m': _bars_1m, 'bars_1h': _bars_1h,
                         'bars_1d': _bars_1d, 'bars_5m_prev': _bars_5m_prev}, _cf)
        print(f'[Data] Cached bars → {_cache_fp}')
    except Exception as _se:
        print(f'[Data] Cache save failed: {_se}')

print(f'[Data] Bars ready — {len(VALID)} 1m | {len(_bars_1h)} 1h | '
      f'{len(_bars_1d)} daily | {len(_bars_5m_prev)} 5m-hist\n')

# ── Replay clock ──────────────────────────────────────────────────────────────

class ReplayClock:
    """Maps real elapsed time to ET market time at SPEED× acceleration."""

    def __init__(self, speed):
        self._speed      = speed
        self._real_start = time.monotonic()
        self._et_start   = _OPEN
        self._frozen     = None    # when set, now() returns this fixed instant

    def now(self):
        if self._frozen is not None:
            return self._frozen
        elapsed = time.monotonic() - self._real_start
        return self._et_start + timedelta(seconds=elapsed * self._speed)

    def freeze_at(self, t):
        """Pin the clock to market time `t` for the duration of a tick. Heavy per-tick
        work (scans/network) otherwise advances real time, which at high speed drifts the
        sim clock hours forward mid-tick — stamping triple-barrier entries far in the future
        so they never mature and the predictor never trains. Pinning makes a tick an instant."""
        self._frozen = t

    def resume(self, t):
        """Resume free-running from instant `t`, discarding the real time the tick consumed
        (so processing time doesn't leak into sim time and skip ticks)."""
        self._frozen = None
        self._real_start = time.monotonic() - (t - self._et_start).total_seconds() / self._speed

    def done(self):
        return self.now() >= _CLOSE

    def progress_pct(self):
        elapsed = (self.now() - _OPEN).total_seconds() / 60
        return min(100.0, elapsed / _TOTAL_MINS * 100)


_clock = ReplayClock(SPEED)

# ── Fake yfinance ─────────────────────────────────────────────────────────────

def _filter_to(df, cutoff_naive):
    """Return rows whose index <= cutoff_naive."""
    if df is None or df.empty:
        return pd.DataFrame()
    try:
        idx = df.index.tz_localize(None) if df.index.tzinfo else df.index
        return df[idx <= cutoff_naive]
    except Exception:
        return df


class _FakeFastInfo:
    def __init__(self, price, avg_vol=1_000_000):
        self.last_price                  = float(price) if price else 0.0
        self.three_month_average_volume  = avg_vol


class _FakeTicker:
    def __init__(self, symbol):
        self.symbol = symbol.upper().strip()

    def history(self, period=None, interval='1d', **kwargs):
        now = _clock.now()
        sym = self.symbol
        _EMPTY = pd.DataFrame(columns=['Open', 'High', 'Low', 'Close', 'Volume'])
        if interval == '1m':
            df = _filter_to(_bars_1m.get(sym), now)
            return df if not df.empty else _EMPTY
        elif interval == '1h':
            df = _filter_to(_bars_1h.get(sym, _bars_1m.get(sym)), now)
            return df if not df.empty else _EMPTY
        elif interval in ('5m', '2m', '15m', '30m'):
            minutes = int(interval.rstrip('m'))
            src = _bars_1m.get(sym)
            today_resampled = pd.DataFrame(columns=['Open', 'High', 'Low', 'Close', 'Volume'])
            if src is not None and not src.empty:
                filtered = _filter_to(src, now)
                if not filtered.empty:
                    today_resampled = filtered.resample(f'{minutes}min').agg(
                        {'Open': 'first', 'High': 'max', 'Low': 'min',
                         'Close': 'last', 'Volume': 'sum'}
                    ).dropna(subset=['Close'])
            # Prepend historical 5m bars from prior days for multi-day fallback context
            prev = _bars_5m_prev.get(sym)
            if prev is not None and not prev.empty:
                df = pd.concat([prev, today_resampled]).sort_index()
                df = df[~df.index.duplicated(keep='last')]
            else:
                df = today_resampled
            return df if not df.empty else _EMPTY
        else:  # 1d, 5d, etc.
            eod = datetime.combine(replay_date, datetime.max.time())
            df  = _filter_to(_bars_1d.get(sym), eod)
            return df if not df.empty else _EMPTY

    @property
    def fast_info(self):
        now = _clock.now()
        df  = _filter_to(_bars_1m.get(self.symbol), now)
        if df.empty:
            return _FakeFastInfo(0.0)
        price   = float(df['Close'].iloc[-1])
        avg_vol = int(df['Volume'].mean()) * 390
        return _FakeFastInfo(price, avg_vol)

    @property
    def info(self):
        return {'shortPercentOfFloat': 0.0, 'shortRatio': 0.0}

    @property
    def institutional_holders(self):
        return None

    @property
    def recommendations(self):
        return None

    @property
    def news(self):
        return []


class _FakeYF:
    def Ticker(self, symbol):
        return _FakeTicker(symbol)

    def download(self, tickers, period=None, interval='1d',
                 group_by='ticker', **kwargs):
        """Multi-symbol download — used by scan_movers with interval='1h'."""
        now  = _clock.now()
        eod  = datetime.combine(replay_date, datetime.max.time())
        syms = tickers.split() if isinstance(tickers, str) else list(tickers)

        frames = {}
        for sym in syms:
            df = None
            if interval == '1h':
                _h = _bars_1h.get(sym)
                _src = _h if (_h is not None and not _h.empty) else _bars_1m.get(sym)
                df = _filter_to(_src, now)
            elif interval == '1m':
                df = _filter_to(_bars_1m.get(sym), now)
            elif interval in ('5m', '2m', '15m', '30m'):
                minutes = int(interval.rstrip('m'))
                src = _bars_1m.get(sym)
                today_df = pd.DataFrame(columns=['Open', 'High', 'Low', 'Close', 'Volume'])
                if src is not None and not src.empty:
                    filtered = _filter_to(src, now)
                    if not filtered.empty:
                        today_df = filtered.resample(f'{minutes}min').agg(
                            {'Open': 'first', 'High': 'max', 'Low': 'min',
                             'Close': 'last', 'Volume': 'sum'}
                        ).dropna(subset=['Close'])
                prev = _bars_5m_prev.get(sym)
                if prev is not None and not prev.empty:
                    combined = pd.concat([prev, today_df]).sort_index()
                    df = combined[~combined.index.duplicated(keep='last')]
                else:
                    df = today_df
            else:
                df = _filter_to(_bars_1d.get(sym), eod)
            if df is not None and not df.empty:
                frames[sym] = df

        if not frames:
            return pd.DataFrame()
        if len(syms) == 1:
            return frames.get(syms[0], pd.DataFrame())
        return pd.concat(frames, axis=1)


_fake_yf = _FakeYF()


class _FakeMarketData:
    """Replay-mode drop-in for MarketData — reads from pre-downloaded bars, no network calls."""

    def __init__(self, config=None):
        pass

    @property
    def _key(self):
        return ''   # disables real Polygon REST calls in scan filters

    def Ticker(self, symbol):
        return _FakeTicker(symbol.upper().strip())

    def download(self, tickers, period=None, interval='1d',
                 group_by='ticker', auto_adjust=True,
                 progress=False, threads=True, timeout=20, **kwargs):
        return _fake_yf.download(tickers, period=period, interval=interval,
                                 group_by=group_by)

    def last_price(self, symbol, allow_stale=True):
        # allow_stale accepted for parity with real MarketData; replay bars are always
        # the simulated-clock price, so there's no stale-vs-fresh distinction here.
        return _FakeTicker(symbol).fast_info.last_price

    # Internal methods called by harvester helpers
    def _history(self, symbol, period, interval):
        return _FakeTicker(symbol).history(period=period, interval=interval)

    def _fetch_bars(self, symbol, period, interval):
        return _FakeTicker(symbol).history(period=period, interval=interval)

    def _fetch_last_price(self, symbol):
        return _FakeTicker(symbol).fast_info.last_price

    def _avg_vol(self, symbol):
        try:
            eod = datetime.combine(replay_date, datetime.max.time())
            df  = _filter_to(_bars_1d.get(symbol.upper()), eod)
            if df is not None and not df.empty and 'Volume' in df.columns:
                return int(df['Volume'].mean())
        except Exception:
            pass
        return 1_000_000

    def _info(self, symbol):
        return {'floatShares': 0, 'sharesOutstanding': 0,
                'shortPercentOfFloat': 0, 'shortRatio': 0}

    def _news(self, symbol):
        return []


# Patch MarketData before importing any trading module so every
# MarketData(config) call inside those modules gets _FakeMarketData.
import modules.market_data as _md_mod
_md_mod.MarketData = _FakeMarketData

# ── Patch the harvester module ────────────────────────────────────────────────

import modules.harvester as _hmod

# Keep yf stub for any remaining direct references (safe no-op now)
_hmod.yf = _fake_yf

# Replace _get_session to use replay clock instead of real wall clock
def _replay_session():
    now = _clock.now()
    m   = now.hour * 60 + now.minute
    if m < 9 * 60 + 30:   return 'PRE_MARKET'
    if m < 10 * 60 + 30:  return 'OPEN_MOMENTUM'
    if m < 12 * 60:        return 'STANDARD'
    if m < 14 * 60:        return 'DEAD_ZONE'
    if m < 15 * 60 + 30:  return 'AFTERNOON'
    if m < 16 * 60:        return 'CLOSE'
    return 'OVERNIGHT'

_hmod._get_session = _replay_session

# ── Boot the trading engine ───────────────────────────────────────────────────

from config import load_config
from modules.paper_trade import PaperTrader
from modules.harvester import StockHarvester, AutoPilot

config = load_config()

# Replay overrides: T+1 settlement and daily limits don't apply in intraday simulation.
# cash_account_mode=False: sell proceeds return to cash immediately (no T+1 unsettled queue).
# daily_spend_limit: default to 80% of starting balance for realistic multi-trade coverage.
config = dict(config)
config['cash_account_mode'] = False
config['per_trade_capital']  = 150.0   # $150/trade → ~3 positions on $500/day
# Gate overrides for sweeps/measurement (apply to both session types)
if args.min_rvol is not None:
    config['min_rvol_regular'] = args.min_rvol; config['min_rvol_momentum'] = args.min_rvol
if args.min_change is not None:
    config['min_change_1h_regular'] = args.min_change; config['min_change_1h_momentum'] = args.min_change
if args.daily_limit is None:
    config['daily_spend_limit'] = START_BAL  # 100% deployable — no artificial cap

# Fresh paper trader — no disk writes during replay
paper = PaperTrader(config)
paper._save = lambda: None   # prevent overwriting real paper_trade.json
paper._state = {
    'balance':   START_BAL,
    'positions': {},
    'history':   [],
}

# Stub universe exposing only symbols we have data for
class _ReplayUniverse:
    def get(self): return list(VALID)

harvester = StockHarvester(paper, config, universe=_ReplayUniverse())
# Don't start harvester's background monitor (would make real API calls)

# Stub SignalAggregator.score_symbol during replay — historical Reddit/EDGAR data unavailable
# Return a passing score so only the real gates (momentum, RVOL, spread, VWAP) filter candidates
from modules.signals import SignalAggregator as _SA
_SA.score_symbol = lambda self, symbol, price: {
    'composite_score': 50, 'positive_count': 4, 'consensus': 4,
    'signal': 'WATCH', 'signal_color': '#60a5fa',
    'data_sources': 0, 'breakdown': {},
}

autopilot = AutoPilot(harvester, paper, config)
autopilot._save  = lambda: None   # prevent writing autopilot.json
# Use an ephemeral predictive model during replay — train on the replayed tape but never
# persist, so a backtest can't pollute the live predictor_model.json. EXCEPT in --seed-model
# warm-up mode, where we deliberately accumulate labeled outcomes across many past days.
try:
    autopilot.predictor._persist = bool(args.seed_model)
    if args.seed_model:
        autopilot.predictor._src = 'replay'      # delay-immune training data
        autopilot.decision_log._src = 'replay'
except Exception:
    pass

# Initialize all state that start() would set up, without launching the thread
autopilot.running              = True
autopilot.daily_spent          = 0.0
autopilot.daily_date           = datetime.now().strftime('%Y-%m-%d')  # match real date → no day-reset
autopilot.stats                = {
    'started': _OPEN.isoformat(),
    'wins': 0, 'losses': 0,
    'total_trades': 0, 'total_harvests': 0,
    'total_profit': 0.0, 'total_win_pct': 0.0, 'total_loss_pct': 0.0,
}
autopilot.log                  = []
autopilot._position_harvests   = {}
autopilot._position_hwm        = {}
autopilot._position_opened     = {}
autopilot._position_entry_rvol = {}
autopilot._momentum_cache      = {}
autopilot._daily_start_balance = START_BAL
autopilot._consecutive_losses  = 0
autopilot._daily_pnl_floor_hit = False
autopilot._gapped_symbols      = set()
autopilot._gap_date            = DATE_STR
autopilot._day_trades_log      = []   # clear real PDT history — fresh account for replay
autopilot._pdt_last_log        = 0.0
autopilot._cascade_strikes     = {}   # fresh cascade cooloff state

# Patch _get_et_now to return replay clock time, not real wall clock.
# Without this, the hard hour gate (now_et.hour >= 16) blocks all buys
# when replay is run after 4 PM ET.
autopilot._get_et_now = lambda: _clock.now()

# Patch scan_movers' date filter to use replay date instead of datetime.now().date().
# Without this, today_bars filter looks for May 30 rows in May 29 bar data → falls to
# hist.tail(78) daily bars, losing intraday 5m signal quality.
harvester._get_today_date = lambda: replay_date

# Pre-populate pre-market gapper watchlist using real pre-market data for the replay date.
# In production this runs automatically during the PRE_MARKET session; in replay we start
# at 9:30 (OPEN) and would miss it.  Fetch once here so gap bonuses apply from tick 1.
print('[Replay] Fetching pre-market gapper data for {}…'.format(DATE_STR))
try:
    _pm_gappers = []
    for _sym in VALID[:60]:
        try:
            t = _yf_real.Ticker(_sym)
            pm = t.history(start=DATE_STR, end=DATE_NEXT, interval='1m', prepost=True)
            if pm is None or pm.empty:
                continue
            # Filter to pre-market bars (before 9:30 ET)
            if pm.index.tzinfo:
                pm.index = pm.index.tz_convert('US/Eastern')
            pre = pm[pm.index.time < __import__('datetime').time(9, 30)]
            if pre.empty or int(pre['Volume'].sum()) < 50_000:
                continue
            # Prior close from daily bars
            hist = _bars_1d.get(_sym)
            if hist is None or len(hist) < 2:
                continue
            prev_close = float(hist['Close'].iloc[-2])
            pm_price   = float(pre['Close'].iloc[-1])
            pm_vol     = int(pre['Volume'].sum())
            gap_pct    = (pm_price - prev_close) / prev_close * 100 if prev_close > 0 else 0
            if gap_pct >= 4.0:   # lower threshold than live (8%) — catches moderately gapped
                _pm_gappers.append({
                    'symbol':          _sym,
                    'gap_pct':         round(gap_pct, 2),
                    'premarket_price': round(pm_price, 4),
                    'prev_close':      round(prev_close, 4),
                    'premarket_vol':   pm_vol,
                })
        except Exception:
            continue
    _pm_gappers.sort(key=lambda x: x['gap_pct'], reverse=True)
    if _pm_gappers:
        autopilot._premarket_watchlist = _pm_gappers
        autopilot._premarket_date = DATE_STR
        print('[Replay] Pre-market gappers: {} found (top: {})'.format(
            len(_pm_gappers),
            ', '.join('{} +{:.1f}%'.format(g['symbol'], g['gap_pct']) for g in _pm_gappers[:5])))
    else:
        print('[Replay] No pre-market gappers found for {}'.format(DATE_STR))
except Exception as _e:
    print(f'[Replay] Pre-market fetch failed: {_e}')
print()

# Inject daily limit override
if args.daily_limit is not None:
    autopilot.config = dict(autopilot.config)
    autopilot.config['daily_spend_limit'] = args.daily_limit
    harvester.config = autopilot.config
    print(f'[Config] Daily spend limit overridden → ${args.daily_limit:.2f}\n')

# ── Diagnostic: show why scan_movers may not produce candidates ───────────────

print('[Diag] Running scan_movers diagnostic…')
_diag_movers = harvester.scan_movers()
print(f'[Diag] scan_movers returned {len(_diag_movers)} symbols total')
if _diag_movers:
    print('[Diag] Top 10 from scan_movers:')
    for _m in _diag_movers[:10]:
        _fail = []
        if _m['price'] <= 0 or _m['price'] >= 5:
            _fail.append(f"price={_m['price']:.4f} (need <$5)")
        if _m['change_1h'] <= -1.0:
            _fail.append(f"change_1h={_m['change_1h']:.2f}% (need >-1%)")
        if _m['vol_ratio'] <= 1.2:
            _fail.append(f"vol_ratio={_m['vol_ratio']:.2f}x (need >1.2x)")
        status = '✗ FILTERED: ' + ', '.join(_fail) if _fail else '✓ PASSES'
        print(f'  {_m["symbol"]:6s}  ${_m["price"]:.4f}  1h:{_m["change_1h"]:+.1f}%  '
              f'rvol:{_m["vol_ratio"]:.2f}x  score:{_m["score"]:.0f}  {status}')
else:
    print('[Diag] scan_movers returned 0 results — checking raw 1h bar availability:')
    _chk_syms = VALID[:5]
    for _s in _chk_syms:
        _df = _bars_1h.get(_s) or _bars_1m.get(_s)
        if _df is not None and not _df.empty:
            print(f'  {_s}: {len(_df)} bars, last={_df.index[-1]}, close={float(_df["Close"].iloc[-1]):.4f}')
        else:
            print(f'  {_s}: NO bars')
print()

# ── Competitor Baseline: canonical Holly AI / ORB gap-and-go ─────────────────
#
# Strategy used by Trade Ideas (Holly), Warrior Trading bots, TrendSpider gap-and-go:
#   1. Capture first-5m candle (ORB) at 9:36 ET
#   2. Buy first breakout above ORB high on any universe symbol
#   3. Target = ORB high + 2×(ORB high – ORB low)  → 2:1 risk/reward
#   4. Hard stop at ORB low
#   5. No harvesting, no momentum scoring, no cascade filter, no multi-source intel
#   6. Close all open positions at 15:30 ET
#   Same starting balance, same per-trade sizing, same universe, same price data.

_dbg_skip = {'no_bars': 0, 'no_first': 0, 'bad_price': 0}   # ORB-capture skip counters

class CompetitorBaseline:

    def __init__(self, balance, per_trade=150.0, max_pos=3):
        self._balance       = float(balance)
        self._start_balance = float(balance)
        self._per_trade     = per_trade
        self._max_pos       = max_pos
        self._positions     = {}   # sym → {shares, entry, stop, target}
        self._orb           = {}   # sym → {high, low}
        self._orb_captured  = False
        self._closed        = set()  # symbols already exited today — no re-entry
        self._daily_spent   = 0.0
        self.log            = []
        self.stats          = {'trades': 0, 'wins': 0, 'losses': 0, 'total_profit': 0.0}

    def _capture_orb(self):
        open_dt  = datetime.combine(replay_date, datetime.strptime('09:30', '%H:%M').time())
        close_dt = datetime.combine(replay_date, datetime.strptime('09:35', '%H:%M').time())
        for sym in VALID:
            bars = _bars_1m.get(sym)
            if bars is None or bars.empty:
                _dbg_skip['no_bars'] += 1
                continue
            idx = bars.index.tz_localize(None) if bars.index.tzinfo else bars.index
            first = bars[(idx >= open_dt) & (idx < close_dt)]
            if len(first) < 1:
                _dbg_skip['no_first'] += 1
                continue
            h = float(first['High'].max())
            l = float(first['Low'].min())
            if h <= 0 or l <= 0 or h < 0.10 or h > 8.0:
                _dbg_skip['bad_price'] += 1
                continue
            self._orb[sym] = {'high': h, 'low': l}

    def tick(self, now_et):
        m = now_et.hour * 60 + now_et.minute

        # Capture ORB levels once at 9:36
        if m >= 9 * 60 + 36 and not self._orb_captured:
            self._capture_orb()
            self._orb_captured = True
            self.log.append({'time': now_et.strftime('%H:%M'), 'action': 'ORB',
                             'note': 'ORB levels captured for {} symbols'.format(len(self._orb))})

        # Force-close all positions at 15:30
        if m >= 15 * 60 + 30:
            for sym in list(self._positions.keys()):
                price = _FakeTicker(sym).fast_info.last_price or self._positions[sym]['entry']
                self._exit(sym, price, 'CLOSE', now_et)
            return

        # Check stop/target on open positions every tick
        for sym in list(self._positions.keys()):
            pos   = self._positions[sym]
            price = _FakeTicker(sym).fast_info.last_price
            if price <= 0:
                continue
            if price <= pos['stop']:
                self._exit(sym, price, 'STOP', now_et)
            elif price >= pos['target']:
                self._exit(sym, price, 'TARGET', now_et)

        # New entries only before 12:00 ET (Holly AI style — open momentum + standard session)
        if m >= 12 * 60:
            return
        if len(self._positions) >= self._max_pos:
            return
        if self._daily_spent >= self._start_balance:
            return

        # Find best ORB breakout candidate not already held or exited
        best = None
        best_score = 0.0
        for sym in VALID:
            if sym in self._positions or sym in self._closed:
                continue
            orb = self._orb.get(sym)
            if not orb:
                continue
            price = _FakeTicker(sym).fast_info.last_price
            if price <= 0 or not (0.10 <= price <= 8.0):
                continue
            if price <= orb['high'] * 1.005:   # must clear ORB high + 0.5% buffer
                continue
            # Score = breakout strength × gap priority
            gap_bonus = next((g['gap_pct'] for g in _pm_gappers
                              if g['symbol'] == sym), 0.0)
            score = (price / orb['high'] - 1) * 100 + gap_bonus * 0.5
            if score > best_score:
                best_score = score
                best = (sym, price, orb)

        if best is None:
            return
        sym, price, orb = best
        shares = max(1, int(self._per_trade / price))
        cost   = shares * price
        if cost > self._balance:
            return

        risk   = max(orb['high'] - orb['low'], 0.01)
        target = round(orb['high'] + 2.0 * risk, 4)
        stop   = round(orb['low'], 4)

        self._balance     -= cost
        self._daily_spent += cost
        self._positions[sym] = {'shares': shares, 'entry': price,
                                'stop': stop, 'target': target}
        self.stats['trades'] += 1
        self.log.append({'time': now_et.strftime('%H:%M'), 'action': 'BUY',
                         'symbol': sym, 'shares': shares, 'price': round(price, 4),
                         'target': target, 'stop': stop,
                         'note': '{} {} sh @ ${:.4f} | tgt ${:.4f} | stop ${:.4f}'.format(
                             sym, shares, price, target, stop)})

    def _exit(self, sym, price, reason, now_et):
        pos = self._positions.pop(sym, None)
        if pos is None:
            return
        self._closed.add(sym)
        proceeds = pos['shares'] * price
        pnl      = (price - pos['entry']) * pos['shares']
        self._balance         += proceeds
        self.stats['total_profit'] += pnl
        if pnl >= 0:
            self.stats['wins'] += 1
        else:
            self.stats['losses'] += 1
        self.log.append({'time': now_et.strftime('%H:%M'), 'action': reason,
                         'symbol': sym, 'shares': pos['shares'], 'price': round(price, 4),
                         'pnl': round(pnl, 2),
                         'note': '{} {} sh @ ${:.4f} | P&L ${:+.2f} [{}]'.format(
                             sym, pos['shares'], price, pnl, reason)})

    def get_mtm(self):
        mtm = self._balance
        for sym, pos in self._positions.items():
            px   = _FakeTicker(sym).fast_info.last_price or pos['entry']
            mtm += pos['shares'] * px
        return mtm

    def get_vault(self):
        return 0.0   # competitor has no vault/profit-lock feature


_competitor = CompetitorBaseline(START_BAL, per_trade=150.0, max_pos=3)
_comp_log_len = 0

# ── Reset replay clock — all setup above consumed real time; anchor to NOW ───
# Without this, data downloads + diagnostics eat into the simulated session.
_clock._real_start = time.monotonic()

# ── Replay loop ───────────────────────────────────────────────────────────────

# Each real tick = 5 market-minutes of simulated time
TICK_MARKET_MINS = 5
TICK_REAL_SECS   = (TICK_MARKET_MINS * 60) / SPEED

_ICONS = {
    'BUY':        '🟢', 'SELL':      '🔴', 'HARVEST':   '💰',
    'REINVEST':   '🔄', 'TRAIL-STOP':'⛔', 'TIME-STOP': '⏱',
    'FINAL-EXIT': '🏁', 'EVASIVE':   '⚠ ', 'HALT':      '🛑',
    'GAP-EXIT':   '📉', 'SCAN':      '🔍', 'SESSION':   '🕐',
    'SKIP':       ' ─', 'ERROR':     '❌', 'SYSTEM':    '⚙ ',
    'CATALYST':   '⚡', 'SPREAD':    '📊',
}

prev_log_len = 0
last_snapshot_minute = -1

print(f'[Replay] ▶  {_OPEN.strftime("%H:%M")} – {_CLOSE.strftime("%H:%M")} ET  '
      f'|  tick every {TICK_MARKET_MINS}m market  ({TICK_REAL_SECS:.1f}s real)\n')

while not _clock.done():
    now_et = _clock.now()
    elapsed_market_mins = (now_et - _OPEN).total_seconds() / 60

    # Fire a tick every 5 market-minutes
    tick_number = int(elapsed_market_mins // TICK_MARKET_MINS)
    if tick_number != getattr(_clock, '_last_tick', -1):
        _clock._last_tick = tick_number

        # Pin the clock to this tick's exact market time so per-tick processing can't drift
        # it (see ReplayClock.freeze_at). Resume from the same instant afterward.
        _tick_et = _OPEN + timedelta(minutes=tick_number * TICK_MARKET_MINS)
        _clock.freeze_at(_tick_et)
        autopilot._momentum_cache = {}   # flush per-tick cache
        try:
            autopilot._tick()
        except Exception as e:
            print(f'  [{now_et.strftime("%H:%M")}] ❌ tick error: {e}')
        finally:
            _clock.resume(_tick_et)
        now_et = _tick_et

        # Tick competitor strategy (same cadence)
        try:
            _competitor.tick(now_et)
        except Exception as e:
            print(f'  [{now_et.strftime("%H:%M")}] ❌ competitor tick error: {e}')

        # Print new log entries — our system
        new_entries = autopilot.log[prev_log_len:]
        prev_log_len = len(autopilot.log)
        for e in new_entries:
            action = e.get('action', '')
            note   = e.get('note', '')
            # Suppress only pure status noise; always show anything decision-related
            _quiet = ('SCAN', 'SESSION', 'SYSTEM')
            _decision = ('BUY', 'SELL', 'HARVEST', 'HALT', 'SKIP', 'LIMIT', 'PDT',
                         'REGIME', 'SCORE-GATE', 'VWAP', 'ENTRY-SKIP', 'SPREAD',
                         'EARNINGS', 'CATALYST', 'ORB', 'BEHAVIORAL', 'SQUEEZE',
                         'INSIDER', 'CONGRESS', 'SCAN')
            if action in _quiet and action not in _decision:
                if not any(x in note for x in ('found', 'No qual', 'blocked', 'DEAD', 'CLOSE')):
                    continue
            icon = _ICONS.get(action, '  ')
            print(f'  [{now_et.strftime("%H:%M")}] {icon} [{action:12s}]  {note[:80]}')

        # Print new competitor log entries
        new_comp = _competitor.log[_comp_log_len:]
        _comp_log_len = len(_competitor.log)
        for e in new_comp:
            action = e.get('action', '')
            note   = e.get('note', e.get('note', ''))
            if action == 'ORB':
                continue   # suppress ORB capture noise
            _comp_icons = {'BUY': '🟦', 'STOP': '🟥', 'TARGET': '🎯', 'CLOSE': '🔲'}
            icon = _comp_icons.get(action, '  ')
            print(f'  [{now_et.strftime("%H:%M")}] {icon} [COMP-{action:8s}]  {note[:80]}')

    # Balance snapshot every 30 market-minutes
    current_minute = int(elapsed_market_mins)
    if current_minute // 30 != last_snapshot_minute:
        last_snapshot_minute = current_minute // 30
        state  = paper.get_state()
        bal    = state.get('balance', 0)
        pos    = state.get('positions', [])
        if isinstance(pos, dict):
            pos = list(pos.values())

        mtm = bal
        for p in pos:
            sym = p.get('symbol') or p.get('Symbol', '')
            px  = _FakeTicker(sym).fast_info.last_price
            mtm += int(p.get('shares', p.get('Shares', 0))) * px

        pnl   = mtm - START_BAL
        pct   = pnl / START_BAL * 100
        prog  = _clock.progress_pct()
        print(f'\n  ── {now_et.strftime("%H:%M")} ET  [{prog:.0f}% done]  '
              f'Cash: ${bal:.2f}  MTM: ${mtm:.2f}  P&L: {pnl:+.2f} ({pct:+.1f}%)  '
              f'Pos: {len(pos)} ──\n')

    time.sleep(max(0.02, TICK_REAL_SECS / 10))

# ── Final report ──────────────────────────────────────────────────────────────

state     = paper.get_state()
balance   = state.get('balance', 0)
positions = state.get('positions', [])
history   = state.get('history', [])
if isinstance(positions, dict):
    positions = list(positions.values())

# Mark open positions to market (EOD price)
mtm = balance
pos_report = []
for p in positions:
    sym   = p.get('symbol') or p.get('Symbol', '')
    cost  = float(p.get('avg_cost', p.get('Price', 0)))
    shrs  = int(p.get('shares', p.get('Shares', 0)))
    px    = _FakeTicker(sym).fast_info.last_price or cost
    gain  = (px - cost) / cost * 100 if cost else 0
    upnl  = (px - cost) * shrs
    mtm  += shrs * px
    pos_report.append((sym, shrs, cost, px, gain, upnl))

stats = autopilot.stats
wins  = stats.get('wins', 0)
loss  = stats.get('losses', 0)
total = max(wins + loss, 1)
pnl   = mtm - START_BAL

# ── Competitor final mark-to-market (force-close any residual positions) ───────
comp_mtm = _competitor.get_mtm()
comp_pnl   = comp_mtm - START_BAL
comp_stats = _competitor.stats
comp_wins  = comp_stats['wins']
comp_loss  = comp_stats['losses']
comp_total = max(comp_wins + comp_loss, 1)
vault_bal  = autopilot._vault_balance

# ── Side-by-side comparison report ────────────────────────────────────────────
W = 30   # column width

print('\n' + '═' * 68)
print('  REPLAY COMPARISON REPORT  —  ' + DATE_STR)
print(f'  {_OPEN.strftime("%H:%M")} – {_CLOSE.strftime("%H:%M")} ET  |  $500/day  |  $150/trade')
print('═' * 68)
print(f'  {"":28s}  {"StopTrading":>14s}  {"Competitor ORB":>14s}')
print(f'  {"":28s}  {"(this system)":>14s}  {"(Holly AI style)":>14s}')
print('  ' + '─' * 64)
print(f'  {"Starting balance":28s}  ${START_BAL:>13.2f}  ${START_BAL:>13.2f}')
print(f'  {"Ending cash":28s}  ${balance:>13.2f}  ${_competitor._balance:>13.2f}')
print(f'  {"Mark-to-market":28s}  ${mtm:>13.2f}  ${comp_mtm:>13.2f}')
print(f'  {"Total P&L":28s}  ${pnl:>+13.2f}  ${comp_pnl:>+13.2f}')
print(f'  {"P&L %":28s}  {pnl/START_BAL*100:>+12.1f}%  {comp_pnl/START_BAL*100:>+12.1f}%')
print(f'  {"Vault (locked profit)":28s}  ${vault_bal:>13.2f}  {"N/A":>14s}')
_our_combined = pnl + vault_bal
print(f'  {"Combined (P&L + vault)":28s}  ${_our_combined:>+13.2f}  ${comp_pnl:>+13.2f}')
print('  ' + '─' * 64)
print(f'  {"Trades":28s}  {stats.get("total_trades",0):>14d}  {comp_stats["trades"]:>14d}')
print(f'  {"Harvests":28s}  {stats.get("total_harvests",0):>14d}  {"0":>14s}')
print(f'  {"Wins":28s}  {wins:>14d}  {comp_wins:>14d}')
print(f'  {"Losses":28s}  {loss:>14d}  {comp_loss:>14d}')
print(f'  {"Win rate":28s}  {wins/total*100:>13.0f}%  {comp_wins/comp_total*100:>13.0f}%')
print(f'  {"Realized P&L":28s}  ${stats.get("total_profit",0):>+13.2f}  ${comp_stats["total_profit"]:>+13.2f}')
print(f'  {"Capital deployed":28s}  ${autopilot.daily_spent:>13.2f}  ${_competitor._daily_spent:>13.2f}')
print('  ' + '─' * 64)
_edge = _our_combined - comp_pnl
_edge_pct = _edge / START_BAL * 100
_winner = 'StopTrading' if _edge >= 0 else 'Competitor'
print(f'  {"Edge (combined vs competitor)":28s}  ${_edge:>+13.2f}  ({_edge_pct:+.1f}%)')
print(f'  {"Winner":28s}  {_winner:>28s}')
print('═' * 68)

# ── Our system detail ──────────────────────────────────────────────────────────
if pos_report:
    print(f'\n  StopTrading — open positions at {_CLOSE.strftime("%H:%M")}:')
    for sym, shrs, cost, px, gain, upnl in pos_report:
        print(f'    {sym:6s}  {shrs}sh @ ${cost:.4f} → ${px:.4f}'
              f'  {gain:+.1f}%  (${upnl:+.2f} unrealized)')

if history:
    print(f'\n  StopTrading — trade history ({len(history)} events):')
    for h in history[-20:]:
        action  = h.get('action', '?')
        sym     = h.get('symbol', '?')
        shrs    = h.get('shares', '')
        px      = h.get('price', 0)
        pnl_h   = h.get('pnl', '')
        t       = h.get('time', '')[:16]
        pnl_str = f'  pnl=${pnl_h:+.2f}' if isinstance(pnl_h, (int, float)) else ''
        print(f'    {t}  {action:5s}  {sym:6s}  {shrs}sh @ ${float(px):.4f}{pnl_str}')

# ── Competitor detail ──────────────────────────────────────────────────────────
comp_trades = [e for e in _competitor.log if e.get('action') in ('BUY','STOP','TARGET','CLOSE')]
if comp_trades:
    print(f'\n  Competitor ORB — trade history ({len(comp_trades)} events):')
    for e in comp_trades:
        action  = e.get('action', '?')
        sym     = e.get('symbol', '?')
        shrs    = e.get('shares', '')
        px      = e.get('price', 0)
        pnl_e   = e.get('pnl', '')
        t       = e.get('time', '')
        pnl_str = f'  pnl=${pnl_e:+.2f}' if isinstance(pnl_e, (int, float)) else ''
        print(f'    {t}  {action:6s}  {sym:6s}  {shrs}sh @ ${float(px):.4f}{pnl_str}')

print()
print('═' * 68)
print(f'  Universe: {len(VALID)} symbols  |  Speed: {SPEED:.0f}×')
print('═' * 68 + '\n')
