import os
import re
import threading
import webbrowser
import time
from flask import Flask, request, jsonify, send_from_directory, session, redirect
from modules.auth import (require_auth, check_password, get_secret_key,
                          generate_reset_token, consume_reset_token,
                          generate_change_token, consume_change_token,
                          check_rate_limit, record_failed_attempt, clear_rate_limit,
                          emergency_reset, get_recovery_hint)

from config import load_config, save_config
from modules.scanner import StockScanner
from modules.watchlist import WatchlistManager
from modules.paper_trade import PaperTrader
from modules.sentiment import SentimentAnalyzer
from modules.auto_trade import AutoTrader
from modules.trend import TrendAnalyzer
from modules.harvester import StockHarvester, AutoPilot, TOP_PENNY_UNIVERSE
from modules.realtime import RealtimeEngine
from modules.accounting import AccountingLedger
from modules.trial import TrialManager
from modules.catalyst import CatalystEngine
from modules.universe import DynamicUniverse
from modules.scheduler import DailyScheduler
from modules.notify    import Notifier
from modules.halts        import HaltMonitor
from modules.ssr          import SSRMonitor
from modules.market_data  import MarketData
from modules.ibkr_feed    import IBKRFeed
from modules.macro_signals   import MacroSignalAgent
from modules.behavioral      import BehavioralAgent
from modules.form4           import InsiderTradeAgent
from modules.earnings_guard  import EarningsGuard
from modules.congress_trades  import CongressAgent
from modules.orb              import ORBAgent
from modules.news_catalyst    import NewsAgent
from modules.float_rotation   import FloatRotationAgent
from modules.short_squeeze    import ShortSqueezeAgent
from modules.sector_momentum  import SectorMomentumAgent

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=None)   # all static files served via explicit routes
app.secret_key = get_secret_key()
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'
app.config['PERMANENT_SESSION_LIFETIME'] = 28800   # 8 hours


@app.after_request
def _security_headers(response):
    response.headers.pop('Server', None)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options']        = 'DENY'
    return response

# ── Helpers ───────────────────────────────────────────────────────────────────

_SYMBOL_RE = re.compile(r'^[A-Z][A-Z0-9.]{0,9}$')

def _valid_symbol(s: str) -> bool:
    return bool(s and _SYMBOL_RE.match(s.upper().strip()))

def _safe_float(val, default: float) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default

def _safe_int(val, default: int) -> int:
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default

config = load_config()
scanner = StockScanner(universe=None, config=config)   # wired to universe after it starts below
watchlist = WatchlistManager()
paper_trader = PaperTrader(config)
sentiment = SentimentAnalyzer(config)
auto_trader = AutoTrader(paper_trader, watchlist, config)
trend_analyzer = TrendAnalyzer()
catalyst = CatalystEngine(config)
catalyst.start()
universe = DynamicUniverse(catalyst_engine=catalyst)
universe.start()
scanner.universe = universe   # now wired to live universe
harvester = StockHarvester(paper_trader, config, universe=universe)
harvester.start_monitor()
engine = RealtimeEngine(config)
engine.start(universe.get() or TOP_PENNY_UNIVERSE)
notifier  = Notifier(config)
halts     = HaltMonitor()
halts.start()
ssr       = SSRMonitor()
ssr.start()
market_data = MarketData(config)
ibkr        = IBKRFeed(config)
ibkr.start(universe.get())
macro       = MacroSignalAgent(config)
macro.start()
behavioral  = BehavioralAgent(config)
behavioral.start()
insider     = InsiderTradeAgent(config)
insider.start()
earnings    = EarningsGuard(config)
earnings.start()
earnings.warm(universe.get())   # pre-warm cache before market open
congress    = CongressAgent(config)
congress.start()
orb         = ORBAgent(universe)
orb.start()
news_agent  = NewsAgent(config, universe)
news_agent.start()
float_rot   = FloatRotationAgent(universe, harvester._float_cache, engine=engine)
float_rot.start()
short_sq    = ShortSqueezeAgent(config, universe, ssr=ssr, engine=engine)
short_sq.start()
sector      = SectorMomentumAgent(config)
sector.start()
autopilot = AutoPilot(harvester, paper_trader, config, engine=engine, catalyst=catalyst,
                      notifier=notifier, halts=halts, ssr=ssr,
                      macro=macro, behavioral=behavioral,
                      insider=insider, earnings=earnings, congress=congress,
                      orb=orb, news=news_agent, float_rotation=float_rot,
                      short_squeeze=short_sq, sector=sector, ibkr=ibkr)
