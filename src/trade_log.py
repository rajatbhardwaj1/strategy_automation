"""
Append-only trade ledger, built from the broker's own order and trade books.

Why the broker's books rather than our own logs: the account is also traded
manually, and Angel One's order book covers every order on the account
regardless of who placed it. Reading it is the only way the ledger is complete.

Why append-only: `orderBook()` and `tradeBook()` are day-scoped - they return
today's activity and nothing else. A day that is never recorded is gone for
good, so the ledger is the archive and the fetch only ever adds to it.

Attribution ("who placed this order") comes from Angel's `ordertag`, which the
strategy now stamps on everything it places - see ORDER_TAGS in
src/angel_api.py. An untagged order is therefore one this code did not place:
a manual order, or one predating tagging. That is recorded honestly as
MANUAL_OR_UNTAGGED rather than guessed at from the order shape.
"""
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

LEDGER_DIR = "trades"
LEDGER_PATH = os.path.join(LEDGER_DIR, "trades.csv")

# The ledger's own schema, stable regardless of what the broker adds or
# renames. Column order is the CSV's column order.
LEDGER_COLUMNS = [
    "date", "time", "symbol", "side", "order_source", "order_type", "variety",
    "status", "qty_ordered", "qty_filled", "limit_price", "trigger_price",
    "avg_fill_price", "value",
    # Charges come from the broker's own contract-note statements, not the live
    # API, which does not report them. They are a real cost of every trade, so
    # P&L is reported both gross and net of them.
    "brokerage", "gst", "stt", "sebi_tax", "exchange_charges", "stamp_duty",
    "other_charges", "ipft_charges", "total_charges",
    "exchange", "producttype", "segment", "ordertag",
    "orderid", "tradeid", "uniqueorderid", "exchangeorderid",
    "fill_time", "update_time", "ingested_at", "source",
]

CHARGE_COLUMNS = [
    "brokerage", "gst", "stt", "sebi_tax", "exchange_charges", "stamp_duty",
    "other_charges", "ipft_charges",
]

NUMERIC_COLUMNS = [
    "qty_ordered", "qty_filled", "limit_price", "trigger_price",
    "avg_fill_price", "value", "total_charges",
] + CHARGE_COLUMNS

# ordertag -> order_source. Kept in sync with src/angel_api.ORDER_TAGS.
TAG_TO_SOURCE = {
    "SIPBUY": "STRATEGY_BUY",
    "AVGDOWN": "STRATEGY_AVG_DOWN",
    "TRAILSELL": "STRATEGY_SELL",
    "TRAILSL": "STRATEGY_SELL_STOPLOSS",
}
UNTAGGED_SOURCE = "MANUAL_OR_UNTAGGED"
# Imported broker statements predate order tagging, so nothing in them can be
# attributed to the strategy or to a manual order. Saying so is better than
# guessing from the order's shape.
HISTORICAL_SOURCE = "HISTORICAL_IMPORT"


def _to_float(value, default: float = 0.0) -> float:
    """Angel returns numbers as strings, empty strings, or nulls."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_angel_datetime(value: str) -> Tuple[str, str]:
    """
    Split Angel's "06-Nov-2026 23:52:42" into (ISO date, time).

    Returns ("", "") for the empty strings it uses when an order never reached
    the exchange, so a missing timestamp stays missing instead of silently
    becoming today.
    """
    if not value or not isinstance(value, str):
        return "", ""
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(value.strip(), fmt)
            return parsed.date().isoformat(), parsed.time().isoformat()
        except ValueError:
            continue
    logger.warning("Unparseable broker timestamp: %r", value)
    return "", ""


def classify_order(ordertag: Optional[str]) -> str:
    """Map an order's tag to its source. Untagged means not placed by us."""
    tag = (ordertag or "").strip().upper()
    if not tag:
        return UNTAGGED_SOURCE
    return TAG_TO_SOURCE.get(tag, f"OTHER_TAG:{tag}")


