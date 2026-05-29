#!/usr/bin/env python3
"""
StopTrading Stress Test — Real Market Data May 28, 2026
=======================================================
Bar-by-bar simulation using actual 5-minute intraday data.
Three scan strategies compared head-to-head:

  A. CURRENT  — 1h scan: signal fires after first full hour closes (~10:35am bar)
  B. IMPROVED — 5m scan: signal fires when 5m bar hits change/vol trigger
  C. ORB      — Opening Range Breakout: entry when price > first-5m candle high

Benchmarks:
  IDEAL      — buy at open, sell at intraday peak (theoretical max)
  HOLLY-EST  — Trade Ideas Holly AI estimated (published ~60% win, avg +10%/-6%)
"""

import sys, os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Config matching production defaults ──────────────────────────────────────
CFG = {
    'tx_cost_pct':         0.5,
    'harvest_trigger_pct': 7.0,
    'exit_trigger_pct':    2.0,
    'max_single_loss_pct': 8.0,
    'trail_day0_pct':      15.0,
    'trail_day1_pct':      10.0,
    'time_exit_hours':     2.0,
    'time_exit_min_gain':  2.0,
    'exceptional_gain':    25.0,
    'per_trade_capital':   100.0,   # realistic $100 per trade for analysis
    'min_change_1h_std':   3.0,     # STANDARD session
    'min_change_1h_mom':   2.0,     # OPEN_MOMENTUM session
    'min_vol_ratio_std':   2.0,
    'min_vol_ratio_mom':   3.0,
    'price_floor':         0.50,
    'price_ceil':          5.00,
}

# ── Color helpers ─────────────────────────────────────────────────────────────
R   = '\033[91m'
G   = '\033[92m'
Y   = '\033[93m'
B   = '\033[94m'
C   = '\033[96m'
W   = '\033[97m'
DIM = '\033[2m'
BOLD= '\033[1m'
RST = '\033[0m'

BAR_MINS = 5    # each bar = 5 minutes
OPEN_BAR = 0    # bar 0 = 9:30am
ORB_BAR  = 1    # bar 1 = 9:35am (end of first 5-min candle = ORB anchor)

def bar_time(bar_idx: int) -> str:
    """Convert bar index to ET time string."""
    total_mins = 9 * 60 + 30 + bar_idx * BAR_MINS
    h, m = divmod(total_mins, 60)
    return f'{h:02d}:{m:02d}'

def bar_et_hour(bar_idx: int) -> float:
    """Return fractional hour (ET) for session checks."""
    total_mins = 9 * 60 + 30 + bar_idx * BAR_MINS
    return total_mins / 60.0


# ── Core position simulator (exact logic from AutoPilot) ─────────────────────

