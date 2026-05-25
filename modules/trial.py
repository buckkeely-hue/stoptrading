"""
10-Hour Trial Run Manager.

Runs a fully autonomous paper trading session and generates:
  - Hourly checkpoints (P&L, decisions, positions)
  - Flaw detection alerts (timing, liquidity, chasing, concentration risk)
  - External consensus comparison (StockTwits trending, Reddit r/pennystocks)
  - Final strategy analysis report

Flaws checked every 30 minutes:
  1. Market hours — penny stocks illiquid outside 9:30am-4pm ET
  2. Chasing — bought after >8% single-hour move (likely late entry)
  3. Thin liquidity — volume < 100K shares (hard to exit cleanly)
  4. Concentration risk — >60% of capital in one stock
  5. Consecutive losses — 3+ stop-losses without a winner
  6. Spread cost — 0.5% tx cost assumption may understate penny stock spreads
  7. Community divergence — we're buying what Reddit/StockTwits are SELLING
"""

import threading
import time
import json
import os
import requests
from datetime import datetime, timezone
from collections import defaultdict

TRIAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'trial.json')

# NYSE market hours (ET = UTC-4 or UTC-5, use simple offset)
MARKET_OPEN_HOUR  = 9   # 9:30 ET
MARKET_CLOSE_HOUR = 16  # 4:00 ET