ledger    = AccountingLedger(config, paper_trader)
trial     = TrialManager(autopilot, paper_trader, harvester, config)
scheduler = DailyScheduler(autopilot, paper_trader, load_config)
scheduler.start()

if config.get('auto_trade_enabled'):
    auto_trader.start()

if config.get('auto_start_enabled') and not autopilot.running:
    _bal = float(config.get('starting_balance', 500.0))
    autopilot.resume()   # resume preserves saved state; start() would reset the account


@app.route('/api/health')
@require_auth
def api_health():
    try:
        return jsonify({
            'status': 'ok',
            'agents': {
                'autopilot':  autopilot.running,
                'catalyst':   catalyst.running,
                'universe':   universe.running,
                'halts':      halts.running,
                'ssr':        ssr.running,
                'macro':      macro.running,
                'behavioral': behavioral.running,
                'insider':    insider.running,
                'earnings':   earnings.running,
                'congress':      congress.running,
                'orb':           orb.running,
                'news':          news_agent.running,
                'float_rotation':float_rot.running,
                'short_squeeze': short_sq.running,
                'sector':        sector.running,
                'ibkr':          ibkr.connected if hasattr(ibkr, 'connected') else False,
            },
            'universe_size': len(universe.get()),
            'earnings_cached': earnings.get_summary().get('dates_known', 0),
            'config': {
                'cash_account_mode':   bool(config.get('cash_account_mode', False)),
                'harvest_trigger_pct': float(config.get('harvest_trigger_pct', 8.0)),
                'min_entry_rvol':      float(config.get('min_entry_rvol', 1.2)),
            },
        })
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500

@app.route('/')
@require_auth
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/login')
def login_page():
    if session.get('authenticated'):
        return redirect('/')
    return send_from_directory(BASE_DIR, 'login.html')


@app.route('/api/login', methods=['POST'])
def api_login():
    ip = request.remote_addr or 'unknown'
    if check_rate_limit(ip):
        return jsonify({'ok': False, 'error': 'Too many failed attempts — try again in 15 minutes'}), 429
    data = request.get_json(force=True) or {}
    if check_password(data.get('password', '')):
        clear_rate_limit(ip)
        session['authenticated'] = True
        session.permanent = True
        return jsonify({'ok': True})
    record_failed_attempt(ip)
    return jsonify({'ok': False, 'error': 'Invalid password'}), 401


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/change-password')
@require_auth
def change_password_page():
    return send_from_directory(BASE_DIR, 'change-password.html')


@app.route('/api/change-password/request', methods=['POST'])
@require_auth
def api_change_password_request():
    cfg = load_config()
    ok, channel, dest = generate_change_token(cfg)
    if not ok:
        return jsonify({'ok': False, 'error': 'No notification channel configured. Add phone or email in Settings → Notifications.'}), 503
    return jsonify({'ok': True, 'channel': channel, 'dest': dest})


@app.route('/api/change-password/confirm', methods=['POST'])
@require_auth
def api_change_password_confirm():
    data   = request.get_json(force=True) or {}
    code   = str(data.get('code', '')).strip()
    new_pw = data.get('new_password', '')
    if len(new_pw) < 8:
        return jsonify({'ok': False, 'error': 'New password must be at least 8 characters'}), 400
    if consume_change_token(code, new_pw):
        session.clear()
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Invalid or expired code'}), 401


@app.route('/reset')
def reset_page():
    return send_from_directory(BASE_DIR, 'reset.html')


