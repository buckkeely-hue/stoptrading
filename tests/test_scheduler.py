"""
Scheduler / NYSE calendar tests.
Intent preserved: the system ONLY runs on real NYSE trading days.
A wrong holiday means trading on a closed market — or missing an open one.
"""
from datetime import date, datetime, timezone, timedelta
from tests.harness_utils import Suite


def run() -> tuple:
    s = Suite('SCHEDULER — NYSE calendar, trading-day detection, DST')

    from modules.scheduler import _is_trading_day, _nyse_holidays, _easter, _nth_weekday

    # ── 2026 confirmed NYSE holidays ──────────────────────────────────────────
    s.section('2026 NYSE holidays (all must be non-trading days)')

    known_2026 = {
        '2026-01-01': "New Year's Day",
        '2026-01-19': 'MLK Day (3rd Mon Jan)',
        '2026-02-16': "Presidents' Day (3rd Mon Feb)",
        '2026-04-03': 'Good Friday (Easter Apr 5)',
        '2026-05-25': 'Memorial Day (last Mon May) — TODAY',
        '2026-06-19': 'Juneteenth',
        '2026-07-03': 'Independence Day (observed, Jul 4 is Sat)',
        '2026-09-07': 'Labor Day (1st Mon Sep)',
        '2026-11-26': 'Thanksgiving (4th Thu Nov)',
        '2026-12-25': 'Christmas',
    }
    for ds, label in known_2026.items():
        y, m, d_ = map(int, ds.split('-'))
        dt = datetime(y, m, d_, 10, 0, tzinfo=timezone.utc)
        s.check(f'{ds} closed — {label}', not _is_trading_day(dt))

    # ── Weekends ──────────────────────────────────────────────────────────────
    s.section('Weekends are never trading days')
    weekends_2026 = [
        ('2026-01-03', 'Sat'), ('2026-01-04', 'Sun'),
        ('2026-05-23', 'Sat'), ('2026-05-24', 'Sun'),
        ('2026-12-26', 'Sat'), ('2026-12-27', 'Sun'),
    ]
    for ds, label in weekends_2026:
        y, m, d_ = map(int, ds.split('-'))
        dt = datetime(y, m, d_, 12, 0, tzinfo=timezone.utc)
        s.check(f'{ds} ({label}) not a trading day', not _is_trading_day(dt))

    # ── Normal trading days ───────────────────────────────────────────────────
    s.section('Normal weekdays (non-holiday) ARE trading days')
    trading_days = [
        ('2026-01-02', 'Friday after New Year'),
        ('2026-01-20', 'Day after MLK Day'),
        ('2026-05-26', 'Day after Memorial Day'),
        ('2026-07-06', 'Monday after July 4th weekend'),
        ('2026-09-08', 'Day after Labor Day'),
        ('2026-11-27', 'Day after Thanksgiving'),
        ('2026-12-24', 'Christmas Eve (no early close rule here)'),
    ]
    for ds, label in trading_days:
        y, m, d_ = map(int, ds.split('-'))
        dt = datetime(y, m, d_, 12, 0, tzinfo=timezone.utc)
        s.check(f'{ds} is a trading day — {label}', _is_trading_day(dt))

    # ── Easter algorithm spot-checks ──────────────────────────────────────────
    s.section('Easter algorithm (drives Good Friday)')
    known_easters = {
        2024: date(2024, 3, 31),
        2025: date(2025, 4, 20),
        2026: date(2026, 4,  5),
        2027: date(2027, 3, 28),
    }
    for year, expected in known_easters.items():
        computed = _easter(year)
        s.check(f'Easter {year} = {expected}', computed == expected,
                f'got {computed}' if computed != expected else '')

    # ── Good Friday derived correctly ─────────────────────────────────────────
    s.section('Good Friday (2 days before Easter) is a holiday')
    for year, easter in known_easters.items():
        gf = easter - timedelta(days=2)
        dt = datetime(gf.year, gf.month, gf.day, 12, 0, tzinfo=timezone.utc)
        s.check(f'Good Friday {year} ({gf}) is closed', not _is_trading_day(dt))

    # ── Holiday observed-date rules ───────────────────────────────────────────
    s.section('Observed-date rules (Sat→Fri, Sun→Mon)')
    # 2021: Jul 4 was a Sunday → observed Mon Jul 5
    # Note: Jul 4 itself is a Sunday — already closed by the weekend gate.
    # The test of interest is that Mon Jul 5 is the observed close.
    jul4_2021 = datetime(2021, 7, 4, 12, 0, tzinfo=timezone.utc)
    jul5_2021 = datetime(2021, 7, 5, 12, 0, tzinfo=timezone.utc)
    jul6_2021 = datetime(2021, 7, 6, 12, 0, tzinfo=timezone.utc)
    s.check('2021-07-04 (Sun) is closed — weekend gate (not holiday logic)',
            not _is_trading_day(jul4_2021))
    s.check('2021-07-05 (Mon observed Independence Day) is closed',
            not _is_trading_day(jul5_2021))
    s.check('2021-07-06 (Tue) is a normal trading day',
            _is_trading_day(jul6_2021))

    # 2020: Christmas Dec 25 was a Friday — no shift needed
    xmas_2020 = datetime(2020, 12, 25, 12, 0, tzinfo=timezone.utc)
    s.check('2020-12-25 (Fri) is closed', not _is_trading_day(xmas_2020))

    # ── DST transition dates don't break detection ────────────────────────────
    s.section('DST transitions do not break trading-day detection')
    # Spring forward 2026: Mar 8 (Sunday) — weekend, not a trading day
    # Fall back 2026: Nov 1 (Sunday) — weekend, not a trading day
    dst_days = [
        ('2026-03-08', False, 'DST spring-forward (Sunday) — weekend'),
        ('2026-03-09', True,  'Monday after spring forward — trading day'),
        ('2026-11-01', False, 'DST fall-back (Sunday) — weekend'),
        ('2026-11-02', True,  'Monday after fall back — trading day'),
    ]
    for ds, expect_trading, label in dst_days:
        y, m, d_ = map(int, ds.split('-'))
        dt = datetime(y, m, d_, 12, 0, tzinfo=timezone.utc)
        result = _is_trading_day(dt)
        s.check(f'{ds} ({label}): trading={expect_trading}',
                result == expect_trading, f'got {result}')

    # ── Multi-year holiday cache doesn't corrupt ──────────────────────────────
    s.section('Holiday cache is year-isolated')
    h2025 = _nyse_holidays(2025)
    h2026 = _nyse_holidays(2026)
    s.check('2025 and 2026 holiday sets are distinct objects', h2025 is not h2026)
    s.check('2025 set has 10 holidays',  len(h2025) == 10, f'got {len(h2025)}')
    s.check('2026 set has 10 holidays',  len(h2026) == 10, f'got {len(h2026)}')

    # Memorial Day shifts year to year
    s.check('Memorial Day 2025 ≠ Memorial Day 2026',
            '2025-05-26' in h2025 and '2026-05-25' in h2026)

    return s.summary()


if __name__ == '__main__':
    run()
