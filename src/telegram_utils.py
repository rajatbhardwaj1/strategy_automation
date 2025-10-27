from src.utils import load_config
import logging
import telegram
import asyncio

# --- Logging Setup ---
LOG_FILE = "logs/utils.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=LOG_FILE,
    filemode="a"  # Append to the log file instead of overwriting
)

logger = logging.getLogger(__name__)

def send_telegram_message(message: str):
    try:
        config = load_config("telegram")
        bot_token = config.get("bot_token")
        chat_id = config.get("chat_id")

        if not bot_token or not chat_id:
            raise ValueError("Telegram bot_token or chat_id missing in config")

        async def _send():
            bot = telegram.Bot(token=bot_token)
            if len(message) <= 4096:
                await bot.send_message(chat_id=chat_id, text=message)
            else:
                for i in range(0, len(message), 4096):
                    await bot.send_message(chat_id=chat_id, text=message[i:i + 4096])

        asyncio.run(_send())

    except Exception as e:
        # Log and re-raise for integration tests
        logger.error(f"Failed to send Telegram message: {e}", exc_info=True)
