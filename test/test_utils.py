import pytest
import logging
import os
import time
from src.utils import fetch_etf_data, fetch_all_etf_data
import pandas as pd
# --- Logging Setup ---
LOG_FILE = "strategy.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=LOG_FILE,
)
logger = logging.getLogger(__name__)

@pytest.mark.integration
def test_fetch_etf_data_real():
    """
    Integration test — calls yfinance API to fetch ETF data.
    Ensures data is returned and correctly structured.
    """
    logger.info("=== Starting fetch_etf_data_real test ===")
    symbol = "NIFTYBEES"  # Use a valid ETF symbol

    try:
        result = fetch_etf_data(symbol)
        logger.info(f"Fetched ETF data for {symbol}: {result}")

        # --- Basic assertions ---
        assert result is not None, "fetch_etf_data returned None"
        assert "etf" in result and result["etf"] == symbol, "ETF symbol mismatch"
        assert isinstance(result["ltp"], float), "LTP must be a float"
        assert isinstance(result["dma"], float), "DMA must be a float"
        assert isinstance(result["pct_diff"], float), "pct_diff must be a float"

        # --- Verify logging to file ---
        time.sleep(1)
        assert os.path.exists(LOG_FILE), f"Log file {LOG_FILE} not found"
        with open(LOG_FILE, "r") as f:
            log_content = f.read()
            assert symbol in log_content or "fetch_etf_data" in log_content or "Fetched ETF" in log_content, \
                "Expected ETF log entry not found"

        print(f"\n✅ Fetched ETF data successfully for {symbol}: {result}")

    except Exception as e:
        logger.error("❌ fetch_etf_data_real test failed due to: %s", e, exc_info=True)
        pytest.fail(f"fetch_etf_data_real test failed due to: {e}")
    finally:
        logger.info("=== fetch_etf_data_real test completed ===")




@pytest.mark.integration
def test_fetch_all_etf_data_real():
    """
    Integration test for fetching all ETFs.
    Actually calls yfinance API, may take longer depending on number of ETFs.
    """
    df = fetch_all_etf_data()

    print("\nFetched All ETF Data:")
    print(df)

    # --- Basic validations ---
    assert isinstance(df, pd.DataFrame), "Result is not a DataFrame"
    assert not df.empty, "DataFrame is empty, no ETF data fetched"
    assert all(col in df.columns for col in ["etf", "ltp", "dma", "pct_diff"]), "Missing expected columns"
    assert df["ltp"].dtype.kind in "f", "ltp column is not float"
    assert df["dma"].dtype.kind in "f", "dma column is not float"
    assert df["pct_diff"].dtype.kind in "f", "pct_diff column is not float"