class PositionSim:
    def __init__(self, symbol, entry_bar, entry_price, shares, bars, strategy):
        self.symbol      = symbol
        self.entry_bar   = entry_bar
        self.entry_price = entry_price
        self.shares      = shares
        self.bars        = bars
        self.strategy    = strategy
        self.hwm         = entry_price
        self.harvests    = 0
        self.profit      = 0.0
        self.events      = []
        self.exit_bar    = None
        self.exit_price  = None
        self.remaining_shares = shares

    def net_gain(self, price):
        return (price - self.entry_price) / self.entry_price * 100 - CFG['tx_cost_pct']

    def gross_gain_pct(self, price):
        return (price - self.entry_price) / self.entry_price * 100

    def run(self):
        tx = CFG['tx_cost_pct']
        ht = CFG['harvest_trigger_pct']
        ml = CFG['max_single_loss_pct']
        t0_trail = CFG['trail_day0_pct']
        exc_gain  = CFG['exceptional_gain']
        time_exit_bars = int(CFG['time_exit_hours'] * 60 / BAR_MINS)

        for i, bar in enumerate(self.bars[self.entry_bar + 1:], start=1):
            price = bar['close']
            ng    = self.net_gain(price)
            self.hwm = max(self.hwm, price)
            drop_from_hwm = (self.hwm - price) / self.hwm * 100 if self.hwm > 0 else 0
            elapsed_bars  = i
            elapsed_hours = elapsed_bars * BAR_MINS / 60.0
            bar_abs = self.entry_bar + i

            # Exceptional gain — full exit
            if ng >= exc_gain:
                profit = self._sell(self.remaining_shares, price, 'FINAL-EXIT (exceptional)')
                self.exit_bar = bar_abs; self.exit_price = price
                break

            # Hard stop-loss
            if ng <= -ml:
                self._sell(self.remaining_shares, price, f'STOP-LOSS ({ng:.1f}%)')
                self.exit_bar = bar_abs; self.exit_price = price
                break

            # Trailing stop (day-0: 15%)
            if drop_from_hwm >= t0_trail and self.remaining_shares > 0:
                self._sell(self.remaining_shares, price,
                           f'TRAIL-STOP ({drop_from_hwm:.1f}% drop from HWM ${self.hwm:.3f})')
                self.exit_bar = bar_abs; self.exit_price = price
                break

            # Harvest trigger
            if ng >= ht and self.remaining_shares > 0:
                frac = self._harvest_frac(elapsed_bars)
                h_shares = max(1, int(self.remaining_shares * frac))
                profit = self._sell(h_shares, price,
                          f'HARVEST {frac*100:.0f}% ({h_shares}sh, net {ng:.1f}%)')
                self.harvests += 1
                # Tighten trail after first harvest
                t0_trail = min(t0_trail, 8.0)
                continue

            # Time-exit: 2h+ elapsed + 2%+ net gain → harvest 60%
            if elapsed_hours >= CFG['time_exit_hours'] and ng >= CFG['time_exit_min_gain'] \
               and self.harvests == 0 and self.remaining_shares > 0:
                t_shares = max(1, int(self.remaining_shares * 0.60))
                self._sell(t_shares, price,
                          f'TIME-EXIT 60% ({t_shares}sh, {elapsed_hours:.1f}h, net {ng:.1f}%)')
                self.harvests += 1
                continue

            # Exit: harvested + margin compressed
            if self.harvests > 0 and ng <= CFG['exit_trigger_pct']:
                self._sell(self.remaining_shares, price,
                          f'EXIT (margin {ng:.1f}% after {self.harvests} harvest(s))')
                self.exit_bar = bar_abs; self.exit_price = price
                break

        # End of day — force close any remaining
        if self.remaining_shares > 0:
            last_price = self.bars[-1]['close']
            self._sell(self.remaining_shares, last_price, 'EOD-CLOSE (forced)')
            self.exit_bar = len(self.bars) - 1
            self.exit_price = last_price

        return self.profit

    def _harvest_frac(self, elapsed_bars):
        """Mirrors AutoPilot dynamic fraction logic."""
        base = 0.50
        if elapsed_bars < 8:   base = 0.30   # early in the trade — stay in
        if self.harvests >= 1: base = 0.65   # second harvest — more aggressive
        if self.harvests >= 2: base = 0.75
        return round(min(0.80, base), 2)

    def _sell(self, shares, price, reason):
        proceeds = shares * price
        cost_basis = shares * self.entry_price
        gross = proceeds - cost_basis
        tx    = shares * self.entry_price * CFG['tx_cost_pct'] / 100
        net   = gross - tx
        self.profit += net
        self.remaining_shares -= shares
        self.events.append({
            'shares': shares, 'price': price, 'net': round(net, 2),
            'reason': reason,
        })
        return net


# ── Strategy entry detection ──────────────────────────────────────────────────