def ledger_key(row: Dict) -> str:
    """
    Stable identity for an order row, used to deduplicate across runs.

    `tradeid` is the broker's per-fill id and comes first, because a statement
    row is one fill and one order can fill many times. `uniqueorderid` is
    Angel's live-API UUID. Failing both, fall back to the order id and then to
    a composite of the trade's economics - enough to spot the same trade
    arriving twice from two overlapping exports, which is exactly what happens
    when statements are downloaded per financial year.
    """
    for field in ("tradeid", "uniqueorderid", "orderid"):
        value = str(row.get(field) or "").strip()
        # A missing CSV cell round-trips as NaN, and str(NaN) is "nan" - which
        # is truthy, so without this guard every reloaded row with no trade id
        # collapses onto the same key and the ledger deduplicates itself away.
        if value and value.lower() not in ("nan", "none", "<na>"):
            return value
    return "|".join(str(row.get(f) or "") for f in
                    ("date", "time", "symbol", "side", "qty_filled", "avg_fill_price"))


def normalise_orders(
    orderbook: Optional[List[Dict]],
    tradebook: Optional[List[Dict]] = None,
    source: str = "orderbook",
) -> pd.DataFrame:
    """
    Turn raw broker rows into ledger rows.

    The order book is the record: it carries the tag, the lifecycle status and
    cancelled/rejected orders, which the trade book has no concept of. The
    trade book is used only to correct fill price and quantity, since it is the
    authoritative record of what actually executed - an order book row can lag
    its fills.
    """
    if not orderbook:
        return pd.DataFrame(columns=LEDGER_COLUMNS)

    # orderid -> aggregated fills. tradeBook() returns None (not []) when empty.
    fills: Dict[str, Dict[str, float]] = {}
    for trade in (tradebook or []):
        oid = str(trade.get("orderid") or "").strip()
        if not oid:
            continue
        qty = _to_float(trade.get("fillsize"))
        price = _to_float(trade.get("fillprice"))
        entry = fills.setdefault(oid, {"qty": 0.0, "value": 0.0})
        entry["qty"] += qty
        entry["value"] += qty * price

    ingested_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    for order in orderbook:
        order_date, order_time = _parse_angel_datetime(order.get("updatetime", ""))
        fill_date, fill_time = _parse_angel_datetime(order.get("filltime", ""))
        exch_date, _ = _parse_angel_datetime(order.get("exchtime", ""))

        qty_filled = _to_float(order.get("filledshares"))
        avg_price = _to_float(order.get("averageprice"))

        oid = str(order.get("orderid") or "").strip()
        if oid in fills and fills[oid]["qty"] > 0:
            qty_filled = fills[oid]["qty"]
            avg_price = fills[oid]["value"] / fills[oid]["qty"]

        # Prefer the exchange's own date for when the trade happened; fall back
        # to the order's last update, which is all a cancelled order has.
        effective_date = exch_date or fill_date or order_date

        rows.append({
            "date": effective_date,
            "time": fill_time or order_time,
            # Strip Angel's "-EQ" suffix so symbols match the rest of the
            # codebase (NIFTYBEES, not NIFTYBEES-EQ).
            "symbol": str(order.get("tradingsymbol") or "").replace("-EQ", "").strip(),
            "side": str(order.get("transactiontype") or "").upper(),
            "order_source": classify_order(order.get("ordertag")),
            "order_type": str(order.get("ordertype") or "").upper(),
            "variety": str(order.get("variety") or "").upper(),
            "status": str(order.get("orderstatus") or order.get("status") or "").lower(),
            "qty_ordered": _to_float(order.get("quantity")),
            "qty_filled": qty_filled,
            "limit_price": _to_float(order.get("price")),
            "trigger_price": _to_float(order.get("triggerprice")),
            "avg_fill_price": round(avg_price, 4),
            "value": round(qty_filled * avg_price, 2),
            # The live API does not report charges; they arrive later via the
            # statement importer, which upserts onto these rows.
            **{column: 0.0 for column in CHARGE_COLUMNS},
            "total_charges": 0.0,
            "exchange": str(order.get("exchange") or ""),
            "producttype": str(order.get("producttype") or ""),
            "segment": "",
            "ordertag": str(order.get("ordertag") or ""),
            "orderid": oid,
            "tradeid": "",
            "uniqueorderid": str(order.get("uniqueorderid") or ""),
            "exchangeorderid": str(order.get("exchangeorderid") or ""),
            "fill_time": order.get("filltime") or "",
            "update_time": order.get("updatetime") or "",
            "ingested_at": ingested_at,
            "source": source,
        })

    return pd.DataFrame(rows, columns=LEDGER_COLUMNS)


