import logging

from src.utils import load_config
from src.telegram_bot import build_application

LOG_FILE = "logs/run_bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=LOG_FILE,
    filemode="a"
)
logger = logging.getLogger(__name__)


def main():
    telegram_cfg = load_config(section="telegram")
    bot_token = telegram_cfg["bot_token"]

    application = build_application(bot_token)

    logger.info("Starting Telegram bot via polling...")
    # run_polling() calls bot.delete_webhook() internally first, so any
    # previously registered webhook is cleared automatically.
    application.run_polling()


if __name__ == "__main__":
    main()
