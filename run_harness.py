#!/usr/bin/env python3
"""
StopTrading Test Harness — master runner.

Usage:
    python3 run_harness.py           # run all suites
    python3 run_harness.py auth      # run one suite by name
    python3 run_harness.py auth pdt  # run multiple suites

Each suite maps to a module under tests/. A non-zero exit code means at least
one assertion failed — safe to hook into CI or a pre-commit check.
"""
import sys, os, time, importlib

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

HEAD  = '\033[94m'
BOLD  = '\033[1m'
GREEN = '\033[92m'
RED   = '\033[91m'
RESET = '\033[0m'

SUITES = {
    'auth':       'tests.test_auth',
    'scheduler':  'tests.test_scheduler',
    'pdt':        'tests.test_pdt',
    'catalyst':   'tests.test_catalyst',
    'config':     'tests.test_config',
    'routes':     'tests.test_routes',
    'notify':     'tests.test_notify',
    'behavioral': 'tests.test_behavioral',
    'fixtures':   'tests.test_fixtures',
}

SUITE_PURPOSE = {
    'auth':       'Password hashing · OTP lifecycle · rate limiting · production isolation',
    'scheduler':  'NYSE holiday calendar · trading-day detection · DST safety',
    'pdt':        'FINRA Rule 4210 · business-day rolling window · age-out behaviour',
    'catalyst':   'Dilution guard expiry · seen-IDs ring buffer · congress guard',
    'config':     'Field persistence · partial save · secret masking',
    'routes':     'Auth protection · symbol validation · numeric sanitisation · headers',
    'notify':     'Delivery chain priority · channel fallthrough · message content',
    'behavioral': 'Time-of-day session gates · boundary precision · DST clock safety',
    'fixtures':   'Congress/catalyst API response parsing · dedup · schema validation',
}


def banner():
    print(f'\n{BOLD}{HEAD}{"═"*62}')
    print('  StopTrading — Architect Test Harness')
    print(f'  {time.strftime("%Y-%m-%d  %H:%M:%S ET")}')
    print(f'{"═"*62}{RESET}')
    print(f'\n  {"Suite":<12}  Purpose')
    print(f'  {"─"*12}  {"─"*44}')
    for name, purpose in SUITE_PURPOSE.items():
        print(f'  {name:<12}  {purpose}')
    print()


def run_suite(name: str) -> tuple:
    """Import and run a suite. Returns (passed, failed, total)."""
    module_path = SUITES[name]
    try:
        mod = importlib.import_module(module_path)
        importlib.reload(mod)   # ensures fresh state on re-runs
        return mod.run()
    except Exception as e:
        import traceback
        print(f'\n{RED}  SUITE CRASH: {name}{RESET}')
        traceback.print_exc()
        return 0, 1, 1


def main():
    requested = [a.lower() for a in sys.argv[1:]]
    unknown   = [r for r in requested if r not in SUITES]
    if unknown:
        print(f'\nUnknown suite(s): {unknown}')
        print(f'Available: {list(SUITES.keys())}')
        sys.exit(1)

    to_run = requested if requested else list(SUITES.keys())
    banner()

    total_passed = total_failed = total_tests = 0
    suite_results = []

    for name in to_run:
        start = time.time()
        passed, failed, total = run_suite(name)
        elapsed = time.time() - start
        total_passed += passed
        total_failed += failed
        total_tests  += total
        suite_results.append((name, passed, failed, total, elapsed))

    # ── Summary table ─────────────────────────────────────────────────────────
    print(f'\n{BOLD}{HEAD}{"═"*62}')
    print('  HARNESS SUMMARY')
    print(f'{"═"*62}{RESET}')
    print(f'\n  {"Suite":<12}  {"Pass":>5}  {"Fail":>5}  {"Total":>6}  {"Time":>6}')
    print(f'  {"─"*12}  {"─"*5}  {"─"*5}  {"─"*6}  {"─"*6}')

    for name, passed, failed, total, elapsed in suite_results:
        color = GREEN if failed == 0 else RED
        flag  = '' if failed == 0 else '  ◀ FAILED'
        print(f'  {color}{name:<12}  {passed:>5}  {failed:>5}  {total:>6}  '
              f'{elapsed:>5.1f}s{flag}{RESET}')

    print(f'  {"─"*12}  {"─"*5}  {"─"*5}  {"─"*6}')
    overall_color = GREEN if total_failed == 0 else RED
    print(f'  {overall_color}{"TOTAL":<12}  {total_passed:>5}  {total_failed:>5}  {total_tests:>6}{RESET}')

    if total_failed == 0:
        print(f'\n  {GREEN}{BOLD}All checks passed — intent preserved.{RESET}')
    else:
        print(f'\n  {RED}{BOLD}{total_failed} check(s) failed — review before merging.{RESET}')

    print()

    # ── Known gaps (architectural notes) ─────────────────────────────────────
    print(f'{HEAD}── Known harness gaps (not failures — require live data) ──{RESET}')
    gaps = [
        'Harvester buy/sell signal logic requires live Alpaca paper trading API',
        'Real ntfy delivery verified only in test_auth_flow.py (live test)',
        'Gmail OAuth token expiry tested live — mock only in test_notify.py',
        'Backtest module not covered — yfinance data required',
        'IBKR feed path not covered — requires broker connection',
        'Form4 SEC EDGAR response parsing not yet fixture-covered',
        'Macro/SSR/halt signal logic not yet covered (external data feeds)',
    ]
    for i, gap in enumerate(gaps, 1):
        print(f'  {i:>2}. {gap}')
    print()

    sys.exit(0 if total_failed == 0 else 1)


if __name__ == '__main__':
    main()
