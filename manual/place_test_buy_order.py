"""
MANUAL test: places a REAL buy order on the configured Angel One account.

Lives outside test/ on purpose. As a pytest test this ran on a bare `pytest`
and transacted; nothing here is collected by pytest, and it refuses to do
anything without an explicit typed confirmation.

    cd ~/strategy_automation
    ./venv/bin/python manual/place_test_buy_order.py

Defaults to 1 share of SILVERBEES as a LIMIT order priced 20% BELOW the last
traded price, so it rests on the book unfilled rather than executing.

The direction matters and is easy to get backwards: a BUY limit ABOVE market
fills immediately at the best available price. It does not rest. So the default
price is derived from the live LTP rather than hardcoded, and the script refuses
a limit at or above LTP unless you pass --force - a fixed figure silently
becomes a market buy as soon as the instrument moves.

Requires the instance's public IP to be registered with Angel One SmartAPI,
or the order is rejected with AG7002.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.angel_api import AngelAPI
from src.utils import fetch_etf_data, load_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="SILVERBEES")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--price", type=float, default=None,
                        help="LIMIT price. Default: 20%% below the last traded price, so the "
                             "order rests unfilled. A limit at or above LTP fills immediately.")
    parser.add_argument("--order-type", default="LIMIT", choices=["LIMIT", "MARKET"])
    parser.add_argument("--yes", action="store_true",
                        help="Skip the typed confirmation. For non-interactive use only.")
    parser.add_argument("--force", action="store_true",
                        help="Allow a LIMIT at or above LTP, which fills immediately.")
    args = parser.parse_args()

    ltp = None
    if args.order_type == "LIMIT":
        data = fetch_etf_data(args.symbol, max_retries=2, delay=2)
        if data:
            ltp = data["ltp"]
        elif args.price is None:
            print(f"Could not fetch a price for {args.symbol}, so no safe default "
                  "limit can be derived. Pass --price explicitly.")
            return 1

        if args.price is None:
            args.price = round(ltp * 0.80, 2)
        elif ltp is not None and args.price >= ltp and not args.force:
            print(f"Refusing: a BUY limit of {args.price} is at or above the last traded "
                  f"price of {ltp}, so it will FILL IMMEDIATELY rather than rest on the "
                  "book. Lower it, or pass --force if an immediate fill is what you want.")
            return 1

    print("=" * 68)
    print("  THIS PLACES A REAL ORDER ON A REAL ACCOUNT WITH REAL MONEY")
    print("=" * 68)
    print(f"  Symbol     : {args.symbol}")
    print(f"  Side       : BUY")
    print(f"  Quantity   : {args.qty}")
    print(f"  Order type : {args.order_type}")
    if args.order_type == "LIMIT":
        gap = f", {abs(round((args.price / ltp - 1) * 100, 1))}% below LTP {ltp}" if ltp else ""
        print(f"  Limit price: {args.price}{gap}")
        if ltp and args.price >= ltp:
            print("  WARNING    : at/above LTP - this will fill immediately, not rest")
    else:
        print("  Limit price: n/a - a MARKET order fills immediately at whatever the book offers")
    print("=" * 68)

    if not args.yes:
        if input('Type "place order" to continue, anything else to abort: ').strip() != "place order":
            print("Aborted. No order was sent.")
            return 1

    creds = load_config(section="trading")
    api = AngelAPI(
        api_key=creds["api_key"],
        user_name=creds["uid"],
        password=creds["password"],
        totp_secret=creds["totp_secret"],
    )

    login_response = api.login()
    if login_response.get("status") not in ("success", "already_logged_in"):
        print(f"Login failed, no order sent: {login_response}")
        return 1
    print(f"Logged in: {login_response.get('status')}")

    kwargs = {"price": args.price} if args.order_type == "LIMIT" else {}
    buy_response = api.place_buy_order(args.symbol, args.qty, args.order_type, **kwargs)
    print(f"\nOrder response: {buy_response}")

    if not buy_response.get("status"):
        print("\nOrder was NOT accepted. If the message mentions a registered IP "
              "(AG7002), add this instance's public IP in the Angel One SmartAPI "
              "app settings. Rejections are also logged to logs/<date>/app.log.")
        return 1

    print("\nOrder accepted. Check the Angel One app - cancel it rather than leaving "
          "it to rest, since a resting buy fills if the price rises to meet it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