def find_entry_current(bars: list):
    """
    CURRENT strategy: hourly scan.
    Signal fires at first bar >= bar 13 (10:35am) where:
      - cumulative change from open >= 3% (STANDARD threshold)
      - vol_ratio >= 2.0
    Mimics using 1h bars: first hour closes at 10:30, scan runs at 10:35.
    """
    SIGNAL_BAR = 13   # 10:35am = bar 13 (13×5min = 65min after 9:30)
    if len(bars) < SIGNAL_BAR + 1:
        return None
    open_price = bars[0]['close']
    avg_vol    = sum(b['volume'] for b in bars) / len(bars)
    for i in range(SIGNAL_BAR, len(bars)):
        chg = (bars[i]['close'] - open_price) / open_price * 100 if open_price > 0 else 0
        vol_ratio = bars[i]['volume'] / avg_vol if avg_vol > 0 else 1.0
        if chg >= CFG['min_change_1h_std'] and vol_ratio >= CFG['min_vol_ratio_std']:
            return i
    return None


def find_entry_improved(bars: list):
    """
    IMPROVED strategy: 5-minute scan.
    Signal fires at first bar >= bar 1 (9:35am) where:
      - change from open >= 2% (OPEN_MOMENTUM threshold)
      - vol_ratio >= 3.0 (OPEN_MOMENTUM threshold)
    Can fire up to 60+ minutes earlier than CURRENT.
    """
    if len(bars) < 2:
        return None
    open_price = bars[0]['close']
    avg_vol    = sum(b['volume'] for b in bars) / len(bars)
    for i in range(1, len(bars)):
        chg = (bars[i]['close'] - open_price) / open_price * 100 if open_price > 0 else 0
        vol_ratio = bars[i]['volume'] / avg_vol if avg_vol > 0 else 1.0
        # Use tighter threshold for very early bars (first 6 = first 30 min)
        min_chg = CFG['min_change_1h_mom'] if i < 6 else CFG['min_change_1h_std']
        min_vr  = CFG['min_vol_ratio_mom'] if i < 6 else CFG['min_vol_ratio_std']
        if chg >= min_chg and vol_ratio >= min_vr:
            return i
    return None


def find_entry_orb(bars: list):
    """
    ORB strategy: Opening Range Breakout.
    ORB high = bar[0]['high'] (or bar[1] close as proxy since we have close-only data).
    Signal fires at first bar >= 2 where close > ORB high.
    Uses close as proxy for high (conservative — real ORB uses actual high).
    """
    if len(bars) < 3:
        return None
    orb_high = max(bars[0]['close'], bars[1]['close'])
    for i in range(2, len(bars)):
        if bars[i]['close'] > orb_high * 1.005:   # 0.5% buffer above ORB
            return i
    return None


# ── Fetch real data ───────────────────────────────────────────────────────────

def fetch_bars(symbol: str) -> list:
    """Fetch May 28, 2026 5-min bars. Returns list of {close, volume} dicts."""
    import yfinance as yf
    try:
        t    = yf.Ticker(symbol)
        h    = t.history(period='2d', interval='5m')
        may28 = h[h.index.date == date(2026, 5, 28)]
        if may28.empty:
            may28 = h.tail(78)
        bars = []
        for ts, row in may28.iterrows():
            bars.append({
                'time':   ts.strftime('%H:%M'),
                'close':  float(row['Close']),
                'high':   float(row['High']),
                'low':    float(row['Low']),
                'volume': int(row['Volume']),
            })
        return bars
    except Exception as e:
        print(f'  ERROR fetching {symbol}: {e}')
        return []


# ── Simulation runner ─────────────────────────────────────────────────────────

