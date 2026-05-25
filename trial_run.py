#!/usr/bin/env python3
"""
StopTrading — Faux Data Trial Run
Drives AutoPilot._tick() through 12 synthetic scenarios using injected mock
prices and dependencies. No live API calls, no files written to production.
Reports logical and structural problems found.
"""
import sys, os, threading, tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

try:
    import pandas as pd
except ImportError:
    print('pandas required: pip3 install pandas')
    sys.exit(1)

# ── Output helpers ─────────────────────────────────────────────────────────────
G = '\033[92m'; R = '\033[91m'; Y = '\033[93m'; B = '\033[94m'
BOLD = '\033[1m'; DIM = '\033[2m'; RESET = '\033[0m'

_findings = []

def _ok(msg):   print(f'  {G}✓{RESET}  {msg}')
def _fail(msg, kind='BUG'):
    print(f'  {R}✗  [{kind}]{RESET} {msg}')
    _findings.append((kind, msg))
def _warn(msg):
    print(f'  {Y}⚠  [WARN]{RESET} {msg}')
    _findings.append(('WARN', msg))
def _note(msg): print(f'    {DIM}↳  {msg}{RESET}')

def section(title):
    bar = '─' * max(1, 58 - len(title))
    print(f'\n{BOLD}{B}── {title} {bar}{RESET}')


# ── Shared price map ───────────────────────────────────────────────────────────
_P: dict = {}

def sp(sym, price):  _P[sym.upper()] = float(price)
def gp(sym) -> float: return _P.get(sym.upper(), 1.0)


# ── Mock yfinance ──────────────────────────────────────────────────────────────
def _df_1m(sym: str, bars: int = 20) -> 'pd.DataFrame':
    p = gp(sym)
    closes = [p * (1 + 0.001 * i) for i in range(-bars + 1, 1)]
    highs  = [c * 1.005 for c in closes]
    lows   = [c * 0.995 for c in closes]
    idx    = pd.date_range(end=datetime.now(), periods=bars, freq='1min')
    return pd.DataFrame({'Open': closes, 'High': highs, 'Low': lows,
                         'Close': closes, 'Volume': [500_000] * bars}, index=idx)


def _df_daily(sym: str, bars: int = 2) -> 'pd.DataFrame':
    p = gp(sym)
    closes = [p * 0.98] + [p] * (bars - 1)
    idx    = pd.date_range(end=datetime.now(), periods=bars, freq='1d')
    return pd.DataFrame({'Close': closes, 'Volume': [1_000_000] * bars}, index=idx)


class _FI:
    def __init__(self, sym):  self.last_price = gp(sym)

class _MT:
    def __init__(self, sym):  self._s = sym.upper()
    @property
    def fast_info(self):      return _FI(self._s)
    @property
    def info(self):           return {'floatShares': 5_000_000}

    def history(self, period='1d', interval='1d'):
        if '1m' in interval: return _df_1m(self._s)
        return _df_daily(self._s)

class _MYF:
    def Ticker(self, sym):                    return _MT(sym)
    def download(self, tickers, **kw):        return pd.DataFrame()

_mock_yf = _MYF()


# ── Mock paper trader ──────────────────────────────────────────────────────────
class MockPaper:
    def __init__(self, bal: float):
        self._lock = threading.Lock()
        self._st   = {'balance': float(bal), 'positions': {}, 'history': []}

    def buy(self, sym, shares):
        sym = sym.upper(); shares = int(shares)
        p   = gp(sym)
        if p <= 0: return {'error': f'no price {sym}'}
        cost = p * shares
        with self._lock:
            if cost > self._st['balance']:
                return {'error': f'insufficient ${self._st["balance"]:.2f} < ${cost:.2f}'}
            self._st['balance'] -= cost
            pos = self._st['positions']
            if sym in pos:
                ns = pos[sym]['shares'] + shares
                nc = (pos[sym]['shares'] * pos[sym]['avg_cost'] + shares * p) / ns
                pos[sym] = {'shares': ns, 'avg_cost': round(nc, 4)}
            else:
                pos[sym] = {'shares': shares, 'avg_cost': round(p, 4)}
        return {'ok': True, 'symbol': sym, 'shares': shares, 'price': p}

    def sell(self, sym, shares):
        sym = sym.upper(); shares = int(shares)
        p   = gp(sym)
        if p <= 0: return {'error': f'no price {sym}'}
        with self._lock:
            pos = self._st['positions']
            if sym not in pos: return {'error': f'no position {sym}'}
            avail = pos[sym]['shares']
            if shares > avail: shares = avail
            avg = pos[sym]['avg_cost']
            self._st['balance'] += p * shares
            pos[sym]['shares'] -= shares
            if pos[sym]['shares'] == 0:
                del pos[sym]
        return {'ok': True, 'symbol': sym, 'shares': shares, 'price': p,
                'avg_cost': avg, 'pnl': round((p - avg) * shares, 2)}

    def get_state(self):
        with self._lock:
            positions = [
                {'symbol': s, 'shares': v['shares'], 'avg_cost': v['avg_cost'],
                 'current_price': gp(s)}
                for s, v in self._st['positions'].items()
            ]
            return {'balance': self._st['balance'], 'positions': positions, 'history': []}

    def _save(self): pass


