#!/usr/bin/env python3
"""
Multi-regime robustness test — does the swing edge survive a bear market, or is it bull-beta?

The single-window swing backtest looked great (74-79% win) but ran entirely in a bull market,
where "buy the dip" always wins. This runs a REAL capital-constrained portfolio (fixed equity,
max concurrent positions, daily mark-to-market, cost on both sides) SEPARATELY across three
regimes — 2020 crash, 2022 bear, 2023-26 bull — and reports CAGR / max-drawdown / Sharpe vs
buy-and-hold SPY in each.

The tell: if `mr_notrend` (no trend filter) collapses in 2022 while `mr_trend` holds up, the
edge is real and the trend filter is what makes it all-weather. If BOTH crater in 2022, the
"edge" was just a bull market wearing a strategy costume.
"""
import numpy as np
import pandas as pd
import yfinance as yf

UNIVERSE = ['SPY','QQQ','IWM','DIA','XLF','XLE','XLK','XLV','XLI','XLY','XLP','XLU','XLB','GLD','SLV',
            'EEM','EFA','TLT','HYG','AAPL','MSFT','JPM','XOM','KO','PG']
DL_START = '2019-01-01'   # 200d warmup before the first regime
COST_SIDE = 0.0005        # 0.05% per side → 0.10% round-trip
MAX_POS  = 8
START_EQ = 100_000.0
MAX_HOLD = 25

REGIMES = {
    '2020_crash': ('2020-01-01', '2020-12-31'),
    '2022_bear':  ('2022-01-01', '2022-12-31'),
    '2023_26_bull': ('2023-01-01', '2026-06-02'),
}
VARIANTS = {
    'mr_notrend': dict(entry_z=-1.5, exit_z=0.0, stop=-8.0, trend=False),
    'mr_trend':   dict(entry_z=-1.5, exit_z=0.0, stop=-8.0, trend=True),
}


def metrics(equity):
    """equity: pd.Series indexed by date. Returns (total%, CAGR%, maxDD%, Sharpe)."""
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
    """closes/ind: dict sym->Series (aligned to `dates`). Returns equity Series + (trades, wins)."""
    cash = START_EQ
    pos = {}   # sym -> dict(shares, entry, edate_idx)
    eq = []
    trades = 0; wins = 0
    for di, d in enumerate(dates):
        # 1) exits
        for sym in list(pos.keys()):
            px = closes[sym].iloc[di]
            if not np.isfinite(px):
                continue
            P = pos[sym]
            ret = (px - P['entry']) / P['entry'] * 100
            z = ind[sym]['z'].iloc[di]
            held = di - P['edate_idx']
            if (np.isfinite(z) and z >= p['exit_z']) or ret <= p['stop'] or held >= MAX_HOLD:
                cash += P['shares'] * px * (1 - COST_SIDE)
                trades += 1
                if ret - 2 * COST_SIDE * 100 > 0:
                    wins += 1
                del pos[sym]
        # 2) entries (fill open slots)
        if len(pos) < MAX_POS:
            eq_now = cash + sum(pos[s]['shares'] * closes[s].iloc[di] for s in pos if np.isfinite(closes[s].iloc[di]))
            for sym in closes:
                if len(pos) >= MAX_POS:
                    break
                if sym in pos:
                    continue
                px = closes[sym].iloc[di]
                z = ind[sym]['z'].iloc[di]
                if not (np.isfinite(px) and np.isfinite(z)):
                    continue
                trend_ok = (not p['trend']) or (np.isfinite(ind[sym]['sma200'].iloc[di]) and px > ind[sym]['sma200'].iloc[di])
                if z <= p['entry_z'] and trend_ok:
                    alloc = eq_now / MAX_POS
                    if alloc > cash:
                        alloc = cash
                    if alloc < 100:
                        continue
                    shares = alloc / px
                    cash -= shares * px * (1 + COST_SIDE)
                    pos[sym] = dict(shares=shares, entry=px, edate_idx=di)
        # 3) mark-to-market equity
        mtm = cash + sum(pos[s]['shares'] * closes[s].iloc[di] for s in pos if np.isfinite(closes[s].iloc[di]))
        eq.append(mtm)
    return pd.Series(eq, index=dates), trades, wins


def main():
    print('MULTI-REGIME ROBUSTNESS TEST — capital-constrained portfolio ($%.0fk, max %d positions)\n'
          % (START_EQ / 1000, MAX_POS), flush=True)
    raw = yf.download(UNIVERSE, start=DL_START, end='2026-06-02', interval='1d',
                      group_by='ticker', auto_adjust=True, progress=False, threads=True)
    # build aligned close matrix + indicators on SPY's calendar
    cal = raw['SPY']['Close'].dropna().index
    closes_full = {}; ind_full = {}
    for sym in UNIVERSE:
        try:
            c = raw[sym]['Close'].reindex(cal).ffill()
        except Exception:
            continue
        closes_full[sym] = c
        sma20 = c.rolling(20).mean(); std20 = c.rolling(20).std()
        ind_full[sym] = {'z': (c - sma20) / std20, 'sma200': c.rolling(200).mean()}

    for rname, (rs, re_) in REGIMES.items():
        mask = (cal >= rs) & (cal <= re_)
        dates = cal[mask]
        spy_w = closes_full['SPY'].loc[dates]
        st, sc, sdd, ssh = metrics(spy_w)
        print('=' * 84)
        print('REGIME %s  (%s → %s)   SPY: %+.1f%% total, %.1f%% CAGR, %.1f%% maxDD, Sharpe %.2f'
              % (rname, rs, re_, st, sc, sdd, ssh))
        print('-' * 84)
        print('  %-11s %8s %8s %8s %8s %7s %7s' % ('variant','total%','CAGR%','maxDD%','Sharpe','trades','win%'))
        for vname, p in VARIANTS.items():
            cl = {s: closes_full[s].loc[dates] for s in closes_full}
            ind = {s: {'z': ind_full[s]['z'].loc[dates], 'sma200': ind_full[s]['sma200'].loc[dates]} for s in closes_full}
            eq, trades, wins = run_portfolio(cl, ind, dates, p)
            t, cg, dd, sh = metrics(eq)
            wr = (wins / trades * 100) if trades else 0
            flag = ''
            if sh > ssh and dd > sdd:   # higher Sharpe AND shallower drawdown than SPY
                flag = '  ✓ beats SPY risk-adj'
            print('  %-11s %+8.1f %+8.1f %8.1f %8.2f %7d %6.1f%%%s' % (vname, t, cg, dd, sh, trades, wr, flag))
        print()

    print('READ: The 2022_bear row is the verdict. If mr_notrend craters there (deep DD / negative) '
          'but mr_trend stays shallow, the edge is real & the trend filter makes it all-weather. '
          'If both crater, it was bull-beta. "Beats SPY risk-adj" = higher Sharpe AND shallower drawdown.')


if __name__ == '__main__':
    main()
