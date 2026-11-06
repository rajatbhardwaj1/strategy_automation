"""
Portfolio performance metrics computed from the trade ledger.

Everything here is pure: it takes a ledger DataFrame (and optionally current
holdings) and returns numbers. No broker calls, no file IO - so it is cheap to
test and safe to re-run.

The ledger records orders, including cancelled and rejected ones. Only filled
quantity moves money, so every calculation here filters on qty_filled > 0.
"""
import logging
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

DAYS_PER_YEAR = 365.0


# ---------------------------------------------------------------------------
# XIRR
# ---------------------------------------------------------------------------
def _npv(rate: float, flows: Sequence[Tuple[date, float]], t0: date) -> float:
    """Net present value of dated cashflows at an annual `rate`."""
    total = 0.0
    for when, amount in flows:
        years = (when - t0).days / DAYS_PER_YEAR
        # (1+rate) can go non-positive during the search; the caller brackets
        # the root to keep us out of that region.
        total += amount / ((1.0 + rate) ** years)
    return total


def xirr(flows: Sequence[Tuple[date, float]], lo: float = -0.9999, hi: float = 10.0) -> Optional[float]:
    """
    Annualised internal rate of return for irregularly dated cashflows - the
    money-weighted return, which is the honest figure for a SIP where
    contributions arrive continuously.

    Sign convention: money leaving you is negative (a buy), money coming back
    is positive (a sell, plus the current portfolio value as a final inflow).

    Bisection rather than Newton: it cannot diverge, and a trade ledger
    regularly produces the flat or sign-heavy flow sets that make Newton
    oscillate. Returns None when no rate can explain the flows - fewer than two
    flows, or all flows the same sign, which has no root at all rather than one
    we failed to find.
    """
    flows = [(d, float(a)) for d, a in flows if a]
    if len(flows) < 2:
        return None
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        # All outflows (nothing sold, no holdings valued) or all inflows.
        return None

    flows = sorted(flows, key=lambda f: f[0])
    t0 = flows[0][0]

    f_lo, f_hi = _npv(lo, flows, t0), _npv(hi, flows, t0)
    if f_lo * f_hi > 0:
        # Root is outside the bracket: a return worse than -99.99% or better
        # than +1000% a year. Report nothing rather than a made-up number.
        logger.warning("XIRR root not bracketed in [%.4f, %.1f]; skipping.", lo, hi)
        return None

    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = _npv(mid, flows, t0)
        if abs(f_mid) < 1e-9:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def cagr(invested: float, current: float, years: float) -> Optional[float]:
    """
    Compound annual growth rate. Unlike XIRR this ignores the timing of
    contributions, so for a SIP it flatters or understates depending on when
    money arrived - reported alongside XIRR, not instead of it.
    """
    if invested <= 0 or current <= 0 or years <= 0:
        return None
    return (current / invested) ** (1.0 / years) - 1.0


# ---------------------------------------------------------------------------
# Realised P&L (FIFO)
# ---------------------------------------------------------------------------
def realised_pnl_fifo(ledger: pd.DataFrame) -> Tuple[float, pd.DataFrame]:
    """
    Match sells against the oldest open buy lots, per symbol, and return
    (total realised P&L, per-symbol breakdown).

    FIFO because that is what Indian tax treatment assumes, so these figures
    line up with a capital-gains statement. Sells with no matching buy lot -
    normal when the ledger starts mid-history, before the buys were recorded -
    are counted into `unmatched_qty` instead of being silently dropped or
    treated as pure profit.
    """
    filled = ledger[ledger["qty_filled"] > 0].copy()
    if filled.empty:
        return 0.0, pd.DataFrame(columns=["symbol", "realised_pnl", "qty_sold", "unmatched_qty"])

    filled["_when"] = pd.to_datetime(filled["date"], errors="coerce")
    filled = filled.sort_values(["_when", "orderid"], kind="mergesort")

    rows = []
    for symbol, group in filled.groupby("symbol", sort=True):
        lots: List[List[float]] = []  # [qty, price] FIFO queue
        realised = 0.0
        qty_sold = 0.0
        unmatched = 0.0

        for _, trade in group.iterrows():
            qty = float(trade["qty_filled"])
            price = float(trade["avg_fill_price"] or 0.0)
            if trade["side"] == "BUY":
                lots.append([qty, price])
                continue

            qty_sold += qty
            remaining = qty
            while remaining > 0 and lots:
                lot = lots[0]
                take = min(remaining, lot[0])
                realised += take * (price - lot[1])
                lot[0] -= take
                remaining -= take
                if lot[0] <= 1e-9:
                    lots.pop(0)
            if remaining > 0:
                unmatched += remaining

        rows.append({
            "symbol": symbol,
            "realised_pnl": round(realised, 2),
            "qty_sold": qty_sold,
            "unmatched_qty": unmatched,
        })

    breakdown = pd.DataFrame(rows)
    return round(float(breakdown["realised_pnl"].sum()), 2), breakdown


