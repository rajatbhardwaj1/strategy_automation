import pandas as pd
import logging
from datetime import datetime, time as dt_time
from typing import Dict, List, Tuple, Optional, Any

import src.utils as utils
from  src.shoonya_api import ShoonyaAPI
from src.utils import fetch_all_etf_data
from src.etf import ETFS as etf_symbols


# ----------------------------
# Logging
# ----------------------------
LOGGING_FILE = "strategy.log"
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

    def __init__(self, api: ShoonyaAPI , config :Dict):
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
    def fetch_holdings_and_cash(self) -> Tuple[pd.DataFrame, float ,pd.DataFrame ]:
        """
        Fetch current ETF holdings and investable cash.
        """
        holdings_df, total_cash, etf_data = self.api.fetch_current_holdings_and_cash()
        logger.info(f"Holdings fetched: {len(holdings_df)} ETFs. Total cash: {total_cash}")
        return holdings_df, total_cash, etf_data


    def _place_buy_order(self, symbol: str, qty: int, price: float = 0.0, order_type ='LMT',  trigger: Optional[float] = None, remarks: Optional[str] = None) -> Dict[str, Any]:
        """
        Place a buy order if sufficient cash is available.
        """
        holdings_df, cash, etf_data = self.fetch_holdings_and_cash()
        if price <= 0:
            raise ValueError(f"Invalid buy price: {price}")
        total_cost = qty * price

        if cash < total_cost:
            msg = f"Insufficient cash to buy {qty} of {symbol}. Available: {cash}, Required: {total_cost}"
            logger.warning(msg)
            utils.send_telegram_message(msg)
            raise InsufficientCashError(msg)

        order_response = self.api.place_order(
            symbol=symbol,
            qty=qty,
            side='B',
            order_type=order_type,
            price=price,
            trigger=trigger,
            remarks=remarks
        )

        logger.info(f"Buy order response for {symbol}: {order_response}")
        return order_response


    def _place_sell_order(self, symbol: str, qty: int, price: float = 0.0,order_type = 'LMT', trigger: Optional[float] = None, remarks: Optional[str] = None) -> Dict[str, Any]:
        """
        Place a sell order for the given symbol and quantity.
        """
        order_response = self.api.place_order(
            symbol=symbol,
            qty=qty,
            side='S',
            order_type= order_type,
            price=price,
            trigger=trigger,
            remarks=remarks
        )

        logger.info(f"Sell order response for {symbol}: {order_response}")
        return order_response


    def place_order(self, symbol: str, qty: int, side: str, order_type: str = 'LMT', price: float = 0.0, trigger: Optional[float] = None, remarks: Optional[str] = None) -> Dict[str, Any]:
        """
        Wrapper to place a buy or sell order.
        """
        if side == 'B':
            return self._place_buy_order(symbol, qty, price,order_type, trigger, remarks)
        elif side == 'S':
            return self._place_sell_order(symbol, qty, price,order_type,  trigger, remarks)
        else:
            raise ValueError(f"Invalid side '{side}'. Must be 'B' or 'S'.")


    # -----------------------------------
    # Trailing Profit Logic
    # -----------------------------------
    def process_trailing_profit(self, book_today: pd.DataFrame) -> int:
        """
        Place trailing profit sell orders for profitable ETFs.
        """
        if book_today.empty:
            logger.info("No ETFs found for trailing profit.")
            return 0

        logger.info("Processing trailing profit orders...")
        for _, row in book_today.iterrows():
            price = round(row["ltp"] - (row["buy_price"] * 0.005), 2)
            trigger = round(price + 0.01, 2)
            logger.debug(f"Placing SL-LMT for {row['etf']} at price {price}, trigger {trigger}")

            self.place_order(
                symbol=row["etf"],
                qty=int(row["held_qty"]),
                side="S",
                order_type="SL-LMT",
                price=price,
                trigger=trigger,
                remarks="Trailing profit",
            )

        return len(book_today)


    # -----------------------------------
    # Buy Logic
    # -----------------------------------
    def buy_etfs(self, etfs_to_buy: pd.DataFrame, held_etfs: pd.DataFrame, cash: float) -> None:
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
                continue

            if symbol in held_etfs["etf"].values:
                held_row = held_etfs.loc[held_etfs["etf"] == symbol].iloc[0]
                buy_price = held_row["buy_price"]
                loss_pct = (buy_price - current_price) / buy_price * 100

                if loss_pct > self.avg_down_threshold:
                    logger.info(f"Averaging down {symbol}: {loss_pct:.2f}% loss.")
                    self.place_order(symbol, shares, "B", "LMT", current_price, None, "BUY_AVERAGE_DOWN")
                    cash -= shares * current_price
                    buy_count += 1
            else:
                logger.info(f"New buy for {symbol} at {current_price}")
                self.place_order(symbol, shares, "B", "LMT", current_price, None, "NEW_BUY")
                cash -= shares * current_price
                buy_count += 1


    # -----------------------------------
    # Main Orchestrator
    # -----------------------------------

    # Call every 10 mins
    def sell_order_processing(self) -> None:
        """
        Main strategy execution function: trailing sell + fresh buys.
        """
        held_etfs, cash , etf_data = self.fetch_holdings_and_cash()

        to_be_trailed = held_etfs[held_etfs["pct_diff"] > self.profit_threshold]
        if not to_be_trailed.empty:
            self.process_trailing_profit(to_be_trailed)


    # Call only once at 3.25 PM
    def buy_order_processing(self):

        held_etfs, cash , etf_data = self.fetch_holdings_and_cash()
        # etf_data = fetch_all_etf_data()
        etfs_to_buy = identify_buy_signals(etf_data, top_n=3)
        self.buy_etfs(etfs_to_buy, held_etfs, cash)

