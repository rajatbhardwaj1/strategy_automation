import time
import os
import logging
from datetime import datetime, time as dt_time, timedelta
import schedule

from src.utils import load_config
from src.shoonya_api import ShoonyaAPI
from src.runner import StrategyRunner   # your runner file
import warnings
from urllib3.exceptions import NotOpenSSLWarning

# Ignore the LibreSSL warning
warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
# --- Logging Setup ---
LOG_FILE = "logs/main.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=LOG_FILE,
    filemode="a"  # Append to the log file instead of overwriting
)

logger = logging.getLogger(__name__)


# ----------------------------
# Initialize API and Runner
# ----------------------------
creds = load_config(section="trading")
strategy_config = load_config(section="strategy")

api = ShoonyaAPI(
    uid=creds["uid"],
    password=creds["password"],
    api_key=creds["api_key"],
    totp_secret=creds["totp_secret"],
    vendor_key=creds["vendor_key"],
    imei=creds["imei"],
)

try:
    api.login()
    logger.info("✅ Shoonya API login successful")
except Exception as e:
    logger.exception(f"Login failed: {e}")
    raise SystemExit(1)

runner = StrategyRunner(api, strategy_config)

# ----------------------------
# Task Functions
# ----------------------------
def run_trailing_sell():
    logger.info("=== Running Trailing Sell ===")
    try:
        runner.run_trailing_sell()
    except Exception as e:
        logger.exception(f"Error in trailing sell: {e}")

def run_buy():
    logger.info("=== Running Daily Buy ===")
    try:
        runner.run_buy()
    except Exception as e:
        logger.exception(f"Error in daily buy: {e}")

# ----------------------------
# Helper: Generate sell times every 10 min
# ----------------------------
def generate_trailing_sell_times(start_time=dt_time(9, 20), end_time=dt_time(15, 20), interval_minutes=10):
    """Generate HH:MM strings between start and end at given interval."""
    times = []
    current = datetime.combine(datetime.today(), start_time)
    end_dt = datetime.combine(datetime.today(), end_time)
    while current <= end_dt:
        times.append(current.strftime("%H:%M"))
        current += timedelta(minutes=interval_minutes)
    return times

trailing_sell_times = generate_trailing_sell_times()

# ----------------------------
# Scheduling Rules
# ----------------------------
# Trailing sell scheduled at exact 10-min intervals Mon-Fri
for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
    for t in trailing_sell_times:
        getattr(schedule.every(), day).at(t).do(run_trailing_sell)

# Daily buy at 15:20 Mon-Fri
for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
    getattr(schedule.every(), day).at("15:20").do(run_buy)

logger.info("=== Strategy Scheduler Started ===")

# ----------------------------
# Main Loop
# ----------------------------

try:
    while True:
        schedule.run_pending()
        time.sleep(30)
except KeyboardInterrupt:
    logger.info("Scheduler stopped manually.")
finally:
    try:
        api.logout()
        logger.info("✅ Shoonya API logged out successfully")
    except Exception as e:
        logger.warning(f"Error during logout: {e}")
