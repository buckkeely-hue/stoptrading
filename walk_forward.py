#!/usr/bin/env python3
"""
Walk-forward validation of trend-following — the anti-overfit test.

A strategy that looks good on the whole history may just be curve-fit. Walk-forward removes that
illusion: on each rolling 2-year IN-SAMPLE window we pick the best config (by Sharpe) from a grid,
then apply THAT choice — chosen using only past data — to the next 1 year OUT-OF-SAMPLE. Stitching
all OOS years gives the honest performance you'd actually have achieved selecting params blind.

If the stitched OOS curve beats / matches SPY risk-adjusted with controlled drawdown, the trend
edge is robust. If OOS collapses or the chosen config thrashes randomly, it was overfit. ~16 yrs
of liquid-name daily history (2010-2026) gives ~14 independent OOS folds.
"""
import numpy as np
import pandas as pd
import yfinance as yf

UNIVERSE = ['SPY','QQQ','IWM','DIA','XLF','XLE','XLK','XLV','XLI','XLY','XLP','XLU','XLB','GLD','SLV',
            'EEM','EFA','TLT','HYG','AAPL','MSFT','JPM','XOM','KO','PG']
DL_START = '2009-06-01'
COST_SIDE = 0.0005
MAX_POS  = 8
START_EQ = 100_000.0
IS_YEARS, OOS_YEARS = 2, 1
FIRST_OOS, LAST_OOS = 2012, 2026

GRID = [
    {'type':'breakout','hi':20,'lo':10}, {'type':'breakout','hi':50,'lo':20}, {'type':'breakout','hi':100,'lo':50},
    {'type':'cross','fast':20,'slow':100}, {'type':'cross','fast':50,'slow':200}, {'type':'cross','fast':50,'slow':100},
    {'type':'trail','fast':50,'slow':200,'trail':0.08}, {'type':'trail','fast':50,'slow':200,'trail':0.12},
    {'type':'trail','fast':50,'slow':200,'trail':0.15},
]


def metrics(e):
    e = e.dropna()
    if len(e) < 5: return (0,0,0,0)
    total = (e.iloc[-1]/e.iloc[0]-1)*100
    yrs = len(e)/252.0
    cagr = ((e.iloc[-1]/e.iloc[0])**(1/yrs)-1)*100 if e.iloc[0]>0 else 0
    dd = ((e/e.cummax())-1).min()*100
    dr = e.pct_change().dropna()
    sh = (dr.mean()/dr.std()*np.sqrt(252)) if dr.std()>0 else 0
    return (total, cagr, dd, sh)


def run(closes, ind, dates, cfg):
    cash=START_EQ; pos={}; eq=[]
    def ent(sym,di):
        px=closes[sym].iloc[di]; I=ind[sym]
        if not np.isfinite(px): return False
        if cfg['type']=='breakout':
            hh=I['hi%d'%cfg['hi']].iloc[di]; s200=I['sma200'].iloc[di]
            return np.isfinite(hh) and np.isfinite(s200) and px>=hh and px>s200
        sf=I['sma%d'%cfg['fast']].iloc[di]; ss=I['sma%d'%cfg['slow']].iloc[di]
        return np.isfinite(sf) and np.isfinite(ss) and px>sf and sf>ss
    def ex(sym,di,P):
        px=closes[sym].iloc[di]; I=ind[sym]
        if not np.isfinite(px): return False
        if cfg['type']=='breakout':
            ll=I['lo%d'%cfg['lo']].iloc[di]; return np.isfinite(ll) and px<=ll
        if cfg['type']=='trail':
            ss=I['sma%d'%cfg['slow']].iloc[di]
            return px<=P['peak']*(1-cfg['trail']) or (np.isfinite(ss) and px<ss)
        sf=I['sma%d'%cfg['fast']].iloc[di]; return np.isfinite(sf) and px<sf
    for di in range(len(dates)):
        for sym in list(pos):
            px=closes[sym].iloc[di]
            if not np.isfinite(px): continue
            P=pos[sym]; P['peak']=max(P['peak'],px)
            if ex(sym,di,P):
                cash+=P['shares']*px*(1-COST_SIDE); del pos[sym]
        if len(pos)<MAX_POS:
            eqn=cash+sum(pos[s]['shares']*closes[s].iloc[di] for s in pos if np.isfinite(closes[s].iloc[di]))
            for sym in closes:
                if len(pos)>=MAX_POS: break
                if sym in pos: continue
                if ent(sym,di):
                    px=closes[sym].iloc[di]; alloc=min(eqn/MAX_POS,cash)
                    if alloc<100: continue
                    sh=alloc/px; cash-=sh*px*(1+COST_SIDE); pos[sym]=dict(shares=sh,entry=px,peak=px)
        eq.append(cash+sum(pos[s]['shares']*closes[s].iloc[di] for s in pos if np.isfinite(closes[s].iloc[di])))
    return pd.Series(eq,index=dates)


