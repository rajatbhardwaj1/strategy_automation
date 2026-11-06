"""
Unit tests for the trade ledger: broker-book normalisation, order attribution,
append-only merging, and statement import.

Missing broker/market SDKs are stubbed by test/conftest.py.
"""
import pandas as pd
import pytest

from src.trade_import import map_scrip, statement_to_ledger
from src.trade_log import (
    UNTAGGED_SOURCE,
    classify_order,
    ledger_key,
    merge_into_ledger,
    normalise_orders,
)


def _order(**overrides):
    """An order book row shaped like Angel One's actual response."""
    base = {
        "variety": "NORMAL", "ordertype": "LIMIT", "producttype": "DELIVERY",
        "price": 250.01, "triggerprice": 0.0, "quantity": "1",
        "tradingsymbol": "SILVERBEES-EQ", "transactiontype": "BUY",
        "exchange": "NSE", "ordertag": "SIPBUY", "averageprice": 250.0,
        "filledshares": "1", "orderid": "1106b6274f31AO", "status": "complete",
        "orderstatus": "complete", "updatetime": "06-Nov-2026 23:52:42",
        "exchtime": "06-Nov-2026 23:52:40", "filltime": "", "fillid": "",
        "uniqueorderid": "9032b9c4-9466-4899-8b6f-d05a296aa137",
        "exchangeorderid": "",
    }
    base.update(overrides)
    return base


# ---------------------------
# Attribution
# ---------------------------
def test_strategy_tags_map_to_sources():
    assert classify_order("SIPBUY") == "STRATEGY_BUY"
    assert classify_order("TRAILSELL") == "STRATEGY_SELL"
    assert classify_order("TRAILSL") == "STRATEGY_SELL_STOPLOSS"
    assert classify_order("sipbuy") == "STRATEGY_BUY"  # case insensitive


def test_untagged_order_is_reported_as_manual_not_guessed():
    """
    An order with no tag was not placed by this code. Saying so is the point of
    the column - inferring "probably strategy" from the order shape would make
    the manual/automated split meaningless.
    """
    assert classify_order("") == UNTAGGED_SOURCE
    assert classify_order(None) == UNTAGGED_SOURCE
    assert classify_order("   ") == UNTAGGED_SOURCE


def test_unrecognised_tag_is_preserved_rather_than_dropped():
    assert classify_order("SOMETHINGELSE") == "OTHER_TAG:SOMETHINGELSE"


# ---------------------------
# Normalisation
# ---------------------------
def test_normalise_extracts_the_economics_and_strips_the_eq_suffix():
    row = normalise_orders([_order()]).iloc[0]
    assert row["symbol"] == "SILVERBEES"
    assert row["side"] == "BUY"
    assert row["order_source"] == "STRATEGY_BUY"
    assert row["qty_filled"] == 1
    assert row["avg_fill_price"] == 250.0
    assert row["value"] == 250.0
    assert row["date"] == "2026-11-06"


def test_cancelled_order_is_recorded_with_zero_filled_value():
    row = normalise_orders([_order(status="cancelled", orderstatus="cancelled",
                                  filledshares="0", averageprice=0.0,
                                  exchtime="")]).iloc[0]
    assert row["status"] == "cancelled"
    assert row["qty_filled"] == 0
    assert row["value"] == 0
    # Falls back to updatetime when the order never reached the exchange.
    assert row["date"] == "2026-11-06"


def test_tradebook_fills_override_the_order_book():
    """
    The trade book is authoritative for what executed; an order book row can
    lag its fills.
    """
    tradebook = [{"orderid": "1106b6274f31AO", "fillsize": "1", "fillprice": "249.5"}]
    row = normalise_orders([_order(averageprice=0.0, filledshares="0")], tradebook).iloc[0]
    assert row["qty_filled"] == 1
    assert row["avg_fill_price"] == pytest.approx(249.5)


def test_tradebook_none_is_tolerated():
    """Angel returns data=None, not [], when there are no trades."""
    assert len(normalise_orders([_order()], None)) == 1
    assert normalise_orders(None).empty


def test_unparseable_timestamp_stays_empty_rather_than_becoming_today():
    row = normalise_orders([_order(updatetime="garbage", exchtime="", filltime="")]).iloc[0]
    assert row["date"] == ""


# ---------------------------
# Merge / dedupe
# ---------------------------
def test_merge_is_append_only_and_idempotent(tmp_path):
    path = str(tmp_path / "trades.csv")
    rows = normalise_orders([_order()])

    _, added, updated = merge_into_ledger(rows, path)
    assert (added, updated) == (1, 0)

    # Re-running the same day must not duplicate - this runs unattended and
    # may be retried.
    ledger, added, updated = merge_into_ledger(rows, path)
    assert (added, updated) == (0, 1)
    assert len(ledger) == 1


