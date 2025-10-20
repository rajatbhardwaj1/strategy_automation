import pandas as pd
import pytest
import logging
import os
import time
from src.utils import load_config
from src.shoonya_api import ShoonyaAPI   # adjust if filename differs


# --- Logging Setup ---
LOG_FILE = "strategy.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=LOG_FILE,
)
logger = logging.getLogger(__name__)

class LoginError(Exception):
    """Raised when login to Shoonya API fails."""
    pass

class NotLoggedInError(Exception):
    """Raised when user attempts API calls before logging in."""
    pass


@pytest.fixture(scope="session")
def credentials():
    """Load credentials from config.yaml (or env)."""
    creds = load_config()
    assert all(k in creds for k in ["uid", "password", "api_key", "totp_secret", "vendor_key", "imei"]), \
        "Missing one or more required credentials"
    return creds


@pytest.fixture(scope="session")
def shoonya_api_instance(credentials):
    """Initialize ShoonyaAPI instance using real credentials."""
    api = ShoonyaAPI(
        uid=credentials["uid"],
        password=credentials["password"],
        api_key=credentials["api_key"],
        totp_secret=credentials["totp_secret"],
        vendor_key=credentials["vendor_key"],
        imei=credentials["imei"],
    )
    return api


def test_login_real(shoonya_api_instance):
    """
    Integration test — performs a real login.
    Ensures logging works and no exceptions are raised.
    """
    logger.info("=== Starting ShoonyaAPI real login test ===")

    try:
        shoonya_api_instance.login()
        assert shoonya_api_instance.is_logged_in, "Login flag not set"
        logger.info("✅ Login successful for user %s", shoonya_api_instance.user_id)

        # Verify that logging actually wrote to file
        time.sleep(1)
        assert os.path.exists(LOG_FILE), f"Log file {LOG_FILE} not found"
        with open(LOG_FILE, "r") as f:
            log_content = f.read()
            assert "Successfully logged in" in log_content or "already logged in" in log_content, \
                "Expected login success message not found in log file"

        print(f"Login test passed for user: {shoonya_api_instance.user_id}")

    except Exception as e:
        logger.error("❌ Login test failed due to: %s", e, exc_info=True)
        pytest.fail(f"Login test failed due to: {e}")
    finally:
        logger.info("=== ShoonyaAPI real login test completed ===")




def test_place_order_real(shoonya_api_instance):
    """
    Integration test: place a small test order.
    WARNING: This will place a real order! Use minimal quantity to avoid losses.
    """
    shoonya_api_instance.login()
    symbol = "NIFTYBEES"   # Replace with a valid tradable symbol
    qty = 1                 # Minimal quantity for testing
    side = "B"              # Buy
    order_type = "MKT"      # Market order

    try:
        response = shoonya_api_instance.place_order(symbol=symbol, qty=qty, side=side, order_type=order_type)
        assert isinstance(response, dict), "Order response should be a dictionary"
        assert "stat" in response, "Response missing 'stat' key"
        print(f"Order test response: {response}")

    except Exception as e:
        logger.error("❌ Place order test failed due to: %s", e, exc_info=True)
        pytest.fail(f"Place order test failed due to: {e}")


def test_fetch_current_holdings_and_cash_real(shoonya_api_instance):
    """
    Integration test: fetch current holdings and cash.
    """
    shoonya_api_instance.login()
    try:
        holdings_df, total_cash = shoonya_api_instance.fetch_current_holdings_and_cash()
        assert isinstance(holdings_df, pd.DataFrame), "Holdings should be a DataFrame"
        assert isinstance(total_cash, (float, int)), "Total cash should be numeric"
        print(f"Holdings:\n{holdings_df}\nTotal investable cash: {total_cash}")

    except NotLoggedInError:
        pytest.fail("User must be logged in to fetch holdings")
    except Exception as e:
        logger.error("❌ Fetch holdings test failed due to: %s", e, exc_info=True)
        pytest.fail(f"Fetch holdings test failed due to: {e}")
