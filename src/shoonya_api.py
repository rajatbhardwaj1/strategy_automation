import pandas as pd
import pyotp
import logging
from src.api_helper import ShoonyaApiPy # Assuming this path is correct
import src.utils as utils               # Assuming this path is correct
from datetime import datetime, time as dt_time
from typing import Optional, Dict, Any, List
from src.etf import ETFS as etf_data
from src.utils import fetch_all_etf_data
from src.telegram_utils import send_telegram_message
# --- Setup Logging ---
# We configure logging to write to a file and include timestamps/levels.
LOGGING_FILE = "logs/shoonya_api.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename=LOGGING_FILE,
    filemode='a' # Append to the log file
)
# Define a logger specific to this module
logger = logging.getLogger(__name__)


# --- Global Constants (for the class scope) ---
MARKET_CUTOFF_TIME = dt_time(14, 0, 0) # 2:00:00 PM for buy restriction
PRICE_ADJUSTMENT_DELTA = 0.01          # Aggressiveness for limit orders
ACTIVE_STATUSES: List[str] = ['OPEN', 'PENDING', 'TRIGGER_PENDING', 'NEW'] # Statuses indicating an order is active

# --- Custom Exceptions for better error handling ---
class LoginError(Exception):
    """Custom exception for Shoonya API login failures."""
    pass

class NotLoggedInError(Exception):
    """Custom exception for methods called when not logged in."""
    pass


def _is_buy_pre_cutoff() -> bool:
    """
    Internal method to check if a 'Buy' order is being attempted before the
    pre-defined MARKET_CUTOFF_TIME (e.g., 2 PM).
    Returns True if the order should be blocked, False otherwise.
    """
    current_time = datetime.now().time()
    if current_time < MARKET_CUTOFF_TIME:
        logger.warning(
            f"Order check blocked: Buy order attempted before {MARKET_CUTOFF_TIME}. "
            f"Current time: {current_time.strftime('%H:%M:%S')}."
        )
        return True
    return False


def _calculate_adjusted_price(side: str, order_type: str, price: float) -> float:
    """
    Internal method to calculate the adjusted limit price for aggressive orders.
    """
    if order_type == 'MKT' or price <= 0:
        return 0.0

    # Aggressive price adjustment using the defined constant
    if side == 'B':
        adjusted_price = price + PRICE_ADJUSTMENT_DELTA
    elif side == 'S':
        adjusted_price = price - PRICE_ADJUSTMENT_DELTA
    else:
        adjusted_price = price

    return round(adjusted_price, 2)


