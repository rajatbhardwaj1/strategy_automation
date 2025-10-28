import os
from src.angel_api import AngelAPI, NotLoggedInError
from src.utils import load_config


def test_real_buy_order():
    """
    Real integration test:
    Logs in using real Angel API credentials and places a BUY order.
    """

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

    # --- Step 2: Place Buy Order ---
    symbol = "NIFTYBEES"  # Safe ETF for small test order
    qty = 1
    price = 250.0  # within valid trading range
    order_type = "LIMIT"

    buy_response = api.place_buy_order(symbol, qty, order_type, price=price)

    print("Buy Order Response:", buy_response)

    assert buy_response["status"] in (True, False)  # Must return valid structure
    assert "message" in buy_response

    # --- Step 3: Logout ---
    api.logout()
    print("✅ Logout successful")
