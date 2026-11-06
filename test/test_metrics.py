"""
Unit tests for the performance metrics (XIRR, CAGR, FIFO realised P&L).

Missing broker/market SDKs are stubbed by test/conftest.py.
"""
from datetime import date

import pandas as pd
import pytest

from src.metrics import cagr, compute_metrics, realised_pnl_fifo, xirr


def _ledger(rows) -> pd.DataFrame:
    """Minimal ledger with the columns the metrics actually read."""
    defaults = {"status": "complete", "order_source": "HISTORICAL_IMPORT",
                "total_charges": 0.0, "orderid": ""}
    return pd.DataFrame([{**defaults, **r} for r in rows])


# ---------------------------
# XIRR
# ---------------------------
def test_xirr_recovers_a_known_annual_return():
    """100 out, 110 back exactly a year later, is 10% a year."""
    rate = xirr([(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 110.0)])
    assert rate == pytest.approx(0.10, abs=1e-4)


def test_xirr_weights_by_when_money_arrived():
    """
    The same +10 profit on money that was only present for half the year is a
    higher annualised rate - the whole reason XIRR is preferred over CAGR here.
    """
    half_year = xirr([(date(2025, 1, 1), -100.0), (date(2025, 7, 1), 110.0)])
    full_year = xirr([(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 110.0)])
    assert half_year > full_year


def test_xirr_handles_a_loss():
    rate = xirr([(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 80.0)])
    assert rate == pytest.approx(-0.20, abs=1e-4)


def test_xirr_returns_none_when_no_rate_can_explain_the_flows():
    # One flow, no flows, and all-same-sign flows have no root at all.
    assert xirr([]) is None
    assert xirr([(date(2025, 1, 1), -100.0)]) is None
    assert xirr([(date(2025, 1, 1), -100.0), (date(2026, 1, 1), -50.0)]) is None


def test_cagr_and_its_guards():
    assert cagr(100.0, 121.0, 2.0) == pytest.approx(0.10, abs=1e-6)
    assert cagr(0.0, 121.0, 2.0) is None
    assert cagr(100.0, 121.0, 0.0) is None


# ---------------------------
# FIFO realised P&L
# ---------------------------
def test_fifo_matches_the_oldest_lot_first():
    """
    Two buys at 10 and 20, then one sell of 10 at 30. FIFO consumes the cheap
    lot first, so realised is 10 x (30-10) = 200, not the average-cost 150.
    """
    total, breakdown = realised_pnl_fifo(_ledger([
        {"date": "2026-01-01", "symbol": "X", "side": "BUY", "qty_filled": 10, "avg_fill_price": 10.0, "value": 100.0},
        {"date": "2026-02-01", "symbol": "X", "side": "BUY", "qty_filled": 10, "avg_fill_price": 20.0, "value": 200.0},
        {"date": "2026-03-01", "symbol": "X", "side": "SELL", "qty_filled": 10, "avg_fill_price": 30.0, "value": 300.0},
    ]))
    assert total == pytest.approx(200.0)
    assert breakdown.loc[0, "unmatched_qty"] == 0


def test_fifo_spans_lots_when_a_sell_is_larger_than_one_lot():
    total, _ = realised_pnl_fifo(_ledger([
        {"date": "2026-01-01", "symbol": "X", "side": "BUY", "qty_filled": 10, "avg_fill_price": 10.0, "value": 100.0},
        {"date": "2026-02-01", "symbol": "X", "side": "BUY", "qty_filled": 10, "avg_fill_price": 20.0, "value": 200.0},
        {"date": "2026-03-01", "symbol": "X", "side": "SELL", "qty_filled": 15, "avg_fill_price": 30.0, "value": 450.0},
    ]))
    # 10 x (30-10) + 5 x (30-20) = 250
    assert total == pytest.approx(250.0)


def test_sell_with_no_buy_lot_is_reported_not_counted_as_pure_profit():
    """
    Normal when the ledger starts mid-history: the buy happened before the
    first imported statement. It must surface rather than inflate profit.
    """
    total, breakdown = realised_pnl_fifo(_ledger([
        {"date": "2026-03-01", "symbol": "X", "side": "SELL", "qty_filled": 10, "avg_fill_price": 30.0, "value": 300.0},
    ]))
    assert total == pytest.approx(0.0)
    assert breakdown.loc[0, "unmatched_qty"] == 10


def test_unfilled_orders_never_move_money():
    total, _ = realised_pnl_fifo(_ledger([
        {"date": "2026-01-01", "symbol": "X", "side": "BUY", "qty_filled": 10, "avg_fill_price": 10.0, "value": 100.0},
        {"date": "2026-02-01", "symbol": "X", "side": "SELL", "qty_filled": 0, "avg_fill_price": 0.0, "value": 0.0, "status": "cancelled"},
    ]))
    assert total == pytest.approx(0.0)


# ---------------------------
# Headline metrics
# ---------------------------
def _simple_ledger():
    return _ledger([
        {"date": "2026-01-01", "symbol": "X", "side": "BUY", "qty_filled": 10, "avg_fill_price": 10.0, "value": 100.0, "total_charges": 5.0},
        {"date": "2026-06-01", "symbol": "X", "side": "SELL", "qty_filled": 5, "avg_fill_price": 20.0, "value": 100.0, "total_charges": 5.0},
    ])


def test_metrics_net_of_charges_and_valued_on_holdings():
    holdings = pd.DataFrame([{"etf": "X", "held_qty": 5, "ltp": 20.0}])
    m = compute_metrics(_simple_ledger(), holdings, as_of=date(2026, 12, 31))
    assert m["buy_value"] == 100.0
    assert m["sell_value"] == 100.0
    assert m["current_value"] == 100.0
    assert m["total_charges"] == 10.0
    # 100 held + 100 sold - 100 bought = +100 gross, 90 after charges.
    assert m["total_pnl"] == 100.0
    assert m["total_pnl_net"] == 90.0
    assert m["xirr"] is not None


def test_missing_holdings_leaves_valuation_unknown_rather_than_zero():
    """
    A broker failure must not be reported as a wiped-out portfolio, which is
    what valuing the position at zero would look like.
    """
    m = compute_metrics(_simple_ledger(), None)
    assert m["current_value"] is None
    assert m["total_pnl"] is None
    assert m["xirr"] is None
    # Facts that do not depend on valuation are still reported.
    assert m["buy_value"] == 100.0
    assert m["total_charges"] == 10.0


def test_empty_ledger_is_all_zeroes_not_an_error():
    m = compute_metrics(pd.DataFrame(), None)
    assert m["orders_total"] == 0
    assert m["xirr"] is None
