"""
Scheduling smoke test: proves that the `schedule` wiring main.py relies on
actually fires at the expected local (IST) wall-clock time inside a
pm2-managed process on this host.

Deliberately READ-ONLY. It registers the job through the exact same
`getattr(schedule.every(), <weekday>).at("HH:MM")` mechanism main.py uses, but
the job only logs in, reads holdings, and sends a Telegram message. It never
imports StrategyRunner or ETFStrategy, so no order-placing code path is
reachable from here.

    python scripts/smoke_test_schedule.py [minutes_from_now]

Exits as soon as the job has fired once.
"""
import logging
import os
import sys
from datetime import datetime, timedelta

# Python puts this script's own directory on sys.path, not the cwd, so the repo
# root has to be added explicitly for "src" to be importable - unlike main.py,
# which already lives at the root. Everything in this project resolves paths
# relative to the repo root ("src/config.yaml", "logs/*.log"), so insist on
# being run from there rather than guessing.
REPO_ROOT = os.getcwd()
if not (os.path.isfile(os.path.join(REPO_ROOT, "main.py"))
        and os.path.isdir(os.path.join(REPO_ROOT, "src"))):
    sys.exit("Run this from the repo root: cd ~/strategy_automation && "
             "./venv/bin/python .claude/skills/deploy-ec2/smoke_test_schedule.py")
sys.path.insert(0, REPO_ROOT)

import schedule

from src.utils import load_config
from src.telegram_utils import send_telegram_message
from src.angel_api import AngelAPI

# Line-buffer stdout, otherwise progress is invisible until exit - under pm2
# that makes a working run look like a silent failure.
sys.stdout.reconfigure(line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("smoke_test")

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"]

fired = False


def job():
    """Read-only stand-in for run_trailing_sell / run_buy."""
    global fired
    fired = True
    actual = datetime.now()
    logger.info(f"JOB FIRED at {actual:%Y-%m-%d %H:%M:%S %Z} (local)")

    creds = load_config(section="trading")
    api = AngelAPI(
        api_key=creds["api_key"],
        user_name=creds["uid"],
        password=creds["password"],
        totp_secret=creds["totp_secret"],
    )
    summary = []
    try:
        api.login()
        logger.info("Broker login OK")
        holdings_df, cash = api.fetch_current_holdings_and_cash()
        logger.info(f"Read {len(holdings_df)} holdings, cash={cash}")
        summary.append(f"login OK, {len(holdings_df)} holdings, cash ₹{cash:,.0f}")
    except Exception as e:
        logger.exception("Broker read failed")
        summary.append(f"broker read FAILED: {type(e).__name__}: {e}")
    finally:
        try:
            api.logout()
        except Exception:
            logger.warning("logout failed", exc_info=True)

    send_telegram_message(
        "\U0001f9ea Scheduler smoke test\n"
        f"Job fired on time at {actual:%H:%M:%S} IST.\n"
        + "\n".join(summary)
        + "\nNo orders were placed."
    )
    logger.info("Telegram notification sent")


def main():
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    now = datetime.now()
    target = (now + timedelta(minutes=minutes)).replace(second=0, microsecond=0)
    day = WEEKDAYS[target.weekday()]
    at = target.strftime("%H:%M")

    # Same registration mechanism as main.py's run_scheduler().
    getattr(schedule.every(), day).at(at).do(job)

    logger.info(f"Now        : {now:%Y-%m-%d %H:%M:%S} ({WEEKDAYS[now.weekday()]})")
    logger.info(f"Scheduled  : {day} at {at}")
    logger.info(f"schedule says next run: {schedule.next_run()}")
    logger.info("Waiting (30s poll interval, same as main.py)...")

    # main.py sleeps 30s between run_pending() calls, so firing can lag the
    # target minute by up to 30s. Poll faster here only to timestamp it
    # precisely; the registration itself is what's under test.
    deadline = target + timedelta(minutes=3)
    while not fired:
        schedule.run_pending()
        if datetime.now() > deadline:
            logger.error("DID NOT FIRE within 3 minutes of target - scheduling is broken")
            send_telegram_message(
                "⚠️ Scheduler smoke test FAILED: job did not fire at "
                f"{at} IST. The scheduler would miss real trade times."
            )
            sys.exit(1)
        schedule.idle_seconds()
        import time as _t
        _t.sleep(1)

    logger.info("SUCCESS: scheduling mechanism works on this host.")


if __name__ == "__main__":
    main()
