import pandas as pd
import pyotp
import time
import logging
from src.api_helper import ShoonyaApiPy
import src.utils as utils
from datetime import datetime, time as dt_time
from typing import Optional, Dict, Any


# --- Setup Logging ---
# In a real application, logging setup would be more extensive (e.g., to file/stdout)
LOGGING_FILE = "strategy.log"
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', filename=LOGGING_FILE)

# --- Global Constants (for the class scope) ---
MARKET_CUTOFF_TIME = dt_time(14, 0, 0) # 2:00:00 PM
PRICE_ADJUSTMENT_DELTA = 0.01


# --- Custom Exceptions for better error handling ---
class LoginError(Exception):
    """Custom exception for Shoonya API login failures."""
    pass

class NotLoggedInError(Exception):
    """Custom exception for methods called when not logged in."""
    pass


class ShoonyaAPI:
    """
    A wrapper class for the Shoonya API to handle login, order checks, and order placement.
    """
    def __init__(self, uid: str, password: str, api_key: str, totp_secret: str, vendor_key: str, imei: str):
        # Use descriptive variable names
        self.api: ShoonyaApiPy = ShoonyaApiPy()
        self.api_key: str = api_key
        self.user_id: str = uid  # Renamed uid to user_id
        self.password: str = password
        self.totp_secret: str = totp_secret
        self.vendor_key: str = vendor_key
        self.imei: str = imei
        self.is_logged_in: bool = False

    def login(self) -> None:
        """
        Performs the 2FA login process with the Shoonya API.
        Raises LoginError on failure.
        """
        if self.is_logged_in:
            logging.info(f"User {self.user_id} is already logged in.")
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
                logging.info(f"Successfully logged in as Shoonya {client_name} ({self.user_id})")
            else:
                msg = f"Failed to login for {self.user_id}. Response: {login_response}"
                logging.error(msg)
                utils.send_telegram_message(msg)
                # Raise specific exception instead of exiting
                raise LoginError(msg)

            # Log any dmsg if present (e.g., session message)
            if 'dmsg' in login_response:
                dmsg = login_response['dmsg']
                logging.warning(f"Login message for {self.user_id}: {dmsg}")
                utils.send_telegram_message(f"{self.user_id}, dmsg: {dmsg}")

        except pyotp.otp.Base32KeyError as e:
            logging.critical(f"Invalid TOTP secret format for {self.user_id}. Error: {e}")
            raise LoginError(f"Invalid TOTP secret format: {e}") from e
        except Exception as e:
            # Catch other potential connection or API errors
            error_msg = f"{self.user_id}, Unexpected error during login: {e}"
            logging.critical(error_msg, exc_info=True)
            utils.send_telegram_message(error_msg)
            raise LoginError(error_msg) from e


    def check_existing_order(self,etf , side):
        orders = pd.DataFrame(self.api.get_order_book())
        etf_symbol = f"{etf}-EQ"
        if not orders.empty:
            existing_orders = orders[['tsym', 'trantype', 'prc', 'status', 'norenordno']] if 'status' in orders else \
                orders[['tsym', 'trantype', 'rejreason']]

            # Check if the same order already exists
            active_orders = existing_orders[
                (existing_orders['tsym'] == etf_symbol) &
                (existing_orders['trantype'] == side) &
                (existing_orders['status'].notna()) &
                (~existing_orders['status'].isin(['CANCELED', 'REJECTED']))
                ]
            if not active_orders.empty:
                print(f"Order for {etf} ({side}) already exists in the system.")
                return True

        return False

    def check_early_sell(self,side):
        current_time = datetime.now().time()
        market_cutoff = datetime.strptime("14:00:00", "%H:%M:%S").time()

        # Restrict buy orders before 2 PM
        if side == 'B' and current_time < market_cutoff:
            print(f"Buy orders cannot be placed before 2 PM. Current time: {current_time}.")
            return


    def place_order(self, etf, qty,side, order_type, price=0, trigger=None, remarks=None):
        etf_symbol = f"{etf}-EQ"
        adjusted_price = round(price + 0.01 if side == 'B' else price - 0.01, 2) if order_type != 'MKT' else 0
        if self.check_early_sell() or self.check_existing_order(etf , side):
            return
        order_response = self.api.place_order(
            buy_or_sell=side, product_type='C', exchange='NSE', tradingsymbol=etf_symbol,
            quantity=qty, discloseqty=0, price_type=order_type, price=adjusted_price,
            trigger_price=trigger, retention='DAY', remarks=remarks
        )


















