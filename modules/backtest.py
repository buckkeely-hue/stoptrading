"""
Backtester — simulates the harvest/autopilot strategy against historical daily OHLCV data.

Strategy modelled:
  - Each day, scan universe for momentum candidates (positive 5-day slope + RVOL > 1.2)
  - Buy top candidates up to daily_limit, per_trade size
  - Harvest 50% of position at harvest_trigger_pct net gain
  - Exit remaining shares when margin compresses to <= exit_trigger_pct after >= 1 harvest
  - Stop loss at stop_loss_pct drawdown from entry

Returns performance report including win rate, Sharpe, max drawdown, equity curve.
"""

import yfinance as yf
import numpy as np
from datetime import datetime, timedelta


def run_backtest(
    universe,
    lookback_days=126,
    harvest_trigger_pct=7.0,
    exit_trigger_pct=5.0,
    stop_loss_pct=15.0,
    per_trade=50.0,
    daily_limit=100.0,
    starting_balance=500.0,
    tx_cost_pct=3.0,
    progress_cb=None,
):
    """
    Simulate the penny-stock harvest strategy over historical data.
    Returns a dict of performance metrics.
    """
    cap   = min(len(universe), 80)    # yfinance cap to avoid rate limits
    syms  = universe[:cap]
    end   = datetime.now()
    start = end - timedelta(days=int(lookback_days * 1.6))

    if progress_cb:
        progress_cb('Downloading {} symbols × {}d history…'.format(cap, lookback_days))

    try:
        raw = yf.download(
            tickers=' '.join(syms),
            start=start.strftime('%Y-%m-%d'),
            end=end.strftime('%Y-%m-%d'),
            interval='1d',
            auto_adjust=True,
            progress=False,
            group_by='ticker',
            threads=True,
            timeout=60,
        )
    except Exception as exc:
        return {'error': str(exc)}

    if raw is None or raw.empty:
        return {'error': 'No historical data returned'}

    # Build per-symbol series
    close_s, vol_s = {}, {}
    for sym in syms:
        try:
            if len(syms) == 1:
                close_s[sym] = raw['Close'].dropna()
                vol_s[sym]   = raw['Volume'].dropna()
            else:
                if sym not in raw.columns.get_level_values(0):
                    continue
                close_s[sym] = raw[sym]['Close'].dropna()
                vol_s[sym]   = raw[sym]['Volume'].dropna()
        except Exception:
            continue

    # Collect all trading dates in lookback window
    all_dates = sorted({d for s in close_s.values() for d in s.index})
    all_dates = all_dates[-lookback_days:]

    balance      = starting_balance
    positions    = {}   # sym -> {entry_price, original_entry, shares, harvests}
    daily_spent  = 0.0
    prev_date    = None
    equity_curve = []
    trades       = []
    wins = losses = 0
    total_win_pct = total_loss_pct = 0.0

    for date in all_dates:
        # New calendar day — reset daily limit
        if prev_date is None or (date - prev_date).days >= 1:
            daily_spent = 0.0
        prev_date = date

        # ── Mark-to-market equity ─────────────────────────────────────────────
        mtm = balance
        for sym, pos in positions.items():
            try:
                p = float(close_s[sym].get(date, pos['entry_price']))
                mtm += pos['shares'] * p
            except Exception:
                pass
        equity_curve.append({'date': str(date.date()), 'equity': round(mtm, 2)})

        # ── Monitor open positions ─────────────────────────────────────────────
        for sym in list(positions.keys()):
            pos = positions[sym]
            try:
                price  = float(close_s[sym].get(date, 0))
                if price <= 0:
                    continue
                entry    = pos['entry_price']
                shares   = pos['shares']
                gain_pct = (price - entry) / entry * 100

                if gain_pct >= harvest_trigger_pct:
                    h_sh = max(1, shares // 2)
                    balance += h_sh * price * (1 - tx_cost_pct / 100)   # sell-side tx cost
                    pos['shares'] -= h_sh
                    pos['harvests'] += 1
                    pos['entry_price'] = price
                    trades.append({'type': 'HARVEST', 'sym': sym,
                                   'gain_pct': round(gain_pct, 2),
                                   'date': str(date.date())})

                elif pos['harvests'] > 0 and gain_pct <= exit_trigger_pct:
                    balance += pos['shares'] * price * (1 - tx_cost_pct / 100)
                    pnl = (price - pos['original_entry']) / pos['original_entry'] * 100
                    trades.append({'type': 'EXIT', 'sym': sym,
                                   'gain_pct': round(pnl, 2),
                                   'date': str(date.date())})
                    if pnl >= 0:
                        wins += 1; total_win_pct += pnl
                    else:
                        losses += 1; total_loss_pct += abs(pnl)
                    del positions[sym]

                elif gain_pct <= -stop_loss_pct:
                    balance += pos['shares'] * price * (1 - tx_cost_pct / 100)
                    trades.append({'type': 'STOP', 'sym': sym,
                                   'gain_pct': round(gain_pct, 2),
                                   'date': str(date.date())})
                    losses += 1; total_loss_pct += abs(gain_pct)
                    del positions[sym]

            except Exception:
                continue

        # ── Look for new buys ──────────────────────────────────────────────────
        if daily_spent >= daily_limit or balance < per_trade:
            continue

        candidates = []
        for sym, series in close_s.items():
            if sym in positions:
                continue
            try:
                idx = list(series.index)
                i   = idx.index(date)
                if i < 6:
                    continue
                price = float(series.iloc[i])
                if not (0.10 <= price <= 8.0):
                    continue
                prev5 = series.iloc[i - 5:i]
                momentum = (price - float(prev5.iloc[0])) / float(prev5.iloc[0]) * 100
                if momentum <= 0:
                    continue
                avg_vol = float(vol_s[sym].iloc[i - 5:i].mean()) if sym in vol_s else 1
                cur_vol = float(vol_s[sym].iloc[i])               if sym in vol_s else 0
                rvol = cur_vol / avg_vol if avg_vol > 0 else 1.0
                if rvol < 1.2:
                    continue
                candidates.append((sym, price, momentum * rvol))
            except Exception:
                continue

        candidates.sort(key=lambda x: x[2], reverse=True)
        for sym, price, _ in candidates[:3]:
            avail = min(per_trade, daily_limit - daily_spent, balance)
            if avail < 5:
                break
            shares = max(1, int(avail / price))
            cost   = shares * price * (1 + tx_cost_pct / 100)   # buy-side tx cost
            balance     -= cost
            daily_spent += cost
            positions[sym] = {
                'entry_price':    price,
                'original_entry': price,
                'shares':         shares,
                'harvests':       0,
            }

    # Close remaining positions at last available price
    for sym, pos in list(positions.items()):
        try:
            price = float(close_s[sym].iloc[-1])
            balance += pos['shares'] * price * (1 - tx_cost_pct / 100)
            pnl = (price - pos['original_entry']) / pos['original_entry'] * 100
            if pnl >= 0:
                wins += 1; total_win_pct += pnl
            else:
                losses += 1; total_loss_pct += abs(pnl)
        except Exception:
            pass

    # ── Performance Metrics ────────────────────────────────────────────────────
    total_closed = wins + losses
    win_rate  = wins / total_closed if total_closed > 0 else 0
    avg_win   = total_win_pct / wins   if wins   > 0 else 0
    avg_loss  = total_loss_pct / losses if losses > 0 else 0

    final_equity = equity_curve[-1]['equity'] if equity_curve else balance
    total_return = (final_equity - starting_balance) / starting_balance * 100

    # Sharpe ratio (annualised, daily returns)
    sharpe = max_dd = 0.0
    if len(equity_curve) > 1:
        equities = [e['equity'] for e in equity_curve]
        rets = np.diff(equities) / np.array(equities[:-1])
        rets = rets[~np.isnan(rets)]
        if len(rets) > 1 and np.std(rets) > 0:
            sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(252))
        peak = starting_balance
        for eq in equities:
            peak = max(peak, eq)
            dd = (peak - eq) / peak * 100
            max_dd = max(max_dd, dd)

    return {
        'starting_balance':    starting_balance,
        'final_equity':        round(final_equity, 2),
        'total_return_pct':    round(total_return, 2),
        'win_rate':            round(win_rate * 100, 1),
        'avg_win_pct':         round(avg_win, 2),
        'avg_loss_pct':        round(avg_loss, 2),
        'wins':                wins,
        'losses':              losses,
        'total_trades':        len(trades),
        'sharpe':              round(sharpe, 3),
        'max_drawdown_pct':    round(max_dd, 2),
        'equity_curve':        equity_curve[-60:],
        'recent_trades':       trades[-20:],
        'universe_size':       len(close_s),
        'lookback_days':       lookback_days,
        'harvest_trigger_pct': harvest_trigger_pct,
        'tx_cost_pct':         tx_cost_pct,
        'survivorship_bias_warning': (
            'CAUTION: Backtest only includes symbols still trading today. '
            'Delisted penny stocks are excluded, inflating historical win rates by an estimated 10-20%.'
        ),
    }
