import pandas as pd
import pyotp
import logging
from SmartApi import SmartConnect  # SmartAPI SDK
from datetime import datetime, time as dt_time, timedelta
from typing import Optional, Dict, Any, List, Tuple
import requests
import json
import os
from src.telegram_utils import send_telegram_message
import asyncio

import time
import telegram  # Assuming python-telegram-bot is installed

# --- Setup Logging ---
LOGGING_FILE = "logs/angel_api.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename=LOGGING_FILE,
    filemode='a'  # Append to the log file
)
logger = logging.getLogger(__name__)

# --- Global Constants (for the class scope) ---
MARKET_CUTOFF_TIME = dt_time(14, 0, 0)  # 2:00:00 PM for buy restriction
PRICE_ADJUSTMENT_DELTA = 0.01  # Aggressiveness for limit orders
# **CORRECTION:** Using Angel Broking's actual active order statuses (typically lower-case)
ACTIVE_STATUSES: List[str] = ['open', 'pending', 'trigger pending']


# --- Custom Exceptions for better error handling ---
class LoginError(Exception):
    """Custom exception for Angel API login failures."""
    pass


class NotLoggedInError(Exception):
    """Custom exception for methods called when not logged in."""
    pass


# ----------------------------------------------------
# --- Utility Functions for Pre-Checks and Symbol Data ---
# ----------------------------------------------------

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

    :param side: 'BUY' or 'SELL'. (Corrected to use full words)
    :param order_type: 'LIMIT', 'MARKET', 'STOPLOSS_LIMIT', 'STOPLOSS_MARKET'.
    """
    # **CORRECTION:** Check for MARKET order type using case-insensitive check
    if 'MARKET' in order_type.upper() or price <= 0:
        return 0.0

    # Aggressive price adjustment using the defined constant
    if side.upper() == 'BUY':
        adjusted_price = price + PRICE_ADJUSTMENT_DELTA
    elif side.upper() == 'SELL':
        adjusted_price = price - PRICE_ADJUSTMENT_DELTA
    else:
        # Fallback for unexpected side
        adjusted_price = price

    return round(adjusted_price, 2)


def get_tradeable_symbols() -> List[Dict[str, str]]:
    """
    Downloads or reads the Angel Broking script master file.
    Returns a list of dictionaries with symbol details.
    """
    OUTPUT_FILE = 'angel_symbols.txt'
    # Check for the file using the already imported 'os'
    if not os.path.exists(OUTPUT_FILE):
        try:
            # **CORRECTION**: Removed redundant 'import os' here.
            url = 'https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json'
            response = requests.get(url)
            # Ensure the request was successful
            if response.status_code != 200:
                logger.error(f"Failed to download symbols file. Status code: {response.status_code}")
                return []

            data = response.json()
            # Filter symbols that end with '-EQ' to focus on Equity segments
            symbols = [item for item in data if item['symbol'].endswith('-EQ')]
            with open(OUTPUT_FILE, 'w') as f:
                for symbol in symbols:
                    f.write(json.dumps(symbol) + '\n')
            logger.info(f"Filtered Angel symbols saved to {OUTPUT_FILE}")
            return symbols
        except Exception as e:
            logger.error(f"Exception in downloading tradeable symbols: {e}")
            return []
    else:
        symbols = []
        try:
            with open(OUTPUT_FILE, 'r') as f:
                for line in f:
                    symbols.append(json.loads(line.strip()))
            return symbols
        except Exception as e:
            logger.error(f"Exception in reading tradeable symbols file: {e}")
            return []



# --- Core AngelAPI Class ---

class AngelAPI:
    """
    A robust wrapper class for the Angel Broking (SmartConnect) API,
    managing login state, symbol mapping, and order handling.
    """
    def __init__(self,
                 api_key: str,
                 user_name: str,
                 password: str,
                 totp_secret: str,
                 telegram_bot_token: Optional[str] = None,
                 telegram_ids: Optional[List[str]] = None,
                 web_id: Optional[str] = None):

        self.api_key: str = api_key
        self.user_name: str = user_name
        self.password: str = password
        self.totp_secret: str = totp_secret
        self.api: Optional[SmartConnect] = None
        self.is_logged_in: bool = False
        self.symbols_map: List[Dict[str, str]] = get_tradeable_symbols() # Load symbols once

    def _find_symbol_details(self, etf_name: str) -> Optional[Tuple[str, str, str]]:
        """
        Maps a simple ETF name (e.g., 'NIFTYBEES') to its token, exchange, and full symbol.
        Returns (token, exchange, full_symbol) or None.
        """
        match = next((s for s in self.symbols_map if s.get('name') == etf_name), None)
        if match:
            return match.get('token'), match.get('exch_seg'), match.get('symbol')
        logger.error(f"Token/Symbol details not found for ETF: {etf_name}")
        return None

    # ------------------
    # Core API Methods
    # ------------------
    def login(self) -> dict:
        """
        Performs the 2FA login process with the Angel API.
        Returns a structured response.
        """
        if self.is_logged_in and self.api is not None:
            msg = f"User {self.user_name} is already logged in."
            logger.info(msg)
            return {"status": "already_logged_in", "message": msg, "response": None}

        try:
            self.api = SmartConnect(api_key=self.api_key)
            totp = pyotp.TOTP(self.totp_secret).now()
            login_response = self.api.generateSession(self.user_name, self.password, totp)

            # --- Check for success ---
            if login_response and login_response.get("status") is True:
                self.is_logged_in = True
                client_name = login_response.get("data", {}).get("name", "Client")
                msg = f"✅ Successfully logged in as Angel {client_name} ({self.user_name})"
                logger.info(msg)
                send_telegram_message(msg)
                return {
                    "status": "success",
                    "message": msg,
                    "response": login_response,
                }

            # --- Login failed case ---
            msg = f"❌ Failed to login for {self.user_name}. Response: {login_response}"
            logger.error(msg)
            send_telegram_message(msg)
            raise LoginError(msg)

        except Exception as e:
            msg = f"{self.user_name}, Unexpected error during login: {e}"
            logger.critical(msg, exc_info=True)
            send_telegram_message(msg)
            raise LoginError(msg) from e

    def logout(self):
        if self.api:
            self.api.terminateSession(self.user_name)
        self.is_logged_in = False
        logger.info("Successfully logged out")

    def _get_active_orders(self, symbol: str, side: str) -> pd.DataFrame:
        """Internal helper to fetch and filter active orders."""
        orders_data = self.api.orderBook()
        if not orders_data or orders_data.get('status') is False:
            logger.warning("Failed to fetch order book.")
            return pd.DataFrame()

        orders = pd.DataFrame(orders_data['data'])
        tradingsymbol_full = f"{symbol}-EQ"

        # Check for existence of required columns before filtering
        required_cols = ['tradingsymbol', 'transactiontype', 'status']
        if not all(col in orders.columns for col in required_cols):
            logger.warning(f"Order book response missing required columns for filtering: {required_cols}, current cols: {orders.columns}")
            return pd.DataFrame()

        active_orders = orders[
            (orders['tradingsymbol'] == tradingsymbol_full) &
            (orders['transactiontype'] == side.upper()) &
            (orders['status'].isin(ACTIVE_STATUSES))
            ]
        return active_orders

    # ------------------
    # Order Placement Methods (Refactored)
    # ------------------

    def _execute_order_placement(
            self,
            symbol: str,
            qty: int,
            side: str,
            order_type: str,
            price: float,
            trigger: Optional[float],
            remarks: Optional[str]
    ) -> Dict[str, Any]:
        """Internal helper to construct and place the order via SmartAPI."""
        details = self._find_symbol_details(symbol)
        if not details:
            return {"status": False, "message": f"Symbol details not found for {symbol}"}

        token, exch, tradingsymbol_full = details

        adjusted_price = _calculate_adjusted_price(side, order_type, price)
        variety = "STOPLOSS" if "STOPLOSS" in order_type.upper() else "NORMAL"
        trigger = round(trigger, 2) if trigger is not None else None

        orderparams = {
            "variety": variety,
            "tradingsymbol": tradingsymbol_full,
            "symboltoken": token,
            "transactiontype": side.upper(),
            "exchange": exch,
            "ordertype": order_type.upper(),
            "producttype": "DELIVERY",
            "duration": "DAY",
            "price": adjusted_price,
            "squareoff": "0",
            "triggerprice": trigger,
            "quantity": qty
        }

        try:
            order_response = self.api.placeOrder(orderparams)
            time.sleep(1)  # Delay as seen in original script

            if order_response :
                orderid = order_response
                orderBook = self.api.orderBook()

                order_entries = orderBook.get("data", [])
                order_info = next(
                    (o for o in order_entries if str(o.get("orderid")) == str(orderid)), None
                )

                status = order_info.get("status", "UNKNOWN") if order_info else "NOT_FOUND"
                msg = (
                    f"Order placed: {side.upper()} {qty} {symbol} at "
                    f"{adjusted_price} (Type: {order_type}). Status: {status}"
                )
                logger.info(msg)
                send_telegram_message(msg)
                return {"status": True, "message": msg, "response": order_response, "final_status": status,
                        "norenordno": orderid}
            else:
                emsg = order_response.get('message', 'Unknown API Error')
                logger.error(f"Order failed for {symbol} ({side}). Response: {order_response}")
                send_telegram_message(f"Order failed for {symbol} ({side}): {emsg}")
                return {"status": False, "message": emsg, "response": order_response}

        except Exception as e:
            msg = f"Exception placing order for {symbol}: {e}"
            logger.critical(msg, exc_info=True)
            send_telegram_message(msg)
            return {"status": False, "message": msg}

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
        Places a new BUY order after performing login and pre-order checks.
        Matches ShoonyaAPI's `place_buy_order` signature.
        """
        if not self.is_logged_in or not self.api:
            logger.error("Attempted to place order without logging in.")
            raise NotLoggedInError("Cannot place order: User is not logged in.")

        # --- Pre-buy checks ---
        # if _is_buy_pre_cutoff():
        #     return {"status": False,
        #             "message": f"Buy orders restricted before {MARKET_CUTOFF_TIME.strftime('%H:%M:%S')}"}

        if self._is_existing_order_active(symbol, 'BUY'):
            return {"status": False, "message": f"Active BUY order for {symbol} already exists."}

        # --- Execute Order ---
        return self._execute_order_placement(
            symbol=symbol,
            qty=qty,
            side='BUY',
            order_type=order_type,
            price=price,
            trigger=trigger,
            remarks=remarks
        )

    def trail_and_place_sell_order(
            self,
            symbol: str,
            qty: int,
            order_type: str = 'LIMIT',  # Angel defaults to LIMIT or MARKET
            price: float = 0.0,
            trigger: Optional[float] = None,
            remarks: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Places a SELL order or modifies an existing active SELL order if the price
        is higher (trailing profit logic). Matches ShoonyaAPI's signature.
        """
        if not self.is_logged_in or not self.api:
            logger.error("Attempted to place order without logging in.")
            raise NotLoggedInError("Cannot place order: User is not logged in.")

        # --- Check for existing active sell orders ---
        active_orders = self._get_active_orders(symbol, 'SELL')
        existing_order = active_orders.iloc[0] if not active_orders.empty else None

        details = self._find_symbol_details(symbol)
        if not details:
            return {"status": False, "message": f"Symbol details not found for {symbol}"}

        token, exch, tradingsymbol_full = details

        if existing_order is not None:
            # Angel returns 'price' as str, need to convert
            previous_price = float(existing_order.get('price', 0.0))
            current_price = price

            # --- Modify order if current sell price is higher ---
            if current_price > previous_price:
                orderno = existing_order['uniqueorderid']

                # Angel Broking modify order requires price and trigger to be adjusted/present
                adjusted_price = _calculate_adjusted_price('SELL', order_type, price)

                modify_response = self.api.modifyOrder(
                    ordertype=order_type.upper(),
                    price=adjusted_price,
                    triggerprice=trigger,  # Should be None if not SL order, otherwise a float
                    quantity=qty,
                    orderid=orderno,
                    variety=existing_order.get('variety', 'NORMAL'),
                )

                if modify_response and modify_response.get("message") == 'SUCCESS':
                    msg = f"Sell order modified for {symbol}. New price: {current_price}"
                    logger.info(msg)
                    send_telegram_message(msg)
                    return {"status": True, "message": msg, "response": modify_response, "norenordno": orderno}
                else:
                    emsg = modify_response.get('message', 'Unknown API Error')
                    logger.error(f"Modify order failed for {symbol}. Response: {modify_response}")
                    send_telegram_message(f"Modify sell order failed for {symbol}: {emsg}")
                    return {"status": False, "message": emsg, "response": modify_response}

            else:
                msg = f"Existing sell order price for {symbol} is higher or equal ({previous_price} >= {current_price}). No modification done."
                logger.info(msg)
                return {"status": False, "message": msg, "response": {"stat": "SKIPPED"}}

        # --- Place new sell order if no active order exists ---
        return self._execute_order_placement(
            symbol=symbol,
            qty=qty,
            side='SELL',
            order_type=order_type,
            price=price,
            trigger=trigger,
            remarks=remarks
        )

    def fetch_current_holdings_and_cash(self) -> Tuple[pd.DataFrame, float]:
        """
        Fetch current ETF holdings and total investable cash from Angel API.

        :return: Tuple of (holdings DataFrame, total investable cash)
        """
        if not self.is_logged_in or not self.api:
            logger.error("Attempted to fetch holdings while not logged in.")
            raise NotLoggedInError("Cannot fetch holdings: User is not logged in.")

        # --- Fetch cash and limits ---
        limits_response = self.api.rmsLimit()
        cash = float(limits_response.get('data', {}).get('availablecash', 0.0))
        logger.info(f"Available Cash: {cash}")

        # --- Fetch holdings ---
        holdings_response = self.api.allholding()
        raw_holdings = holdings_response.get('data', {}).get('holdings', [])
        if not raw_holdings:
            logger.info("No holdings found for user.")
            empty_df = pd.DataFrame(
                columns=["etf", "held_qty", "buy_price", "invested", "ltp", "pct_diff"]
            )
            return empty_df, cash

        # --- Prepare holdings data ---
        held_etfs_data = []
        for holding in raw_holdings:
            tsym_full = holding.get('tradingsymbol')
            tsym = tsym_full.replace('-EQ', '')  # Normalize symbol
            qty = int(holding.get('quantity', 0))
            if qty <= 0 or not tsym.endswith('BEES'):  # Assuming only ETF/BEES are relevant
                continue

            buy_price = float(holding.get('averageprice', 0.0))
            ltp = float(holding.get('ltp', 0.0))
            invested_value = qty * buy_price

            pct_diff = round(((ltp - buy_price) / buy_price) * 100, 2) if buy_price != 0 else 0

            held_etfs_data.append({
                "etf": tsym,
                "held_qty": qty,
                "buy_price": buy_price,
                "invested": invested_value,
                "ltp": ltp,
                "pct_diff": pct_diff
            })

        holdings_df = pd.DataFrame(held_etfs_data).drop_duplicates(subset=['etf'])
        logger.info(f"Fetched {len(holdings_df)} ETF holdings. Total Cash Value: {cash}")

        return holdings_df, cash

        # ------------------
        # Helper/Internal Methods (SRP)
        # ------------------

    def _is_existing_order_active(self, symbol: str, side: str) -> bool:
        """
        Internal method to check if an active order for the given symbol and side exists.
        Returns True if an active order exists, False otherwise.
        """
        if not self.is_logged_in or not self.api:
            logger.warning("Attempted _is_existing_order_active check while logged out.")
            return False

        active_orders = self._get_active_orders(symbol, side)

        if not active_orders.empty:
            logger.warning(
                f"Order check blocked: Active order for {symbol} ({side}) already exists. "
                f"ID: {active_orders['uniqueorderid'].iloc[0]}"
            )
            return True

        return False