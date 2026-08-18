import yfinance as yf
import time
import yaml
import pandas as pd
from src.etf import ETFS as etf_symbols
from typing import Optional, List, Dict
import logging
from src.telegram_utils import send_telegram_message
import telegram
import asyncio

BACK_DAYS = 45
MA_PERIOD = 10
ORDER_VALUE = 0
THRESHOLD_LOSS = -4             # THIS VALUE SHOULD ALWAYS BE NEGATIVE.
THRESHOLD_PROFIT = 6.5
MIN_PROFIT = 3                  # PROFIT TO BOOK IF THERE'S NO MORE MONEY TO BUY ETFS
ETF_TO_BUY = 1


# --- Logging Setup ---
LOG_FILE = "logs/utils.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=LOG_FILE,
    filemode="a"  # Append to the log file instead of overwriting
)

logger = logging.getLogger(__name__)



def load_config(section: str, path: str = "src/config.yaml") -> dict:
    """
    Load a specific section from the YAML config file.

    :param section: The section to load ("strategy", "trading", "telegram").
    :param path: Path to the YAML config file.
    :return: Dictionary containing the requested configuration.
    """
    try:
        with open(path, "r") as f:
            cfg = yaml.safe_load(f)

        if section not in cfg:
            logger.warning(f"Section '{section}' not found in config file.")
            return {}

        config_section = cfg[section]
        logger.info(f"Loaded '{section}' configuration: {list(config_section.keys())}")
        return config_section

    except FileNotFoundError:
        logger.error(f"Config file not found at {path}")
        raise
    except yaml.YAMLError as e:
        logger.error(f"Error parsing YAML file: {e}")
        raise


def fetch_etf_data(symbol: str, max_retries: int = 10, delay: int = 5) -> Optional[Dict]:
    """
    Fetch today's LTP and percentage difference for a single ETF, retrying if necessary.

    :param symbol: ETF symbol, e.g., "NIFTYBEES"
    :param max_retries: Number of retry attempts
    :param delay: Delay between retries (in seconds)
    :return: Dictionary with ETF data or None if failed
    """
    for attempt in range(1, max_retries + 1):
        try:
            etf = yf.Ticker(symbol + ".NS")
            hist = etf.history(period=f"{BACK_DAYS}d")

            if len(hist) < MA_PERIOD:
                logger.warning(f"Not enough data for {symbol} (attempt {attempt}/{max_retries})")
                time.sleep(delay)
                continue

            latest_close = hist['Close'].iloc[-1]
            moving_avg = hist['Close'].rolling(window=MA_PERIOD).mean().iloc[-1]
            pct_diff = ((latest_close - moving_avg) / moving_avg * 100) if moving_avg else None

            return {
                "etf": symbol,
                "ltp": round(latest_close, 2),
                "dma": round(moving_avg, 2),
                "pct_diff": round(pct_diff, 2) if pct_diff is not None else None
            }

        except Exception as e:
            logger.error(f"Error fetching data for {symbol} (attempt {attempt}/{max_retries}): {e}")
            time.sleep(delay)

    send_telegram_message(f"Failed to fetch data for {symbol} after {max_retries} attempts.")
    return None


def fetch_all_etf_data(symbols: List[str] = etf_symbols) -> pd.DataFrame:
    """
    Fetch today's data for all ETFs dynamically (without saving to CSV).

    :param symbols: List of ETF symbols to fetch
    :return: DataFrame with ETF data (may be empty if all fail)
    """
    etf_data: List[Dict] = []

    for symbol in symbols:
        data = fetch_etf_data(symbol)
        if data:
            etf_data.append(data)

    if len(etf_data) < len(symbols):
        send_telegram_message("Not all ETF data fetched successfully. Some ETFs may be missing.")

    if etf_data:
        df = pd.DataFrame(etf_data)
        df.sort_values(by="pct_diff", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    logger.warning("No ETF data could be fetched.")
    return pd.DataFrame()  # return empty DataFrame if all fail


def format_buy_signal_message(etfs_to_buy: pd.DataFrame) -> str:
    if etfs_to_buy.empty:
        return "📉 No buy signals generated today."

    lines = ["📊 *Top Buy Signals (3:25 PM)*\n"]
    for idx, row in etfs_to_buy.iterrows():
        lines.append(
            f"• {row['etf']} → {row['pct_diff']:.2f}% below DMA"
        )

    return "\n".join(lines)


def format_etf_list_message(etf_df: pd.DataFrame) -> str:
    """
    Format the full ETF snapshot (LTP, DMA, % diff) as a monospaced table
    for display in a Telegram message. Sorted by pct_diff ascending
    (most fallen first), matching fetch_all_etf_data's ordering.
    """
    if etf_df.empty:
        return "⚠️ No ETF data available right now."

    header = f"{'ETF':<12}{'LTP':>10}{'DMA':>10}{'%Diff':>9}"
    lines = [header, "-" * len(header)]
    for _, row in etf_df.iterrows():
        pct = row["pct_diff"]
        pct_str = f"{pct:.2f}" if pct is not None else "N/A"
        lines.append(f"{row['etf']:<12}{row['ltp']:>10}{row['dma']:>10}{pct_str:>9}")

    table = "\n".join(lines)
    return f"📊 *ETF Snapshot* ({len(etf_df)} funds)\n```\n{table}\n```"


def format_portfolio_message(holdings_df: pd.DataFrame, profit_threshold: float = 6.5) -> str:
    """
    Format current holdings as a monospaced table, including the sell
    target price (buy_price appreciated by profit_threshold%, matching
    the trailing-profit sell trigger used by ETFStrategy).
    """
    if holdings_df.empty:
        return "📭 No holdings found."

    header = f"{'ETF':<12}{'Qty':>6}{'Buy':>9}{'LTP':>9}{'Gain%':>8}{'SellAt':>9}"
    lines = [header, "-" * len(header)]
    for _, row in holdings_df.iterrows():
        sell_target = round(row["buy_price"] * (1 + profit_threshold / 100), 2)
        lines.append(
            f"{row['etf']:<12}{row['held_qty']:>6}{row['buy_price']:>9.2f}"
            f"{row['ltp']:>9.2f}{row['pct_diff']:>8.2f}{sell_target:>9.2f}"
        )
    table = "\n".join(lines)

    total_invested = holdings_df["invested"].sum()
    total_current = (holdings_df["ltp"] * holdings_df["held_qty"]).sum()
    total_pct = round((total_current - total_invested) / total_invested * 100, 2) if total_invested else 0.0
    summary = f"\nInvested: ₹{total_invested:,.0f}  Current: ₹{total_current:,.0f}  Overall: {total_pct:+.2f}%"

    return (
        f"💼 *Portfolio* ({len(holdings_df)} holdings, sell target = buy price +{profit_threshold}%)\n"
        f"```\n{table}\n```{summary}"
    )