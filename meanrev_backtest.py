#!/usr/bin/env python3
"""
Mean-reversion backtest — the Harvester philosophy in its NATIVE habitat.

Tests whether "harvest the predictable middle" clears costs when pointed at the right market:
liquid ETFs (tight spreads, deep liquidity, real intraday mean-reversion) with a mean-reversion
entry (buy dips below the rolling mean, sell the reversion) instead of penny-momentum chasing.

Delay-immune: runs on real historical 1-minute bars across settled days — same free, accurate
validation method as the penny sweep, so the two are directly comparable. Pure vectorized sim
(no broker, no real money) — this answers "is there an edge in this pond?" before any rewrite.

Metric that matters: mean NET return per trade (after cost) and win rate. Penny harvesting gave
~10% win vs ~54% breakeven (negative expectancy). If liquid mean-reversion flips that, the
engine belongs here.
"""
import sys, os
from datetime import date, timedelta
import numpy as np
import pandas as pd
import yfinance as yf

# Liquid ETFs: index + sector + commodity. Tight spreads (~0.01-0.04%), deep liquidity,
# well-documented intraday mean-reversion. The opposite of the penny universe.
UNIVERSE = ['SPY','QQQ','IWM','DIA','XLF','XLE','XLK','XLV','XLI','XLY','XLP','XLU','GLD','SLV','EEM','TLT','HYG']
WINDOW   = 20      # rolling bars for the mean/std band
COST_PCT = 0.06    # round-trip cost % for liquid ETFs (tiny spread + slippage buffer)
N_DAYS   = 16

# Variants: how deep a dip to buy, where to sell the reversion, how tight the stop.
VARIANTS = {
    'mr_to_mean': dict(entry_z=-1.5, exit_z=0.0,  stop_pct=-1.2),   # buy -1.5σ dip, sell back at mean
    'mr_half':    dict(entry_z=-1.5, exit_z=-0.5, stop_pct=-1.0),   # take half the reversion
    'mr_tight':   dict(entry_z=-1.0, exit_z=0.0,  stop_pct=-0.8),   # shallower dip, tighter stop
    'mr_deep':    dict(entry_z=-2.0, exit_z=0.0,  stop_pct=-1.5),   # only deep dips
}


def trading_days(n):
    d = date(2026, 6, 2)
    out = []
    while len(out) < n:
        if d.weekday() < 5 and not (d.month == 5 and d.day == 25):
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def simulate(closes, p):
    """One symbol-day: long-only intraday mean-reversion. Returns list of NET %% returns per trade."""
    n = len(closes)
    if n < WINDOW + 5:
        return []
    s = pd.Series(closes)
    rmean = s.rolling(WINDOW).mean().values
    rstd  = s.rolling(WINDOW).std().values
    trades = []
    in_pos = False; entry = 0.0
    for i in range(WINDOW, n):
        sd = rstd[i]
        if not np.isfinite(sd) or sd <= 0:
            continue
        z = (closes[i] - rmean[i]) / sd
        if not in_pos:
            if z <= p['entry_z']:
                in_pos = True; entry = closes[i]
        else:
            ret = (closes[i] - entry) / entry * 100.0
            if z >= p['exit_z'] or ret <= p['stop_pct'] or i == n - 1:
                trades.append(ret - COST_PCT)   # net of round-trip cost
                in_pos = False
    return trades


def fetch_day(day):
    """Batch 1-min bars for the whole universe on one settled day. {sym: np.array(closes)}."""
    try:
        df = yf.download(UNIVERSE, start=day.isoformat(), end=(day + timedelta(days=1)).isoformat(),
                         interval='1m', group_by='ticker', auto_adjust=True, progress=False, threads=True)
    except Exception:
        return {}
    out = {}
    for sym in UNIVERSE:
        try:
            sub = df[sym] if len(UNIVERSE) > 1 else df
            c = sub['Close'].dropna().values
            if len(c) > WINDOW + 5:
                out[sym] = c
        except Exception:
            continue
    return out


def main():
    days = trading_days(N_DAYS)
    print('MEAN-REVERSION BACKTEST — %d liquid ETFs x %d settled days (cost %.2f%%/round-trip)\n'
          % (len(UNIVERSE), len(days), COST_PCT), flush=True)
    # download once, reuse across variants
    daydata = {}
    for d in days:
        bars = fetch_day(d)
        daydata[d] = bars
        print('  data %s: %d symbols' % (d.isoformat(), len(bars)), flush=True)

    print('\n' + '=' * 80)
    print('RESULTS  (NET = after %.2f%% round-trip cost; penny harvesting was ~10%% win, negative EV)' % COST_PCT)
    print('=' * 80)
    print('%-11s %7s %7s %9s %9s %9s %9s' % ('variant','trades','win%','mean net','median','total net','net/day'))
    print('-' * 80)
    summary = {}
    for vname, p in VARIANTS.items():
        allt = []
        for d in days:
            for sym, closes in daydata.get(d, {}).items():
                allt.extend(simulate(closes, p))
        if not allt:
            print('%-11s   (no trades)' % vname); continue
        arr = np.array(allt)
        wr = float((arr > 0).mean() * 100)
        summary[vname] = dict(n=len(arr), win=wr, mean=float(arr.mean()), total=float(arr.sum()))
        print('%-11s %7d %6.1f%% %+8.3f%% %+8.3f%% %+8.2f%% %+8.2f%%' % (
            vname, len(arr), wr, arr.mean(), np.median(arr), arr.sum(), arr.sum() / len(days)))
    # verdict
    print('\nVERDICT:')
    pos = [(v, s) for v, s in summary.items() if s['mean'] > 0 and s['win'] > 50]
    if pos:
        best = max(pos, key=lambda kv: kv[1]['mean'])
        print('  ✓ EDGE FOUND — %s: %.1f%% win, %+.3f%% mean net/trade over %d trades.' % (
            best[0], best[1]['win'], best[1]['mean'], best[1]['n']))
        print('    Positive expectancy AFTER cost in the engine\'s native habitat. Worth a real pivot.')
    else:
        print('  ✗ No variant shows positive net expectancy with >50%% win. Mean-reversion on these')
        print('    names/params does not clear cost either — try multi-day timeframe or other instruments.')
    print('\n  (Per-trade %. A 0.1%%/trade edge x many trades/day x compounding is how income strategies work.)')


if __name__ == '__main__':
    main()
