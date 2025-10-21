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


@pytest.fixture(scope="session")
def credentials():
    """Load credentials from config.yaml (or env)."""
    creds = load_config(section="trading")
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