@app.route('/api/reset-password/request', methods=['POST'])
def api_reset_request():
    cfg = load_config()
    ok, channel, dest = generate_reset_token(cfg)
    if not ok:
        return jsonify({'ok': False, 'error': 'No notification channel configured. Add phone or email in Settings → Notifications.'}), 503
    return jsonify({'ok': True, 'channel': channel, 'dest': dest})


@app.route('/api/reset-password/confirm', methods=['POST'])
def api_reset_confirm():
    data  = request.get_json(force=True) or {}
    token = str(data.get('token', '')).strip()
    pw    = data.get('password', '')
    if len(pw) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters'}), 400
    if consume_reset_token(token, pw):
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Invalid or expired code'}), 401


@app.route('/api/reset-password/emergency', methods=['POST'])
def api_emergency_reset():
    data = request.get_json(force=True) or {}
    code = str(data.get('recovery_code', '')).strip()
    pw   = data.get('password', '')
    if len(pw) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters'}), 400
    if emergency_reset(code, pw):
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Invalid recovery code'}), 401


@app.route('/api/reset-password/recovery-hint')
def api_recovery_hint():
    return jsonify({'hint': get_recovery_hint()})


@app.route('/index.css')
def styles():
    return send_from_directory(BASE_DIR, 'index.css')


@app.route('/api/scan')
@require_auth
def api_scan():
    try:
        results = scanner.scan()
        return jsonify(results)
    except Exception:
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/trend')
@require_auth
def api_trend():
    symbol = request.args.get('symbol', '').strip().upper()
    if not _valid_symbol(symbol):
        return jsonify({'error': 'Invalid symbol'}), 400
    return jsonify(trend_analyzer.analyze(symbol))


@app.route('/api/harvest/scan')
@require_auth
def api_harvest_scan():
    return jsonify(harvester.scan_movers())

@app.route('/api/harvest/state')
@require_auth
def api_harvest_state():
    return jsonify(harvester.get_state())

@app.route('/api/harvest/start', methods=['POST'])
@require_auth
def api_harvest_start():
    b = request.get_json() or {}
    symbol = b.get('symbol', '').strip().upper()
    capital = _safe_float(b.get('capital'), 500.0)
    if not _valid_symbol(symbol):
        return jsonify({'error': 'Invalid symbol'}), 400
    return jsonify(harvester.start_harvest(symbol, capital))

@app.route('/api/autopilot/status')
@require_auth
def api_autopilot_status():
    return jsonify(autopilot.get_status())

@app.route('/api/autopilot/start', methods=['POST'])
@require_auth
def api_autopilot_start():
    b = request.get_json() or {}
    balance = _safe_float(b.get('balance'), 500.0)
    return jsonify(autopilot.start(balance))

@app.route('/api/autopilot/stop', methods=['POST'])
@require_auth
def api_autopilot_stop():
    return jsonify(autopilot.stop())

@app.route('/api/autopilot/resume', methods=['POST'])
@require_auth
def api_autopilot_resume():
    return jsonify(autopilot.resume())

@app.route('/api/vault')
@require_auth
def api_vault_status():
    return jsonify(autopilot.get_vault_status())

@app.route('/api/vault/transfer', methods=['POST'])
@require_auth
def api_vault_transfer():
    b = request.get_json() or {}
    amount = _safe_float(b.get('amount'), None)
    return jsonify(autopilot.vault_transfer_to_trading(amount))

@app.route('/api/vault/withdraw', methods=['POST'])
@require_auth
def api_vault_withdraw():
    b = request.get_json() or {}
    amount = _safe_float(b.get('amount'), None)
    return jsonify(autopilot.vault_withdraw(amount))


@app.route('/api/accounting')
@require_auth
def api_accounting():
    return jsonify(ledger.get_report())

@app.route('/api/accounting/siphon', methods=['POST'])
@require_auth
def api_accounting_siphon():
    b = request.get_json() or {}
    amount = _safe_float(b.get('amount'), 0.0)
    return jsonify(ledger.manual_siphon(amount))

