#!/usr/bin/env python3
"""
Multi-day backtest sweep — the delay-IMMUNE effectiveness test.

Runs the replay harness across many settled trading days × a few strategy variants, on real
historical 1-minute bars (reconstructing real-time minute-by-minute, no live feed needed), and
reports which configuration — if any — clears breakeven and/or beats the built-in Holly-style
competitor. This is the single most informative thing we can do for free: it answers "does this
approach have an edge?" before spending a cent on a real-time feed.

Each replay runs EPHEMERAL and side-effect-free (decision_log / whatif / trade_record disabled,
no --seed-model) via a temp config pointed at by STOPTRADING_CONFIG — the live config.json and
research data files are never touched.
"""
import os, sys, json, re, subprocess, tempfile
from datetime import date, timedelta

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
from config import load_config

SPEED = '800'
N_DAYS = 16            # trading days to attempt (yfinance keeps ~30 calendar days of 1m bars)
PER_RUN_TIMEOUT = 240  # seconds

# Variants test the strategic questions, not a giant grid:
#   harvest  — current config (baseline: harvest the predictable middle)
#   momentum — harvest_regime off (hunt explosive movers — does trading the rip win here?)
#   let_run  — harvest but wider target + ratchet (ride the fat tail the bimodal data showed)
COMMON = {'decision_log_enabled': False, 'whatif_enabled': False, 'trade_record_enabled': False}
VARIANTS = {
    'harvest':  {**COMMON},
    'momentum': {**COMMON, 'harvest_regime': False},
    'let_run':  {**COMMON, 'harvest_regime': True, 'harvest_trigger_pct': 7.0, 'max_single_loss_pct': 3.0},
}


def trading_days(n):
    d = date(2026, 6, 2)   # most recent settled day (today 6/3 is unsettled)
    out = []
    while len(out) < n:
        if d.weekday() < 5 and not (d.month == 5 and d.day == 25):  # skip weekends + Memorial Day
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return list(reversed(out))


def _two(pat, text):
    m = re.search(pat, text, re.M)
    return (m.group(1), m.group(2)) if m else (None, None)


def parse(text):
    if 'REPLAY COMPARISON REPORT' not in text:
        return None
    def f(x):
        try: return float(x)
        except Exception: return None
    st_t, co_t = _two(r'^\s*Trades\s+(\d+)\s+(\d+)', text)
    st_w, co_w = _two(r'^\s*Wins\s+(\d+)\s+(\d+)', text)
    st_l, co_l = _two(r'^\s*Losses\s+(\d+)\s+(\d+)', text)
    st_pnl, co_pnl = _two(r'Total P&L\s+\$\s*([-+0-9.]+)\s+\$\s*([-+0-9.]+)', text)
    st_dep, co_dep = _two(r'Capital deployed\s+\$\s*([0-9.]+)\s+\$\s*([0-9.]+)', text)
    return {
        'st_trades': int(st_t or 0), 'co_trades': int(co_t or 0),
        'st_wins': int(st_w or 0), 'st_losses': int(st_l or 0),
        'st_pnl': f(st_pnl), 'co_pnl': f(co_pnl),
        'st_dep': f(st_dep) or 0.0,
    }


def run_one(cfg_path, day):
    env = dict(os.environ); env['STOPTRADING_CONFIG'] = cfg_path
    try:
        r = subprocess.run(['python3', os.path.join(_DIR, 'replay.py'), '--date', day, '--speed', SPEED],
                           cwd=_DIR, env=env, capture_output=True, text=True, timeout=PER_RUN_TIMEOUT)
        return parse(r.stdout)
    except Exception:
        return None


def main():
    base = load_config()
    days = trading_days(N_DAYS)
    print('SWEEP: %d days x %d variants (speed %sx)\n' % (len(days), len(VARIANTS), SPEED), flush=True)
    results = {}
    for vname, override in VARIANTS.items():
        cfg = dict(base); cfg.update(override)
        fd, cfg_path = tempfile.mkstemp(prefix='sweep_%s_' % vname, suffix='.json', dir='/tmp')
        with os.fdopen(fd, 'w') as fh:
            json.dump(cfg, fh)
        rows = []
        for day in days:
            res = run_one(cfg_path, day)
            if res is None:
                print('  %-9s %s  — no data/skip' % (vname, day), flush=True); continue
            rows.append((day, res))
            print('  %-9s %s  ST: %d trd  $%+.2f  (%dW/%dL)  | COMP $%+.2f' % (
                vname, day, res['st_trades'], res['st_pnl'] or 0, res['st_wins'], res['st_losses'], res['co_pnl'] or 0), flush=True)
        os.unlink(cfg_path)
        results[vname] = rows

    print('\n' + '=' * 72)
    print('SWEEP SUMMARY  (ST = StopTrading variant, COMP = Holly-style benchmark)')
    print('=' * 72)
    print('%-9s %5s %6s %7s %8s %7s %8s %7s' % ('variant','days','trades','wins','losses','win%','net P&L','vs COMP'))
    print('-' * 72)
    for vname, rows in results.items():
        d = [r for _, r in rows if r['st_trades'] > 0 or True]
        ndays = len(rows)
        trades = sum(r['st_trades'] for _, r in rows)
        wins = sum(r['st_wins'] for _, r in rows); losses = sum(r['st_losses'] for _, r in rows)
        netp = sum((r['st_pnl'] or 0) for _, r in rows)
        comp = sum((r['co_pnl'] or 0) for _, r in rows)
        beat = sum(1 for _, r in rows if (r['st_pnl'] or 0) > (r['co_pnl'] or 0))
        wr = (wins / (wins + losses) * 100) if (wins + losses) else 0.0
        print('%-9s %5d %6d %7d %8d %6.0f%% %+8.2f %+8.2f' % (vname, ndays, trades, wins, losses, wr, netp, netp - comp))
        print('           beat benchmark on %d/%d days' % (beat, ndays))
    print('\nNote: breakeven win rate ~54%% at 0.76%% cost. Net P&L is on $500/day paper. '
          'Positive net AND win% above breakeven = a real edge worth taking to real-time.')


if __name__ == '__main__':
    main()
