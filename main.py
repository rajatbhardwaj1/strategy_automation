import time
import os
import logging
from datetime import datetime, time as dt_time, timedelta
import schedule

from src.utils import load_config
from src.telegram_utils import send_telegram_message
from src.runner import StrategyRunner   # your runner file
import warnings
from urllib3.exceptions import NotOpenSSLWarning
from src.angel_api import AngelAPI

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

# ----------------------------
# Task Functions
# ----------------------------
def run_trailing_sell():
    creds = load_config(section="trading")
    strategy_config = load_config(section="strategy")

    api = AngelAPI(
        api_key=creds["api_key"],
        user_name=creds["uid"],
        password=creds["password"],
        totp_secret=creds["totp_secret"]
    )
    try:
        api.login()
        logger.info("API login successful")
    except Exception as e:
        logger.exception(f"Login failed: {e}")
        send_telegram_message(
            f"\U0001f525 **Trailing Sell Aborted**\nLogin failed: {type(e).__name__}: {e}"
        )
        raise SystemExit(1)

    runner = StrategyRunner(api, strategy_config)
    logger.info("=== Running Trailing Sell ===")
    try:
        runner.run_trailing_sell()
    except Exception as e:
        logger.exception(f"Error in trailing sell: {e}")
        send_telegram_message(f"\U0001f525 **Trailing Sell Crashed**\nError: {type(e).__name__}: {e}")

    finally:
        try:
            api.logout()
            logger.info("API logged out successfully")
        except Exception as e:
            logger.warning(f"Error during logout: {e}")


def run_buy():
    creds = load_config(section="trading")
    strategy_config = load_config(section="strategy")
    api = AngelAPI(
        api_key=creds["api_key"],
        user_name=creds["uid"],
        password=creds["password"],
        totp_secret=creds["totp_secret"]
    )
    try:
        api.login()
        logger.info("API login successful")
    except Exception as e:
        logger.exception(f"Login failed: {e}")
        send_telegram_message(
            f"\U0001f525 **Daily Buy Aborted**\nLogin failed: {type(e).__name__}: {e}"
        )
        raise SystemExit(1)
    runner = StrategyRunner(api, strategy_config)
    logger.info("=== Running Daily Buy ===")
    try:
        runner.run_buy()
    except Exception as e:
        logger.exception(f"Error in daily buy: {e}")
        send_telegram_message(f"\U0001f525 **Daily Buy Crashed**\nError: {type(e).__name__}: {e}")

    finally:
        try:
            api.logout()
            logger.info("API logged out successfully")
        except Exception as e:
            logger.warning(f"Error during logout: {e}")

# ----------------------------
# Helper: Generate sell times every 10 min
# ----------------------------
# --- One-off exchange sessions outside the Mon-Fri calendar -----------------
# NSE runs Diwali Muhurat trading on a day the weekday schedule below never
# covers (2026-11-08 is a Sunday), so it needs explicit registration.
#
# SELL ONLY. No buy job is registered for these sessions: the daily buy sizes
# itself off net worth and would open a fresh position into a thin, one-hour
# session. Trailing sells only ever close positions that have already cleared
# the profit threshold, so the downside of running them is bounded.
#
# Sources disagreed on whether continuous trading ends at 15:30 or 15:40 IST,
# so the last fire is 15:20 - inside the earlier of the two, and the same
# 10-minute margin before the close that the normal weekday schedule uses.
#
# Each job is guarded on the date, because schedule.every().day.at() would
# otherwise keep firing every day forever. Remove the entry once the session
# has passed.
SPECIAL_SELL_SESSIONS = [
    {
        "name": "Diwali Muhurat 2026 (Samvat 2083)",
        "date": "2026-11-08",
        "start": dt_time(14, 30),
        "end": dt_time(15, 20),
    },
]


def run_trailing_sell_for_session(session):
    """
    Trailing sell for a one-off session, gated on today actually being that
    date. Returns without touching the broker on any other day.
    """
    today = datetime.now().date().isoformat()
    if today != session["date"]:
        return
    logger.info(f"=== Special session sell: {session['name']} ({today}) ===")
    run_trailing_sell()


def generate_trailing_sell_times(start_time=dt_time(9, 20), end_time=dt_time(15, 20), interval_minutes=10):
    """Generate HH:MM strings between start and end at given interval."""
    times = []
    current = datetime.combine(datetime.today(), start_time)
    end_dt = datetime.combine(datetime.today(), end_time)
    while current <= end_dt:
        times.append(current.strftime("%H:%M"))
        current += timedelta(minutes=interval_minutes)
    return times

def run_scheduler():
    # ----------------------------
    # Scheduling Rules
    # ----------------------------
    # Trailing sell scheduled at exact 10-min intervals Mon-Fri
    trailing_sell_times = generate_trailing_sell_times()

    for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
        for t in trailing_sell_times:
            getattr(schedule.every(), day).at(t).do(run_trailing_sell)

    # Daily buy at 15:20 Mon-Fri
    for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
        getattr(schedule.every(), day).at("15:20").do(run_buy)

    # One-off sessions: trailing sells only, never a buy.
    for session in SPECIAL_SELL_SESSIONS:
        times = generate_trailing_sell_times(session["start"], session["end"])
        for t in times:
            schedule.every().day.at(t).do(run_trailing_sell_for_session, session=session)
        logger.info(
            f"Registered {len(times)} sell-only slots for {session['name']} "
            f"on {session['date']} ({times[0]}-{times[-1]} IST)."
        )

    logger.info("=== Strategy Scheduler Started ===")
    # ---------------
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped manually.")


run_scheduler()

# ----------------------------
# Main Loop
# ----------------------------

