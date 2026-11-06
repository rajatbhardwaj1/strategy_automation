import os
from src.angel_api import AngelAPI, NotLoggedInError
from src.strategy import ETFStrategy, identify_buy_signals
from src.utils import load_config
from src.utils import fetch_all_etf_data, format_buy_signal_message
from src.telegram_utils import send_telegram_message
import pytest

# Every test here logs into the real Angel One account with the credentials in
# src/config.yaml, and test_trailing_profit runs sell_order_processing against
# it, which can place orders. Deselected unless -m live is passed.
#
# The real buy-order test lives in manual/place_test_buy_order.py instead -
# outside test/ so pytest cannot collect it, and behind a typed confirmation.
pytestmark = pytest.mark.live


def test_holdings():
    # --- Load from environment (recommended) ---
    creds = load_config(section="trading")

    api = AngelAPI(
        api_key=creds["api_key"],
        user_name=creds["uid"],
        password=creds["password"],
        totp_secret=creds["totp_secret"]
    )
    # --- Step 1: Login ---
    login_response = api.login()
    print(login_response)
    assert login_response["status"] in ("success", "already_logged_in")
    print("✅ Login successful")


    holdings_df, total_cash = api.fetch_current_holdings_and_cash()

    print(holdings_df)
    print(total_cash)

def test_trailing_profit():
    creds = load_config(section="trading")

    api = AngelAPI(
        api_key=creds["api_key"],
        user_name=creds["uid"],
        password=creds["password"],
        totp_secret=creds["totp_secret"]
    )
    # --- Step 1: Login ---
    login_response = api.login()
    print(login_response)
    strategy_config = load_config(section="strategy")

    strategy = ETFStrategy(api , strategy_config)
    strategy.sell_order_processing()


def test_current_signals():
    creds = load_config(section="trading")

    api = AngelAPI(
        api_key=creds["api_key"],
        user_name=creds["uid"],
        password=creds["password"],
        totp_secret=creds["totp_secret"]
    )
    # --- Step 1: Login ---
    login_response = api.login()
    print(login_response)
    strategy_config = load_config(section="strategy")
    strategy = ETFStrategy(api , strategy_config)

    held_etfs, cash = strategy.fetch_holdings_and_cash()
    etf_data = fetch_all_etf_data()
    etfs_to_buy = identify_buy_signals(etf_data, top_n=3)
    print(etfs_to_buy)


def test_get_active_sell_orders(tmp_path):
    """
    Writes the fetched order book to tmp_path, not the repo root: the previous
    "sell_orders.csv" target is a tracked file, so a run would overwrite it
    with whatever this account happens to return and dirty the working tree.
    """
    creds = load_config(section="trading")

    api = AngelAPI(
        api_key=creds["api_key"],
        user_name=creds["uid"],
        password=creds["password"],
        totp_secret=creds["totp_secret"]
    )
    # --- Step 1: Login ---
    login_response = api.login()
    print(login_response)
    strategy_config = load_config(section="strategy")
    strategy = ETFStrategy(api, strategy_config)

    sellorders = api._get_active_sell_orders("SILVERBEES")
    out = tmp_path / "sell_orders.csv"
    sellorders.to_csv(out, index=False)
    print(f"Saved to {out}")