import pandas as pd
import pyotp
import logging
from src.api_helper import ShoonyaApiPy # Assuming this path is correct
import src.utils as utils               # Assuming this path is correct
from datetime import datetime, time as dt_time
from typing import Optional, Dict, Any, List
from src.etf import ETFS as etf_data
from src.utils import fetch_all_etf_data

# --- Setup Logging ---
# We configure logging to write to a file and include timestamps/levels.
LOGGING_FILE = "strategy.log"
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

    def login(self) -> None:
        """
        Performs the 2FA login process with the Shoonya API.
        Logs status and raises LoginError on failure.
        """
        if self.is_logged_in:
            logger.info(f"User {self.user_id} is already logged in.")
            return

        try:
            totp = pyotp.TOTP(self.totp_secret)
            login_response: Optional[Dict[str, Any]] = self.api.login(
                userid=self.user_id, password=self.password, twoFA=totp.now(),
                vendor_code=self.vendor_key, api_secret=self.api_key, imei=self.imei
            )

            # Check for successful login status 'Ok'
            if login_response and login_response.get('stat') == 'Ok':
                self.is_logged_in = True
                client_name = login_response.get('uname', 'Client')
                logger.info(f"Successfully logged in as Shoonya {client_name} ({self.user_id})")
            else:
                msg = f"Failed to login for {self.user_id}. Response: {login_response}"
                logger.error(msg)
                utils.send_telegram_message(msg)
                # Raise specific exception instead of exiting
                raise LoginError(msg)

            # Log any dmsg if present (e.g., session message)
            if 'dmsg' in login_response:
                dmsg = login_response['dmsg']
                logger.warning(f"Login message for {self.user_id}: {dmsg}")
                utils.send_telegram_message(f"{self.user_id}, dmsg: {dmsg}")

        except pyotp.otp.Base32KeyError as e:
            logger.critical(f"Invalid TOTP secret format for {self.user_id}. Error: {e}")
            raise LoginError(f"Invalid TOTP secret format: {e}") from e
        except Exception as e:
            # Catch other potential connection or API errors
            error_msg = f"{self.user_id}, Unexpected error during login: {e}"
            logger.critical(error_msg, exc_info=True)
            utils.send_telegram_message(error_msg)
            raise LoginError(error_msg) from e

    def place_order(self, symbol: str, qty: int, side: str, order_type: str, price: float = 0.0, trigger: Optional[float] = None, remarks: Optional[str] = None) -> Dict[str, Any]:
        """
        Places a trading order after performing preliminary login and risk checks.

        :param symbol: The trading symbol (e.g., 'NIFTYBEES').
        :param qty: The order quantity.
        :param side: 'B' (Buy) or 'S' (Sell).
        :param order_type: 'LMT', 'MKT', 'SL-LMT', etc.
        :param price: The limit price.
        :param trigger: The trigger price (for SL orders).
        :param remarks: Optional remarks for the order.
        :return: The API response dictionary for the order placement.
        """
        # 1. Check Login State
        if not self.is_logged_in:
            logger.error("Attempted to place order without logging in.")
            raise NotLoggedInError("Cannot place order: User is not logged in.")

        # 2. Pre-order Checks (SRP: delegate checks to dedicated methods)
        if self._is_buy_pre_cutoff(side=side):
            return {"stat": "REJECTED", "emsg": f"Buy orders restricted before {MARKET_CUTOFF_TIME.strftime('%H:%M:%S')}."}

        if self._is_existing_order_active(symbol, side):
            return {"stat": "REJECTED", "emsg": f"Active order for {symbol} ({side}) already exists."}

        # 3. Prepare Order Parameters
        tradingsymbol_full = f"{symbol}-EQ"
        adjusted_price = self._calculate_adjusted_price(side, order_type, price)

        # 4. Place Order API Call
        order_response = self.api.place_order(
            buy_or_sell=side, product_type='C', exchange='NSE', tradingsymbol=tradingsymbol_full,
            quantity=qty, discloseqty=0, price_type=order_type, price=adjusted_price,
            trigger_price=trigger, retention='DAY', remarks=remarks
        )

        # 5. Handle and Log API Response
        if order_response and order_response.get('stat') == 'Ok':
            logger.info(f"Order placed successfully: {order_response.get('norenordno')} for {symbol}.")
            utils.send_telegram_message(f"{side} order for {qty} {symbol} placed at {adjusted_price}")

        else:
            emsg = order_response.get('emsg', 'Unknown API Error')
            logger.error(f"Order failed for {symbol}. Response: {order_response}")
            utils.send_telegram_message(f"Order failed for {symbol}: {emsg}")

        return order_response

    def fetch_current_holdings_and_cash(self) -> tuple[pd.DataFrame, float]:
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
        total_invest = cash

        # --- Fetch holdings ---
        raw_holdings = pd.DataFrame(self.api.get_holdings())
        if raw_holdings.empty:
            logger.info("No holdings found for user.")
            empty_df = pd.DataFrame(
                columns=["etf", "held_qty", "buy_price", "invested", "ltp", "pct_diff"]
            )
            return empty_df, total_invest

        # --- Prepare holdings data ---
        holdings_data = []
        for _, row in raw_holdings.iterrows():
            qty = int(row.get("holdqty", 0)) or int(row.get("npoadqty", 0))
            if qty <= 0:
                continue
            invested_value = qty * float(row.get("upldprc", 0.0))
            total_invest += invested_value

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
            return empty_df, total_invest

        # --- Fetch current ETF prices ---
        etf_prices_df = fetch_all_etf_data()
        if etf_prices_df.empty:
            msg = "Failed to fetch ETF prices. Cannot proceed with holdings calculation."
            logger.error(msg)
            utils.send_telegram_message(msg)
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

        return holdings_df, total_invest


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

    def _is_buy_pre_cutoff(self, side: str) -> bool:
        """
        Internal method to check if a 'Buy' order is being attempted before the
        pre-defined MARKET_CUTOFF_TIME (e.g., 2 PM).
        Returns True if the order should be blocked, False otherwise.
        """
        if side == 'B':
            current_time = datetime.now().time()
            if current_time < MARKET_CUTOFF_TIME:
                logger.warning(
                    f"Order check blocked: Buy order attempted before {MARKET_CUTOFF_TIME}. "
                    f"Current time: {current_time.strftime('%H:%M:%S')}."
                )
                return True
        return False

    def _calculate_adjusted_price(self, side: str, order_type: str, price: float) -> float:
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
