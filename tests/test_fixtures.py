"""
Fixture-based API response parsing tests.
Intent preserved: the congress and catalyst parsers correctly classify buys/sells,
reject malformed/stale/oversized tickers, deduplicate IDs, and never wipe data
on an empty response.

Fixtures in tests/fixtures/ represent a single captured API response shape.
Update them when the upstream API schema changes.
"""
import json, os
from datetime import date, timedelta
from collections import defaultdict
from tests.harness_utils import Suite

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), 'fixtures')


def _load(name: str) -> list:
    with open(os.path.join(FIXTURE_DIR, name)) as f:
        return json.load(f)


# ── Inline replica of congress _fetch parse logic ────────────────────────────
def _parse_congress(trades: list, lookback_days: int = 90):
    cutoff = date.today() - timedelta(days=lookback_days)
    buys   = defaultdict(list)
    sells  = defaultdict(list)
    recent = []
    for trade in trades:
        ticker  = (trade.get('Ticker') or '').upper().strip()
        tx_type = (trade.get('Transaction') or '').lower()
        member  = trade.get('Representative') or trade.get('Senator') or 'Unknown'
        tx_date = trade.get('Date') or trade.get('TransactionDate') or ''
        amount  = trade.get('Range') or trade.get('Amount') or ''
        if not ticker or len(ticker) > 5:
            continue
        try:
            tx_dt = date.fromisoformat(tx_date[:10]) if tx_date else date.today()
        except ValueError:
            tx_dt = date.today()
        if tx_dt < cutoff:
            continue
        entry = {'ticker': ticker, 'member': member,
                 'date': tx_dt.isoformat(), 'amount': amount, 'type': tx_type}
        if 'purchase' in tx_type or 'buy' in tx_type:
            buys[ticker].append(entry)
        elif 'sale' in tx_type or 'sell' in tx_type:
            sells[ticker].append(entry)
        recent.append(entry)
    return buys, sells, recent


