#!/usr/bin/env python3
"""
Daily paper-trading journal / matrix.

The paper phase is a cost-free sandbox — this captures ONE reviewable row per trading day
so flaws and improvements are spotted by scanning a column, not re-diagnosing from scratch.
Run nightly (after close) from the refine cron. Appends to daily_matrix.jsonl and rewrites a
human-readable daily_matrix.txt (last ~20 days).

Columns map to the failure modes we've actually hit:
  • FUNNEL  — scan cycles → no-qualifying → each gate's rejects → buys (where candidates die)
  • P&L     — trades, realized, win rate, end balance, capital deployed
  • MODEL   — predictor n / base win-rate / out-of-sample AUC (calibration trajectory)
  • EXITS   — exit-outcome sample size (reversal-ladder validation progress)
  • FLAGS   — errors / stale-price / halts (silent-degradation watch)
"""
import os, json, re, subprocess
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
MATRIX_JSONL = os.path.join(_DIR, 'daily_matrix.jsonl')
MATRIX_TXT   = os.path.join(_DIR, 'daily_matrix.txt')


def _et_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')
    except Exception:
        return datetime.now().strftime('%Y-%m-%d')


def _load(path, default):
    try:
        with open(os.path.join(_DIR, path)) as f:
            return json.load(f)
    except Exception:
        return default


def _trades_today(today):
    """Per-day trades + realized P&L from the accounting ledger (entries are datetime-stamped)."""
    led = _load('accounting.json', {})
    rows = [e for e in led.get('entries', []) if str(e.get('time', '')).startswith(today)]
    sells = [e for e in rows if e.get('action') == 'SELL']
    buys  = [e for e in rows if e.get('action') == 'BUY']
    wins  = [e for e in sells if float(e.get('pnl', 0)) > 0]
    realized = round(sum(float(e.get('pnl', 0)) for e in sells), 2)
    deployed = round(sum(float(e.get('amount', 0)) for e in buys), 2)
    return {
        'buys': len(buys), 'sells': len(sells),
        'realized_pnl': realized,
        'wins': len(wins), 'losses': len(sells) - len(wins),
        'win_rate': round(len(wins) / max(len(sells), 1) * 100, 1),
        'deployed': deployed,
        'symbols': sorted(set(e.get('symbol', '') for e in buys if e.get('symbol'))),
    }


def _funnel_today():
    """Where candidates die — tallied over today's session (entries after the day-start marker)."""
    ap = _load('autopilot.json', {})
    log = ap.get('log', [])
    start = 0
    for i in range(len(log) - 1, -1, -1):
        if 'New trading day' in str(log[i].get('note', '')):
            start = i; break
    day = log[start:]
    def cnt(action, contains=None):
        n = 0
        for e in day:
            if e.get('action') == action and (contains is None or contains in str(e.get('note', ''))):
                n += 1
        return n
    return {
        'scan_cycles':  cnt('SCAN', 'Scanning'),
        'no_qualifying': cnt('SCAN', 'No qualifying'),
        'rvol_gate':    cnt('RVOL-GATE'),
        'signal_gate':  cnt('SIGNAL-GATE'),
        'score_gate':   cnt('SCORE-GATE'),
        'predict_gate': cnt('PREDICT-GATE'),
        'vwap':         cnt('VWAP'),
        'spread':       cnt('SPREAD'),
        'entry_skip':   cnt('ENTRY-SKIP'),
        'buys':         cnt('BUY'),
    }, {
        'errors':      cnt('ERROR'),
        'stale_price': cnt('STALE-PRICE'),
        'halt':        cnt('HALT'),
    }


def _model_snapshot():
    try:
        from config import load_config
        from modules.model_eval import load_rows, summarize, load_exit_rows
        cfg = load_config(); rows = load_rows()
        m = summarize(cfg, rows) if rows else {'n': 0}
        return {
            'n_trained':     m.get('n', 0),
            'base_win_rate': m.get('base_rate', 0),
            'auc':           m.get('auc_model', None),
            'exit_n':        len(load_exit_rows()),
        }
    except Exception:
        md = _load('predictor_model.json', {})
        n = md.get('n_trained', 0)
        return {'n_trained': n, 'base_win_rate': round(md.get('wins', 0) / max(n, 1), 3),
                'auc': None, 'exit_n': 0}