def load_ledger(path: str = LEDGER_PATH) -> pd.DataFrame:
    """Read the ledger, or an empty one shaped correctly if it does not exist."""
    if not os.path.exists(path):
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    id_columns = ["orderid", "tradeid", "uniqueorderid", "exchangeorderid"]
    ledger = pd.read_csv(path, dtype={column: str for column in id_columns},
                         keep_default_na=False, na_values=[])
    # Tolerate a ledger written before a column existed.
    for column in LEDGER_COLUMNS:
        if column not in ledger.columns:
            ledger[column] = ""
    for column in NUMERIC_COLUMNS:
        ledger[column] = pd.to_numeric(ledger[column], errors="coerce").fillna(0.0)
    return ledger[LEDGER_COLUMNS]


def merge_into_ledger(
    new_rows: pd.DataFrame,
    path: str = LEDGER_PATH,
) -> Tuple[pd.DataFrame, int, int]:
    """
    Merge rows into the ledger on disk. Returns (ledger, added, updated).

    Upsert rather than blind append: a mid-session run can see an order as
    "open" that a later run sees as "complete", and the ledger should end up
    with the final state, not both. Identity is ledger_key(), so re-running
    the same day is harmless - which matters because this runs unattended and
    may be retried.
    """
    existing = load_ledger(path)
    if new_rows.empty:
        return existing, 0, 0

    new_rows = new_rows.reindex(columns=LEDGER_COLUMNS)
    existing["_key"] = existing.apply(lambda r: ledger_key(r.to_dict()), axis=1)
    new_rows = new_rows.copy()
    new_rows["_key"] = new_rows.apply(lambda r: ledger_key(r.to_dict()), axis=1)

    known = set(existing["_key"])
    incoming_keys = set(new_rows["_key"])
    added = len(incoming_keys - known)
    updated = len(incoming_keys & known)

    # Last write wins for a key already present; concat-then-drop keeps the
    # new row because it is later in the frame. An empty `existing` is skipped
    # rather than concatenated, since concatenating all-NA frames is
    # deprecated and changes the resulting dtypes.
    frames = [frame for frame in (existing, new_rows) if not frame.empty]
    merged = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0].copy()
    merged = merged.drop_duplicates(subset="_key", keep="last")
    merged = merged.drop(columns="_key")

    # Chronological, with undated rows (never reached the exchange) last.
    merged["_when"] = pd.to_datetime(merged["date"], errors="coerce")
    merged = merged.sort_values("_when", na_position="last", kind="mergesort")
    merged = merged.drop(columns="_when").reset_index(drop=True)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    merged.to_csv(path, index=False)
    logger.info("Ledger %s: %d rows (%d added, %d updated)",
                path, len(merged), added, updated)
    return merged, added, updated


def fetch_broker_books(api) -> Tuple[List[Dict], List[Dict]]:
    """
    Read today's order book and trade book. Read-only: nothing here places,
    modifies or cancels anything.
    """
    orderbook = api.api.orderBook().get("data") or []
    tradebook = api.api.tradeBook().get("data") or []
    logger.info("Fetched %d orders, %d trades from the broker.",
                len(orderbook), len(tradebook))
    return orderbook, tradebook