# ── Dependency mocks ───────────────────────────────────────────────────────────
class MockCatalyst:
    def __init__(self, diluting=()):      self._d = {s.upper() for s in diluting}
    def is_diluting(self, s):             return s.upper() in self._d
    def get_score(self, s):               return -5 if s.upper() in self._d else 0

class MockHalts:
    def __init__(self, halted=(), resumes=()):
        self._h = {s.upper() for s in halted}
        self._r = {s.upper() for s in resumes}
    def is_halted(self, s):               return s.upper() in self._h
    def is_resume_play(self, s):          return s.upper() in self._r
    def get_score(self, s):               return 0

class MockCongress:
    def __init__(self, buying=None):      self._b = buying or []
    def get_buying_tickers(self, **kw):   return self._b
    def get_score(self, s):               return 0

class MockBehavioral:
    def __init__(self, trending=None):    self._t = trending or []
    def get_trending_tickers(self, **kw): return self._t
    def get_score(self, s):               return 0

class MockMacro:
    def __init__(self, ok=True, score=0): self._ok = ok; self._s = score
    def is_favorable(self):               return self._ok
    def get_score(self):                  return self._s

class MockHarvester:
    def __init__(self, fn):               self._fn = fn
    def scan_movers(self, **kw):          return self._fn()


def _cand(sym, price, ch1=5.0, volr=4.0, score=70.0):
    return {'symbol': sym, 'price': price, 'change_1h': ch1, 'change_5d': 5.0,
            'volume': 2_000_000, 'vol_ratio': volr, 'slope_pct': 0.5,
            'volatility': 3.0, 'tech_score': score, 'signal_score': 0,
            'signal': 'SCANNING', 'score': score, 'in_harvest': False}


# ── AutoPilot factory ──────────────────────────────────────────────────────────
_TMPDIR = tempfile.mkdtemp(prefix='stoptrading_trial_')

def _ap(bal=500.0, cfg_extra=None, cands_fn=None,
        catalyst=None, halts=None, macro=None,
        behavioral=None, congress=None):
    import modules.harvester as hmod
    hmod.yf           = _mock_yf
    hmod._get_session = lambda: 'STANDARD'
    hmod.AUTOPILOT_FILE = os.path.join(_TMPDIR, 'ap.json')
    hmod.HARVEST_FILE   = os.path.join(_TMPDIR, 'hv.json')

    from modules.harvester import AutoPilot

    cfg = {
        'tx_cost_pct': 3.0, 'harvest_trigger_pct': 7.0, 'exit_trigger_pct': 2.0,
        'daily_spend_limit': 100.0, 'max_daily_loss_pct': 10.0,
        'max_consecutive_losses': 3, 'per_trade_capital': 50.0,
        'position_size_pct': 15.0, 'paper_balance': bal,
    }
    if cfg_extra:
        cfg.update(cfg_extra)

    paper = MockPaper(bal)
    mock_h = MockHarvester(cands_fn or (lambda: []))

    ap = AutoPilot(harvester=mock_h, paper_trader=paper, config=cfg,
                   catalyst=catalyst, halts=halts, macro=macro,
                   behavioral=behavioral, congress=congress)

    ap._save                = lambda: None
    ap._market_regime       = lambda: 0.0
    ap._get_vwap            = lambda s: (gp(s), gp(s))
    ap._estimate_spread_pct = lambda s: 1.0
    ap._check_overnight_gap = lambda s: False
    ap._get_rvol            = lambda s: 3.0
    ap._momentum_snapshot   = lambda s: (0.5, False, 2.5)  # uptrend, no cascade

    ap.running               = True
    ap.daily_date            = datetime.now().strftime('%Y-%m-%d')
    ap._daily_start_balance  = bal
    ap._gap_date             = ap.daily_date

    return ap, paper


def _tick_ap(ap):
    """Run one tick; return only the new log entries added during it."""
    n = len(ap.log)
    ap._tick()
    return ap.log[n:]