@app.route('/api/accounting/return', methods=['POST'])
@require_auth
def api_accounting_return():
    b = request.get_json() or {}
    amount = _safe_float(b.get('amount'), 0.0)
    return jsonify(ledger.return_to_trading(amount))

@app.route('/api/accounting/reset', methods=['POST'])
@require_auth
def api_accounting_reset():
    b = request.get_json() or {}
    bal = b.get('balance')
    ledger.reset(_safe_float(bal, None) if bal is not None else None)
    return jsonify({'ok': True})


@app.route('/api/scheduler')
@require_auth
def api_scheduler():
    return jsonify(scheduler.get_status())

@app.route('/api/scheduler/config', methods=['POST'])
@require_auth
def api_scheduler_config():
    global config
    b = request.get_json() or {}
    existing = load_config()
    if 'auto_start_enabled' in b:
        existing['auto_start_enabled'] = bool(b['auto_start_enabled'])
    if 'daily_balance' in b:
        existing['daily_balance'] = _safe_float(b.get('daily_balance'), existing.get('daily_balance', 500.0))
    if 'daily_spend_limit' in b:
        existing['daily_spend_limit'] = _safe_float(b.get('daily_spend_limit'), existing.get('daily_spend_limit', 500.0))
    config = save_config(existing)
    return jsonify({'ok': True})

@app.route('/api/report')
@require_auth
def api_report():
    import yfinance as yf
    from datetime import datetime
    state     = paper_trader.get_state()
    ap        = autopilot.get_status()
    balance   = state.get('balance', 0)
    positions = state.get('positions', [])
    history   = state.get('history', [])

    enriched = []
    for p in positions:
        symbol = p['symbol']
        entry  = float(p['avg_cost'])
        shares = int(p['shares'])
        try:
            price = float(yf.Ticker(symbol).fast_info.last_price)
        except Exception:
            price = entry
        gain_pct = (price - entry) / entry * 100 if entry > 0 else 0
        hwm = autopilot._position_hwm.get(symbol, price)
        opened = autopilot._position_opened.get(symbol)
        days_open = (datetime.now() - opened).days if opened else None
        enriched.append({
            **p,
            'current_price':   round(price, 4),
            'gain_pct':        round(gain_pct, 2),
            'market_value':    round(price * shares, 2),
            'unrealized_pnl':  round((price - entry) * shares, 2),
            'hwm':             round(hwm, 4),
            'days_open':       days_open,
        })

    stats  = ap.get('stats', {})
    wins   = stats.get('wins', 0)
    losses = stats.get('losses', 0)

    # Today's trades from history
    today = datetime.now().strftime('%Y-%m-%d')
    todays_history = [h for h in history if h.get('time', '').startswith(today)]

    return jsonify({
        'balance':           balance,
        'positions':         enriched,
        'todays_trades':     todays_history,
        'all_history':       history[-200:],
        'autopilot_running': ap.get('running', False),
        'daily_spent':       ap.get('daily_spent', 0),
        'daily_limit':       float(config.get('daily_spend_limit', 100.0)),
        'stats': {
            'total_trades':   stats.get('total_trades', 0),
            'total_harvests': stats.get('total_harvests', 0),
            'total_profit':   round(stats.get('total_profit', 0), 2),
            'wins':           wins,
            'losses':         losses,
            'win_rate':       round(wins / max(wins + losses, 1) * 100, 1),
            'avg_win_pct':    round(stats.get('total_win_pct', 0) / max(wins, 1), 2),
            'avg_loss_pct':   round(stats.get('total_loss_pct', 0) / max(losses, 1), 2),
        },
        'scheduler':         scheduler.get_status(),
        'accounting':        ledger.get_report(),
    })


@app.route('/api/trial')
@require_auth
def api_trial():
    return jsonify(trial.get_status())

@app.route('/api/trial/start', methods=['POST'])
@require_auth
def api_trial_start():
    b = request.get_json() or {}
    balance = _safe_float(b.get('balance'), 500.0)
    hours   = _safe_float(b.get('hours'), 10.0)
    return jsonify(trial.start(balance, hours))

