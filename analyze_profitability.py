#!/usr/bin/env python3
"""
Profitability concept for the harvesting strategy — success % and profit return when targeting
the predictable MIDDLE instead of the optimal highs/lows.

The middle-ground bet is structurally different from momentum:
  • Momentum: LOW win rate, HIGH reward:risk (rare big rips pay for many small losses).
  • Harvesting: HIGH win rate, LOW reward:risk (frequent small wins, fast small losses).
So a harvesting engine LIVES OR DIES ON WIN RATE. This quantifies exactly what win rate the
current target/stop/cost demand, what each scenario returns, and how the live data compares.
"""
import os, json
from config import load_config
from modules.tx_cost import transaction_cost_pct
from modules.model_eval import load_rows

C = load_config()
T = float(C.get('harvest_trigger_pct', 4.0))     # net target captured on a win
S = float(C.get('max_single_loss_pct', 3.0))     # stop (loss leg)


def breakeven(T, S, c):
    """Win rate at which expectancy = 0:  p(T−c) = (1−p)(S+c)  ⇒  p = (S+c)/(T+S)."""
    return (S + c) / (T + S)


def expectancy(p, T, S, c):
    """Net expected return per round-trip, in % of deployed capital."""
    return p * (T - c) - (1 - p) * (S + c)


def section(title): print('\n' + title + '\n' + '-' * len(title))


print('=' * 70)
print('HARVESTING PROFITABILITY MODEL   target=%.1f%%  stop=%.1f%%' % (T, S))
print('=' * 70)

section('1) SUCCESS %% REQUIRED — breakeven win rate by transaction cost')
print('   reward:risk before cost = %.2f : 1  (this is why win rate, not size, is the lever)' % (T / S))
print('   %-12s %-16s %-s' % ('cost', 'breakeven win%', 'margin of safety needed'))
for c in (0.5, 1.0, 1.5, 2.0, 2.5):
    be = breakeven(T, S, c)
    print('   %-12s %-16s need win%% comfortably above %.0f%%' % ('%.1f%%' % c, '%.1f%%' % (be * 100), be * 100))

section('2) PROFIT RETURN — net expectancy per round-trip (%% of capital deployed)')
print('   %-10s' % 'win%' + ''.join('  c=%.1f%%' % c for c in (1.0, 1.5, 2.0)))
for p in (0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80):
    row = '   %-10s' % ('%.0f%%' % (p * 100))
    for c in (1.0, 1.5, 2.0):
        e = expectancy(p, T, S, c)
        row += '  %+6.2f%%' % e
    print(row)

section('3) WHAT IT COMPOUNDS TO — illustrative, at a representative cost')
c_rep = 1.5
print('   Assumes ~N independent round-trips/day on deployed capital; 252 trading days.')
print('   %-8s %-12s %-14s %-16s' % ('win%', 'E/trade', 'E/day (3 trips)', 'annual (compounded)'))
for p in (0.55, 0.62, 0.65, 0.70, 0.75):
    e = expectancy(p, T, S, c_rep) / 100.0
    trips = 3
    daily = (1 + e) ** trips - 1
    annual = (1 + daily) ** 252 - 1
    print('   %-8s %+10.2f%% %+12.2f%% %16s' % (
        '%.0f%%' % (p * 100), e * 100, daily * 100,
        ('%+.0f%%' % (annual * 100)) if abs(annual) < 50 else ('%.0fx' % (1 + annual))))
print('   (compounding cuts both ways — below breakeven it decays just as fast)')

section('4) LIVE DATA — actual barrier win rate vs the breakeven it must beat')
rows = load_rows()
rets = [float(r['ret']) for r in rows if r.get('ret') is not None]
n = len(rets)
if n:
    wins_T = sum(1 for r in rets if r >= T)              # would have hit the target
    loss_S = sum(1 for r in rets if r <= -S)             # would have hit the stop
    p_emp = wins_T / n
    # use a representative observed cost if spread present, else flat
    spreads = [float(r['f'].get('spread_pct', 0)) for r in rows if r.get('f')]
    sp = sorted([s for s in spreads if s > 0])
    med_sp = sp[len(sp) // 2] if sp else 0.0
    c_obs = transaction_cost_pct(med_sp, None, None, C) if med_sp else float(C.get('tx_cost_pct', 1.5))
    be = breakeven(T, S, c_obs)
    print('   n=%d barrier outcomes (delay-immune)' % n)
    print('   hit target (≥%.0f%%): %d   |   hit stop (≤-%.0f%%): %d' % (T, wins_T, S, loss_S))
    print('   empirical win rate : %.0f%%' % (p_emp * 100))
    print('   median spread obs  : %.2f%%   ⇒   modeled cost %.2f%%' % (med_sp, c_obs))
    print('   breakeven win rate : %.0f%%' % (be * 100))
    verdict = 'ABOVE breakeven → +EV' if p_emp > be else 'BELOW breakeven → −EV (selection must lift win rate)'
    print('   expectancy @ live  : %+.2f%%/trade   → %s' % (expectancy(p_emp, T, S, c_obs), verdict))
    print('   ⚠ small sample — directional; this is the metric the paper phase must move.')
else:
    print('   no barrier data yet')
print()
