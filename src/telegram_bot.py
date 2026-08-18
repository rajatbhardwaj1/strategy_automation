import asyncio
import logging

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from src.utils import fetch_all_etf_data, format_etf_list_message

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
    /show_etfs - fetches live LTP/DMA/%diff for every tracked ETF and
    replies with the snapshot. fetch_all_etf_data() is a slow, blocking,
    network-bound call (yfinance, with retries per symbol), so it's run
    in a worker thread to avoid blocking the bot's event loop.
    """
    chat_id = update.effective_chat.id
    logger.info(f"/show_etfs requested by chat_id={chat_id}")
    await update.message.reply_text("Fetching live ETF data, this can take a minute...")

    try:
        etf_df = await asyncio.to_thread(fetch_all_etf_data)
        message = format_etf_list_message(etf_df)
        await _send_chunked(update, message)
    except Exception as e:
        logger.exception(f"Error handling /show_etfs: {e}")
        await update.message.reply_text(f"❌ Failed to fetch ETF data: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Strategy bot is live.\n\nAvailable commands:\n/show_etfs - show live ETF snapshot"
    )


BOT_COMMANDS = [
    BotCommand("show_etfs", "Show live ETF snapshot (LTP/DMA/%diff)"),
    BotCommand("start", "Show available commands"),
]


async def _post_init(application: Application) -> None:
    # Registers the commands with Telegram so they show up as tappable
    # suggestions in the client's "/" command menu.
    await application.bot.set_my_commands(BOT_COMMANDS)


def build_application(bot_token: str) -> Application:
    application = Application.builder().token(bot_token).post_init(_post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("show_etfs", show_etfs))
    return application
