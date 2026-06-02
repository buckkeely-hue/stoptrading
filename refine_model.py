#!/usr/bin/env python3
"""
Automated predictive-model refinement (closed-loop, guarded).

Runs after market close. Re-validates the model OUT-OF-SAMPLE on all accumulated barrier
outcomes, auto-tunes blend_k, and promotes/demotes the model's operating authority based
on validated performance:

    shadow  →  conviction-sizing  →  gating

Safety design:
  • Bounded parameters (blend_k ∈ [40,120]); change only on a clear margin (hysteresis).
  • Promotion needs sample-size + discrimination + calibration thresholds, ALL met.
  • Demotion uses a lower threshold band (hysteresis) so it can't flip-flop.
  • Gating (which blocks trades) has a much higher bar than sizing.
  • Every run logs the metric trajectory to refinement_log.jsonl and notifies.
  • Intended to run post-close so any change takes effect the NEXT session, with a heads-up
    notification the operator can veto overnight.

Usage:
    python3 refine_model.py            # apply changes + log + notify
    python3 refine_model.py --dry-run  # report only, change nothing
"""
import sys, os, json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config, save_config
from modules.model_eval import (load_rows, summarize, tune_blend_k,
                                load_exit_rows, summarize_exits, tune_exit_thresh)

REFINE_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'refinement_log.jsonl')

# ── Promotion / demotion thresholds (hysteresis bands) ──────────────────────────
MIN_N_SIZING   = 120     # samples before conviction-sizing is even considered
MIN_N_GATING   = 300     # gating blocks trades → much higher bar
AUC_PROMOTE    = 0.58    # discrimination needed to promote sizing
AUC_DEMOTE     = 0.52    # below this → demote sizing
AUC_GATE_PROMOTE = 0.62
AUC_GATE_DEMOTE  = 0.55
CAL_PROMOTE_MULT = 1.05  # model Brier must be ≤ base-rate Brier × this to promote
CAL_DEMOTE_MULT  = 1.20
BLEND_K_MARGIN = 0.02    # only retune blend_k if AUC improves by this much
BLEND_K_RANGE  = (40, 120)
# Exit-side tuning
MIN_N_EXIT        = 40      # exit-outcome samples before tuning the exit threshold
LADDER_CORR_TRUST = -0.10   # reversal-score↔forward-return corr must be ≤ this (predictive)
LADDER_CORR_BAD   = 0.15    # ≥ this ⇒ ladder anti-predictive → disable predictive exit
EXIT_THRESH_RANGE = (0.40, 0.75)
EXIT_THRESH_MARGIN = 0.05


