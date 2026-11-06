"""
Importer for Angel One "TradeBook And Charges" statements (.csv and .xlsx).

These are the only source of two things the live API cannot give:
  * history beyond today - orderBook()/tradeBook() are day-scoped;
  * charges - brokerage, GST, STT, stamp duty, which are a real cost of
    every trade and must come off the P&L.

Both file formats share one layout: a preamble of client code, date range and
charge totals, then a "TradeBook And Charges" header row, then one row per
fill, then a trailing NOTE. The header is located by name rather than by row
number, so a statement with a different preamble length still parses.
"""
import logging
import os
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.trade_log import (
    CHARGE_COLUMNS,
    HISTORICAL_SOURCE,
    LEDGER_COLUMNS,
)

logger = logging.getLogger(__name__)

HEADER_MARKER = "Scrip/Contract"

# Statement column -> ledger charge column.
CHARGE_MAP = {
    "Brokerage": "brokerage",
    "GST": "gst",
    "STT": "stt",
    "Sebi Tax": "sebi_tax",
    "Exchange Turnover Charges": "exchange_charges",
    "Stamp Duty": "stamp_duty",
    "Other Charges": "other_charges",
    "IPFT Charges": "ipft_charges",
}

# Statements name the scrip by AMC and fund description; the rest of the
# codebase uses NSE tickers (see src/etf.py). Only mappings that are certain
# are listed - an unmapped scrip keeps a name derived from the statement and is
# flagged in the import summary, because silently merging two different
# instruments under one symbol would corrupt the FIFO P&L.
#
# Add to this map rather than editing the ledger when a new scrip appears.
SCRIP_TO_SYMBOL = {
    "NIP IND ETF IT": "ITBEES",
    "NIP IND ETF GOLD BEES": "GOLDBEES",
    "NIP IND ETF INFRA BEES": "INFRABEES",
    "NIP IND ETF HANGSENG BEES": "HNGSNGBEES",
    "NIP IND ETF PSU BANK BEES": "PSUBNKBEES",
    "NIP IND ETF CONSUMPTION": "CONSUMBEES",
    "CPSE ETF": "CPSEETF",
    "MOTILAL OS MIDCAP100 ETF": "MOM100",
    "MOTILAL OS NASDAQ100 ETF": "MON100",
    "NIPPONAMC - NETFSILVER": "SILVERBEES",
    "NIPPONAMC - NETFAUTO": "AUTOBEES",
    "NIPPONAMC - NETFPHARMA": "PHARMABEES",
    "KOTAKMAMC - KOTAKALPHA": "ALPHA",
    "ICICIPRAMC - ICICIFMCG": "FMCGIETF",
    "ICICIPRAMC - ICICIMOM30": "MOM30IETF",
    "ICICIPRAMC - ICICICOMMO": "COMMOIETF",
    "ICICIPRAMC - BHARATIWIN": "ICICIB22",
    "ICICIPRAMC - METALIETF": "METALIETF",
    "HDFCAMC - HDFCSML250": "HDFCSML250",
    "MOTILALAMC - MOREALTY": "MOREALTY",
    "MOTILALAMC - MOVALUE": "MOVALUE",
    "MOTILALAMC - MODEFENCE": "MODEFENCE",
    "BIRLASLAMC - HEALTHY": "HEALTHY",
    "MIRAEAMC - MIDSMALL": "MIDSMALL",
    "MIRAEAMC - SMALLCAP": "SMALLCAP",
    "TATAAML - TNIDETF": "TNIDETF",
    "KOTAKMAMC - KOTAKMID50": "KOTAKMID50",
    "MIRAEAMC - MAFSETF": "MAFSETF",
    "MIRAEAMC - MAFANG": "MAFANG",
    "ICICIPRAMC - ICICIBANKP": "ICICIBANKP",
}


def map_scrip(scrip: str) -> Tuple[str, bool]:
    """
    Resolve a statement scrip name to a ticker. Returns (symbol, was_mapped).

    Unmapped names fall back to the segment after " - " (the AMC's own short
    code) or the whole name, uppercased. That keeps the same scrip consistent
    with itself across statements - which is what FIFO matching needs - while
    was_mapped=False surfaces it for review.
    """
    cleaned = " ".join(str(scrip or "").split()).strip()
    if not cleaned:
        return "", False
    if cleaned in SCRIP_TO_SYMBOL:
        return SCRIP_TO_SYMBOL[cleaned], True
    fallback = cleaned.split(" - ")[-1] if " - " in cleaned else cleaned
    return fallback.upper().replace(" ", ""), False


