#!/usr/bin/env python3
"""
AutoPilot lifecycle simulation — fictitious penny stocks, no real money.

Scenarios:
  1. GOPH  — full harvest cycle: buy → harvest×2 → exit
  2. IDEX  — trailing stop: buy → price runs → crashes from high-water mark
  3. MMAT  — time-stop: position stalls 3 days with no profit → force exit
"""
import sys, os, threading, textwrap
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Shared price registry (updated before each simulated tick) ────────────────
PRICES: dict = {}


# ── Minimal in-memory paper trader ───────────────────────────────────────────
class MockPaper:
    def __init__(self, balance: float = 500.0):
        self._lock = threading.Lock()
        self._state = {'balance': balance, 'positions': {}, 'history': []}

    def _save(self):
        pass  # no-op — no file I/O in simulation

    def buy(self, symbol: str, shares: int) -> dict:
        price = PRICES.get(symbol, 0.0)
        cost  = round(shares * price, 6)
        if cost > self._state['balance'] + 0.01:
            return {'error': f'insufficient funds (have ${self._state["balance"]:.2f}, need ${cost:.2f})'}
        self._state['balance'] -= cost
        pos = self._state['positions']
        if symbol in pos:
            old     = pos[symbol]
            ns      = old['shares'] + shares
            new_avg = (old['shares'] * old['avg_cost'] + cost) / ns
            pos[symbol] = {'symbol': symbol, 'shares': ns, 'avg_cost': round(new_avg, 6)}
        else:
            pos[symbol] = {'symbol': symbol, 'shares': shares, 'avg_cost': price}
        return {'ok': True}

    def sell(self, symbol: str, shares: int) -> dict:
        price = PRICES.get(symbol, 0.0)
        pos   = self._state['positions']
        if symbol not in pos or pos[symbol]['shares'] < shares:
            return {'error': f'not enough shares (have {pos.get(symbol,{}).get("shares",0)}, need {shares})'}
        self._state['balance'] = round(self._state['balance'] + shares * price, 6)
        pos[symbol]['shares'] -= shares
        if pos[symbol]['shares'] == 0:
            del pos[symbol]
        return {'ok': True}

    def get_state(self) -> dict:
        positions = [dict(v) for v in self._state['positions'].values()]
        return {'balance': round(self._state['balance'], 2), 'positions': positions}


class MockHarvester:
    def __init__(self):
        self.candidates: list = []

    def scan_movers(self, extra_symbols=None) -> list:
        return self.candidates


# ── Terminal formatting ───────────────────────────────────────────────────────
CLR = {
    'BUY':        '\033[92m',   # green
    'HARVEST':    '\033[94m',   # blue
    'REINVEST':   '\033[96m',   # cyan
    'EXIT':       '\033[93m',   # yellow
    'TRAIL-STOP': '\033[91m',   # red
    'TIME-STOP':  '\033[91m',   # red
    'LIMIT':      '\033[33m',   # orange
    'SKIP':       '\033[90m',   # grey
    'SCAN':       '\033[90m',
    'SESSION':    '\033[90m',
    'SYSTEM':     '\033[90m',
    'REGIME':     '\033[33m',
}
RESET = '\033[0m'
BOLD  = '\033[1m'
DIM   = '\033[2m'

QUIET_ACTIONS = {'SCAN', 'SESSION', 'SYSTEM', 'LIMIT', 'SKIP'}


def section(title: str):
    print(f'\n{BOLD}{"━"*72}{RESET}')
    print(f'{BOLD}  {title}{RESET}')
    print(f'{BOLD}{"━"*72}{RESET}')


def print_tick(tick_num: int, prices: dict, entries: list, balance: float):
    price_str = '  '.join(f'{s}=${p:.4f}' for s, p in prices.items())
    print(f'\n  {BOLD}Tick {tick_num}{RESET}  [{price_str}]  balance=${balance:.2f}')
    for e in entries:
        action = e.get('action', '')
        color  = CLR.get(action, '')
        note   = e.get('note', '')
        if action in QUIET_ACTIONS:
            print(f'    {DIM}  · {note[:90]}{RESET}')
        else:
            lines = textwrap.wrap(note, 80)
            print(f'    {color}▶ [{action:<10}]{RESET} {lines[0]}')
            for l in lines[1:]:
                print(f'               {l}')


