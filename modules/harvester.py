import numpy as np
import requests
from modules.market_data import MarketData
import threading
import time
import json
import os
from datetime import datetime, timezone, timedelta
from modules.signals import SignalAggregator
from modules.io_safe import atomic_write_json
from modules.predictor import Predictor
from modules.tx_cost import transaction_cost_pct
from modules.decision_log import DecisionRecorder
from modules.adaptive_policy import AdaptivePolicy
from modules.trade_record import TradeRecorder
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo('America/New_York')
except ImportError:
    _ET = None   # fallback: UTC-4 approximation used below


def _now_et():
    """Current time in US/Eastern (falls back to naive local if zoneinfo missing)."""
    return datetime.now(_ET) if _ET else datetime.now()

HARVEST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'harvests.json')

from modules.universe import FALLBACK as TOP_PENNY_UNIVERSE

# Sessions where new buys are allowed; DEAD_ZONE and OVERNIGHT are skipped
_BUY_SESSIONS = {'OPEN_MOMENTUM', 'STANDARD', 'AFTERNOON', 'CLOSE'}
# Sessions when the regular market is OPEN (9:30-16:00 ET) — the only time it's safe to TRANSACT
# exits. Includes DEAD_ZONE (open, just no new buys). Outside these (PRE_MARKET/OVERNIGHT/HOLIDAY)
# penny prices are stale and fills unreliable, so exits are deferred to the next open.
_MARKET_OPEN_SESSIONS = {'OPEN_MOMENTUM', 'STANDARD', 'DEAD_ZONE', 'AFTERNOON', 'CLOSE'}


def _pdt_window_dates(today) -> set:
    """Return set of 'YYYY-MM-DD' strings for the last 5 Mon–Fri business days (including today)."""
    result = set()
    d = today
    while len(result) < 5:
        if d.weekday() < 5:
            result.add(d.strftime('%Y-%m-%d'))
        d -= timedelta(days=1)
    return result


def _get_session():
    """Return current ET trading session name. Returns 'HOLIDAY' on weekends/NYSE holidays."""
    now_et = datetime.now(_ET) if _ET else datetime.now(timezone.utc) + timedelta(hours=-4)
    try:
        from modules.scheduler import _is_trading_day
        if not _is_trading_day(now_et):
            return 'HOLIDAY'
    except Exception:
        if now_et.weekday() >= 5:   # weekend fallback if scheduler import fails
            return 'HOLIDAY'
    m = now_et.hour * 60 + now_et.minute
    if m < 4 * 60:        return 'OVERNIGHT'
    if m < 9 * 60 + 30:   return 'PRE_MARKET'
    if m < 10 * 60 + 30:  return 'OPEN_MOMENTUM'
    if m < 12 * 60:       return 'STANDARD'
    if m < 14 * 60:       return 'DEAD_ZONE'
    if m < 15 * 60 + 30:  return 'AFTERNOON'
    if m < 16 * 60:       return 'CLOSE'
    return 'OVERNIGHT'

class HarvestPosition:
    def __init__(self, symbol, shares, entry_price, capital):
        self.symbol = symbol
        self.shares = shares
        self.entry_price = entry_price
        self.capital = capital          # total capital deployed
        self.harvests_done = 0
        self.total_harvested = 0.0
        self.started = datetime.now().isoformat()
        self.status = 'active'          # active | harvesting | exiting | closed
        self.log = []

    def to_dict(self):
        return {
            'symbol': self.symbol,
            'shares': self.shares,
            'entry_price': self.entry_price,
            'capital': self.capital,
            'harvests_done': self.harvests_done,
            'total_harvested': round(self.total_harvested, 2),
            'started': self.started,
            'status': self.status,
            'log': self.log[-20:],
        }


class StockHarvester:
    def __init__(self, paper_trader, config, universe=None):
        self.paper_trader = paper_trader
        self.config = config
        self.universe = universe   # DynamicUniverse — optional
        self.positions = {}      # symbol -> HarvestPosition
        self.log = []
        self.running = False
        self._thread = None
        self._lock = threading.Lock()
        self.signals = SignalAggregator(config)
        self._md     = MarketData(config)
        self._float_cache = {}   # symbol -> (float_shares, timestamp)
        self._load()

    def _get_today_date(self):
        """Overrideable by replay harness to inject replay date instead of real wall date."""
        return datetime.now().date()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self):
        try:
            if os.path.exists(HARVEST_FILE):
                with open(HARVEST_FILE) as f:
                    data = json.load(f)
                self.log = data.get('log', [])
        except Exception:
            pass

    def _save(self):
        try:
            data = {
                'positions': {s: p.to_dict() for s, p in self.positions.items()},
                'log': self.log[-200:],
            }
            with open(HARVEST_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    # ── Top 50 Mover Scan ─────────────────────────────────────────────────────

    def _poly_snapshot_filter(self, symbols):
        """
        Single Polygon REST call to pre-filter universe by price + dollar volume.
        Reduces 5-min bar fetches from ~80 to ~25 symbols, staying under free-tier rate limits.
        Returns filtered list (or original list if Polygon key unavailable).
        """
        key = self._md._key
        if not key:
            return symbols
        try:
            url = ('https://api.polygon.io/v2/snapshot/locale/us/markets/stocks'
                   '?tickers={}&apiKey={}').format(','.join(symbols[:200]), key)
            r = requests.get(url, headers={'User-Agent': 'StopTrading/1.0'}, timeout=12)
            if r.status_code != 200:
                return symbols
            snaps = r.json().get('tickers', [])
            filtered = []
            for s in snaps:
                sym   = s.get('ticker', '')
                day   = s.get('day', {})
                price = float(day.get('c', 0) or
                              s.get('lastTrade', {}).get('p', 0) or
                              s.get('prevDay', {}).get('c', 0) or 0)
                vol   = float(day.get('v', 0))
                if 0.50 <= price < float(self.config.get('max_stock_price', 15.0)) and price * vol >= 100_000:
                    filtered.append(sym)
            return filtered if len(filtered) >= 5 else symbols
        except Exception:
            return symbols

    def scan_movers(self, extra_symbols=None):
        """Fetch top penny stocks from universe, score by movement quality, return ranked list.

        Uses 5-minute bars (period='5d') so signal fires intraday instead of waiting for
        the hourly bar to close. change_1h field is now change-from-today's-open for
        API compatibility.
        """
        universe = list(self.universe.get()) if self.universe else list(TOP_PENNY_UNIVERSE)
        # Always union the curated penny fallback so dynamic-screener drift (into illiquid or
        # out-of-band names) can never starve the scan to zero candidates.
        universe = list(dict.fromkeys(universe + list(TOP_PENNY_UNIVERSE)))
        if extra_symbols:
            universe = list(dict.fromkeys(list(universe) + [s.upper() for s in extra_symbols if s.upper() not in universe]))
        # Pre-filter with Polygon snapshot: 1 API call instead of 80 — avoids free-tier rate limit
        universe = self._poly_snapshot_filter(universe)[:40]
        try:
            data = self._md.download(
                tickers=' '.join(universe),
                period='5d',
                interval='5m',
                group_by='ticker',
                auto_adjust=True,
                progress=False,
                threads=True,
                timeout=30
            )
        except Exception:
            return []

        today_date = self._get_today_date()
        results = []
        for symbol in universe:
            try:
                if len(universe) == 1:
                    hist = data
                else:
                    if symbol not in data.columns.get_level_values(0):
                        continue
                    hist = data[symbol].dropna()

                if hist.empty or len(hist) < 4:
                    continue

                # Split into today's bars vs full history for different metrics
                try:
                    today_bars = hist[hist.index.date == today_date]
                except Exception:
                    today_bars = hist.tail(78)
                if today_bars.empty or len(today_bars) < 2:
                    today_bars = hist.tail(78)

                price = float(today_bars['Close'].iloc[-1])
                if price < 0.50 or price >= 10.0:
                    continue

                # Float filter: micro-caps produce explosive moves; large-float stocks filtered out
                fl = self._float_cache.get(symbol, (0, 0))[0]
                if fl > 0 and fl > 10_000_000:
                    continue

                # change_from_open: compare to today's 9:30 open (more signal than bar-vs-bar)
                open_price = float(today_bars['Close'].iloc[0])
                change_1h = ((price - open_price) / open_price * 100) if open_price > 0 else 0

                # 5-day context: use full history
                open_5d = float(hist['Close'].iloc[0])
                change_5d = ((price - open_5d) / open_5d * 100) if open_5d > 0 else 0

                # Volume on today's 5m bars
                volume = int(today_bars['Volume'].iloc[-1]) if not np.isnan(today_bars['Volume'].iloc[-1]) else 0
                avg_vol_5m = int(today_bars['Volume'].mean())
                total_today_vol = int(today_bars['Volume'].sum())
                # Dollar volume: today's cumulative volume × price ≥ $100K
                if price * total_today_vol < 100_000:
                    continue
                # Higher threshold for stocks above $5 (they require real conviction)
                if price >= 5.0 and price * total_today_vol < 500_000:
                    continue
                # Relative volume — today's CUMULATIVE pace vs the prior-day average at the
                # same point in the session. Stable intraday, unlike last-bar/avg-bar which
                # is ~0 on the current forming bar and in afternoon lulls (that bug made the
                # vol_ratio>2× gate unpassable after the open). >1 = above its normal pace.
                try:
                    by_day = hist.groupby(hist.index.date)['Volume'].sum()
                    prior = by_day[by_day.index != today_date]
                    avg_daily_vol = float(prior.mean()) if len(prior) else float(by_day.mean())
                    elapsed_frac = max(0.05, min(1.0, len(today_bars) / 78.0))   # 78 5-min bars / RTH day
                    expected_by_now = avg_daily_vol * elapsed_frac
                    vol_ratio = (total_today_vol / expected_by_now) if expected_by_now > 0 else 1.0
                except Exception:
                    vol_ratio = volume / avg_vol_5m if avg_vol_5m > 0 else 1.0

                # Momentum slope via linear regression on last 12 five-minute bars
                recent = today_bars['Close'].tail(12).values
                x = np.arange(len(recent))
                slope = np.polyfit(x, recent, 1)[0] if len(recent) >= 3 else 0
                slope_pct = (slope / price * 100) if price > 0 else 0

                # Upside volatility: only reward bars that closed higher (directional momentum)
                tail = today_bars['Close'].tail(20)
                up_bars = tail[tail.diff().fillna(0) > 0]
                upside_vol = float(up_bars.std() / tail.mean() * 100) if len(up_bars) >= 3 else 0

                # Momentum acceleration: consecutive green bars + volume acceleration
                # "Rocket setup" — each green bar with more volume than the last
                recent_c = today_bars['Close'].tail(6).values
                recent_v = today_bars['Volume'].tail(6).values
                green_streak = 0
                for j in range(len(recent_c) - 1, 0, -1):
                    if recent_c[j] > recent_c[j - 1]:
                        green_streak += 1
                    else:
                        break
                vol_accel = (
                    len(recent_v) >= 6 and
                    float(recent_v[-1] + recent_v[-2]) / 2 > float(recent_v[-4] + recent_v[-5]) / 2
                )

                # Float rotation: today's volume as fraction of float
                # >10% = elevated conviction; >100% = float fully turned over (potential exhaustion)
                float_rotation = total_today_vol / fl if fl > 0 else 0.0

                # Base technical score
                tech_score = 0
                tech_score += min(max(slope_pct * 10, 0), 35)
                tech_score += min(vol_ratio / 5 * 25, 25)
                tech_score += min(abs(change_1h) / 10 * 20, 20)
                tech_score += min(upside_vol / 5 * 20, 20)

                # Momentum acceleration bonus: up to +20 for 4-bar rocket, +15 for vol accel
                tech_score += min(green_streak * 5, 20)
                if vol_accel and green_streak >= 2:
                    tech_score += 15

                # Float rotation bonus: heavily traded relative to float = institutional attention
                if 0 < float_rotation < 1.5:
                    tech_score += min(float_rotation * 20, 20)
                elif float_rotation >= 1.5:
                    tech_score -= 15   # over-extended — float turned over 1.5×

                results.append({
                    'symbol': symbol,
                    'price': round(price, 4),
                    'change_1h': round(change_1h, 2),
                    'change_5d': round(change_5d, 2),
                    'volume': volume,
                    'vol_ratio': round(vol_ratio, 2),
                    'slope_pct': round(slope_pct, 4),
                    'volatility': round(upside_vol, 2),
                    'green_streak': green_streak,
                    'float_rotation': round(float_rotation, 3),
                    'tech_score': round(min(tech_score, 100), 1),
                    'signal_score': 0,
                    'signal': 'SCANNING',
                    'score': round(min(tech_score, 100), 1),
                    'in_harvest': symbol in self.positions and self.positions[symbol].status == 'active',
                })
            except Exception:
                continue

        # Signal scoring is on-demand (🔍 button) — don't block the scan
        for r in results:
            r['score'] = r['tech_score']

        results.sort(key=lambda x: x['score'], reverse=True)
        return results[:50]

    # ── Harvest Engine ───────────────────────────────────────────────────────

    def start_harvest(self, symbol, capital):
        """Open a harvest position using paper_trader."""
        symbol = symbol.upper().strip()
        with self._lock:
            if symbol in self.positions and self.positions[symbol].status == 'active':
                return {'error': symbol + ' already being harvested'}

            try:
                ticker = self._md.Ticker(symbol)
                price = float(ticker.fast_info.last_price)
                if price <= 0:
                    return {'error': 'Could not get price for ' + symbol}
            except Exception as e:
                return {'error': str(e)}

            shares = int(capital / price)
            if shares < 1:
                return {'error': 'Insufficient capital for 1 share at $' + str(price)}

            result = self.paper_trader.buy(symbol, shares)
            if not result.get('ok'):
                return {'error': result.get('error', 'Buy failed')}

            pos = HarvestPosition(symbol, shares, price, capital)
            pos.log.append(self._entry(symbol, 'OPEN', shares, price, 'Harvest started'))
            self.positions[symbol] = pos
            self._log('OPEN', symbol, shares, price, capital, 'Harvest started')
            self._save()
            return {'ok': True, 'symbol': symbol, 'shares': shares, 'price': price}

    def stop_harvest(self, symbol):
        """Manually close a harvest position."""
        symbol = symbol.upper()
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos or pos.status == 'closed':
                return {'error': 'No active harvest for ' + symbol}
            return self._close_position(pos, 'Manual close')

    def _check_positions(self):
        """Core harvest loop — runs every 60s in background thread."""
        tx_cost_pct = transaction_cost_pct(None, None, None, self.config)   # unified flat estimate
        harvest_trigger = float(self.config.get('harvest_trigger_pct', 10.0))
        exit_trigger = float(self.config.get('exit_trigger_pct', 5.0))

        with self._lock:
            for symbol, pos in list(self.positions.items()):
                if pos.status != 'active':
                    continue
                try:
                    ticker = self._md.Ticker(symbol)
                    price = float(ticker.fast_info.last_price)
                    if price <= 0:
                        continue

                    gain_pct = (price - pos.entry_price) / pos.entry_price * 100
                    net_gain_pct = gain_pct - tx_cost_pct  # subtract round-trip cost

                    # Harvest: profit > tx_cost + harvest_trigger
                    if net_gain_pct >= harvest_trigger:
                        self._do_harvest(pos, price, net_gain_pct)

                    # Exit: margin has compressed to exit_trigger or below
                    elif pos.harvests_done > 0 and net_gain_pct <= exit_trigger:
                        self._close_position(pos, 'Margin compressed to {:.1f}% — exit'.format(net_gain_pct))

                    # Stop loss: -15% — protect capital
                    elif gain_pct <= -15:
                        self._close_position(pos, 'Stop loss hit at {:.1f}%'.format(gain_pct))

                except Exception:
                    continue
            self._save()

    def _do_harvest(self, pos, price, net_gain_pct):
        """Harvest 50% of position profit, reinvest remainder."""
        harvest_shares = max(1, pos.shares // 2)
        result = self.paper_trader.sell(pos.symbol, harvest_shares)
        if not result.get('ok'):
            return

        harvest_value = harvest_shares * price
        pos.total_harvested += harvest_value
        pos.harvests_done += 1
        pos.shares -= harvest_shares

        # Reinvest harvested value: buy more shares
        reinvest_shares = int(harvest_value / price)
        if reinvest_shares >= 1:
            buy_result = self.paper_trader.buy(pos.symbol, reinvest_shares)
            if buy_result.get('ok'):
                pos.shares += reinvest_shares
                note = 'Harvested {} shares @ ${:.4f} ({:.1f}% gain) → reinvested {} shares'.format(
                    harvest_shares, price, net_gain_pct, reinvest_shares)
            else:
                note = 'Harvested {} shares @ ${:.4f} ({:.1f}% gain) — reinvest failed'.format(
                    harvest_shares, price, net_gain_pct)
        else:
            note = 'Harvested {} shares @ ${:.4f} ({:.1f}% gain)'.format(harvest_shares, price, net_gain_pct)

        pos.entry_price = price  # reset basis to current price
        pos.log.append(self._entry(pos.symbol, 'HARVEST', harvest_shares, price, note))
        self._log('HARVEST', pos.symbol, harvest_shares, price, harvest_value, note)

    def _close_position(self, pos, reason):
        result = self.paper_trader.sell(pos.symbol, pos.shares)
        pos.status = 'closed'
        note = reason + ' | Total harvested: ${:.2f}'.format(pos.total_harvested)
        pos.log.append(self._entry(pos.symbol, 'CLOSE', pos.shares, 0, note))
        self._log('CLOSE', pos.symbol, pos.shares, 0, pos.total_harvested, note)
        return {'ok': True, 'reason': reason}

    # ── Background Thread ─────────────────────────────────────────────────────

    def start_monitor(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        threading.Thread(target=self._prefetch_floats, daemon=True).start()

    def stop_monitor(self):
        self.running = False

    def _get_float(self, symbol):
        """Return float shares cached for 4 hours. Returns 0 if unknown."""
        cached = self._float_cache.get(symbol)
        if cached and (time.time() - cached[1]) < 14400:
            return cached[0]
        try:
            info = self._md.Ticker(symbol).info
            fl = int(info.get('floatShares') or info.get('sharesOutstanding') or 0)
            self._float_cache[symbol] = (fl, time.time())
            return fl
        except Exception:
            self._float_cache[symbol] = (0, time.time())
            return 0

    def _prefetch_floats(self):
        """Background: warm float cache for all universe symbols before market open."""
        time.sleep(5)
        while True:
            try:
                symbols = self.universe.get() if self.universe else TOP_PENNY_UNIVERSE
                for sym in symbols:
                    if sym not in self._float_cache or (time.time() - self._float_cache[sym][1]) > 14400:
                        self._get_float(sym)
                        time.sleep(0.4)
            except Exception:
                pass
            time.sleep(4 * 3600)

    def _loop(self):
        while self.running:
            try:
                self._check_positions()
            except Exception:
                pass
            time.sleep(60)

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self):
        state_positions = []
        with self._lock:
            for symbol, pos in self.positions.items():
                try:
                    ticker = self._md.Ticker(symbol)
                    price = float(ticker.fast_info.last_price)
                except Exception:
                    price = pos.entry_price

                gain_pct = (price - pos.entry_price) / pos.entry_price * 100 if pos.entry_price > 0 else 0
                d = pos.to_dict()
                d['current_price'] = round(price, 4)
                d['gain_pct'] = round(gain_pct, 2)
                d['current_value'] = round(pos.shares * price, 2)
                state_positions.append(d)

        return {
            'positions': state_positions,
            'log': self.log[-50:],
            'monitoring': self.running,
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _entry(self, symbol, action, shares, price, note):
        return {
            'time': datetime.now().strftime('%H:%M:%S'),
            'action': action,
            'shares': shares,
            'price': round(price, 4),
            'note': note,
        }

    def _log(self, action, symbol, shares, price, value, note):
        self.log.append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'action': action,
            'symbol': symbol,
            'shares': shares,
            'price': round(price, 4),
            'value': round(value, 2),
            'note': note,
        })


# ── AutoPilot ─────────────────────────────────────────────────────────────────

AUTOPILOT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'autopilot.json')
VAULT_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vault.json')

