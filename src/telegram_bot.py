import asyncio
import logging
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

import pandas as pd
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from src.utils import (
    fetch_all_etf_data,
    format_etf_list_message,
    format_portfolio_message,
    load_config,
    sort_etf_snapshot,
    sort_holdings,
)
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

# --- Tap-to-sort -----------------------------------------------------------
# Telegram messages are static text, so a column header cannot be clicked.
# Inline keyboard buttons under the message are the equivalent: a tap fires a
# CallbackQuery, the frame is re-sorted and the same message is edited in
# place. Tapping the active column again flips the direction.
#
# (label, sort key, default descending) per view. The default direction is the
# one that answers the obvious question about that column first: biggest
# position, biggest gain - but most-fallen for %diff, matching the ordering
# fetch_all_etf_data already returns.
SORT_KEYS: Dict[str, List[Tuple[str, str, bool]]] = {
    "etf": [
        ("%Diff", "pct_diff", False),
        ("Qty", "held_qty", True),
        ("Bought", "bought", True),
        ("SellAt", "sell_at", True),
    ],
    "pf": [
        ("Gain%", "pct_diff", True),
        ("P&L", "gain", True),
        ("Invested", "invested", True),
        ("ETF", "etf", False),
    ],
}

# The view's initial sort, and the button that starts out marked active.
DEFAULT_SORT = {"etf": ("pct_diff", False), "pf": ("pct_diff", True)}

# Sorting must not re-run the fetch: fetch_all_etf_data is a slow yfinance call
# with per-symbol retries and the holdings fetch is a broker round trip, so a
# tap would take a minute and could return different numbers than the message
# being edited. Cache the frames per rendered message instead, keyed by
# (chat_id, message_id), and keep the cache small - this is a single-user bot,
# so a handful of recent snapshots is all that can be in play.
_SNAPSHOT_CACHE: "OrderedDict[Tuple[int, int], dict]" = OrderedDict()
_SNAPSHOT_CACHE_MAX = 16


def _cache_snapshot(chat_id: int, message_id: int, entry: dict) -> None:
    _SNAPSHOT_CACHE[(chat_id, message_id)] = entry
    _SNAPSHOT_CACHE.move_to_end((chat_id, message_id))
    while len(_SNAPSHOT_CACHE) > _SNAPSHOT_CACHE_MAX:
        _SNAPSHOT_CACHE.popitem(last=False)


def _sort_keyboard(view: str, active_key: str, descending: bool) -> InlineKeyboardMarkup:
    """
    One button per sortable column, two per row so the labels stay readable on
    a phone. The active column carries an arrow showing the current direction;
    its callback_data flips that direction, so tapping it again toggles.
    """
    buttons = []
    for label, key, default_desc in SORT_KEYS[view]:
        if key == active_key:
            text = f"{'▼' if descending else '▲'} {label}"
            next_desc = not descending
        else:
            text = label
            next_desc = default_desc
        # callback_data is capped at 64 bytes; this stays well inside it.
        buttons.append(
            InlineKeyboardButton(text, callback_data=f"srt|{view}|{key}|{int(next_desc)}")
        )
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def _render(entry: dict, key: str, descending: bool) -> str:
    """Re-sort the cached frames and render the message body."""
    if entry["view"] == "etf":
        etf_df = sort_etf_snapshot(
            entry["etf_df"], entry["holdings_df"], key, descending, entry["profit_threshold"]
        )
        return format_etf_list_message(etf_df, entry["holdings_df"], entry["profit_threshold"])
    holdings_df = sort_holdings(entry["holdings_df"], key, descending)
    return format_portfolio_message(holdings_df, entry["profit_threshold"])


async def _send_sortable(update: Update, entry: dict) -> None:
    """
    Send a snapshot with sort buttons attached, and remember the frames behind
    it so a later tap can re-sort without re-fetching.

    A message over the 4096-byte limit has to be split, and buttons can only
    attach to one message - so an oversized table falls back to the plain
    chunked send rather than silently losing rows.
    """
    view = entry["view"]
    key, descending = DEFAULT_SORT[view]
    text = _render(entry, key, descending)

    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        logger.warning(
            f"{view} snapshot is {len(text)} bytes, over the {TELEGRAM_MESSAGE_LIMIT} "
            "limit - sending chunked without sort buttons."
        )
        await _send_chunked(update, text)
        return

    sent = await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=_sort_keyboard(view, key, descending)
    )
    entry["key"], entry["descending"] = key, descending
    _cache_snapshot(sent.chat_id, sent.message_id, entry)


async def on_sort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle a sort button tap. CallbackQueryHandler takes no chat filter, so the
    owner check that gates every command handler is repeated here by hand -
    otherwise a forwarded message's buttons would be a way in.
    """
    query = update.callback_query
    owner_id = int(load_config(section="telegram")["chat_id"])
    if update.effective_chat is None or update.effective_chat.id != owner_id:
        logger.warning(f"Ignoring callback from chat_id={getattr(update.effective_chat, 'id', None)}")
        return

    message = query.message
    entry = _SNAPSHOT_CACHE.get((message.chat_id, message.message_id))
    if entry is None:
        # Restarts clear the cache, and old messages fall out of it.
        await query.answer("This snapshot expired - run the command again.", show_alert=True)
        return

    try:
        _, view, key, desc_flag = query.data.split("|")
        descending = bool(int(desc_flag))
    except (AttributeError, ValueError):
        logger.warning(f"Unparseable callback_data: {query.data!r}")
        await query.answer()
        return

    # Acknowledge first: Telegram spins the button until the query is answered.
    await query.answer()

    text = _render(entry, key, descending)
    try:
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=_sort_keyboard(view, key, descending)
        )
    except BadRequest as e:
        # Re-tapping a single-row or all-equal column can produce byte-identical
        # text, which Telegram rejects. Nothing is wrong, so stay quiet.
        if "not modified" not in str(e).lower():
            raise
        logger.info(f"Sort by {key} left the message unchanged.")
        return

    entry["key"], entry["descending"] = key, descending
    _cache_snapshot(message.chat_id, message.message_id, entry)


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

        await _send_sortable(update, {
            "view": "etf",
            "etf_df": etf_df,
            "holdings_df": holdings_df,
            "profit_threshold": profit_threshold,
        })
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
        await _send_sortable(update, {
            "view": "pf",
            "etf_df": None,
            "holdings_df": holdings_df,
            "profit_threshold": profit_threshold,
        })
    except Exception as e:
        logger.exception(f"Error handling /portfolio: {e}")
        await update.message.reply_text(f"❌ Failed to fetch portfolio: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Strategy bot is live.\n\n"
        "Available commands:\n"
        "/show_etfs - show live ETF snapshot with your position\n"
        "/portfolio - show holdings with sell targets\n\n"
        "Tap the buttons under a snapshot to re-sort it by that column; "
        "tap the same one again to reverse the order."
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
    application.add_handler(CallbackQueryHandler(on_sort, pattern=r"^srt\|"))
    return application