def test_merge_upserts_a_changed_status(tmp_path):
    path = str(tmp_path / "trades.csv")
    merge_into_ledger(normalise_orders([_order(status="open", orderstatus="open",
                                               filledshares="0")]), path)
    ledger, _, _ = merge_into_ledger(normalise_orders([_order()]), path)
    assert len(ledger) == 1
    assert ledger.iloc[0]["status"] == "complete"
    assert ledger.iloc[0]["qty_filled"] == 1


def test_merge_keeps_existing_rows_when_new_data_arrives(tmp_path):
    path = str(tmp_path / "trades.csv")
    merge_into_ledger(normalise_orders([_order()]), path)
    ledger, added, _ = merge_into_ledger(
        normalise_orders([_order(orderid="OTHER", uniqueorderid="other-uuid",
                                 tradingsymbol="GOLDBEES-EQ")]), path)
    assert added == 1
    assert set(ledger["symbol"]) == {"SILVERBEES", "GOLDBEES"}


def test_ledger_key_prefers_tradeid_then_falls_back():
    assert ledger_key({"tradeid": "T1", "uniqueorderid": "U1", "orderid": "O1"}) == "T1"
    assert ledger_key({"tradeid": "", "uniqueorderid": "U1", "orderid": "O1"}) == "U1"
    assert ledger_key({"orderid": "O1"}) == "O1"
    # With no identifiers at all, the trade's economics identify it.
    composite = ledger_key({"date": "2026-01-01", "symbol": "X", "side": "BUY",
                            "qty_filled": 10, "avg_fill_price": 5.0})
    assert "X" in composite and "BUY" in composite


# ---------------------------
# Statement import
# ---------------------------
def test_scrip_mapping_resolves_known_names_and_flags_the_rest():
    assert map_scrip("NIP IND ETF IT") == ("ITBEES", True)
    assert map_scrip("NIPPONAMC - NETFSILVER") == ("SILVERBEES", True)
    # Unknown: falls back to the AMC's own code and reports it was not mapped,
    # so it cannot silently merge with a different instrument.
    assert map_scrip("SOMEAMC - NEWTHING") == ("NEWTHING", False)


def test_truncated_xlsx_scrip_resolves_to_the_same_symbol_as_the_full_name():
    """
    Angel's .xlsx export truncates some scrip names at 20 characters and leaves
    others intact, so the same ETF appears twice in one file. Both spellings
    must land on one symbol or FIFO matches sells against the wrong lots.
    """
    assert map_scrip("NIP IND ETF GOLD BEES") == ("GOLDBEES", True)
    assert map_scrip("NIP IND ETF GOLD BEE") == ("GOLDBEES", True)
    assert map_scrip("NIPPONAMC - NETFSILVER") == ("SILVERBEES", True)
    assert map_scrip("NIPPONAMC - NETFSILV") == ("SILVERBEES", True)
    assert map_scrip("MOTILALAMC - MOREALT") == ("MOREALTY", True)


def test_ambiguous_prefix_is_left_unmapped_rather_than_guessed():
    # "KOTAKMAMC - KOTAK" prefixes KOTAKALPHA, KOTAKMID50 and KOTAKMNC.
    symbol, mapped = map_scrip("KOTAKMAMC - KOTAK")
    assert mapped is False


def _statement_rows():
    return pd.DataFrame([
        {"Scrip/Contract": "NIP IND ETF IT", "Buy/Sell": "Buy", "Buy Price": 31.37,
         "Sell Price": None, "Quantity": 222, "Brokerage": 6.96, "GST": 1.29,
         "STT": 0, "Sebi Tax": 0.01, "Exchange Turnover Charges": 0.21,
         "Stamp Duty": 1, "Other Charges": 0, "IPFT Charges": 0,
         "Order Type": "Delivery", "Segment": "CAPITAL", "Exchange": "NSE",
         "Order ID": "1100000086330564", "Trade ID": "211640469",
         "Date": "9/24/26 0:00"},
        {"Scrip/Contract": "TATAAML - TNIDETF", "Buy/Sell": "Sell", "Buy Price": None,
         "Sell Price": 91.0, "Quantity": 243, "Brokerage": 20, "GST": 3.73,
         "STT": 0.64, "Sebi Tax": 0.02, "Exchange Turnover Charges": 0.68,
         "Stamp Duty": 0, "Other Charges": 0, "IPFT Charges": 0,
         "Order Type": "Delivery", "Segment": "CAPITAL", "Exchange": "NSE",
         "Order ID": "1300000059566785", "Trade ID": "606814433",
         "Date": "11/5/26 0:00"},
    ])


