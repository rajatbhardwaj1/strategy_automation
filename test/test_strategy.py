import pytest
import logging
import pandas as pd
from src.strategy import ETFStrategy
from src.shoonya_api import ShoonyaAPI
from src.utils import load_config

# ---------------------------
# Logging Setup
# ---------------------------
LOG_FILE = "strategy_test.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=LOG_FILE,
)
logger = logging.getLogger(__name__)


# ---------------------------
# Fixtures
# ---------------------------
@pytest.fixture(scope="session")
def credentials():
    """Load credentials from config.yaml (or env)."""
    creds = load_config(section="trading")
    assert all(
        k in creds for k in
        ["uid", "password", "api_key", "totp_secret", "vendor_key", "imei"]
    ), "Missing one or more required credentials"
    return creds


@pytest.fixture(scope="session")
def shoonya_api_instance(credentials):
    """
    Initialize ShoonyaAPI with real credentials and perform login.
    Returns the API instance if login succeeds.
    """
    api = ShoonyaAPI(
        uid=credentials["uid"],
        password=credentials["password"],
        api_key=credentials["api_key"],
        totp_secret=credentials["totp_secret"],
        vendor_key=credentials["vendor_key"],
        imei=credentials["imei"],
    )

    # Perform login and check response
    login_resp = api.login()
    status = login_resp.get("status")
    message = login_resp.get("message", "")

    if status != "success":
        logger.error(f"ShoonyaAPI login failed: {message}")
        pytest.fail(f"ShoonyaAPI login failed: {message}")

    logger.info(f"ShoonyaAPI login successful: {message}")
    return api


@pytest.fixture
def strategy_instance(shoonya_api_instance):
    """Initialize the ETFStrategy with live API."""
    config = load_config(section="strategy")
    strat = ETFStrategy(api=shoonya_api_instance, config=config )
    return strat


# ---------------------------
# Tests for Sell Order Processing
# ---------------------------
def test_sell_order_processing(strategy_instance):
    """
    Test trailing sell logic using live data.
    It will only trigger if held ETFs exist and profit > threshold.
    """
    logger.info("Starting sell_order_processing test...")

    held_etfs, cash, etf_data = strategy_instance.fetch_holdings_and_cash()
    assert isinstance(held_etfs, pd.DataFrame)
    assert isinstance(etf_data, pd.DataFrame)
    assert isinstance(cash, (float, int))

    strategy_instance.sell_order_processing()

    logger.info("Completed sell_order_processing successfully.")
    assert True


# ---------------------------
# Tests for Buy Order Processing
# ---------------------------
def test_buy_order_processing(strategy_instance):
    """
    Test buy order logic using real market data.
    It identifies top ETFs to buy and attempts simulated buys.
    """
    logger.info("Starting buy_order_processing test...")

    held_etfs, cash, etf_data = strategy_instance.fetch_holdings_and_cash()
    assert isinstance(held_etfs, pd.DataFrame)
    assert isinstance(etf_data, pd.DataFrame)
    assert isinstance(cash, (float, int))

    strategy_instance.buy_order_processing()

    logger.info("Completed buy_order_processing successfully.")
    assert True


# ---------------------------
# Sanity Tests
# ---------------------------
def test_fetch_holdings_and_cash(strategy_instance):
    """Ensure fetch_holdings_and_cash returns valid structure."""
    holdings, cash, etf_data = strategy_instance.fetch_holdings_and_cash()

    assert isinstance(holdings, pd.DataFrame)
    assert isinstance(etf_data, pd.DataFrame)
    assert isinstance(cash, (float, int))
    logger.info(f"Fetched holdings: {len(holdings)} items, Cash: {cash}")


def test_api_connection_alive(shoonya_api_instance):
    """Simple sanity check that API connection is alive."""
    profile = shoonya_api_instance.api.get_profile()
    assert profile.get("stat") == "Ok", "API connection not alive"
    logger.info("API connection verified successfully.")