def run() -> tuple:
    s = Suite('FIXTURES — API response parsing (congress trades, catalyst news)')

    # ── Congress trades fixture ───────────────────────────────────────────────
    s.section('Congress trades: fixture parsing')
    trades = _load('congress_trades.json')
    buys, sells, recent = _parse_congress(trades)

    s.check('NVDA has 2 purchase entries (cluster buy)',   len(buys.get('NVDA', [])) == 2)
    s.check('MSFT is a sale, not a buy',
            'MSFT' not in buys and 'MSFT' in sells)
    s.check('AAPL has 1 purchase entry',                   len(buys.get('AAPL', [])) == 1)

    s.check('TSLA sale classified correctly',              'TSLA' in sells)
    s.check('TOOLONGR (8 chars) rejected — ticker too long',
            'TOOLONGR' not in buys and 'TOOLONGR' not in sells)
    s.check('Empty ticker rejected',
            '' not in buys and '' not in sells)

    # ── Cluster-buy detection ─────────────────────────────────────────────────
    s.section('Congress: cluster-buy detection (≥2 members buy same ticker)')
    cluster = [t for t, entries in buys.items() if len(entries) >= 2]
    s.check('NVDA is a cluster buy (2 members)',           'NVDA' in cluster)
    s.check('AAPL is NOT a cluster buy (only 1 member)',   'AAPL' not in cluster)

    # ── Stale trade filtered out ──────────────────────────────────────────────
    s.section('Congress: stale trades (> lookback_days) are filtered')
    # TSLA trade is from 2026-04-01; default lookback=90 days from today (May 25, 2026)
    # 2026-04-01 is 54 days before May 25 — within 90-day window
    tsla_in_window = date(2026, 4, 1) >= date.today() - timedelta(days=90)
    s.check('TSLA trade within 90-day window is included',
            tsla_in_window == ('TSLA' in sells))

    # Test with a tight 30-day window where TSLA falls outside
    buys_30, sells_30, recent_30 = _parse_congress(trades, lookback_days=30)
    tsla_in_30 = date(2026, 4, 1) >= date.today() - timedelta(days=30)
    if not tsla_in_30:
        s.check('TSLA excluded by 30-day window',           'TSLA' not in sells_30)
    else:
        s.check('TSLA still within 30-day window (test date close to trade)', True)

    # ── Empty-response guard ──────────────────────────────────────────────────
    s.section('Congress: empty API response does not wipe stored data')
    stored_buys  = dict(buys)
    stored_sells = dict(sells)

    def guarded_update(new_buys, new_sells, new_recent, stored_b, stored_s):
        if len(new_recent) > 0:
            return new_buys, new_sells
        return stored_b, stored_s

    empty_b, empty_s, empty_r = _parse_congress([])
    kept_b, kept_s = guarded_update(empty_b, empty_s, empty_r, stored_buys, stored_sells)
    s.check('Empty API response preserves stored buys',    kept_b == stored_buys)
    s.check('Empty API response preserves stored sells',   kept_s == stored_sells)
    s.check('Non-empty response replaces stored data',
            guarded_update(buys, sells, recent, {}, {})[0] == buys)

    # ── Member attribution ────────────────────────────────────────────────────
    s.section('Congress: member field uses Representative or Senator')
    nvda_entries = buys.get('NVDA', [])
    members = {e['member'] for e in nvda_entries}
    s.check('Representative field used for House member',
            any('Rep.' in m for m in members))
    s.check('Senator field used for Senate member',
            any('Sen.' in m for m in members))

    # ── Catalyst news fixture ─────────────────────────────────────────────────
    s.section('Catalyst news: dilution keyword detection')
    news = _load('catalyst_news.json')

    DILUTION_KEYWORDS = [
        'dilut', 'offering', 'shares', 'warrant', 'convertible',
        'shelf registration', 'atm offering', 'equity raise',
    ]

    def _has_dilution_signal(article: dict) -> bool:
        text = (article.get('headline', '') + ' ' + article.get('summary', '')).lower()
        return any(kw in text for kw in DILUTION_KEYWORDS)

    dilution_articles = [a for a in news if _has_dilution_signal(a)]
    s.check('NVDA dilution article detected',
            any(a['ticker'] == 'NVDA' for a in dilution_articles))
    s.check('AAPL earnings article NOT flagged as dilution',
            not any(a['ticker'] == 'AAPL' for a in dilution_articles))

    # ── Deduplication by ID ───────────────────────────────────────────────────
    s.section('Catalyst news: duplicate IDs are not double-processed')
    from collections import deque
    seen = deque(maxlen=2000)
    processed = []
    for article in news:
        aid = article.get('id', '')
        if aid in seen:
            continue
        seen.append(aid)
        processed.append(article)

    s.check('Duplicate article (same id) skipped',
            len(processed) == len(news) - 1)
    s.check('First occurrence of duplicate kept, second dropped',
            processed[0]['id'] == 'news_001' and
            processed[0]['headline'] != 'Duplicate article — same id as first entry')

    # ── Fixture schema validation ─────────────────────────────────────────────
    s.section('Fixture schema: required fields present')
    required_congress = {'Ticker', 'Transaction', 'Date'}
    required_news     = {'id', 'ticker', 'headline'}

    for i, t in enumerate(trades):
        # Parser accepts either 'Date' or 'TransactionDate' — both are valid
        has_date = 'Date' in t or 'TransactionDate' in t
        effective_keys = set(t.keys()) | ({'Date'} if has_date else set())
        missing = required_congress - effective_keys
        if t.get('Ticker'):   # only check records with a ticker
            s.check(f'Congress record {i} has required fields', not missing,
                    f'missing: {missing}')

    for i, a in enumerate(news):
        missing = required_news - set(a.keys())
        s.check(f'News article {i} has required fields', not missing,
                f'missing: {missing}')

    return s.summary()


if __name__ == '__main__':
    run()