# ---------------------------------------------------------------------------
# Headline summary
# ---------------------------------------------------------------------------
def compute_metrics(
    ledger: pd.DataFrame,
    holdings_df: Optional[pd.DataFrame] = None,
    as_of: Optional[date] = None,
) -> Dict:
    """
    Headline numbers for the report.

    `current_value` comes from live holdings when available. Without it the
    unrealised side is unknown, so XIRR is not reported either rather than
    being computed against a portfolio valued at zero - which would read as a
    catastrophic loss instead of "not enough information".
    """
    as_of = as_of or date.today()
    out: Dict = {
        "as_of": as_of.isoformat(),
        "orders_total": int(len(ledger)),
        "orders_filled": 0,
        "orders_cancelled_or_rejected": 0,
        "charges_only_rows": 0,
        "buy_value": 0.0,
        "sell_value": 0.0,
        "net_invested": 0.0,
        "total_charges": 0.0,
        "current_value": None,
        "realised_pnl": 0.0,
        "unrealised_pnl": None,
        "total_pnl": None,
        "total_pnl_net": None,
        "xirr": None,
        "cagr": None,
        "first_trade": None,
        "last_trade": None,
        "symbols_traded": 0,
        "by_source": {},
    }
    if ledger.empty:
        return out

    out["by_source"] = ledger["order_source"].value_counts().to_dict()

    filled = ledger[ledger["qty_filled"] > 0].copy()
    out["orders_filled"] = int(len(filled))
    out["orders_cancelled_or_rejected"] = int(
        ledger["status"].astype(str).str.lower().isin(["cancelled", "rejected"]).sum()
    )
    # Broker charge lines: real cost, not an execution. Counted so the row
    # total on the report reconciles.
    out["charges_only_rows"] = int(
        (ledger["status"].astype(str).str.lower() == "charges_only").sum()
    )
    if filled.empty:
        return out

    filled["_when"] = pd.to_datetime(filled["date"], errors="coerce")
    filled = filled.dropna(subset=["_when"])
    if filled.empty:
        return out

    buys = filled[filled["side"] == "BUY"]
    sells = filled[filled["side"] == "SELL"]
    out["buy_value"] = round(float(buys["value"].sum()), 2)
    out["sell_value"] = round(float(sells["value"].sum()), 2)
    out["net_invested"] = round(out["buy_value"] - out["sell_value"], 2)
    # Charges are only known for trades imported from contract-note
    # statements; the live API does not report them, so this is a floor on the
    # true cost rather than the exact figure.
    out["total_charges"] = round(float(ledger["total_charges"].sum()), 2)
    out["symbols_traded"] = int(filled["symbol"].nunique())
    out["first_trade"] = filled["_when"].min().date().isoformat()
    out["last_trade"] = filled["_when"].max().date().isoformat()

    out["realised_pnl"], _ = realised_pnl_fifo(ledger)

    if holdings_df is not None and not holdings_df.empty:
        current = float((holdings_df["ltp"] * holdings_df["held_qty"]).sum())
        out["current_value"] = round(current, 2)
        # Total P&L needs no lot matching: everything paid out, everything
        # taken back, plus what is still on the books.
        out["total_pnl"] = round(current + out["sell_value"] - out["buy_value"], 2)
        out["total_pnl_net"] = round(out["total_pnl"] - out["total_charges"], 2)
        out["unrealised_pnl"] = round(out["total_pnl"] - out["realised_pnl"], 2)

        # Cashflows are what actually left and entered the bank: a buy costs
        # its value plus charges, a sell returns its value less charges. Using
        # gross values would overstate the return by the whole charge bill.
        flows: List[Tuple[date, float]] = []
        for _, trade in filled.iterrows():
            charges = float(trade.get("total_charges") or 0.0)
            if trade["side"] == "BUY":
                amount = -(float(trade["value"]) + charges)
            else:
                amount = float(trade["value"]) - charges
            flows.append((trade["_when"].date(), amount))
        flows.append((as_of, current))
        out["xirr"] = xirr(flows)

        years = (as_of - filled["_when"].min().date()).days / DAYS_PER_YEAR
        out["cagr"] = cagr(out["buy_value"], current + out["sell_value"], years)
    elif holdings_df is not None:
        # Explicitly held nothing, as opposed to "holdings unavailable".
        out["current_value"] = 0.0
        out["total_pnl"] = round(out["sell_value"] - out["buy_value"], 2)
        out["total_pnl_net"] = round(out["total_pnl"] - out["total_charges"], 2)
        out["unrealised_pnl"] = round(out["total_pnl"] - out["realised_pnl"], 2)

    return out
