#!/usr/bin/env python3
"""
Trend-following multi-regime test — the final probe.

Mean-reversion underperformed buy-and-hold because it FIGHTS the market's drift (sells winners,
sits in cash). Trend-following does the opposite: buy strength, ride it, exit when the trend
breaks. It's the one systematic style with robust cross-regime academic support (time-series
momentum / managed futures) — it should capture bull drift AND go to cash when trends break
(sidestepping bear drawdowns).

Same capital-constrained portfolio engine, same 3 regimes, same SPY benchmark as the
mean-reversion test — directly comparable. Verdict bar (unchanged): beat buy-and-hold SPY on a
RISK-ADJUSTED basis (higher Sharpe AND shallower drawdown) across regimes. If it does, we've
finally found a real edge. If not, the honest answer is passive/core.
"""
import numpy as np
import pandas as pd
import yfinance as yf

UNIVERSE = ['SPY','QQQ','IWM','DIA','XLF','XLE','XLK','XLV','XLI','XLY','XLP','XLU','XLB','GLD','SLV',
            'EEM','EFA','TLT','HYG','AAPL','MSFT','JPM','XOM','KO','PG']
DL_START = '2019-01-01'
COST_SIDE = 0.0005
MAX_POS  = 8
START_EQ = 100_000.0

REGIMES = {
    '2020_crash':   ('2020-01-01', '2020-12-31'),
    '2022_bear':    ('2022-01-01', '2022-12-31'),
    '2023_26_bull': ('2023-01-01', '2026-06-02'),
}
# type: 'cross' MA-cross, 'breakout' Donchian, 'trail' cross-entry + trailing stop
VARIANTS = {
    'tf_cross':    dict(type='cross'),                  # in when close>SMA50 & SMA50>SMA200; out when close<SMA50
    'tf_breakout': dict(type='breakout', hi=50, lo=20), # in on 50d high (above SMA200); out on 20d low
    'tf_trail':    dict(type='trail', trail=0.12),      # cross entry; out on 12% trail-from-peak or close<SMA200
}


