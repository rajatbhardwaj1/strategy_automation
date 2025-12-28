import logging
import telegram
import asyncio
import yaml
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


def send_telegram_message(message: str):
    # print(f"Sending msg on telegram {message}")
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

if __name__ == '__main__':
    send_telegram_message("testing")