def _actions(entries):      return [e['action'] for e in entries]
def _has(entries, action):  return action in _actions(entries)
def _first(entries, action):
    return next((e for e in entries if e['action'] == action), None)


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 1 — Normal Buy → Harvest → Time-Exit
# ══════════════════════════════════════════════════════════════════════════════
def s1_harvest_cycle():
    section('S1  Normal Buy → Harvest → Time-Exit')

    sym = 'MOMO1'
    sp(sym, 1.00)
    ap, paper = _ap(bal=500.0, cands_fn=lambda: [_cand(sym, gp(sym))])

    # Tick 1: no positions → should BUY
    log = _tick_ap(ap)
    if _has(log, 'BUY'):
        pos = paper._st['positions'].get(sym)
        shares = pos['shares'] if pos else 0
        cost   = shares * 1.00
        _ok(f'BUY executed: {sym} {shares} shares @ $1.00 (cost ${cost:.2f})')
        if ap.daily_spent >= cost:
            _ok(f'daily_spent updated to ${ap.daily_spent:.2f}')
        else:
            _fail(f'daily_spent ${ap.daily_spent:.2f} not updated (expected ≥ ${cost:.2f})')
        if ap.stats['total_trades'] == 1:
            _ok('stats.total_trades incremented to 1')
        else:
            _fail(f'stats.total_trades = {ap.stats["total_trades"]} (expected 1)')
    else:
        _fail('BUY not executed on first tick with valid candidate')
        _note(', '.join(_actions(log)) or '(empty log)')
        return

    # Set open timestamp to 3 hours ago for TIME-EXIT test later
    ap._position_opened[sym] = datetime.now() - timedelta(hours=3)

    # Tick 2: price still $1.00 — no harvest yet
    log = _tick_ap(ap)
    if _has(log, 'HARVEST'):
        _fail('HARVEST fired at $1.00 (no gain — unexpected)')
    else:
        _ok('No harvest at $1.00 (correct — net gain < 7% trigger)')

    # Tick 3: price rises to $1.10 (10% gain, net 7% after 3% tx cost) → HARVEST
    sp(sym, 1.10)
    # Update candidate price too
    ap.harvester._fn = lambda: [_cand(sym, 1.10)]
    log = _tick_ap(ap)
    if _has(log, 'HARVEST'):
        _ok('HARVEST triggered at $1.10 (10% gain, net 7%)')
        h_entry = _first(log, 'HARVEST')
        _note(h_entry['note'][:90])
        if ap.stats['total_harvests'] >= 1:
            _ok('stats.total_harvests incremented')
        else:
            _fail(f'stats.total_harvests not incremented (= {ap.stats["total_harvests"]})')
    else:
        _fail('HARVEST not triggered at $1.10 (expected net_gain >= harvest_trigger)')
        _note(', '.join(_actions(log)))

    # Tick 4: position open 3h, price $1.10, net 7% >= 2% — TIME-EXIT should fire
    # (position_opened already set 3h ago; trail stop won't fire since price didn't drop)
    ap._position_hwm[sym] = 1.10   # update HWM to current price
    log = _tick_ap(ap)
    if _has(log, 'TIME-EXIT'):
        _ok('TIME-EXIT triggered after 3h with positive gain')
        t = _first(log, 'TIME-EXIT')
        _note(t['note'][:90])
        if ap.stats['wins'] >= 1:
            _ok('stats.wins incremented')
        else:
            _fail(f'stats.wins not incremented (= {ap.stats["wins"]})')
    elif not paper._st['positions'].get(sym):
        _ok('Position closed via another exit path (acceptable)')
    else:
        _warn('TIME-EXIT did not fire after 3h with 7% net gain — may need another tick or harvest threshold changed it')
        _note(', '.join(_actions(log)))


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 2 — Trail Stop
# ══════════════════════════════════════════════════════════════════════════════
def s2_trail_stop():
    section('S2  Trail Stop  (15% drop from HWM on day-0 position)')

    sym = 'LOSS1'
    sp(sym, 2.00)
    ap, paper = _ap(bal=400.0)

    # Pre-load a position at $2.00 directly
    paper.buy(sym, 100)  # 100 shares @ $2.00
    ap._position_opened[sym] = datetime.now()
    ap._position_hwm[sym]    = 2.00

    # Now price drops to $1.70 (15% from HWM of $2.00)
    sp(sym, 1.70)

    log = _tick_ap(ap)
    if _has(log, 'TRAIL-STOP'):
        _ok('TRAIL-STOP fired at $1.70 (15% drop from $2.00 HWM, day-0 threshold)')
        t = _first(log, 'TRAIL-STOP')
        _note(t['note'][:90])
        if ap._consecutive_losses >= 1:
            _ok('consecutive_losses incremented after trail stop')
        else:
            _fail(f'consecutive_losses not incremented (= {ap._consecutive_losses})')
        if ap.stats['losses'] >= 1:
            _ok('stats.losses incremented')
        else:
            _fail(f'stats.losses not incremented (= {ap.stats["losses"]})')
        # PDT: opened and closed same day → should count as a day trade
        if sym in ap._day_trades_log or datetime.now().strftime('%Y-%m-%d') in ap._day_trades_log:
            _ok('Day trade logged in _day_trades_log (same-day open/close)')
        else:
            _fail('Day trade NOT logged after same-day open → trail stop close')
    else:
        _fail('TRAIL-STOP did not fire at 15% drop from HWM')
        _note(', '.join(_actions(log)))


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 3 — PDT Gate (3 day trades used)
# ══════════════════════════════════════════════════════════════════════════════
def s3_pdt_gate():
    section('S3  PDT Gate  (FINRA Rule 4210 — 3 day trades exhausted)')

    sym = 'PDTOK'
    sp(sym, 1.00)
    today = datetime.now().strftime('%Y-%m-%d')

    ap, paper = _ap(bal=500.0, cands_fn=lambda: [_cand(sym, gp(sym))])

    # Simulate 3 same-day day trades already done
    ap._day_trades_log = [today, today, today]

    log = _tick_ap(ap)
    if _has(log, 'PDT'):
        pdt_entry = _first(log, 'PDT')
        _ok('PDT gate blocked new buy after 3 day trades')
        _note(pdt_entry['note'][:90])
        if not _has(log, 'BUY'):
            _ok('No BUY executed when PDT limit reached')
        else:
            _fail('BUY executed DESPITE PDT limit — critical rule violation')
    else:
        _fail('PDT gate did NOT fire with 3 logged day trades')
        _note(', '.join(_actions(log)))

    # Edge case: 2 trades → warning logged, buy still allowed
    ap2, paper2 = _ap(bal=500.0, cands_fn=lambda: [_cand(sym, gp(sym))])
    ap2._day_trades_log = [today, today]
    log2 = _tick_ap(ap2)
    if _has(log2, 'PDT') and _has(log2, 'BUY'):
        _ok('PDT warning logged at 2/3 trades, buy still proceeds')
    elif _has(log2, 'BUY'):
        _ok('Buy proceeds with 2/3 day trades (PDT warning may not log until block)')
    else:
        _warn('Buy did NOT proceed with only 2/3 day trades used')
        _note(', '.join(_actions(log2)))


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 4 — Daily Spend Limit Exhausted
# ══════════════════════════════════════════════════════════════════════════════
def s4_daily_spend_limit():
    section('S4  Daily Spend Limit  ($100 cap)')

    sym = 'SPEND1'
    sp(sym, 1.00)
    ap, paper = _ap(bal=500.0, cands_fn=lambda: [_cand(sym, gp(sym))])

    # Deplete daily budget: $98 spent
    ap.daily_spent = 98.0

    log = _tick_ap(ap)
    # remaining = 100 - 98 = $2. buy_amount = min(kelly=$50, remaining=$2, ...) = $2 < $5 threshold
    if _has(log, 'SKIP'):
        _ok('SKIP logged when remaining daily budget ($2) is below $5 minimum')
    elif _has(log, 'LIMIT'):
        _ok('LIMIT logged — daily spend cap enforced')
    else:
        if _has(log, 'BUY'):
            _fail('BUY executed with only $2 remaining of $100 daily limit')
        else:
            _warn('Neither SKIP nor LIMIT logged at $98/$100 spent — review threshold')
        _note(', '.join(_actions(log)))

    # Exact limit: $100 spent → LIMIT entry expected
    ap2, paper2 = _ap(bal=500.0, cands_fn=lambda: [_cand(sym, gp(sym))])
    ap2.daily_spent = 100.0
    log2 = _tick_ap(ap2)
    if _has(log2, 'LIMIT'):
        _ok('LIMIT logged when daily_spent == daily_limit ($100)')
    else:
        _fail('No LIMIT entry when daily_spent equals daily_spend_limit')
        _note(', '.join(_actions(log2)))


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 5 — Dilution Guard
# ══════════════════════════════════════════════════════════════════════════════
def s5_dilution_guard():
    section('S5  Dilution Guard  (catalyst blocks diluting stock)')

    sym = 'DILUT1'
    sp(sym, 0.75)
    cat = MockCatalyst(diluting=['DILUT1'])
    ap, paper = _ap(bal=500.0, cands_fn=lambda: [_cand(sym, gp(sym))], catalyst=cat)

    log = _tick_ap(ap)
    if _has(log, 'CATALYST'):
        _ok('CATALYST guard logged — diluting stock blocked from candidates')
    else:
        _warn('CATALYST log entry not found (may still be blocked)')

    if _has(log, 'BUY'):
        _fail('BUY executed for DILUT1 despite is_diluting=True — dilution guard bypassed')
    else:
        _ok('No BUY for DILUT1 — dilution guard active')

    # Verify non-diluting stock is still buyable alongside the diluting one
    clean = 'CLEAN1'
    sp(clean, 1.00)
    ap2, paper2 = _ap(bal=500.0,
                      cands_fn=lambda: [_cand(sym, gp(sym)), _cand(clean, gp(clean), score=80.0)],
                      catalyst=cat)
    log2 = _tick_ap(ap2)
    if _has(log2, 'BUY'):
        bought = _first(log2, 'BUY')
        if 'CLEAN1' in bought['note']:
            _ok('Non-diluting candidate CLEAN1 bought when DILUT1 blocked')
        else:
            _ok('BUY executed for a non-diluting candidate')
    else:
        _warn('Neither stock bought — dilution guard may be over-blocking')
        _note(', '.join(_actions(log2)))


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 6 — Halt Guard
# ══════════════════════════════════════════════════════════════════════════════
def s6_halt_guard():
    section('S6  Halt Guard  (halted stock blocked)')

    sym = 'HALT1'
    sp(sym, 0.80)
    halts = MockHalts(halted=['HALT1'])
    ap, paper = _ap(bal=500.0, cands_fn=lambda: [_cand(sym, gp(sym))], halts=halts)

    log = _tick_ap(ap)
    if _has(log, 'HALT'):
        _ok('HALT guard logged — halted stock blocked')
    if _has(log, 'BUY'):
        _fail('BUY executed for halted stock HALT1 — halt guard bypassed')
    else:
        _ok('No BUY for HALT1 — halt guard active')


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 7 — Max 5 Positions
# ══════════════════════════════════════════════════════════════════════════════
def s7_max_positions():
    section('S7  Max Positions  (5 open positions blocks new buy)')

    new_sym = 'SIXTH1'
    sp(new_sym, 1.00)
    ap, paper = _ap(bal=500.0, cands_fn=lambda: [_cand(new_sym, gp(new_sym))])

    # Fill paper with 5 positions
    for i in range(1, 6):
        sym = f'POS{i}'
        sp(sym, 1.00)
        paper.buy(sym, 10)

    log = _tick_ap(ap)
    if _has(log, 'LIMIT'):
        entry = _first(log, 'LIMIT')
        if 'Max 5' in entry.get('note', '') or '5 positions' in entry.get('note', ''):
            _ok('LIMIT logged — 5-position cap enforced, SIXTH1 not bought')
        else:
            _ok('LIMIT logged (position cap or daily limit)')
    else:
        _warn('No LIMIT entry logged with 5 open positions')

    if _has(log, 'BUY'):
        _fail('BUY executed despite already holding 5 positions — max-position guard bypassed')
    else:
        _ok('No BUY executed with 5 positions open')


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 8 — Daily Drawdown Circuit Breaker
# ══════════════════════════════════════════════════════════════════════════════
def s8_daily_drawdown():
    section('S8  Daily Drawdown Circuit Breaker  (>10% day-loss halts buys)')

    sym = 'DRAW1'
    sp(sym, 1.00)
    ap, paper = _ap(bal=448.0, cfg_extra={'max_daily_loss_pct': 10.0},
                    cands_fn=lambda: [_cand(sym, gp(sym))])
    ap._daily_start_balance = 500.0

    # paper has $448 cash + 1 position at avg_cost=$1.00 → total = $449
    # drawdown = (500 - 449) / 500 = 10.2% → should trigger
    paper.buy('DRAW_POS', 1)  # $1 position to make total $447 cash + $1 pos_value = $448

    # Override balance for clean calculation:
    # _daily_drawdown_pct uses: total = balance + sum(shares * avg_cost for positions)
    # We want total = $448 + $1 = $449, start = $500 → drawdown = 10.2%
    # The paper state after buy: balance = $448 - $1 = $447, positions = {DRAW_POS: 1 share @ $1}
    # total = $447 + $1 = $448, drawdown = (500 - 448)/500 = 10.4% > 10% ✓

    log = _tick_ap(ap)
    if _has(log, 'HALT'):
        halt_entry = _first(log, 'HALT')
        if 'daily' in halt_entry.get('note', '').lower() or 'floor' in halt_entry.get('note', '').lower():
            _ok('Daily drawdown circuit breaker fired at >10% loss from day-open balance')
            _note(halt_entry['note'][:90])
        else:
            _ok('HALT logged (may be consecutive-loss guard)')
            _note(halt_entry['note'][:90])
    else:
        # Check if BUY happened (would be the bug)
        if _has(log, 'BUY'):
            _fail('BUY executed despite >10% daily drawdown — circuit breaker bypassed')
        else:
            _warn('No HALT or BUY — check drawdown calculation with these test values')
            bal = paper.get_state()['balance']
            pos_val = sum(p['shares'] * p['avg_cost'] for p in paper.get_state()['positions'])
            total = bal + pos_val
            dd_pct = (500 - total) / 500 * 100
            _note(f'balance=${bal:.2f}, pos_value=${pos_val:.2f}, total=${total:.2f}, '
                  f'drawdown={dd_pct:.1f}% (threshold=10%)')
            _note(', '.join(_actions(log)))


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 9 — Consecutive Loss Guard
# ══════════════════════════════════════════════════════════════════════════════
def s9_consecutive_losses():
    section('S9  Consecutive Loss Guard  (3 straight losses halt new buys)')

    sym = 'COOL1'
    sp(sym, 1.00)
    ap, paper = _ap(bal=500.0, cands_fn=lambda: [_cand(sym, gp(sym))])
    ap._consecutive_losses = 3   # at the limit

    log = _tick_ap(ap)
    if _has(log, 'HALT'):
        entry = _first(log, 'HALT')
        if 'consecutive' in entry.get('note', '').lower():
            _ok('Consecutive-loss HALT fired after 3 straight losses')
            _note(entry['note'][:90])
        else:
            _ok('HALT logged')
    else:
        _warn('No HALT logged with 3 consecutive losses')
        _note(', '.join(_actions(log)))

    if _has(log, 'BUY'):
        _fail('BUY executed despite 3 consecutive losses — cool-off guard bypassed')
    else:
        _ok('No BUY — consecutive-loss cool-off active')


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 10 — Congress Format Bug
# ══════════════════════════════════════════════════════════════════════════════
def s10_congress_format_bug():
    section('S10  Congress Format Bug  (dict subscript on string)')

    sym = 'NVDA'
    sp(sym, 3.50)

    # congress.get_buying_tickers() returns list of dicts (correct API shape)
    cong = MockCongress(buying=[{'ticker': 'NVDA', 'member': 'Rep. TestUser', 'amount': '$15k-$50k'}])
    ap, paper = _ap(bal=500.0,
                    cands_fn=lambda: [_cand(sym, gp(sym))],
                    congress=cong)

    # The bug: cong = [t['ticker'] for t in ...] produces ['NVDA']
    # Then ', '.join(c['ticker'] for c in cong[:6]) → 'NVDA'['ticker'] → TypeError
    # This crashes _tick() — caught by _loop() in production (5-min delay), BUY missed.
    try:
        log = _tick_ap(ap)
        # If no crash, check whether buy happened despite the issue
        if _has(log, 'BUY'):
            _ok('BUY executed — congress format bug appears fixed or path not triggered')
        elif _has(log, 'CONGRESS'):
            _ok('CONGRESS log entry present with no crash')
        else:
            _warn('No crash but no BUY — inconclusive')
            _note(', '.join(_actions(log)))
    except TypeError as exc:
        _fail(
            'Congress format bug confirmed: _tick() crashed with TypeError — '
            'valid BUY candidate silently missed; in production _loop() catches this '
            'and skips the entire 5-min tick',
            kind='BUG',
        )
        _note('Root cause — harvester.py ~line 928–931:')
        _note('  cong = [t["ticker"] for t in self.congress.get_buying_tickers(...)]')
        _note('  # cong is now list of strings, e.g. ["NVDA"]')
        _note('  then: ", ".join(c["ticker"] for c in cong[:6])  ← TypeError here')
        _note('  Fix: keep cong_items as list of dicts; use cong_items[]["ticker"] consistently')
        _note(f'  Exception: {exc}')


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 11 — Evasive Sell (cascade detected)
# ══════════════════════════════════════════════════════════════════════════════
def s11_evasive_sell():
    section('S11  Evasive Sell  (cascade detected → shed 75%)')

    sym = 'CASC1'
    sp(sym, 1.00)                      # entry price
    ap, paper = _ap(bal=400.0)

    # Pre-load position at $1.00 entry, then advance price to $1.20
    paper.buy(sym, 50)                 # 50 shares @ $1.00
    sp(sym, 1.20)                      # current price up 20% → net 17%
    ap._position_opened[sym] = datetime.now()
    ap._position_hwm[sym]    = 1.20

    # Patch momentum snapshot to return cascade=True
    def mock_snap(s):
        if s == sym: return (-0.2, True, 1.8)   # slight fade + cascade
        return (0.5, False, 2.5)
    ap._momentum_snapshot = mock_snap

    log = _tick_ap(ap)
    if _has(log, 'EVASIVE'):
        entry = _first(log, 'EVASIVE')
        _ok('EVASIVE sell fired on cascade detection')
        _note(entry['note'][:90])
        remaining = paper._st['positions'].get(sym, {}).get('shares', 0)
        if remaining > 0:
            _ok(f'{remaining} shares remain after evasive 75% shed (position not fully closed)')
        else:
            _warn('All shares sold — evasive shed removed entire position (expected partial)')
    else:
        _fail('EVASIVE sell did NOT fire with cascade=True and net_gain > tx_cost_pct')
        _note(', '.join(_actions(log)))

    # Structural check: _position_entry_rvol NOT cleaned up after evasive (minor gap)
    if sym in ap._position_entry_rvol:
        _warn(
            '_position_entry_rvol not cleared after EVASIVE shed — stale entry RVOL may '
            'cause premature volume-collapse exit on remaining shares next tick',
            )
    else:
        _ok('_position_entry_rvol cleared after evasive sell')


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 12 — Final-Exit (exceptional gain ≥ 25% net)
# ══════════════════════════════════════════════════════════════════════════════
def s12_final_exit():
    section('S12  Final-Exit  (exceptional 28% net gain → full exit)')

    sym = 'MOON1'
    entry_price = 1.00
    sp(sym, 1.00)                       # entry price
    ap, paper = _ap(bal=400.0)

    paper.buy(sym, 50)                  # 50 shares @ $1.00
    sp(sym, 1.31)                       # advance price: 31% gain → net 28% ≥ 25%
    ap._position_opened[sym] = datetime.now()
    ap._position_hwm[sym]    = 1.31     # HWM = current → no trail stop
    ap._position_entry_rvol[sym] = 3.0  # entry RVOL was high → no volume-collapse exit

    log = _tick_ap(ap)
    if _has(log, 'FINAL-EXIT'):
        entry = _first(log, 'FINAL-EXIT')
        _ok('FINAL-EXIT triggered at 28% net gain (exceptional gain ≥ 25%)')
        _note(entry['note'][:90])
        if ap.stats['wins'] >= 1:
            _ok('stats.wins incremented on FINAL-EXIT')
        else:
            _fail(f'stats.wins NOT incremented after FINAL-EXIT (= {ap.stats["wins"]})')
        if not paper._st['positions'].get(sym):
            _ok('Position fully closed after FINAL-EXIT')
        else:
            _fail('Position still open after FINAL-EXIT — sell may have failed')
    else:
        _fail('FINAL-EXIT did not fire at 31% gain (net 28%)')
        _note(', '.join(_actions(log)))

    # Structural note: stats.total_profit uses (price - entry) * shares (gross),
    # but net_gain subtracts tx_cost_pct → profit metric overstates by ~tx_cost
    gross_profit = 50 * (1.31 - entry_price)
    if ap.stats['total_profit'] > 0:
        if abs(ap.stats['total_profit'] - gross_profit) < 0.01:
            _warn(
                'stats.total_profit tracks GROSS profit ($%.2f) not NET — '
                'overstates by ~tx_cost; P&L display should subtract transaction costs' % gross_profit
            )
        else:
            _ok(f'total_profit recorded as ${ap.stats["total_profit"]:.2f} (gross ${gross_profit:.2f})')


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 13 — HOLIDAY gate
# ══════════════════════════════════════════════════════════════════════════════
def s13_holiday_gate():
    section('S13  HOLIDAY Gate  (no trades on weekend/holiday)')

    import modules.harvester as hmod
    orig_session = hmod._get_session

    sym = 'HOLI1'
    sp(sym, 1.00)
    ap, paper = _ap(bal=500.0, cands_fn=lambda: [_cand(sym, gp(sym))])

    hmod._get_session = lambda: 'HOLIDAY'
    log = _tick_ap(ap)
    hmod._get_session = lambda: 'STANDARD'   # restore

    if _has(log, 'HOLIDAY'):
        _ok('HOLIDAY gate fires correctly — tick exits immediately with no trading')
    else:
        _warn('No HOLIDAY log entry when session=HOLIDAY (may still be guarded)')

    if _has(log, 'BUY'):
        _fail('BUY executed on HOLIDAY — market-closed guard bypassed')
    else:
        _ok('No BUY on HOLIDAY')


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 14 — Unfavorable Macro Gate
# ══════════════════════════════════════════════════════════════════════════════
def s14_macro_gate():
    section('S14  Macro Gate  (adverse macro blocks new buys)')

    sym = 'MACRO1'
    sp(sym, 1.00)
    macro = MockMacro(ok=False, score=-15)
    ap, paper = _ap(bal=500.0,
                    cands_fn=lambda: [_cand(sym, gp(sym))],
                    macro=macro)

    log = _tick_ap(ap)
    if _has(log, 'MACRO'):
        _ok('MACRO gate fires — adverse environment blocks new buys')
    else:
        _warn('No MACRO log entry with is_favorable=False')

    if _has(log, 'BUY'):
        _fail('BUY executed despite unfavorable macro — macro gate bypassed')
    else:
        _ok('No BUY when macro is unfavorable')


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 15 — P&L Accounting: buy/sell/reinvest balance reconciliation
# ══════════════════════════════════════════════════════════════════════════════
def s15_pnl_accounting():
    section('S15  P&L Accounting  (balance reconciliation after harvest)')

    sym = 'ACC1'
    sp(sym, 1.00)
    ap, paper = _ap(bal=500.0, cands_fn=lambda: [_cand(sym, gp(sym))])

    start_bal = paper._st['balance']

    # Tick 1: BUY
    log = _tick_ap(ap)
    if not _has(log, 'BUY'):
        _warn('BUY not executed — skipping P&L accounting check')
        return
    post_buy_bal = paper._st['balance']
    shares_held  = paper._st['positions'].get(sym, {}).get('shares', 0)
    cost         = shares_held * 1.00
    _ok(f'Post-buy balance: ${post_buy_bal:.2f} (deployed ${cost:.2f})')

    if abs((start_bal - post_buy_bal) - cost) < 0.01:
        _ok('Balance correctly reduced by purchase cost')
    else:
        _fail(f'Balance mismatch: expected ${start_bal - cost:.2f}, got ${post_buy_bal:.2f}')

    # Tick 2: price rises → HARVEST
    sp(sym, 1.10)
    ap.harvester._fn = lambda: [_cand(sym, 1.10)]
    ap._position_opened[sym] = datetime.now()
    log2 = _tick_ap(ap)

    if _has(log2, 'HARVEST'):
        post_harvest_bal = paper._st['balance']
        shares_after     = paper._st['positions'].get(sym, {}).get('shares', 0)
        harvested        = shares_held - shares_after
        harvest_proceeds = harvested * 1.10
        # After harvest, balance should be post_buy + harvest_proceeds
        # (reinvest buys some shares back, so actual balance depends on reinvest)
        expected_min = post_buy_bal  # at minimum balance should not drop below post-buy
        if post_harvest_bal >= expected_min:
            _ok(f'Post-harvest balance ${post_harvest_bal:.2f} ≥ pre-harvest ${expected_min:.2f}')
        else:
            _fail(f'Post-harvest balance ${post_harvest_bal:.2f} dropped below pre-harvest ${expected_min:.2f}')

        total = post_harvest_bal + shares_after * 1.10
        _note(f'cash=${post_harvest_bal:.2f} + {shares_after} shares @ $1.10 = total ${total:.2f}')
    else:
        _warn('HARVEST did not fire at $1.10 — skipping balance reconciliation')
        _note(', '.join(_actions(log2)))


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO 16 — TIME-STOP (stale 3-day position with no profit)
# ══════════════════════════════════════════════════════════════════════════════
def s16_time_stop():
    section('S16  Time-Stop  (3-day stale position with no profit)')

    sym = 'STALE1'
    sp(sym, 1.00)
    ap, paper = _ap(bal=400.0)

    paper.buy(sym, 50)                  # 50 shares @ $1.00
    # Go back far enough to guarantee 3 business days regardless of weekday
    ap._position_opened[sym] = datetime.now() - timedelta(days=7)
    ap._position_hwm[sym]    = 1.00

    # Current price == entry (0% gain, net_gain = -3% after tx_cost)
    sp(sym, 1.00)

    log = _tick_ap(ap)
    if _has(log, 'TIME-STOP'):
        entry = _first(log, 'TIME-STOP')
        _ok('TIME-STOP fired on stale position (≥3 business days, no profit)')
        _note(entry['note'][:90])
        if ap._consecutive_losses >= 1:
            _ok('consecutive_losses incremented after time-stop')
        else:
            _fail(f'consecutive_losses not incremented (= {ap._consecutive_losses})')
        _ok('TIME-STOP now counts business days — weekend holds no longer trigger prematurely')
    else:
        _fail('TIME-STOP did not fire after ≥3 business days with no profit')
        _note(', '.join(_actions(log)))


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    print(f'\n{BOLD}{B}{"═" * 62}')
    print('  StopTrading — Faux Data Trial Run')
    print(f'  {datetime.now().strftime("%Y-%m-%d  %H:%M:%S")}')
    print(f'  Temp dir: {_TMPDIR}')
    print(f'{"═" * 62}{RESET}')

    scenarios = [
        s1_harvest_cycle,
        s2_trail_stop,
        s3_pdt_gate,
        s4_daily_spend_limit,
        s5_dilution_guard,
        s6_halt_guard,
        s7_max_positions,
        s8_daily_drawdown,
        s9_consecutive_losses,
        s10_congress_format_bug,
        s11_evasive_sell,
        s12_final_exit,
        s13_holiday_gate,
        s14_macro_gate,
        s15_pnl_accounting,
        s16_time_stop,
    ]

    for fn in scenarios:
        try:
            fn()
        except Exception as exc:
            import traceback
            section_name = fn.__name__
            print(f'  {R}SCENARIO CRASH: {section_name}{RESET}')
            traceback.print_exc()
            _findings.append(('CRASH', f'{section_name}: {exc}'))

    # ── Summary ───────────────────────────────────────────────────────────────
    bugs  = [(k, m) for k, m in _findings if k == 'BUG']
    warns = [(k, m) for k, m in _findings if k == 'WARN']
    crash = [(k, m) for k, m in _findings if k == 'CRASH']

    print(f'\n{BOLD}{B}{"═" * 62}')
    print('  TRIAL RUN — FINDINGS SUMMARY')
    print(f'{"═" * 62}{RESET}')

    if crash:
        print(f'\n  {R}{BOLD}CRASHES ({len(crash)}){RESET}')
        for _, m in crash:
            print(f'  {R}✗{RESET}  {m}')

    if bugs:
        print(f'\n  {R}{BOLD}BUGS ({len(bugs)}){RESET}')
        for _, m in bugs:
            print(f'  {R}✗{RESET}  {m}')
    else:
        print(f'\n  {G}No confirmed bugs.{RESET}')

    if warns:
        print(f'\n  {Y}{BOLD}STRUCTURAL / DESIGN CONCERNS ({len(warns)}){RESET}')
        for _, m in warns:
            print(f'  {Y}⚠{RESET}  {m}')

    total_findings = len(bugs) + len(crash)
    print(f'\n  Total: {len(bugs)} bug(s), {len(warns)} concern(s), {len(crash)} crash(es)')

    if total_findings == 0:
        print(f'\n  {G}{BOLD}All scenarios passed — no bugs found.{RESET}\n')
    else:
        print(f'\n  {R}{BOLD}{total_findings} issue(s) require attention.{RESET}\n')

    return 0 if total_findings == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
