#!/usr/bin/env python3
"""
Crypto grid backtest — does the "harvest the margins, both sides, set-and-forget" model work
in spot crypto where the penny universe failed? A grid trades PRICE LEVELS, not predictions
(so it sidesteps the inverted scorer entirely): buy one lot each time price crosses a grid line
DOWN, sell one lot each time it crosses a line UP — capturing one grid step (minus fees) per
oscillation. Inventory is capped so a trend can't accumulate unbounded losing lots.

The question this answers: grid net (after Alpaca fees) vs buy-and-hold, split by RANGING vs
TRENDING months — i.e. when does this make money and how badly does it bleed when it doesn't.

Data: yfinance hourly bars (BTC-USD/ETH-USD/SOL-USD). Side-effect-free; touches no live state.
"""
import bisect
import numpy as np
import yfinance as yf

CAPITAL    = 500.0      # account size
FEE_SIDE   = 0.0025     # Alpaca crypto taker fee per side (~0.25%, conservative entry tier)
MAX_LOTS   = 20         # inventory cap — most lots held at once (caps trend drawdown)
SYMBOLS    = ['BTC-USD', 'ETH-USD', 'SOL-USD']
STEPS      = [0.008, 0.012, 0.020]   # grid spacing to sweep (0.8% / 1.2% / 2.0%)
PERIOD     = '1y'
MA_WINDOW  = 200        # trend filter: only buy dips while price >= this many-hour moving avg
TREND_THRESH = 0.15     # |buy-hold monthly move| above this ⇒ call the month "trending"


def grid_levels(lo, hi, step):
    """Geometric grid lines spanning a band around the observed range."""
    lines, p = [], lo * 0.92
    top = hi * 1.08
    while p <= top:
        lines.append(p); p *= (1.0 + step)
    return lines


def moving_avg(prices, window):
    """Trailing simple moving average aligned to prices (warmup uses the running mean)."""
    c = np.cumsum(np.insert(prices, 0, 0.0))
    out = np.empty(len(prices))
    for i in range(len(prices)):
        a = max(0, i - window + 1)
        out[i] = (c[i + 1] - c[a]) / (i + 1 - a)
    return out


def run_grid(prices, step, ma=None):
    """Crossing-based spot grid with CORRECT rung pairing: a lot bought at line k is sold when
    price next crosses line k+1 up — realizing exactly one grid step (minus fees). Inventory
    (open buy rungs) is capped at MAX_LOTS.

    Trend filter: when `ma` is supplied, dips are only bought while price >= its moving average
    (don't catch falling knives); sells into strength always fire, so a downtrend sheds inventory
    and stops adding to it. ma=None reproduces the unfiltered grid."""
    lines = grid_levels(prices.min(), prices.max(), step)
    lot_value = CAPITAL / MAX_LOTS
    held = {}                 # line index k -> count of lots bought at lines[k] (sell at k+1)
    n_held = 0
    realized = 0.0; fees = 0.0; trips = 0
    prev = prices[0]
    for i in range(1, len(prices)):
        p = prices[i]
        if p == prev:
            continue
        lo, hi = (prev, p) if p > prev else (p, prev)
        k0 = bisect.bisect_right(lines, lo)
        k1 = bisect.bisect_right(lines, hi)
        if p > prev:                                  # up-cross line k → sell lot bought at k-1
            for k in range(k0, k1):
                if held.get(k - 1, 0) > 0:
                    held[k - 1] -= 1; n_held -= 1
                    buy_px, sell_px = lines[k - 1], lines[k]
                    qty = lot_value / buy_px
                    fee = (qty * sell_px + qty * buy_px) * FEE_SIDE
                    realized += qty * (sell_px - buy_px) - fee; fees += fee; trips += 1
        else:                                         # down-cross line k → buy a lot at k
            if ma is not None and p < ma[i]:
                prev = p; continue                    # trend filter: no dip-buying below the MA
            for k in range(k1 - 1, k0 - 1, -1):
                if n_held < MAX_LOTS:
                    held[k] = held.get(k, 0) + 1; n_held += 1
        prev = p
    inv_qty = sum(cnt * (lot_value / lines[k]) for k, cnt in held.items())
    inv_cost = lot_value * n_held
    unrealized = inv_qty * prices[-1] - inv_cost      # mark held inventory to last price
    return {'realized': realized, 'unrealized': unrealized, 'fees': fees,
            'trips': trips, 'held': n_held, 'net': realized + unrealized}


def monthly_segments(idx, prices):
    """Yield (label, start_idx, end_idx, buy-hold return) per calendar month."""
    months = {}
    for i, t in enumerate(idx):
        months.setdefault((t.year, t.month), []).append(i)
    for (y, m), ids in sorted(months.items()):
        a, b = ids[0], ids[-1] + 1
        if b - a < 24:
            continue
        yield ('%04d-%02d' % (y, m), a, b, prices[b - 1] / prices[a] - 1.0)


def main():
    for sym in SYMBOLS:
        df = yf.download(sym, period=PERIOD, interval='1h', auto_adjust=True, progress=False)
        if df is None or df.empty:
            print('%s: no data' % sym); continue
        close = df['Close'].dropna()
        prices = np.asarray(close.values, dtype=float).ravel()
        idx = close.index
        bh_total = prices[-1] / prices[0] - 1.0
        ma = moving_avg(prices, MA_WINDOW)
        print('\n=== %s ===  %d hourly bars | buy-hold over window: %+.1f%%'
              % (sym, len(prices), bh_total * 100))
        # sweep grid steps; unfiltered vs trend-filtered, full window
        print('  grid-step sweep (after %.2f%%/side fees, cap %d lots) — UNFILTERED vs %d-hr-MA FILTER:'
              % (FEE_SIDE * 100, MAX_LOTS, MA_WINDOW))
        best = None
        for step in STEPS:
            u = run_grid(prices, step)
            f = run_grid(prices, step, ma=ma)
            print('    step %4.1f%%  | unfilt net $%+7.2f (%+5.1f%%) trips %4d held %2d'
                  '  | FILTER net $%+7.2f (%+5.1f%%) realized $%+7.2f unreal $%+7.2f trips %4d held %2d'
                  % (step * 100, u['net'], u['net'] / CAPITAL * 100, u['trips'], u['held'],
                     f['net'], f['net'] / CAPITAL * 100, f['realized'], f['unrealized'], f['trips'], f['held']))
            if best is None or f['net'] > best[1]['net']:
                best = (step, f)
        step = best[0]
        print('  monthly regime breakdown @ step %.1f%% WITH filter  (grid net vs buy-hold):' % (step * 100))
        for label, a, b, bh in monthly_segments(idx, prices):
            r = run_grid(prices[a:b], step, ma=ma[a:b])
            regime = 'TREND ' if abs(bh) >= TREND_THRESH else 'range '
            print('    %s  %s  bh %+6.1f%%   grid net $%+7.2f (%+5.1f%%)  trips %d'
                  % (label, regime, bh * 100, r['net'], r['net'] / CAPITAL * 100, r['trips']))


if __name__ == '__main__':
    main()