def _parse_statement_date(value) -> str:
    """
    Statement dates arrive as a datetime (xlsx) or "11/5/26 0:00" (csv).

    The CSV form is month/day/two-digit-year. %x-style guessing would read
    "11/5/26" as 5 November in one locale and 11 May in another, so the format
    is pinned explicitly.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.date().isoformat()
    text = str(value).strip()
    for fmt in ("%m/%d/%y %H:%M", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        logger.warning("Unparseable statement date: %r", value)
        return ""
    return parsed.date().isoformat()


def _read_raw(path: str) -> pd.DataFrame:
    """Read the whole sheet headerless, so the preamble can be skipped by name."""
    if path.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(path, header=None, sheet_name=0)
    return pd.read_csv(path, header=None, dtype=str, keep_default_na=False,
                       skip_blank_lines=False)


def parse_statement(path: str) -> pd.DataFrame:
    """Parse one statement file into raw trade rows (statement columns)."""
    raw = _read_raw(path)

    header_idx = None
    for idx in range(len(raw)):
        if str(raw.iloc[idx, 0]).strip() == HEADER_MARKER:
            header_idx = idx
            break
    if header_idx is None:
        raise ValueError(f"{path}: no '{HEADER_MARKER}' header row found - "
                         "is this an Angel One TradeBook And Charges export?")

    columns = [str(c).strip() for c in raw.iloc[header_idx].tolist()]
    body = raw.iloc[header_idx + 1:].copy()
    body.columns = columns

    # Rows after the table are a blank line and a "NOTE: ..." line.
    body = body[body["Scrip/Contract"].notna()]
    body = body[~body["Scrip/Contract"].astype(str).str.strip().isin(["", "nan"])]
    body = body[~body["Scrip/Contract"].astype(str).str.startswith("NOTE")]
    logger.info("%s: parsed %d trade rows.", os.path.basename(path), len(body))
    return body.reset_index(drop=True)


def statement_to_ledger(
    body: pd.DataFrame,
    source: str,
    since: Optional[date] = None,
) -> Tuple[pd.DataFrame, Dict]:
    """
    Convert parsed statement rows into ledger rows.

    Every row in these statements is an executed fill - the statement has no
    concept of a cancelled order - so qty_filled is the quantity and status is
    "complete". Side decides which of the two mutually exclusive price columns
    to read.
    """
    rows: List[Dict] = []
    unmapped: Dict[str, int] = {}
    skipped_before_cutoff = 0
    ingested_at = datetime.now().isoformat(timespec="seconds")

    for _, trade in body.iterrows():
        when = _parse_statement_date(trade.get("Date"))
        if since and when and date.fromisoformat(when) < since:
            skipped_before_cutoff += 1
            continue

        side = str(trade.get("Buy/Sell") or "").strip().upper()
        qty = pd.to_numeric(trade.get("Quantity"), errors="coerce")
        qty = 0.0 if pd.isna(qty) else float(qty)

        price_column = "Buy Price" if side == "BUY" else "Sell Price"
        price = pd.to_numeric(trade.get(price_column), errors="coerce")
        price = 0.0 if pd.isna(price) else float(price)

        charges = {}
        for statement_column, ledger_column in CHARGE_MAP.items():
            amount = pd.to_numeric(trade.get(statement_column), errors="coerce")
            charges[ledger_column] = 0.0 if pd.isna(amount) else float(amount)

        symbol, was_mapped = map_scrip(trade.get("Scrip/Contract"))
        if not was_mapped:
            unmapped[str(trade.get("Scrip/Contract")).strip()] = \
                unmapped.get(str(trade.get("Scrip/Contract")).strip(), 0) + 1

        rows.append({
            "date": when,
            "time": "",
            "symbol": symbol,
            "side": side,
            "order_source": HISTORICAL_SOURCE,
            "order_type": str(trade.get("Order Type") or "").upper(),
            "variety": "",
            "status": "complete",
            "qty_ordered": qty,
            "qty_filled": qty,
            "limit_price": 0.0,
            "trigger_price": 0.0,
            "avg_fill_price": round(price, 4),
            "value": round(qty * price, 2),
            **charges,
            "total_charges": round(sum(charges.values()), 2),
            "exchange": str(trade.get("Exchange") or ""),
            "producttype": str(trade.get("Order Type") or ""),
            "segment": str(trade.get("Segment") or ""),
            "ordertag": "",
            "orderid": str(trade.get("Order ID") or "").strip(),
            "tradeid": str(trade.get("Trade ID") or "").strip(),
            "uniqueorderid": "",
            "exchangeorderid": "",
            "fill_time": "",
            "update_time": "",
            "ingested_at": ingested_at,
            "source": source,
        })

    summary = {
        "parsed": len(body),
        "imported": len(rows),
        "skipped_before_cutoff": skipped_before_cutoff,
        "unmapped_scrips": unmapped,
    }
    return pd.DataFrame(rows, columns=LEDGER_COLUMNS), summary


def import_statements(
    paths: List[str],
    since: Optional[date] = None,
) -> Tuple[pd.DataFrame, List[Dict]]:
    """Parse and convert several statement files into one frame of ledger rows."""
    frames, summaries = [], []
    for path in paths:
        body = parse_statement(path)
        rows, summary = statement_to_ledger(body, os.path.basename(path), since)
        summary["file"] = os.path.basename(path)
        frames.append(rows)
        summaries.append(summary)
    if not frames:
        return pd.DataFrame(columns=LEDGER_COLUMNS), summaries
    return pd.concat(frames, ignore_index=True), summaries