def metrics(equity):
    e = equity.dropna()
    if len(e) < 5:
        return (0, 0, 0, 0)
    total = (e.iloc[-1] / e.iloc[0] - 1) * 100
    yrs = len(e) / 252.0
    cagr = ((e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1) * 100 if e.iloc[0] > 0 else 0
    dd = ((e / e.cummax()) - 1).min() * 100
    dr = e.pct_change().dropna()
    sharpe = (dr.mean() / dr.std() * np.sqrt(252)) if dr.std() > 0 else 0
    return (total, cagr, dd, sharpe)


def run_portfolio(closes, ind, dates, p):
    cash = START_EQ; pos = {}; eq = []; trades = 0; wins = 0
    def entry_sig(sym, di):
        px = closes[sym].iloc[di]; I = ind[sym]
        if not np.isfinite(px): return False
        s50, s200 = I['sma50'].iloc[di], I['sma200'].iloc[di]
        if p['type'] == 'breakout':
            hh = I['hi'].iloc[di]
            return np.isfinite(hh) and np.isfinite(s200) and px >= hh and px > s200
        # cross / trail share the same long-trend entry
        return np.isfinite(s50) and np.isfinite(s200) and px > s50 and s50 > s200
    def exit_sig(sym, di, P):
        px = closes[sym].iloc[di]; I = ind[sym]
        if not np.isfinite(px): return False
        if p['type'] == 'breakout':
            ll = I['lo'].iloc[di]
            return np.isfinite(ll) and px <= ll
        if p['type'] == 'trail':
            s200 = I['sma200'].iloc[di]
            return px <= P['peak'] * (1 - p['trail']) or (np.isfinite(s200) and px < s200)
        s50 = I['sma50'].iloc[di]
        return np.isfinite(s50) and px < s50

    for di, d in enumerate(dates):
        # exits
        for sym in list(pos.keys()):
            px = closes[sym].iloc[di]
            if not np.isfinite(px):
                continue
            P = pos[sym]
            if px > P['peak']:
                P['peak'] = px
            if exit_sig(sym, di, P):
                ret = (px - P['entry']) / P['entry'] * 100
                cash += P['shares'] * px * (1 - COST_SIDE)
                trades += 1
                if ret - 2 * COST_SIDE * 100 > 0:
                    wins += 1
                del pos[sym]
        # entries
        if len(pos) < MAX_POS:
            eq_now = cash + sum(pos[s]['shares'] * closes[s].iloc[di] for s in pos if np.isfinite(closes[s].iloc[di]))
            for sym in closes:
                if len(pos) >= MAX_POS:
                    break
                if sym in pos:
                    continue
                if entry_sig(sym, di):
                    px = closes[sym].iloc[di]
                    alloc = min(eq_now / MAX_POS, cash)
                    if alloc < 100:
                        continue
                    shares = alloc / px
                    cash -= shares * px * (1 + COST_SIDE)
                    pos[sym] = dict(shares=shares, entry=px, peak=px)
        mtm = cash + sum(pos[s]['shares'] * closes[s].iloc[di] for s in pos if np.isfinite(closes[s].iloc[di]))
        eq.append(mtm)
    return pd.Series(eq, index=dates), trades, wins


def main():
    print('TREND-FOLLOWING MULTI-REGIME TEST — capital-constrained portfolio ($%.0fk, max %d)\n'
          % (START_EQ / 1000, MAX_POS), flush=True)
    raw = yf.download(UNIVERSE, start=DL_START, end='2026-06-02', interval='1d',
                      group_by='ticker', auto_adjust=True, progress=False, threads=True)
    cal = raw['SPY']['Close'].dropna().index
    closes_full = {}; ind_full = {}
    for sym in UNIVERSE:
        try:
            c = raw[sym]['Close'].reindex(cal).ffill()
        except Exception:
            continue
        closes_full[sym] = c
        ind_full[sym] = {
            'sma50': c.rolling(50).mean(), 'sma200': c.rolling(200).mean(),
            'hi': c.rolling(50).max().shift(1), 'lo': c.rolling(20).min().shift(1),
        }

    for rname, (rs, re_) in REGIMES.items():
        mask = (cal >= rs) & (cal <= re_); dates = cal[mask]
        st, sc, sdd, ssh = metrics(closes_full['SPY'].loc[dates])
        print('=' * 84)
        print('REGIME %s  (%s → %s)   SPY: %+.1f%% total, %.1f%% CAGR, %.1f%% maxDD, Sharpe %.2f'
              % (rname, rs, re_, st, sc, sdd, ssh))
        print('-' * 84)
        print('  %-11s %8s %8s %8s %8s %7s %7s' % ('variant','total%','CAGR%','maxDD%','Sharpe','trades','win%'))
        for vname, p in VARIANTS.items():
            cl = {s: closes_full[s].loc[dates] for s in closes_full}
            ind = {s: {k: ind_full[s][k].loc[dates] for k in ind_full[s]} for s in closes_full}
            eq, trades, wins = run_portfolio(cl, ind, dates, p)
            t, cg, dd, sh = metrics(eq)
            wr = (wins / trades * 100) if trades else 0
            flag = '  ✓ beats SPY risk-adj' if (sh > ssh and dd > sdd) else ''
            print('  %-11s %+8.1f %+8.1f %8.1f %8.2f %7d %6.1f%%%s' % (vname, t, cg, dd, sh, trades, wr, flag))
        print()

    print('READ: trend-following should capture bull drift (2023-26 near/above SPY) AND cut to cash in')
    print('the 2022 bear (shallow drawdown). "Beats SPY risk-adj" across all 3 regimes = a real, all-weather')
    print('edge worth building. If it only matches/trails SPY, the honest answer is passive/core indexing.')


if __name__ == '__main__':
    main()
