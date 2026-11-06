"""
Renders the trade ledger into trades/REPORT.md - the human-readable record of
what this strategy has actually made.

Written to be read by someone who did not write the code: every metric says
what it means, and anything unknown is shown as "n/a" with the reason rather
than as a zero that reads like a real number.
"""
import logging
import os
from datetime import date, datetime
from typing import Dict, List, Optional

import pandas as pd

from src.metrics import compute_metrics, realised_pnl_fifo
from src.trade_log import LEDGER_PATH

logger = logging.getLogger(__name__)

REPORT_PATH = os.path.join("trades", "REPORT.md")
RECENT_TRADES = 25


def _money(value: Optional[float], signed: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"₹{value:+,.0f}" if signed else f"₹{value:,.0f}"


def _pct(value: Optional[float]) -> str:
    """Rates are stored as fractions; XIRR of 0.182 renders as +18.2%."""
    return "n/a" if value is None else f"{value * 100:+.1f}%"


def _per_symbol(ledger: pd.DataFrame, holdings_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """One row per symbol: what went in, what came out, what is still held."""
    filled = ledger[ledger["qty_filled"] > 0]
    if filled.empty:
        return pd.DataFrame()

    _, realised = realised_pnl_fifo(ledger)
    realised_by_symbol = dict(zip(realised["symbol"], realised["realised_pnl"])) \
        if not realised.empty else {}

    held_qty, held_value = {}, {}
    if holdings_df is not None and not holdings_df.empty:
        for _, row in holdings_df.iterrows():
            held_qty[row["etf"]] = float(row["held_qty"])
            held_value[row["etf"]] = float(row["ltp"]) * float(row["held_qty"])

    rows = []
    for symbol, group in filled.groupby("symbol"):
        buys = group[group["side"] == "BUY"]
        sells = group[group["side"] == "SELL"]
        bought = float(buys["value"].sum())
        sold = float(sells["value"].sum())
        current = held_value.get(symbol, 0.0)
        rows.append({
            "Symbol": symbol,
            "Trades": len(group),
            "Bought": bought,
            "Sold": sold,
            "Held": held_qty.get(symbol, 0.0),
            "Value now": current,
            "Realised": realised_by_symbol.get(symbol, 0.0),
            "Net P&L": current + sold - bought,
            "Charges": float(group["total_charges"].sum()),
        })
    out = pd.DataFrame(rows).sort_values("Net P&L", ascending=False)
    return out.reset_index(drop=True)


def _markdown_table(headers: List[str], rows: List[List[str]], aligns: Optional[List[str]] = None) -> str:
    aligns = aligns or ["left"] * len(headers)
    separator = {"left": ":---", "right": "---:", "center": ":---:"}
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(separator[a] for a in aligns) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def render_report(
    ledger: pd.DataFrame,
    holdings_df: Optional[pd.DataFrame] = None,
    as_of: Optional[date] = None,
    cash: Optional[float] = None,
) -> str:
    as_of = as_of or date.today()
    m = compute_metrics(ledger, holdings_df, as_of)
    generated = datetime.now().strftime("%d %b %Y, %H:%M IST")

    parts: List[str] = []
    parts.append("# Trade record and performance\n")
    parts.append(
        f"Automatically regenerated after every market close. "
        f"Last updated **{generated}**.\n"
    )

    if ledger.empty:
        parts.append("\nNo trades recorded yet.\n")
        return "\n".join(parts)

    # ---------------- Headline ----------------
    pnl = m["total_pnl_net"] if m["total_pnl_net"] is not None else m["total_pnl"]
    if pnl is None:
        verdict = "Holdings unavailable, so the current position cannot be valued."
    elif pnl >= 0:
        verdict = f"**Up {_money(pnl)}** after charges."
    else:
        verdict = f"**Down {_money(abs(pnl))}** after charges."

    parts.append(f"\n## Where it stands\n\n{verdict}\n")
    parts.append(_markdown_table(
        ["Metric", "Value", "What it means"],
        [
            ["**XIRR**", f"**{_pct(m['xirr'])}**",
             "Annualised return that accounts for *when* each rupee went in. The headline number for a SIP."],
            ["CAGR", _pct(m["cagr"]),
             "Annualised growth ignoring contribution timing. Shown for comparison; XIRR is the more honest figure here."],
            ["Net P&L after charges", _money(m["total_pnl_net"], signed=True),
             "Everything sold, plus what is still held, minus everything bought and all brokerage and taxes."],
            ["Gross P&L", _money(m["total_pnl"], signed=True),
             "The same, before charges."],
            ["— of which realised", _money(m["realised_pnl"], signed=True),
             "Locked in by selling, matched FIFO (oldest lots first, as Indian capital-gains treatment assumes)."],
            ["— of which unrealised", _money(m["unrealised_pnl"], signed=True),
             "Still on the books, moving with the market."],
            ["Charges paid", _money(m["total_charges"]),
             "Brokerage, GST, STT, SEBI fees, stamp duty. Known only for trades imported from contract notes."],
            ["Currently invested", _money(m["current_value"]),
             "Market value of holdings right now."],
            ["Net deployed", _money(m["net_invested"]),
             "Total bought minus total sold."],
        ],
        ["left", "right", "left"],
    ))

    # ---------------- Activity ----------------
    parts.append("\n## Activity\n")
    cash_row = [["Idle cash", _money(cash)]] if cash is not None else []
    parts.append(_markdown_table(
        ["", "Value"],
        [
            ["Period covered", f"{m['first_trade']} → {m['last_trade']}"],
            ["Filled orders", f"{m['orders_filled']:,}"],
            ["Cancelled or rejected", f"{m['orders_cancelled_or_rejected']:,}"],
            ["Distinct ETFs traded", f"{m['symbols_traded']}"],
            ["Total bought", _money(m["buy_value"])],
            ["Total sold", _money(m["sell_value"])],
        ] + cash_row,
        ["left", "right"],
    ))

    # ---------------- Who placed what ----------------
    if m["by_source"]:
        parts.append("\n## Who placed each order\n")
        parts.append(
            "Attribution comes from the `ordertag` the strategy stamps on every "
            "order it places. Orders placed by hand in the Angel One app carry "
            "no tag, and trades imported from historical statements predate "
            "tagging entirely.\n"
        )
        labels = {
            "STRATEGY_BUY": "Strategy — daily buy",
            "STRATEGY_AVG_DOWN": "Strategy — averaging down",
            "STRATEGY_SELL": "Strategy — trailing-profit sell",
            "STRATEGY_SELL_STOPLOSS": "Strategy — stop-loss sell",
            "MANUAL_OR_UNTAGGED": "Manual (placed in the app)",
            "HISTORICAL_IMPORT": "Imported from broker statements",
        }
        parts.append(_markdown_table(
            ["Source", "Orders"],
            [[labels.get(k, k), f"{v:,}"]
             for k, v in sorted(m["by_source"].items(), key=lambda kv: -kv[1])],
            ["left", "right"],
        ))

    # ---------------- Per symbol ----------------
    per_symbol = _per_symbol(ledger, holdings_df)
    if not per_symbol.empty:
        parts.append("\n## By ETF\n")
        parts.append(
            "`Net P&L` is value-now plus everything sold, minus everything "
            "bought — so a fully exited position shows pure realised profit, "
            "and a held one includes its current paper gain.\n"
        )
        parts.append(_markdown_table(
            ["ETF", "Trades", "Bought", "Sold", "Held", "Value now", "Net P&L", "Charges"],
            [[
                str(r["Symbol"]), f"{int(r['Trades'])}",
                _money(r["Bought"]), _money(r["Sold"]),
                f"{int(r['Held'])}" if r["Held"] else "—",
                _money(r["Value now"]) if r["Value now"] else "—",
                _money(r["Net P&L"], signed=True), _money(r["Charges"]),
            ] for _, r in per_symbol.iterrows()],
            ["left", "right", "right", "right", "right", "right", "right", "right"],
        ))

    # ---------------- Recent trades ----------------
    filled = ledger[ledger["qty_filled"] > 0].copy()
    if not filled.empty:
        filled["_when"] = pd.to_datetime(filled["date"], errors="coerce")
        recent = filled.sort_values("_when", ascending=False).head(RECENT_TRADES)
        parts.append(f"\n## Last {len(recent)} filled trades\n")
        parts.append(_markdown_table(
            ["Date", "ETF", "Side", "Qty", "Price", "Value", "Source"],
            [[
                str(r["date"]), str(r["symbol"]), str(r["side"]),
                f"{int(r['qty_filled'])}", f"{r['avg_fill_price']:,.2f}",
                _money(r["value"]), str(r["order_source"]).replace("_", " ").title(),
            ] for _, r in recent.iterrows()],
            ["left", "left", "left", "right", "right", "right", "left"],
        ))

    # ---------------- Methodology ----------------
    parts.append(f"""
## How to read this

**XIRR** is the number to watch. It is the annual rate that makes all the
dated cashflows balance, so money added last week is not credited with a full
year of growth. For a SIP that is the only fair way to state a return.

**Net P&L** is computed without lot matching: everything paid out, everything
taken back, plus the market value of what is still held. It is exact.
Splitting it into realised and unrealised does need lot matching, which is
done FIFO.

**Charges** are the real cost of trading — {_money(m['total_charges'])} so far.
They are only present for trades imported from Angel One contract notes; the
live API does not report them, so recent automated trades currently show zero
charges and the true figure is slightly higher.

### Caveats

- Holdings are valued at the last traded price when the report was generated.
- A sell with no matching buy in the ledger (because the ledger starts mid-history)
  is counted in the net figures but cannot be attributed a cost basis, so the
  realised/unrealised split is approximate for those symbols.
- Nothing here is tax advice. FIFO matching happens to align with Indian
  capital-gains treatment, but this is not a tax statement.

---

*Generated by `trade_logger.py`. Source of truth is
[`trades/trades.csv`](trades.csv) — {len(ledger):,} order rows.*
""")
    return "\n".join(parts)


def write_report(
    ledger: pd.DataFrame,
    holdings_df: Optional[pd.DataFrame] = None,
    path: str = REPORT_PATH,
    as_of: Optional[date] = None,
    cash: Optional[float] = None,
) -> str:
    report = render_report(ledger, holdings_df, as_of, cash)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as handle:
        handle.write(report)
    logger.info("Wrote %s (%d chars).", path, len(report))
    return report