@app.route('/api/trial/stop', methods=['POST'])
@require_auth
def api_trial_stop():
    return jsonify(trial.stop())


@app.route('/api/catalyst')
@require_auth
def api_catalyst():
    return jsonify(catalyst.get_recent(50))

@app.route('/api/catalyst/scores')
@require_auth
def api_catalyst_scores():
    return jsonify(catalyst.get_all_scores())

@app.route('/api/catalyst/symbol')
@require_auth
def api_catalyst_symbol():
    symbol = request.args.get('symbol', '').strip().upper()
    if not _valid_symbol(symbol):
        return jsonify({'error': 'Invalid symbol'}), 400
    return jsonify(catalyst.get_catalyst(symbol))

@app.route('/api/universe')
@require_auth
def api_universe():
    return jsonify(universe.status())

@app.route('/api/halts')
@require_auth
def api_halts():
    return jsonify(halts.status())

@app.route('/api/universe/gappers')
@require_auth
def api_gappers():
    min_gap = _safe_float(request.args.get('min_gap'), 8.0)
    return jsonify(universe.get_premarket_gappers(min_gap_pct=min_gap))

@app.route('/api/ssr')
@require_auth
def api_ssr():
    return jsonify(ssr.status())

@app.route('/api/ibkr')
@require_auth
def api_ibkr():
    return jsonify(ibkr.status())

@app.route('/api/data-sources')
@require_auth
def api_data_sources():
    cfg = load_config()
    return jsonify({
        'polygon':   {'configured': bool(cfg.get('polygon_key')),   'provider': 'Polygon.io'},
        'benzinga':  {'configured': bool(cfg.get('benzinga_key')),  'provider': 'Benzinga Pro'},
        'finnhub':   {'configured': bool(cfg.get('finnhub_key')),   'provider': 'Finnhub'},
        'ibkr':      ibkr.status(),
        'halts':     halts.status(),
        'ssr':       {'count': ssr.count, 'last_fetch': ssr.last_fetch},
        'yfinance':  {'active': True, 'note': 'fallback — 15-min delayed'},
        'edgar':     {'active': True, 'note': 'EDGAR atom + EFTS full-text'},
    })

@app.route('/api/macro')
@require_auth
def api_macro():
    try:
        return jsonify(macro.get_summary())
    except Exception:
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/behavioral')
@require_auth
def api_behavioral():
    try:
        ticker = request.args.get('ticker', '').strip().upper()
        summary = behavioral.get_summary()
        if ticker and _valid_symbol(ticker):
            summary['ticker_score'] = behavioral.get_score(ticker)
            summary['reddit_score'] = behavioral.get_reddit_score(ticker)
            summary['stocktwits_score'] = behavioral.get_stocktwits_score(ticker)
        return jsonify(summary)
    except Exception:
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/insider')
@require_auth
def api_insider():
    try:
        ticker = request.args.get('ticker', '').strip().upper()
        if ticker and _valid_symbol(ticker):
            return jsonify({'ticker': ticker, 'score': insider.get_score(ticker),
                            'recent': insider.get_recent(10)})
        return jsonify(insider.get_summary())
    except Exception:
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/earnings')
@require_auth
def api_earnings():
    try:
        ticker = request.args.get('ticker', '').strip().upper()
        if ticker and _valid_symbol(ticker):
            return jsonify({'ticker': ticker,
                            'days_to_earnings': earnings.days_to_earnings(ticker),
                            'block_buy': earnings.should_block_buy(ticker),
                            'force_exit': earnings.should_force_exit(ticker),
                            'note': earnings.earnings_note(ticker)})
        return jsonify(earnings.get_summary())
    except Exception:
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/congress')
@require_auth
def api_congress():
    try:
        ticker = request.args.get('ticker', '').strip().upper()
        if ticker and _valid_symbol(ticker):
            return jsonify({'ticker': ticker, 'score': congress.get_score(ticker),
                            'trades': congress.get_recent(20)})
        return jsonify(congress.get_summary())
    except Exception:
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/backtest', methods=['POST'])
@require_auth
def api_backtest():
    from modules.backtest import run_backtest
    b = request.get_json() or {}
    syms = universe.get()
    result = run_backtest(
        universe=syms,
        lookback_days=_safe_int(b.get('lookback_days'), 90),
        harvest_trigger_pct=_safe_float(b.get('harvest_trigger_pct'), 7.0),
        exit_trigger_pct=_safe_float(b.get('exit_trigger_pct'), 5.0),
        stop_loss_pct=_safe_float(b.get('stop_loss_pct'), 15.0),
        per_trade=_safe_float(b.get('per_trade'), 50.0),
        daily_limit=_safe_float(b.get('daily_limit'), 500.0),
        starting_balance=_safe_float(b.get('starting_balance'), 500.0),
        tx_cost_pct=_safe_float(b.get('tx_cost_pct'), config.get('tx_cost_pct', 3.0)),
    )
    return jsonify(result)


