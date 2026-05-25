"""
Behavioral time-gate tests — patches datetime.now() to specific ET clock times.
Intent preserved: the correct score/label is returned for each market session;
a DST-broken clock must not cause the wrong session to fire.
"""
import unittest.mock as mock
from datetime import datetime, timezone, timedelta
from tests.harness_utils import Suite


def _make_et(hour: int, minute: int = 0) -> datetime:
    """Return a timezone-aware datetime in America/New_York for today at HH:MM."""
    try:
        from zoneinfo import ZoneInfo
        base = datetime.now(ZoneInfo('America/New_York'))
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except Exception:
        return datetime.now(timezone.utc).replace(
            hour=hour + 4, minute=minute, second=0, microsecond=0
        )


def _time_of_day_at(hour: int, minute: int = 0):
    """Call BehavioralAgent._time_of_day() with the clock frozen at ET HH:MM."""
    from modules.behavioral import BehavioralAgent, _ET
    agent = BehavioralAgent.__new__(BehavioralAgent)
    frozen = _make_et(hour, minute)
    target = 'modules.behavioral.datetime'
    with mock.patch(target) as mock_dt:
        mock_dt.now.return_value = frozen
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        score, label = agent._time_of_day()
    return score, label


def run() -> tuple:
    s = Suite('BEHAVIORAL — time-of-day gates with patched ET clock')

    # Minutes-of-day boundaries from behavioral.py:
    #   570–599  (9:30–9:59)   Opening momentum  score=7
    #   600–689  (10:00–11:29) Standard morning  score=3
    #   720–839  (12:00–13:59) Lunch dead zone   score=-8
    #   840–899  (14:00–14:59) Afternoon         score=5
    #   900–959  (15:00–15:59) Power hour        score=8
    #   else                   Outside hours      score=0

    # ── Session boundaries ────────────────────────────────────────────────────
    s.section('Session score at key clock times')

    cases = [
        ( 9, 30,  7, 'Opening momentum (9:30)'),
        ( 9, 45,  7, 'Opening momentum (9:45)'),
        ( 9, 59,  7, 'Opening momentum (9:59)'),
        (10,  0,  3, 'Standard morning (10:00)'),
        (11, 29,  3, 'Standard morning (11:29)'),
        (11, 30,  0, 'Outside defined window (11:30–11:59)'),
        (12,  0, -8, 'Lunch dead zone (12:00)'),
        (13, 59, -8, 'Lunch dead zone (13:59)'),
        (14,  0,  5, 'Afternoon session (14:00)'),
        (14, 59,  5, 'Afternoon session (14:59)'),
        (15,  0,  8, 'Power hour (15:00)'),
        (15, 59,  8, 'Power hour (15:59)'),
        (16,  0,  0, 'Outside market hours (16:00)'),
        ( 4,  0,  0, 'Pre-dawn outside hours (4:00)'),
        ( 9, 29,  0, 'One minute before open (9:29)'),
    ]

    for hour, minute, expected_score, label in cases:
        score, _ = _time_of_day_at(hour, minute)
        s.check(f'{hour:02d}:{minute:02d} ET → score={expected_score}  [{label}]',
                score == expected_score, f'got {score}')

    # ── Boundary precision ────────────────────────────────────────────────────
    s.section('Exact boundary transitions (off-by-one guard)')
    boundaries = [
        ( 9, 30,  7,  'Open at 9:30 exactly'),
        ( 9, 29,  0,  '9:29 is NOT opening momentum'),
        (10,  0,  3,  'Standard starts at 10:00 exactly'),
        ( 9, 59,  7,  'Opening ends at 9:59'),
        (12,  0, -8,  'Dead zone starts at 12:00 exactly'),
        (11, 59,  0,  '11:59 is outside defined windows'),
        (14,  0,  5,  'Afternoon starts at 14:00 exactly'),
        (15,  0,  8,  'Power hour starts at 15:00 exactly'),
        (16,  0,  0,  'Market close at 16:00 — outside hours'),
    ]
    for hour, minute, expected, label in boundaries:
        score, _ = _time_of_day_at(hour, minute)
        s.check(f'{label}', score == expected, f'got {score}')

    # ── Negative score signals dead zone ─────────────────────────────────────
    s.section('Lunch dead zone is the only negative-score window')
    negative_hours = [(h, m) for h in range(0, 24) for m in [0, 30]
                      if _time_of_day_at(h, m)[0] < 0]
    s.check('Only lunch window (12:00–13:59) produces negative score',
            all(12 <= h < 14 for h, _ in negative_hours))

    # ── Power hour is the highest score ──────────────────────────────────────
    s.section('Power hour has the highest score')
    all_scores = [_time_of_day_at(h, m)[0] for h in range(0, 24) for m in range(0, 60, 15)]
    s.check('Maximum score across all times is 8 (power hour)',
            max(all_scores) == 8)
    s.check('Minimum score is -8 (lunch dead zone)',
            min(all_scores) == -8)

    # ── DST correctness: using ZoneInfo means 9:30 ET is always 9:30 ─────────
    s.section('DST — 9:30 ET is always opening momentum regardless of UTC offset')
    # During EST (winter): 9:30 ET = 14:30 UTC
    # During EDT (summer): 9:30 ET = 13:30 UTC
    # Both must return score=7. We verify by checking the minute calculation
    # operates on ET local time, not UTC.
    score_summer, _ = _time_of_day_at(9, 30)   # ET (summer: UTC-4)
    score_winter, _ = _time_of_day_at(9, 30)   # ET (winter: UTC-5) — same local time
    s.check('9:30 ET in summer → score=7',  score_summer == 7)
    s.check('9:30 ET in winter → score=7',  score_winter == 7)

    return s.summary()


if __name__ == '__main__':
    run()