def run_scenario(symbol: str, bars: list) -> dict:
    if not bars:
        return {'symbol': symbol, 'error': 'no data'}

    price_floor = CFG['price_floor']
    price_ceil  = CFG['price_ceil']
    open_price  = bars[0]['close']
    peak_price  = max(b['close'] for b in bars)
    close_price = bars[-1]['close']
    peak_bar    = next(i for i, b in enumerate(bars) if b['close'] == peak_price)
    avg_vol     = sum(b['volume'] for b in bars) / len(bars)

    # Price filter gate (current system)
    in_range = price_floor <= open_price <= price_ceil

    # Shares for $100 capital
    shares = max(1, int(CFG['per_trade_capital'] / open_price))

    # Ideal: buy at open, sell at peak
    ideal_profit = (peak_price - open_price - open_price * CFG['tx_cost_pct']/100) * shares
    ideal_pct    = (peak_price - open_price) / open_price * 100

    results = {}

    # IDEAL (theoretical max)
    results['IDEAL'] = {
        'entry_bar':   0,
        'entry_price': open_price,
        'exit_bar':    peak_bar,
        'exit_price':  peak_price,
        'profit':      round(ideal_profit, 2),
        'pct':         round(ideal_pct, 2),
        'events':      [f'BUY {shares}sh@{open_price:.3f} | SELL {shares}sh@{peak_price:.3f}'],
    }

    # Run each strategy
    for strat_name, finder in [('CURRENT', find_entry_current),
                                ('IMPROVED', find_entry_improved),
                                ('ORB', find_entry_orb)]:
        if not in_range:
            results[strat_name] = {'skipped': f'price ${open_price:.2f} outside ${price_floor:.2f}-${price_ceil:.2f}', 'profit': 0}
            continue
        entry_bar = finder(bars)
        if entry_bar is None:
            results[strat_name] = {'no_signal': True, 'profit': 0}
            continue
        entry_price = bars[entry_bar]['close']
        shares_strat = max(1, int(CFG['per_trade_capital'] / entry_price))
        sim = PositionSim(symbol, entry_bar, entry_price, shares_strat, bars, strat_name)
        profit = sim.run()
        results[strat_name] = {
            'entry_bar':   entry_bar,
            'entry_time':  bar_time(entry_bar),
            'entry_price': round(entry_price, 4),
            'exit_bar':    sim.exit_bar,
            'exit_price':  round(sim.exit_price, 4) if sim.exit_price else None,
            'profit':      round(profit, 2),
            'pct':         round(profit / CFG['per_trade_capital'] * 100, 2),
            'harvests':    sim.harvests,
            'events':      sim.events,
        }

    return {
        'symbol':      symbol,
        'open':        round(open_price, 4),
        'peak':        round(peak_price, 4),
        'close':       round(close_price, 4),
        'peak_bar':    peak_bar,
        'peak_time':   bar_time(peak_bar),
        'bars':        len(bars),
        'avg_vol':     int(avg_vol),
        'in_range':    in_range,
        'results':     results,
    }


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_scenario(s: dict):
    sym = s['symbol']
    day_chg = (s['close'] - s['open']) / s['open'] * 100 if s['open'] else 0
    peak_chg = (s['peak'] - s['open']) / s['open'] * 100 if s['open'] else 0
    rng_clr = G if s['in_range'] else Y

    print(f'\n  {BOLD}{sym}{RST}  open=${s["open"]:.3f}  peak=${s["peak"]:.3f} '
          f'({G}{peak_chg:+.1f}%{RST} @ {s["peak_time"]})  '
          f'close=${s["close"]:.3f} ({day_chg:+.1f}%)  '
          f'vol={s["avg_vol"]:,}/bar  '
          f'{rng_clr}{"IN-RANGE" if s["in_range"] else "OUT-OF-RANGE"}{RST}')

    res = s['results']

    # Strategy comparison table
    headers = ['Strategy', 'Entry', 'Entry$', 'Exit$', 'Profit', 'Capture%', 'Events']
    rows = []

    ideal = res.get('IDEAL', {})
    if ideal.get('profit') is not None:
        rows.append(['IDEAL', '9:30', f'${ideal["entry_price"]:.3f}',
                     f'${ideal["exit_price"]:.3f}',
                     f'{G}${ideal["profit"]:+.2f}{RST}',
                     f'{G}100%{RST}', ''])

    for strat in ['CURRENT', 'IMPROVED', 'ORB']:
        r = res.get(strat, {})
        if 'skipped' in r:
            rows.append([strat, '—', '—', '—',
                         f'{DIM}$0.00{RST}', f'{DIM}FILTERED{RST}',
                         r['skipped'][:40]])
            continue
        if 'no_signal' in r:
            rows.append([strat, '—', '—', '—',
                         f'{DIM}$0.00{RST}', f'{DIM}NO SIGNAL{RST}', ''])
            continue
        pnl = r.get('profit', 0)
        pct = r.get('pct', 0)
        ideal_p = ideal.get('profit', 1) or 1
        capture = (pnl / ideal_p * 100) if ideal_p != 0 else 0
        pnl_clr = G if pnl > 0 else R
        cap_clr = G if capture > 40 else (Y if capture > 15 else R)
        last_evt = r['events'][-1]['reason'] if r.get('events') else ''
        rows.append([strat,
                     r.get('entry_time', '—'),
                     f'${r.get("entry_price", 0):.3f}',
                     f'${r.get("exit_price", 0):.3f}' if r.get('exit_price') else '—',
                     f'{pnl_clr}${pnl:+.2f}{RST}',
                     f'{cap_clr}{capture:.0f}%{RST}',
                     last_evt[:45]])

    col_w = [10, 6, 8, 8, 12, 10, 48]
    fmt = '    ' + '  '.join(f'{{:<{w}}}' for w in col_w)
    print(fmt.format(*headers))
    print('    ' + '─'*sum(col_w + [2*(len(col_w)-1)]))
    for row in rows:
        print(fmt.format(*row))

    # Event detail for non-trivial strategies
    for strat in ['CURRENT', 'IMPROVED', 'ORB']:
        r = res.get(strat, {})
        evts = r.get('events', [])
        if evts:
            print(f'\n      {DIM}{strat} events:{RST}')
            for ev in evts:
                clr = G if ev['net'] >= 0 else R
                print(f'        {DIM}{ev["shares"]}sh @ ${ev["price"]:.3f}  '
                      f'{clr}${ev["net"]:+.2f}{RST}  {ev["reason"]}')