class TrialManager:
    """Orchestrates a timed paper trading trial with analysis."""

    def __init__(self, autopilot, paper_trader, harvester, config):
        self.autopilot    = autopilot
        self.paper        = paper_trader
        self.harvester    = harvester
        self.config       = config
        self.running      = False
        self._thread      = None
        self._lock        = threading.Lock()
        self.duration_hrs = 10
        self.start_time   = None
        self.end_time     = None
        self.start_balance = 500.0
        self.checkpoints  = []
        self.flaws        = []
        self.comparisons  = []
        self.log          = []
        self._consecutive_losses = 0
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self):
        try:
            if os.path.exists(TRIAL_FILE):
                with open(TRIAL_FILE) as f:
                    d = json.load(f)
                self.checkpoints   = d.get('checkpoints', [])
                self.flaws         = d.get('flaws', [])
                self.comparisons   = d.get('comparisons', [])
                self.log           = d.get('log', [])
                self.running       = d.get('running', False)
                self.start_time    = d.get('start_time')
                self.end_time      = d.get('end_time')
                self.start_balance = d.get('start_balance', 500.0)
                self.duration_hrs  = d.get('duration_hrs', 10)

                # Trial expired while service was down
                if self.running and self.end_time:
                    if datetime.now().isoformat() >= self.end_time:
                        self.running = False
                        return

                # Trial was running — resume AutoPilot thread automatically
                if self.running:
                    self._add_log('TRIAL', 'Service restarted — resuming trial from saved state')
                    self.autopilot.resume()
                    self._thread = threading.Thread(target=self._loop, daemon=True)
                    self._thread.start()
        except Exception:
            pass

    def _save(self):
        try:
            with open(TRIAL_FILE, 'w') as f:
                json.dump({
                    'running':       self.running,
                    'start_time':    self.start_time,
                    'end_time':      self.end_time,
                    'start_balance': self.start_balance,
                    'duration_hrs':  self.duration_hrs,
                    'checkpoints':   self.checkpoints,
                    'flaws':         self.flaws[-100:],
                    'comparisons':   self.comparisons[-50:],
                    'log':           self.log[-500:],
                }, f, indent=2)
        except Exception:
            pass

    # ── Start / Stop ─────────────────────────────────────────────────────────

    def start(self, starting_balance=500.0, duration_hrs=10):
        if self.running:
            return {'error': 'Trial already running'}

        self.start_balance = starting_balance
        self.duration_hrs  = duration_hrs
        self.start_time    = datetime.now().isoformat()
        self.end_time      = datetime.fromtimestamp(
            time.time() + duration_hrs * 3600).isoformat()
        self.checkpoints   = []
        self.flaws         = []
        self.comparisons   = []
        self.log           = []
        self._consecutive_losses = 0
        self.running       = True

        # Reset paper account and start AutoPilot
        result = self.autopilot.start(starting_balance)
        if result.get('error'):
            self.running = False
            return result

        self._add_log('TRIAL', 'Trial started — ${:.0f} paper balance, {} hours, scanning every 5 min'.format(
            starting_balance, duration_hrs))
        self._add_log('TRIAL', 'Strategy: buy top momentum penny stock (<$5) · harvest at 10% net · exit at 5% margin · stop-loss -15% · $100/day cap')

        self._save()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return {'ok': True, 'end_time': self.end_time}

    def stop(self):
        self.running = False
        self.autopilot.stop()
        self._add_log('TRIAL', 'Trial stopped manually')
        self._save()
        return {'ok': True}

    # ── Monitor Loop ─────────────────────────────────────────────────────────

    def _loop(self):
        check_interval = 30 * 60  # 30 minutes
        last_check = 0

        while self.running:
            now = time.time()

            # End time reached
            if self.end_time and datetime.now().isoformat() >= self.end_time:
                self.running = False
                self.autopilot.stop()
                self._final_report()
                self._add_log('TRIAL', 'Trial complete — {} hours elapsed'.format(self.duration_hrs))
                self._save()
                break

            # 30-minute checkpoint
            if now - last_check >= check_interval:
                try:
                    self._checkpoint()
                    self._detect_flaws()
                    self._fetch_external_comparison()
                except Exception as e:
                    self._add_log('ERROR', 'Checkpoint failed: ' + str(e))
                last_check = now
                self._save()

            time.sleep(60)

    # ── Checkpoint ───────────────────────────────────────────────────────────

    def _checkpoint(self):
        state  = self.paper.get_state()
        balance = state.get('balance', 0)
        positions = state.get('positions', [])
        history   = state.get('history', [])
        ap_status = self.autopilot.get_status()

        elapsed_mins = self._elapsed_minutes()
        pnl = balance - self.start_balance
        pct = pnl / self.start_balance * 100 if self.start_balance > 0 else 0

        sells = [h for h in history if h.get('action') == 'SELL']
        buys  = [h for h in history if h.get('action') == 'BUY']

        cp = {
            'time':        datetime.now().strftime('%H:%M:%S'),
            'elapsed_min': round(elapsed_mins),
            'balance':     round(balance, 2),
            'pnl':         round(pnl, 2),
            'pnl_pct':     round(pct, 2),
            'positions':   len(positions),
            'total_buys':  len(buys),
            'total_sells': len(sells),
            'daily_spent': ap_status.get('daily_spent', 0),
        }
        self.checkpoints.append(cp)

        self._add_log('CHECK', '${:.2f} balance | P&L: {:+.2f} ({:+.1f}%) | {} positions | {} buys {} sells'.format(
            balance, pnl, pct, len(positions), len(buys), len(sells)))

    # ── Flaw Detection ───────────────────────────────────────────────────────

    def _detect_flaws(self):
        state    = self.paper.get_state()
        balance  = state.get('balance', 0)
        positions = state.get('positions', [])
        history   = state.get('history', [])
        ap_log    = list(reversed(self.autopilot.log[-50:]))

        flaws_this_round = []

        # 1. Market hours check
        hour_et = self._et_hour()
        if hour_et < 9 or hour_et >= 16:
            flaws_this_round.append({
                'severity': 'HIGH',
                'type':     'Market Hours',
                'detail':   'Current ET hour is {}:xx — NYSE is closed (9:30am–4pm). Penny stock prices are stale, spreads are wide, fills are unreliable. Algorithm should pause trading outside market hours.'.format(hour_et),
                'fix':      'Add market_hours_only gate to AutoPilot._tick() — skip buy logic if hour < 9 or hour >= 16 ET'
            })

        # 2. Concentration risk
        if positions and balance > 0:
            total_value = sum(p.get('market_value', 0) for p in positions)
            for p in positions:
                mv = p.get('market_value', 0)
                if total_value > 0 and mv / (total_value + balance) > 0.6:
                    flaws_this_round.append({
                        'severity': 'MEDIUM',
                        'type':     'Concentration Risk',
                        'detail':   '{} holds {:.0f}% of total capital (${:.2f}). A single bad stock can wipe the account.'.format(
                            p['symbol'], mv / (total_value + balance) * 100, mv),
                        'fix':      'Cap per-stock allocation at 30% of total capital. Diversify across 3–5 positions.'
                    })

        # 3. Chasing detection — did we buy after a big single-hour move?
        for h in history[-10:]:
            if h.get('action') == 'BUY':
                note = ''
                for entry in ap_log:
                    if h['symbol'] in entry.get('note', '') and 'BUY' in entry.get('action', ''):
                        note = entry.get('note', '')
                        break
                if '1h:+' in note:
                    try:
                        pct_str = note.split('1h:+')[1].split('%')[0]
                        if float(pct_str) > 8:
                            flaws_this_round.append({
                                'severity': 'MEDIUM',
                                'type':     'Chasing Move',
                                'detail':   'Bought {} after a +{:.1f}% single-hour move. Late entry on momentum plays often results in buying the top. Smart money already in — retail (us) is the exit liquidity.'.format(
                                    h['symbol'], float(pct_str)),
                                'fix':      'Add max_entry_move threshold: skip stocks already up >5% in current hour. Look for pre-breakout setups instead.'
                            })
                    except Exception:
                        pass

        # 4. Consecutive loss streak from AP log
        loss_streak = 0
        for entry in ap_log[:20]:
            if entry.get('action') == 'STOP-LOSS':
                loss_streak += 1
            elif entry.get('action') in ('HARVEST', 'EXIT'):
                break
        if loss_streak >= 2:
            flaws_this_round.append({
                'severity': 'HIGH',
                'type':     'Loss Streak',
                'detail':   '{} consecutive stop-losses detected. Market conditions may be unfavorable for momentum buying. Continuing to trade into a downtrend multiplies losses.'.format(loss_streak),
                'fix':      'Add drawdown circuit breaker: if 2+ stop-losses in 2 hours, pause new buys for 1 hour. Re-evaluate trend before re-entering.'
            })

        # 5. Thin volume / liquidity
        recent_buys = [h for h in history[-5:] if h.get('action') == 'BUY']
        for b in recent_buys:
            import yfinance as yf
            try:
                ticker = yf.Ticker(b['symbol'])
                vol = ticker.fast_info.three_month_average_volume or 0
                if 0 < vol < 500000:
                    flaws_this_round.append({
                        'severity': 'MEDIUM',
                        'type':     'Thin Liquidity',
                        'detail':   '{} has avg daily volume ~{:,} shares. Penny stocks with <500K avg volume have large bid-ask spreads — our 0.5% tx cost assumption understates actual slippage, which could be 2–5%.'.format(
                            b['symbol'], int(vol)),
                        'fix':      'Add minimum avg_volume filter of 1M shares/day. Higher liquidity = tighter spread = more realistic fills.'
                    })
            except Exception:
                pass

        # 6. Strategy structural flaw: harvest math
        harvest_trig = float(self.config.get('harvest_trigger_pct', 10.0))
        tx_cost      = float(self.config.get('tx_cost_pct', 0.5))
        if harvest_trig < tx_cost * 4:
            flaws_this_round.append({
                'severity': 'LOW',
                'type':     'Harvest Math',
                'detail':   'Harvest triggers at {:.0f}% but tx cost is {:.1f}% round-trip. With 4 harvests at {:.0f}%, total tx costs eat {:.1f}% of gains. Consider raising harvest trigger to {:.0f}% minimum.'.format(
                    harvest_trig, tx_cost, harvest_trig, tx_cost * 4, tx_cost * 6),
                'fix':      'Raise harvest_trigger_pct to at least 3x the tx_cost_pct to ensure meaningful profit extraction.'
            })

        # 7. Time-of-day pattern flaw
        if hour_et == 12 or hour_et == 13:
            flaws_this_round.append({
                'severity': 'LOW',
                'type':     'Midday Lull',
                'detail':   'Market is in the 12pm–1pm ET "dead zone". Penny stock volume and volatility typically collapse midday. Momentum signals are weaker and false breakouts are common.',
                'fix':      'Add time-weighted scoring: reduce position sizing by 50% between 11:30am–1:30pm ET. Most penny stock action occurs 9:30–11am and 3–4pm.'
            })

        # Add new flaws (deduplicate by type)
        existing_types = {f['type'] for f in self.flaws[-20:]}
        for flaw in flaws_this_round:
            if flaw['type'] not in existing_types:
                flaw['time'] = datetime.now().strftime('%H:%M:%S')
                self.flaws.append(flaw)
                self._add_log('FLAW', '[{}] {}: {}'.format(
                    flaw['severity'], flaw['type'], flaw['detail'][:80]))

    # ── External Comparison ───────────────────────────────────────────────────

    def _fetch_external_comparison(self):
        """Pull Reddit r/pennystocks hot + StockTwits trending and compare."""
        try:
            community_picks = {}

            # StockTwits trending
            r = requests.get(
                'https://api.stocktwits.com/api/2/trending/symbols.json',
                timeout=8)
            if r.status_code == 200:
                for sym in r.json().get('symbols', []):
                    s = sym.get('symbol', '').upper()
                    if s and len(s) <= 5:
                        community_picks[s] = community_picks.get(s, 0) + 2

            # Reddit r/pennystocks new posts (no API key — public JSON)
            headers = {'User-Agent': 'StopTrading-Trial/1.0'}
            r2 = requests.get(
                'https://www.reddit.com/r/pennystocks/hot.json?limit=25',
                headers=headers, timeout=8)
            if r2.status_code == 200:
                for post in r2.json().get('data', {}).get('children', []):
                    title = post['data'].get('title', '').upper()
                    ups   = post['data'].get('ups', 0)
                    # Extract 1-5 letter ticker symbols from title
                    import re
                    tickers = re.findall(r'\b[A-Z]{2,5}\b', title)
                    skip = {'THE', 'AND', 'FOR', 'WITH', 'THIS', 'THAT', 'FROM',
                            'INTO', 'HAVE', 'BEEN', 'WILL', 'WHAT', 'YOUR',
                            'NYSE', 'NASDAQ', 'ETF', 'CEO', 'IPO', 'SEC'}
                    for t in tickers:
                        if t not in skip:
                            community_picks[t] = community_picks.get(t, 0) + (1 + ups // 50)

            # Get our current positions
            state = self.paper.get_state()
            our_symbols = {p['symbol'] for p in state.get('positions', [])}
            ap_log = self.autopilot.log[-20:]
            our_recent_buys = {e['note'].split()[0] for e in ap_log
                               if e.get('action') == 'BUY' and e.get('note')}

            # Build comparison
            top_community = sorted(community_picks.items(), key=lambda x: x[1], reverse=True)[:15]
            community_symbols = {s for s, _ in top_community}

            overlap    = our_symbols & community_symbols
            we_own_not_community = our_symbols - community_symbols
            community_not_us = community_symbols - our_symbols - our_recent_buys

            comparison = {
                'time': datetime.now().strftime('%H:%M:%S'),
                'community_top': [{'symbol': s, 'mentions': m} for s, m in top_community],
                'overlap': list(overlap),
                'we_only': list(we_own_not_community),
                'community_only': list(community_not_us)[:8],
                'alignment_pct': round(len(overlap) / max(len(our_symbols), 1) * 100, 0) if our_symbols else 0,
            }
            self.comparisons.append(comparison)

            if overlap:
                self._add_log('COMPARE', 'Community agrees with {} of our picks: {}'.format(
                    len(overlap), ', '.join(overlap)))
            if community_not_us:
                self._add_log('COMPARE', 'Community hot picks we missed: {}'.format(
                    ', '.join(list(community_not_us)[:5])))
            if we_own_not_community:
                self._add_log('COMPARE', 'We hold stocks community is NOT talking about: {} — possible low-awareness early entries or noise'.format(
                    ', '.join(we_own_not_community)))

        except Exception as e:
            self._add_log('COMPARE', 'External comparison failed: ' + str(e))

    # ── Final Report ─────────────────────────────────────────────────────────

    def _final_report(self):
        state = self.paper.get_state()
        balance = state.get('balance', 0)
        pnl = balance - self.start_balance
        pct = pnl / self.start_balance * 100 if self.start_balance > 0 else 0

        ap = self.autopilot.stats
        sells = len([h for h in state.get('history', []) if h.get('action') == 'SELL'])
        buys  = len([h for h in state.get('history', []) if h.get('action') == 'BUY'])

        self._add_log('REPORT', '═══ TRIAL COMPLETE — {} HOUR RESULTS ═══'.format(self.duration_hrs))
        self._add_log('REPORT', 'Final Balance: ${:.2f} | Start: ${:.2f} | P&L: {:+.2f} ({:+.1f}%)'.format(
            balance, self.start_balance, pnl, pct))
        self._add_log('REPORT', 'Trades: {} buys, {} sells | Harvests: {} | Total profit realized: ${:.2f}'.format(
            buys, sells, ap.get('total_harvests', 0), ap.get('total_profit', 0)))
        self._add_log('REPORT', 'Flaws detected: {} | Critical: {} | Medium: {} | Low: {}'.format(
            len(self.flaws),
            len([f for f in self.flaws if f.get('severity') == 'HIGH']),
            len([f for f in self.flaws if f.get('severity') == 'MEDIUM']),
            len([f for f in self.flaws if f.get('severity') == 'LOW'])))

        # Strategy verdict
        if pct > 5:
            verdict = 'PROFITABLE — Strategy performed well in paper conditions. Validate with larger sample.'
        elif pct > 0:
            verdict = 'MARGINAL — Small gain. Transaction costs and slippage may eliminate profit in live trading.'
        elif pct > -10:
            verdict = 'LOSS — Strategy underperformed. Review flaw alerts before deploying real capital.'
        else:
            verdict = 'SIGNIFICANT LOSS — Strategy needs fundamental adjustment. Do not use with real money.'

        self._add_log('REPORT', 'Verdict: ' + verdict)

        if self.comparisons:
            avg_align = sum(c.get('alignment_pct', 0) for c in self.comparisons) / len(self.comparisons)
            self._add_log('REPORT', 'Community alignment: {:.0f}% avg — algorithm and crowd agreed on {:.0f}% of picks'.format(
                avg_align, avg_align))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _elapsed_minutes(self):
        if not self.start_time:
            return 0
        start = datetime.fromisoformat(self.start_time)
        return (datetime.now() - start).total_seconds() / 60

    def _et_hour(self):
        """Return current hour in US Eastern time, accounting for DST."""
        from datetime import timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        year = now_utc.year
        # DST: 2nd Sunday of March → 1st Sunday of November
        from datetime import date
        mar1 = date(year, 3, 1)
        dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
        nov1 = date(year, 11, 1)
        dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
        today = now_utc.date()
        offset = -4 if dst_start <= today < dst_end else -5
        return (now_utc.hour + offset) % 24

    def _add_log(self, action, note):
        self.log.append({
            'time':   datetime.now().strftime('%H:%M:%S'),
            'action': action,
            'note':   note,
        })

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self):
        state   = self.paper.get_state()
        balance = state.get('balance', 0)
        pnl     = round(balance - self.start_balance, 2)
        elapsed = self._elapsed_minutes()
        remaining = max(0, self.duration_hrs * 60 - elapsed)

        ap = self.autopilot.get_status()

        # Merge trial events + AutoPilot trading log into one unified feed
        trial_entries = [dict(e, source='trial') for e in self.log[-300:]]
        ap_entries    = [dict(e, source='ap')    for e in self.autopilot.log[-300:]]
        combined = trial_entries + ap_entries
        combined.sort(key=lambda e: e.get('time', ''), reverse=True)

        return {
            'running':         self.running,
            'start_time':      self.start_time,
            'end_time':        self.end_time,
            'elapsed_min':     round(elapsed),
            'remaining_min':   round(remaining),
            'duration_hrs':    self.duration_hrs,
            'start_balance':   self.start_balance,
            'current_balance': round(balance, 2),
            'pnl':             pnl,
            'pnl_pct':         round(pnl / self.start_balance * 100, 2) if self.start_balance > 0 else 0,
            'positions':       state.get('positions', []),
            'daily_spent':     ap.get('daily_spent', 0),
            'ap_stats':        ap.get('stats', {}),
            'checkpoints':     self.checkpoints,
            'flaws':           list(reversed(self.flaws)),
            'comparisons':     list(reversed(self.comparisons[:5])),
            'log':             combined[:200],
        }