def print_result(ap, paper: MockPaper):
    s   = ap.stats
    st  = paper.get_state()
    pnl = s['total_profit']
    pnl_clr = '\033[92m' if pnl >= 0 else '\033[91m'
    print(f'\n  {BOLD}── Result ──────────────────────────────{RESET}')
    print(f'  Balance  : ${st["balance"]:.2f}  (started $500.00)')
    print(f'  Net P&L  : {pnl_clr}${pnl:+.2f}{RESET}')
    print(f'  Trades   : {s["total_trades"]}  Harvests: {s["total_harvests"]}')
    print(f'  W / L    : {s["wins"]}W / {s["losses"]}L')
    remaining = [f'{p["symbol"]} ×{p["shares"]}@${p["avg_cost"]:.4f}' for p in st['positions']]
    print(f'  Open pos : {", ".join(remaining) if remaining else "none — fully closed"}')


# ── AutoPilot factory ─────────────────────────────────────────────────────────
def make_ap(paper: MockPaper, harvester: MockHarvester):
    from modules.harvester import AutoPilot
    cfg = {
        'tx_cost_pct':          0.5,
        'harvest_trigger_pct':  7.0,
        'exit_trigger_pct':     5.0,
        'daily_spend_limit':  300.0,   # generous so reinvest never hits the ceiling
        'per_trade_capital':   50.0,
    }
    ap = AutoPilot(harvester, paper, cfg)
    ap.running            = True
    ap.daily_spent        = 0.0
    ap.log                = []
    ap._position_hwm      = {}
    ap._position_opened   = {}
    ap._position_harvests = {}
    ap._day_trades_log    = []
    ap._consecutive_losses= 0
    ap.stats = {
        'total_trades': 0, 'total_harvests': 0, 'total_profit': 0.0,
        'started': datetime.now().isoformat(),
        'wins': 0, 'losses': 0, 'total_win_pct': 0.0, 'total_loss_pct': 0.0,
    }
    # Neutralise all network calls; simulate 10:30 ET (mid-session, valid buy window)
    _sim_et = datetime(2025, 1, 2, 10, 30, 0)
    ap._market_regime = lambda: 0.5
    ap._get_vwap      = lambda sym: (None, None)
    ap._get_rvol      = lambda sym: 1.8
    ap._get_et_now    = lambda: _sim_et
    return ap


def do_tick(ap, tick_prices: dict, mock_yf) -> list:
    """Update price registry, rebind yf mock, run one _tick(), return new log entries."""
    PRICES.update(tick_prices)

    def make_ticker(sym):
        t = MagicMock()
        t.fast_info.last_price = PRICES.get(sym, 0.0)
        return t

    mock_yf.Ticker.side_effect = make_ticker
    before = len(ap.log)
    ap._tick()
    return ap.log[before:]


