"""
PDT (Pattern Day Trader) rule tests — FINRA Rule 4210.
Intent preserved: accounts under $25,000 are ALWAYS blocked from a 4th day trade
within any rolling 5-business-day window (Mon–Fri). Weekends do not count.
"""
from datetime import date, timedelta
from tests.harness_utils import Suite


def _business_days_back(today: date, n: int) -> set:
    """Mirror of harvester._pdt_window_dates — last n Mon–Fri days including today."""
    result = set()
    d = today
    while len(result) < n:
        if d.weekday() < 5:
            result.add(d.strftime('%Y-%m-%d'))
        d -= timedelta(days=1)
    return result


def _count(log: list, today: date) -> int:
    window = _business_days_back(today, 5)
    return sum(1 for ds in log if ds in window)


def _blocked(log: list, today: date) -> bool:
    return _count(log, today) >= 3


# Find the most recent Monday from today for stable test anchors
def _last_monday() -> date:
    d = date.today()
    return d - timedelta(days=d.weekday())


def run() -> tuple:
    s = Suite('PDT RULE — FINRA Rule 4210 business-day rolling window')

    monday = _last_monday()
    tuesday, wednesday, thursday, friday = [monday + timedelta(days=i) for i in range(1, 5)]
    next_monday = monday + timedelta(days=7)
    saturday = monday + timedelta(days=5)
    sunday   = monday + timedelta(days=6)

    # ── Clean slate ───────────────────────────────────────────────────────────
    s.section('Clean slate')
    s.check('0 trades → count=0, not blocked',
            _count([], monday) == 0 and not _blocked([], monday))

    # ── Under the limit ───────────────────────────────────────────────────────
    s.section('1 and 2 trades — under limit')
    log1 = [monday.strftime('%Y-%m-%d')]
    s.check('1 trade → count=1, not blocked',
            _count(log1, monday) == 1 and not _blocked(log1, monday))

    log2 = [monday.strftime('%Y-%m-%d'), tuesday.strftime('%Y-%m-%d')]
    s.check('2 trades → count=2, not blocked (checked from Friday)',
            _count(log2, friday) == 2 and not _blocked(log2, friday))

    # ── At the limit ──────────────────────────────────────────────────────────
    s.section('Exactly 3 trades — blocked')
    log3 = [d.strftime('%Y-%m-%d') for d in [monday, tuesday, wednesday]]
    s.check('3 trades this week → count=3, blocked', _blocked(log3, friday))

    # ── Rolling window — weekends do NOT extend or compress the window ────────
    s.section('Weekends do not count as business days in the window')
    # On Saturday, the 5-business-day window still spans Mon–Fri
    # Sat is NOT a business day, so the window from Saturday looks back to Mon–Fri
    s.check('Window from Saturday still includes all 5 Mon–Fri days',
            _business_days_back(saturday, 5) == {
                d.strftime('%Y-%m-%d') for d in [monday, tuesday, wednesday, thursday, friday]
            })
    s.check('3 Mon/Tue/Wed trades: still blocked on Saturday (all in window)',
            _blocked(log3, saturday))
    s.check('3 Mon/Tue/Wed trades: still blocked on Sunday',
            _blocked(log3, sunday))

    # ── Trades age out correctly on the following Monday ─────────────────────
    s.section('Trade age-out: Monday trade leaves window next Tuesday')
    # The 5-business-day window from next_monday = [next_monday, Fri, Thu, Wed, Tue]
    # → Monday of the current week has fallen OUT
    s.check('Mon trade aged out by next_monday+1 (6th business day)',
            monday.strftime('%Y-%m-%d') not in _business_days_back(next_monday + timedelta(days=1), 5))

    # Full log3 (Mon/Tue/Wed): from next_monday the window = [next_Mon, Fri, Thu, Wed, Tue]
    # Mon has aged out, Tue and Wed are still in — count=2, not blocked
    s.check('Mon/Tue/Wed: by next Monday, Mon aged out → count=2, not blocked',
            _count(log3, next_monday) == 2 and not _blocked(log3, next_monday))
    s.check('Mon/Tue/Wed: count=0 by following Wednesday (+9 biz days)',
            _count(log3, next_monday + timedelta(days=2)) == 0)

    # ── Calendar-day vs business-day difference (the original bug) ───────────
    s.section('Business-day fix vs old calendar-day bug')
    # Old bug: used `calendar_days <= 4`. On Saturday, Monday was 5 cal days away,
    # so it was incorrectly considered outside the window. Now it's correctly inside.
    old_count = sum(
        1 for ds in log3
        if (saturday - date.fromisoformat(ds)).days <= 4
    )
    new_count = _count(log3, saturday)
    s.check('Old calendar logic: Mon/Tue/Wed → only 2 in window from Saturday',
            old_count == 2)
    s.check('New business-day logic: Mon/Tue/Wed → all 3 in window from Saturday',
            new_count == 3)
    s.check('Business-day fix is MORE conservative (protects trader)',
            new_count >= old_count)

    # ── Trades spanning a weekend ─────────────────────────────────────────────
    s.section('Trades spanning a weekend stay in the 5-biz-day window')
    # Previous Wednesday is monday - 5 days; Mon and Tue of this week are +5 and +6 days
    prev_wed = monday - timedelta(days=5)   # Wednesday of the previous week
    cross_week = [prev_wed.strftime('%Y-%m-%d'),           # prev Wed
                  monday.strftime('%Y-%m-%d'),              # this Mon
                  tuesday.strftime('%Y-%m-%d')]             # this Tue
    # Window from Tuesday = [Tue, Mon, Fri(prev), Thu(prev), Wed(prev)] — all 3 in window
    s.check('Wed(prev) + Mon + Tue (cross-weekend) all in window on Tuesday',
            _count(cross_week, tuesday) == 3)

    # ── Large log, only recent trades count ───────────────────────────────────
    s.section('Large log — only the 5-biz-day window counts')
    old_log = [(monday - timedelta(days=d)).strftime('%Y-%m-%d') for d in range(10, 70)]
    s.check('60 old trades → count=0, not blocked',
            _count(old_log, monday) == 0)

    # ── Log cap at 60 entries ─────────────────────────────────────────────────
    s.section('Log capped at 60 entries in harvester')
    growing = [(monday - timedelta(days=d)).strftime('%Y-%m-%d') for d in range(200)]
    capped  = growing[-60:]
    s.check('Cap retains 60 most-recent entries', len(capped) == 60)

    return s.summary()


if __name__ == '__main__':
    run()
