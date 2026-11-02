"""
Unit tests for the /show_etfs snapshot message (format_etf_list_message).

Each row carries the live market view (LTP / DMA / %Diff) plus the position
held in that ETF: Qty, AvgBuy, the total it was Bought for, and SellAt (the
trailing-profit target, AvgBuy +profit_threshold%). Not-held ETFs show NA.

Missing broker/market SDKs are stubbed by test/conftest.py.
"""
import pandas as pd

from src.utils import format_etf_list_message


def _etf_df() -> pd.DataFrame:
    """Market snapshot, most-fallen first, as fetch_all_etf_data returns it."""
    return pd.DataFrame([
        {"etf": "GOLDBEES", "ltp": 82.50, "dma": 85.00, "pct_diff": -2.94},
        {"etf": "NIFTYBEES", "ltp": 285.00, "dma": 280.00, "pct_diff": 1.79},
    ])


def _holdings_df() -> pd.DataFrame:
    """GOLDBEES held, NIFTYBEES not - as the broker returns holdings."""
    return pd.DataFrame([{
        "etf": "GOLDBEES",
        "held_qty": 100,
        "buy_price": 80.00,
        "invested": 8000.00,
        "ltp": 82.50,
        "pct_diff": 3.13,
    }])


def _row_for(message: str, symbol: str) -> str:
    """The single table line describing `symbol`."""
    rows = [line for line in message.splitlines() if line.startswith(symbol)]
    assert len(rows) == 1, f"expected one {symbol} row, got {rows}"
    return rows[0]


# ---------------------------
# Held ETFs
# ---------------------------
def test_held_etf_row_reports_qty_avg_bought_and_sell_target():
    """100 @ 80.00 -> bought for 8,000, sell target 80 * 1.065 = 85.20."""
    message = format_etf_list_message(_etf_df(), _holdings_df(), profit_threshold=6.5)
    row = _row_for(message, "GOLDBEES")

    assert "100" in row              # Qty
    assert "80.00" in row            # AvgBuy
    assert "8,000" in row            # total bought for
    assert "85.20" in row            # SellAt = AvgBuy +6.5%
    # The market columns it already had must survive.
    assert "82.5" in row and "85.0" in row and "-2.94" in row


def test_sell_target_follows_the_configured_profit_threshold():
    """The target is not hardcoded: at 10% it is 80.00 -> 88.00."""
    message = format_etf_list_message(_etf_df(), _holdings_df(), profit_threshold=10.0)

    assert "88.00" in _row_for(message, "GOLDBEES")
    assert "SellAt = AvgBuy +10.0%" in message


def test_header_counts_funds_and_held_positions():
    message = format_etf_list_message(_etf_df(), _holdings_df())

    assert "2 funds, 1 held" in message
    for column in ("ETF", "LTP", "DMA", "%Diff", "Qty", "AvgBuy", "Bought", "SellAt"):
        assert column in message


# ---------------------------
# Not-held ETFs
# ---------------------------
def test_unheld_etf_shows_na_in_every_position_column():
    message = format_etf_list_message(_etf_df(), _holdings_df())
    row = _row_for(message, "NIFTYBEES")

    assert row.count("NA") == 4      # Qty, AvgBuy, Bought, SellAt
    assert "285.0" in row and "1.79" in row


def test_without_holdings_every_row_is_na():
    """
    The broker fetch is an overlay - when it fails, /show_etfs still sends the
    market snapshot with the position columns blanked out.
    """
    message = format_etf_list_message(_etf_df(), None)

    assert "2 funds, 0 held" in message
    assert _row_for(message, "GOLDBEES").count("NA") == 4
    assert _row_for(message, "NIFTYBEES").count("NA") == 4


def test_empty_holdings_frame_is_treated_as_nothing_held():
    empty = pd.DataFrame(columns=["etf", "held_qty", "buy_price", "invested", "ltp", "pct_diff"])
    message = format_etf_list_message(_etf_df(), empty)

    assert "0 held" in message
    assert _row_for(message, "GOLDBEES").count("NA") == 4


# ---------------------------
# Shape / regressions
# ---------------------------
def test_empty_snapshot_message_unchanged():
    assert format_etf_list_message(pd.DataFrame()) == "⚠️ No ETF data available right now."


def test_table_rows_are_column_aligned_and_fit_one_telegram_message():
    """
    The table lives in a ``` block, so every row must be the same width as
    the header, and the whole message has to stay under Telegram's 4096-char
    limit (chunking would split the code fence and break the formatting).
    """
    many = pd.DataFrame([
        {"etf": f"ETFSYMBOL{i:02d}", "ltp": 1234.56, "dma": 1200.00, "pct_diff": -1.23}
        for i in range(40)
    ])
    message = format_etf_list_message(many, _holdings_df())

    table = message.split("```")[1].strip("\n").splitlines()
    widths = {len(line) for line in table}
    assert len(widths) == 1, f"ragged table widths: {sorted(widths)}"
    assert len(message) < 4096, len(message)


def test_missing_pct_diff_renders_na_not_nan():
    """
    pandas turns a missing pct_diff into NaN (not None), so the cell has to be
    guarded with isna() or it reads "nan" in the message.
    """
    etf_df = pd.DataFrame([{"etf": "GOLDBEES", "ltp": 82.50, "dma": 85.00, "pct_diff": None}])
    row = _row_for(format_etf_list_message(etf_df), "GOLDBEES")

    assert "nan" not in row
    assert "N/A" in row
