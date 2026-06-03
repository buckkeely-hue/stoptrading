#!/usr/bin/env python3
"""
Spread ↔ harvest-target dynamics — is there a better scalable range?

Two layers:
  A) DETERMINISTIC cost/gate identity (needs no data): given the spread guard
     (eligible spread ≤ 40% of harvest target), what does a trade NET when it hits target,
     and what is the highest spread a target can economically tolerate ("scalable ceiling")?
  B) EMPIRICAL EV: replay the realized triple-barrier returns we've collected through each
     candidate harvest target, net of the spread-aware cost, to estimate expected value.

Layer A is exact today. Layer B is only as good as the (currently small) barrier sample —
read it as directional, not conclusive, until n grows.
"""
import json
import os

from config import load_config
from modules.tx_cost import transaction_cost_pct
from modules.model_eval import load_rows

CFG = load_config()
SLIP = float(CFG.get('slippage_buffer_pct', 0.5))
GATE_FRAC = 0.40   # spread guard: eligible spread ≤ 40% of harvest target (harvester.py)


def cost_at_spread(spread_pct):
    # reg fee is ~0.02–0.04% for penny notionals; fold the flat reg default in
    return transaction_cost_pct(spread_pct, None, None, CFG) if spread_pct > 0 else SLIP + 0.02


def layer_a():
    print('A) DETERMINISTIC cost/gate identity  (slippage buffer = %.2f%%)' % SLIP)
    print('   net_if_hit = target − (spread + slippage + reg);  spread ceiling = 40%% of target\n')
    print('   %-8s %-12s %-12s %-14s %-12s' % ('target', 'spread_ceil', 'cost@ceil', 'net@ceil', 'net@1%spread'))
    print('   ' + '-' * 62)
    for T in (2, 3, 4, 5, 6, 8, 10):
        ceil = GATE_FRAC * T
        cost_ceil = cost_at_spread(ceil)
        net_ceil = T - cost_ceil
        net_tight = T - cost_at_spread(1.0)
        print('   %5.0f%%   %8.1f%%    %8.2f%%    %+9.2f%%    %+8.2f%%' % (
            T, ceil, cost_ceil, net_ceil, net_tight))
    # break-even target at the gate ceiling: 0.6*T = slippage+reg  → T*(1-0.4) = SLIP+0.02
    be = (SLIP + 0.02) / (1 - GATE_FRAC)
    print('\n   Break-even target at the gate ceiling: %.2f%% (below this, a worst-case-spread '
          'fill loses even on a WIN)\n' % be)


def layer_b():
    rows = load_rows()                       # delay-immune (replay/legacy) barrier returns
    rets = [float(r['ret']) for r in rows if r.get('ret') is not None]
    n = len(rets)
    print('B) EMPIRICAL EV over realized barrier returns  (n=%d)' % n)
    if n < 10:
        print('   ⚠ sample too small for a stable estimate — directional only.\n')
    if not rets:
        return
    print('   %-8s %-10s %-12s %-12s %-12s' % ('target', 'P(hit)', 'spread_ceil', 'cost', 'EV/trade'))
    print('   ' + '-' * 58)
    for T in (2, 3, 4, 5, 6, 8, 10):
        ceil = GATE_FRAC * T
        cost = cost_at_spread(ceil)
        p_hit = sum(1 for r in rets if r >= T) / n
        # Exit model: hit target → net +(T−cost). Else realize the (clipped) barrier return − cost.
        ev = 0.0
        for r in rets:
            if r >= T:
                ev += (T - cost)
            else:
                ev += (max(-15.0, r) - cost)
        ev /= n
        print('   %5.0f%%   %7.2f    %8.1f%%    %7.2f%%    %+9.3f%%' % (T, p_hit, ceil, cost, ev))
    print()
    # also show the shape that drives it
    buckets = [(-99, 0), (0, 2), (2, 4), (4, 6), (6, 10), (10, 999)]
    print('   realized-return shape:')
    for lo, hi in buckets:
        c = sum(1 for r in rets if lo <= r < hi)
        print('     [%4.0f%%, %4.0f%%): %2d  %s' % (lo, hi, c, '█' * c))
    print()


if __name__ == '__main__':
    print('=' * 66)
    print('SPREAD ↔ HARVEST-TARGET DYNAMICS')
    print('  current: harvest=%.1f%%  exit=%.1f%%  stop=%.1f%%' % (
        CFG.get('harvest_trigger_pct', 4), CFG.get('exit_trigger_pct', 2),
        CFG.get('max_single_loss_pct', 5)))
    print('=' * 66 + '\n')
    layer_a()
    layer_b()