class ShoonyaAPI:
    """
    A robust wrapper class for the Shoonya API, managing login state,
    pre-order checks, and order placement.
    """
    def __init__(self, uid: str, password: str, api_key: str, totp_secret: str, vendor_key: str, imei: str):
        self.api: ShoonyaApiPy = ShoonyaApiPy()
        self.api_key: str = api_key
        self.user_id: str = uid
        self.password: str = password
        self.totp_secret: str = totp_secret
        self.vendor_key: str = vendor_key
        self.imei: str = imei
        self.is_logged_in: bool = False

    # ------------------
    # Core API Methods
    # ------------------
    def login(self) -> dict:
        """
        Performs the 2FA login process with the Shoonya API.
        Logs status, sends Telegram alerts on failure,
        and returns a structured response.

        Returns:
            dict: {
                "status": "success" | "already_logged_in" | "failed" | "error",
                "message": str,
                "response": Optional[dict]
            }
        """
        if self.is_logged_in:
            msg = f"User {self.user_id} is already logged in."
            logger.info(msg)
            return {"status": "already_logged_in", "message": msg, "response": None}

        try:
            totp = pyotp.TOTP(self.totp_secret)
            login_response = self.api.login(
                userid=self.user_id,
                password=self.password,
                twoFA=totp.now(),
                vendor_code=self.vendor_key,
                api_secret=self.api_key,
                imei=self.imei,
            )

            # --- Check for success ---
            if login_response and login_response.get("stat") == "Ok":
                self.is_logged_in = True
                client_name = login_response.get("uname", "Client")
                msg = f"✅ Successfully logged in as Shoonya {client_name} ({self.user_id})"
                logger.info(msg)

                # Optional: show extra message if present
                if "dmsg" in login_response:
                    dmsg = login_response["dmsg"]
                    logger.warning(f"Login message for {self.user_id}: {dmsg}")
                    send_telegram_message(f"{self.user_id}, dmsg: {dmsg}")

                return {
                    "status": "success",
                    "message": msg,
                    "response": login_response,
                }

            # --- Login failed case ---
            msg = f"❌ Failed to login for {self.user_id}. Response: {login_response}"
            logger.error(msg)
            send_telegram_message(msg)
            raise LoginError(msg)



        except Exception as e:
            msg = f"{self.user_id}, Unexpected error during login: {e}"
            logger.critical(msg, exc_info=True)
            send_telegram_message(msg)
            raise LoginError(msg) from e

    def logout(self):
        self.api.logout()
        logger.info("Successfully logged out")

    def place_buy_order(
            self,
            symbol: str,
            qty: int,
            order_type: str,
            price: float = 0.0,
            trigger: Optional[float] = None,
            remarks: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Places or modifies a trading order after performing login and pre-order checks.

        :param symbol: Trading symbol (e.g., 'NIFTYBEES').
        :param qty: Order quantity.
        :param order_type: 'LMT', 'MKT', 'SL-LMT', etc.
        :param price: Limit price.
        :param trigger: Trigger price for SL orders.
        :param remarks: Optional remarks.
        :return: API response dictionary.
        """
        if not self.is_logged_in:
            logger.error("Attempted to place order without logging in.")
            raise NotLoggedInError("Cannot place order: User is not logged in.")

        tradingsymbol_full = f"{symbol}-EQ"

        # --- Pre-buy checks ---
        if _is_buy_pre_cutoff():
            return {"stat": "REJECTED",
                    "emsg": f"Buy orders restricted before {MARKET_CUTOFF_TIME.strftime('%H:%M:%S')}"}

        if self._is_existing_order_active(symbol, 'B'):
            return {"stat": "REJECTED", "emsg": f"Active buy order for {symbol} already exists."}

        # --- Calculate adjusted price and place buy order ---
        adjusted_price = _calculate_adjusted_price('B', order_type, price)
        order_response = self.api.place_order(
            buy_or_sell='B',
            product_type='C',
            exchange='NSE',
            tradingsymbol=tradingsymbol_full,
            quantity=qty,
            discloseqty=0,
            price_type=order_type,
            price=adjusted_price,
            trigger_price=trigger,
            retention='DAY',
            remarks=remarks
        )

        # --- Handle API response ---
        if order_response.get("stat") == "Ok":
            logger.info(f"Buy order placed successfully: {order_response.get('norenordno')} for {symbol}.")
            send_telegram_message(f"Buy order for {qty} {symbol} placed at {adjusted_price}")
        else:
            emsg = order_response.get('emsg', 'Unknown API Error')
            logger.error(f"Buy order failed for {symbol}. Response: {order_response}")
            send_telegram_message(f"Buy order failed for {symbol}: {emsg}")

        return order_response


    def trail_and_place_sell_order(
            self,
            symbol: str,
            qty: int,
            order_type: str = 'S',
            price: float = 0.0,
            trigger: Optional[float] = None,
            remarks: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Places or modifies a trading order after performing login and pre-order checks.

        :param symbol: Trading symbol (e.g., 'NIFTYBEES').
        :param qty: Order quantity.
        :param side: 'B' (Buy) or 'S' (Sell).
        :param order_type: 'LMT', 'MKT', 'SL-LMT', etc.
        :param price: Limit price.
        :param trigger: Trigger price for SL orders.
        :param remarks: Optional remarks.
        :return: API response dictionary.
        """
        if not self.is_logged_in:
            logger.error("Attempted to place order without logging in.")
            raise NotLoggedInError("Cannot place order: User is not logged in.")

        tradingsymbol_full = f"{symbol}-EQ"


        # --- Check for existing active sell orders ---
        orders_data = self.api.get_order_book()
        existing_order = None
        if orders_data:
            orders = pd.DataFrame(orders_data)
            tradingsymbol_full = f"{symbol}-EQ"
            if all(col in orders.columns for col in ['tsym', 'trantype', 'status', 'norenordno', 'prc']):
                active_orders = orders[
                    (orders['tsym'] == tradingsymbol_full) &
                    (orders['trantype'] == 'S') &
                    (orders['status'].isin(ACTIVE_STATUSES))
                    ]
                if not active_orders.empty:
                    existing_order = active_orders.iloc[0]

        if existing_order is not None:
            # --- Modify order if current sell price is higher ---
            current_price = price
            previous_price = float(existing_order['prc'])
            if current_price > previous_price:
                orderno = existing_order['norenordno']
                modify_response = self.api.modify_order(
                    exchange='NSE',
                    tradingsymbol=tradingsymbol_full,
                    orderno=orderno,
                    newquantity=qty,
                    newprice_type=order_type,
                    newprice=current_price,
                    newtrigger_price=trigger,
                    amo='No'
                )
                logger.info(f"Sell order modified for {symbol}: {modify_response}")
                send_telegram_message(f"Modified sell order for {symbol} to price {current_price}")
                return modify_response
            else:
                logger.info(f"Existing sell order price for {symbol} is higher. No modification done.")
                return {"stat": "SKIPPED", "emsg": "Existing sell order price higher; no action taken."}

        # --- Place new sell order if no active order exists ---
        adjusted_price = _calculate_adjusted_price("S", order_type, price)
        order_response = self.api.place_order(
            buy_or_sell="S",
            product_type='C',
            exchange='NSE',
            tradingsymbol=tradingsymbol_full,
            quantity=qty,
            discloseqty=0,
            price_type=order_type,
            price=adjusted_price,
            trigger_price=trigger,
            retention='DAY',
            remarks=remarks
        )

        if order_response.get("stat") == "Ok":
            logger.info(f"Sell order placed successfully: {order_response.get('norenordno')} for {symbol}.")
            send_telegram_message(f"Sell order for {qty} {symbol} placed at {adjusted_price}")
        else:
            emsg = order_response.get('emsg', 'Unknown API Error')
            logger.error(f"Sell order failed for {symbol}. Response: {order_response}")
            send_telegram_message(f"Sell order failed for {symbol}: {emsg}")

        return order_response




    def fetch_current_holdings_and_cash(self) -> tuple[pd.DataFrame, float , pd.DataFrame]:
        """
        Fetch current ETF holdings and total cash from Shoonya API.

        :return: Tuple of (holdings DataFrame, total investable cash)
        """
        if not self.is_logged_in:
            logger.error("Attempted to fetch holdings while not logged in.")
            raise NotLoggedInError("Cannot fetch holdings: User is not logged in.")

        # --- Fetch cash and limits ---
        limits = self.api.get_limits()
        cash = float(limits.get("cash", 0.0)) + float(limits.get("payin", 0.0))
        etf_prices_df = fetch_all_etf_data()

        # --- Fetch holdings ---
        raw_holdings = pd.DataFrame(self.api.get_holdings())
        if raw_holdings.empty:
            logger.info("No holdings found for user.")
            empty_df = pd.DataFrame(
                columns=["etf", "held_qty", "buy_price", "invested", "ltp", "pct_diff"]
            )
            return empty_df, total_invest, etf_prices_df

        # --- Prepare holdings data ---
        holdings_data = []
        for _, row in raw_holdings.iterrows():
            qty = int(row.get("holdqty", 0)) or int(row.get("npoadqty", 0))
            if qty <= 0:
                continue
            invested_value = qty * float(row.get("upldprc", 0.0))

            for entry in row.get("exch_tysm", []):
                tsym = entry["tsym"].replace("-EQ", "")
                holdings_data.append(
                    {
                        "etf": tsym,
                        "held_qty": qty,
                        "buy_price": float(row.get("upldprc", 0.0)),
                        "invested": invested_value,
                    }
                )

        if not holdings_data:
            empty_df = pd.DataFrame(
                columns=["etf", "held_qty", "buy_price", "invested", "ltp", "pct_diff"]
            )
            return empty_df, cash, etf_prices_df

        # --- Fetch current ETF prices ---
        if etf_prices_df.empty:
            msg = "Failed to fetch ETF prices. Cannot proceed with holdings calculation."
            logger.error(msg)
            send_telegram_message(msg)
            raise RuntimeError(msg)  # Stop further execution

        # --- Combine holdings with current prices ---
        holdings_df = pd.DataFrame(holdings_data)
        holdings_df = holdings_df.merge(
            etf_prices_df[["etf", "ltp"]], on="etf", how="left"
        )
        holdings_df["pct_diff"] = (
                (holdings_df["ltp"] - holdings_df["buy_price"]) / holdings_df["buy_price"] * 100
        ).round(2)

        logger.info(f"Fetched {len(holdings_df)} ETF holdings. Total investable cash: {total_invest}")

        return holdings_df, cash , etf_prices_df


    # ------------------
    # Helper/Internal Methods (SRP)
    # ------------------

    def _is_existing_order_active(self, symbol: str, side: str) -> bool:
        """
        Internal method to check if an active, non-final order for the given symbol and side exists.
        Returns True if an active order exists, False otherwise.
        """
        if not self.is_logged_in:
            logger.warning("Attempted _is_existing_order_active check while logged out.")
            return False

        orders_data = self.api.get_order_book()
        if not orders_data:
            return False

        try:
            orders = pd.DataFrame(orders_data)
        except ValueError as e:
            logger.error(f"Failed to convert order book to DataFrame: {e}")
            return False # Fail safe: assume no existing order if data is malformed

        tradingsymbol_full = f"{symbol}-EQ"

        # Robust check for required columns
        required_cols = ['tsym', 'trantype', 'status', 'norenordno']
        if not all(col in orders.columns for col in required_cols):
             logger.warning(f"Order book response missing required columns for filtering: {required_cols}")
             return False

        # Filter for the symbol, side, and active statuses
        active_orders = orders[
            (orders['tsym'] == tradingsymbol_full) &
            (orders['trantype'] == side) &
            (orders['status'].isin(ACTIVE_STATUSES))
        ]

        if not active_orders.empty:
            logger.warning(
                f"Order check blocked: Active order for {symbol} ({side}) already exists. "
                f"ID: {active_orders['norenordno'].iloc[0]}"
            )
            return True

        return False
