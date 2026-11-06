"""
Unit tests for the tap-to-sort reordering behind the /show_etfs and /portfolio
inline keyboards (sort_etf_snapshot, sort_holdings).

These helpers only reorder rows - the formatters still derive every displayed
cell - so the tests assert symbol order, not rendering.

Missing broker/market SDKs are stubbed by test/conftest.py.
"""
import pandas as pd

from src.utils import sort_etf_snapshot, sort_holdings


def _etf_df() -> pd.DataFrame:
    """Market snapshot, most-fallen first, as fetch_all_etf_data returns it."""
    return pd.DataFrame([
        {"etf": "GOLDBEES", "ltp": 82.50, "dma": 85.00, "pct_diff": -2.94},
        {"etf": "ITBEES", "ltp": 40.00, "dma": 40.20, "pct_diff": -0.50},
        {"etf": "NIFTYBEES", "ltp": 285.00, "dma": 280.00, "pct_diff": 1.79},
    ])


def _holdings_df() -> pd.DataFrame:
    """GOLDBEES and NIFTYBEES held, ITBEES not."""
    return pd.DataFrame([
        {"etf": "GOLDBEES", "held_qty": 100, "buy_price": 80.00,
         "invested": 8000.00, "ltp": 82.50, "pct_diff": 3.13},
        {"etf": "NIFTYBEES", "held_qty": 10, "buy_price": 270.00,
         "invested": 2700.00, "ltp": 285.00, "pct_diff": 5.56},
    ])


def _order(df: pd.DataFrame) -> list:
    return list(df["etf"])


# ---------------------------
# ETF snapshot sorting
# ---------------------------
def test_sorts_by_market_column_in_both_directions():
    assert _order(sort_etf_snapshot(_etf_df(), _holdings_df(), "pct_diff")) == \
        ["GOLDBEES", "ITBEES", "NIFTYBEES"]
    assert _order(sort_etf_snapshot(_etf_df(), _holdings_df(), "pct_diff", descending=True)) == \
        ["NIFTYBEES", "ITBEES", "GOLDBEES"]


def test_sorts_by_position_column_held_in_holdings_not_etf_df():
    # Qty: GOLDBEES 100, NIFTYBEES 10. ITBEES is not held.
    assert _order(sort_etf_snapshot(_etf_df(), _holdings_df(), "held_qty", descending=True))[:2] == \
        ["GOLDBEES", "NIFTYBEES"]
    # Bought: GOLDBEES 8000, NIFTYBEES 2700.
    assert _order(sort_etf_snapshot(_etf_df(), _holdings_df(), "bought", descending=True))[:2] == \
        ["GOLDBEES", "NIFTYBEES"]
    # SellAt at +6.5%: NIFTYBEES 287.55, GOLDBEES 85.20.
    assert _order(sort_etf_snapshot(_etf_df(), _holdings_df(), "sell_at", descending=True))[:2] == \
        ["NIFTYBEES", "GOLDBEES"]


def test_unheld_etfs_sort_last_in_both_directions():
    """
    A position column says nothing about an ETF that is not held, so those rows
    stay at the bottom either way rather than leading an ascending sort.
    """
    for descending in (False, True):
        order = _order(sort_etf_snapshot(_etf_df(), _holdings_df(), "held_qty", descending))
        assert order[-1] == "ITBEES", order


def test_position_sort_without_holdings_leaves_order_unchanged():
    """The /show_etfs holdings overlay is optional - a broker failure must not
    reorder or drop the market snapshot."""
    assert _order(sort_etf_snapshot(_etf_df(), None, "held_qty", descending=True)) == \
        _order(_etf_df())
    assert _order(sort_etf_snapshot(_etf_df(), pd.DataFrame(), "bought")) == _order(_etf_df())


def test_unknown_key_and_empty_frame_are_passed_through():
    assert _order(sort_etf_snapshot(_etf_df(), _holdings_df(), "nonsense")) == _order(_etf_df())
    assert sort_etf_snapshot(pd.DataFrame(), _holdings_df(), "pct_diff").empty


def test_sort_is_stable_so_ties_keep_incoming_order():
    tied = pd.DataFrame([
        {"etf": "AAA", "ltp": 1.0, "dma": 1.0, "pct_diff": 0.0},
        {"etf": "BBB", "ltp": 1.0, "dma": 1.0, "pct_diff": 0.0},
    ])
    assert _order(sort_etf_snapshot(tied, None, "pct_diff")) == ["AAA", "BBB"]


# ---------------------------
# Holdings sorting
# ---------------------------
def test_holdings_sort_by_gain_and_invested():
    assert _order(sort_holdings(_holdings_df(), "pct_diff", descending=True)) == \
        ["NIFTYBEES", "GOLDBEES"]
    assert _order(sort_holdings(_holdings_df(), "invested", descending=True)) == \
        ["GOLDBEES", "NIFTYBEES"]
    assert _order(sort_holdings(_holdings_df(), "etf", descending=False)) == \
        ["GOLDBEES", "NIFTYBEES"]


def test_holdings_unknown_key_and_empty_frame_are_passed_through():
    assert _order(sort_holdings(_holdings_df(), "nonsense")) == _order(_holdings_df())
    assert sort_holdings(pd.DataFrame(), "pct_diff").empty
