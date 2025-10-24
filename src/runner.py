import logging
import os
from datetime import datetime
from typing import Dict
from src.utils import load_config
from src.shoonya_api import ShoonyaAPI
from strategy import ETFStrategy

# ----------------------------
# Logging Setup
# ----------------------------
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "strategy_runner.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=LOG_FILE,
    filemode='a'
)
logger = logging.getLogger(__name__)


class StrategyRunner:
    """
    Wrapper class to run ETFStrategy periodically.
    Initializes ShoonyaAPI with credentials loaded from config.
    """

    def __init__(self,shoonya_api,  config: Dict):
        # Load trading credentials from config

        self.strategy = ETFStrategy(shoonya_api, config)

    def run_trailing_sell(self):
        """
        Run trailing sell logic (can be scheduled every 10 mins).
        """
        logger.info("Running trailing sell orders...")
        try:
            self.strategy.sell_order_processing()
        except Exception as e:
            logger.exception(f"Error in trailing sell: {e}")

    def run_buy(self):
        """
        Run buy logic (once per day at 3:25 PM).
        """
        logger.info("Running buy orders...")
        try:
            self.strategy.buy_order_processing()
        except Exception as e:
            logger.exception(f"Error in buy processing: {e}")


if __name__ == "__main__":
    # ----------------------------
    # Strategy Config
    # ----------------------------
    strategy_config = load_config(section="strategy")
    creds = load_config(section="trading")

    api = ShoonyaAPI(
        uid=creds["uid"],
        password=creds["password"],
        api_key=creds["api_key"],
        totp_secret=creds["totp_secret"],
        vendor_key=creds["vendor_key"],
        imei=creds["imei"],
    )
    # to do --> login logout , buy and sell timings ......
    api.login()
    runner = StrategyRunner(api, strategy_config)
    # Example runs
    runner.run_trailing_sell()  # Run trailing sell immediately
    runner.run_buy()            # Run buy immediately
    api.logout()