@app.route('/api/realtime/scores')
@require_auth
def api_realtime_scores():
    return jsonify(engine.get_all_scores())


@app.route('/api/realtime/symbol')
@require_auth
def api_realtime_symbol():
    symbol = request.args.get('symbol', '').strip().upper()
    if not _valid_symbol(symbol):
        return jsonify({'error': 'Invalid symbol'}), 400
    engine.add_symbol(symbol)
    return jsonify(engine.score(symbol))


@app.route('/api/harvest/signal')
@require_auth
def api_harvest_signal():
    symbol = request.args.get('symbol', '').strip().upper()
    price  = _safe_float(request.args.get('price'), 0.0)
    if not _valid_symbol(symbol):
        return jsonify({'error': 'Invalid symbol'}), 400
    return jsonify(harvester.signals.score_symbol(symbol, price))

@app.route('/api/harvest/stop', methods=['POST'])
@require_auth
def api_harvest_stop():
    b = request.get_json() or {}
    symbol = b.get('symbol', '').strip().upper()
    if not _valid_symbol(symbol):
        return jsonify({'error': 'Invalid symbol'}), 400
    return jsonify(harvester.stop_harvest(symbol))


@app.route('/api/watchlist')
@require_auth
def api_watchlist():
    try:
        prices = watchlist.get_prices()
        return jsonify(prices)
    except Exception:
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/watchlist/add', methods=['POST'])
@require_auth
def api_watchlist_add():
    data = request.get_json(force=True) or {}
    symbol = data.get('symbol', '').upper().strip()
    if not _valid_symbol(symbol):
        return jsonify({'error': 'Invalid symbol'}), 400
    watchlist.add(symbol)
    return jsonify({'success': True, 'symbol': symbol})


@app.route('/api/watchlist/remove', methods=['POST'])
@require_auth
def api_watchlist_remove():
    data = request.get_json(force=True) or {}
    symbol = data.get('symbol', '').upper().strip()
    if not _valid_symbol(symbol):
        return jsonify({'error': 'Invalid symbol'}), 400
    watchlist.remove(symbol)
    return jsonify({'success': True, 'symbol': symbol})


@app.route('/api/paper')
@require_auth
def api_paper():
    try:
        state = paper_trader.get_state()
        return jsonify(state)
    except Exception:
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/paper/buy', methods=['POST'])
@require_auth
def api_paper_buy():
    data = request.get_json(force=True) or {}
    symbol = data.get('symbol', '').upper().strip()
    shares = _safe_int(data.get('shares'), 0)
    if not _valid_symbol(symbol) or shares <= 0:
        return jsonify({'error': 'Valid symbol and positive shares required'}), 400
    result = paper_trader.buy(symbol, shares)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/paper/sell', methods=['POST'])
@require_auth
def api_paper_sell():
    data = request.get_json(force=True) or {}
    symbol = data.get('symbol', '').upper().strip()
    shares = _safe_int(data.get('shares'), 0)
    if not _valid_symbol(symbol) or shares <= 0:
        return jsonify({'error': 'Valid symbol and positive shares required'}), 400
    result = paper_trader.sell(symbol, shares)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/sentiment')
