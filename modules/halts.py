"""
HaltMonitor — Real-time FINRA/Nasdaq trading halt detection.

Polls the Nasdaq halt RSS feed every 30 seconds.
Tracks halted symbols and recently resumed symbols.

Resume plays: stocks that halt on the way up (LULD, T5) then resume
are high-probability momentum continuation entries.

Halt codes:
  T1   Halt - News Pending
  T2   Halt - News Released
  T5   Single Stock Circuit Breaker (LULD)
  T12  Halt - Additional Information Requested by Exchange
  LUDP Limit Up-Limit Down Pause
  LUDS Limit Up-Limit Down Straddle Condition
  M    Market-wide circuit breaker

Resume codes (entry opportunity if stock was halted going up):
  T3   Resume - News Dissemination
  T7   Resume - Single Stock Circuit Breaker Criteria Met
  T8   Resume - Single Stock Circuit Breaker Criteria Not Met
"""

import threading
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

_HALT_RSS = 'https://www.nasdaqtrader.com/dynamic/Rss/NasdaqHalts.xml'

_RESUME_CODES = {'T3', 'T7', 'T8'}
_BULLISH_HALT_CODES = {'T5', 'LUDP', 'LUDS'}   # circuit breaker = momentum surge

_HEADERS = {'User-Agent': 'StopTrading/1.0 buckkeely@gmail.com'}


class HaltMonitor:
    """
    Polls Nasdaq halt RSS every 30s.
    resume_plays: symbols halted upward then resumed — prime momentum entries.
    """

    def __init__(self):
        self._lock         = threading.Lock()
        self._halted       = {}   # symbol -> {code, time, reason}
        self._resume_plays = {}   # symbol -> datetime of resume (kept 30 min)
        self.running       = False
        self.last_poll     = None
        self.error_log     = []

    def start(self):
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self._poll()
            except Exception as e:
                self.error_log.append(str(e))
                self.error_log = self.error_log[-5:]
            time.sleep(30)

    def _poll(self):
        r = requests.get(_HALT_RSS, headers=_HEADERS, timeout=10)
        if r.status_code != 200:
            return
        root = ET.fromstring(r.content)
        now  = datetime.now()

        with self._lock:
            # Expire old resume plays (>30 min old)
            cutoff = now - timedelta(minutes=30)
            self._resume_plays = {
                s: t for s, t in self._resume_plays.items() if t > cutoff
            }

            for item in root.iter('item'):
                sym  = _text(item, 'Symbol') or _text(item, 'symbol', '')
                code = _text(item, 'ReasonCode') or _text(item, 'reasonCode', '')
                desc = _text(item, 'Reason') or _text(item, 'reason', '')
                halt_time_str = _text(item, 'HaltTime') or _text(item, 'haltTime', '')

                if not sym or not code:
                    continue
                sym = sym.strip().upper()

                if code in _RESUME_CODES:
                    # Check if this symbol was previously halted for a bullish reason
                    prev = self._halted.get(sym, {})
                    if prev.get('code') in _BULLISH_HALT_CODES:
                        self._resume_plays[sym] = now
                    self._halted.pop(sym, None)
                else:
                    self._halted[sym] = {
                        'code':   code,
                        'reason': desc,
                        'time':   halt_time_str or now.isoformat(),
                    }

        self.last_poll = now.isoformat()

    def is_halted(self, symbol):
        with self._lock:
            return symbol.upper() in self._halted

    def is_resume_play(self, symbol):
        """True if symbol recently resumed from a circuit-breaker halt — momentum entry."""
        with self._lock:
            return symbol.upper() in self._resume_plays

    def get_score(self, symbol):
        """
        +20 for resume play (circuit breaker halt → resumed = momentum confirmed).
        -50 if currently halted (never buy into a live halt).
        """
        sym = symbol.upper()
        with self._lock:
            if sym in self._halted:
                return -50
            if sym in self._resume_plays:
                return 20
        return 0

    def status(self):
        with self._lock:
            return {
                'halted':        list(self._halted.keys()),
                'resume_plays':  list(self._resume_plays.keys()),
                'last_poll':     self.last_poll,
                'errors':        self.error_log,
            }


def _text(element, tag, default=''):
    child = element.find(tag)
    return (child.text or default) if child is not None else default
