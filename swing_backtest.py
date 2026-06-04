#!/usr/bin/env python3
"""
Swing backtest — the Harvester philosophy on the timeframe it was actually built for.

Two intraday experiments converged on the same lesson: intraday moves are smaller than the
round-trip cost, so neither penny-momentum nor liquid mean-reversion clears cost. The fix the
data points to is TIMEFRAME, not universe. On daily bars / multi-day holds, moves are 2-10%
(huge vs ~0.1% cost), the HFT speed disadvantage vanishes, and mean-reversion is far more robust.

This tests swing mean-reversion on liquid ETFs + large-caps over ~2 years of free daily history,
with a TREND FILTER (only buy dips in uptrends — fixes the falling-knife failure we saw), net of
cost, and benchmarks every variant against simply buying & holding SPY (the honest bar to beat).

Edge = positive mean net/trade AND beating buy-and-hold SPY on a risk-adjusted basis.
"""
import numpy as np
import pandas as pd
import yfinance as yf

UNIVERSE = ['SPY','QQQ','IWM','DIA','XLF','XLE','XLK','XLV','XLI','XLY','XLP','XLU','XLB','GLD','SLV',
            'EEM','EFA','TLT','HYG','AAPL','MSFT','JPM','XOM','KO','PG']
START, END = '2024-06-01', '2026-06-02'
COST_PCT = 0.10          # round-trip cost % (liquid; tiny spread + slippage)
MAX_HOLD = 25            # trading days max hold before forced exit

# Variants: dip depth, exit target, stop, and whether a long-trend filter is applied.
VARIANTS = {
    'mr_notrend': dict(entry_z=-1.5, exit_z=0.0, stop=-8.0, trend=False),
    'mr_trend':   dict(entry_z=-1.5, exit_z=0.0, stop=-8.0, trend=True),    # only buy dips above SMA200
    'mr_deep':    dict(entry_z=-2.0, exit_z=0.0, stop=-10.0, trend=True),
    'mr_quick':   dict(entry_z=-1.5, exit_z=0.5, stop=-6.0, trend=True),    # take a small overshoot past mean
}


def simulate(df, p):
    """Daily swing mean-reversion on one symbol. Returns list of net %% per trade + hold days."""
    c = df['Close'].values
    n = len(c)
    if n < 220:
        return []
    s = pd.Series(c)
    sma20 = s.rolling(20).mean().values
    std20 = s.rolling(20).std().values
    sma200 = s.rolling(200).mean().values
    trades = []
    i = 200
    while i < n - 1:
        sd = std20[i]
        if not np.isfinite(sd) or sd <= 0:
            i += 1; continue
        z = (c[i] - sma20[i]) / sd
        trend_ok = (not p['trend']) or (np.isfinite(sma200[i]) and c[i] > sma200[i])
        if z <= p['entry_z'] and trend_ok:
            entry = c[i]
            # hold until reversion to mean (z>=exit_z), stop, or max hold
            for j in range(i + 1, min(i + 1 + MAX_HOLD, n)):
                ret = (c[j] - entry) / entry * 100.0
                zj = (c[j] - sma20[j]) / std20[j] if (np.isfinite(std20[j]) and std20[j] > 0) else 0.0
                if zj >= p['exit_z'] or ret <= p['stop'] or j == min(i + MAX_HOLD, n - 1):
                    trades.append((ret - COST_PCT, j - i))
                    i = j
                    break
            else:
                i += 1
        else:
            i += 1
    return trades


def main():
    print('SWING BACKTEST — %d liquid names, daily bars %s→%s, cost %.2f%%/round-trip\n'
          % (len(UNIVERSE), START, END, COST_PCT), flush=True)
    data = yf.download(UNIVERSE, start=START, end=END, interval='1d',
                       group_by='ticker', auto_adjust=True, progress=False, threads=True)
    # SPY buy & hold benchmark over the window
    spy = data['SPY']['Close'].dropna()
    spy_years = len(spy) / 252.0
    spy_total = (spy.iloc[-1] / spy.iloc[0] - 1) * 100
    spy_cagr = ((spy.iloc[-1] / spy.iloc[0]) ** (1 / spy_years) - 1) * 100
    print('Benchmark: buy & hold SPY  →  %+.1f%% total  (%.1f%%/yr CAGR) over %.1f yrs\n' % (
        spy_total, spy_cagr, spy_years))

    print('=' * 86)
    print('%-11s %7s %7s %9s %9s %8s %9s %10s' % (
        'variant','trades','win%','mean net','median','avg hold','total net','~ann. ret'))
    print('-' * 86)
    summary = {}
    for vname, p in VARIANTS.items():
        allt = []
        for sym in UNIVERSE:
            try:
                df = data[sym].dropna()
            except Exception:
                continue
            allt.extend(simulate(df, p))
        if not allt:
            print('%-11s   (no trades)' % vname); continue
        rets = np.array([t[0] for t in allt]); holds = np.array([t[1] for t in allt])
        wr = float((rets > 0).mean() * 100)
        # rough annualized: equal-weight sequential proxy — total net spread over the window years
        ann = rets.sum() / spy_years
        summary[vname] = dict(n=len(rets), win=wr, mean=float(rets.mean()), total=float(rets.sum()), ann=ann)
        print('%-11s %7d %6.1f%% %+8.3f%% %+8.3f%% %7.1fd %+8.1f%% %+9.1f%%' % (
            vname, len(rets), wr, rets.mean(), np.median(rets), holds.mean(), rets.sum(), ann))

    print('\nVERDICT:')
    edge = [(v, s) for v, s in summary.items() if s['mean'] > 0 and s['win'] > 50]
    if edge:
        best = max(edge, key=lambda kv: kv[1]['mean'])
        v, s = best
        print('  ✓ POSITIVE EXPECTANCY — %s: %.1f%% win, %+.3f%% mean net/trade over %d trades (avg hold matters).' % (
            v, s['win'], s['mean'], s['n']))
        print('    This is the engine\'s native timeframe. Compare its risk-adjusted return to SPY before pivoting.')
    else:
        print('  ✗ No variant clears positive net expectancy with >50%% win even on the swing timeframe.')
        print('    Strong signal that systematic retail edge here is thin — the honest move may be a')
        print('    passive/core approach rather than active trading. (Per-trade %, net of cost.)')
    print('\n  The bar that matters: beat buy & hold SPY (%+.1f%%/yr) on a RISK-ADJUSTED basis, not just gross.' % spy_cagr)


if __name__ == '__main__':
    main()