@require_auth
def api_sentiment():
    symbol = request.args.get('symbol', '').upper().strip()
    if not _valid_symbol(symbol):
        return jsonify({'error': 'Invalid symbol'}), 400
    try:
        result = sentiment.analyze(symbol)
        return jsonify(result)
    except Exception:
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/notifications')
@require_auth
def api_notifications():
    return jsonify({
        'configured': notifier.is_configured(),
        'log':        notifier.get_log(),
    })


@app.route('/api/notifications/test', methods=['POST'])
@require_auth
def api_notifications_test():
    notifier.send('StopTrading test message — notifications are working ✓')
    return jsonify({'ok': True, 'message': 'Test notification sent'})


# All secret keys masked — never returned in plaintext
_SECRET_KEYS = {
    'alpaca_paper_key', 'alpaca_paper_secret', 'alpaca_live_key', 'alpaca_live_secret',
    'reddit_client_id', 'reddit_secret', 'reddit_client_secret',
    'polygon_key', 'benzinga_key', 'finnhub_key',
    'twilio_account_sid', 'twilio_auth_token',
    'smtp_password',
    'gmail_client_secret', 'gmail_refresh_token',
}

@app.route('/api/settings')
@require_auth
def api_settings():
    cfg = load_config()
    safe = {k: ('***' if v else '') if k in _SECRET_KEYS else v for k, v in cfg.items()}
    return jsonify(safe)


_SETTINGS_ALLOWLIST = {
    'alpaca_paper_key','alpaca_paper_secret','alpaca_live_key','alpaca_live_secret',
    'reddit_client_id','reddit_secret','finnhub_key','polygon_key','benzinga_key',
    'use_ibkr_feed','ibkr_host','ibkr_port','ibkr_client_id',
    'paper_balance','auto_trade_enabled','buy_trigger','sell_target','stop_loss',
    'harvest_trigger_pct','exit_trigger_pct','tx_cost_pct','daily_spend_limit',
    'per_trade_capital','siphon_rate','auto_siphon','live_mode',
    'max_daily_loss_pct','max_consecutive_losses','position_size_pct',
    'notify_phone','ntfy_topic','twilio_account_sid','twilio_auth_token','twilio_from',
    'notify_email','smtp_host','smtp_port','smtp_user','smtp_password',
    'buy_momentum_threshold','sell_profit_target','stop_loss_pct',
    'reddit_client_secret','reddit_user_agent',
}

@app.route('/api/settings', methods=['POST'])
@require_auth
def api_settings_save():
    data = request.get_json(force=True) or {}
    global config
    existing = load_config()
    clean = {}
    for k, v in data.items():
        if k not in _SETTINGS_ALLOWLIST:
            continue
        clean[k] = existing.get(k, '') if v == '***' else v
    config = save_config(clean)
    sentiment._config    = config
    auto_trader._config  = config
    paper_trader._config = config
    notifier.config      = config
    autopilot.config     = config
    market_data.config   = config
    ibkr.config          = config
    return jsonify({'success': True})


@app.route('/api/autotrade')
@require_auth
def api_autotrade():
    cfg = load_config()
    return jsonify({
        'enabled': cfg.get('auto_trade_enabled', False),
        'log': auto_trader.get_log(),
    })


@app.route('/api/autotrade/toggle', methods=['POST'])
@require_auth
def api_autotrade_toggle():
    global config
    cfg = load_config()
    enabled = not cfg.get('auto_trade_enabled', False)
    config = save_config({'auto_trade_enabled': enabled})
    if enabled:
        auto_trader.start()
    else:
        auto_trader.stop()
    return jsonify({'success': True, 'enabled': enabled})


def open_browser():
    time.sleep(1.2)
    webbrowser.open('http://localhost:5175')


if __name__ == '__main__':
    from waitress import serve
    t = threading.Thread(target=open_browser, daemon=True)
    t.start()
    serve(app, host='0.0.0.0', port=5175, ident=None)