def main():
    print('WALK-FORWARD VALIDATION — trend-following, %d-yr IS → %d-yr OOS, %d-config grid\n'
          % (IS_YEARS, OOS_YEARS, len(GRID)), flush=True)
    raw = yf.download(UNIVERSE, start=DL_START, end='2026-06-02', interval='1d',
                      group_by='ticker', auto_adjust=True, progress=False, threads=True)
    cal = raw['SPY']['Close'].dropna().index
    closes={}; ind={}
    for sym in UNIVERSE:
        try: c=raw[sym]['Close'].reindex(cal).ffill()
        except Exception: continue
        closes[sym]=c
        I={}
        for w in (20,50,100,200): I['sma%d'%w]=c.rolling(w).mean()
        for w in (20,50,100): I['hi%d'%w]=c.rolling(w).max().shift(1)
        for w in (10,20,50): I['lo%d'%w]=c.rolling(w).min().shift(1)
        ind[sym]=I

    oos_segments=[]; rows=[]
    for oos_yr in range(FIRST_OOS, LAST_OOS+1):
        is_s='%d-01-01'%(oos_yr-IS_YEARS); is_e='%d-01-01'%oos_yr
        oos_s=is_e; oos_e='%d-01-01'%(oos_yr+OOS_YEARS)
        is_d=cal[(cal>=is_s)&(cal<is_e)]; oos_d=cal[(cal>=oos_s)&(cal<oos_e)]
        if len(is_d)<200 or len(oos_d)<20: continue
        # select best config on IS by Sharpe
        best=None
        for cfg in GRID:
            cl={s:closes[s].loc[is_d] for s in closes}; ic={s:{k:ind[s][k].loc[is_d] for k in ind[s]} for s in closes}
            _,_,_,sh=metrics(run(cl,ic,is_d,cfg))
            if best is None or sh>best[1]: best=(cfg,sh)
        cfg=best[0]
        # apply blind to OOS
        cl={s:closes[s].loc[oos_d] for s in closes}; ic={s:{k:ind[s][k].loc[oos_d] for k in ind[s]} for s in closes}
        eq=run(cl,ic,oos_d,cfg)
        t,cg,dd,sh=metrics(eq)
        spy=closes['SPY'].loc[oos_d]; st,_,sdd,ssh=metrics(spy)
        tag='%s' % cfg['type'] + (('-%d/%d'%(cfg.get('hi',cfg.get('fast')),cfg.get('lo',cfg.get('slow')))) )
        rows.append((oos_yr,tag,t,dd,sh,st,sdd,ssh))
        # stitch OOS returns
        oos_segments.append(eq.pct_change().dropna())

    print('%-6s %-16s %9s %8s %8s | %9s %8s %8s' % ('OOS','chosen(IS-best)','ret%','maxDD%','Sharpe','SPY ret%','SPY DD%','SPY Shp'))
    print('-'*86)
    beat=0
    for (yr,tag,t,dd,sh,st,sdd,ssh) in rows:
        b = (sh>ssh and dd>sdd)
        beat += 1 if b else 0
        print('%-6d %-16s %+9.1f %8.1f %8.2f | %+9.1f %8.1f %8.2f%s' % (yr,tag,t,dd,sh,st,sdd,ssh,'  ✓' if b else ''))

    # stitched OOS equity curve
    allr = pd.concat(oos_segments)
    eq = (1+allr).cumprod()*START_EQ
    t,cg,dd,sh = metrics(eq)
    spy_oos = closes['SPY'].loc[cal[(cal>='%d-01-01'%FIRST_OOS)&(cal<'%d-01-01'%(LAST_OOS+1))]]
    st,scg,sdd,ssh = metrics(spy_oos)
    print('\n' + '='*86)
    print('STITCHED OUT-OF-SAMPLE (%d-%d, params always chosen blind from prior 2yr):' % (FIRST_OOS, LAST_OOS))
    print('  STRATEGY : %+.0f%% total | %.1f%% CAGR | %.1f%% maxDD | Sharpe %.2f' % (t,cg,dd,sh))
    print('  SPY hold : %+.0f%% total | %.1f%% CAGR | %.1f%% maxDD | Sharpe %.2f' % (st,scg,sdd,ssh))
    print('  beat SPY risk-adjusted in %d of %d OOS years' % (beat, len(rows)))
    print('\nVERDICT: robust if OOS Sharpe is competitive with SPY AND maxDD is clearly shallower')
    print('(the trend-following promise: similar long-run return, far less pain). Overfit if OOS')
    print('Sharpe collapses vs the in-sample numbers or the chosen config thrashes year to year.')


if __name__ == '__main__':
    main()
