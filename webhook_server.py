import logging

from src.utils import load_config
from src.telegram_bot import build_application

LOG_FILE = "logs/webhook_server.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=LOG_FILE,
    filemode="a"
)
logger = logging.getLogger(__name__)


def main():
    telegram_cfg = load_config(section="telegram")
    webhook_cfg = load_config(section="telegram_webhook")

    bot_token = telegram_cfg["bot_token"]
    application = build_application(bot_token)

    logger.info("Starting Telegram webhook server...")
    application.run_webhook(
        listen=webhook_cfg.get("listen_host", "127.0.0.1"),
        port=int(webhook_cfg.get("port", 8443)),
        url_path=webhook_cfg["url_path"],
        webhook_url=webhook_cfg["webhook_url"],
        secret_token=webhook_cfg["secret_token"],
    )


if __name__ == "__main__":
    main()
