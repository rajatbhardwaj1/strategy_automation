"""
Hermetic unit tests for the hydration (loan) rule in ETFStrategy.buy_etfs.

Hydration is money borrowed and parked in the broker account:
  * the broker reports it as available cash, so it is fully spendable,
  * but it is a liability, so it must never inflate the sizing base
    (the `total_value` numerator of total_value / buy_divisor).

Missing broker/market SDKs are stubbed by test/conftest.py.
"""
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.strategy import ETFStrategy


BUY_DIVISOR = 40
PRICE = 100.0


# ---------------------------
# Fixtures / helpers
# ---------------------------
@pytest.fixture(autouse=True)
def _mute_telegram(monkeypatch):
    """buy_etfs notifies on every path; keep the tests offline."""
    monkeypatch.setattr("src.strategy.send_telegram_message", lambda *a, **k: None)


def _held(holdings_value: float) -> pd.DataFrame:
    """One held ETF (never a buy candidate here) worth `holdings_value`."""
    if holdings_value == 0:
        return pd.DataFrame(columns=["etf", "held_qty", "buy_price", "invested", "ltp", "pct_diff"])
    return pd.DataFrame([{
        "etf": "HELDETF",
        "held_qty": int(holdings_value / 200),
        "buy_price": 200.0,
        "invested": holdings_value,
        "ltp": 200.0,
        "pct_diff": 0.0,
    }])


def _candidate(symbol: str = "NEWETF", price: float = PRICE) -> pd.DataFrame:
    """A single buy candidate, in a downtrend so it is never skipped."""
    return pd.DataFrame([{"etf": symbol, "ltp": price, "pct_diff": -5.0}])


def _strategy(hydration: float, holdings: pd.DataFrame, cash: float):
    """ETFStrategy wired to a fake broker that always fills the order."""
    api = MagicMock()
    api.fetch_current_holdings_and_cash.return_value = (holdings, cash)
    api.place_buy_order.return_value = {"status": True, "final_status": "complete"}
    config = {"buy_divisor": BUY_DIVISOR, "hydration": hydration, "max_daily_buys": 1}
    return ETFStrategy(api=api, config=config), api


def _placed_qty(api) -> int:
    """Quantity of the single buy order the fake broker received."""
    assert api.place_buy_order.call_count == 1, api.place_buy_order.call_args_list
    return api.place_buy_order.call_args.kwargs["qty"]


# ---------------------------
# Numerator: hydration is excluded
# ---------------------------
def test_hydration_is_excluded_from_the_sizing_base():
    """
    Same account, same cash, same holdings - only the declared hydration
    differs. The loan must shrink the numerator by exactly its own size.

    cash 1,00,000 + holdings 4,00,000 = 5,00,000 gross.
      hydration 0        -> base 5,00,000 -> 12,500 -> 125 shares @ 100
      hydration 1,00,000 -> base 4,00,000 -> 10,000 -> 100 shares @ 100
    """
    holdings = _held(400_000)

    strat_no_loan, api_no_loan = _strategy(0, holdings, cash=100_000)
    strat_no_loan.buy_etfs(_candidate(), holdings, cash=100_000)

    strat_loan, api_loan = _strategy(100_000, holdings, cash=100_000)
    strat_loan.buy_etfs(_candidate(), holdings, cash=100_000)

    assert _placed_qty(api_no_loan) == 125
    assert _placed_qty(api_loan) == 100
    # The whole difference is the loan, divided by the buy divisor.
    expected_gap = (100_000 / BUY_DIVISOR) / PRICE
    assert _placed_qty(api_no_loan) - _placed_qty(api_loan) == expected_gap


def test_zero_hydration_keeps_the_original_sizing():
    """Default config (hydration: 0) must behave exactly as before."""
    holdings = _held(400_000)
    strat, api = _strategy(0, holdings, cash=100_000)
    strat.buy_etfs(_candidate(), holdings, cash=100_000)

    expected = int(((100_000 + 400_000) / BUY_DIVISOR) // PRICE)
    assert _placed_qty(api) == expected == 125


# ---------------------------
# Cash: hydration is spendable
# ---------------------------
def test_hydration_funds_a_buy_when_own_cash_is_zero():
    """
    Own cash 0, the 1,00,000 sitting in the account is purely the loan.
    The buy still goes through - hydration counts as cash - and is sized off
    the holdings alone (1,00,000 cash - 1,00,000 loan + 4,00,000 holdings).
    """
    holdings = _held(400_000)
    strat, api = _strategy(100_000, holdings, cash=100_000)
    strat.buy_etfs(_candidate(), holdings, cash=100_000)

    assert _placed_qty(api) == int((400_000 / BUY_DIVISOR) // PRICE) == 100


def test_spendable_ceiling_is_cash_only_and_never_cash_plus_hydration():
    """
    Loan mostly drawn down: only 5,000 is left in the account while the
    1,00,000 liability still stands. The order is capped by the real 5,000
    (50 shares), not by cash + hydration (which would wrongly allow 76).
    """
    holdings = _held(400_000)
    strat, api = _strategy(100_000, holdings, cash=5_000)
    strat.buy_etfs(_candidate(), holdings, cash=5_000)

    assert _placed_qty(api) == int(5_000 // PRICE) == 50


def test_drawn_down_hydration_shrinks_the_sizing_base():
    """
    Spending the loan makes own cash negative, so the base keeps tracking
    net-of-loan worth: 5,000 - 1,00,000 + 20,00,000 = 19,05,000 -> 47,625,
    which is what sizing uses while cash is still ample.
    """
    holdings = _held(2_000_000)
    strat, api = _strategy(100_000, holdings, cash=200_000)
    strat.buy_etfs(_candidate(), holdings, cash=200_000)

    base = 200_000 - 100_000 + 2_000_000
    assert _placed_qty(api) == int((base / BUY_DIVISOR) // PRICE) == 525


# ---------------------------
# Edge: loan larger than net worth
# ---------------------------
def test_no_buy_when_hydration_exceeds_net_worth():
    """A base of zero or less means no capital of my own - nothing is bought."""
    holdings = _held(0)
    strat, api = _strategy(100_000, holdings, cash=100_000)
    strat.buy_etfs(_candidate(), holdings, cash=100_000)

    api.place_buy_order.assert_not_called()