# ═══════════════════════════════════════════════════════════════════════════════
# RUN ALL SCENARIOS
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print(f'\n{BOLD}StopTrading AutoPilot — Lifecycle Simulation{RESET}')
    print('Fictitious tickers only. Paper account. No real orders placed.\n')
    print('Config: harvest_trig=7%  exit_trig=5%  tx_cost=0.5%  daily_limit=$300  per_trade=$50\n')

    with patch('modules.harvester._get_session', return_value='STANDARD'), \
         patch('modules.harvester.yf') as mock_yf:

        # ─────────────────────────────────────────────────────────────────────
        # SCENARIO 1 — GOPH: full harvest cycle
        #
        # Price path: $1.00 → buy
        #             $1.085 → net gain 8.0% ≥ 7%  → HARVEST #1 + REINVEST
        #             $1.160 → net gain ~8.7%       → HARVEST #2 + REINVEST
        #             $1.140 → margin compresses to ~3%  ≤ 5% after 2 harvests → EXIT
        # ─────────────────────────────────────────────────────────────────────
        section('Scenario 1 — GOPH  (buy → harvest×2 → exit)')

        p1 = MockPaper(500.0)
        h1 = MockHarvester()
        ap1 = make_ap(p1, h1)

        h1.candidates = [{'symbol': 'GOPH', 'price': 1.00, 'change_1h': 3.2, 'change_5d': 5.0,
                          'vol_ratio': 2.5, 'tech_score': 72, 'score': 72,
                          'combined_score': 72, 'rt_signal': '', 'rt_score': 0}]

        e = do_tick(ap1, {'GOPH': 1.00}, mock_yf)
        print_tick(1, {'GOPH': 1.00}, e, p1.get_state()['balance'])

        h1.candidates = []   # already in position — no new buys attempted

        e = do_tick(ap1, {'GOPH': 1.085}, mock_yf)
        print_tick(2, {'GOPH': 1.085}, e, p1.get_state()['balance'])

        e = do_tick(ap1, {'GOPH': 1.160}, mock_yf)
        print_tick(3, {'GOPH': 1.160}, e, p1.get_state()['balance'])

        e = do_tick(ap1, {'GOPH': 1.140}, mock_yf)
        print_tick(4, {'GOPH': 1.140}, e, p1.get_state()['balance'])

        print_result(ap1, p1)

        # ─────────────────────────────────────────────────────────────────────
        # SCENARIO 2 — IDEX: trailing stop
        #
        # Price path: $1.00 → buy (50 shares)
        #             $1.05 → gain 4.5%, HWM=$1.05 — below harvest, no action
        #             $1.10 → net 9.5% ≥ 7% → HARVEST #1 + REINVEST, HWM=$1.10
        #             $0.92 → drop from HWM=(1.10-0.92)/1.10=16.4% ≥ 15% → TRAIL-STOP
        # ─────────────────────────────────────────────────────────────────────
        section('Scenario 2 — IDEX  (buy → runs up → crashes → trailing stop)')

        p2 = MockPaper(500.0)
        h2 = MockHarvester()
        ap2 = make_ap(p2, h2)

        h2.candidates = [{'symbol': 'IDEX', 'price': 1.00, 'change_1h': 5.1, 'change_5d': 7.0,
                          'vol_ratio': 3.8, 'tech_score': 81, 'score': 81,
                          'combined_score': 81, 'rt_signal': '', 'rt_score': 0}]

        e = do_tick(ap2, {'IDEX': 1.00}, mock_yf)
        print_tick(1, {'IDEX': 1.00}, e, p2.get_state()['balance'])

        h2.candidates = []

        # Price grinds up — gain 4.5%, below harvest trigger
        e = do_tick(ap2, {'IDEX': 1.05}, mock_yf)
        print_tick(2, {'IDEX': 1.05}, e, p2.get_state()['balance'])

        # Crosses harvest trigger — HWM locked at $1.10
        e = do_tick(ap2, {'IDEX': 1.10}, mock_yf)
        print_tick(3, {'IDEX': 1.10}, e, p2.get_state()['balance'])

        # Stock collapses — 16.4% drop from HWM → trailing stop fires
        e = do_tick(ap2, {'IDEX': 0.92}, mock_yf)
        print_tick(4, {'IDEX': 0.92}, e, p2.get_state()['balance'])

        print_result(ap2, p2)

        # ─────────────────────────────────────────────────────────────────────
        # SCENARIO 3 — MMAT: time-stop
        #
        # Position opened 3 days ago at $0.75 (injected directly into MockPaper).
        # Current price $0.73 — net gain = -3.2%, days_open = 3.
        # Condition: days_open >= 3 AND net_gain <= 0  → TIME-STOP
        # ─────────────────────────────────────────────────────────────────────
        section('Scenario 3 — MMAT  (stalls 3 days with no profit → time-stop)')

        p3 = MockPaper(500.0)
        h3 = MockHarvester()
        ap3 = make_ap(p3, h3)

        # Pre-inject the position as if it was bought 3 days ago
        PRICES['MMAT'] = 0.75
        p3.buy('MMAT', 66)   # 66 shares @ $0.75 = $49.50 — matches per_trade sizing

        # Wind back the open timestamp 3 days so time-stop fires immediately
        ap3._position_opened['MMAT'] = datetime.now() - timedelta(days=3)
        ap3._position_harvests['MMAT'] = 0
        ap3.daily_spent = 49.50   # reflect that the buy already happened
        ap3.stats['total_trades'] = 1

        print(f'\n  Pre-state: 66 shares MMAT @ $0.75 (opened 3 days ago)')
        print(f'  Strategy: if price flat/down after 3 days → exit dead capital\n')

        h3.candidates = []

        # Price slightly down from entry — time-stop should fire
        e = do_tick(ap3, {'MMAT': 0.73}, mock_yf)
        print_tick(1, {'MMAT': 0.73}, e, p3.get_state()['balance'])

        print_result(ap3, p3)

    # ── Simulation complete ──────────────────────────────────────────────────
    print(f'\n{"━"*72}')
    print(f'{BOLD}  Simulation complete.{RESET}')
    print(f'  All scenarios run against an in-memory paper account.')
    print(f'  Harvester, VWAP, regime, and RVOL calls were mocked.')
    print(f'  Core AutoPilot logic (monitoring, stop rules, Kelly sizing)')
    print(f'  ran unmodified from modules/harvester.py.\n')
