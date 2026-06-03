"""
Per-transaction cost as a calculus — modeled from observable market microstructure, so it is
valued WITHOUT needing a live buy/sell mechanism.

Round-trip cost decomposes into four parts, ranked by how observable they are without a fill:

    commission%  — Alpaca equities = $0. Known a priori.
    reg_fee%     — SEC §31 + FINRA TAF, sell-side only. Pure formula from price×shares.
    spread%      — (ask−bid)/mid. You pay the FULL spread per round trip (buy at ask, sell at
                   bid). Observable NOW from the quote — the dominant penny-stock cost.
    slippage%    — your order walking a thin book. The ONLY fill-dependent unknown; bounded by a
                   conservative buffer now, calibrated later as (realized − modeled).

Cost is the HURDLE RATE the expected move must clear: take a trade only if expected_edge% > the
value returned here. One function, used by the harvester P&L, the predictor's EV sizing, and the
spread/harvest dynamics analysis — so every part of the system agrees on what a trade costs.
"""

# FINRA Trading Activity Fee (sell-side): $0.000166/share, capped at $8.30 per trade.
_TAF_PER_SHARE = 0.000166
_TAF_CAP       = 8.30
# SEC Section 31 fee (sell-side): ~$27.80 per $1,000,000 of principal ⇒ 0.00278% of notional.
_SEC_PCT       = 0.0000278


def reg_fee_pct(price, shares):
    """Regulatory fees as % of notional (sell-side). Exact when price/shares known."""
    try:
        price = float(price); shares = float(shares)
    except Exception:
        return 0.0
    notional = price * shares
    if notional <= 0:
        return 0.0
    taf = min(_TAF_PER_SHARE * shares, _TAF_CAP)
    sec = _SEC_PCT * notional
    return (taf + sec) / notional * 100.0


def transaction_cost_pct(spread_pct=None, price=None, shares=None, config=None):
    """Round-trip transaction cost, in percent. The single source of truth for trade cost.

    spread_pct : observed (ask−bid)/mid×100 at decision time; None/0 ⇒ estimate unavailable.
    price,shares: for the exact regulatory-fee term (falls back to a flat reg default if absent).
    config     : reads commission_pct, reg_fee_pct (fallback), slippage_buffer_pct, tx_cost_pct
                 (the conservative floor used when spread is unknown), and broker_execution."""
    cfg = config or {}
    commission = float(cfg.get('commission_pct', 0.0))          # Alpaca equities: $0
    slippage   = float(cfg.get('slippage_buffer_pct', 0.5))     # the fill-dependent unknown
    reg        = reg_fee_pct(price, shares) if (price and shares) else float(cfg.get('reg_fee_pct', 0.02))

    if cfg.get('broker_execution'):
        # Real fills (Alpaca paper or live) already embed the spread in the execution price, so
        # modeling it here too would double-count. Let slippage_buffer absorb the residual.
        return round(commission + reg + slippage, 3)
    if spread_pct and float(spread_pct) > 0:
        # Spread observed → the rigorous, decomposed cost (round-trip = full quoted spread).
        return round(commission + reg + float(spread_pct) + slippage, 3)
    # Spread unavailable → fall back to the flat configured total (backward-compatible floor),
    # so cost is never silently understated when we couldn't sample the quote.
    return float(cfg.get('tx_cost_pct', 1.5))
