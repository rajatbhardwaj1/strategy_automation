import pandas as pd
import logging
from datetime import  time as dt_time
from typing import Dict, List, Tuple, Optional, Any, Union
from src.utils import fetch_all_etf_data, format_buy_signal_message
import src.utils as utils
from  src.shoonya_api import ShoonyaAPI
from src.angel_api import AngelAPI
from src.telegram_utils import send_telegram_message

# ----------------------------
# Logging
# ----------------------------
LOGGING_FILE = "logs/strategy.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename=LOGGING_FILE,
    filemode='a'
)
logger = logging.getLogger(__name__)


#-------
# Constants
# ----------------------------
MARKET_CUTOFF_TIME = dt_time(14, 0, 0)
PRICE_ADJUSTMENT_DELTA = 0.01
ACTIVE_STATUSES: List[str] = ['OPEN', 'PENDING', 'TRIGGER_PENDING', 'NEW']



# ----------------------------
# Exceptions
# ----------------------------
class NotLoggedInError(Exception):
    pass

class InsufficientCashError(Exception):
    pass


# ----------------------------
# Strategy Class
# ----------------------------
def identify_buy_signals(etf_df: pd.DataFrame, top_n: int = 3) -> pd.DataFrame:
    """
    Identify top ETFs to buy based on % deviation from DMA (falling the most).
    """
    if etf_df.empty:
        logger.warning("No ETF data available to generate buy signals.")
        return pd.DataFrame()

    etf_sorted = etf_df.sort_values("pct_diff").head(top_n)
    logger.info(f"Top {top_n} ETFs to consider for buying:\n{etf_sorted[['etf', 'pct_diff']]}")
    return etf_sorted