class AutoPilot:
    """
    Fully autonomous paper trading engine.
    - Scans penny stocks every 5 minutes
    - Buys top candidate if daily spend < $100 limit
    - Harvests at 10% net gain, reinvests
    - Exits when margin drops to <= 5% after at least one harvest
    - Hard stop-loss at -15%
    - All decisions logged with reasoning
    """

    def __init__(self, harvester, paper_trader, config, engine=None, catalyst=None, notifier=None, halts=None, ssr=None, macro=None, behavioral=None, insider=None, earnings=None, congress=None, orb=None, news=None, float_rotation=None, short_squeeze=None, sector=None, ibkr=None, social=None, feed=None):
        self.harvester  = harvester
        self.paper      = paper_trader
        self.config     = config
        self._md        = MarketData(config)
        self.engine     = engine     # RealtimeEngine — optional
        self.catalyst   = catalyst   # CatalystEngine — optional
        self.notifier   = notifier   # Notifier — optional, silently skipped if None
        self.halts      = halts      # HaltMonitor — optional
        self.ssr        = ssr        # SSRMonitor — optional
        self.macro      = macro      # MacroSignalAgent — optional
        self.behavioral = behavioral # BehavioralAgent — optional
        self.insider    = insider    # InsiderTradeAgent — optional
        self.earnings   = earnings   # EarningsGuard — optional
        self.congress   = congress   # CongressAgent — optional
        self.orb          = orb            # ORBAgent — optional
        self.news         = news           # NewsAgent — optional
        self.float_rotation = float_rotation  # FloatRotationAgent — optional
        self.short_squeeze  = short_squeeze   # ShortSqueezeAgent — optional
        self.sector         = sector          # SectorMomentumAgent — optional
        self.ibkr           = ibkr            # IBKRFeed — optional, for real bid-ask spread
        self.social         = social          # SocialFlowAgent — optional crowd-flow signal
        self.feed           = feed            # FeedManager — real-time/delayed/down contract
        self.running   = False
        self._thread = None
        self._lock = threading.Lock()
        self.log = []
        self.daily_spent = 0.0
        # Capital basis freed by sells today. The daily spend gate uses NET deployed
        # (spent − returned) so closing a position recycles its budget within the day
        # instead of permanently consuming it. Fed centrally by _on_paper_sell below.
        self.daily_returned = 0.0
        self.daily_date = _now_et().strftime('%Y-%m-%d')
        # Predictive model — calibrated P(win) for entries + reversal score for exits.
        self.predictor = Predictor(config)
        # Adaptive policy — reads the banked barrier outcomes and throttles live exposure
        # (size) up/down vs the breakeven win rate. Real-time risk/profitability governor.
        self.adaptive = AdaptivePolicy(config)
        # Decision Record — captures the full per-cycle decision context (counterfactual
        # candidates + per-agent attribution + regime) for future learning paradigms.
        self.decision_log = DecisionRecorder(self)
        # Immutable trade-lifecycle ledger (trades.jsonl) — full entry→harvests→exit record.
        self.trade_record = TradeRecorder(self)
        self._predict_realized = {}   # symbol -> cumulative realized P&L this position (for labels)
        # Central hook so EVERY exit path (harvest, stop, EOD, stream) recycles budget + records.
        try:
            self.paper.register_callback(self._on_paper_sell)
            self.paper.register_callback(self.trade_record.on_trade)
        except Exception:
            pass
        self.stats = {
            'total_trades': 0,
            'total_harvests': 0,
            'total_profit': 0.0,
            'started': None,
            'wins': 0,
            'losses': 0,
            'total_win_pct': 0.0,
            'total_loss_pct': 0.0,
        }
        self._position_harvests   = {}   # symbol -> harvest count for this position
        self._position_tx_cost    = {}   # symbol -> spread-aware round-trip cost % captured at entry
        self._harvest_level       = {}   # symbol -> current armed harvest trigger %% (ratchets up on sustained incline)
        self._last_time_harvest   = {}   # symbol -> epoch ts of last time-based harvest slice
        self._entry_blacklist     = {}   # symbol -> epoch ts; skip entries (bad price / repeated buy failure)
        self._position_hwm        = {}   # symbol -> high-water mark price (trailing stop)
        self._position_opened     = {}   # symbol -> datetime when position was opened
        self._consecutive_losses  = 0    # straight losses since last winning exit
        self._daily_start_balance = 0.0  # balance at start of current trading day
        self._daily_pnl_floor_hit = False # True once daily loss limit is breached
        self._gapped_symbols      = set() # symbols gap-checked today ('SYM_YYYY-MM-DD')
        self._gap_date            = ''    # date string for _gapped_symbols reset
        self._position_entry_rvol = {}   # symbol -> RVOL at entry (volume exhaustion reference)
        self._momentum_cache      = {}   # 'SYM_HH:MM' -> (slope, cascade, rvol) per-minute cache
        # PDT tracking: accounts under $25k limited to 3 day trades per rolling 5-business-day window
        self._day_trades_log      = []   # list of 'YYYY-MM-DD' strings when a day trade completed
        self._pdt_last_log        = 0.0  # epoch — suppress duplicate PDT log spam (log at most once/30m)
        self._session_last_log    = 0.0  # epoch — suppress SESSION/SKIP spam (log at most once/30m)
        self._halt_notify_ts      = 0.0  # epoch — suppress consecutive-loss halt spam (once/30m)
        self._cascade_strikes     = {}   # symbol -> (count, last_epoch) — drop after 5 cascade skips
        # Profit vault — 50% of each harvest gain is quarantined here, never reinvested
        self._vault_balance  = 0.0   # total accumulated, not yet withdrawn
        self._vault_entries  = []    # [{ts, symbol, gross_profit, vault_amount, harvest_num}]
        self._vault_withdrawn = 0.0  # lifetime total marked as taken out
        # ORB stream fast-buy: track which symbols have already fired today
        self._orb_fired    = set()       # symbols where stream ORB-buy fired this session
        self._orb_buy_lock = threading.Lock()  # prevents concurrent ORB buys
        # Real-time architecture
        self._stream_lock         = threading.Lock()  # serialises stream-triggered sells/harvests
        self._stats_lock          = threading.Lock()  # guards stats/consecutive-loss from stream race
        self._premarket_watchlist = []   # [{symbol, gap_pct, ...}] built pre-open, used at OPEN
        self._premarket_date      = ''   # date string — prevents re-scanning same day
        self._load()

    def _load(self):
        try:
            if os.path.exists(AUTOPILOT_FILE):
                with open(AUTOPILOT_FILE) as f:
                    d = json.load(f)
                self.log = d.get('log', [])
                loaded = d.get('stats', {})
                for k, v in self.stats.items():
                    self.stats[k] = loaded.get(k, v)
                saved_date = d.get('daily_date', '')
                if saved_date == _now_et().strftime('%Y-%m-%d'):
                    self.daily_spent          = d.get('daily_spent', 0.0)
                    self.daily_returned       = d.get('daily_returned', 0.0)
                    self.daily_date           = saved_date
                    self._daily_start_balance = d.get('daily_start_balance', 0.0)
                    self._daily_pnl_floor_hit = d.get('daily_pnl_floor_hit', False)
                self._consecutive_losses = d.get('consecutive_losses', 0)
                self._day_trades_log     = d.get('day_trades_log', [])
        except Exception:
            pass
        try:
            if os.path.exists(VAULT_FILE):
                with open(VAULT_FILE) as f:
                    v = json.load(f)
                self._vault_balance   = float(v.get('balance', 0.0))
                self._vault_entries   = v.get('entries', [])
                self._vault_withdrawn = float(v.get('withdrawn', 0.0))
        except Exception:
            pass

    def _save(self):
        try:
            atomic_write_json(AUTOPILOT_FILE, {
                'log':                 self.log[-1000:],
                'stats':               self.stats,
                'daily_spent':         self.daily_spent,
                'daily_returned':      self.daily_returned,
                'daily_date':          self.daily_date,
                'running':             self.running,
                'consecutive_losses':  self._consecutive_losses,
                'daily_start_balance': self._daily_start_balance,
                'daily_pnl_floor_hit': self._daily_pnl_floor_hit,
                'day_trades_log':      self._day_trades_log,
            })
        except Exception:
            pass
        try:
            atomic_write_json(VAULT_FILE, {
                'balance':   round(self._vault_balance, 4),
                'withdrawn': round(self._vault_withdrawn, 4),
                'entries':   self._vault_entries[-500:],
            })
        except Exception:
            pass

    # ── Profit Vault ─────────────────────────────────────────────────────────

    def _vault_deposit(self, symbol: str, gross_profit: float, harvest_num: int):
        """Move 50% of net gain into vault — deducted from paper cash, never reinvested."""
        if gross_profit <= 0:
            return
        amount = round(gross_profit * 0.50, 4)
        if getattr(self, '_vault_deduct_now', True):
            ok = self.paper.vault_deposit(amount)
            if not ok:
                return
        self._vault_balance += amount
        self._vault_entries.append({
            'ts':          datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'symbol':      symbol,
            'gross_profit': round(gross_profit, 4),
            'vault_amount': amount,
            'harvest_num': harvest_num,
        })
        self._entry('VAULT', '{} — harvest #{}: deposited ${:.2f} (50% of ${:.2f} gain) | vault total ${:.2f}'.format(
            symbol, harvest_num, amount, gross_profit, self._vault_balance))

    def get_vault_status(self) -> dict:
        """Return vault balance, weekly breakdown, and full entry log."""
        from datetime import date as _date
        weekly = {}
        for e in self._vault_entries:
            try:
                d = _date.fromisoformat(e['ts'][:10])
                # ISO week key: "YYYY-Www"
                week_key = d.strftime('%Y-W%W')
                weekly[week_key] = round(weekly.get(week_key, 0.0) + e['vault_amount'], 4)
            except Exception:
                pass
        return {
            'vault_balance':     round(self._vault_balance, 2),
            'total_deposited':   round(sum(e['vault_amount'] for e in self._vault_entries), 2),
            'total_withdrawn':   round(self._vault_withdrawn, 2),
            'weekly_summary':    weekly,
            'entries':           self._vault_entries[-100:],
        }

    def vault_transfer_to_trading(self, amount: float = None) -> dict:
        """Move vault funds back into the paper trading balance. Defaults to full balance."""
        amount = round(float(amount or self._vault_balance), 4)
        if amount <= 0:
            return {'error': 'No vault balance to transfer'}
        if amount > self._vault_balance:
            return {'error': 'Amount exceeds vault balance ${:.2f}'.format(self._vault_balance)}
        self._vault_balance -= amount
        self.paper.vault_return(amount)
        self._vault_entries.append({
            'ts':          datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'symbol':      'TRANSFER',
            'gross_profit': 0,
            'vault_amount': -amount,
            'harvest_num': 0,
        })
        self._save()
        return {'ok': True, 'transferred': amount, 'vault_remaining': round(self._vault_balance, 2)}

    def vault_withdraw(self, amount: float = None) -> dict:
        """Mark vault funds as withdrawn (taken as profit, not returned to trading)."""
        amount = round(float(amount or self._vault_balance), 4)
        if amount <= 0:
            return {'error': 'No vault balance to withdraw'}
        if amount > self._vault_balance:
            return {'error': 'Amount exceeds vault balance ${:.2f}'.format(self._vault_balance)}
        self._vault_balance   -= amount
        self._vault_withdrawn += amount
        self._vault_entries.append({
            'ts':          datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'symbol':      'WITHDRAW',
            'gross_profit': 0,
            'vault_amount': -amount,
            'harvest_num': 0,
        })
        self._save()
        return {'ok': True, 'withdrawn': amount, 'vault_remaining': round(self._vault_balance, 2),
                'total_withdrawn_lifetime': round(self._vault_withdrawn, 2)}

    def resume(self):
        """Resume thread after a service restart without resetting the paper account."""
        if self.running:
            return {'error': 'AutoPilot already running'}
        self.running = True
        # Register stream price callback — MUST happen here too, not just in start(),
        # otherwise an auto-resumed bot has no event-driven sub-minute stop-loss.
        self._register_stream_callback()
        self._entry('SYSTEM', 'AutoPilot resumed after service restart — continuing from saved state')
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return {'ok': True}

    def _register_stream_callback(self):
        """Idempotently register the stream price callback for event-driven exits."""
        if getattr(self, '_stream_cb_registered', False):
            return
        if self.engine and hasattr(self.engine, 'cache'):
            self.engine.cache.register_price_callback(self._on_stream_price)
            self._stream_cb_registered = True

    def _alert_stale_price(self, symbol, shares, entry):
        """Throttled warning (once / 5 min per symbol) when a HELD position has no live
        price — the stop-loss is effectively blind until a price returns."""
        alerts = getattr(self, '_stale_price_alerts', None)
        if alerts is None:
            alerts = self._stale_price_alerts = {}
        now = datetime.now()
        last = alerts.get(symbol)
        if last and (now - last).total_seconds() < 300:
            return
        alerts[symbol] = now
        note = '{} — NO LIVE PRICE for held position ({} sh @ ${:.4f}); stop-loss is BLIND until feed returns'.format(
            symbol, shares, entry)
        self._entry('STALE-PRICE', note)
        try:
            self._notify('⚠️ ' + note, 'alert')
        except Exception:
            pass

    def _on_paper_sell(self, event):
        """Central hook on every paper SELL — recycle the freed capital basis back into
        today's spend budget so a closed position frees room for new entries. Bounded so
        net deployed never goes below zero (selling a prior-day position can't inflate
        today's budget past what was actually spent today)."""
        try:
            if event.get('type') != 'SELL':
                return
            freed = float(event.get('shares', 0)) * float(event.get('avg_cost', 0))
            # Lock-free on purpose: this fires synchronously inside paper.sell(), which can
            # be called while self._lock is held — acquiring it here would deadlock. A soft
            # budget counter doesn't need strict locking; the GIL keeps the write safe.
            if freed > 0:
                self.daily_returned = min(self.daily_spent, getattr(self, 'daily_returned', 0.0) + freed)
            # NOTE: entry labeling is handled by the triple-barrier watch (update_barriers),
            # NOT here — labels track the price path, not the bot's exit timing.
            # Open an EXIT-outcome watch: was leaving here good (price fell after) or early
            # (price kept rising)? Feeds the nightly exit-threshold tuner.
            try:
                sym = event.get('symbol')
                px  = float(event.get('price', 0.0)); avg = float(event.get('avg_cost', 0.0))
                if sym and px > 0 and avg > 0:
                    gain_pct = (px - avg) / avg * 100.0
                    vel = self.engine.cache.velocity.get(sym, {}) if (self.engine and hasattr(self.engine, 'cache')) else {}
                    opened = self._position_opened.get(sym)
                    mins = ((datetime.now() - opened).total_seconds() / 60.0) if opened else 0.0
                    rs = self.predictor.reversal_score(vel, gain_pct, mins)
                    self.predictor.open_exit_watch(sym, rs, gain_pct, mins, 'exit', px,
                                                   self._get_et_now().timestamp())
            except Exception:
                pass
        except Exception:
            pass

    def _net_deployed_today(self):
        """Capital deployed from today's budget, net of positions already closed today."""
        return max(0.0, self.daily_spent - getattr(self, 'daily_returned', 0.0))

    def start(self, starting_balance=500.0):
        if self.running:
            return {'error': 'AutoPilot already running'}
        # Reset paper account to starting balance
        with self.paper._lock:
            self.paper._state['balance'] = starting_balance
            self.paper._state['positions'] = {}
            self.paper._state['history'] = []
            self.paper._save()
        self.daily_spent = 0.0
        self.daily_returned = 0.0
        self.daily_date = _now_et().strftime('%Y-%m-%d')
        self.stats['started'] = datetime.now().isoformat()
        self.stats['total_trades'] = 0
        self.stats['total_harvests'] = 0
        self.stats['total_profit'] = 0.0
        self.log = []
        self._position_harvests   = {}
        self._position_hwm        = {}
        self._position_opened     = {}
        self._position_entry_rvol = {}
        self._momentum_cache      = {}
        state = self.paper.get_state()
        self._daily_start_balance = state.get('balance', starting_balance)
        self._consecutive_losses  = 0
        self._daily_pnl_floor_hit = False
        self._gapped_symbols      = set()
        self._gap_date            = _now_et().strftime('%Y-%m-%d')
        self._premarket_watchlist = []
        self._premarket_date      = ''
        self._entry('SYSTEM', 'AutoPilot started — $%.2f paper balance' % starting_balance)
        self.running = True
        # Register stream price callback for event-driven stop-loss
        self._register_stream_callback()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._save()
        return {'ok': True}

    def stop(self):
        self.running = False
        self._entry('SYSTEM', 'AutoPilot stopped by user')
        self._save()
        return {'ok': True}

    def _loop(self):
        while self.running:
            try:
                self._tick()
            except Exception as e:
                self._entry('ERROR', str(e))
            self._save()
            time.sleep(60)  # 1-minute polling; stream callbacks handle sub-minute stops

    def _tick(self):
        today = _now_et().strftime('%Y-%m-%d')
        if today != self.daily_date:
            self.daily_date           = today
            self.daily_spent          = 0.0
            self.daily_returned       = 0.0
            self._daily_pnl_floor_hit = False
            self._consecutive_losses  = 0
            self._gapped_symbols      = set()
            self._gap_date            = today
            self._orb_fired           = set()    # reset ORB fast-buy history each day
            self._cascade_strikes     = {}       # reset cascade cooloff each day
            bal = self.paper.get_state().get('balance', 0)
            self._daily_start_balance = bal
            self._entry('SYSTEM', 'New trading day — circuit breakers reset, starting balance ${:.2f}'.format(bal))

        # Seed daily_start_balance on the very first tick after resume/start
        if self._daily_start_balance == 0.0:
            self._daily_start_balance = self.paper.get_state().get('balance', 0)

        # Time-of-day session gate
        session = _get_session()
        if session == 'HOLIDAY':
            self._entry('HOLIDAY', 'NYSE market closed (holiday/weekend) — no trading today')
            return   # prices unavailable; skip all monitoring
        elif session in ('DEAD_ZONE', 'OVERNIGHT'):
            now_ts = time.time()
            if now_ts - self._session_last_log >= 1800:
                msg = ('DEAD_ZONE (12:00-14:00 ET) — monitoring positions, no new buys'
                       if session == 'DEAD_ZONE' else
                       'OVERNIGHT — monitoring positions only, no new buys')
                self._entry('SESSION', msg)
                self._session_last_log = now_ts

        # Pre-market gapper scan — run once per day at PRE_MARKET to build OPEN watchlist
        today = _now_et().strftime('%Y-%m-%d')
        if session == 'PRE_MARKET' and self._premarket_date != today:
            if hasattr(self.harvester, 'universe') and self.harvester.universe:
                try:
                    gappers = self.harvester.universe.get_premarket_gappers(
                        min_gap_pct=8.0, md=self.harvester._md)
                    if gappers:
                        self._premarket_watchlist = gappers
                        self._premarket_date = today
                        syms = ', '.join(g['symbol'] for g in gappers[:6])
                        self._entry('PRE-MARKET',
                            'Gapper scan: {} tickers (top: {})'.format(len(gappers), syms))
                except Exception:
                    pass

        # EOD forced-close — exit all open positions at start of CLOSE session
        # Triggers on: force_eod_close (every night) OR weekend_close (Friday only)
        _is_friday = (datetime.now(_ET) if _ET else datetime.now()).weekday() == 4
        _do_eod_close = (
            self.config.get('force_eod_close', False) or
            (self.config.get('weekend_close', True) and _is_friday)
        )
        if session == 'CLOSE' and _do_eod_close:
            state = self.paper.get_state()
            for pos in state.get('positions', []):
                sym    = pos['symbol']
                shrs   = int(pos['shares'])
                if shrs <= 0:
                    continue
                try:
                    eod_price = self._get_live_price(sym)
                    if eod_price <= 0:
                        continue
                    result = self.paper.sell(sym, shrs, price_override=eod_price)
                    if result.get('ok'):
                        eod_entry  = float(pos['avg_cost'])
                        eod_profit = self._net_profit(shrs, eod_price, eod_entry)
                        self.stats['total_profit'] += eod_profit
                        self._cleanup_position(sym)
                        eod_pct = (eod_price - eod_entry) / eod_entry * 100 if eod_entry > 0 else 0
                        if eod_profit >= 0:
                            self.stats['wins'] += 1
                            self.stats['total_win_pct'] += eod_pct
                            self._consecutive_losses = 0
                        else:
                            self.stats['losses'] += 1
                            self.stats['total_loss_pct'] += abs(eod_pct)
                            self._consecutive_losses += 1
                        _close_reason = 'weekend close — no overnight hold' if (_is_friday and not self.config.get('force_eod_close', False)) else 'forced end-of-day close'
                        self._entry('EOD-CLOSE',
                            '{} — {} | {} shares @ ${:.4f} | net P&L ${:.2f}'.format(
                                sym, _close_reason, shrs, eod_price, eod_profit))
                        from modules.notify import msg_exit
                        self._notify(msg_exit('EOD-CLOSE', sym, _close_reason, eod_profit))
                except Exception as e:
                    self._entry('ERROR', '{} EOD-CLOSE failed: {}'.format(sym, str(e)))
            return   # skip remaining tick after EOD close

        # Parameters (tx cost is now per-position + spread-aware; see self._tx_cost in the loop)
        harvest_trig = float(self.config.get('harvest_trigger_pct', 7.0))
        exit_trig    = float(self.config.get('exit_trigger_pct', 2.0))
        balance      = self.paper.get_state().get('balance', 0)
        daily_pct    = float(self.config.get('daily_spend_pct', 0))
        daily_limit  = (balance * daily_pct / 100) if daily_pct > 0 else float(self.config.get('daily_spend_limit', 100.0))

        # ── Step 1: Monitor existing positions ──────────────────────────────
        # Flush per-minute momentum cache at start of each tick
        self._momentum_cache = {}

        # Advance triple-barrier watches every tick (resolves matured setups → trains the
        # model on the price PATH, independent of whether the bot still holds the name).
        try:
            _bts = self._get_et_now().timestamp()
            self.predictor.update_barriers(self._get_live_price, _bts)
            self.predictor.update_exit_watches(self._get_live_price, _bts)
            self.decision_log.update(self._get_live_price, _bts)
            self.trade_record.flush_closed()   # finalize closed trades (exit reason now logged)
        except Exception:
            pass

        state = self.paper.get_state()
        # Market-hours exit gate: only TRANSACT exits while the regular market is open. Outside
        # 9:30-16:00 ET penny prices are stale and fills unreliable, so positions are HELD and
        # exits deferred to the next open (barriers/labels above still update — they don't trade).
        _exits_allowed = (session in _MARKET_OPEN_SESSIONS
                          or not bool(self.config.get('market_hours_only_exits', True)))
        if not _exits_allowed and state.get('positions'):
            _nts = time.time()
            if _nts - getattr(self, '_ahx_last_log', 0) >= 1800:
                self._entry('SESSION', 'Outside market hours — holding {} position(s); exits deferred '
                            'to next open (stale-price protection)'.format(len(state.get('positions', []))))
                self._ahx_last_log = _nts
        for pos in state.get('positions', []):
            symbol = pos['symbol']
            if not _exits_allowed:
                continue   # hold; never transact an exit on a stale after-hours price
            try:
                price = self._get_live_price(symbol)
                entry = float(pos['avg_cost'])
                shares = int(pos['shares'])
                if entry <= 0 or shares <= 0:
                    continue
                if price <= 0:
                    # No live price (halt / dead feed). We can't safely transact on a
                    # bad price, but silently skipping leaves the stop-loss BLIND — so
                    # raise a throttled alert so a human can intervene, then retry next tick.
                    self._alert_stale_price(symbol, shares, entry)
                    continue

                gain_pct = (price - entry) / entry * 100
                tx_cost_pct = self._tx_cost(symbol)   # spread-aware, locked in at entry
                net_gain = gain_pct - tx_cost_pct
                harvests_done = self._position_harvests.get(symbol, 0)

                # Update high-water mark and open timestamp
                hwm = max(price, self._position_hwm.get(symbol, price))
                self._position_hwm[symbol] = hwm
                if symbol not in self._position_opened:
                    self._position_opened[symbol] = datetime.now()
                drop_from_hwm = (hwm - price) / hwm * 100 if hwm > 0 else 0
                days_open = self._business_days_open(self._position_opened[symbol])

                # ── EARNINGS-EXIT: force exit before earnings report ───────────
                if self.earnings and self.earnings.should_force_exit(symbol):
                    result = self.paper.sell(symbol, shares, price_override=price)
                    if result.get('ok'):
                        profit = self._net_profit(shares, price, entry)
                        # Stats mutated ONLY after confirmed sell
                        self.stats['total_profit'] += profit
                        self._cleanup_position(symbol)
                        if profit >= 0:
                            self.stats['wins'] += 1
                            self.stats['total_win_pct'] += gain_pct
                            self._consecutive_losses = 0
                        else:
                            self.stats['losses'] += 1
                            self.stats['total_loss_pct'] += abs(gain_pct)
                            self._consecutive_losses += 1
                        note = '{} — EARNINGS-EXIT: report due tomorrow | {} shares @ ${:.4f} | net P&L ${:.2f}'.format(
                            symbol, shares, price, profit)
                        self._entry('EARNINGS-EXIT', note)
                        from modules.notify import msg_exit
                        self._notify(msg_exit('EARNINGS-EXIT', symbol, 'earnings due tomorrow', profit), 'alert')
                    else:
                        self._entry('ERROR', '{} EARNINGS-EXIT sell failed: {}'.format(
                            symbol, result.get('error', 'unknown')))
                    continue

                # ── GAP-EXIT: overnight gap down >10% ──────────────────────────
                if self._check_overnight_gap(symbol):
                    result = self.paper.sell(symbol, shares, price_override=price)
                    if result.get('ok'):
                        loss = self._net_profit(shares, price, entry)
                        self.stats['total_profit'] += loss
                        self._cleanup_position(symbol)
                        self.stats['losses'] += 1
                        self.stats['total_loss_pct'] += abs(gain_pct)
                        self._consecutive_losses += 1
                        note = '{} — gapped down >10% from prior close | sold ALL {} shares @ ${:.4f} | net P&L ${:.2f}'.format(
                            symbol, shares, price, loss)
                        self._entry('GAP-EXIT', note)
                        from modules.notify import msg_exit
                        self._notify(msg_exit('GAP-EXIT', symbol, 'Overnight gap >10%', loss), 'alert')
                    else:
                        self._entry('ERROR', '{} GAP-EXIT sell failed: {}'.format(
                            symbol, result.get('error', 'unknown')))
                    continue

                # ── PREDICTIVE-EXIT: bank the gain when live order flow says reversal ──────
                # Conservative: only fires when already in profit, so it protects winners
                # (takes profit early on exhaustion) rather than forcing losses.
                if self.config.get('predictor_exit', True) and net_gain > tx_cost_pct:
                    _vel = self.engine.cache.velocity.get(symbol, {}) if (self.engine and hasattr(self.engine, 'cache')) else {}
                    _mins = (datetime.now() - self._position_opened.get(symbol, datetime.now())).total_seconds() / 60.0
                    _rev = self.predictor.reversal_score(_vel, net_gain, _mins)
                    # Crowd-exhaustion boost: loud buzz rolling over often marks the top.
                    if self.social and self.config.get('social_flow_enabled', True):
                        try:
                            _rev = min(1.0, _rev + 0.6 * self.social.get_exhaustion(symbol))
                        except Exception:
                            pass
                    if _rev >= float(self.config.get('predictor_exit_thresh', 0.55)):
                        result = self.paper.sell(symbol, shares, price_override=price)
                        if result.get('ok'):
                            profit = self._net_profit(shares, price, entry)
                            self.stats['total_profit'] += profit
                            self._cleanup_position(symbol)
                            if profit >= 0:
                                self.stats['wins'] += 1
                                self.stats['total_win_pct'] += gain_pct
                                self._consecutive_losses = 0
                            else:
                                self.stats['losses'] += 1
                                self.stats['total_loss_pct'] += abs(gain_pct)
                                self._consecutive_losses += 1
                            note = '{} — PREDICT-EXIT: reversal score {:.2f} | sold {} @ ${:.4f} | net {:.1f}% | P&L ${:.2f}'.format(
                                symbol, _rev, shares, price, net_gain, profit)
                            self._entry('PREDICT-EXIT', note)
                            from modules.notify import msg_exit
                            self._notify(msg_exit('PREDICT-EXIT', symbol, 'order-flow reversal', profit))
                        continue

                # ── FINAL-EXIT oracle: full exit when multiple signals say rally is over ──
                should_exit, exit_reason = self._should_exit_permanently(
                    symbol, price, net_gain, days_open, harvests_done, session)
                if should_exit and net_gain > tx_cost_pct:
                    result = self.paper.sell(symbol, shares, price_override=price)
                    if result.get('ok'):
                        profit = self._net_profit(shares, price, entry)
                        self.stats['total_profit'] += profit
                        self._cleanup_position(symbol)
                        if profit >= 0:
                            self.stats['wins'] += 1
                            self.stats['total_win_pct'] += gain_pct
                            self._consecutive_losses = 0
                        else:
                            self.stats['losses'] += 1
                            self.stats['total_loss_pct'] += abs(gain_pct)
                            self._consecutive_losses += 1
                        note = '{} — FINAL-EXIT: {} | sold ALL {} shares @ ${:.4f} | net P&L ${:.2f}'.format(
                            symbol, exit_reason, shares, price, profit)
                        self._entry('FINAL-EXIT', note)
                        from modules.notify import msg_exit
                        self._notify(msg_exit('FINAL-EXIT', symbol, exit_reason, profit))
                    continue

                # ── STOP-LOSS: hard cap on single-trade loss before trail fires ────────
                max_single_loss = float(self.config.get('max_single_loss_pct', 8.0))
                if net_gain <= -max_single_loss:
                    result = self.paper.sell(symbol, shares, price_override=price)
                    if result.get('ok'):
                        loss = self._net_profit(shares, price, entry)
                        self.stats['total_profit'] += loss
                        self._cleanup_position(symbol)
                        self.stats['losses'] += 1
                        self.stats['total_loss_pct'] += abs(gain_pct)
                        self._consecutive_losses += 1
                        note = '{} — STOP-LOSS: net {:.1f}% ≤ -{:.0f}% threshold | sold ALL {} shares @ ${:.4f} | P&L ${:.2f}'.format(
                            symbol, net_gain, max_single_loss, shares, price, loss)
                        self._entry('STOP-LOSS', note)
                        from modules.notify import msg_exit
                        self._notify(msg_exit('STOP-LOSS', symbol,
                            'loss {:.1f}% exceeds -{:.0f}% limit'.format(net_gain, max_single_loss), loss), 'alert')
                    continue

                # ── EVASIVE: cascade sell detected — shed 75% to protect gains ──────────
                _, cascade, _ = self._momentum_snapshot(symbol)
                if cascade and net_gain > tx_cost_pct:
                    evasive_shares = max(1, int(shares * 0.75))
                    result = self.paper.sell(symbol, evasive_shares, price_override=price)
                    if result.get('ok'):
                        protected = self._net_profit(evasive_shares, price, entry)
                        self.stats['total_profit'] += protected
                        self._position_harvests[symbol] = harvests_done + 1
                        if protected >= 0:
                            self.stats['wins'] += 1
                            self._consecutive_losses = 0
                        else:
                            self.stats['losses'] += 1
                            self._consecutive_losses += 1
                        note = '{} — cascade sell detected | shed {} shares (75%) @ ${:.4f} | ' \
                               'protected ${:.2f} | {} shares remain'.format(
                                   symbol, evasive_shares, price, protected, shares - evasive_shares)
                        self._entry('EVASIVE', note)
                        from modules.notify import msg_evasive
                        self._notify(msg_evasive(symbol, evasive_shares, price, protected), 'alert')
                    continue   # re-evaluate the remainder next cycle

                # ── Adaptive incline-aware harvest planner (post-purchase monitor) ─────
                mins_held = (datetime.now() - self._position_opened.get(symbol, datetime.now())).total_seconds() / 60.0
                if self.config.get('adaptive_harvest_enabled', True):
                    plan = self._plan_harvest(symbol, net_gain, harvests_done, days_open, session, mins_held)
                else:
                    slope_now, _, _ = self._momentum_snapshot(symbol)
                    eff = (min(2.0, harvest_trig) if harvests_done == 0 and slope_now > 0.5 else harvest_trig)
                    plan = ({'action': 'harvest', 'ratchet_to': None, 'reason': 'fixed trigger',
                             'fraction': self._harvest_fraction(symbol, days_open, harvests_done, session)}
                            if net_gain >= eff else {'action': 'hold', 'fraction': 0.0, 'ratchet_to': None})

                if plan['action'] in ('harvest', 'time_harvest'):
                    frac           = plan['fraction']
                    harvest_shares = max(1, int(shares * frac))
                    result = self.paper.sell(symbol, harvest_shares, price_override=price)
                    if result.get('ok'):
                        profit = self._net_profit(harvest_shares, price, entry, symbol)
                        self.stats['total_harvests'] += 1
                        self.stats['total_profit']   += profit
                        self._position_harvests[symbol] = harvests_done + 1
                        if profit >= 0:
                            self.stats['wins'] += 1
                            self._consecutive_losses = 0
                        else:
                            self.stats['losses'] += 1
                            self._consecutive_losses += 1
                        # Sustained incline → ratchet the armed trigger UP (let the runner run);
                        # throttle the time-harvest cadence so it can't fire every tick.
                        if plan.get('ratchet_to') is not None:
                            self._harvest_level[symbol] = plan['ratchet_to']
                        if plan['action'] == 'time_harvest':
                            self._last_time_harvest[symbol] = time.time()
                        tag = 'TIME-HARVEST' if plan['action'] == 'time_harvest' else 'HARVEST'
                        note = '{} — sold {:.0f}% ({} shares) @ ${:.4f} | net {:.1f}% | profit ${:.2f} | {}'.format(
                                   symbol, frac * 100, harvest_shares, price, net_gain, profit, plan.get('reason', ''))
                        self._entry(tag, note)
                        from modules.notify import msg_harvest
                        self._notify(msg_harvest(symbol, frac * 100, net_gain, profit, harvests_done + 1))
                        # Vault: 50% of net gain quarantined as profit — deducted from cash, not reinvested
                        if profit > 0:
                            self._vault_deposit(symbol, profit, harvests_done + 1)
                        # Reinvest moderate price-harvests back into the runner. NEVER reinvest a
                        # time-harvest — its purpose is to recycle capital OUT of a stalled name.
                        reinvest = 0 if (frac >= 0.75 or plan['action'] == 'time_harvest') else harvest_shares
                        if reinvest >= 1:
                            buy = self.paper.buy(symbol, reinvest, price_override=price)
                            if buy.get('ok'):
                                self._entry('REINVEST',
                                    '{} — bought {} shares @ ${:.4f} (reinvested harvest, exempt daily limit)'.format(
                                        symbol, reinvest, price))

                else:
                    # Trailing stop — 5% from HWM on all days (flat, dynamic protection)
                    trail_pct = 5.0
                    if drop_from_hwm >= trail_pct:
                        result = self.paper.sell(symbol, shares, price_override=price)
                        if result.get('ok'):
                            loss = self._net_profit(shares, price, entry)
                            self.stats['total_profit'] += loss
                            self._cleanup_position(symbol)
                            self.stats['losses'] += 1
                            self.stats['total_loss_pct'] += abs(gain_pct)
                            self._consecutive_losses += 1
                            note = '{} — sold ALL {} shares @ ${:.4f} | {:.1f}% drop from HWM ${:.4f} ' \
                                   '(trail={:.0f}% day {}) | net P&L ${:.2f}'.format(
                                       symbol, shares, price, drop_from_hwm, hwm, trail_pct, days_open, loss)
                            self._entry('TRAIL-STOP', note)
                            from modules.notify import msg_exit
                            self._notify(msg_exit('TRAIL-STOP', symbol,
                                '{:.1f}% drop from HWM'.format(drop_from_hwm), loss), 'alert')

                    # Time-exit: up >2% after 2 hours — harvest 60% (legacy; superseded by the
                    # adaptive planner's TIME-HARVEST when adaptive_harvest_enabled).
                    elif (not self.config.get('adaptive_harvest_enabled', True)
                          and (datetime.now() - self._position_opened.get(symbol, datetime.now())).total_seconds() >= 5400
                          and net_gain >= 1.5):
                        time_shares = max(1, int(shares * 0.60))
                        result = self.paper.sell(symbol, time_shares, price_override=price)
                        if result.get('ok'):
                            profit = self._net_profit(time_shares, price, entry)
                            self.stats['total_profit'] += profit
                            self.stats['wins'] += 1
                            self.stats['total_win_pct'] += gain_pct
                            self._consecutive_losses = 0
                            self._position_harvests[symbol] = harvests_done + 1
                            note = '{} — 2h time-harvest: sold 60% ({} shares) @ ${:.4f} | {:.1f}% gain | P&L ${:.2f} | {} shares remain'.format(
                                symbol, time_shares, price, net_gain, profit, shares - time_shares)
                            self._entry('TIME-EXIT', note)
                            from modules.notify import msg_exit
                            self._notify(msg_exit('TIME-EXIT', symbol,
                                '2h time-exit at {:.1f}%'.format(net_gain), profit))

                    # Time-stop: stale position after 3 days with no profit
                    elif days_open >= 3 and net_gain <= 0:
                        result = self.paper.sell(symbol, shares, price_override=price)
                        if result.get('ok'):
                            loss = self._net_profit(shares, price, entry)
                            self.stats['total_profit'] += loss
                            self._cleanup_position(symbol)
                            self.stats['losses'] += 1
                            self.stats['total_loss_pct'] += abs(gain_pct)
                            self._consecutive_losses += 1
                            note = '{} — sold ALL {} shares @ ${:.4f} | {} biz days open, ' \
                                   'no profit ({:.1f}%) | net P&L ${:.2f}'.format(
                                       symbol, shares, price, days_open, net_gain, loss)
                            self._entry('TIME-STOP', note)
                            from modules.notify import msg_exit
                            self._notify(msg_exit('TIME-STOP', symbol,
                                '{} days open, no profit'.format(days_open), loss), 'alert')

                    # Exit: only after at least one harvest when margin compresses
                    elif harvests_done > 0 and net_gain <= exit_trig:
                        result = self.paper.sell(symbol, shares, price_override=price)
                        if result.get('ok'):
                            profit = self._net_profit(shares, price, entry)
                            self.stats['total_profit'] += profit
                            self._cleanup_position(symbol)
                            pct = (price - entry) / entry * 100 if entry > 0 else 0
                            if pct >= 0:
                                self.stats['wins'] += 1
                                self.stats['total_win_pct'] += pct
                                self._consecutive_losses = 0
                            else:
                                self.stats['losses'] += 1
                                self.stats['total_loss_pct'] += abs(pct)
                                self._consecutive_losses += 1
                            note = '{} — sold ALL {} shares @ ${:.4f} | margin {:.1f}% ≤ {:.0f}% ' \
                                   'after {} harvest(s) | net P&L ${:.2f}'.format(
                                       symbol, shares, price, net_gain, exit_trig, harvests_done, profit)
                            self._entry('EXIT', note)
                            from modules.notify import msg_exit
                            self._notify(msg_exit('EXIT', symbol,
                                'margin {:.1f}% after {} harvests'.format(net_gain, harvests_done), profit))

            except Exception as e:
                self._entry('ERROR', '{} monitor failed: {}'.format(symbol, str(e)))

        # ── Step 2: Look for new buy opportunity ────────────────────────────
        if session not in _BUY_SESSIONS:
            return

        # Hard ET hour gate — belt-and-suspenders in addition to session check
        now_et = self._get_et_now()
        if now_et.hour < 9 or now_et.hour >= 16:
            return

        # Wait for the 5-minute opening range to confirm direction before buying
        if session == 'OPEN_MOMENTUM' and now_et.hour == 9 and now_et.minute < 35:
            self._entry('SESSION', 'Waiting for 9:35 opening range confirmation — no buys yet')
            return

        balance = self.paper.get_state().get('balance', 0)
        positions = [p['symbol'] for p in self.paper.get_state().get('positions', [])]

        max_positions = int(self.config.get('max_concurrent_positions', 8))
        if len(positions) >= max_positions:
            self._entry('LIMIT', 'Max {} positions held — no new buys until one is sold'.format(max_positions))
            return

        if self._net_deployed_today() >= daily_limit:
            self._entry('LIMIT', 'Daily spend limit $%.0f reached (net deployed) — no new buys' % daily_limit)
            return

        # PDT rule gate — skipped entirely in cash account mode (no PDT restriction on cash accounts)
        if not self.config.get('cash_account_mode', False):
            account_value = self.paper.get_state().get('balance', 0)
            if account_value < 25000:
                now_date = (datetime.now(_ET) if _ET else datetime.now()).date()
                window    = _pdt_window_dates(now_date)
                recent_dts = sum(1 for ds in self._day_trades_log if ds in window)
                if recent_dts >= 3:
                    now_ts = time.time()
                    if now_ts - self._pdt_last_log >= 1800:
                        self._entry('PDT',
                            'Day-trade guard: {} same-day round-trips in rolling 5-biz-day window — '
                            'cool-off applied (cash_account_mode=false; set true to remove restriction; '
                            'account ${:.0f})'.format(recent_dts, account_value))
                        self._pdt_last_log = now_ts
                    return
                elif recent_dts == 2:
                    self._entry('PDT', 'Day-trade guard: {}/3 same-day round-trips used — one remaining'.format(recent_dts))

        # Market regime gate: no new buys when SPY is down > 1.5%
        spy_chg = self._market_regime()
        if spy_chg <= -1.5:
            self._entry('REGIME', 'SPY {:.2f}% — bearish tape, no new buys this cycle'.format(spy_chg))
            return

        # Macro environment gate: SEVERE-ONLY circuit breaker. A harvesting engine extracts the
        # predictable middle regardless of the seasonal calendar, so a soft score (e.g. summer
        # doldrums −10) must NOT sideline the whole session — macro still feeds the ranking bonus
        # and the exposure governor. Only a genuine shock (real unrest/econ, ≤ macro_veto_score)
        # halts new buys. Configurable; set macro_hard_gate=false to disable entirely.
        if self.macro and self.config.get('macro_hard_gate', True):
            macro_score = self.macro.get_score()
            veto = float(self.config.get('macro_veto_score', -20))
            if macro_score <= veto:
                self._entry('MACRO', 'Macro SEVERELY adverse (score {:+d} ≤ {:+.0f}) — skipping new buys'.format(macro_score, veto))
                return

        # Daily drawdown circuit breaker — halt if down >max_daily_loss_pct% from day open
        max_dd_pct = float(self.config.get('max_daily_loss_pct', 10.0))
        drawdown = self._daily_drawdown_pct(balance)
        if drawdown >= max_dd_pct:
            if not self._daily_pnl_floor_hit:
                self._daily_pnl_floor_hit = True
                reason = 'Daily loss floor hit — down {:.1f}% (${:.2f}) from day open — no new buys for rest of session'.format(
                    drawdown, self._daily_start_balance - balance)
                self._entry('HALT', reason)
                from modules.notify import msg_halt
                self._notify(msg_halt(reason), 'alert')
            return

        # Consecutive loss guard — cool-off after N straight losing trades
        max_streak = int(self.config.get('max_consecutive_losses', 3))
        if self._consecutive_losses >= max_streak:
            now_ts = time.time()
            if now_ts - self._halt_notify_ts >= 1800:
                reason = '{} consecutive losses — cooling off, no new buys'.format(
                    self._consecutive_losses)
                self._entry('HALT', reason)
                from modules.notify import msg_halt
                self._notify(msg_halt(reason), 'alert')
                self._halt_notify_ts = now_ts
            return

        remaining_today = daily_limit - self._net_deployed_today()
        kelly_size = self._kelly_size(balance)
        # Hard cap: never deploy more than position_size_pct% of balance in one trade
        pos_cap = balance * float(self.config.get('position_size_pct', 15.0)) / 100.0
        buy_amount = min(kelly_size, remaining_today, balance, pos_cap)
        # Adaptive exposure governor: throttle size up/down vs the breakeven win rate the banked
        # outcomes imply — protect capital when we're losing, press when we're winning. Bounded.
        buy_amount *= self.adaptive.exposure_scale()
        if buy_amount < 5:
            now_ts = time.time()
            if now_ts - self._session_last_log >= 1800:
                self._entry('SKIP', 'Insufficient capital for new buy (available: $%.2f)' % buy_amount)
                self._session_last_log = now_ts
            return

        # Scan for best candidate
        # Pull Reddit trending, ORB breakouts, insider cluster, and congress buys as bonus universe additions
        reddit_tickers = []
        if self.behavioral:
            reddit_tickers = [t['ticker'] for t in self.behavioral.get_trending_tickers(min_velocity=10)]
            if reddit_tickers:
                self._entry('BEHAVIORAL', 'Reddit trending additions: {}'.format(', '.join(reddit_tickers[:8])))
        if self.orb:
            orb_breaks = self.orb.get_breakouts()
            if orb_breaks:
                orb_syms = [b['symbol'] for b in orb_breaks]
                self._entry('ORB', 'Opening range breakouts: {} (top: {} +{:.1f}%)'.format(
                    len(orb_breaks), orb_breaks[0]['symbol'], orb_breaks[0]['breakout_pct']))
                reddit_tickers = list(dict.fromkeys(reddit_tickers + orb_syms))
        if self.news:
            hot_news = [t['ticker'] for t in self.news.get_hot_tickers(min_score=8)]
            if hot_news:
                self._entry('NEWS', 'News catalyst additions: {}'.format(', '.join(hot_news[:6])))
                reddit_tickers = list(dict.fromkeys(reddit_tickers + hot_news))
        if self.short_squeeze:
            squeeze_cands = [c['ticker'] for c in self.short_squeeze.get_squeeze_candidates(min_score=15)]
            if squeeze_cands:
                self._entry('SQUEEZE', 'Short squeeze candidates: {}'.format(', '.join(squeeze_cands[:5])))
                reddit_tickers = list(dict.fromkeys(reddit_tickers + squeeze_cands))
        if self.insider:
            cluster = [t['ticker'] for t in self.insider.get_cluster_tickers(min_filings=2)]
            if cluster:
                self._entry('INSIDER', 'Insider cluster buy additions: {}'.format(', '.join(cluster[:6])))
                reddit_tickers = list(dict.fromkeys(reddit_tickers + cluster))
        if self.congress:
            cong_items = self.congress.get_buying_tickers(min_members=1)
            cong = [t['ticker'] for t in cong_items]
            if cong:
                self._entry('CONGRESS', 'Congress buy additions: {}'.format(', '.join(cong[:6])))
                reddit_tickers = list(dict.fromkeys(reddit_tickers + cong))
        self._entry('SCAN', 'Scanning top movers for buy opportunity...')
        try:
            movers = self.harvester.scan_movers(extra_symbols=reddit_tickers)
            is_momentum_session = session == 'OPEN_MOMENTUM'
            # Config-driven so the gate can be swept/tuned without code edits (defaults match
            # prior hardcoded values, so live behavior is unchanged). vol_ratio is the new
            # cumulative-pace RVOL (see scan_movers).
            if is_momentum_session:
                min_change_1h = float(self.config.get('min_change_1h_momentum', 2.0))
                min_rvol      = float(self.config.get('min_rvol_momentum', 2.0))
            else:
                min_change_1h = float(self.config.get('min_change_1h_regular', 3.0))
                min_rvol      = float(self.config.get('min_rvol_regular', 1.5))
            if self.config.get('harvest_regime', True):
                # HARVEST REGIME (north star): fish for the predictable MIDDLE — steady, liquid,
                # tight-spread names with repeatable intraday range — NOT explosive breakouts.
                # Momentum hunting (open-ended change_1h, high RVOL spikes, parabolic runners)
                # produces a bimodal "fail or rip" population with no harvestable middle. We
                # therefore BOUND change and volume into a moderate band, impose a price floor
                # (a tight-spread proxy — sub-$1 pennies carry punishing spreads), cap volatility,
                # and reject extended runners outright.
                lo_chg   = float(self.config.get('harvest_min_change_1h', 1.0))
                hi_chg   = float(self.config.get('harvest_max_change_1h', 8.0))
                lo_rvol  = float(self.config.get('harvest_min_rvol', 1.2))
                hi_rvol  = float(self.config.get('harvest_max_rvol', 5.0))
                max_vol  = float(self.config.get('harvest_max_volatility', 18.0))
                min_px   = float(self.config.get('harvest_min_price', 1.0))
                max_px   = float(self.config.get('max_stock_price', 15.0))
                max_5d   = float(self.config.get('harvest_max_change_5d', 25.0))
                candidates = [m for m in movers
                              if m['symbol'] not in positions
                              and not self._blacklisted(m['symbol'])
                              and min_px <= m['price'] < max_px
                              and lo_chg <= m['change_1h'] <= hi_chg          # moderate move, not parabolic
                              and lo_rvol <= m['vol_ratio'] <= hi_rvol        # active but not a squeeze spike
                              and (m.get('volatility', 0.0) <= max_vol or not m.get('volatility'))
                              and m['change_5d'] <= max_5d]                   # not an extended runner
            else:
                # MOMENTUM REGIME (legacy): hunt explosive movers. Retained for A/B comparison.
                candidates = [m for m in movers
                              if m['symbol'] not in positions
                              and not self._blacklisted(m['symbol'])
                              and m['price'] > 0
                              and m['price'] < float(self.config.get('max_stock_price', 15.0))
                              and m['change_1h'] > min_change_1h
                              and m['vol_ratio'] > min_rvol
                              # Skip only truly exhausted runners: big multi-day move AND volume fading.
                              and not (m['change_5d'] > 30.0 and m['vol_ratio'] < 2.0)]

            # Earnings guard: never open a position within 2 days of earnings
            if self.earnings:
                pre_count = len(candidates)
                candidates = [c for c in candidates if not self.earnings.should_block_buy(c['symbol'])]
                blocked = pre_count - len(candidates)
                if blocked:
                    self._entry('EARNINGS', '{} candidates blocked — earnings within {} days'.format(
                        blocked, 2))
                # Register any new tickers for earnings tracking
                for c in candidates:
                    self.earnings.register(c['symbol'])

            # Halt guard: never buy into a live trading halt
            if self.halts:
                pre_count = len(candidates)
                candidates = [c for c in candidates if not self.halts.is_halted(c['symbol'])]
                if len(candidates) < pre_count:
                    self._entry('HALT', '{} candidates blocked — currently halted'.format(pre_count - len(candidates)))

            # Catalyst + dilution guard filter
            if self.catalyst:
                pre_count = len(candidates)
                candidates = [
                    c for c in candidates
                    if not self.catalyst.is_diluting(c['symbol'])
                    and self.catalyst.get_score(c['symbol']) >= 0
                ]
                blocked = pre_count - len(candidates)
                if blocked > 0:
                    self._entry('CATALYST', '{} candidates blocked by dilution guard'.format(blocked))

            if not candidates:
                self._entry('SCAN', 'No qualifying candidates found this cycle')
                return

            # Blend realtime engine score into ranking if available
            if self.engine:
                for c in candidates:
                    try:
                        rt = self.engine.score(c['symbol'])
                        rt_score = rt.get('composite_score', 0)
                        c['combined_score'] = round(c['score'] * 0.6 + rt_score * 0.4, 1)
                        c['rt_signal'] = rt.get('signal', '')
                        c['rt_score'] = rt_score
                    except Exception:
                        c['combined_score'] = c['score']
                        c['rt_signal'] = ''
                        c['rt_score'] = 0
                candidates = [c for c in candidates if c.get('rt_signal') != 'AVOID']
                candidates.sort(key=lambda x: x.get('combined_score', 0), reverse=True)
            else:
                for c in candidates:
                    c['combined_score'] = c['score']
                    c['rt_signal'] = ''
                    c['rt_score'] = 0

            # Pre-compute boost lookup tables (avoid re-calling per-candidate)
            _multi_stream = set()
            if hasattr(self.harvester, 'universe') and self.harvester.universe:
                try:
                    _multi_stream = set(self.harvester.universe.get_multi_stream(min_sources=2))
                except Exception:
                    pass
            _premarket_map = {g['symbol']: g for g in self._premarket_watchlist}

            # Boost candidates: halt-resume, SSR, behavioral, macro, insider, congress
            macro_score = self.macro.get_score() if self.macro else 0
            # In harvest regime, suppress the explosive/breakout narrative bonuses (gap, ORB,
            # rocket-streak, float-rotation, squeeze) — they bias ranking toward the parabolic
            # names the north star avoids. Information-edge agents (news, insider, congress,
            # sector, order-flow) stay on.
            harvest_mode = bool(self.config.get('harvest_regime', True))
            for c in candidates:
                sym = c['symbol']
                bonus = 0
                if self.halts and self.halts.is_resume_play(sym):
                    bonus += 20
                    c['rt_signal'] = 'RESUME'
                if self.ssr and self.ssr.is_ssr(sym):
                    bonus += 10
                if self.behavioral:
                    beh = self.behavioral.get_score(sym)
                    bonus += max(-10, min(20, beh))
                    c['behavioral_score'] = beh
                if macro_score != 0:
                    bonus += max(-10, min(10, macro_score // 2))
                if self.insider:
                    insider_bonus = self.insider.get_score(sym)
                    bonus += insider_bonus
                    if insider_bonus > 0:
                        c['insider_signal'] = 'CLUSTER_BUY'
                if self.congress:
                    congress_bonus = self.congress.get_score(sym)
                    bonus += congress_bonus
                    if congress_bonus > 0:
                        c['congress_signal'] = 'BUYING'
                # OFI from realtime velocity cache — buy-side order flow pressure
                if self.engine and hasattr(self.engine, 'cache'):
                    vel = self.engine.cache.velocity.get(sym, {})
                    ofi = vel.get('ofi', 0.0)
                    if ofi > 0.3:
                        bonus += int(ofi * 10)   # up to +10 for strong buy-side flow
                    elif ofi < -0.3:
                        bonus -= int(abs(ofi) * 10)
                # Multi-stream universe consensus — ticker confirmed by 2+ independent sources
                if sym in _multi_stream:
                    bonus += 15
                    c['rt_signal'] = c.get('rt_signal') or 'MULTI-STREAM'
                # Pre-market gapper — identified before open as high-probability setup
                gap_info = _premarket_map.get(sym)
                if gap_info and not harvest_mode:
                    gap_bonus = min(25, int(gap_info.get('gap_pct', 0) * 1.5))
                    bonus += gap_bonus
                    c['rt_signal'] = 'GAP-{:.0f}%'.format(gap_info['gap_pct'])
                # ORB breakout — price broke above first-5m candle high
                if self.orb and not harvest_mode:
                    orb_lvl = self.orb.get_orb_level(sym)
                    if orb_lvl:
                        orb_high = orb_lvl.get('high', 0)
                        if orb_high > 0 and c['price'] > orb_high * 1.005:
                            orb_pct = (c['price'] / orb_high - 1) * 100
                            orb_bonus = min(20, int(orb_pct * 3))
                            bonus += orb_bonus
                            c['rt_signal'] = c.get('rt_signal') or 'ORB+{:.1f}%'.format(orb_pct)
                # Rocket setup: consecutive green bars with volume acceleration
                if c.get('green_streak', 0) >= 3 and not harvest_mode:
                    bonus += min(c['green_streak'] * 3, 12)   # up to +12 for 4-bar streak
                # News catalyst score
                if self.news:
                    news_pts = self.news.get_news_score(sym)
                    if news_pts != 0:
                        bonus += max(-20, min(25, news_pts))
                        if news_pts >= 15:
                            c['rt_signal'] = c.get('rt_signal') or 'NEWS-CATALYST'
                # Float rotation: strong buying relative to available supply
                if self.float_rotation and not harvest_mode:
                    fr_pts = self.float_rotation.get_rotation_score(sym)
                    if fr_pts != 0:
                        bonus += fr_pts
                # Short squeeze setup
                if self.short_squeeze and not harvest_mode:
                    sq_pts = self.short_squeeze.get_squeeze_score(sym)
                    if sq_pts >= 10:
                        bonus += min(sq_pts, 20)
                        c['rt_signal'] = c.get('rt_signal') or 'SQUEEZE'
                # Options flow: unusual call buying = institutional conviction pre-move
                if self.engine and hasattr(self.engine, 'cache'):
                    opts = list(self.engine.cache.options_flow.get(sym, []))
                    if opts:
                        call_prem = sum(e.get('premium', 0) for e in opts if e.get('type') == 'CALL')
                        put_prem  = sum(e.get('premium', 0) for e in opts if e.get('type') == 'PUT')
                        if call_prem > 50:   # >$50K call premium = meaningful conviction
                            opts_bonus = min(int(call_prem / 10), 20)
                            bonus += opts_bonus
                            c['rt_signal'] = c.get('rt_signal') or 'OPTIONS-FLOW'
                        elif put_prem > call_prem * 2:
                            bonus -= 10   # heavy put buying = bearish institutional bet
                # Sector tailwind/headwind
                if self.sector:
                    sec_pts = self.sector.get_sector_score(sym)
                    if sec_pts != 0:
                        bonus += sec_pts
                # Cap total agent-bonus stacking so no single narrative pile-up
                # (insider + news + gap + ORB + squeeze …) can swamp the price/volume
                # base score and buy a thin name on story alone.
                bonus = max(-40, min(50, bonus))
                if bonus:
                    c['combined_score'] = round(c.get('combined_score', c['score']) + bonus, 1)
            if any(c.get('combined_score', 0) != c['score'] for c in candidates):
                candidates.sort(key=lambda x: x.get('combined_score', 0), reverse=True)

            if not candidates:
                self._entry('SCAN', 'All candidates rated AVOID by realtime signals — skipping')
                return

            # VWAP filter: skip candidates trading > 5% above intraday VWAP
            while candidates:
                top = candidates[0]
                vwap, _ = self._get_vwap(top['symbol'])
                if vwap and top['price'] > vwap * 1.05:
                    self._entry('VWAP',
                        '{} at ${:.4f} is {:.1f}% above VWAP ${:.4f} — skipping'.format(
                            top['symbol'], top['price'], (top['price'] / vwap - 1) * 100, vwap))
                    candidates.pop(0)
                else:
                    break
            if not candidates:
                self._entry('VWAP', 'All candidates above VWAP — no clean entry this cycle')
                return

            # Pick the highest-ranked candidate with a VALID live price. A single bad-price /
            # halted / stale top pick must NOT black-hole the day (smoke test caught exactly this:
            # SNDL ranked #1 and failed to buy 24× in a row). Fall through to the next candidate
            # and blacklist the bad one for the session so it isn't re-chosen every cycle.
            best = None
            for cand in candidates:
                if self._blacklisted(cand['symbol']):
                    continue
                lp = self._get_live_price(cand['symbol'])
                if lp and lp > 0:
                    best, symbol, price = cand, cand['symbol'], lp
                    break
                self._blacklist_entry(cand['symbol'])
                self._entry('ENTRY-SKIP',
                    '{} no valid live price — blacklisted, trying next candidate'.format(cand['symbol']))
            if best is None:
                self._entry('SCAN', 'No candidate with a valid live price this cycle')
                return

            # Decision Record — capture this cycle's full context (all movers, who passed the
            # filter, per-agent attribution, regime, the top pick) for counterfactual learning.
            if self.config.get('decision_log_enabled', True):
                try:
                    _et = self._get_et_now()
                    regime = {'spy_chg': round(float(spy_chg), 2), 'session': session,
                              'hod': _et.hour * 60 + _et.minute, 'dow': _et.weekday()}
                    self.decision_log.record(movers, {c['symbol'] for c in candidates},
                                             symbol, regime, _et.timestamp())
                except Exception:
                    pass

            # Feed-mode guard — refuse timing-sensitive entries on a degraded (delayed) feed.
            # Default OFF so the paper phase keeps collecting data; turn ON before live money.
            if self.config.get('block_trades_on_delayed', False) and self.feed and not self.feed.is_real_time():
                self._entry('DELAYED-FEED', 'Entry blocked — market-data feed is {} (not real-time)'.format(self.feed.mode()))
                return

            # Minimum combined score gate — skip low-conviction setups
            min_score = int(self.config.get('min_buy_score', 20))
            best_score = best.get('combined_score', best.get('score', 0))
            if best_score < min_score:
                self._entry('SCORE-GATE',
                    '{} score {} below minimum {} — skipping'.format(symbol, best_score, min_score))
                return

            # 1-min entry confirmation: reject if momentum is actively declining or cascade forming.
            # After 5 consecutive cascade rejections, cool-off symbol for 30m so the next candidate
            # gets a chance rather than the cascade trap repeating all session.
            entry_slope, entry_cascade, _ = self._momentum_snapshot(symbol)
            cascade_count, cascade_ts = self._cascade_strikes.get(symbol, (0, 0.0))
            _cascade_cooloff = 1800.0  # 30-minute cooloff after 5 cascade rejections
            if time.time() - cascade_ts < _cascade_cooloff and cascade_count >= 5:
                candidates = [c for c in candidates if c['symbol'] != symbol]
                if not candidates:
                    self._entry('ENTRY-SKIP', '{} in cascade cooloff — no other candidates'.format(symbol))
                    return
                best   = candidates[0]
                symbol = best['symbol']
                price  = best['price']
                entry_slope, entry_cascade, _ = self._momentum_snapshot(symbol)
            if entry_cascade:
                count = cascade_count + 1 if symbol == best['symbol'] else 1
                self._cascade_strikes[symbol] = (count, time.time())
                self._entry('ENTRY-SKIP',
                    '{} cascade pattern on 1m bars at entry ({}/{} strikes) — skipping'.format(
                        symbol, count, 5))
                return
            else:
                if symbol in self._cascade_strikes:
                    del self._cascade_strikes[symbol]   # clear strikes on clean entry
            if entry_slope < -0.5:
                self._entry('ENTRY-SKIP',
                    '{} 1m slope {:.2f}%/bar declining at entry — skipping this cycle'.format(
                        symbol, entry_slope))
                return

            # Spread guard: estimated H-L spread must be < 40% of harvest target
            est_spread = self._estimate_spread_pct(symbol)
            best['spread_pct'] = est_spread   # predictive feature + cost-calibration target
            spread_limit = harvest_trig * 0.40
            if est_spread > spread_limit:
                self._entry('SPREAD',
                    '{} estimated spread {:.1f}% > {:.1f}% limit (40% of {:.0f}% harvest target) '
                    '— entry not viable'.format(symbol, est_spread, spread_limit, harvest_trig))
                return

            # Live conviction gate — use the already-computed combined_score (scan momentum +
            # realtime velocity + capped agent bonuses). The old SignalAggregator.score_symbol
            # gate was removed here: it ran on stale daily/yfinance data (300s cache), gated on
            # fundamentals irrelevant to a day-trade, and silently failed open — so it either
            # blocked good fast movers or did nothing. The live composite is the honest signal.
            min_sig_score = int(self.config.get('min_signal_score', 35))
            live_score = best.get('combined_score', best.get('score', 0))
            if live_score < min_sig_score:
                self._entry('SIGNAL-GATE',
                    '{} live composite {:.0f} below {} — skip'.format(symbol, live_score, min_sig_score))
                return

            shares = max(1, int(buy_amount / price))
            cost   = shares * price
            rvol   = self._get_rvol(symbol)

            # RVOL gate: require at least 1.2× average volume — low-volume entries rarely follow through
            min_rvol = float(self.config.get('min_entry_rvol', 1.2))
            if rvol < min_rvol:
                self._entry('RVOL-GATE',
                    '{} rvol {:.1f}x below {:.1f}x minimum — waiting for volume confirmation'.format(
                        symbol, rvol, min_rvol))
                return

            # ── Social-flow: crowd signal for the chosen candidate (1 fetch, cached) ──────
            social_score = 0
            if self.social and self.config.get('social_flow_enabled', True):
                try:
                    sf = self.social.get_features(symbol)
                    best['social_velocity']   = sf['social_velocity']
                    best['social_bull']       = sf['social_bull']
                    best['social_exhaustion'] = sf['social_exhaustion']
                    social_score = self.social.get_social_score(symbol)   # bot-discounted, capped
                    best['combined_score'] = best.get('combined_score', best.get('score', 0)) + social_score
                except Exception:
                    pass

            # ── Predictive model: calibrated P(win) → optional gate + conviction sizing ──
            velocity = {}
            if self.engine and hasattr(self.engine, 'cache'):
                velocity = self.engine.cache.velocity.get(symbol, {})
            pred  = self.predictor.predict(best, velocity, rvol)
            p_win = pred['p_win']
            if self.config.get('predictor_gate', False):
                min_p = float(self.config.get('predictor_min_p', 0.45))
                if p_win < min_p:
                    self._entry('PREDICT-GATE',
                        '{} P(win) {:.0%} below {:.0%} — skip (model n={})'.format(
                            symbol, p_win, min_p, pred['n_trained']))
                    return
            if self.config.get('predictor_sizing', True):
                # EV-aware conviction sizing (P(win) × expected edge), then re-clamp to the
                # same caps the base size respected so it can't breach daily/balance/per-trade.
                adj   = buy_amount * self.predictor.size_multiplier(
                    p_win, pred.get('expected_edge'),
                    transaction_cost_pct(est_spread, price, shares, self.config))
                adj   = min(adj, remaining_today, balance, pos_cap * 1.25)
                shares = max(1, int(adj / price))
                cost   = shares * price
            # Open a triple-barrier watch: label this setup by the price PATH (target/stop/
            # horizon first-touch), independent of when the bot actually exits. Uses the
            # simulated clock under replay so barriers resolve there too.
            self.predictor.open_barrier(
                symbol, pred['features'], price, self._get_et_now().timestamp(),
                float(self.config.get('harvest_trigger_pct', 4.0)),
                float(self.config.get('max_single_loss_pct', 5.0)))

            cat_score  = self.catalyst.get_score(symbol) if self.catalyst else 0
            news_score = self.news.get_news_score(symbol) if self.news else 0
            cat_score  = cat_score + news_score

            # Execute at the SAME validated live price the decision/barrier used — avoids a
            # divergent second price-fetch inside the trader (which could fail or fill at a
            # different tick) and keeps entry/barrier/decision prices consistent.
            result = self.paper.buy(symbol, shares, price_override=price)
            if result.get('ok'):
                self.daily_spent += cost
                self.stats['total_trades'] += 1
                self._position_harvests[symbol] = 0
                # Lock in the spread-aware round-trip cost at entry — reused by the monitor loop
                # so it never re-samples the quote per tick.
                self._position_tx_cost[symbol] = transaction_cost_pct(est_spread, price, shares, self.config)
                self._harvest_level[symbol] = float(self.config.get('harvest_trigger_pct', 4.0))  # arm; ratchets up on incline
                # Bank the full entry context onto the trade-lifecycle ledger.
                try:
                    self.trade_record.set_context(symbol, {
                        'score': round(float(best.get('combined_score', best.get('score', 0))), 1),
                        'spread_pct': est_spread,
                        'rvol': round(float(rvol), 2),
                        'change_1h': round(float(best.get('change_1h', 0)), 2),
                        'p_win': round(float(p_win), 4),
                        'expected_edge': round(float(pred.get('expected_edge', 0.0)), 3),
                        'tx_cost_pct': self._position_tx_cost[symbol],
                        'exposure_scale': round(float(self.adaptive.exposure_scale()), 3),
                        'session': session,
                    })
                except Exception:
                    pass
                self._position_entry_rvol[symbol] = rvol   # save for volume-exhaustion check
                rt_info     = ' | RT:{} ({:.0f})'.format(
                    best.get('rt_signal', ''), best.get('rt_score', 0)) if self.engine else ''
                cat_info    = ' | CAT:{:+d}'.format(cat_score) if (self.catalyst or self.news) else ''
                spy_info    = ' | SPY:{:+.1f}%'.format(spy_chg) if spy_chg != 0 else ''
                spread_info = ' | spread~{:.1f}%'.format(est_spread)
                combined_score = best.get('combined_score', best.get('score', 0))
                pred_info = ' | P(win):{:.0%} edge:{:+.1f}%{}'.format(
                    p_win, pred.get('expected_edge', 0.0), '' if pred['alpha'] >= 0.999 else '~') + (
                    ' [learning n={}]'.format(pred['n_trained']) if pred['n_trained'] < 100 else '')
                self._entry('BUY',
                    '{} — {} shares @ ${:.4f} | cost ${:.2f} | kelly ${:.0f} | '
                    'score {:.0f}{}{}{}{}{} | 1h:{:+.1f}% rvol:{:.1f}x'.format(
                        symbol, shares, price, cost, kelly_size,
                        combined_score, pred_info, rt_info, cat_info, spy_info, spread_info,
                        best.get('change_1h', 0), rvol))
                from modules.notify import msg_buy
                self._notify(msg_buy(symbol, shares, price, cost, combined_score))
            else:
                self._blacklist_entry(symbol)
                self._entry('ERROR', 'Buy failed: {} — blacklisted for session'.format(result.get('error', 'unknown')))

        except Exception as e:
            self._entry('SCAN', 'Scan error: ' + str(e))

    # ── Real-time stream integration ──────────────────────────────────────────

    def _get_live_price(self, symbol: str) -> float:
        """
        Price priority: Polygon/Finnhub stream tick (<60s old) → velocity tracker → yfinance.
        Eliminates yfinance round-trips for positions when streaming is active.
        """
        if self.engine and hasattr(self.engine, 'cache'):
            ticks = self.engine.cache.price_ticks.get(symbol)
            if ticks:
                ts, price, _ = ticks[-1]
                if time.time() - ts < 60 and price > 0:
                    return float(price)
            vel = self.engine.cache.velocity.get(symbol, {})
            if vel.get('price', 0) > 0:
                return float(vel['price'])
        return self._md.last_price(symbol)

    def _momentum_from_cache(self, symbol: str):
        """
        Read momentum from RealtimeCache instead of fetching yfinance 1-min bars.
        Returns (slope_pct, cascade_bool, rvol) matching _momentum_snapshot() contract,
        or None if cache is empty for this symbol.
        """
        if not self.engine or not hasattr(self.engine, 'cache'):
            return None
        vel = self.engine.cache.velocity.get(symbol, {})
        if not vel or not vel.get('price', 0):
            return None
        pct_1m = vel.get('pct_1m', 0.0)
        pct_5m = vel.get('pct_5m', 0.0)
        ofi    = vel.get('ofi', 0.0)
        vol_r  = vel.get('vol_ratio', 1.0)
        # Cascade: strong sell-side order flow + both timeframes declining
        cascade = ofi < -0.4 and pct_1m < 0 and pct_5m < 0
        return (round(pct_1m, 3), cascade, round(vol_r, 2))

    def _on_stream_price(self, symbol: str, price: float, volume: int):  # noqa: ARG002 volume unused here, used by cache accumulator
        """
        Fast-path callback invoked by RealtimeCache on every Polygon/Finnhub tick.
        Two fast-paths:
          1. ORB breakout buy — fires immediately when price crosses ORB trigger
          2. Stream stop-loss — fires immediately when position hits max-loss threshold
        Both are non-blocking so the stream message thread is never stalled.
        """
        if not self.running or price <= 0:
            return

        # ── Outlier guard ──────────────────────────────────────────────────────
        # Reject obviously-bad single ticks (odd-lot prints, crosses, fat-fingers) so a
        # corrupt price can never trigger a forced stop/sell at a fake level. A move >25%
        # from the last good price is accepted only if the very next tick confirms it
        # (two consecutive ticks within 5% of each other); a lone spike is dropped.
        lg = getattr(self, '_last_good_stream_px', None)
        if lg is None:
            lg = self._last_good_stream_px = {}
        pend = getattr(self, '_stream_pending_px', None)
        if pend is None:
            pend = self._stream_pending_px = {}
        prev = lg.get(symbol)
        if prev and prev > 0 and abs(price - prev) / prev > 0.25:
            confirm = pend.get(symbol)
            if confirm and abs(price - confirm) / price <= 0.05:
                pass  # two consecutive ticks agree on the new level → real move, accept
            else:
                pend[symbol] = price   # remember this suspect tick; wait for confirmation
                return
        pend.pop(symbol, None)
        lg[symbol] = price

        # ── Fast-path 1: ORB breakout buy ──────────────────────────────────────
        # Bypasses the 60s scan loop — entry fires within 1 stream tick of breakout.
        # Disabled in harvest regime: it is a pure-breakout (extreme-entry) play, the opposite
        # of harvesting the predictable middle. The stream stop-loss/harvest path below stays on.
        if (self.orb and not self.config.get('harvest_regime', True)
                and symbol not in self._orb_fired
                and _get_session() in _BUY_SESSIONS - {'PRE_MARKET', 'AFTERNOON', 'CLOSE'}):
            orb_lvl = self.orb.get_orb_level(symbol)
            if orb_lvl:
                orb_high = float(orb_lvl.get('high', 0))
                if orb_high > 0 and price > orb_high * 1.005:
                    self._orb_fired.add(symbol)   # mark before spawning to prevent duplicate
                    threading.Thread(
                        target=self._orb_fast_buy, args=(symbol, price, orb_high),
                        daemon=True).start()

        # ── Fast-path 2: stream harvest + stop-loss ────────────────────────────
        # Market-hours exit gate: after-hours stream ticks are stale/illiquid — never transact on
        # them; the tick loop holds and defers to the next open (same rule as the monitor loop).
        if self.config.get('market_hours_only_exits', True) and _get_session() not in _MARKET_OPEN_SESSIONS:
            return
        positions = {p['symbol']: p for p in self.paper.get_state().get('positions', [])}
        if symbol not in positions:
            return
        pos    = positions[symbol]
        entry  = float(pos.get('avg_cost', 0))
        shares = int(pos.get('shares', 0))
        if entry <= 0 or shares <= 0:
            return
        tx_cost      = self._tx_cost(symbol)   # spread-aware, locked in at entry
        net_gain     = (price - entry) / entry * 100 - tx_cost
        # Respect the ADAPTIVE ratcheted trigger — once the planner has raised the level on a
        # sustained incline, the fast stream path must not harvest below it.
        harvest_trig = self._harvest_level.get(symbol, float(self.config.get('harvest_trigger_pct', 6.0)))
        max_loss     = float(self.config.get('max_single_loss_pct', 8.0))

        # 2a: stream harvest — captures gain at the tick price rather than up to 60s later
        if net_gain >= harvest_trig:
            if not self._stream_lock.acquire(blocking=False):
                return
            try:
                pos2 = {p['symbol']: p for p in self.paper.get_state().get('positions', [])}.get(symbol)
                if not pos2 or int(pos2.get('shares', 0)) <= 0:
                    return
                shares2       = int(pos2.get('shares', 0))
                days_open     = (datetime.now() - self._position_opened.get(symbol, datetime.now())).days
                harvests_done = self._position_harvests.get(symbol, 0)
                frac          = self._harvest_fraction(symbol, days_open, harvests_done, _get_session())
                h_shares      = max(1, int(shares2 * frac))
                result = self.paper.sell(symbol, h_shares, price_override=price)
                if result.get('ok'):
                    profit = self._net_profit(h_shares, price, entry)
                    with self._stats_lock:
                        self.stats['total_harvests'] += 1
                        self.stats['total_profit']   += profit
                        self._position_harvests[symbol] = harvests_done + 1
                        if profit >= 0:
                            self.stats['wins'] += 1
                            self._consecutive_losses = 0
                        else:
                            self.stats['losses'] += 1
                            self._consecutive_losses += 1
                    note = '{} — STREAM-HARVEST: {:.0f}% ({} shares) @ ${:.4f} | net {:.1f}% | P&L ${:.2f}'.format(
                        symbol, frac * 100, h_shares, price, net_gain, profit)
                    self._entry('HARVEST', note)
                    from modules.notify import msg_harvest
                    self._notify(msg_harvest(symbol, frac * 100, net_gain, profit,
                                             self._position_harvests.get(symbol, 1)))
                    if profit > 0:
                        self._vault_deposit(symbol, profit, self._position_harvests.get(symbol, 1))
                    self._save()
            finally:
                self._stream_lock.release()
            return

        # 2b: stream stop-loss
        if net_gain > -max_loss:
            return
        if not self._stream_lock.acquire(blocking=False):
            return
        try:
            pos2 = {p['symbol']: p for p in self.paper.get_state().get('positions', [])}.get(symbol)
            if not pos2 or int(pos2.get('shares', 0)) <= 0:
                return
            result = self.paper.sell(symbol, shares, price_override=price)
            if result.get('ok'):
                loss = self._net_profit(shares, price, entry)
                self._cleanup_position(symbol)
                with self._stats_lock:
                    self.stats['total_profit'] += loss
                    self.stats['losses'] += 1
                    self.stats['total_loss_pct'] += abs((price - entry) / entry * 100)
                    self._consecutive_losses += 1
                note = '{} — STREAM-STOP: net {:.1f}% ≤ -{:.0f}% | {} shares @ ${:.4f} | P&L ${:.2f}'.format(
                    symbol, net_gain, max_loss, shares, price, loss)
                self._entry('STOP-LOSS', note)
                from modules.notify import msg_exit
                self._notify(msg_exit('STOP-LOSS', symbol,
                    'stream stop {:.1f}%'.format(net_gain), loss), 'alert')
                self._save()
        finally:
            self._stream_lock.release()

    def _orb_fast_buy(self, symbol: str, trigger_price: float, orb_high: float):
        """
        Stream-triggered ORB breakout buy.  Executes immediately when Polygon stream
        reports a price above the ORB high + 0.5% — no yfinance scan latency.
        Runs in a daemon thread; uses _orb_buy_lock to prevent concurrent entries.
        """
        if not self._orb_buy_lock.acquire(blocking=False):
            return
        try:
            # Session guard: ORB only valid during OPEN_MOMENTUM and STANDARD sessions
            session = _get_session()
            if session not in ('OPEN_MOMENTUM', 'STANDARD'):
                return

            # PDT guard — skipped in cash account mode
            if not self.config.get('cash_account_mode', False):
                account_value = self.paper.get_state().get('balance', 0)
                if account_value < 25_000:
                    now_date = (datetime.now(_ET) if _ET else datetime.now()).date()
                    window = _pdt_window_dates(now_date)
                    if sum(1 for ds in self._day_trades_log if ds in window) >= 3:
                        return

            # Capital guard
            balance       = self.paper.get_state().get('balance', 0)
            daily_limit   = float(self.config.get('daily_spend_limit', 100.0))
            remaining     = daily_limit - self._net_deployed_today()
            kelly         = self._kelly_size(balance)
            pos_cap       = balance * float(self.config.get('position_size_pct', 15.0)) / 100.0
            buy_amount    = min(kelly, remaining, balance, pos_cap)
            if buy_amount < 5:
                return

            # Price ceiling and floor
            if trigger_price < 0.50 or trigger_price >= 10.0:
                return

            # No existing position in this symbol
            state    = self.paper.get_state()
            existing = {p['symbol'] for p in state.get('positions', [])}
            if symbol in existing:
                return

            # Execute buy at stream price
            shares = max(1, int(buy_amount / trigger_price))
            result = self.paper.buy(symbol, shares, price_override=trigger_price)
            if result.get('ok'):
                cost = shares * trigger_price
                self.daily_spent += cost
                self.stats['total_trades'] += 1
                self._position_harvests[symbol]   = 0
                self._position_entry_rvol[symbol] = 2.0   # ORB entries are volume-confirmed by definition
                self._position_hwm[symbol]        = trigger_price
                self._position_opened[symbol]     = datetime.now()
                orb_pct = (trigger_price / orb_high - 1) * 100
                note = ('{} — STREAM-ORB: {} shares @ ${:.4f} | cost ${:.2f} | '
                        'ORB break +{:.1f}% above first-5m high ${:.3f}').format(
                    symbol, shares, trigger_price, cost, orb_pct, orb_high)
                self._entry('BUY', note)
                from modules.notify import msg_buy
                self._notify(msg_buy(symbol, shares, trigger_price, cost, 85))
                self._save()
        except Exception as e:
            self._entry('ERROR', 'ORB fast-buy {}: {}'.format(symbol, str(e)))
        finally:
            self._orb_buy_lock.release()

    # ── Helper Methods ─────────────────────────────────────────────────────────

    def _get_et_now(self):
        """Return current ET datetime. Extracted for testability."""
        return datetime.now(_ET) if _ET else datetime.now(timezone.utc) + timedelta(hours=-4)

    def _get_rvol(self, symbol):
        """Relative volume: today's (time-normalized) volume vs 5-day average.

        Today's daily bar is only a PARTIAL session early in the day, so comparing it raw
        to full-day averages understates RVOL at the open — exactly when morning-momentum
        entries fire. We project today's volume to a full session by the elapsed RTH
        fraction before taking the ratio.
        """
        try:
            hist = self._md.Ticker(symbol).history(period='5d', interval='1d')
            if len(hist) < 2:
                return 1.0
            today_vol = float(hist['Volume'].iloc[-1])
            avg_vol   = float(hist['Volume'].iloc[:-1].mean())
            if avg_vol <= 0:
                return 1.0
            now     = self._get_et_now()
            mins    = now.hour * 60 + now.minute
            elapsed = max(0, min(390, mins - 570))   # 570 = 9:30 ET, 390 = full RTH minutes
            frac    = elapsed / 390.0
            if 0 < frac < 1.0:
                today_vol = today_vol / max(frac, 0.05)   # projected full-day volume
            return round(today_vol / avg_vol, 2)
        except Exception:
            return 1.0

    def _kelly_size(self, balance):
        """
        Kelly Criterion position sizing.
        f* = (p*b - q) / b  where b = avg_win / avg_loss
        Clamped to 5%-25% of balance; falls back to config per_trade when history < 10.
        """
        wins   = self.stats.get('wins', 0)
        losses = self.stats.get('losses', 0)
        config_size = float(self.config.get('per_trade_capital', 50.0))
        # Harvesting wants MANY SMALL positions, so the Kelly fraction is clamped low (default
        # 4%-12% of balance, vs the old 5%-25%) — caps single-name exposure and spreads capital.
        f_min = float(self.config.get('kelly_min_frac', 0.04))
        f_max = float(self.config.get('kelly_max_frac', 0.12))
        if wins + losses < 10:
            return min(config_size, balance * f_max)

        p       = wins / (wins + losses)
        q       = 1 - p
        avg_win  = self.stats.get('total_win_pct', 0) / wins   if wins   > 0 else 7.0
        avg_loss = self.stats.get('total_loss_pct', 0) / losses if losses > 0 else 10.0
        b = avg_win / avg_loss if avg_loss > 0 else 1.5
        f = (p * b - q) / b
        f = max(f_min, min(f, f_max))
        return round(balance * f, 2)

    def _market_regime(self):
        """Return SPY % change from today's session open to current price (intraday resolution)."""
        try:
            spy = self._md.Ticker('SPY').history(period='1d', interval='5m')
            if spy.empty or len(spy) < 2:
                return 0.0
            now_et    = self._get_et_now()
            today_str = now_et.strftime('%Y-%m-%d')
            try:
                today_bars = spy[spy.index.strftime('%Y-%m-%d') == today_str]
            except Exception:
                today_bars = spy
            if len(today_bars) < 2:
                today_bars = spy
            session_open = float(today_bars['Open'].iloc[0])
            current      = float(today_bars['Close'].iloc[-1])
            return round((current - session_open) / session_open * 100, 2) if session_open > 0 else 0.0
        except Exception:
            return 0.0

    def _get_vwap(self, symbol):
        """Intraday VWAP. Reads from stream tick accumulator first (no network call)."""
        if self.engine and hasattr(self.engine, 'cache'):
            stream_vwap = self.engine.cache.get_intraday_vwap(symbol)
            if stream_vwap > 0:
                price = self._get_live_price(symbol)
                return round(stream_vwap, 4), round(price, 4)
        try:
            hist = self._md.Ticker(symbol).history(period='1d', interval='1m')
            if hist.empty or hist['Volume'].sum() == 0:
                return None, None
            tp = (hist['High'] + hist['Low'] + hist['Close']) / 3
            vwap = float((tp * hist['Volume']).sum() / hist['Volume'].sum())
            price = float(hist['Close'].iloc[-1])
            return round(vwap, 4), round(price, 4)
        except Exception:
            return None, None

    def _notify(self, message: str, level: str = 'info'):
        """Send notification if notifier is configured — never raises."""
        try:
            if self.notifier:
                self.notifier.send(message, level)
        except Exception:
            pass

    def _momentum_snapshot(self, symbol: str):
        """
        Fetch 1-min bars and return (slope_5bar_pct, cascade_bool, rvol).
        Cached per symbol per minute so 5 positions = 5 calls total per tick, not 25+.
        slope_5bar_pct: % change over last 5 bars (positive = uptrend)
        cascade: True when 3 consecutive red bars with volume > 1.5× avg minute vol
        rvol: last bar volume vs intraday avg minute volume
        """
        cache_key = '{}_{}'.format(symbol, datetime.now().strftime('%H:%M'))
        if cache_key in self._momentum_cache:
            return self._momentum_cache[cache_key]
        # Prefer real-time cache (no network call) over yfinance
        from_cache = self._momentum_from_cache(symbol)
        if from_cache is not None:
            self._momentum_cache[cache_key] = from_cache
            return from_cache
        result = (0.0, False, 1.0)
        try:
            hist = self._md.Ticker(symbol).history(period='1d', interval='1m')
            if len(hist) < 6:
                self._momentum_cache[cache_key] = result
                return result
            closes   = hist['Close'].values
            volumes  = hist['Volume'].values
            avg_vol  = float(volumes.mean()) if volumes.mean() > 0 else 1.0
            # 5-bar slope as % change
            slope = (closes[-1] - closes[-5]) / closes[-5] * 100 if closes[-5] > 0 else 0.0
            # Cascade: last 3 bars each closing lower than prior, with elevated avg volume
            cascade = (
                closes[-1] < closes[-2] < closes[-3] < closes[-4] and
                float(volumes[-3:].mean()) > avg_vol * 1.5
            )
            rvol = float(volumes[-1]) / avg_vol
            result = (round(slope, 3), cascade, round(rvol, 2))
        except Exception:
            pass
        self._momentum_cache[cache_key] = result
        return result

    def _plan_harvest(self, symbol, net_gain, harvests_done, days_open, session, mins_held):
        """Incline-aware adaptive harvest planner — the post-purchase monitor's brain.

        Decides, from the live INCLINE (5-bar slope confirmed by order-flow + volume), whether to:
          • RATCHET (sustained growth): take a thin slice and SCALE THE TRIGGER UP — re-arm the
            next harvest a step higher so a genuine runner keeps running instead of being clipped
            at a fixed %. This is the answer to 'scale upward when the incline shows sustained
            growth'.
          • HARVEST normally (fading/flat at the trigger): the standard adaptive fraction.
          • TIME-HARVEST (range-bound drift, no incline): extract small slices on a TIMER once the
            position is profitable-past-cost and has been idle — capture the predictable middle and
            recycle capital instead of waiting on a % that may never come.
        Returns {'action','fraction','ratchet_to','reason','incline'}."""
        import time as _t
        cfg = self.config
        base_trig = float(cfg.get('harvest_trigger_pct', 4.0))
        level = self._harvest_level.get(symbol, base_trig)        # armed trigger (may have ratcheted up)

        slope, cascade, rvol = self._momentum_snapshot(symbol)
        vel = self.engine.cache.velocity.get(symbol, {}) if (self.engine and hasattr(self.engine, 'cache')) else {}
        ofi = float(vel.get('ofi', 0.0))
        strong  = float(cfg.get('incline_strong_slope', 0.4))
        flat_th = float(cfg.get('incline_flat_slope', 0.12))
        incline_up   = slope >= strong and ofi >= 0 and rvol >= 1.0   # the incline predictor
        incline_flat = abs(slope) < flat_th

        if net_gain >= level:
            if incline_up and harvests_done < int(cfg.get('harvest_ratchet_max', 4)):
                step = float(cfg.get('harvest_ratchet_step', 2.0))
                frac = float(cfg.get('harvest_ratchet_fraction', 0.20))
                return {'action': 'harvest', 'fraction': frac, 'ratchet_to': round(net_gain + step, 2),
                        'reason': 'sustained incline (slope {:.2f}%, ofi {:+.2f}, rvol {:.1f}) → slice {:.0f}%, raise trigger to {:.1f}%'.format(
                            slope, ofi, rvol, frac * 100, net_gain + step),
                        'incline': round(slope, 3)}
            frac = self._harvest_fraction(symbol, days_open, harvests_done, session)
            return {'action': 'harvest', 'fraction': frac, 'ratchet_to': None,
                    'reason': 'at trigger, incline not sustained (slope {:.2f}%) → harvest {:.0f}%'.format(slope, frac * 100),
                    'incline': round(slope, 3)}

        if cfg.get('harvest_time_enabled', True) and incline_flat:
            cost = self._tx_cost(symbol)
            time_min = float(cfg.get('harvest_time_min', 45.0))
            last = self._last_time_harvest.get(symbol, 0.0)
            since = (_t.time() - last) / 60.0 if last else 1e9
            if net_gain > cost and mins_held >= time_min and since >= time_min:
                frac = float(cfg.get('harvest_time_fraction', 0.25))
                return {'action': 'time_harvest', 'fraction': frac, 'ratchet_to': None,
                        'reason': 'range-bound {:.0f}m (slope {:.2f}%), net {:.1f}% > cost {:.1f}% → time-slice {:.0f}%'.format(
                            mins_held, slope, net_gain, cost, frac * 100),
                        'incline': round(slope, 3)}
        return {'action': 'hold', 'fraction': 0.0, 'ratchet_to': None, 'reason': 'below trigger', 'incline': round(slope, 3)}

    def funnel_today(self):
        """Where candidates die this session — tallied from the live log for the North-Star tab.
        Ordered as the decision flow: scan → upstream vetoes → per-gate rejects → buys → harvests."""
        log = self.log
        start = 0
        for i in range(len(log) - 1, -1, -1):
            if 'New trading day' in str(log[i].get('note', '')):
                start = i
                break
        day = log[start:]
        def cnt(action, contains=None):
            n = 0
            for e in day:
                if e.get('action') == action and (contains is None or contains in str(e.get('note', ''))):
                    n += 1
            return n
        steps = [
            ('scans',        cnt('SCAN', 'Scanning'),        'scan cycles run'),
            ('macro_veto',   cnt('MACRO'),                   'blocked: macro severely adverse'),
            ('regime_veto',  cnt('REGIME'),                  'blocked: bearish SPY tape'),
            ('no_qualifying', cnt('SCAN', 'No qualifying') + cnt('SCAN', 'valid live price'), 'no candidate passed the bands'),
            ('rvol_gate',    cnt('RVOL-GATE'),               'rejected: RVOL'),
            ('signal_gate',  cnt('SIGNAL-GATE'),             'rejected: live composite'),
            ('score_gate',   cnt('SCORE-GATE'),              'rejected: combined score < min'),
            ('spread_gate',  cnt('SPREAD'),                  'rejected: spread too wide'),
            ('vwap_gate',    cnt('VWAP'),                    'rejected: above VWAP'),
            ('entry_skip',   cnt('ENTRY-SKIP'),              'skipped: cascade/slope/bad price'),
            ('predict_gate', cnt('PREDICT-GATE'),            'rejected: model P(win)'),
            ('pos_limit',    cnt('LIMIT'),                   'held: max positions / daily cap'),
            ('delayed_block', cnt('DELAYED-FEED'),           'blocked: delayed feed'),
            ('buys',         cnt('BUY'),                     'BUYS executed'),
            ('harvests',     cnt('HARVEST') + cnt('TIME-HARVEST'), 'harvests taken'),
        ]
        return [{'step': k, 'count': v, 'desc': d} for (k, v, d) in steps]

    def harvest_ladder(self):
        """Live harvest state per open position for the North-Star dashboard: each position's
        current net gain, its ARMED (possibly ratcheted) trigger, harvests already taken, incline,
        and the planner's next move — so the ratchet/time-harvest mechanism is watchable live."""
        out = []
        try:
            state = self.paper.get_state()
        except Exception:
            return out
        base_trig = float(self.config.get('harvest_trigger_pct', 4.0))
        try:
            session = _get_session()
        except Exception:
            session = ''
        for pos in state.get('positions', []):
            sym = pos.get('symbol')
            try:
                entry  = float(pos.get('avg_cost', 0))
                shares = int(pos.get('shares', 0))
                if entry <= 0:
                    continue
                price = self._get_live_price(sym) or float(pos.get('current_price', entry) or entry)
                gain  = (price - entry) / entry * 100.0
                tx    = self._tx_cost(sym)
                net   = gain - tx
                armed = self._harvest_level.get(sym, base_trig)
                hd    = self._position_harvests.get(sym, 0)
                opened = self._position_opened.get(sym)
                mins  = ((datetime.now() - opened).total_seconds() / 60.0) if opened else 0.0
                slope = self._momentum_snapshot(sym)[0]
                plan  = self._plan_harvest(sym, net, hd, 0, session, mins)
                out.append({'symbol': sym, 'shares': shares, 'net_gain': round(net, 2),
                            'armed_trigger': round(armed, 2), 'harvests': hd,
                            'mins_held': round(mins, 0), 'incline': slope,
                            'next_action': plan['action'], 'next_reason': plan.get('reason', '')})
            except Exception:
                continue
        return out

    def _harvest_fraction(self, symbol: str, days_open: int, harvests_done: int, session: str) -> float:
        """
        Dynamic sell fraction at harvest time (0.25 – 0.80).
        Strong momentum + first harvest → sell less, let the trade develop.
        Fading momentum / old position / late session → sell more, protect gains.
        """
        slope, cascade, rvol = self._momentum_snapshot(symbol)

        base = 0.50

        if slope > 0.3 and rvol > 2.0 and harvests_done == 0 and days_open == 0:
            base = 0.20   # strong first-harvest momentum — stay in the trade
        elif slope < 0.0 or rvol < 1.0:
            base = 0.65   # momentum fading — harvest more now

        # Each additional harvest increases aggressiveness (diminishing edge)
        base = base + harvests_done * 0.10

        # Late-day sessions: lock in more before close
        if session in ('CLOSE', 'AFTERNOON'):
            base = max(base, 0.65)

        return round(min(0.80, base), 2)

    def _should_exit_permanently(self, symbol: str, price: float,
                                  net_gain: float, days_open: int, harvests_done: int,
                                  session: str) -> tuple:
        """
        Multi-signal oracle: (True, reason) when full exit beats continuing to harvest.
        Checked before the standard harvest gate.
        """
        slope, cascade, rvol = self._momentum_snapshot(symbol)

        # 1. Exceptional gain — lock everything in, don't be greedy
        if net_gain >= 25.0:
            return True, 'Exceptional {:.1f}% gain — full exit'.format(net_gain)

        # 2. Multiple harvests done + price has fallen back below VWAP → rally spent
        if harvests_done >= 3:
            vwap, _ = self._get_vwap(symbol)
            if vwap and price < vwap:
                return True, '{} harvests + price below VWAP — rally exhausted'.format(harvests_done)

        # 3. Volume collapse on a profitable position → no fuel to continue
        entry_rvol = self._position_entry_rvol.get(symbol, 2.0)
        if rvol < 0.5 and entry_rvol > 1.5 and net_gain > 0:
            return True, 'Volume collapsed {:.1f}x (was {:.1f}x at entry) — exit {:.1f}% gain'.format(
                rvol, entry_rvol, net_gain)

        # 4. CLOSE session + any held position with net profit → don't carry overnight
        if session == 'CLOSE' and net_gain > 0:
            return True, 'CLOSE session, day-{} position — taking {:.1f}% gain, no overnight'.format(
                days_open + 1, net_gain)

        # 5. Momentum has reversed hard after 2+ harvests
        if harvests_done >= 2 and slope < -0.5:
            return True, '{} harvests + momentum reversed ({:.2f}%/bar) — final exit'.format(
                harvests_done, slope)

        return False, ''

    def _cleanup_position(self, symbol):
        """Remove all per-position tracking state after a close."""
        self._position_harvests.pop(symbol, None)
        self._position_hwm.pop(symbol, None)
        self._position_tx_cost.pop(symbol, None)
        self._harvest_level.pop(symbol, None)
        self._last_time_harvest.pop(symbol, None)
        opened = self._position_opened.pop(symbol, None)
        # Track PDT day trades — only relevant when NOT in cash account mode
        if opened and not self.config.get('cash_account_mode', False):
            today_str = (datetime.now(_ET) if _ET else datetime.now()).strftime('%Y-%m-%d')
            if opened.strftime('%Y-%m-%d') == today_str:
                self._day_trades_log.append(today_str)
                self._day_trades_log = self._day_trades_log[-60:]

    def _business_days_open(self, opened_dt: datetime) -> int:
        """Count Mon–Fri business days elapsed since opened_dt (not calendar days)."""
        opened = opened_dt.date() if hasattr(opened_dt, 'date') else opened_dt
        today  = (datetime.now(_ET) if _ET else datetime.now()).date()
        count  = 0
        d      = opened
        while d < today:
            if d.weekday() < 5:
                count += 1
            d += timedelta(days=1)
        return count

    def _blacklisted(self, sym):
        """True if a symbol is in the session entry-blacklist (and not yet expired)."""
        exp = self._entry_blacklist.get(sym)
        if not exp:
            return False
        try:
            now = self._get_et_now().timestamp()
        except Exception:
            return True
        if now >= exp:
            self._entry_blacklist.pop(sym, None)
            return False
        return True

    def _blacklist_entry(self, sym, minutes=20):
        """Cool a symbol off from new entries (invalid price / repeated buy failure) so one bad
        top-pick can't black-hole the day by being re-chosen every scan cycle."""
        try:
            self._entry_blacklist[sym] = self._get_et_now().timestamp() + minutes * 60
        except Exception:
            pass

    def _tx_cost(self, symbol):
        """Spread-aware round-trip cost % for an open position (captured at entry); falls back to
        the unified flat estimate if we never recorded one (e.g. position opened pre-upgrade)."""
        c = self._position_tx_cost.get(symbol)
        return c if c is not None else transaction_cost_pct(None, None, None, self.config)

    def _net_profit(self, shares: int, price: float, entry: float, symbol: str = None) -> float:
        """Net profit after subtracting round-trip transaction cost (spread-aware when known)."""
        gross  = shares * (price - entry)
        tx_pct = self._tx_cost(symbol) if symbol else transaction_cost_pct(None, entry, shares, self.config)
        tx     = shares * entry * tx_pct / 100.0
        return round(gross - tx, 2)

    def _daily_drawdown_pct(self, current_balance):
        """
        Current day's realized drawdown as % of day-open balance.
        Uses (cash + positions at avg_cost) so deploying capital doesn't
        false-trigger the floor — only actual losses do.
        """
        if self._daily_start_balance <= 0:
            return 0.0
        state = self.paper.get_state()
        pos_value = sum(p['shares'] * p['avg_cost'] for p in state.get('positions', []))
        total = current_balance + pos_value
        return max(0.0, (self._daily_start_balance - total) / self._daily_start_balance * 100)

    def _estimate_spread_pct(self, symbol):
        """
        Bid-ask spread as % of price.
        Priority: IBKR real-time quote (exact) → H-L/Close proxy from 1-min bars.
        Fails open (returns 0.0) so a timeout never silently blocks a trade.
        """
        if self.ibkr and self.ibkr.connected:
            try:
                q = self.ibkr.get_quote(symbol)
                bid, ask = float(q.get('bid', 0)), float(q.get('ask', 0))
                if bid > 0 and ask > 0:
                    return round((ask - bid) / ((ask + bid) / 2) * 100, 2)
            except Exception:
                pass
        try:
            hist = self._md.Ticker(symbol).history(period='1d', interval='1m').tail(10)
            if hist.empty or len(hist) < 3:
                return 0.0
            spread = ((hist['High'] - hist['Low']) / hist['Close'].replace(0, float('nan'))).mean() * 100
            return round(float(spread), 2)
        except Exception:
            return 0.0

    def _check_overnight_gap(self, symbol):
        """
        True if today's daily bar is >10% below yesterday's close (gap-down risk).
        Runs once per symbol per calendar day — subsequent calls return False.
        """
        today = _now_et().strftime('%Y-%m-%d')
        if today != self._gap_date:
            self._gapped_symbols = set()
            self._gap_date = today
        gap_key = '{}_{}'.format(symbol, today)
        if gap_key in self._gapped_symbols:
            return False
        self._gapped_symbols.add(gap_key)
        try:
            hist = self._md.Ticker(symbol).history(period='2d', interval='1d')
            if len(hist) < 2:
                return False
            prev  = float(hist['Close'].iloc[-2])
            today_close = float(hist['Close'].iloc[-1])
            return prev > 0 and (today_close - prev) / prev * 100 <= -10.0
        except Exception:
            return False

    def _entry(self, action, note):
        entry = {
            'time': datetime.now().strftime('%H:%M:%S'),
            'action': action,
            'note': note,
        }
        self.log.append(entry)

    def get_status(self):
        state   = self.paper.get_state()
        balance = state.get('balance', 0)
        return {
            'running':     self.running,
            'balance':     balance,
            'positions':   state.get('positions', []),
            'daily_spent': round(self._net_deployed_today(), 2),
            'daily_limit': round((state.get('balance', 0) * float(self.config.get('daily_spend_pct', 0)) / 100)
                           if float(self.config.get('daily_spend_pct', 0)) > 0
                           else float(self.config.get('daily_spend_limit', 100.0)), 2),
            'stats':       self.stats,
            'log':         list(reversed([e for e in self.log[-200:] if e.get('action') not in ('SESSION', 'SYSTEM')])),
            'guardrails': {
                'daily_pnl_floor_hit':    self._daily_pnl_floor_hit,
                'daily_drawdown_pct':     round(self._daily_drawdown_pct(balance), 1),
                'daily_start_balance':    round(self._daily_start_balance, 2),
                'consecutive_losses':     self._consecutive_losses,
                'max_consecutive_losses': int(self.config.get('max_consecutive_losses', 3)),
                'max_daily_loss_pct':     float(self.config.get('max_daily_loss_pct', 10.0)),
            },
            'pdt': {
                'cash_account_mode': bool(self.config.get('cash_account_mode', False)),
                'day_trades_this_week': sum(
                    1 for ds in self._day_trades_log
                    if ds in _pdt_window_dates(
                        (datetime.now(_ET) if _ET else datetime.now()).date()
                    )
                ),
                'limit': 'unlimited' if self.config.get('cash_account_mode') else 3,
                'rule':  'T+1 cash settlement — no PDT restriction' if self.config.get('cash_account_mode')
                         else 'FINRA Rule 4210 — max 3 day trades per 5-day window for accounts <$25k',
            },
        }