def whatif(date_str):
    """Deterministic sandbox of a SETTLED day through the current code: returns what the
    current strategy would have done that day, alongside the built-in competitor benchmark.
    Run only on settled (prior) days so the data is final and the result is reproducible."""
    try:
        log = os.path.join('/tmp', 'whatif_%s.log' % date_str)
        with open(log, 'w') as fh:
            # --seed-model: the nightly settled-day replay is ALSO our delay-immune training
            # collector — it persists clean 'replay'-tagged data and trains the model on it,
            # while doubling as the what-if measurement.
            subprocess.run(['python3', os.path.join(_DIR, 'replay.py'), '--date', date_str,
                            '--speed', '4000', '--seed-model'], cwd=_DIR, timeout=480, stdout=fh,
                           stderr=subprocess.STDOUT)
        t = open(log).read()
        if 'REPLAY COMPARISON REPORT' not in t:
            return None
        def two(pat):
            m = re.search(pat, t, re.M)
            return (float(m.group(1)), float(m.group(2))) if m else (None, None)
        st_trd, comp_trd = two(r'^\s*Trades\s+(\d+)\s+(\d+)')
        st_mtm, comp_mtm = two(r'Total P&L\s+\$\s*([-+]?[0-9.]+)\s+\$\s*([-+]?[0-9.]+)')
        st_real, _       = two(r'Realized P&L\s+\$\s*([-+]?[0-9.]+)\s+\$\s*([-+]?[0-9.]+)')
        st_dep, comp_dep = two(r'Capital deployed\s+\$\s*([0-9.]+)\s+\$\s*([0-9.]+)')
        return {
            'st_trades': int(st_trd or 0), 'st_realized': st_real, 'st_mtm': st_mtm, 'st_deployed': st_dep,
            'comp_trades': int(comp_trd or 0), 'comp_mtm': comp_mtm,
            'edge': round((st_mtm or 0) - (comp_mtm or 0), 2),
        }
    except Exception:
        return None


def build_row(today=None):
    today = today or _et_today()
    pnl = _trades_today(today)
    funnel, flags = _funnel_today()
    model = _model_snapshot()
    bal = round(_load('paper_trades.json', {}).get('balance', 0), 2)
    # new model samples since the prior journal row
    prev = _last_row()
    n_new = model['n_trained'] - prev.get('model', {}).get('n_trained', model['n_trained']) if prev else 0
    return {
        'date': today,
        'trades': pnl['buys'] + pnl['sells'],
        'pnl': pnl, 'funnel': funnel, 'flags': flags,
        'model': {**model, 'n_new': max(0, n_new)},
        'end_balance': bal,
        'ts': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }


def _last_row():
    if not os.path.exists(MATRIX_JSONL):
        return None
    try:
        lines = [l for l in open(MATRIX_JSONL) if l.strip()]
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


def _render():
    rows = []
    for l in open(MATRIX_JSONL):
        if l.strip():
            try: rows.append(json.loads(l))
            except Exception: pass
    rows = rows[-20:]
    out = ['StopTrading — Daily Paper-Trading Matrix (last %d days)' % len(rows), '=' * 118]
    out.append('%-11s %4s %8s %5s %6s | %5s %5s %4s %4s | mdl_n %4s | whatif(settled): ST  COMP  edge | flags' % (
        'DATE', 'trd', 'realiz', 'win%', 'deploy', 'scan', 'noq', 'gate', 'buy', 'AUC'))
    out.append('-' * 118)
    for r in rows:
        p = r.get('pnl', {}); f = r.get('funnel', {}); m = r.get('model', {}); fl = r.get('flags', {})
        gates = f.get('rvol_gate', 0) + f.get('signal_gate', 0) + f.get('score_gate', 0) + f.get('vwap', 0) + f.get('spread', 0)
        flagstr = ','.join('%s:%d' % (k, v) for k, v in fl.items() if v) or '-'
        auc = m.get('auc'); auc = ('%.2f' % auc) if isinstance(auc, (int, float)) else '-'
        w = r.get('whatif')
        if w:
            wi = '%+6.2f %+6.2f %+6.2f' % (w.get('st_mtm') or 0, w.get('comp_mtm') or 0, w.get('edge') or 0)
        else:
            wi = '   (pending settle)  '
        out.append('%-11s %4d %+8.2f %5.0f %6.0f | %5d %5d %4d %4d | %5d %4s | %s | %s' % (
            r.get('date', '?'), r.get('trades', 0), p.get('realized_pnl', 0), p.get('win_rate', 0),
            p.get('deployed', 0),
            f.get('scan_cycles', 0), f.get('no_qualifying', 0), gates, f.get('buys', 0),
            m.get('n_trained', 0), auc, wi, flagstr))
    open(MATRIX_TXT, 'w').write('\n'.join(out) + '\n')


def run(today=None):
    row = build_row(today)
    # replace any existing row for the same date (idempotent re-runs)
    existing = []
    if os.path.exists(MATRIX_JSONL):
        for l in open(MATRIX_JSONL):
            if l.strip():
                try:
                    r = json.loads(l)
                    if r.get('date') != row['date']:
                        existing.append(r)
                except Exception:
                    pass
    existing.append(row)
    # Backfill the what-if for the most recent SETTLED day that lacks one (data is final by
    # the next session). One deterministic replay per night — measures the strategy's
    # settled-day performance vs the competitor as the code evolves.
    try:
        from config import load_config
        if load_config().get('whatif_enabled', True):
            for r in reversed(existing[:-1]):           # skip today's (unsettled) row
                if 'whatif' not in r:
                    wf = whatif(r['date'])
                    if wf:
                        r['whatif'] = wf
                    break
    except Exception:
        pass
    with open(MATRIX_JSONL, 'w') as f:
        for r in existing:
            f.write(json.dumps(r) + '\n')
    _render()
    return row


if __name__ == '__main__':
    r = run()
    print('journal row written for', r['date'])
    print(open(MATRIX_TXT).read())