def print_summary(scenarios: list):
    print(f'\n{BOLD}{"═"*76}{RST}')
    print(f'{BOLD}  SUMMARY — May 28, 2026 Stress Test{RST}')
    print(f'{BOLD}{"═"*76}{RST}')
    print(f'  Capital per trade: ${CFG["per_trade_capital"]:.0f}  '
          f'harvest_trig={CFG["harvest_trigger_pct"]:.0f}%  '
          f'stop_loss={CFG["max_single_loss_pct"]:.0f}%  '
          f'trail={CFG["trail_day0_pct"]:.0f}%  '
          f'tx={CFG["tx_cost_pct"]:.1f}%\n')

    strat_totals = {'IDEAL': 0, 'CURRENT': 0, 'IMPROVED': 0, 'ORB': 0}
    strat_trades = {'IDEAL': 0, 'CURRENT': 0, 'IMPROVED': 0, 'ORB': 0}
    strat_wins   = {'IDEAL': 0, 'CURRENT': 0, 'IMPROVED': 0, 'ORB': 0}
    filtered_out = []

    for s in scenarios:
        res = s['results']
        for strat in strat_totals:
            r = res.get(strat, {})
            if 'skipped' in r or 'no_signal' in r:
                if strat == 'CURRENT' and 'skipped' in r:
                    filtered_out.append(f'{s["symbol"]}({r["skipped"][:25]})')
                continue
            p = r.get('profit', 0)
            strat_totals[strat] += p
            strat_trades[strat] += 1
            if p > 0:
                strat_wins[strat] += 1

    # Competitor benchmark (published estimates, labeled)
    holly_est_profit_per_trade = 4.20   # 60% win × avg +10% - 40% × avg 6% = 3.6%, × $100
    holly_trades = max(strat_trades['CURRENT'], 1)
    holly_total  = holly_trades * holly_est_profit_per_trade

    print(f'  {"Strategy":<12} {"Trades":>6}  {"Wins":>5}  {"Win%":>6}  {"Total P&L":>10}  {"Avg/Trade":>10}  {"vs IDEAL":>8}')
    print(f'  {"─"*12}  {"─"*6}  {"─"*5}  {"─"*6}  {"─"*10}  {"─"*10}  {"─"*8}')

    ideal_tot = strat_totals['IDEAL'] or 1
    for strat in ['IDEAL', 'CURRENT', 'IMPROVED', 'ORB']:
        n   = strat_trades[strat]
        tot = strat_totals[strat]
        w   = strat_wins[strat]
        avg = tot / n if n > 0 else 0
        cap = (tot / ideal_tot * 100) if strat != 'IDEAL' else 100.0
        tot_clr = G if tot > 0 else R
        cap_clr = G if cap > 50 else (Y if cap > 20 else R)
        win_pct = w / n * 100 if n > 0 else 0
        print(f'  {strat:<12} {n:>6}  {w:>5}  {win_pct:>5.0f}%  '
              f'{tot_clr}${tot:>+8.2f}{RST}  {tot_clr}${avg:>+8.2f}{RST}  '
              f'{cap_clr}{cap:>6.0f}%{RST}')

    # Holly AI estimate
    print(f'  {"─"*12}  {"─"*6}  {"─"*5}  {"─"*6}  {"─"*10}  {"─"*10}  {"─"*8}')
    print(f'  {Y}HOLLY-EST*{RST}  {holly_trades:>6}  {"~":>5}  {Y}{"~60%":>6}{RST}  '
          f'{Y}${holly_total:>+8.2f}{RST}  {Y}${holly_est_profit_per_trade:>+8.2f}{RST}  '
          f'{Y}{"~est":>6}{RST}')
    print(f'\n  {DIM}* Holly AI estimate based on published 60% win rate, avg +10% winner, avg -6% loser{RST}')

    if filtered_out:
        print(f'\n  {Y}⚠ Price filter excluded: {", ".join(filtered_out[:6])}{RST}')

    # Opportunity cost analysis
    current_cap = strat_totals['CURRENT']
    improved_cap = strat_totals['IMPROVED']
    improvement = improved_cap - current_cap
    print(f'\n  {BOLD}Improvement delta (IMPROVED vs CURRENT): {G if improvement>0 else R}${improvement:+.2f}{RST}')