def main():
    dry = '--dry-run' in sys.argv
    cfg = load_config()
    rows = load_rows()
    n = len(rows)
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    actions = []

    if n < 15:
        _finish(cfg, {'n': n}, ['insufficient data (n=%d) — need ≥15 barrier outcomes' % n],
                dry, now, None)
        return

    # 1) Tune blend_k (bounded, hysteresis vs current)
    cur_k = float(cfg.get('predictor_blend_k', 60.0))
    cands = tuple(k for k in (40, 60, 90, 120) if BLEND_K_RANGE[0] <= k <= BLEND_K_RANGE[1])
    best_k, table = tune_blend_k(cfg, rows, cands)
    cur_auc  = next((t['auc'] for t in table if t['blend_k'] == cur_k), None)
    best_auc = next(t['auc'] for t in table if t['blend_k'] == best_k)
    new_k = cur_k
    if best_k != cur_k and (cur_auc is None or (best_auc - cur_auc) >= BLEND_K_MARGIN):
        new_k = float(best_k)
        actions.append('blend_k %.0f→%.0f (OOS AUC %.3f→%.3f)' % (cur_k, new_k, cur_auc or 0, best_auc))

    # 2) Metrics at the chosen blend_k
    cfg_eval = dict(cfg); cfg_eval['predictor_blend_k'] = new_k
    m = summarize(cfg_eval, rows)
    auc_m = m['auc_model']; brier_m = m['brier']; base_brier = m['base_brier']
    sizing = bool(cfg.get('predictor_sizing', False))
    gating = bool(cfg.get('predictor_gate', False))

    # 3) Conviction-sizing promote/demote
    if not sizing:
        if (n >= MIN_N_SIZING and auc_m >= AUC_PROMOTE and brier_m <= base_brier * CAL_PROMOTE_MULT):
            sizing = True
            actions.append('PROMOTE → conviction-sizing ON (n=%d AUC=%.3f Brier=%.3f≤%.3f)'
                           % (n, auc_m, brier_m, base_brier * CAL_PROMOTE_MULT))
    else:
        if auc_m < AUC_DEMOTE or brier_m > base_brier * CAL_DEMOTE_MULT:
            sizing = False; gating = False
            actions.append('DEMOTE → back to shadow (AUC=%.3f or Brier=%.3f degraded)' % (auc_m, brier_m))

    # 4) Gating promote/demote (only on top of active sizing; higher bar)
    if sizing and not gating:
        if n >= MIN_N_GATING and auc_m >= AUC_GATE_PROMOTE:
            gating = True
            actions.append('PROMOTE → entry gate ON (n=%d AUC=%.3f)' % (n, auc_m))
    elif gating and auc_m < AUC_GATE_DEMOTE:
        gating = False
        actions.append('DEMOTE → entry gate OFF (AUC=%.3f)' % auc_m)

    # 4b) EXIT-side refinement — validate the reversal ladder and tune the exit threshold
    exit_rows = load_exit_rows()
    em = summarize_exits(exit_rows)
    cur_xt  = float(cfg.get('predictor_exit_thresh', 0.55))
    new_xt  = cur_xt
    exit_on = bool(cfg.get('predictor_exit', True))
    if em.get('n', 0) >= MIN_N_EXIT:
        corr = em.get('ladder_corr', 0.0)
        if exit_on and corr >= LADDER_CORR_BAD:
            exit_on = False
            actions.append('DEMOTE → predictive exit OFF (ladder anti-predictive corr=%+.2f)' % corr)
        elif corr <= LADDER_CORR_TRUST:
            best = tune_exit_thresh(exit_rows)
            if best and EXIT_THRESH_RANGE[0] <= best['thresh'] <= EXIT_THRESH_RANGE[1] \
                    and abs(best['thresh'] - cur_xt) >= EXIT_THRESH_MARGIN:
                new_xt = float(best['thresh'])
                actions.append('exit_thresh %.2f→%.2f (would-fire exits avg fwd %+.2f%%, n=%d)'
                               % (cur_xt, new_xt, best['mean_fwd'], best['support']))

    # 5) Apply (atomic) unless dry-run
    changes = {}
    if new_k != cur_k:                                   changes['predictor_blend_k'] = new_k
    if sizing != bool(cfg.get('predictor_sizing', False)): changes['predictor_sizing'] = sizing
    if gating != bool(cfg.get('predictor_gate', False)):   changes['predictor_gate'] = gating
    if new_xt != cur_xt:                                 changes['predictor_exit_thresh'] = new_xt
    if exit_on != bool(cfg.get('predictor_exit', True)):   changes['predictor_exit'] = exit_on
    if changes and not dry:
        save_config(changes)
        _restart_server(actions)        # reload config (+ persisted model) into the live process
    if not actions:
        actions.append('no change — metrics within band (shadow held)' if not sizing
                       else 'no change — sizing held')

    _finish(cfg_eval, m, actions, dry, now,
            {'sizing': sizing, 'gating': gating, 'blend_k': new_k, 'exit_on': exit_on, 'exit_thresh': new_xt},
            table=table, em=em)


def _restart_server(actions):
    """Bounce the launchd-managed server so it reloads config (+ the day's persisted model).
    KeepAlive restarts it; resume() preserves paper state. Safe because this runs post-close."""
    import subprocess
    try:
        subprocess.run(['launchctl', 'stop', 'com.stoptrading'], timeout=15, check=False)
        actions.append('restarted server to load changes')
    except Exception as e:
        actions.append('config saved but server restart failed (%s) — applies next restart' % e)


def _finish(cfg, m, actions, dry, now, state, table=None, em=None):
    line = {'time': now, 'dry_run': dry, 'metrics': m, 'exit_metrics': em, 'state': state, 'actions': actions}
    try:
        with open(REFINE_LOG, 'a') as fh:
            fh.write(json.dumps(line) + '\n')
    except Exception:
        pass

    # Build a concise human report / notification
    parts = ['Model refinement %s — %s' % (('(dry-run)' if dry else ''), now)]
    if m.get('n', 0):
        parts.append('n=%d base=%.0f%% | AUC model %.3f vs heuristic %.3f | Brier %.3f (base %.3f)' % (
            m['n'], 100 * m.get('base_rate', 0), m.get('auc_model', 0),
            m.get('auc_heuristic', 0), m.get('brier', 0), m.get('base_brier', 0)))
        if state:
            parts.append('mode: %s%s | blend_k=%.0f' % (
                'SIZING' if state['sizing'] else 'shadow',
                ' + GATE' if state['gating'] else '', state['blend_k']))
    if em and em.get('n', 0):
        parts.append('exit ladder: n=%d corr=%+.2f good=%.0f%% mean_fwd=%+.2f%% | thresh=%.2f %s' % (
            em['n'], em.get('ladder_corr', 0), 100 * em.get('good_rate', 0), em.get('mean_fwd', 0),
            (state or {}).get('exit_thresh', 0), 'ON' if (state or {}).get('exit_on', True) else 'OFF'))
    elif em is not None:
        parts.append('exit ladder: collecting data (n=%d, need %d)' % (em.get('n', 0), MIN_N_EXIT))
    parts += ['• ' + a for a in actions]
    report = '\n'.join(parts)
    print(report)

    try:
        from modules.notify import Notifier
        Notifier(load_config()).send(report, 'info')
    except Exception:
        pass

    # Append today's row to the daily paper-trading journal/matrix (best-effort).
    try:
        import daily_journal
        daily_journal.run()
    except Exception:
        pass


if __name__ == '__main__':
    main()
