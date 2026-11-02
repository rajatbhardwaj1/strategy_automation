import asyncio
import logging

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from src.utils import fetch_all_etf_data, format_etf_list_message, format_portfolio_message, load_config
from src.angel_api import AngelAPI

LOG_FILE = "logs/telegram_bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=LOG_FILE,
    filemode="a"
)
logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096


async def _send_chunked(update: Update, text: str, parse_mode: str = "Markdown") -> None:
    for i in range(0, len(text), TELEGRAM_MESSAGE_LIMIT):
        await update.message.reply_text(text[i:i + TELEGRAM_MESSAGE_LIMIT], parse_mode=parse_mode)


async def show_etfs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /show_etfs - fetches live LTP/DMA/%diff for every tracked ETF, overlays
    the position held in each one (qty, average buy price, amount bought for,
    sell target) and replies with the snapshot. fetch_all_etf_data() is a slow,
    blocking, network-bound call (yfinance, with retries per symbol) and the
    holdings fetch logs into the broker, so both run in a worker thread to
    avoid blocking the bot's event loop.
    """
    chat_id = update.effective_chat.id
    logger.info(f"/show_etfs requested by chat_id={chat_id}")
    await update.message.reply_text("Fetching live ETF data, this can take a minute...")

    try:
        etf_df = await asyncio.to_thread(fetch_all_etf_data)

        # Positions are an overlay: if the broker login fails the market
        # snapshot is still worth sending, just with NA in those columns.
        try:
            holdings_df, profit_threshold = await asyncio.to_thread(_fetch_portfolio_snapshot)
        except Exception as e:
            logger.warning(f"Holdings unavailable for /show_etfs: {e}", exc_info=True)
            holdings_df = None
            profit_threshold = load_config(section="strategy").get("profit_threshold", 6.5)

        message = format_etf_list_message(etf_df, holdings_df, profit_threshold)
        await _send_chunked(update, message)
    except Exception as e:
        logger.exception(f"Error handling /show_etfs: {e}")
        await update.message.reply_text(f"❌ Failed to fetch ETF data: {e}")


def _fetch_portfolio_snapshot():
    """
    Blocking: logs into the broker, fetches holdings, logs out. Runs in a
    worker thread via asyncio.to_thread so it doesn't block the bot's
    event loop. Login/logout per call matches the pattern used by
    main.py / runner.py elsewhere in this codebase.
    """
    creds = load_config(section="trading")
    strategy_cfg = load_config(section="strategy")
    profit_threshold = strategy_cfg.get("profit_threshold", 6.5)

    api = AngelAPI(
        api_key=creds["api_key"],
        user_name=creds["uid"],
        password=creds["password"],
        totp_secret=creds["totp_secret"],
    )
    try:
        api.login()
        holdings_df, _cash = api.fetch_current_holdings_and_cash()
    finally:
        try:
            api.logout()
        except Exception:
            logger.warning("Error during logout in portfolio fetch", exc_info=True)

    return holdings_df, profit_threshold


async def show_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /portfolio - shows current holdings with buy price, LTP, current gain%,
    and the sell target price (buy price + profit_threshold%, matching the
    trailing-profit sell trigger in ETFStrategy).
    """
    chat_id = update.effective_chat.id
    logger.info(f"/portfolio requested by chat_id={chat_id}")
    await update.message.reply_text("Fetching your portfolio, one moment...")

    try:
        holdings_df, profit_threshold = await asyncio.to_thread(_fetch_portfolio_snapshot)
        message = format_portfolio_message(holdings_df, profit_threshold)
        await _send_chunked(update, message)
    except Exception as e:
        logger.exception(f"Error handling /portfolio: {e}")
        await update.message.reply_text(f"❌ Failed to fetch portfolio: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Strategy bot is live.\n\n"
        "Available commands:\n"
        "/show_etfs - show live ETF snapshot with your position\n"
        "/portfolio - show holdings with sell targets"
    )


BOT_COMMANDS = [
    BotCommand("show_etfs", "Show live ETF snapshot with your position"),
    BotCommand("portfolio", "Show holdings with sell targets"),
    BotCommand("start", "Show available commands"),
]


async def _post_init(application: Application) -> None:
    """
    Publish the "/" command menu to the owner's chat only.

    set_my_commands() with no scope registers globally, and Telegram stores
    that list server-side - so anyone who opens the bot sees every command
    listed. The delete clears any previously-registered global list; without
    it the old global registration survives on Telegram's side and keeps
    advertising the commands to everyone regardless of what we set here.
    """
    owner_id = load_config(section="telegram")["chat_id"]
    await application.bot.delete_my_commands(scope=BotCommandScopeDefault())
    await application.bot.set_my_commands(
        BOT_COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id)
    )


def build_application(bot_token: str) -> Application:
    """
    Every handler is gated on an owner-only chat filter. A bot's username is
    discoverable, and these commands expose live holdings, so updates from any
    other chat must not reach a handler at all. Unauthorized chats get silence
    rather than a rejection notice - a reply would confirm the bot is live.
    """
    owner_only = filters.Chat(chat_id=load_config(section="telegram")["chat_id"])

    application = Application.builder().token(bot_token).post_init(_post_init).build()
    application.add_handler(CommandHandler("start", start, filters=owner_only))
    application.add_handler(CommandHandler("show_etfs", show_etfs, filters=owner_only))
    application.add_handler(CommandHandler("portfolio", show_portfolio, filters=owner_only))
    return application