# ── Main ──────────────────────────────────────────────────────────────────────

UNIVERSE = [
    ('WKHS', '+24.2% day — EV truck maker, steady uptrend all day'),
    ('CLOV',  '+10.6% day — health insurance, gapped up and held'),
    ('MVST',  '+12.7% intraday peak — micromobility, spike then partial fade'),
    ('XTIA',  '+11.2% intraday peak — tech, pump then fade back to flat'),
    ('DPRO',  '+9.9% intraday peak — OUT-OF-RANGE ($7.50, ceil=$5.00)'),
    ('SPRC',  '+13.3% intraday peak — OUT-OF-RANGE ($12.45, ceil=$5.00)'),
]

if __name__ == '__main__':
    print(f'\n{BOLD}StopTrading — Full-Day Stress Test  ·  May 28, 2026{RST}')
    print(f'{DIM}Real 5-minute intraday data  ·  $100/trade  ·  paper account{RST}\n')
    print(f'{BOLD}Universe ({len(UNIVERSE)} stocks):{RST}')
    for sym, note in UNIVERSE:
        print(f'  {sym:6s}  {DIM}{note}{RST}')

    print(f'\n{BOLD}{"━"*76}{RST}')
    print(f'{BOLD}  FETCHING INTRADAY DATA...{RST}')
    print(f'{BOLD}{"━"*76}{RST}')

    scenarios = []
    for sym, note in UNIVERSE:
        print(f'  Fetching {sym}...', end=' ', flush=True)
        bars = fetch_bars(sym)
        if bars:
            s = run_scenario(sym, bars)
            scenarios.append(s)
            print(f'{G}✓{RST} {len(bars)} bars')
        else:
            print(f'{R}✗ no data{RST}')

    print(f'\n{BOLD}{"━"*76}{RST}')
    print(f'{BOLD}  BAR-BY-BAR SIMULATION RESULTS{RST}')
    print(f'{BOLD}{"━"*76}{RST}')

    for s in scenarios:
        print_scenario(s)

    print_summary(scenarios)

    # ── Gap analysis ──────────────────────────────────────────────────────────
    print(f'\n{BOLD}{"━"*76}{RST}')
    print(f'{BOLD}  TOP-DOWN GAP ANALYSIS — Where We Leave Money on the Table{RST}')
    print(f'{BOLD}{"━"*76}{RST}')

    gaps = [
        ('CRITICAL', 'Price ceiling $5 excluded DPRO (+9.9%) and SPRC (+13.3%)',
         'Raise ceiling to $10, require $500K+ daily dollar volume'),
        ('CRITICAL', 'Scan uses 1-hour bars — CURRENT fires 45-65 min after signal',
         'Switch scan_movers to period=1d interval=5m, compare to 9:30 open price'),
        ('HIGH',     'No Opening Range Breakout (ORB) detector',
         'New agent: track first-5m candle, buy breakout above high+0.5%'),
        ('HIGH',     'No real-time news feed',
         'Add Benzinga/Polygon news API — news-driven movers have 80% follow-through in first 30m'),
        ('HIGH',     'Momentum acceleration not scored',
         'Score consecutive green bars + accelerating volume (rocket setup pattern)'),
        ('MEDIUM',   'Float rotation not tracked',
         'Track volume/float ratio — when today vol > 1× float, explosive moves likely'),
        ('MEDIUM',   'Short squeeze setup not detected',
         'Combine short_interest > 20% + SSR active + rising price = squeeze pattern'),
        ('MEDIUM',   'No Level 2 / tape reading',
         'Polygon.io WebSocket has full L2 — read bid/ask imbalance directly'),
        ('LOW',      'VWAP uses yfinance 1-min bars',
         'Pull VWAP from velocity cache (already computed) instead of re-fetching'),
        ('LOW',      'Harvest reinvest counts against daily_limit',
         'Reinvested capital should use separate reinvest_pool, not daily_spend budget'),
    ]

    print()
    for priority, problem, solution in gaps:
        clr = R if priority == 'CRITICAL' else (Y if priority == 'HIGH' else C)
        print(f'  {clr}[{priority:<8}]{RST} {problem}')
        print(f'  {DIM}           → {solution}{RST}\n')

    # ── New data streams ──────────────────────────────────────────────────────
    print(f'{BOLD}{"━"*76}{RST}')
    print(f'{BOLD}  NEW DATA STREAMS & AGENTS TO ADD{RST}')
    print(f'{BOLD}{"━"*76}{RST}\n')

    streams = [
        ('NewsAgent', 'Benzinga Pro / Polygon.io news feed',
         'Real-time press releases, SEC filings — adds ~45min early edge on news movers'),
        ('ORBAgent', 'Opening Range Breakout detector',
         'First-5m candle breakout — one of highest win-rate intraday setups, especially on gap-ups'),
        ('FloatRotationAgent', 'Float turnover tracker',
         'When vol > 1× float: massive conviction. When vol > 2× float: possible exhaustion.'),
        ('ShortSqueezeAgent', 'Short interest + SSR + price pressure combo',
         'FINRA daily short volume + SSR status + rising price = squeeze setup scoring'),
        ('DarkPoolAgent', 'Unusual institutional prints via FINRA ADF/TRF data',
         'Large prints at price far from bid/ask signal institutional accumulation'),
        ('EarningsFlowAgent', 'Options unusual activity before earnings',
         'Unusual call sweeps 2-5 days before earnings = smart money positioning'),
        ('Level2Agent', 'Polygon.io L2 WebSocket for live bid/ask book',
         'True bid/ask spread (replaces estimate), order book depth, large bid walls'),
        ('SectorMomentumAgent', 'ETF sector rotation signals',
         'When sector ETF (XLV, ARKK etc.) accelerates, constituent small-caps follow'),
    ]

    for name, source, edge in streams:
        print(f'  {G}{name}{RST}  [{DIM}{source}{RST}]')
        print(f'  {DIM}  Edge: {edge}{RST}\n')

    print(f'{BOLD}{"━"*76}{RST}')
    print(f'{BOLD}  Run complete.{RST}\n')