def test_statement_import_reads_the_side_specific_price_and_sums_charges():
    rows, summary = statement_to_ledger(_statement_rows(), "test.csv")
    assert summary["imported"] == 2

    buy = rows[rows["side"] == "BUY"].iloc[0]
    assert buy["symbol"] == "ITBEES"
    assert buy["avg_fill_price"] == pytest.approx(31.37)
    assert buy["value"] == pytest.approx(222 * 31.37, abs=0.01)
    assert buy["total_charges"] == pytest.approx(9.47, abs=0.01)
    # Statement rows are executed fills by definition.
    assert buy["status"] == "complete"
    assert buy["qty_filled"] == 222

    sell = rows[rows["side"] == "SELL"].iloc[0]
    assert sell["symbol"] == "TNIDETF"
    assert sell["avg_fill_price"] == pytest.approx(91.0)


def test_statement_dates_are_month_first():
    """
    "11/5/26" is 5 November, not 11 May - locale-dependent parsing would put
    half the ledger in the wrong month.
    """
    rows, _ = statement_to_ledger(_statement_rows(), "test.csv")
    assert set(rows["date"]) == {"2026-09-24", "2026-11-05"}


def test_charge_only_line_is_labelled_and_keyed_apart_from_its_fill():
    """
    Angel emits one charge line per order: zero qty, zero price, no Trade ID,
    sharing the order id with the real fill. Without a distinct key the two
    collide and one overwrites the other; without the label it looks like a
    phantom trade.
    """
    rows = pd.DataFrame([
        {"Scrip/Contract": "NIPPONAMC - NETFSILVER", "Buy/Sell": "Buy",
         "Buy Price": 0, "Sell Price": None, "Quantity": 0, "Brokerage": 2,
         "GST": 0.36, "STT": 0, "Sebi Tax": 0, "Exchange Turnover Charges": 0,
         "Stamp Duty": 0, "Other Charges": 0, "IPFT Charges": 0,
         "Order Type": "Intraday", "Segment": "CAPITAL", "Exchange": "NSE",
         "Order ID": "1300000069950000", "Trade ID": None,
         "Date": "10/29/25 0:00"},
        {"Scrip/Contract": "NIPPONAMC - NETFSILVER", "Buy/Sell": "Buy",
         "Buy Price": 141.19, "Sell Price": None, "Quantity": 1, "Brokerage": 0,
         "GST": 0, "STT": 0, "Sebi Tax": 0, "Exchange Turnover Charges": 0,
         "Stamp Duty": 0, "Other Charges": 0, "IPFT Charges": 0,
         "Order Type": "Delivery", "Segment": "CAPITAL", "Exchange": "NSE",
         "Order ID": "1300000069950000", "Trade ID": "608294336",
         "Date": "10/29/25 0:00"},
    ])
    ledger, summary = statement_to_ledger(rows, "test.xlsx")
    assert summary["charges_only"] == 1

    charge_row = ledger[ledger["status"] == "charges_only"].iloc[0]
    fill_row = ledger[ledger["status"] == "complete"].iloc[0]
    assert charge_row["total_charges"] == pytest.approx(2.36)
    assert charge_row["qty_filled"] == 0
    # Distinct keys, so the merge keeps both.
    assert ledger_key(charge_row.to_dict()) != ledger_key(fill_row.to_dict())


def test_charge_only_rows_survive_a_merge_alongside_their_fill(tmp_path):
    rows = pd.DataFrame([
        {"Scrip/Contract": "NIP IND ETF IT", "Buy/Sell": "Buy", "Buy Price": 0,
         "Sell Price": None, "Quantity": 0, "Brokerage": 2, "GST": 0.36,
         "STT": 0, "Sebi Tax": 0, "Exchange Turnover Charges": 0,
         "Stamp Duty": 0, "Other Charges": 0, "IPFT Charges": 0,
         "Order Type": "Intraday", "Segment": "CAPITAL", "Exchange": "NSE",
         "Order ID": "SAME", "Trade ID": None, "Date": "10/29/25 0:00"},
        {"Scrip/Contract": "NIP IND ETF IT", "Buy/Sell": "Buy", "Buy Price": 31.0,
         "Sell Price": None, "Quantity": 10, "Brokerage": 0, "GST": 0,
         "STT": 0, "Sebi Tax": 0, "Exchange Turnover Charges": 0,
         "Stamp Duty": 0, "Other Charges": 0, "IPFT Charges": 0,
         "Order Type": "Delivery", "Segment": "CAPITAL", "Exchange": "NSE",
         "Order ID": "SAME", "Trade ID": "T1", "Date": "10/29/25 0:00"},
    ])
    ledger_rows, _ = statement_to_ledger(rows, "test.xlsx")
    merged, added, _ = merge_into_ledger(ledger_rows, str(tmp_path / "t.csv"))
    assert added == 2 and len(merged) == 2


def test_since_cutoff_excludes_older_trades():
    from datetime import date as _date
    rows, summary = statement_to_ledger(_statement_rows(), "test.csv",
                                        since=_date(2026, 10, 1))
    assert summary["imported"] == 1
    assert summary["skipped_before_cutoff"] == 1
    assert rows.iloc[0]["date"] == "2026-11-05"
