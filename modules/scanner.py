import yfinance as yf
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

PENNY_CANDIDATES = [
    'SNDL','CLOV','EXPR','BB','NOK','NAKD','SOFI','PLTR','NIO','WISH',
    'CTRM','SHIP','GNUS','ZOM','IDEX','SENS','BOXL','WKHS','NKLA','RKT',
    'ATOS','CLOV','OCGN','CIDM','BBIG','MMAT','EEENF','BFRI','ISPC','NUVB',
    'PRTY','SAVA','GOVX','MVIS','MOGO','NEXT','OBSV','PBTS','PHUN','PPBT',
    'RDBX','RLFTF','SFUN','SIGA','SOPA','SRGA','SURF','TPVG','TRVI','UONE',
    'VEON','VINC','VTNR','WEJO','XBIO','XERS','YCBD','YMAB','ZSAN','ZYXI',
    'ACST','ADTX','AGRX','AHPI','AIKI','AIOT','ALBT','ALDX','ALIM','ALPP',
    'AMMO','ANGH','ANVS','APGN','APRE','AQMS','AREC','ARGS','ARMP','ARNC',
    'AULT','AVCO','AVIR','AWRE','AXLA','AYRO','AZRX','BHAT','BKYI','BLBX',
    'BLNK','BLPH','BNET','BNGO','BNTC','BPMC','BRTX','BTBT','BTCS','BTTX',
    'BWAY','BYFC','CARV','CBAT','CBDD','CCEL','CCNC','CDAK','CELZ','CENN',
    'CERE','CERS','CETX','CGEM','CGIX','CHCI','CLEU','CLIN','CLPS','CLRB',
    'CMPX','CMRA','CNDB','CNET','CNFINANCE','COCP','CODX','COEP','COGT','COIN',
    'CPOP','CPTK','CPUH','CREG','CRESUD','CRKN','CRSA','CRVO','CRVS','CTXR',
    'CYCC','CYCN','CYNE','CYTO','DARE','DBGI','DCFC','DFFN','DKNG','DLPN',
    'DPRO','DRMA','DRRX','DTSS','DVAX','DWIN','DWSN','DYAI','EAST','EDSA'
]

def fetch_ticker(symbol):
    try:
        t = yf.Ticker(symbol)
        info = t.fast_info
        price = getattr(info, 'last_price', None)
        if price is None or price <= 0 or price > 5.0:
            return None
        prev = getattr(info, 'previous_close', price)
        change_pct = ((price - prev) / prev * 100) if prev else 0
        volume = getattr(info, 'three_month_average_volume', 0) or 0
        last_vol = getattr(info, 'last_volume', 0) or 0
        vol_spike = (last_vol / volume) if volume > 0 else 0
        score = _score(price, change_pct, vol_spike)
        return {
            'symbol': symbol,
            'price': round(price, 4),
            'change_pct': round(change_pct, 2),
            'volume': int(last_vol),
            'avg_volume': int(volume),
            'vol_spike': round(vol_spike, 2),
            'score': round(score, 1),
            'sentiment': 0
        }
    except Exception:
        return None

def _score(price, change_pct, vol_spike):
    score = 0
    if vol_spike >= 3:
        score += 40
    elif vol_spike >= 2:
        score += 25
    elif vol_spike >= 1.5:
        score += 15
    if change_pct >= 10:
        score += 30
    elif change_pct >= 5:
        score += 20
    elif change_pct >= 2:
        score += 10
    elif change_pct < -10:
        score -= 20
    if price < 1:
        score += 5
    elif price < 3:
        score += 10
    return max(0, min(100, score))

def run_scan(sentiment_cache=None):
    results = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(fetch_ticker, s): s for s in PENNY_CANDIDATES}
        for fut in as_completed(futures):
            r = fut.result()
            if r:
                if sentiment_cache and r['symbol'] in sentiment_cache:
                    r['sentiment'] = sentiment_cache[r['symbol']].get('score', 0)
                results.append(r)
    results.sort(key=lambda x: x['score'], reverse=True)
    return results