class ETFStrategy:
    """
    Strategy class to automate ETF trading:
      - Fetch holdings & cash
      - Identify buy/sell opportunities
      - Place orders via ShoonyaAPI
    """

    def __init__(self, api:  AngelAPI , config :Dict):
        self.api = api
        self.dma_window = config.get("dma_window", 10)
        self.profit_threshold = config.get("profit_threshold", 6.5)
        self.trailing_stop = config.get("trailing_stop", 0.5)
        self.max_daily_buys = config.get("max_daily_buys", 1)
        self.avg_down_threshold = config.get("avg_down_threshold", 4.0)
        self.hydration = config.get("hydration" , 0)
        self.hydration_increment = config.get("hydration_increment" , 0)
        self.buy_divisor = config.get("buy_divisor" , 40)

        # ------------------------
        # Core Methods
        # ------------------------
    def fetch_holdings_and_cash(self) -> Tuple[pd.DataFrame, float  ]:
        """
        Fetch current ETF holdings and investable cash.
        """
        holdings_df, total_cash = self.api.fetch_current_holdings_and_cash()
        logger.info(f"Holdings fetched: {len(holdings_df)} ETFs. Total cash: {total_cash}")
        return holdings_df, total_cash


    def _place_buy_order(self, symbol: str, qty: int, price: float = 0.0, order_type ='LIMIT',  trigger: Optional[float] = None, remarks: Optional[str] = None) -> Dict[str, Any]:
        """
        Place a buy order if sufficient cash is available.
        """
        holdings_df, cash = self.fetch_holdings_and_cash()
        if price <= 0:
            raise ValueError(f"Invalid buy price: {price}")
        total_cost = qty * price

        # if cash < total_cost:
        #     msg = f"Insufficient cash to buy {qty} of {symbol}. Available: {cash}, Required: {total_cost}"
        #     logger.warning(msg)
        #     send_telegram_message(msg)
            # raise InsufficientCashError(msg)

        order_response = self.api.place_buy_order(
            symbol=symbol,
            qty=qty,
            order_type=order_type,
            price=price,
            trigger=trigger,
            remarks=remarks
        )

        logger.info(f"Buy order response for {symbol}: {order_response}")
        return order_response


    def _place_sell_order(self, symbol: str, qty: int, price: float = 0.0,order_type = 'LIMIT', trigger: Optional[float] = None, remarks: Optional[str] = None) -> Dict[str, Any]:
        """
        Place a sell order for the given symbol and quantity.
        """
        order_response = self.api.trail_and_place_sell_order(
            symbol=symbol,
            qty=qty,
            order_type= order_type,
            price=price,
            trigger=trigger,
            remarks=remarks
        )

        logger.info(f"Sell order response for {symbol}: {order_response}")
        return order_response


    # -----------------------------------
    # Trailing Profit Logic
    # -----------------------------------
    def process_trailing_profit(self, book_today: pd.DataFrame):
        """
        Place trailing profit sell orders for profitable ETFs.
        """
        if book_today.empty:
            logger.info("No ETFs found for trailing profit.")
        else:
            logger.info("Processing trailing profit orders...")
            for _, row in book_today.iterrows():
                price = round(row["ltp"] - (row["buy_price"] * 0.005), 2)
                trigger = round(price + 0.01, 2)
                logger.debug(f"Placing STOPLOSS_LIMIT for {row['etf']} at price {price}, trigger {trigger}")

                self._place_sell_order(row["etf"], row["held_qty"], price,"STOPLOSS_LIMIT",  trigger, "Trailing profit")





    # -----------------------------------
    # Buy Logic
    # -----------------------------------
    def buy_etfs(self, etfs_to_buy: pd.DataFrame, held_etfs: pd.DataFrame, cash: float) :
        """
        Buy selected ETFs, averaging down existing positions where applicable.
        """
        total_value = cash + (held_etfs["ltp"] * held_etfs["held_qty"]).sum()
        buy_count = 0

        for _, row in etfs_to_buy.iterrows():
            if buy_count >= self.max_daily_buys:
                logger.info("Max daily buy limit reached.")
                break

            symbol = row["etf"]
            pct_diff = row["pct_diff"]
            current_price = row["ltp"]

            # Skip ETFs not in downtrend
            if pct_diff > 0:
                continue

            buy_amount = min(total_value / self.buy_divisor, cash + self.hydration)
            shares = int(buy_amount // current_price)
            if shares <= 0:
                msg = (
                    f"⚠️ **Insufficient Funds**\n"
                    f"Symbol: {symbol}\n"
                    f"Req: ₹{current_price}\n"
                    f"Cash: ₹{cash:.2f}"
                )
                send_telegram_message(msg)
                continue  # Uncommented this so the loop skips to the next item

            if symbol in held_etfs["etf"].values:
                held_row = held_etfs.loc[held_etfs["etf"] == symbol].iloc[0]
                buy_price = held_row["buy_price"]
                loss_pct = (buy_price - current_price) / buy_price * 100

                if loss_pct > self.avg_down_threshold:
                    logger.info(f"Averaging down {symbol}: {loss_pct:.2f}% loss.")

                    response = self._place_buy_order(symbol, shares, current_price, "LIMIT", None, "BUY_AVERAGE_DOWN")

                    if response.get("status") and response.get("final_status") != 'rejected':
                        cash -= shares * current_price
                        buy_count += 1

                        # Success Message
                        send_telegram_message(
                            f"✅ **AVERAGE DOWN Executed**\n"
                            f"Symbol: {symbol}\n"
                            f"Qty: {shares} @ ₹{current_price}\n"
                            f"Current Loss: {loss_pct:.2f}%"
                        )
                    else:
                        # Failure Message
                        error_reason = response.get('message', 'Unknown Error')
                        send_telegram_message(
                            f"❌ **Avg Down Failed**\n"
                            f"Symbol: {symbol}\n"
                            f"Reason: {error_reason}"
                        )

                # Logic for New Buy (Not currently held)
            else:
                logger.info(f"New buy for {symbol} at {current_price}")

                response = self._place_buy_order(symbol, shares, current_price, "LIMIT", None, "NEW_BUY")

                if response.get("status") and response.get("final_status") != 'rejected':
                    cash -= shares * current_price
                    buy_count += 1

                    # Success Message
                    send_telegram_message(
                        f"✅ **NEW BUY Executed**\n"
                        f"Symbol: {symbol}\n"
                        f"Qty: {shares} @ ₹{current_price}\n"
                        f"Allocated: ₹{shares * current_price:.2f}"
                    )
                else:
                    # Failure Message
                    error_reason = response.get('message', 'Unknown Error')
                    send_telegram_message(
                        f"❌ **New Buy Failed**\n"
                        f"Symbol: {symbol}\n"
                        f"Reason: {error_reason}"
                    )



    # -----------------------------------
    # Main Orchestrator
    # -----------------------------------

    # Call every 10 mins
    def sell_order_processing(self) -> None:
        held_etfs, cash  = self.fetch_holdings_and_cash()
        to_be_trailed = held_etfs[held_etfs["pct_diff"] > self.profit_threshold]
        if not to_be_trailed.empty:
            self.process_trailing_profit(to_be_trailed)


    # Call only once at 3.25 PM
    def buy_order_processing(self):
        held_etfs, cash  = self.fetch_holdings_and_cash()
        etf_data = fetch_all_etf_data()
        etfs_to_buy = identify_buy_signals(etf_data, top_n=3)
        try:
            message = format_buy_signal_message(etfs_to_buy)
            send_telegram_message(message)
        except Exception as e:
            send_telegram_message(f"Exception in sending buy signals : {e}")
        self.buy_etfs(etfs_to_buy, held_etfs, cash)

