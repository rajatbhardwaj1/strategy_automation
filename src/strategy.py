import pandas as pd
import logging
from datetime import datetime, time as dt_time
from typing import Dict, List, Tuple

from src.api_helper import ShoonyaApiPy
import src.utils as utils
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
class ETFStrategy:
    """
    Strategy class to automate ETF trading:
      - Fetch holdings & cash
      - Identify buy/sell opportunities
      - Place orders via ShoonyaAPI
    """

    def __init__(self, api: 'ShoonyaAPI', min_cash_reserve: float = 10000):
        self.api = api
        self.min_cash_reserve = min_cash_reserve





