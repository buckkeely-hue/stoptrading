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

_bars_1m = {}   # sym → DataFrame, 1-min bars for replay date
_bars_1h = {}   # sym → DataFrame, 1-hour bars last 2 days (for scan_movers)
_bars_1d = {}   # sym → DataFrame, daily bars last 5 days (for _get_rvol)


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


for sym in UNIVERSE:
    try:
        t = _yf_real.Ticker(sym)
        m1 = _tz_strip(t.history(start=DATE_STR, end=DATE_NEXT,
                                   interval='1m', prepost=False))
        if not m1.empty and len(m1) > 30:
            _bars_1m[sym] = m1
        d1 = _tz_strip(t.history(start=DATE_PREV, end=DATE_NEXT, interval='1d'))
        if not d1.empty:
            _bars_1d[sym] = d1
    except Exception:
        pass

VALID = [s for s in _bars_1m if len(_bars_1m[s]) > 30]
print(f'[Data] {len(VALID)} symbols with 1-min data')

if not VALID:
    print('[ERROR] No 1-min data downloaded — is this a trading day?')
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

print(f'[Data] Download complete — {len(VALID)} 1m | {len(_bars_1h)} 1h | '
      f'{len(_bars_1d)} daily\n')

# ── Replay clock ──────────────────────────────────────────────────────────────

class ReplayClock:
    """Maps real elapsed time to ET market time at SPEED× acceleration."""

    def __init__(self, speed):
        self._speed      = speed
        self._real_start = time.monotonic()
        self._et_start   = _OPEN

    def now(self):
        elapsed = time.monotonic() - self._real_start
        return self._et_start + timedelta(seconds=elapsed * self._speed)

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
        if interval == '1m':
            df = _filter_to(_bars_1m.get(sym), now)
            return df if not df.empty else pd.DataFrame(
                columns=['Open', 'High', 'Low', 'Close', 'Volume'])
        elif interval == '1h':
            df = _filter_to(_bars_1h.get(sym, _bars_1m.get(sym)), now)
            return df if not df.empty else pd.DataFrame(
                columns=['Open', 'High', 'Low', 'Close', 'Volume'])
        else:  # 1d, 5d, etc.
            eod = datetime.combine(replay_date, datetime.max.time())
            df  = _filter_to(_bars_1d.get(sym), eod)
            return df if not df.empty else pd.DataFrame(
                columns=['Open', 'High', 'Low', 'Close', 'Volume'])

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
            if interval == '1h':
                _h = _bars_1h.get(sym)
                _src = _h if (_h is not None and not _h.empty) else _bars_1m.get(sym)
                df = _filter_to(_src, now)
            elif interval == '1m':
                df = _filter_to(_bars_1m.get(sym), now)
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

# ── Patch the harvester module ────────────────────────────────────────────────

import modules.harvester as _hmod

# Replace yfinance
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

autopilot = AutoPilot(harvester, paper, config)
autopilot._save  = lambda: None   # prevent writing autopilot.json

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

        autopilot._momentum_cache = {}   # flush per-tick cache
        try:
            autopilot._tick()
        except Exception as e:
            print(f'  [{now_et.strftime("%H:%M")}] ❌ tick error: {e}')

        # Print new log entries
        new_entries = autopilot.log[prev_log_len:]
        prev_log_len = len(autopilot.log)
        for e in new_entries:
            action = e.get('action', '')
            note   = e.get('note', '')
            # Skip noisy scan/session lines unless they led somewhere
            if action in ('SCAN', 'SESSION') and not any(
                    x in note for x in ('found', 'No qual', 'blocked', 'DEAD', 'CLOSE')):
                continue
            icon = _ICONS.get(action, '  ')
            print(f'  [{now_et.strftime("%H:%M")}] {icon} [{action:12s}]  {note[:72]}')

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

print('\n' + '═' * 64)
print('  REPLAY FINAL REPORT')
print(f'  {DATE_STR}  {_OPEN.strftime("%H:%M")} – {_CLOSE.strftime("%H:%M")} ET')
print('═' * 64)
print(f'  Starting balance :  ${START_BAL:.2f}')
print(f'  Ending cash      :  ${balance:.2f}')
print(f'  Mark-to-market   :  ${mtm:.2f}')
print(f'  Total P&L        :  ${pnl:+.2f}  ({pnl / START_BAL * 100:+.1f}%)')
print()
print(f'  Trades executed  :  {stats.get("total_trades", 0)}')
print(f'  Harvests         :  {stats.get("total_harvests", 0)}')
print(f'  Wins             :  {wins}')
print(f'  Losses           :  {loss}')
print(f'  Win rate         :  {wins / total * 100:.0f}%')
print(f'  Realized P&L     :  ${stats.get("total_profit", 0):+.2f}')
print(f'  Daily spent      :  ${autopilot.daily_spent:.2f}')
print(f'  Consec losses    :  {autopilot._consecutive_losses}')
print(f'  Floor tripped    :  {autopilot._daily_pnl_floor_hit}')

if pos_report:
    print(f'\n  Open positions at {_CLOSE.strftime("%H:%M")} close:')
    for sym, shrs, cost, px, gain, upnl in pos_report:
        print(f'    {sym:6s}  {shrs}sh @ ${cost:.4f} → ${px:.4f}'
              f'  {gain:+.1f}%  (${upnl:+.2f} unrealized)')

if history:
    print(f'\n  Trade history ({len(history)} events):')
    for h in history[-30:]:
        action = h.get('action', '?')
        sym    = h.get('symbol', '?')
        shrs   = h.get('shares', '')
        px     = h.get('price', 0)
        pnl_h  = h.get('pnl', '')
        t      = h.get('time', '')[:16]
        pnl_str = f'  pnl=${pnl_h:+.2f}' if isinstance(pnl_h, (int, float)) else ''
        print(f'    {t}  {action:5s}  {sym:6s}  {shrs}sh @ ${float(px):.4f}{pnl_str}')

print()
print('═' * 64)
print(f'  Universe: {len(VALID)} symbols  |  Speed: {SPEED:.0f}×')
print('═' * 64 + '\n')
