"""
Daily trade recorder.

After every market close it reads the broker's own order and trade books,
appends anything new to trades/trades.csv, regenerates trades/REPORT.md, and
pushes both to the private repo so there is an off-box record.

    python trade_logger.py              # run the scheduler (what pm2 runs)
    python trade_logger.py --once       # record right now and exit
    python trade_logger.py --import-statements a.csv b.xlsx --since 2025-10-01
    python trade_logger.py --report-only

Read-only against the broker: it fetches books and holdings and places nothing.
"""
import argparse
import logging
import sys
import time
from datetime import date, datetime

import schedule

from src.angel_api import AngelAPI
from src.git_sync import commit_and_push
from src.metrics import compute_metrics
from src.trade_import import import_statements
from src.trade_log import (
    LEDGER_PATH,
    fetch_broker_books,
    load_ledger,
    merge_into_ledger,
    normalise_orders,
)
from src.trade_report import REPORT_PATH, write_report
from src.telegram_utils import send_telegram_message
from src.utils import load_config

LOG_FILE = "logs/trade_logger.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=LOG_FILE,
    filemode="a",
)
logger = logging.getLogger(__name__)

# NSE closes at 15:30 IST. 16:05 leaves room for the order book to settle
# while staying well clear of the 15:20 buy the scheduler runs.
RUN_AT = "16:05"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


def _broker() -> AngelAPI:
    creds = load_config(section="trading")
    return AngelAPI(
        api_key=creds["api_key"],
        user_name=creds["uid"],
        password=creds["password"],
        totp_secret=creds["totp_secret"],
    )


def _fetch_holdings(api):
    """
    Holdings value the current position for the report. A failure here is not
    fatal - the ledger is still worth recording - so it degrades to None, which
    the report renders as "n/a" rather than as a zero-valued portfolio.
    """
    try:
        return api.fetch_current_holdings_and_cash()
    except Exception as e:
        logger.warning("Holdings unavailable: %s: %s", type(e).__name__, e, exc_info=True)
        return None, None


def record_today(push: bool = True) -> dict:
    """Fetch today's books, merge, regenerate the report, push."""
    api = _broker()
    api.login()
    logger.info("Broker login OK.")
    try:
        orderbook, tradebook = fetch_broker_books(api)
        new_rows = normalise_orders(orderbook, tradebook)
        ledger, added, updated = merge_into_ledger(new_rows, LEDGER_PATH)
        holdings_df, cash = _fetch_holdings(api)
    finally:
        try:
            api.logout()
        except Exception:
            logger.warning("Logout failed.", exc_info=True)

    write_report(ledger, holdings_df, REPORT_PATH, date.today(), cash)
    metrics = compute_metrics(ledger, holdings_df)

    status = "push skipped"
    if push:
        status = commit_and_push(
            [LEDGER_PATH, REPORT_PATH],
            f"Trade record for {date.today().isoformat()} "
            f"({added} new, {updated} updated)",
        )

    summary = {"added": added, "updated": updated, "rows": len(ledger),
               "git": status, "metrics": metrics}
    logger.info("Run complete: %s", summary)
    return summary


def notify(summary: dict) -> None:
    m = summary["metrics"]
    xirr = "n/a" if m["xirr"] is None else f"{m['xirr'] * 100:+.1f}%"
    pnl = m["total_pnl_net"]
    pnl_text = "n/a" if pnl is None else f"₹{pnl:+,.0f}"
    send_telegram_message(
        "📒 Trade record updated\n"
        f"New orders: {summary['added']} (updated {summary['updated']})\n"
        f"Ledger: {summary['rows']} rows\n"
        f"Net P&L: {pnl_text}   XIRR: {xirr}\n"
        f"Git: {summary['git']}"
    )


def run_once(push: bool = True) -> None:
    try:
        notify(record_today(push=push))
    except Exception as e:
        logger.exception("Trade recording failed.")
        send_telegram_message(
            f"🔥 Trade record failed\n{type(e).__name__}: {e}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true",
                        help="Record now and exit, instead of scheduling.")
    parser.add_argument("--no-push", action="store_true",
                        help="Write the ledger and report but do not touch git.")
    parser.add_argument("--report-only", action="store_true",
                        help="Regenerate the report from the existing ledger. No broker call.")
    parser.add_argument("--import-statements", nargs="+", metavar="FILE",
                        help="Import Angel One TradeBook And Charges exports (.csv/.xlsx).")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="With --import-statements: ignore trades before this date.")
    args = parser.parse_args()

    if args.import_statements:
        since = date.fromisoformat(args.since) if args.since else None
        rows, summaries = import_statements(args.import_statements, since)
        for s in summaries:
            print(f"{s['file']}: parsed {s['parsed']}, imported {s['imported']}, "
                  f"skipped {s['skipped_before_cutoff']} before cutoff")
            if s["unmapped_scrips"]:
                print("  scrips with no explicit ticker mapping "
                      "(add them to SCRIP_TO_SYMBOL in src/trade_import.py):")
                for scrip, count in sorted(s["unmapped_scrips"].items()):
                    print(f"    {scrip}  x{count}")
        ledger, added, updated = merge_into_ledger(rows, LEDGER_PATH)
        print(f"\nLedger now {len(ledger)} rows ({added} added, {updated} updated).")
        return 0

    if args.report_only:
        ledger = load_ledger(LEDGER_PATH)
        api = _broker()
        holdings_df, cash = None, None
        try:
            api.login()
            holdings_df, cash = _fetch_holdings(api)
        except Exception as e:
            logger.warning("Broker unavailable for valuation: %s", e)
        finally:
            try:
                api.logout()
            except Exception:
                pass
        write_report(ledger, holdings_df, REPORT_PATH, date.today(), cash)
        print(f"Wrote {REPORT_PATH} from {len(ledger)} ledger rows.")
        return 0

    if args.once:
        run_once(push=not args.no_push)
        return 0

    for day in WEEKDAYS:
        getattr(schedule.every(), day).at(RUN_AT).do(run_once)
    logger.info("Trade logger scheduled for %s on %s.", RUN_AT, ", ".join(WEEKDAYS))
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
