import asyncio
import html
import logging
import os
import re
import random
import zipfile
import aiosqlite
from contextvars import ContextVar
from cryptography.fernet import Fernet, InvalidToken
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, StateFilter, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton as TelegramInlineKeyboardButton, InlineKeyboardMarkup,
    Message, CallbackQuery, ReplyKeyboardRemove,
    ReplyKeyboardMarkup, KeyboardButton as TelegramKeyboardButton, LabeledPrice,
    MessageEntity
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError, UserPrivacyRestrictedError, PeerFloodError,
    PremiumAccountRequiredError, EntityBoundsInvalidError,
    SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError,
    PasswordHashInvalidError,
)
from telethon.tl.types import (
    UserStatusOnline, Chat, DocumentAttributeCustomEmoji, InputStickerSetShortName,
    MessageEntityCustomEmoji, MessageEntityMentionName, ChannelParticipantsAdmins,
)
from telethon.utils import get_display_name, get_peer_id
from telethon.tl.functions.messages import GetStickerSetRequest
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.users import GetFullUserRequest
from urllib.parse import urlparse
from telethon.utils import get_display_name, get_peer_id

load_dotenv()

logging.basicConfig(level=logging.ERROR)
log = logging.getLogger("@pro_utaggerbot")

def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} environment variable is required")
    return value


def required_int_env(name: str, hint: str) -> int:
    value = required_env(name).strip()
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer. {hint}") from exc


API_ID    = required_int_env(
    "API_ID", "Set the numeric app ID from my.telegram.org in Render, not placeholder text."
)
if not 1 <= API_ID <= 2_147_483_647:
    raise RuntimeError("API_ID must be the valid app ID from my.telegram.org, not a Telegram user ID")
API_HASH  = required_env("API_HASH")
BOT_TOKEN = required_env("BOT_TOKEN")
ADMIN_ID  = required_int_env("ADMIN_ID", "Set the numeric Telegram user ID.")
if ADMIN_ID <= 0:
    raise RuntimeError("ADMIN_ID must be a positive Telegram user ID")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@owapro")
IS_RENDER_SERVICE = bool(os.getenv("RENDER_SERVICE_ID"))
DB_FILE = (
    os.path.join("/tmp", "database22.db")
    if IS_RENDER_SERVICE
    else os.getenv("DB_FILE", "database22.db")
)
CLONE_TOKEN_ENCRYPTION_KEY = os.getenv("CLONE_TOKEN_ENCRYPTION_KEY", "").strip()
CUSTOM_EMOJI_PACK = os.getenv("CUSTOM_EMOJI_PACK", "").strip()

AD_TEXT = "🤖 Powered by @Prime_utaggerbot 🚀"
BIO_AD_TEXT = "🤖 Powered by @Prime_utaggerbot 🚀"
AUTO_REPLY_AD = f"{AD_TEXT}\n🤖 Avto javob qilindi."
SOURCE_FILE = os.getenv("SOURCE_FILE", __file__)


def get_utag_command_help() -> str:
    return (
        "📌 <b>uTag buyruqlari</b> (guruhda yozing):\n"
        "• <code>.s</code> — uTag boshlash\n"
        "• <code>.r</code> yoki <code>.u</code> — random uTag\n"
        "• <code>.f</code> — uTag to'xtatish"
    )

storage = MemoryStorage()
primary_bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
_active_bot: ContextVar[Bot] = ContextVar("active_bot", default=primary_bot)
_active_clone_owner: ContextVar[int | None] = ContextVar("active_clone_owner", default=None)
_active_bot_username: ContextVar[str] = ContextVar("active_bot_username", default="")


class ActiveBotProxy:
    def __getattr__(self, name):
        return getattr(_active_bot.get(), name)


bot = ActiveBotProxy()
dp  = Dispatcher(storage=storage)

userbot_clients: dict[str, TelegramClient] = {}
_utag_tasks: dict[str, asyncio.Task] = {}
_utag_suppress_ad_tasks: set[asyncio.Task] = set()
_raid_locks: dict[str, asyncio.Lock] = {}
_raid_mass_tasks: dict[tuple[str, str], asyncio.Task] = {}
_raid_profile_selection_sessions: dict[str, list[str]] = {}
_bot_utag_tasks: dict[int, asyncio.Task] = {}
_bot_group_word_counts: dict[int, dict[str, int]] = {}
_auto_reply_cooldowns: dict[tuple[str, int], datetime] = {}
_scrape_group_choices: dict[str, dict[str, object]] = {}
_custom_emoji_pack_cache: list[tuple[int, str]] = []
_custom_emoji_pack_lock = asyncio.Lock()
_group_random_words_cache: dict[int, tuple[datetime, list[str]]] = {}
_button_custom_emoji_ids: dict[str, str] = {}
_button_catalog: dict[str, str] = {}
_button_emoji_target_keys: list[str] = []
_button_styles = ("success", "primary", "danger")
_button_style_index = 0
bot_username = ""
clone_bots: dict[int, Bot] = {}
clone_polling_tasks: dict[int, asyncio.Task] = {}


def is_clone_bot() -> bool:
    return _active_clone_owner.get() is not None


def is_panel_owner(user_id: int) -> bool:
    owner_id = _active_clone_owner.get()
    return user_id == (owner_id if owner_id is not None else ADMIN_ID)


def current_panel_owner_id() -> int:
    return _active_clone_owner.get() or ADMIN_ID


def clone_token_cipher() -> Fernet:
    if not CLONE_TOKEN_ENCRYPTION_KEY:
        raise RuntimeError("CLONE_TOKEN_ENCRYPTION_KEY is not configured")
    return Fernet(CLONE_TOKEN_ENCRYPTION_KEY.encode())


def _build_emoji_button(button_type, text: str, kwargs: dict):
    global _button_style_index
    if "style" not in kwargs:
        kwargs["style"] = _button_styles[_button_style_index % len(_button_styles)]
        _button_style_index += 1
    key = str(kwargs.get("callback_data") or kwargs.get("url") or text)
    if key and not key.startswith(("button_emoji_", "admin_custom_emoji", "admin_button_emoji")):
        _button_catalog[key] = text
        icon_id = _button_custom_emoji_ids.get(key)
        if icon_id and not kwargs.get("icon_custom_emoji_id"):
            kwargs["icon_custom_emoji_id"] = icon_id
    return button_type(text=text, **kwargs)


def InlineKeyboardButton(*, text: str, **kwargs):
    return _build_emoji_button(TelegramInlineKeyboardButton, text, kwargs)


def KeyboardButton(*, text: str, **kwargs):
    return _build_emoji_button(TelegramKeyboardButton, text, kwargs)


# ─────────────────────────────────────────────
# STATES
# ─────────────────────────────────────────────
class UserStatesGroup(StatesGroup):
    login_phone        = State()
    login_code         = State()
    login_2fa          = State()
    utag_group_link    = State()
    utag_custom_suffix = State()
    utag_speed         = State()
    auto_msg_text      = State()
    auto_msg_usernames = State()
    auto_reply_text    = State()
    scrape_group_select = State()
    scrape_mode        = State()
    scrape_count       = State()
    admin_broadcast        = State()
    admin_add_channel      = State()
    admin_raid_add_group   = State()
    admin_raid_user        = State()
    admin_raid_profile     = State()
    admin_custom_emoji_pack = State()
    admin_give_pro         = State()
    admin_clone_price      = State()
    clone_token_input      = State()
    ban_target_input       = State()
    contest_channel        = State()
    contest_max_users      = State()
    contest_req_channels   = State()
    contest_prize_text     = State()

# ─────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────
async def init_db():
    global CUSTOM_EMOJI_PACK
    os.makedirs(os.path.dirname(os.path.abspath(DB_FILE)), exist_ok=True)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            fullname TEXT,
            username TEXT,
            referrer_id TEXT,
            pro_until TEXT,
            raid_until TEXT,
            notified_10m INTEGER DEFAULT 0,
            first_seen TEXT
        )""")
        try:
            await db.execute("ALTER TABLE users ADD COLUMN raid_until TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN raid_enabled INTEGER DEFAULT 0")
        except Exception:
            pass
        await db.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            user_id    TEXT,
            account_id TEXT PRIMARY KEY,
            name       TEXT,
            phone      TEXT,
            session    TEXT
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS scraped_users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id   TEXT,
            group_link TEXT,
            telegram_user_id TEXT,
            username   TEXT,
            fullname   TEXT,
            scraped_at TEXT
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS auto_reply_settings (
            owner_id      TEXT PRIMARY KEY,
            enabled       INTEGER DEFAULT 0,
            response_text TEXT NOT NULL,
            updated_at    TEXT
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS utag_settings (
            owner_id TEXT PRIMARY KEY,
            delay    REAL NOT NULL DEFAULT 0.5,
            updated_at TEXT
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS clone_orders (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            payload TEXT NOT NULL UNIQUE,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            telegram_charge_id TEXT UNIQUE,
            created_at TEXT NOT NULL
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS cloned_bots (
            bot_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            username TEXT NOT NULL,
            token_encrypted TEXT NOT NULL,
            order_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS raid_groups (
            chat_id   TEXT PRIMARY KEY,
            title     TEXT NOT NULL,
            added_at  TEXT NOT NULL
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS raid_usage (
            user_id   TEXT PRIMARY KEY,
            used_at   TEXT NOT NULL
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS raid_settings (
            user_id TEXT PRIMARY KEY,
            delay REAL NOT NULL DEFAULT 1.0,
            batch_size INTEGER NOT NULL DEFAULT 10
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS raid_profile_groups (
            profile_user_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            title TEXT NOT NULL,
            has_ban_rights INTEGER NOT NULL DEFAULT 0,
            selected INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (profile_user_id, chat_id)
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_group_members (
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            username TEXT,
            fullname TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )""")
        try:
            await db.execute(
                "ALTER TABLE scraped_users ADD COLUMN telegram_user_id TEXT"
            )
        except Exception:
            pass
        await db.commit()
        async with db.execute(
            "SELECT id, pro_until FROM users WHERE raid_until IS NULL AND pro_until IS NOT NULL"
        ) as cur:
            existing_pro_users = await cur.fetchall()
        migration_now = datetime.now(timezone.utc)
        for user_id, pro_until in existing_pro_users:
            if not is_pro_user(pro_until):
                continue
            pro_end = datetime.fromisoformat(pro_until)
            if pro_end.tzinfo is None:
                pro_end = pro_end.replace(tzinfo=timezone.utc)
            raid_end = min(pro_end, migration_now + timedelta(days=7))
            await db.execute(
                "UPDATE users SET raid_until = ? WHERE id = ?",
                (raid_end.isoformat(), user_id)
            )
        await db.commit()
        # Kanallar jadval (admin boshqaradi)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS sub_channels (
            channel_username TEXT PRIMARY KEY,
            channel_url      TEXT,
            added_at         TEXT
        )""")
        # Konkurslar
        await db.execute("""
        CREATE TABLE IF NOT EXISTS contests (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id       TEXT,
            message_id    TEXT,
            prize_text    TEXT,
            max_users     INTEGER,
            req_channels  TEXT,
            status        TEXT DEFAULT 'active',
            winner_id     TEXT,
            created_at    TEXT
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS contest_participants (
            contest_id INTEGER,
            user_id    TEXT,
            username   TEXT,
            fullname   TEXT,
            joined_at  TEXT,
            PRIMARY KEY (contest_id, user_id)
        )""")
        await db.commit()
        rows = await db.execute_fetchall(
            "SELECT value FROM bot_settings WHERE key = 'custom_emoji_pack'"
        )
        if rows:
            CUSTOM_EMOJI_PACK = rows[0][0]
        rows = await db.execute_fetchall(
            "SELECT key, value FROM bot_settings WHERE key LIKE 'button_emoji:%'"
        )
        _button_custom_emoji_ids.clear()
        _button_custom_emoji_ids.update({
            key.removeprefix("button_emoji:"): value for key, value in rows
        })

# ─────────────────────────────────────────────
# CHANNEL HELPERS
# ─────────────────────────────────────────────
async def get_channels() -> list[dict]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT channel_username, channel_url FROM sub_channels") as cur:
            rows = await cur.fetchall()
    return [{"username": r[0], "url": r[1]} for r in rows]


async def get_raid_groups() -> list[dict]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT chat_id, title FROM raid_groups ORDER BY title COLLATE NOCASE"
        ) as cur:
            rows = await cur.fetchall()
    return [{"chat_id": row[0], "title": row[1]} for row in rows]


async def get_raid_groups_for_user(user_id: int | str, selected_only: bool = True) -> list[dict]:
    where = "AND selected = 1" if selected_only else ""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT chat_id, title FROM raid_profile_groups "
            f"WHERE profile_user_id = ? AND has_ban_rights = 1 {where} "
            "ORDER BY title COLLATE NOCASE",
            (str(user_id),)
        ) as cur:
            rows = await cur.fetchall()
    if rows:
        return [{"chat_id": row[0], "title": row[1]} for row in rows]
    # Eski sozlamalar admin profiliga tegishli bo'lsa, moslikni saqlaymiz.
    if str(user_id) == str(ADMIN_ID):
        return await get_raid_groups()
    return []


async def get_owner_raid_groups(profile_user_id: int | str = ADMIN_ID) -> list[dict]:
    """Tanlangan profil guruhlarini oladi; legacy ro'yxat faqat admin uchun."""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT chat_id, title FROM raid_profile_groups "
            "WHERE profile_user_id = ? AND selected = 1 AND has_ban_rights = 1 "
            "ORDER BY title COLLATE NOCASE",
            (str(profile_user_id),)
        ) as cur:
            rows = await cur.fetchall()
    if rows:
        return [{"chat_id": row[0], "title": row[1]} for row in rows]
    if str(profile_user_id) == str(ADMIN_ID):
        return await get_raid_groups()
    return []


async def run_owner_command_raid(requester_id: int, target_username: str, report_chat_id: int):
    if not await user_has_raid_access(requester_id):
        await bot.send_message(report_chat_id, "Raid owner tomonidan berilmagan.")
        return
    profile_user_id = current_panel_owner_id() if is_clone_bot() else ADMIN_ID
    raid_client = userbot_clients.get(str(profile_user_id))
    if not raid_client:
        await bot.send_message(report_chat_id, "Raid profil userboti ulanmagan.")
        return
    groups = await get_owner_raid_groups(profile_user_id)
    if not groups:
        await bot.send_message(report_chat_id, "Raid profili hali guruh tanlamagan.")
        return
    try:
        target = await raid_client.get_entity(target_username)
        me = await raid_client.get_me()
        if getattr(target, "id", None) == me.id:
            await bot.send_message(report_chat_id, "Owner akkauntini ban qilib bo'lmaydi.")
            return
    except Exception:
        await bot.send_message(report_chat_id, f"{target_username} topilmadi.")
        return
    banned_groups = []
    for group in groups:
        try:
            entity = await raid_client.get_entity(int(group["chat_id"]))
            permissions = await raid_client.get_permissions(entity, me)
            if not has_ban_rights(permissions):
                continue
            await raid_client.edit_permissions(entity, target, view_messages=False)
            banned_groups.append(group["title"])
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 1)
        except Exception as exc:
            log.warning("Owner command raid failed: %s", exc)
    if banned_groups:
        await bot.send_message(
            report_chat_id,
            f"✅ {html.escape(target_username)} barcha tanlangan guruhlarda ban qilindi: "
            f"{len(banned_groups)} ta."
        )
    else:
        await bot.send_message(report_chat_id, "Hech bir tanlangan guruhda ban bajarilmadi.")


def has_ban_rights(permissions) -> bool:
    admin_rights = getattr(permissions, "admin_rights", None)
    return bool(
        getattr(permissions, "is_creator", False)
        or getattr(admin_rights, "ban_users", False)
    )


def has_add_members_rights(permissions) -> bool:
    admin_rights = getattr(permissions, "admin_rights", None)
    return bool(
        getattr(permissions, "is_creator", False)
        or getattr(admin_rights, "invite_users", False)
    )


def has_raid_group_rights(permissions) -> bool:
    return has_ban_rights(permissions)

async def check_subscriptions(user_id: int) -> tuple[bool | None, str | None]:
    channels = await get_channels()
    for ch in channels:
        try:
            # Majburiy kanallar asosiy bot sozlamasidan umumiy ishlaydi.
            # Clone botlar kanalga alohida admin qilinishi shart emas.
            member = await primary_bot.get_chat_member(
                chat_id=ch["username"], user_id=user_id
            )
            status = getattr(member.status, "value", member.status)
            subscribed = status in {"creator", "administrator", "member"}
            subscribed = subscribed or (
                status == "restricted" and bool(getattr(member, "is_member", False))
            )
            if not subscribed:
                return False, ch["username"]
        except Exception as exc:
            log.warning(
                "Obuna tekshirilmadi (%s, user %s): %s",
                ch["username"], user_id, exc
            )
            return None, ch["username"]
    return True, None


def subscription_error_message(channel: str | None, check_failed: bool) -> str:
    channel_name = html.escape(channel or "kanal")
    if check_failed:
        return (
            f"⚠️ <b>{channel_name}</b> kanalini tekshirib bo'lmadi. "
            "Botni shu kanalga admin qilib qo'shing va kanal username'i/ID'sini tekshiring."
        )
    return f"⚠️ Botdan foydalanish uchun <b>{channel_name}</b> kanaliga a'zo bo'ling:"

# ─────────────────────────────────────────────
# PRO HELPERS
# ─────────────────────────────────────────────
def is_pro_user(pro_until_str: str | None) -> bool:
    if not pro_until_str:
        return False
    try:
        until_dt = datetime.fromisoformat(pro_until_str)
        return datetime.now(timezone.utc) < until_dt
    except Exception:
        return False

def make_text_unique(text: str) -> str:
    if not text:
        return ""
    invisible = ["\u200c", "\u200d", "\u200b"]
    return f"{text}{''.join(random.choices(invisible, k=3))}"


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


# Random uTag uchun 50 tadan ko'p kulgili so'zlar.
# Hashtag ataylab ishlatilmaydi: Telegram xabarida "#" chiqmaydi.
RANDOM_UTAG_WORDS = [
    "shirincha", "qahramon", "kapalak", "quvnoq", "chaqmoq", "ziravor",
    "pahlavon", "sehrgar", "yulduzcha", "qiziqvoy", "raketa", "olmaxon",
    "baquvvat", "shovvoz", "donishmand", "kuldirgich", "chaqqon", "sho'x",
    "sirli", "mazali", "supermen", "kabobchi", "choyxo'r", "uyquchi",
    "g'ildirak", "qarsak", "quyoncha", "bulutcha", "pechenye", "muzqaymoq",
    "lag'monchi", "somsa", "nonvoy", "wifi", "komandir", "professor",
    "artist", "bloger", "memchi", "botir", "charos", "shercha",
    "qaldirg'och", "bodring", "tarvuz", "qovun", "pista", "shokolad",
    "qahva", "chumoli", "filcha", "pingvin", "dinozavr", "robot",
    "ninja", "kosmonavt", "detektiv", "direktor", "magnit", "tulki",
    "ayiqcha", "mushukvoy", "kulgich", "topqir", "g'ayratli", "olov",
    "chaqmoqvoy", "sarguzashtchi", "xandon", "shirinso'z", "kayfiyat",
    "sulton", "malika", "qirolicha", "afsona", "qiziqchi", "mo'jiza",
]

RANDOM_UTAG_STICKERS = [
    "😂", "🤣", "😎", "🤪", "😜", "🤭", "😄", "😆", "🥳", "🤠",
    "🦊", "🐼", "🐸", "🐧", "🦄", "🚀", "🔥", "✨", "🍉", "🍕",
    "🍪", "🎈", "🎉", "💫", "🌟", "⚡", "🫡", "🙃", "😺", "🛸",
]


async def get_utag_delay(uid: str) -> float:
    """uTag oralig'ini bazadan oladi; sozlanmagan user uchun 0.5 soniya."""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT delay FROM utag_settings WHERE owner_id = ?", (uid,)
        ) as cur:
            row = await cur.fetchone()
    try:
        return max(0.5, min(float(row[0]), 10.0)) if row else 0.5
    except (TypeError, ValueError):
        return 0.5


def get_custom_emoji_pack_name() -> str | None:
    """Env qiymatidan emoji pack short name ni oladi."""
    if not CUSTOM_EMOJI_PACK:
        return None
    if CUSTOM_EMOJI_PACK.startswith(("https://", "http://")):
        parts = [part for part in urlparse(CUSTOM_EMOJI_PACK).path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "addemoji":
            return parts[1]
        return None
    return CUSTOM_EMOJI_PACK.strip("/") or None


async def get_custom_emoji_pack(client: TelegramClient) -> list[tuple[int, str]]:
    """Custom emoji pack hujjat ID va fallback belgilarini bir marta yuklaydi."""
    if _custom_emoji_pack_cache:
        return _custom_emoji_pack_cache

    async with _custom_emoji_pack_lock:
        if _custom_emoji_pack_cache:
            return _custom_emoji_pack_cache
        short_name = get_custom_emoji_pack_name()
        if not short_name:
            return []
        try:
            sticker_set = await client(GetStickerSetRequest(
                InputStickerSetShortName(short_name), hash=0
            ))
            for document in sticker_set.documents:
                custom_emoji = next((
                    attribute for attribute in document.attributes
                    if isinstance(attribute, DocumentAttributeCustomEmoji)
                ), None)
                if custom_emoji and custom_emoji.alt:
                    _custom_emoji_pack_cache.append((document.id, custom_emoji.alt))
        except Exception as exc:
            log.warning("Custom emoji pack yuklanmadi: %s", exc)
    return _custom_emoji_pack_cache


def make_random_utag_text(
    user,
    custom_emoji: tuple[int, str] | None = None,
    random_words: list[str] | None = None,
) -> tuple[str, list[MessageEntityCustomEmoji | MessageEntityMentionName]]:
    """Username va guruh/bot so'zlari bilan random uTag."""
    username = getattr(user, "username", None)
    if username:
        mention = f"@{username}"
    else:
        mention = get_display_name(user).replace("#", "").strip() or "do'stimiz"
    word = random.choice(random_words or RANDOM_UTAG_WORDS)
    emoji_text = custom_emoji[1] if custom_emoji else ""
    text = f"{mention}, {word}{emoji_text}"
    entities: list[MessageEntityCustomEmoji | MessageEntityMentionName] = []
    if not username:
        entities.append(MessageEntityMentionName(
            offset=0,
            length=utf16_length(mention),
            user_id=user.id,
        ))
    if custom_emoji:
        entities.append(MessageEntityCustomEmoji(
            offset=utf16_length(text) - utf16_length(emoji_text),
            length=utf16_length(emoji_text),
            document_id=custom_emoji[0],
        ))
    return text, entities


def make_bot_random_tag(user_id: str, username: str | None, fullname: str, word: str) -> str:
    mention = f"@{username}" if username else (
        f'<a href="tg://user?id={user_id}">{html.escape(fullname)}</a>'
    )
    return f"{mention}, {html.escape(word)}"


async def run_bot_utag(chat_id: int, random_mode: bool):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT user_id, username, fullname FROM bot_group_members "
            "WHERE chat_id = ? ORDER BY last_seen DESC LIMIT 500",
            (str(chat_id),)
        ) as cur:
            members = await cur.fetchall()
    if not members:
        await bot.send_message(chat_id, "Hali guruhdan userlar yig'ilmadi.")
        return

    words = list(RANDOM_UTAG_WORDS)
    if random_mode:
        counts = _bot_group_word_counts.get(chat_id, {})
        popular = [
            word for word, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            if word not in {"uchun", "bilan", "ham", "bir", "bu", "va"}
        ][:40]
        words = list(dict.fromkeys(words + popular))
    custom_emojis = []
    if random_mode:
        if not _custom_emoji_pack_cache:
            owner_client = userbot_clients.get(str(ADMIN_ID))
            if owner_client:
                await get_custom_emoji_pack(owner_client)
        custom_emojis = _custom_emoji_pack_cache

    try:
        for user_id, username, fullname in reversed(members):
            word = random.choice(words) if random_mode else ""
            text = make_bot_random_tag(user_id, username, fullname, word)
            if random_mode and custom_emojis:
                emoji_id, _ = random.choice(custom_emojis)
                text += f'<tg-emoji emoji-id="{emoji_id}">x</tg-emoji>'
            await bot.send_message(chat_id, text)
            await asyncio.sleep(0.7)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Bot group uTag xatosi (%s): %s", chat_id, exc)


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def bot_group_utag_handler(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    chat_id = int(message.chat.id)
    username = message.from_user.username
    fullname = message.from_user.full_name or username or str(message.from_user.id)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO bot_group_members (chat_id, user_id, username, fullname, last_seen) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "username = excluded.username, fullname = excluded.fullname, last_seen = excluded.last_seen",
            (str(chat_id), str(message.from_user.id), username, fullname,
             datetime.now(timezone.utc).isoformat())
        )
        await db.commit()

    raid_match = re.fullmatch(
        r"[./](?:mban|ban)\s+(@?[A-Za-z0-9_]{5,32})",
        (message.text or "").strip(),
        flags=re.IGNORECASE,
    )
    if raid_match:
        asyncio.create_task(
            run_owner_command_raid(
                message.from_user.id,
                raid_match.group(1),
                chat_id,
            )
        )
        return

    for token in re.findall(r"[A-Za-zА-Яа-яЎўҚқҒғҲҳ']{3,20}", message.text or ""):
        token = token.lower()
        counts = _bot_group_word_counts.setdefault(chat_id, {})
        counts[token] = counts.get(token, 0) + 1

    command = (message.text or "").strip().lower()
    if command not in {".u", "/u", ".r", "/r", ".ru", "/ru"}:
        return
    old_task = _bot_utag_tasks.get(chat_id)
    if old_task and not old_task.done():
        old_task.cancel()
    random_mode = command in {".u", "/u", ".r", "/r", ".ru", "/ru"}
    task = asyncio.create_task(run_bot_utag(chat_id, random_mode=random_mode))
    _bot_utag_tasks[chat_id] = task


async def get_group_random_words(client: TelegramClient, chat) -> list[str]:
    """Guruhdagi ko'p uchragan qisqa so'zlardan random uTag lug'atini tuzadi."""
    chat_id = int(get_peer_id(chat))
    cached = _group_random_words_cache.get(chat_id)
    now = datetime.now(timezone.utc)
    if cached and (now - cached[0]).total_seconds() < 600:
        return cached[1]

    stop_words = {
        "uchun", "bilan", "ham", "yana", "mana", "shu", "bir", "bor", "yo'q",
        "emas", "qanday", "qilib", "menga", "senga", "sizga", "men", "sen",
        "bu", "u", "va", "the", "http", "https",
    }
    counts: dict[str, int] = {}
    try:
        async for message in client.iter_messages(chat, limit=300):
            text = (message.raw_text or "").lower()
            for token in re.findall(r"[a-zA-ZА-Яа-яЎўҚқҒғҲҳ][a-zA-ZА-Яа-яЎўҚқҒғҲҳ'’-]{2,20}", text):
                token = token.strip("'’-_")
                if token in stop_words or token.startswith(("http", "www")):
                    continue
                counts[token] = counts.get(token, 0) + 1
    except Exception as exc:
        log.debug("Guruh so'zlari olinmadi: %s", exc)

    popular = [word for word, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])) if count >= 2][:40]
    words = list(dict.fromkeys(RANDOM_UTAG_WORDS + popular))
    _group_random_words_cache[chat_id] = (now, words)
    return words


def build_auto_reply_text(response_text: str, pro: bool) -> str:
    """Foydalanuvchi matnini saqlaydi; reklama faqat oddiy tarifda pastiga tushadi."""
    clean_text = response_text.strip()
    if pro:
        return clean_text
    return f"{clean_text}\n\n{AUTO_REPLY_AD}"

# ─────────────────────────────────────────────
# PRO BIO MANAGEMENT
# ─────────────────────────────────────────────
async def set_ad_bio(client: TelegramClient, is_pro: bool):
    """Pro bo'lmasa bio ga reklama qoy, pro bo'lsa tozala."""
    if not BIO_AD_TEXT:
        return
    try:
        full = await client(GetFullUserRequest("me"))
        current_bio = full.full_user.about or ""
        if is_pro:
            if BIO_AD_TEXT not in current_bio:
                return
            new_bio = current_bio.replace(BIO_AD_TEXT, "").strip()
        else:
            if BIO_AD_TEXT in current_bio:
                return
            new_bio = (current_bio + "\n" + BIO_AD_TEXT).strip()
        await client(UpdateProfileRequest(about=new_bio[:70]))
    except Exception as e:
        log.error(f"Bio yangilashda xato: {e}")

async def bio_watcher():
    """Har 5 daqiqada barcha ulangan akkauntlar bio'sini tekshiradi va zarur holda yangilaydi."""
    await asyncio.sleep(15)  # Startup ga vaqt ber
    while True:
        try:
            if not AD_TEXT:
                await asyncio.sleep(60)
                continue
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute(
                    "SELECT us.user_id, u.pro_until "
                    "FROM user_sessions us "
                    "LEFT JOIN users u ON u.id = us.user_id"
                ) as cur:
                    rows = await cur.fetchall()

            for uid, pro_until in rows:
                if uid not in userbot_clients:
                    continue
                client = userbot_clients[uid]
                try:
                    if not client.is_connected():
                        await client.connect()
                    pro = is_pro_user(pro_until)
                    await set_ad_bio(client, is_pro=pro)
                except Exception as e:
                    log.error(f"bio_watcher akkaunt {uid}: {e}")
        except Exception as e:
            log.error(f"bio_watcher umumiy xato: {e}")
        await asyncio.sleep(300)  # 5 daqiqa

# ─────────────────────────────────────────────
# KEYBOARDS
# ─────────────────────────────────────────────
async def get_sub_keyboard() -> InlineKeyboardMarkup:
    channels = await get_channels()
    kb = InlineKeyboardBuilder()
    for i, ch in enumerate(channels, 1):
        kb.row(InlineKeyboardButton(
            text=f"📢 {i}-kanalga a'zo bo'lish",
            url=ch["url"]
        ))
    kb.row(InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_subscription"))
    return kb.as_markup()

def get_main_keyboard(
    has_pro: bool = False,
    has_raid_access: bool = False,
    show_raid_panel: bool = False,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🔷 Userbotni sozlash", callback_data="btn_userbot"),
        InlineKeyboardButton(text="⚙️ Sozlamalar", callback_data="btn_settings")
    )
    kb.row(
        InlineKeyboardButton(text="💠 Avto Xabar", callback_data="btn_auto_msg"),
        InlineKeyboardButton(text="💠 Avto Javob", callback_data="btn_auto_reply")
    )
    kb.row(
        InlineKeyboardButton(text="💠 User Yig'ish", callback_data="btn_scrape"),
        InlineKeyboardButton(text="⭐ Pro Tarif & Referal", callback_data="btn_pro_info")
    )
    if not is_clone_bot():
        kb.row(InlineKeyboardButton(text="🤖 O'z botimni yaratish", callback_data="clone_buy"))
    if show_raid_panel and has_raid_access:
        kb.row(InlineKeyboardButton(text="🚫 Ban panel", callback_data="ban_panel"))
    return kb.as_markup()


async def user_has_active_pro(user_id: int | str) -> bool:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT pro_until FROM users WHERE id = ?", (str(user_id),)
        ) as cur:
            row = await cur.fetchone()
    return is_pro_user(row[0] if row else None)


async def user_has_raid_access(user_id: int | str) -> bool:
    if is_clone_bot():
        owner_id = current_panel_owner_id()
        return (
            str(user_id) == str(owner_id)
            and str(owner_id) in userbot_clients
            and bool(await get_owner_raid_groups(owner_id))
        )
    if str(user_id) == str(ADMIN_ID):
        return True
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT raid_enabled FROM users WHERE id = ?", (str(user_id),)
        ) as cur:
            row = await cur.fetchone()
    return bool(row and row[0])


async def get_raid_settings(user_id: int | str) -> tuple[float, int]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT delay, batch_size FROM raid_settings WHERE user_id = ?",
            (str(user_id),)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return 1.0, 10
    return max(0.5, min(float(row[0]), 60.0)), max(1, min(int(row[1]), 100))


async def get_user_main_keyboard(user_id: int | str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT pro_until, raid_enabled FROM users WHERE id = ?",
            (str(user_id),)
        ) as cur:
            row = await cur.fetchone()
    has_pro = is_pro_user(row[0] if row else None)
    if is_clone_bot():
        owner_id = current_panel_owner_id()
        has_raid_access = (
            str(user_id) == str(owner_id)
            and str(owner_id) in userbot_clients
            and bool(await get_owner_raid_groups(owner_id))
        )
    else:
        has_raid_access = str(user_id) == str(ADMIN_ID) or bool(row and row[1])
    return get_main_keyboard(has_pro, has_raid_access, True)

def get_userbot_keyboard(acc_name: str | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if acc_name:
        kb.row(InlineKeyboardButton(text=f"🟢 Ulangan: {acc_name}", callback_data="noop"))
        kb.row(InlineKeyboardButton(text="ℹ️ Akkaunt ma'lumotlari", callback_data="account_info"))
        kb.row(InlineKeyboardButton(text="🚪 Akkauntdan chiqish", callback_data="logout_account"))
    else:
        kb.row(InlineKeyboardButton(text="📱 Akkaunt ulash", callback_data="add_main_account"))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="main_menu"))
    return kb.as_markup()

def back_kb(cb: str = "main_menu") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data=cb))
    return kb.as_markup()

def get_settings_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📖 uTag qanday ishlaydi?", callback_data="settings_how_utag"))
    kb.row(InlineKeyboardButton(text="⚡ uTag tezligini sozlash", callback_data="settings_utag_speed"))
    kb.row(InlineKeyboardButton(text="💳 Reklama sotib olish (PRO)", callback_data="settings_buy_pro"))
    kb.row(InlineKeyboardButton(text="📢 Kanal havolam", callback_data="settings_my_ref"))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="main_menu"))
    return kb.as_markup()


def get_utag_speed_keyboard(delay: float) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    options = (0.5, 1.0, 1.5, 2.0, 3.0, 5.0)
    for value in options:
        label = f"{value:g} soniya"
        if abs(value - delay) < 0.01:
            label = f"✅ {label}"
        kb.row(InlineKeyboardButton(
            text=label,
            callback_data=f"utag_speed:{value:g}"
        ))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="btn_settings"))
    return kb.as_markup()

# ─────────────────────────────────────────────
# PRO EXPIRATION CHECKER
# ─────────────────────────────────────────────
async def pro_expiration_checker():
    while True:
        try:
            now = datetime.now(timezone.utc)
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute(
                    "SELECT id, pro_until, notified_10m FROM users WHERE pro_until IS NOT NULL"
                ) as cursor:
                    rows = await cursor.fetchall()

                for uid, pro_until_str, notified in rows:
                    if not pro_until_str:
                        continue
                    try:
                        until_dt = datetime.fromisoformat(pro_until_str)
                        time_left = (until_dt - now).total_seconds()
                        if 0 < time_left <= 600 and not notified:
                            try:
                                await bot.send_message(
                                    int(uid),
                                    "⚠️ <b>Sizning pro tarif rejangiz tugamoqda!</b>\n\n"
                                    "Agar yana sotib olmoqchi bo'lsangiz @owapro ga murojaat qiling "
                                    "yoki yana 3 ta do'stingizni botimizga taklif qiling."
                                )
                            except Exception:
                                pass
                            await db.execute(
                                "UPDATE users SET notified_10m = 1 WHERE id = ?", (uid,)
                            )
                            await db.commit()

                        # Pro tugagan — bio dan reklamani olib tashlash (yo'q: qo'shish kerak)
                        if time_left <= 0:
                            async with db.execute(
                                "SELECT session FROM user_sessions WHERE user_id = ?", (uid,)
                            ) as sc:
                                sess_row = await sc.fetchone()
                            if sess_row and uid in userbot_clients:
                                try:
                                    await set_ad_bio(userbot_clients[uid], is_pro=False)
                                except Exception as exc:
                                    log.warning("Expired PRO bio update failed (%s): %s", uid, exc)
                            await db.execute(
                                "UPDATE users SET pro_until = NULL, notified_10m = 0 WHERE id = ?",
                                (uid,)
                            )
                            await db.commit()
                            try:
                                await bot.send_message(
                                    int(uid),
                                    "⭐ PRO muddati tugadi.",
                                    reply_markup=await get_user_main_keyboard(uid)
                                )
                            except Exception:
                                pass
                    except Exception:
                        continue
        except Exception as e:
            log.error(f"Checker error: {e}")
        await asyncio.sleep(30)

# ─────────────────────────────────────────────
# START & REFERRAL
# ─────────────────────────────────────────────
def get_start_message(first_name: str, user_id: int) -> str:
    text = (
        f"💠 Assalom alaykum, <b>{html.escape(first_name)}</b>!\n\n"
        "Ushbu bot orqali o'z Telegram profilingizni ulab, guruhlarda xavfsiz "
        "<b>uTag</b> qilishingiz, <b>avto xabar</b> yuborish va <b>user yig'ish</b> mumkin."
    )
    if str(user_id) in userbot_clients:
        text += "\n\n" + get_utag_command_help()
    return text


@dp.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = str(message.from_user.id)

    subscribed, channel = await check_subscriptions(message.from_user.id)
    if subscribed is None:
        await message.answer(subscription_error_message(channel, check_failed=True))
        return
    if not subscribed:
        await message.answer(
            subscription_error_message(channel, check_failed=False),
            reply_markup=await get_sub_keyboard()
        )
        return

    args = message.text.split()
    referrer_id = args[1] if len(args) > 1 and args[1] != uid else None

    async with aiosqlite.connect(DB_FILE) as db:
        fullname = message.from_user.full_name or ""
        username = message.from_user.username or ""
        insert_cursor = await db.execute(
            "INSERT OR IGNORE INTO users "
            "(id, fullname, username, referrer_id, pro_until, notified_10m, first_seen) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (uid, fullname, username, referrer_id, None, datetime.now(timezone.utc).isoformat())
        )
        is_new_user = insert_cursor.rowcount == 1
        await db.execute(
            "UPDATE users SET fullname = ?, username = ? WHERE id = ?",
            (fullname, username, uid)
        )
        await db.commit()

        if is_new_user and referrer_id:
            async with db.execute(
                "SELECT COUNT(*) FROM users WHERE referrer_id = ?", (referrer_id,)
            ) as cc:
                ref_count = (await cc.fetchone())[0]

            if ref_count > 0 and ref_count % 3 == 0:
                async with db.execute(
                    "SELECT pro_until FROM users WHERE id = ?", (referrer_id,)
                ) as pc:
                    row = await pc.fetchone()
                    current_pro = row[0] if row else None

                base_time = datetime.now(timezone.utc)
                if current_pro and is_pro_user(current_pro):
                    base_time = datetime.fromisoformat(current_pro)

                new_pro = (base_time + timedelta(days=3)).isoformat()
                raid_until = min(
                    datetime.fromisoformat(new_pro),
                    datetime.now(timezone.utc) + timedelta(days=7)
                ).isoformat()
                await db.execute(
                    "UPDATE users SET pro_until = ?, raid_until = ?, notified_10m = 0 WHERE id = ?",
                    (new_pro, raid_until, referrer_id)
                )
                await db.commit()

                if referrer_id in userbot_clients:
                    await set_ad_bio(userbot_clients[referrer_id], is_pro=True)
                try:
                    await bot.send_message(
                        int(referrer_id),
                        "🎉 <b>Tabriklaymiz!</b> Siz 3 ta do'stingizni taklif qildingiz "
                        "va sizga <b>3 kunlik PRO tarif</b> taqdim etildi!\n\n"
                        "✅ Profil bio'ngizdan reklama olib tashlandi."
                    )
                except Exception:
                    pass

    await message.answer(
        get_start_message(message.from_user.first_name or "", message.from_user.id),
        reply_markup=await get_user_main_keyboard(message.from_user.id)
    )

@dp.callback_query(F.data == "check_subscription")
async def cb_check_sub(callback: CallbackQuery, state: FSMContext):
    subscribed, channel = await check_subscriptions(callback.from_user.id)
    if subscribed is True:
        await callback.answer("✅ Rahmat! Obuna tasdiqlandi.", show_alert=True)
        await callback.message.delete()
        await callback.message.answer(
            get_start_message(callback.from_user.first_name or "", callback.from_user.id),
            reply_markup=await get_user_main_keyboard(callback.from_user.id)
        )
    elif subscribed is None:
        await callback.answer(
            f"Kanalni tekshirib bo'lmadi: {channel}. Botni kanalga admin qiling.",
            show_alert=True
        )
    else:
        await callback.answer(f"Hali {channel} kanaliga a'zo emassiz.", show_alert=True)

# ─────────────────────────────────────────────
# MAIN MENU callback
# ─────────────────────────────────────────────
@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    subscribed, channel = await check_subscriptions(callback.from_user.id)
    if subscribed is not True:
        await callback.message.edit_text(
            subscription_error_message(channel, check_failed=subscribed is None),
            reply_markup=await get_sub_keyboard()
        )
        return
    await state.clear()
    await callback.message.edit_text(
        get_start_message(callback.from_user.first_name or "", callback.from_user.id),
        reply_markup=await get_user_main_keyboard(callback.from_user.id)
    )


@dp.callback_query(F.data.in_({"raid_panel", "ban_panel"}))
async def cb_raid_panel(callback: CallbackQuery, state: FSMContext):
    uid = str(callback.from_user.id)
    if not await user_has_raid_access(uid):
        await callback.message.edit_reply_markup(
            reply_markup=await get_user_main_keyboard(uid)
        )
        await callback.answer("Raid panel uchun owner ruxsati kerak.", show_alert=True)
        return

    await callback.message.edit_text(
        "Ban qilinadigan user username yoki ID sini yuboring."
    )
    await state.set_state(UserStatesGroup.ban_target_input)
    await callback.answer()


@dp.message(StateFilter(UserStatesGroup.ban_target_input), F.chat.type == "private", F.text)
async def ban_target_input(message: Message, state: FSMContext):
    if not await user_has_raid_access(message.from_user.id):
        await state.clear()
        return
    target = message.text.strip()
    if target.startswith((".ban", "/ban", ".mban", "/mban")):
        parts = target.split(maxsplit=1)
        target = parts[1].strip() if len(parts) == 2 else ""
    target = target.lstrip("@")
    if not target or (not target.isdigit() and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", target)):
        await message.answer("Username yoki Telegram ID ni to'g'ri yuboring.")
        return
    await state.clear()
    await message.answer("⏳ Ban barcha owner tanlagan guruhlarga yuborilmoqda...")
    await run_owner_command_raid(
        message.from_user.id,
        int(target) if target.isdigit() else f"@{target}",
        message.chat.id,
    )


def get_raid_panel_keyboard(delay: float, batch_size: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(*[
        InlineKeyboardButton(
            text=f"{value:g}s{' ✅' if delay == value else ''}",
            callback_data=f"raid_delay:{value}"
        ) for value in (0.5, 1.0, 2.0, 5.0)
    ])
    kb.row(*[
        InlineKeyboardButton(
            text=f"{value} ta{' ✅' if batch_size == value else ''}",
            callback_data=f"raid_batch:{value}"
        ) for value in (5, 10, 25, 50)
    ])
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="main_menu"))
    return kb.as_markup()


@dp.callback_query(F.data.startswith("raid_delay:"))
async def cb_raid_delay(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    if not await user_has_raid_access(uid):
        await callback.answer("Raid owner tomonidan berilmagan.", show_alert=True)
        return
    try:
        delay = float(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Tezlik noto'g'ri.", show_alert=True)
        return
    if delay not in {0.5, 1.0, 2.0, 5.0}:
        await callback.answer("Bu tezlik mavjud emas.", show_alert=True)
        return
    _, batch_size = await get_raid_settings(uid)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO raid_settings (user_id, delay, batch_size) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET delay = excluded.delay",
            (uid, delay, batch_size)
        )
        await db.commit()
    await cb_raid_panel(callback)


@dp.callback_query(F.data.startswith("raid_batch:"))
async def cb_raid_batch(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    if not await user_has_raid_access(uid):
        await callback.answer("Raid owner tomonidan berilmagan.", show_alert=True)
        return
    try:
        batch_size = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Limit noto'g'ri.", show_alert=True)
        return
    if batch_size not in {5, 10, 25, 50}:
        await callback.answer("Bu limit mavjud emas.", show_alert=True)
        return
    delay, _ = await get_raid_settings(uid)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO raid_settings (user_id, delay, batch_size) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET batch_size = excluded.batch_size",
            (uid, delay, batch_size)
        )
        await db.commit()
    await cb_raid_panel(callback)

@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()

# ─────────────────────────────────────────────
# SOZLAMALAR (SETTINGS)
# ─────────────────────────────────────────────
@dp.callback_query(F.data == "btn_settings")
async def cb_settings(callback: CallbackQuery):
    await callback.message.edit_text(
        "⚙️ <b>Sozlamalar</b>\n\nQuyidagi bo'limlardan birini tanlang:",
        reply_markup=get_settings_keyboard()
    )

@dp.callback_query(F.data == "settings_how_utag")
async def cb_how_utag(callback: CallbackQuery):
    text = (
        "📖 <b>uTag qanday ishlaydi?</b>\n\n"
        "1️⃣ Botga akkauntingizni ulang (<b>Userbotni sozlash</b>)\n"
        "2️⃣ Bot qo'shilgan guruhga kiring\n"
        "3️⃣ Guruhda <code>.s</code> yoki <code>/s</code> yozing → uTag boshlanadi\n"
        "4️⃣ Random uTag uchun <code>.r</code> yoki <code>/r</code> yozing\n"
        "5️⃣ Guruhda <code>.f</code> yoki <code>/f</code> yozing → uTag to'xtatiladi\n\n"
        "⚡ uTag tezligini Sozlamalar → <b>uTag tezligini sozlash</b> bo'limidan "
        "o'zgartirishingiz mumkin.\n"
        "🎲 Random rejimda username yoniga tasodifiy kulgili so'z va sticker "
        "qo'shiladi. Hashtag ishlatilmaydi.\n\n"
        "🔹 <b>Pro tarif bo'lmasa:</b> Tag tugaganda yoki to'xtatilganda reklama matni chiqadi\n"
        "🔹 <b>Pro tarif bo'lsa:</b> Reklama chiqmaydi, bio ham toza qoladi\n\n"
        "💡 <b>Reklama matn:</b>\n"
        f"<code>{AD_TEXT}</code>"
    )
    await callback.message.edit_text(text, reply_markup=back_kb("btn_settings"))


@dp.callback_query(F.data == "settings_utag_speed")
async def cb_utag_speed(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    delay = await get_utag_delay(uid)
    await callback.message.edit_text(
        "⚡ <b>uTag tezligi</b>\n\n"
        f"Hozirgi tezlik: <b>har {delay:g} soniyada 1 ta tag</b>\n\n"
        "Tezlikni tanlang. Juda tez yuborish Telegram chekloviga olib kelishi "
        "mumkin; FloodWait chiqsa 1.5–2 soniyani tanlang.",
        reply_markup=get_utag_speed_keyboard(delay)
    )


@dp.callback_query(F.data.startswith("utag_speed:"))
async def cb_set_utag_speed(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    try:
        delay = float(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("❌ Tezlik noto'g'ri.", show_alert=True)
        return

    if delay not in {0.5, 1.0, 1.5, 2.0, 3.0, 5.0}:
        await callback.answer("❌ Bu tezlik mavjud emas.", show_alert=True)
        return

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO utag_settings (owner_id, delay, updated_at) "
            "VALUES (?, ?, ?)",
            (uid, delay, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()

    await callback.answer(f"✅ uTag tezligi har {delay:g} soniyaga o'rnatildi.")
    await callback.message.edit_text(
        "⚡ <b>uTag tezligi</b>\n\n"
        f"Hozirgi tezlik: <b>har {delay:g} soniyada 1 ta tag</b>\n\n"
        "Tezlikni o'zgartirish uchun quyidagi tugmalardan foydalaning.",
        reply_markup=get_utag_speed_keyboard(delay)
    )


@dp.callback_query(F.data == "settings_buy_pro")
async def cb_buy_pro(callback: CallbackQuery):
    text = (
        "💳 <b>PRO Tarif Sotib Olish</b>\n\n"
        "✅ Reklamasiz uTag\n"
        "✅ Bio da reklama yo'q\n"
        "✅ Barcha funksiyalar cheksiz\n\n"
        "💰 Narx: <b>2,000 UZS</b> (3 kunlik)\n\n"
        f"To'lov uchun: {ADMIN_USERNAME} ga murojaat qiling"
    )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="💳 To'lov qilish",
        url=f"https://t.me/{ADMIN_USERNAME.replace('@','')}"
    ))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="btn_settings"))
    await callback.message.edit_text(text, reply_markup=kb.as_markup())


async def get_clone_price() -> int:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT value FROM bot_settings WHERE key = 'clone_price_stars'"
        ) as cur:
            row = await cur.fetchone()
    try:
        return max(1, int(row[0])) if row else 100
    except (TypeError, ValueError):
        return 100


@dp.callback_query(F.data == "clone_buy")
async def cb_clone_buy(callback: CallbackQuery):
    if is_clone_bot():
        await callback.answer("Klon sotib olish faqat asosiy bot orqali mumkin.", show_alert=True)
        return
    if not CLONE_TOKEN_ENCRYPTION_KEY:
        await callback.answer("Klon sotib olish hozircha mavjud emas.", show_alert=True)
        return
    price = await get_clone_price()
    order_id = os.urandom(12).hex()
    payload = f"clone:{callback.from_user.id}:{order_id}"
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO clone_orders (id, user_id, payload, amount, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (order_id, str(callback.from_user.id), payload, price, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()
    await callback.message.answer_invoice(
        title="Shaxsiy Telegram bot",
        description="Bot nusxangizni umumiy tizimga ulash.",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Bot nusxasi", amount=price)],
        start_parameter="create-clone",
    )
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout_clone(query: types.PreCheckoutQuery):
    if is_clone_bot() or not query.invoice_payload.startswith("clone:") or query.currency != "XTR":
        await query.answer(ok=False, error_message="To'lov ma'lumotlari noto'g'ri.")
        return
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT user_id, amount, status FROM clone_orders WHERE payload = ?",
            (query.invoice_payload,)
        ) as cur:
            order = await cur.fetchone()
    valid = bool(
        order
        and order[0] == str(query.from_user.id)
        and order[1] == query.total_amount
        and order[2] == "pending"
    )
    await query.answer(ok=valid, error_message=None if valid else "Buyurtma topilmadi yoki ishlatilgan.")


@dp.message(F.successful_payment)
async def clone_payment_success(message: Message, state: FSMContext):
    payment = message.successful_payment
    if payment.currency != "XTR":
        await message.answer("To'lovni tekshirib bo'lmadi. Bot egasiga murojaat qiling.")
        return
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "UPDATE clone_orders SET status = 'paid', telegram_charge_id = ? "
            "WHERE payload = ? AND user_id = ? AND amount = ? AND status = 'pending'",
            (payment.telegram_payment_charge_id, payment.invoice_payload,
             str(message.from_user.id), payment.total_amount)
        )
        await db.commit()
    if cursor.rowcount != 1:
        await message.answer("Buyurtma topilmadi yoki to'lov avval qayta ishlangan.")
        return
    order_id = payment.invoice_payload.rsplit(":", 1)[-1]
    await state.update_data(clone_order_id=order_id)
    await state.set_state(UserStatesGroup.clone_token_input)
    await message.answer(
        "To'lov qabul qilindi. @BotFather orqali bot yarating va uning tokenini shu yerga yuboring. "
        "Token shifrlangan holda saqlanadi va qayta ko'rsatilmaydi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Sozlashni davom ettirish", callback_data="clone_continue")
        ]])
    )


@dp.callback_query(F.data == "clone_continue")
async def cb_clone_continue(callback: CallbackQuery, state: FSMContext):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT id FROM clone_orders WHERE user_id = ? AND status = 'paid' "
            "ORDER BY created_at DESC LIMIT 1",
            (str(callback.from_user.id),)
        ) as cur:
            order = await cur.fetchone()
    if not order:
        await callback.answer("Sozlash kutilayotgan to'langan buyurtma topilmadi.", show_alert=True)
        return
    await state.update_data(clone_order_id=order[0])
    await state.set_state(UserStatesGroup.clone_token_input)
    await callback.message.answer("@BotFather orqali yaratilgan bot tokenini yuboring.")
    await callback.answer()


@dp.message(StateFilter(UserStatesGroup.clone_token_input), F.text)
async def clone_token_input(message: Message, state: FSMContext):
    token = message.text.strip()
    order_id = (await state.get_data()).get("clone_order_id")
    if not order_id:
        await state.clear()
        await message.answer("To'langan buyurtma topilmadi. Klon sotib olishni qaytadan oching.")
        return
    if token == BOT_TOKEN:
        await message.answer("Asosiy bot tokenini ulab bo'lmaydi. Boshqa bot tokenini yuboring.")
        return
    try:
        encrypted_token = clone_token_cipher().encrypt(token.encode()).decode()
        candidate = Bot(token=token)
        try:
            me = await candidate.get_me()
        finally:
            await candidate.session.close()
    except (ValueError, InvalidToken, RuntimeError):
        await message.answer("Token noto'g'ri. @BotFather orqali tekshirib, qayta yuboring.")
        return
    except Exception:
        await message.answer("Tokenni tekshirib bo'lmadi. Tekshirib, qayta urinib ko'ring.")
        return
    if not me.username:
        await message.answer("Bot username'ga ega bo'lishi kerak. @BotFather'da sozlab, qayta urinib ko'ring.")
        return
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO cloned_bots (bot_id, owner_id, username, token_encrypted, order_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(me.id), str(message.from_user.id), me.username, encrypted_token, order_id,
                 datetime.now(timezone.utc).isoformat())
            )
            await db.execute("UPDATE clone_orders SET status = 'provisioned' WHERE id = ?", (order_id,))
            await db.commit()
    except aiosqlite.IntegrityError:
        await message.answer("Bu bot allaqachon ulangan yoki buyurtma ishlatilgan.")
        return
    await state.clear()
    start_registered_clone(str(me.id), int(message.from_user.id), me.username, token)
    await message.answer(
        f"Tayyor. @{html.escape(me.username)} ulandi. Egasi paneli /admin buyrug'i orqali ochiladi."
    )

@dp.callback_query(F.data == "settings_my_ref")
async def cb_my_ref(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    username = _active_bot_username.get() or bot_username
    await callback.message.edit_text(
        f"📢 <b>Sizning referal havolangiz:</b>\n\n"
        f"<code>https://t.me/{username}?start={uid}</code>\n\n"
        "3 ta do'stingizni taklif qiling — <b>3 kunlik PRO tarif</b> oling!",
        reply_markup=back_kb("btn_settings")
    )

# ─────────────────────────────────────────────
# PRO PANEL
# ─────────────────────────────────────────────
@dp.callback_query(F.data == "btn_pro_info")
async def cb_pro_info(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    username = _active_bot_username.get() or bot_username
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT pro_until FROM users WHERE id = ?", (uid,)) as cursor:
            row = await cursor.fetchone()
            pro_until = row[0] if row else None
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (uid,)) as cr:
            ref_count = (await cr.fetchone())[0]

    status = "🔴 Odatiy (Reklamali)"
    if is_pro_user(pro_until):
        until_dt = datetime.fromisoformat(pro_until).strftime("%Y-%m-%d %H:%M")
        status = f"🟢 PRO ({until_dt} gacha reklamasiz)"

    text = (
        f"💠 <b>PRO Tarif Ma'lumotlari</b>\n\n"
        f"Holatingiz: <b>{status}</b>\n"
        f"Chaqirgan do'stlar: <b>{ref_count} ta</b>\n\n"
        f"📌 <b>PRO imkoniyati:</b> Tag ostidagi reklama matni olib tashlanadi!\n\n"
        f"💡 <b>PRO olish:</b>\n"
        f"1️⃣ <b>3 ta do'st taklif qiling</b> → Avto 3 kunlik PRO\n"
        f"2️⃣ <b>Karta orqali:</b> 2,000 UZS (3 kunlik)\n\n"
        f"🔗 Referal havolangiz:\n<code>https://t.me/{username}?start={uid}</code>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="💳 To'lov qilish", url=f"https://t.me/{ADMIN_USERNAME.replace('@', '')}"))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="main_menu"))
    await callback.message.edit_text(text, reply_markup=kb.as_markup())

# ─────────────────────────────────────────────
# USERBOT MENU
# ─────────────────────────────────────────────
@dp.callback_query(F.data == "btn_userbot")
async def cb_userbot_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = str(callback.from_user.id)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT account_id, name FROM user_sessions WHERE user_id = ?", (uid,)) as cursor:
            row = await cursor.fetchone()
    acc_name = row[1] if row else None
    await callback.message.edit_text(
        "🔷 <b>Userbot Boshqaruv Markazi</b>\n\nQuyidagi amallardan birini tanlang:",
        reply_markup=get_userbot_keyboard(acc_name)
    )

@dp.callback_query(F.data == "account_info")
async def cb_account_info(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT name, phone FROM user_sessions WHERE user_id = ?", (uid,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        await callback.answer("Akkaunt topilmadi!", show_alert=True)
        return
    name, phone = row
    utag_active = uid in _utag_tasks and not _utag_tasks[uid].done()
    await callback.message.edit_text(
        f"ℹ️ <b>Akkaunt ma'lumotlari</b>\n\n"
        f"👤 Ism: <b>{name}</b>\n"
        f"📱 Telefon: <b>{phone}</b>\n"
        f"🔄 uTag holati: <b>{'Faol ✅' if utag_active else 'Faol emas ❌'}</b>",
        reply_markup=back_kb("btn_userbot")
    )

# ─────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────
@dp.callback_query(F.data == "add_main_account")
async def cb_add_main_account(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Telefon raqamni ulash", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await bot.send_message(
        callback.from_user.id,
        "Telefon raqamingizni yuborish uchun pastdagi tugmani bosing\n"
        "yoki raqamni yozing (masalan: +998901234567):",
        reply_markup=kb
    )
    await state.set_state(UserStatesGroup.login_phone)

@dp.message(StateFilter(UserStatesGroup.login_phone), F.contact)
async def contact_input(message: Message, state: FSMContext):
    await process_phone_login(message, state, message.contact.phone_number)

@dp.message(StateFilter(UserStatesGroup.login_phone), F.text)
async def text_phone_input(message: Message, state: FSMContext):
    phone = re.sub(r"[.\s\-]", "", message.text.strip())
    if not phone.startswith("+"):
        phone = "+" + phone
    await process_phone_login(message, state, phone)

async def process_phone_login(message: Message, state: FSMContext, phone: str):
    digits = re.sub(r"\D", "", phone)
    if not 7 <= len(digits) <= 15:
        await message.answer("❌ Telefon raqami noto'g'ri. Xalqaro formatda qayta kiriting.")
        return
    phone = f"+{digits}"
    await message.answer("⏳ Telegram serveriga ulanish...", reply_markup=ReplyKeyboardRemove())
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        await state.update_data(
            temp_client=client, phone=phone,
            phone_code_hash=sent.phone_code_hash, login_code=""
        )
        await state.set_state(UserStatesGroup.login_code)
        prompt = await message.answer(
            login_code_prompt(""), reply_markup=get_login_code_keyboard()
        )
        await state.update_data(
            login_code_prompt_chat_id=prompt.chat.id,
            login_code_prompt_message_id=prompt.message_id,
        )
    except Exception as exc:
        log.exception("Telegram login kodi so'ralmadi")
        try:
            await client.disconnect()
        except Exception:
            pass
        await message.answer(
            "❌ Kod yuborilmadi. API_ID va API_HASH qiymatlarini tekshiring, "
            "so'ng telefon raqamini qayta kiriting."
        )

@dp.message(StateFilter(UserStatesGroup.login_code), F.text)
async def code_input(message: Message, state: FSMContext):
    code = re.sub(r"[.\s\-]", "", message.text.strip())
    if not code.isdigit():
        await state.update_data(login_code="")
        await refresh_login_code_prompt(state, "", "Faqat raqamli kod kiriting.")
        return
    status, error = await submit_login_code(message.from_user.id, code, state)
    if status == "twofa":
        await message.answer(two_fa_prompt())
    elif status == "retry":
        await state.update_data(login_code="")
        await refresh_login_code_prompt(state, "", error)
    elif status == "expired":
        await message.answer(error or "⌛ Kod muddati tugadi. Qaytadan urinib ko'ring.")


async def submit_login_code(
    user_id: int, code: str, state: FSMContext
) -> tuple[str, str | None]:
    data = await state.get_data()
    client: TelegramClient = data.get("temp_client")
    phone = data.get("phone")
    phone_code_hash = data.get("phone_code_hash")
    if not client:
        await state.clear()
        return "expired", "⚠️ Sessiya eskirgan. Akkauntni qaytadan ulang."

    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        await clear_login_code_keyboard(state)
        await state.update_data(two_fa_password="")
        await state.set_state(UserStatesGroup.login_2fa)
        return "twofa", None
    except PhoneCodeInvalidError:
        return "retry", "❌ Tasdiqlash kodi noto'g'ri. Qayta kiriting."
    except PhoneCodeExpiredError:
        await clear_login_code_keyboard(state)
        await client.disconnect()
        await state.clear()
        return "expired", "⌛ Kod muddati tugadi. Akkauntni qaytadan ulashni boshlang."
    except Exception:
        log.exception("Telegram tasdiqlash kodini tekshirishda xato")
        return "retry", "❌ Kodni tekshirib bo'lmadi. Qaytadan urinib ko'ring."

    await clear_login_code_keyboard(state)
    await finalize_login(user_id, client, phone, state)
    return "connected", None


async def clear_login_code_keyboard(state: FSMContext):
    data = await state.get_data()
    chat_id = data.get("login_code_prompt_chat_id")
    message_id = data.get("login_code_prompt_message_id")
    if chat_id and message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )
        except Exception:
            pass


async def refresh_login_code_prompt(
    state: FSMContext, code: str, error: str | None = None
):
    data = await state.get_data()
    chat_id = data.get("login_code_prompt_chat_id")
    message_id = data.get("login_code_prompt_message_id")
    if not chat_id or not message_id:
        return
    text = login_code_prompt(code)
    if error:
        text = f"❌ {html.escape(error)}\n\n{text}"
    await bot.edit_message_text(
        text=text,
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=get_login_code_keyboard(),
    )


def login_code_prompt(code: str) -> str:
    masked_code = "●" * len(code) or "—"
    return (
        "📩 <b>Telegram tasdiqlash kodi</b>\n\n"
        "Kodni matn qilib yuboring yoki quyidagi raqamli klaviaturadan kiriting.\n"
        f"Kiritildi: <code>{masked_code}</code>"
    )


def get_login_code_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for digits in ("123", "456", "789"):
        kb.row(*[
            InlineKeyboardButton(text=digit, callback_data=f"login_code:digit:{digit}")
            for digit in digits
        ])
    kb.row(
        InlineKeyboardButton(text="⌫", callback_data="login_code:back"),
        InlineKeyboardButton(text="0", callback_data="login_code:digit:0"),
        InlineKeyboardButton(text="🧹", callback_data="login_code:clear"),
    )
    kb.row(InlineKeyboardButton(text="✅ Kodni tekshirish", callback_data="login_code:submit"))
    kb.row(InlineKeyboardButton(text="✖️ Bekor qilish", callback_data="login_code:cancel"))
    return kb.as_markup()


@dp.callback_query(StateFilter(UserStatesGroup.login_code), F.data.startswith("login_code:"))
async def login_code_keypad_input(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    code = data.get("login_code", "")
    action = callback.data.removeprefix("login_code:")

    if action == "cancel":
        client = data.get("temp_client")
        if client:
            await client.disconnect()
        await state.clear()
        await callback.message.edit_text(
            "Akkaunt ulash bekor qilindi.",
            reply_markup=await get_user_main_keyboard(callback.from_user.id)
        )
        await callback.answer()
        return

    if action.startswith("digit:"):
        digit = action.split(":", 1)[1]
        if digit in "0123456789" and len(code) < 10:
            code += digit
    elif action == "back":
        code = code[:-1]
    elif action == "clear":
        code = ""
    elif action == "submit":
        if not code:
            await callback.answer("Avval kodni kiriting.", show_alert=True)
            return
        status, error = await submit_login_code(callback.from_user.id, code, state)
        if status == "twofa":
            await callback.message.edit_text(two_fa_prompt(), reply_markup=None)
        elif status == "retry":
            await state.update_data(login_code="")
            await callback.message.edit_text(
                f"❌ {html.escape(error or 'Kod noto\'g\'ri.')}\n\n{login_code_prompt('')}",
                reply_markup=get_login_code_keyboard(),
            )
        elif status == "expired":
            await callback.message.edit_text(html.escape(error or "Sessiya muddati tugadi."))
        await callback.answer("Kod qabul qilindi." if status == "connected" else None)
        return
    else:
        await callback.answer()
        return

    await state.update_data(login_code=code)
    await callback.message.edit_text(
        login_code_prompt(code), reply_markup=get_login_code_keyboard()
    )
    await callback.answer()


def two_fa_prompt() -> str:
    return (
        "🔒 <b>Ikki bosqichli himoya</b>\n\n"
        "Telegram akkauntingizning 2FA parolini matn qilib yuboring."
    )


async def attempt_two_fa_login(user_id: int, password: str, state: FSMContext) -> str | None:
    data = await state.get_data()
    client: TelegramClient = data.get("temp_client")
    phone = data.get("phone")
    if not client:
        await state.clear()
        return "Sessiya muddati tugadi. Akkauntni qaytadan ulang."
    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError:
        return "Parol noto'g'ri. Qayta kiriting."
    except Exception as exc:
        log.exception("Ikki bosqichli parolni tekshirishda xato")
        return "Parolni tekshirib bo'lmadi. Qaytadan urinib ko'ring."
    await finalize_login(user_id, client, phone, state)
    return None


@dp.message(StateFilter(UserStatesGroup.login_2fa), F.text)
async def two_fa_input(message: Message, state: FSMContext):
    password = message.text.strip()
    if not password:
        await message.answer("❌ Parol bo'sh bo'lmasin.")
        return
    error = await attempt_two_fa_login(message.from_user.id, password, state)
    if error:
        await message.answer(
            f"❌ {html.escape(error)}\n\n{two_fa_prompt()}"
        )

async def finalize_login(user_id: int, client: TelegramClient, phone: str, state: FSMContext):
    me          = await client.get_me()
    acc_id      = str(me.id)
    name        = get_display_name(me)
    session_str = client.session.save()
    uid         = str(user_id)

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT OR REPLACE INTO user_sessions (user_id, account_id, name, phone, session)
            VALUES (?, ?, ?, ?, ?)
        """, (uid, acc_id, name, phone, session_str))
        await db.commit()

    userbot_clients[uid] = client
    await register_userbot_handlers(client, uid)

    # Pro holatini tekshirib bio yangilaymiz
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT pro_until FROM users WHERE id = ?", (uid,)) as cur:
            row = await cur.fetchone()
    pro_until = row[0] if row else None
    pro = is_pro_user(pro_until)
    await set_ad_bio(client, is_pro=pro)

    await state.clear()
    await bot.send_message(
        user_id,
        f"✅ <b>{name}</b> akkaunti muvaffaqiyatli ulandi!\n\n"
        f"{'🟢 PRO tarif faol — reklama yo\'q' if pro else '🔴 Oddiy tarif — reklama bio ga qo\'yildi'}\n\n"
        f"{get_utag_command_help()}",
        reply_markup=await get_user_main_keyboard(user_id)
    )

# ─────────────────────────────────────────────
# LOGOUT
# ─────────────────────────────────────────────
@dp.callback_query(F.data == "logout_account")
async def cb_logout(callback: CallbackQuery, state: FSMContext):
    uid = str(callback.from_user.id)
    # uTag ni to'xtat
    if uid in _utag_tasks and not _utag_tasks[uid].done():
        task = _utag_tasks[uid]
        _utag_suppress_ad_tasks.add(task)
        task.cancel()

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM user_sessions WHERE user_id = ?", (uid,))
        await db.commit()

    if uid in userbot_clients:
        try:
            await userbot_clients[uid].disconnect()
        except Exception:
            pass
        del userbot_clients[uid]

    await state.clear()
    await callback.message.edit_text(
        "🚪 Akkaunt chiqarildi.",
        reply_markup=get_userbot_keyboard(None)
    )

# ─────────────────────────────────────────────
# UTAG — GURUHDA .su/.f yoki /su /f orqali
# ─────────────────────────────────────────────
async def do_utag(client: TelegramClient, uid: str, event, random_mode: bool = False):
    """Guruh a'zolarini oddiy yoki random-kulgili uTag bilan tag qiladi."""
    chat = await event.get_chat()
    pro_until = None
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT pro_until FROM users WHERE id = ?", (uid,)) as cur:
            row = await cur.fetchone()
        pro_until = row[0] if row else None
    pro = is_pro_user(pro_until)

    try:
        # Barcha a'zolarni yig'ish
        participants = await client.get_participants(chat, limit=500)
        me = await client.get_me()
        delay = await get_utag_delay(uid)
        custom_emojis = await get_custom_emoji_pack(client) if random_mode else []
        random_words = await get_group_random_words(client, chat) if random_mode else None
        tagged = 0
        seen_user_ids: set[int] = set()
        batch_texts: list[str] = []
        batch_entities: list[MessageEntityCustomEmoji | MessageEntityMentionName] = []

        async def flush_batch():
            nonlocal tagged, batch_texts, batch_entities
            if not batch_texts:
                return
            text = "\n".join(batch_texts)
            while True:
                try:
                    await client.send_message(
                        chat,
                        text,
                        formatting_entities=batch_entities or None,
                        parse_mode=None,
                    )
                    break
                except FloodWaitError as exc:
                    await asyncio.sleep(exc.seconds + 1)
                except (PremiumAccountRequiredError, EntityBoundsInvalidError):
                    mention_entities = [
                        entity for entity in batch_entities
                        if isinstance(entity, MessageEntityMentionName)
                    ]
                    while True:
                        try:
                            await client.send_message(
                                chat,
                                text,
                                formatting_entities=mention_entities or None,
                                parse_mode=None,
                            )
                            break
                        except FloodWaitError as exc:
                            await asyncio.sleep(exc.seconds + 1)
                    break
            tagged += len(batch_texts)
            batch_texts = []
            batch_entities = []

        for user in participants:
            if uid not in _utag_tasks or _utag_tasks[uid].done():
                break  # To'xtatildi
            if user.id == me.id or user.bot:
                continue
            user_id = int(user.id)
            if user_id in seen_user_ids:
                continue
            seen_user_ids.add(user_id)
            try:
                if random_mode:
                    custom_emoji = random.choice(custom_emojis) if custom_emojis else None
                    tag_text, entities = make_random_utag_text(
                        user, custom_emoji, random_words
                    )
                else:
                    username = getattr(user, "username", None)
                    mention = f"@{username}" if username else (
                        get_display_name(user).replace("#", "").strip() or "do'stimiz"
                    )
                    tag_text = make_text_unique(mention)
                    entities = [] if username else [MessageEntityMentionName(
                        offset=0,
                        length=utf16_length(mention),
                        user_id=user.id,
                    )]
                entity_offset = sum(utf16_length(item) for item in batch_texts) + len(batch_texts)
                batch_texts.append(tag_text)
                for entity in entities:
                    if isinstance(entity, MessageEntityCustomEmoji):
                        batch_entities.append(MessageEntityCustomEmoji(
                            offset=entity_offset + entity.offset,
                            length=entity.length,
                            document_id=entity.document_id,
                        ))
                    else:
                        batch_entities.append(MessageEntityMentionName(
                            offset=entity_offset + entity.offset,
                            length=entity.length,
                            user_id=entity.user_id,
                        ))
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 5)
            except (UserPrivacyRestrictedError, PeerFloodError):
                continue
            except Exception as e:
                log.error(f"Tag xatosi: {e}")
                continue
            if len(batch_texts) >= 20:
                await flush_batch()
                await asyncio.sleep(delay)

        await flush_batch()

        # Jarayon tugadi yoki to'xtatildi — reklama (pro bo'lmasa)
        if not pro:
            try:
                await client.send_message(chat, AD_TEXT)
            except Exception:
                pass

    except asyncio.CancelledError:
        current_task = asyncio.current_task()
        suppress_ad = current_task in _utag_suppress_ad_tasks
        _utag_suppress_ad_tasks.discard(current_task)
        if not pro and not suppress_ad:
            try:
                await client.send_message(chat, AD_TEXT)
            except Exception:
                pass
    except Exception as e:
        log.error(f"uTag xatosi: {e}")

async def register_userbot_handlers(client: TelegramClient, uid: str):
    """Userbot uchun guruh event handlerlarini ro'yxatdan o'tkazish."""

    async def is_auto_reply_enabled() -> tuple[bool, str | None]:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT enabled, response_text FROM auto_reply_settings WHERE owner_id = ?",
                (uid,)
            ) as cur:
                row = await cur.fetchone()
        return bool(row and row[0]), row[1] if row else None

    @client.on(events.NewMessage(incoming=True))
    async def on_incoming_message(event):
        """Offline paytda kelgan shaxsiy xabarga sozlangan javobni yuboradi."""
        if not event.is_private or not event.raw_text:
            return
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False) or getattr(sender, "id", None) is None:
            return

        enabled, response_text = await is_auto_reply_enabled()
        if not enabled or not response_text:
            return

        try:
            me = await client.get_me()
            if isinstance(getattr(me, "status", None), UserStatusOnline):
                return
            cooldown_key = (uid, int(sender.id))
            last_sent = _auto_reply_cooldowns.get(cooldown_key)
            now = datetime.now(timezone.utc)
            if last_sent and (now - last_sent).total_seconds() < 900:
                return

            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute(
                    "SELECT pro_until FROM users WHERE id = ?", (uid,)
                ) as cur:
                    row = await cur.fetchone()
            pro = is_pro_user(row[0] if row else None)
            reply_text = build_auto_reply_text(response_text, pro)
            await event.respond(reply_text)
            _auto_reply_cooldowns[cooldown_key] = now
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds)
        except Exception as exc:
            log.error(f"Avto javob xatosi ({uid}): {exc}")

    @client.on(events.NewMessage(pattern=r'^[./](s|su)$', incoming=False, outgoing=True))
    async def on_start_utag(event):
        if event.chat_id is None:
            return
        # Avvalgi taskni bekor qil
        if uid in _utag_tasks and not _utag_tasks[uid].done():
            old_task = _utag_tasks[uid]
            _utag_suppress_ad_tasks.add(old_task)
            old_task.cancel()
            await asyncio.sleep(0.5)
        task = asyncio.create_task(do_utag(client, uid, event, random_mode=False))
        _utag_tasks[uid] = task
        try:
            await event.delete()
        except Exception:
            pass

    @client.on(events.NewMessage(pattern=r'^[./](r|ru|u)$', incoming=False, outgoing=True))
    async def on_start_random_utag(event):
        """.ru yoki /ru — har bir userga random so'z va sticker bilan uTag."""
        if event.chat_id is None:
            return
        if uid in _utag_tasks and not _utag_tasks[uid].done():
            old_task = _utag_tasks[uid]
            _utag_suppress_ad_tasks.add(old_task)
            old_task.cancel()
            await asyncio.sleep(0.5)
        task = asyncio.create_task(do_utag(client, uid, event, random_mode=True))
        _utag_tasks[uid] = task
        try:
            await event.delete()
        except Exception:
            pass

    @client.on(events.NewMessage(pattern=r'^[./](f)$', incoming=False, outgoing=True))
    async def on_stop_utag(event):
        if uid in _utag_tasks and not _utag_tasks[uid].done():
            _utag_tasks[uid].cancel()
        try:
            await event.delete()
        except Exception:
            pass

    @client.on(events.NewMessage(
        pattern=r'^[./](?:mban|ban)\s+(@[A-Za-z0-9_]{5,32})\s*$',
        incoming=False,
        outgoing=True,
    ))
    async def on_raid_ban(event):
        try:
            await event.delete()
        except Exception:
            pass
        if not await user_has_raid_access(uid):
            await event.respond("Raid owner tomonidan berilmagan.")
            return

        raid_client = userbot_clients.get(str(ADMIN_ID))
        if not raid_client:
            await event.respond("Owner profil userboti ulanmagan.")
            return

        lock = _raid_locks.setdefault(uid, asyncio.Lock())
        async with lock:
            delay, batch_size = await get_raid_settings(uid)

            groups = await get_owner_raid_groups()
            if not groups:
                await event.respond("Owner hali raid guruhlarini tanlamagan.")
                return
            username = event.pattern_match.group(1)
            try:
                target = await raid_client.get_entity(username)
                me = await raid_client.get_me()
                if getattr(target, "id", None) == me.id:
                    await event.respond("O'z akkauntingizni ban qilib bo'lmaydi.")
                    return
            except Exception:
                await event.respond(f"{username} topilmadi yoki akkaunt ko'ra olmaydi.")
                return

            banned_groups: list[str] = []
            no_rights: list[str] = []
            for group in groups:
                try:
                    entity = await raid_client.get_entity(int(group["chat_id"]))
                    permissions = await raid_client.get_permissions(entity, me)
                    if not has_ban_rights(permissions):
                        no_rights.append(group["title"])
                        continue
                    await raid_client.edit_permissions(entity, target, view_messages=False)
                    banned_groups.append(group["title"])
                    await asyncio.sleep(delay)
                except FloodWaitError as exc:
                    log.warning("Raid ban FloodWait (%s): %s", group["title"], exc)
                    break
                except Exception as exc:
                    log.warning("Raid ban failed in %s: %s", group["title"], exc)

            if banned_groups:
                group_list = ", ".join(html.escape(name) for name in banned_groups)
                await event.respond(
                    f"✅ {html.escape(username)} ban qilindi ({len(banned_groups)} guruh): {group_list}"
                )
            elif no_rights:
                names = ", ".join(html.escape(name) for name in no_rights)
                await event.respond(f"Hech bir guruhda ban huquqi yo'q: {names}")
            else:
                await event.respond(f"{html.escape(username)} hech qaysi ruxsatli guruhda ban qilinmadi.")

# ─────────────────────────────────────────────
# AVTO XABAR
# ─────────────────────────────────────────────
@dp.callback_query(F.data == "btn_auto_msg")
async def cb_auto_msg_menu(callback: CallbackQuery, state: FSMContext):
    uid = str(callback.from_user.id)
    if uid not in userbot_clients:
        await callback.answer("❗ Avval akkaunt ulang!", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "💠 <b>Avto Xabar</b>\n\nYubormoqchi bo'lgan xabar matnini kiriting:",
        reply_markup=back_kb("main_menu")
    )
    await state.set_state(UserStatesGroup.auto_msg_text)

@dp.message(StateFilter(UserStatesGroup.auto_msg_text), F.text)
async def auto_msg_text_input(message: Message, state: FSMContext):
    await state.update_data(auto_text=message.text)
    await message.answer(
        "📋 Endi foydalanuvchi usernamlarini kiriting (har birini yangi qatordan):\n"
        "Masalan:\n@user1\n@user2",
        reply_markup=back_kb("main_menu")
    )
    await state.set_state(UserStatesGroup.auto_msg_usernames)

@dp.message(StateFilter(UserStatesGroup.auto_msg_usernames), F.text)
async def auto_msg_send(message: Message, state: FSMContext):
    data = await state.get_data()
    text = data.get("auto_text", "")
    uid  = str(message.from_user.id)
    client = userbot_clients.get(uid)
    if not client:
        await message.answer("❌ Akkaunt topilmadi.")
        await state.clear()
        return

    usernames = [username.strip() for username in message.text.splitlines() if username.strip()]
    progress_message = await message.answer(
        f"⏳ {len(usernames)} ta foydalanuvchiga xabar yuborilmoqda..."
    )

    sent = 0
    failed = 0
    not_processed = 0
    peer_flood = False
    for index, username in enumerate(usernames, start=1):
        while True:
            try:
                await client.send_message(username, text, parse_mode=None)
                sent += 1
                await asyncio.sleep(2)
                break
            except FloodWaitError as exc:
                await progress_message.edit_text(
                    f"⏸ Telegram {exc.seconds} soniya kutishni so'radi. "
                    f"{index}/{len(usernames)}-foydalanuvchidan davom etaman."
                )
                await asyncio.sleep(exc.seconds + 1)
            except PeerFloodError as exc:
                failed += 1
                peer_flood = True
                not_processed = len(usernames) - index
                log.warning("Avto xabar Telegram anti-spam limiti sabab to'xtadi: %s", exc)
                break
            except Exception as exc:
                failed += 1
                log.warning("Avto xabar yuborilmadi (%s): %s", username, exc)
                break

        if peer_flood:
            break
        if index % 10 == 0 or index == len(usernames):
            await progress_message.edit_text(
                f"⏳ Jarayon: {index}/{len(usernames)} | yuborildi: {sent} | xato: {failed}"
            )

    await message.answer(
        f"✅ Yuborildi: <b>{sent}</b>\n❌ Muvaffaqiyatsiz: <b>{failed}</b>\n"
        f"⏭ Yuborilmay qoldi: <b>{not_processed}</b>"
        + ("\n⚠️ Telegram anti-spam cheklovi sabab yuborish to'xtatildi." if peer_flood else ""),
        reply_markup=await get_user_main_keyboard(message.from_user.id)
    )
    await state.clear()

# ─────────────────────────────────────────────
# AVTO JAVOB
# ─────────────────────────────────────────────
async def get_auto_reply_settings(uid: str) -> tuple[bool, str | None]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT enabled, response_text FROM auto_reply_settings WHERE owner_id = ?",
            (uid,)
        ) as cur:
            row = await cur.fetchone()
    return bool(row and row[0]), row[1] if row else None


def get_auto_reply_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="✏️ Javob matnini o'zgartirish",
        callback_data="auto_reply_set"
    ))
    if enabled:
        kb.row(InlineKeyboardButton(
            text="⛔ Avto javobni o'chirish",
            callback_data="auto_reply_disable"
        ))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="main_menu"))
    return kb.as_markup()


@dp.callback_query(F.data == "btn_auto_reply")
async def cb_auto_reply_menu(callback: CallbackQuery, state: FSMContext):
    uid = str(callback.from_user.id)
    if uid not in userbot_clients:
        await callback.answer("❗ Avval akkaunt ulang!", show_alert=True)
        return
    await state.clear()
    enabled, response_text = await get_auto_reply_settings(uid)
    status = "🟢 Yoqilgan" if enabled else "🔴 O'chirilgan"
    saved_text = response_text or "Hali javob matni saqlanmagan."
    await callback.message.edit_text(
        "💠 <b>Avto Javob</b>\n\n"
        f"Holati: <b>{status}</b>\n"
        "Akkaunt <b>offline</b> bo'lganda shaxsiy xabarlarga avtomatik javob beradi.\n"
        "Bir odamga 15 daqiqada ko'pi bilan bir marta javob yuboriladi.\n"
        "Oddiy tarifda reklama siz kiritgan matnning tagiga qo'shiladi.\n"
        "PRO tarifda reklama umuman chiqmaydi.\n\n"
        f"<b>Joriy javob:</b>\n{html.escape(saved_text)}",
        reply_markup=get_auto_reply_keyboard(enabled)
    )


@dp.callback_query(F.data == "auto_reply_set")
async def cb_auto_reply_set(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✏️ Foydalanuvchi sizga yozganda, akkauntingiz offline bo'lsa "
        "yuboriladigan javob matnini kiriting.\n\n"
        "Siz yozgan matn javobning asosiy qismi bo'ladi.\n"
        "Oddiy tarifda uning tagiga avtomatik reklama qo'shiladi.\n"
        "PRO tarifda reklama qo'shilmaydi.",
        reply_markup=back_kb("btn_auto_reply")
    )
    await state.set_state(UserStatesGroup.auto_reply_text)


@dp.message(StateFilter(UserStatesGroup.auto_reply_text), F.text)
async def auto_reply_text_input(message: Message, state: FSMContext):
    response_text = message.text.strip()
    if not response_text:
        await message.answer("❌ Javob matni bo'sh bo'lmasin.")
        return
    uid = str(message.from_user.id)
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO auto_reply_settings "
            "(owner_id, enabled, response_text, updated_at) VALUES (?, 1, ?, ?)",
            (uid, response_text, now)
        )
        await db.commit()
    await state.clear()
    await message.answer(
        "✅ Avto javob yoqildi va bazaga saqlandi.\n"
        "Akkauntingiz offline bo'lganda foydalanuvchiga javob yuboriladi.\n"
        "Oddiy tarifda reklama matn tagiga qo'shiladi, PRO tarifda esa reklama chiqmaydi.",
        reply_markup=await get_user_main_keyboard(message.from_user.id)
    )


@dp.callback_query(F.data == "auto_reply_disable")
async def cb_auto_reply_disable(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE auto_reply_settings SET enabled = 0 WHERE owner_id = ?",
            (uid,)
        )
        await db.commit()
    await callback.answer("⛔ Avto javob o'chirildi.", show_alert=True)
    await callback.message.edit_text(
        "💠 <b>Avto Javob</b>\n\n"
        "Holati: <b>🔴 O'chirilgan</b>\n"
        "Avto javobni qayta yoqish uchun javob matnini saqlang.",
        reply_markup=get_auto_reply_keyboard(False)
    )

# ─────────────────────────────────────────────
# USER YIG'ISH (SCRAPE)
# ─────────────────────────────────────────────
def get_scrape_groups_keyboard(uid: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, data in _scrape_group_choices.get(uid, {}).items():
        title = str(data["title"])
        kb.row(InlineKeyboardButton(
            text=f"👥 {title[:42]}",
            callback_data=f"scrape_group:{key}"
        ))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="main_menu"))
    return kb.as_markup()


def get_scrape_count_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    buttons = [
        InlineKeyboardButton(text=f"👥 {count}", callback_data=f"scrape_count:{count}")
        for count in (100, 200, 500, 1000, 1500)
    ]
    kb.row(*buttons[:3])
    kb.row(*buttons[3:])
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="btn_scrape"))
    return kb.as_markup()


def get_scrape_mode_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="⚡ Tez terish", callback_data="scrape_mode:fast"),
        InlineKeyboardButton(text="🐢 Sekin terish", callback_data="scrape_mode:slow"),
    )
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="btn_scrape"))
    return kb.as_markup()


@dp.callback_query(F.data == "btn_scrape")
async def cb_scrape_menu(callback: CallbackQuery, state: FSMContext):
    uid = str(callback.from_user.id)
    client = userbot_clients.get(uid)
    if not client:
        await callback.answer("❗ Avval akkaunt ulang!", show_alert=True)
        return

    await state.clear()
    await callback.message.edit_text("⏳ Akkauntingiz ko'ra oladigan guruhlar olinmoqda...")
    choices: dict[str, dict[str, object]] = {}
    try:
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not dialog.is_group and not (
                dialog.is_channel and getattr(entity, "megagroup", False)
            ):
                continue
            key = str(dialog.id).replace("-", "m")
            choices[key] = {
                "entity": entity,
                "title": dialog.name or str(dialog.id),
            }
            if len(choices) >= 50:
                break
    except Exception as exc:
        log.error(f"Guruhlar ro'yxatini olishda xato ({uid}): {exc}")

    if not choices:
        await callback.message.edit_text(
            "❌ Akkaunt ko'ra oladigan guruh topilmadi.\n\n"
            "Yopiq guruhda akkauntingiz a'zo bo'lishi va xabarlar tarixini "
            "ko'ra olishi kerak.",
            reply_markup=back_kb("main_menu")
        )
        return

    _scrape_group_choices[uid] = choices
    await callback.message.edit_text(
        "💠 <b>User Yig'ish</b>\n\n"
        "Foydalanuvchilarni yig'ish uchun guruhni tugma orqali tanlang.\n"
        "Bot guruhdagi mavjud xabarlar mualliflarini tahlil qiladi.",
        reply_markup=get_scrape_groups_keyboard(uid)
    )


@dp.callback_query(F.data.startswith("scrape_group:"))
async def cb_scrape_group_selected(callback: CallbackQuery, state: FSMContext):
    uid = str(callback.from_user.id)
    key = callback.data.split(":", 1)[1]
    choice = _scrape_group_choices.get(uid, {}).get(key)
    if not choice:
        await callback.answer("❌ Guruh tanlovi eskirgan. Qaytadan oching.", show_alert=True)
        return
    await state.update_data(scrape_group_key=key)
    await callback.message.edit_text(
        f"✅ Tanlandi: <b>{html.escape(str(choice['title']))}</b>\n\n"
        "Terish usulini tanlang:",
        reply_markup=get_scrape_mode_keyboard()
    )
    await state.set_state(UserStatesGroup.scrape_mode)


@dp.callback_query(F.data.startswith("scrape_mode:"))
async def cb_scrape_mode(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id <= 0 or callback.message is None:
        await callback.answer("❌ So'rovni bajarib bo'lmadi.", show_alert=True)
        return
    mode = callback.data.split(":", 1)[1]
    if mode not in {"fast", "slow"}:
        await callback.answer("❌ Noto'g'ri rejim.", show_alert=True)
        return
    await state.update_data(scrape_mode=mode)
    await state.set_state(UserStatesGroup.scrape_count)
    mode_label = "Tez" if mode == "fast" else "Sekin"
    await callback.message.edit_text(
        f"⚙️ Rejim: <b>{mode_label} terish</b>\n\n"
        "Nechta foydalanuvchi yig'ish kerak? 100, 200, 500, 1000 yoki 1500 ni tanlang:",
        reply_markup=get_scrape_count_keyboard()
    )
    await callback.answer()


async def run_scrape_for_count(
    message: Message, state: FSMContext, uid: str, count: int, mode: str
):
    client = userbot_clients.get(uid)
    data = await state.get_data()
    choice = _scrape_group_choices.get(uid, {}).get(data.get("scrape_group_key", ""))
    if not client or not choice:
        await message.answer("❌ Guruh tanlovi topilmadi. Qaytadan urinib ko'ring.")
        await state.clear()
        return

    group = choice["entity"]
    group_title = str(choice["title"])
    message_limit = 1000 if mode == "fast" else None
    mode_label = "tez" if mode == "fast" else "sekin"
    await message.answer(
        f"⏳ <b>{html.escape(group_title)}</b> guruhidagi xabarlar {mode_label} rejimda tahlil qilinmoqda..."
    )

    try:
        me = await client.get_me()
        found: dict[int, tuple[str, str, str]] = {}
        async for item in client.iter_messages(group, limit=message_limit):
            sender = await item.get_sender()
            if not sender or getattr(sender, "bot", False):
                continue
            sender_id = getattr(sender, "id", None)
            if not sender_id or sender_id == me.id or sender_id in found:
                continue
            sender_username = getattr(sender, "username", None)
            if not sender_username:
                continue
            username = f"@{sender_username}"
            fullname = get_display_name(sender).strip()
            found[int(sender_id)] = (username, fullname, str(sender_id))
            if len(found) >= count:
                break

        scraped_at = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_FILE) as db:
            existing_rows = await db.execute_fetchall(
                "SELECT telegram_user_id FROM scraped_users "
                "WHERE owner_id = ? AND group_link = ?",
                (uid, group_title)
            )
            existing_ids = {str(row[0]) for row in existing_rows}
            rows = [
                (uid, group_title, user_id, username, fullname, scraped_at)
                for user_id, (username, fullname, user_id) in found.items()
                if user_id not in existing_ids
            ]
            await db.executemany(
                "INSERT INTO scraped_users "
                "(owner_id, group_link, telegram_user_id, username, fullname, scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows
            )
            await db.commit()

        await message.answer(
            f"✅ Foydalanuvchilar yig'ildi va bazaga saqlandi!\n\n"
            f"🆕 Yangi saqlanganlar: <b>{len(rows)}</b> ta\n"
            f"👥 Jami topilgan: <b>{len(found)}</b> ta\n\n"
            "Ro'yxat shaxsiy chatga 150 tadan bo'lib yuboriladi.",
            reply_markup=await get_user_main_keyboard(uid)
        )
        if found:
            batch: list[str] = []
            batch_chars = 0
            for username, _, _ in found.values():
                extra_chars = len(username) + (1 if batch else 0)
                if batch and (len(batch) >= 150 or batch_chars + extra_chars > 3900):
                    await bot.send_message(chat_id=int(uid), text="\n".join(batch))
                    batch = []
                    batch_chars = 0
                    extra_chars = len(username)
                batch.append(username)
                batch_chars += extra_chars
            if batch:
                await bot.send_message(chat_id=int(uid), text="\n".join(batch))
        else:
            await bot.send_message(
                chat_id=int(uid), text="Username’li foydalanuvchi topilmadi."
            )
    except Exception as exc:
        log.error(f"User yig'ishda xato ({uid}): {exc}")
        await message.answer(
            "❌ Guruh xabarlarini o'qib bo'lmadi. "
            "Akkaunt guruhga a'zo ekanini va xabarlar tarixini ko'ra olishini tekshiring.",
            reply_markup=await get_user_main_keyboard(uid)
        )
    await state.clear()


@dp.callback_query(F.data.startswith("scrape_count:"))
async def cb_scrape_count(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id <= 0 or callback.message is None:
        await callback.answer("❌ So'rovni bajarib bo'lmadi.", show_alert=True)
        return
    try:
        count = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("❌ Noto'g'ri son.", show_alert=True)
        return
    if count not in (100, 200, 500, 1000, 1500):
        await callback.answer("❌ Noto'g'ri son.", show_alert=True)
        return
    data = await state.get_data()
    mode = data.get("scrape_mode")
    if mode not in {"fast", "slow"}:
        await callback.answer("❌ Avval terish usulini tanlang.", show_alert=True)
        return

    await callback.answer(f"{count} ta tanlandi")
    await run_scrape_for_count(
        callback.message, state, str(callback.from_user.id), count, mode
    )


@dp.message(StateFilter(UserStatesGroup.scrape_count), F.text)
async def scrape_count_input(message: Message, state: FSMContext):
    try:
        count = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Tugmalardan birini tanlang yoki 1 dan 1500 gacha son kiriting.")
        return
    if not 1 <= count <= 1500:
        await message.answer("❌ 1 dan 1500 gacha son kiriting.")
        return
    data = await state.get_data()
    mode = data.get("scrape_mode")
    if mode not in {"fast", "slow"}:
        await message.answer("❌ Avval tez yoki sekin terishni tanlang.")
        return
    await run_scrape_for_count(message, state, str(message.from_user.id), count, mode)

# ─────────────────────────────────────────────
# ADMIN PANEL
# ─────────────────────────────────────────────
def get_admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if not is_clone_bot():
        kb.row(InlineKeyboardButton(text="📢 Kanallarni boshqarish", callback_data="admin_channels"))
        kb.row(InlineKeyboardButton(text="🤖 Klonlar va Stars narxi", callback_data="admin_clone_settings"))
    kb.row(InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="admin_users"))
    kb.row(InlineKeyboardButton(text="⭐ PRO berish", callback_data="admin_give_pro"))
    kb.row(InlineKeyboardButton(text="🎉 Konkurs yaratish", callback_data="admin_contest_create"))
    kb.row(InlineKeyboardButton(text="🏆 Aktiv konkurslar", callback_data="admin_contests_list"))
    kb.row(InlineKeyboardButton(text="📣 Xabar tarqatish", callback_data="admin_broadcast"))
    kb.row(InlineKeyboardButton(text="😀 Custom emoji pack", callback_data="admin_custom_emoji"))
    if is_clone_bot():
        kb.row(InlineKeyboardButton(text="🚫 Ban panel", callback_data="ban_panel"))
        kb.row(InlineKeyboardButton(text="🛡 Raid guruhlarini tanlash", callback_data="admin_raid_groups"))
    else:
        kb.row(InlineKeyboardButton(text="🚫 Ban panel", callback_data="ban_panel"))
        kb.row(InlineKeyboardButton(text="🛡 Raid guruhlarini tanlash", callback_data="admin_raid_groups"))
        kb.row(InlineKeyboardButton(text="👤 Raid userlariga ruxsat", callback_data="admin_raid_users"))
    return kb.as_markup()


def normalize_custom_emoji_pack(value: str) -> str | None:
    value = value.strip()
    if value.lower() in {"/off", "off"}:
        return ""
    if value.startswith(("https://", "http://")):
        parsed = urlparse(value)
        parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.netloc.lower() not in {"t.me", "www.t.me", "telegram.me"}
            or len(parts) != 2
            or parts[0] != "addemoji"
        ):
            return None
        value = parts[1]
    elif "/" in value:
        return None
    value = value.strip("/")
    return value if re.fullmatch(r"[A-Za-z0-9_]+", value) else None


def get_admin_channels_keyboard(channels: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for ch in channels:
        kb.row(InlineKeyboardButton(
            text=f"🗑 {ch['username']} ni o'chirish",
            callback_data=f"del_channel:{ch['username']}"
        ))
    kb.row(InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="admin_add_channel"))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_panel"))
    return kb.as_markup()


def get_admin_raid_groups_keyboard(groups: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for group in groups:
        kb.row(InlineKeyboardButton(
            text=f"🗑 {group['title'][:40]}",
            callback_data=f"admin_raid_del:{group['chat_id']}"
        ))
    kb.row(InlineKeyboardButton(
        text="➕ Ban huquqi bor guruh qo'shish",
        callback_data="admin_raid_add_group"
    ))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_panel"))
    return kb.as_markup()


def get_profile_raid_groups_keyboard(profile_id: str, groups: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for group in groups:
        mark = "✅" if group["selected"] else "⬜"
        kb.row(InlineKeyboardButton(
            text=f"{mark} {group['title'][:45]}",
            callback_data=f"raid_profile_toggle:{profile_id}:{group['chat_id']}"
        ))
    kb.row(InlineKeyboardButton(text="⬅️ Profillar", callback_data="admin_raid_groups"))
    return kb.as_markup()


def get_multi_profile_groups_keyboard(
    session_id: str, groups: list[dict]
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for group in groups:
        mark = "✅" if group["selected"] else "⬜"
        kb.row(InlineKeyboardButton(
            text=f"{mark} {group['title'][:45]} ({group['profiles']} ta profil)",
            callback_data=f"raid_multi_toggle:{session_id}:{group['chat_id']}"
        ))
    kb.row(InlineKeyboardButton(text="⬅️ Raid profillari", callback_data="admin_raid_groups"))
    return kb.as_markup()


def get_admin_raid_users_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="➕ Userga raid berish/olish", callback_data="admin_raid_user_set"))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_panel"))
    return kb.as_markup()

@dp.message(Command("admin"), F.chat.type == "private")
async def cmd_admin(message: Message):
    if not is_panel_owner(message.from_user.id):
        return
    await message.answer("🔐 <b>Admin Panel</b>", reply_markup=get_admin_keyboard())


@dp.message(Command("source"), F.chat.type == "private")
async def cmd_source(message: Message):
    """Admin uchun kodni .py fayl va ZIP arxiv sifatida yuboradi."""
    if message.from_user.id != ADMIN_ID:
        return

    source_path = SOURCE_FILE if os.path.isfile(SOURCE_FILE) else __file__
    if not os.path.isfile(source_path):
        await message.answer("❌ Python fayli topilmadi.")
        return

    # SOURCE_FILE yuklangan .txt bo'lsa ham, Telegramga haqiqiy .py nomida yuboramiz.
    output_dir = os.path.dirname(os.path.abspath(source_path)) or "."
    python_path = os.path.join(output_dir, "pro_utaggerbot.py")
    generated_python = os.path.abspath(source_path) != os.path.abspath(python_path)
    archive_path = os.path.join(
        output_dir,
        "pro_utaggerbot_package.zip"
    )
    try:
        if generated_python:
            with open(source_path, "rb") as source_file, open(python_path, "wb") as python_file:
                python_file.write(source_file.read())

        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.write(python_path, arcname="pro_utaggerbot.py")
            for support_file in (
                "requirements.txt",
                "Procfile",
                "start.sh",
                ".env.example",
                "HOSTING.md",
            ):
                support_path = os.path.join(output_dir, support_file)
                if os.path.isfile(support_path):
                    archive.write(support_path, arcname=support_file)

        await message.answer_document(
            types.FSInputFile(python_path, filename="pro_utaggerbot.py"),
            caption="✅ Yangilangan bot kodi (.py)."
        )
        await message.answer_document(
            types.FSInputFile(archive_path, filename="pro_utaggerbot_package.zip"),
            caption="✅ Bot kodi ZIP arxivda."
        )
    except Exception as exc:
        log.error(f"Source fayl yuborishda xato: {exc}")
        await message.answer("❌ Fayllarni yuborishda xatolik yuz berdi.")
    finally:
        try:
            os.remove(archive_path)
        except OSError:
            pass
        if generated_python:
            try:
                os.remove(python_path)
            except OSError:
                pass


@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: CallbackQuery):
    if not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    try:
        await callback.message.edit_text("🔐 <b>Admin Panel</b>", reply_markup=get_admin_keyboard())
    except Exception:
        await callback.answer()


@dp.callback_query(F.data == "admin_clone_settings")
async def cb_admin_clone_settings(callback: CallbackQuery):
    if is_clone_bot() or not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    price = await get_clone_price()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT COUNT(*) FROM cloned_bots WHERE active = 1") as cur:
            clone_count = (await cur.fetchone())[0]
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="💫 Narxni o'zgartirish", callback_data="admin_clone_price"))
    kb.row(InlineKeyboardButton(text="📋 Klonlar ro'yxati", callback_data="admin_clones_list"))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_panel"))
    await callback.message.edit_text(
        f"🤖 <b>Bot klonlarini sotish</b>\n\nNarx: <b>{price} Stars</b>\nFaol klonlar: <b>{clone_count}</b>",
        reply_markup=kb.as_markup()
    )


@dp.callback_query(F.data == "admin_clone_price")
async def cb_admin_clone_price(callback: CallbackQuery, state: FSMContext):
    if is_clone_bot() or not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    await callback.message.edit_text(
        "Klonning yangi narxini Telegram Stars'da yuboring (1 dan 100000 gacha butun son):",
        reply_markup=back_kb("admin_clone_settings")
    )
    await state.set_state(UserStatesGroup.admin_clone_price)


@dp.message(StateFilter(UserStatesGroup.admin_clone_price), F.text)
async def admin_clone_price_input(message: Message, state: FSMContext):
    if is_clone_bot() or not is_panel_owner(message.from_user.id):
        return
    try:
        price = int(message.text.strip())
    except ValueError:
        await message.answer("1 dan 100000 gacha butun Stars sonini kiriting.")
        return
    if not 1 <= price <= 100000:
        await message.answer("Narx 1 dan 100000 Stars oralig'ida bo'lishi kerak.")
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO bot_settings (key, value) VALUES ('clone_price_stars', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(price),)
        )
        await db.commit()
    await state.clear()
    await message.answer(
        f"Narx belgilandi: <b>{price} Stars</b>.",
        reply_markup=get_admin_keyboard()
    )


@dp.callback_query(F.data == "admin_clones_list")
async def cb_admin_clones_list(callback: CallbackQuery):
    if is_clone_bot() or not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT username, owner_id, created_at FROM cloned_bots WHERE active = 1 ORDER BY created_at DESC LIMIT 50"
        ) as cur:
            rows = await cur.fetchall()
    text = "📋 <b>Ulangan klonlar</b>\n\n" + (
        "\n".join(f"@{html.escape(name)} | egasi <code>{owner}</code>" for name, owner, _ in rows)
        if rows else "Hozircha faol klonlar yo'q."
    )
    await callback.message.edit_text(text, reply_markup=back_kb("admin_clone_settings"))


@dp.callback_query(F.data == "admin_custom_emoji")
async def cb_admin_custom_emoji(callback: CallbackQuery):
    if not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    current = get_custom_emoji_pack_name() or "Sozlanmagan"
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="➕ Pack qo'shish / almashtirish",
        callback_data="admin_custom_emoji_set"
    ))
    kb.row(InlineKeyboardButton(
        text="🎨 Tugmalar emoji'larini sozlash",
        callback_data="admin_button_emojis"
    ))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_panel"))
    await callback.message.edit_text(
        "😀 <b>Custom emoji pack</b>\n\n"
        f"Joriy pack: <code>{html.escape(current)}</code>\n"
        "Pack havolasi yoki short name yuboring. O'chirish uchun /off yozing.",
        reply_markup=kb.as_markup()
    )


@dp.callback_query(F.data == "admin_custom_emoji_set")
async def cb_admin_custom_emoji_set(callback: CallbackQuery, state: FSMContext):
    if not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    await callback.message.edit_text(
        "Custom emoji pack havolasini (https://t.me/addemoji/...) yoki short name'ni yuboring.\n"
        "Packni o'chirish uchun /off yozing.",
        reply_markup=back_kb("admin_custom_emoji")
    )
    await state.set_state(UserStatesGroup.admin_custom_emoji_pack)


@dp.message(StateFilter(UserStatesGroup.admin_custom_emoji_pack), F.text)
async def admin_custom_emoji_pack_input(message: Message, state: FSMContext):
    global CUSTOM_EMOJI_PACK
    if not is_panel_owner(message.from_user.id):
        return
    pack_name = normalize_custom_emoji_pack(message.text)
    if pack_name is None:
        await message.answer(
            "❌ Pack havolasi noto'g'ri. https://t.me/addemoji/SHORT_NAME yoki short name yuboring."
        )
        return

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO bot_settings (key, value) VALUES ('custom_emoji_pack', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (pack_name,)
        )
        await db.commit()
    async with _custom_emoji_pack_lock:
        CUSTOM_EMOJI_PACK = pack_name
        _custom_emoji_pack_cache.clear()

    await state.clear()
    result = "o'chirildi" if not pack_name else f"<code>{html.escape(pack_name)}</code> saqlandi"
    await message.answer(
        f"✅ Custom emoji pack {result}.",
        reply_markup=get_admin_keyboard()
    )


@dp.callback_query(F.data == "admin_raid_groups")
async def cb_admin_raid_groups(callback: CallbackQuery):
    if not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    async with aiosqlite.connect(DB_FILE) as db:
        query = (
            "SELECT DISTINCT u.id, u.fullname, u.username "
            "FROM users u JOIN raid_profile_groups r ON r.profile_user_id = u.id "
            "WHERE r.selected = 1"
        )
        params = ()
        if is_clone_bot():
            query += " AND u.id = ?"
            params = (str(current_panel_owner_id()),)
        query += " ORDER BY u.first_seen DESC"
        async with db.execute(query, params) as cur:
            profiles = await cur.fetchall()
    listing = "\n".join(
        f"• {html.escape(fullname or username or user_id)} "
        f"(<code>{user_id}</code>)" for user_id, fullname, username in profiles
    ) or "Hali profil uchun guruh tanlanmagan."
    await callback.message.edit_text(
        "🛡 <b>Raid profillari va guruhlari</b>\n\n"
        f"{listing}\n\n"
        "User ID yoki username yuboring. Bot o‘sha profilning admin va ban huquqi bor guruhlarini chiqaradi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➕ Profil guruhlarini topish", callback_data="admin_raid_add_group")
        ], [
            InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_panel")
        ]])
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_raid_add_group")
async def cb_admin_raid_add_group(callback: CallbackQuery, state: FSMContext):
    if not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    await callback.message.edit_text(
        "Raid uchun profil user ID yoki username yuboring.\n"
        + ("Clone botda faqat o'z profilingizni kiriting." if is_clone_bot() else
           "Masalan: <code>123456789</code> yoki <code>@username</code>"),
        reply_markup=back_kb("admin_raid_groups")
    )
    await state.set_state(UserStatesGroup.admin_raid_profile)


@dp.message(StateFilter(UserStatesGroup.admin_raid_profile), F.text)
async def admin_raid_add_group_input(message: Message, state: FSMContext):
    if not is_panel_owner(message.from_user.id):
        return
    raw_profiles = [
        item.strip().lstrip("@").strip()
        for item in re.split(r"[\s,;]+", message.text.strip())
        if item.strip()
    ]
    if is_clone_bot() and raw_profiles != [str(current_panel_owner_id())]:
        await message.answer("Clone botda faqat o'z Telegram ID'ingizni yuboring.")
        return
    if not 1 <= len(raw_profiles) <= 5:
        await message.answer("Bir martada 1 dan 5 tagacha user ID yoki username yuboring.")
        return

    profiles = []
    async with aiosqlite.connect(DB_FILE) as db:
        for raw_profile in raw_profiles:
            if raw_profile.isdigit():
                async with db.execute(
                    "SELECT id, fullname, username FROM users WHERE id = ?", (raw_profile,)
                ) as cur:
                    profile = await cur.fetchone()
            else:
                async with db.execute(
                    "SELECT id, fullname, username FROM users WHERE lower(username) = lower(?)",
                    (raw_profile,)
                ) as cur:
                    profile = await cur.fetchone()
            if not profile:
                await message.answer(f"{raw_profile}: user botdan foydalanmagan yoki topilmadi.")
                return
            profile_id, fullname, username = profile
            if str(profile_id) not in userbot_clients:
                await message.answer(f"{raw_profile}: userbot akkaunti ulanmagan.")
                return
            profiles.append(profile)

    discovered_by_group: dict[str, dict[str, object]] = {}
    for profile_id, fullname, username in profiles:
        client = userbot_clients[str(profile_id)]
        try:
            if not client.is_connected():
                await client.connect()
            me = await client.get_me()
            dialogs = []
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                is_group = bool(getattr(dialog, "is_group", False)) or isinstance(entity, Chat) or bool(getattr(entity, "megagroup", False))
                if is_group:
                    dialogs.append(entity)
            permission_limit = asyncio.Semaphore(20)

            async def inspect_group(entity):
                async with permission_limit:
                    try:
                        permissions = await client.get_permissions(entity, me)
                        if not has_raid_group_rights(permissions):
                            return None
                        chat_id = str(get_peer_id(entity))
                        return chat_id, get_display_name(entity) or chat_id
                    except Exception:
                        return None

            checked_groups = await asyncio.gather(
                *(inspect_group(entity) for entity in dialogs)
            )
            for item in (group for group in checked_groups if group):
                chat_id, title = item
                record = discovered_by_group.setdefault(
                    chat_id, {"chat_id": chat_id, "title": title, "profiles": [], "selected": False}
                )
                record["profiles"].append(str(profile_id))
        except Exception as exc:
            log.warning("Profil guruhlarini topishda xato (%s): %s", profile_id, exc)
            continue

    if not discovered_by_group:
        await message.answer("Berilgan profillarda ban huquqi bor guruhlar topilmadi.")
        return
    session_id = os.urandom(5).hex()
    profile_ids = [str(profile[0]) for profile in profiles]
    _raid_profile_selection_sessions[session_id] = profile_ids
    discovered = list(discovered_by_group.values())
    async with aiosqlite.connect(DB_FILE) as db:
        for group in discovered:
            chat_id, title = group["chat_id"], group["title"]
            for profile_id in group["profiles"]:
                await db.execute(
                    "INSERT INTO raid_profile_groups "
                    "(profile_user_id, chat_id, title, has_ban_rights, selected, updated_at) "
                    "VALUES (?, ?, ?, 1, 0, ?) "
                    "ON CONFLICT(profile_user_id, chat_id) DO UPDATE SET title = excluded.title, "
                    "has_ban_rights = 1, updated_at = excluded.updated_at",
                    (profile_id, chat_id, title, datetime.now(timezone.utc).isoformat())
                )
        await db.commit()
    await state.clear()
    await message.answer(
        f"✅ {len(profiles)} ta profil uchun {len(discovered)} ta guruh topildi. "
        "Bir guruh tanlansa, shu guruhda huquqi bor barcha profillar parallel ishlaydi.",
        reply_markup=get_multi_profile_groups_keyboard(session_id, discovered)
    )


@dp.callback_query(F.data.startswith("raid_multi_toggle:"))
async def cb_raid_multi_toggle(callback: CallbackQuery):
    if not is_panel_owner(callback.from_user.id):
        await callback.answer("Raid sozlamasi faqat owner uchun.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Tanlov noto'g'ri.", show_alert=True)
        return
    session_id, chat_id = parts[1], parts[2]
    profile_ids = _raid_profile_selection_sessions.get(session_id)
    if not profile_ids:
        await callback.answer("Raid tanlov sessiyasi eskirgan.", show_alert=True)
        return
    if is_clone_bot() and profile_ids != [str(current_panel_owner_id())]:
        await callback.answer("Clone faqat o'z profilingizni boshqara oladi.", show_alert=True)
        return
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM raid_profile_groups WHERE profile_user_id IN "
            f"({','.join('?' for _ in profile_ids)}) AND chat_id = ? AND selected = 1",
            (*profile_ids, chat_id)
        ) as cur:
            selected_count = (await cur.fetchone())[0]
        all_count = len(profile_ids)
        new_selected = 0 if selected_count == all_count else 1
        for profile_id in profile_ids:
            await db.execute(
                "UPDATE raid_profile_groups SET selected = ?, updated_at = ? "
                "WHERE profile_user_id = ? AND chat_id = ? AND has_ban_rights = 1",
                (new_selected, datetime.now(timezone.utc).isoformat(), profile_id, chat_id)
            )
            if not is_clone_bot():
                await db.execute(
                    "UPDATE users SET raid_enabled = EXISTS (SELECT 1 FROM raid_profile_groups "
                    "WHERE profile_user_id = users.id AND selected = 1 AND has_ban_rights = 1) "
                    "WHERE id = ?", (profile_id,)
                )
        await db.commit()
        async with db.execute(
            "SELECT title, selected FROM raid_profile_groups WHERE chat_id = ? "
            "AND has_ban_rights = 1 LIMIT 1", (chat_id,)
        ) as cur:
            group_row = await cur.fetchone()
    if not group_row:
        await callback.answer("Guruh topilmadi.", show_alert=True)
        return
    active_profiles = [
        profile_id for profile_id in profile_ids
        if (profile_id in profile_ids and selected_count >= 0)
    ]
    count_client = userbot_clients.get(active_profiles[0])
    member_count = 0
    if count_client:
        try:
            members = await count_client.get_participants(await count_client.get_entity(int(chat_id)))
            member_count = len(members)
        except Exception:
            pass
    await callback.message.edit_text(
        f"🛡 <b>{html.escape(group_row[0])}</b>\n\n"
        f"A'zolar soni: <b>{member_count}</b> ta\n"
        f"Tanlangan profillar: <b>{len(active_profiles)} ta</b>\n"
        "Ban qilish vaqtini tanlang:",
        reply_markup=get_multi_raid_duration_keyboard(session_id, chat_id)
    )
    await callback.answer("Guruh tanlandi.")


def get_multi_raid_duration_keyboard(session_id: str, chat_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for minutes in (1, 5, 10):
        kb.row(InlineKeyboardButton(
            text=f"⏱ {minutes} daqiqada ban qilish",
            callback_data=f"raid_multi_duration:{session_id}:{chat_id}:{minutes}"
        ))
    kb.row(InlineKeyboardButton(text="⬅️ Profillar", callback_data="admin_raid_groups"))
    return kb.as_markup()


@dp.callback_query(F.data.startswith("raid_multi_duration:"))
async def cb_raid_multi_duration(callback: CallbackQuery):
    if not is_panel_owner(callback.from_user.id):
        await callback.answer("Raid faqat owner uchun.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Raid tanlovi noto'g'ri.", show_alert=True)
        return
    _, session_id, chat_id, minutes_text = parts
    profile_ids = _raid_profile_selection_sessions.get(session_id, [])
    if is_clone_bot() and profile_ids != [str(current_panel_owner_id())]:
        await callback.answer("Clone faqat o'z profilingizni boshqara oladi.", show_alert=True)
        return
    try:
        minutes = int(minutes_text)
    except ValueError:
        await callback.answer("Vaqt noto'g'ri.", show_alert=True)
        return
    if minutes not in {1, 5, 10} or not profile_ids:
        await callback.answer("Raid vaqti yoki profillar noto'g'ri.", show_alert=True)
        return
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT profile_user_id FROM raid_profile_groups WHERE chat_id = ? "
            "AND selected = 1 AND has_ban_rights = 1 AND profile_user_id IN "
            f"({','.join('?' for _ in profile_ids)})",
            (chat_id, *profile_ids)
        ) as cur:
            selected_profiles = {row[0] for row in await cur.fetchall()}
    tasks = []
    for profile_id in profile_ids:
        if profile_id not in selected_profiles:
            continue
        client = userbot_clients.get(profile_id)
        if not client:
            continue
        task_key = (profile_id, chat_id)
        old_task = _raid_mass_tasks.get(task_key)
        if old_task and not old_task.done():
            continue
        task = asyncio.create_task(
            run_mass_raid(client, profile_id, chat_id, minutes, callback.message.chat.id)
        )
        _raid_mass_tasks[task_key] = task
        tasks.append(task)
    if not tasks:
        await callback.answer("Ulangan va ruxsatli userbot topilmadi.", show_alert=True)
        return
    await callback.message.edit_text(
        f"🛡 <b>{len(tasks)} ta profil bilan parallel raid boshlandi</b>\n"
        f"Vaqt rejasi: <b>{minutes} daqiqa</b>",
        reply_markup=get_multi_raid_stop_keyboard(session_id, chat_id)
    )
    await callback.answer("Parallel raid boshlandi.")


def get_multi_raid_stop_keyboard(session_id: str, chat_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="⏹ Barcha raidlarni to'xtatish",
        callback_data=f"raid_multi_stop:{session_id}:{chat_id}"
    ))
    return kb.as_markup()


@dp.callback_query(F.data.startswith("raid_multi_stop:"))
async def cb_raid_multi_stop(callback: CallbackQuery):
    if not is_panel_owner(callback.from_user.id):
        await callback.answer("Raidni faqat owner to'xtata oladi.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    profile_ids = _raid_profile_selection_sessions.get(parts[1], []) if len(parts) == 3 else []
    if is_clone_bot() and profile_ids != [str(current_panel_owner_id())]:
        await callback.answer("Clone faqat o'z raidini to'xtata oladi.", show_alert=True)
        return
    chat_id = parts[2] if len(parts) == 3 else ""
    stopped = 0
    for profile_id in profile_ids:
        task = _raid_mass_tasks.get((profile_id, chat_id))
        if task and not task.done():
            task.cancel()
            stopped += 1
    await callback.message.edit_text(f"⏹ {stopped} ta parallel raid to'xtatildi.")
    await callback.answer("Raidlar to‘xtatildi.")


@dp.callback_query(F.data.startswith("raid_profile_toggle:"))
async def cb_raid_profile_toggle(callback: CallbackQuery):
    if not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Tanlov noto'g'ri.", show_alert=True)
        return
    profile_id, chat_id = parts[1], parts[2]
    if is_clone_bot() and profile_id != str(current_panel_owner_id()):
        await callback.answer("Clone faqat o'z profilingizni boshqara oladi.", show_alert=True)
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE raid_profile_groups SET selected = CASE selected WHEN 1 THEN 0 ELSE 1 END, "
            "updated_at = ? WHERE profile_user_id = ? AND chat_id = ?",
            (datetime.now(timezone.utc).isoformat(), profile_id, chat_id)
        )
        if not is_clone_bot():
            await db.execute(
                "UPDATE users SET raid_enabled = EXISTS ("
                "SELECT 1 FROM raid_profile_groups WHERE profile_user_id = users.id "
                "AND selected = 1 AND has_ban_rights = 1) WHERE id = ?",
                (profile_id,)
            )
        await db.commit()
        async with db.execute(
            "SELECT chat_id, title, selected FROM raid_profile_groups "
            "WHERE profile_user_id = ? AND has_ban_rights = 1 ORDER BY title COLLATE NOCASE",
            (profile_id,)
        ) as cur:
            groups = [
                {"chat_id": row[0], "title": row[1], "selected": bool(row[2])}
                for row in await cur.fetchall()
            ]
    selected_group = next((group for group in groups if group["chat_id"] == chat_id), None)
    if not selected_group or not selected_group["selected"]:
        await callback.message.edit_reply_markup(
            reply_markup=get_profile_raid_groups_keyboard(profile_id, groups)
        )
        await callback.answer("Guruh tanlovi olib tashlandi.")
        return

    client = userbot_clients.get(profile_id)
    if not client:
        await callback.answer("Bu profil userboti ulanmagan.", show_alert=True)
        return
    try:
        entity = await client.get_entity(int(chat_id))
        members = await client.get_participants(entity)
        member_count = len(members)
    except Exception as exc:
        log.warning("Raid guruh a'zolari soni olinmadi: %s", exc)
        await callback.answer("Guruh a'zolari sonini olib bo'lmadi.", show_alert=True)
        return

    await callback.message.edit_text(
        f"🛡 <b>{html.escape(selected_group['title'])}</b>\n\n"
        f"A'zolar soni: <b>{member_count}</b> ta\n"
        "Ban qilish vaqtini tanlang:",
        reply_markup=get_raid_duration_keyboard(profile_id, chat_id, member_count)
    )
    await callback.answer("Guruh tanlandi.")


def get_raid_duration_keyboard(
    profile_id: str, chat_id: str, member_count: int
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for minutes in (1, 5, 10):
        kb.row(InlineKeyboardButton(
            text=f"⏱ {minutes} daqiqada ban qilish",
            callback_data=f"raid_duration:{profile_id}:{chat_id}:{minutes}"
        ))
    kb.row(InlineKeyboardButton(
        text="⬅️ Guruhlar ro'yxati",
        callback_data="admin_raid_groups"
    ))
    return kb.as_markup()


async def run_mass_raid(
    client: TelegramClient,
    profile_id: str,
    chat_id: str,
    minutes: int,
    report_chat_id: int,
):
    task_key = (profile_id, chat_id)
    banned = 0
    skipped = 0
    failed = 0
    failure_reasons: dict[str, int] = {}
    try:
        if not client.is_connected():
            await client.connect()
        entity = await client.get_entity(int(chat_id))
        me = await client.get_me()
        participants = await client.get_participants(entity)
        raid_started_at = datetime.now(timezone.utc)
        admins = await client.get_participants(
            entity, filter=ChannelParticipantsAdmins()
        )
        admin_ids = {int(member.id) for member in admins}
        admin_ids.add(int(me.id))
        skipped = sum(1 for member in participants if int(member.id) in admin_ids)
        targets = [member for member in participants if int(member.id) not in admin_ids]

        if not targets:
            await bot.send_message(report_chat_id, "Raid uchun ban qilinadigan a'zo topilmadi.")
            return

        concurrency = min(5, max(1, len(targets) // 20))
        progress_message = await bot.send_message(
            report_chat_id,
            f"🛡 Raid boshlandi\n"
            f"Ban: <b>0/{len(targets)}</b>\n"
            f"Xato: <b>0</b>\n"
            f"Workerlar: <b>{concurrency}</b>\n"
            f"Reja vaqti: <b>{minutes} daqiqa</b>",
            reply_markup=get_raid_progress_keyboard(profile_id, chat_id)
        )

        counter_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(concurrency)

        async def ban_member(member):
            nonlocal banned, failed
            async with semaphore:
                for attempt in range(3):
                    try:
                        await client.edit_permissions(entity, member, view_messages=False)
                        async with counter_lock:
                            banned += 1
                        return
                    except FloodWaitError as exc:
                        if attempt == 2:
                            async with counter_lock:
                                failed += 1
                                failure_reasons["FloodWaitError"] = (
                                    failure_reasons.get("FloodWaitError", 0) + 1
                                )
                            log.warning(
                                "Raid ban FloodWait retries exhausted (%s, %s, member=%s): %s",
                                profile_id, chat_id, getattr(member, "id", "?"), exc
                            )
                            return
                        await asyncio.sleep(exc.seconds + 1)
                    except Exception as exc:
                        if attempt == 2:
                            async with counter_lock:
                                failed += 1
                                error_name = type(exc).__name__
                                failure_reasons[error_name] = (
                                    failure_reasons.get(error_name, 0) + 1
                                )
                            log.warning(
                                "Mass raid failed (%s, %s, member=%s): %s",
                                profile_id, chat_id, getattr(member, "id", "?"), exc
                            )
                        else:
                            await asyncio.sleep(0.2)

        for start in range(0, len(targets), concurrency):
            batch = targets[start:start + concurrency]
            await asyncio.gather(*(ban_member(member) for member in batch))
            batch_number = start // concurrency + 1
            if batch_number % 4 != 0 and start + len(batch) < len(targets):
                continue
            try:
                await progress_message.edit_text(
                    f"🛡 Raid davom etmoqda\n"
                    f"Ban: <b>{banned}/{len(targets)}</b>\n"
                    f"Xato: <b>{failed}</b>\n"
                    f"Qolgan: <b>{max(0, len(targets) - banned - failed)}</b>\n"
                    f"Workerlar: <b>{concurrency}</b>",
                    reply_markup=get_raid_progress_keyboard(profile_id, chat_id)
                )
            except Exception:
                pass
        try:
            failure_detail = ", ".join(
                f"{name}: {count}" for name, count in sorted(failure_reasons.items())
            )
            failure_summary = f" ({html.escape(failure_detail)})" if failure_detail else ""
            await progress_message.edit_text(
                f"✅ Raid tugadi\n"
                f"Ban: <b>{banned}</b>\n"
                f"Admin/owner: <b>{skipped}</b>\n"
                f"Xato: <b>{failed}</b>{failure_summary}",
                reply_markup=None
            )
        except Exception:
            await bot.send_message(
                report_chat_id,
                f"✅ Raid tugadi. Ban: <b>{banned}</b>, admin/owner: <b>{skipped}</b>, "
                f"xato: <b>{failed}</b>.",
            )
    except asyncio.CancelledError:
        await bot.send_message(report_chat_id, f"⏹ Raid to'xtatildi. Ban qilingan: {banned} ta.")
        raise
    except Exception as exc:
        log.error("Mass raid umumiy xatosi (%s, %s): %s", profile_id, chat_id, exc)
        await bot.send_message(report_chat_id, "❌ Raid bajarilmadi. Userbot va guruh huquqlarini tekshiring.")
    finally:
        _raid_mass_tasks.pop(task_key, None)


def get_raid_progress_keyboard(profile_id: str, chat_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="⏹ Raidni to'xtatish",
        callback_data=f"raid_stop:{profile_id}:{chat_id}"
    ))
    return kb.as_markup()


@dp.callback_query(F.data.startswith("raid_stop:"))
async def cb_raid_stop(callback: CallbackQuery):
    if is_clone_bot() or callback.from_user.id != ADMIN_ID:
        await callback.answer("Raidni faqat owner to'xtata oladi.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Raid tanlovi noto'g'ri.", show_alert=True)
        return
    task = _raid_mass_tasks.get((parts[1], parts[2]))
    if not task or task.done():
        await callback.answer("Bu raid allaqachon tugagan.", show_alert=True)
        return
    task.cancel()
    await callback.answer("Raid to'xtatilmoqda.")


@dp.callback_query(F.data.startswith("raid_duration:"))
async def cb_raid_duration(callback: CallbackQuery):
    if is_clone_bot() or callback.from_user.id != ADMIN_ID:
        await callback.answer("Raid faqat owner uchun.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Raid tanlovi noto'g'ri.", show_alert=True)
        return
    _, profile_id, chat_id, minutes_text = parts
    try:
        minutes = int(minutes_text)
    except ValueError:
        await callback.answer("Vaqt noto'g'ri.", show_alert=True)
        return
    if minutes not in {1, 5, 10}:
        await callback.answer("Bu vaqt mavjud emas.", show_alert=True)
        return
    if not await user_has_raid_access(profile_id):
        await callback.answer("Profilga raid ruxsati berilmagan.", show_alert=True)
        return
    client = userbot_clients.get(profile_id)
    if not client:
        await callback.answer("Profil userboti ulanmagan.", show_alert=True)
        return
    task_key = (profile_id, chat_id)
    old_task = _raid_mass_tasks.get(task_key)
    if old_task and not old_task.done():
        await callback.answer("Bu guruhda raid allaqachon ishlayapti.", show_alert=True)
        return
    await callback.answer(f"Raid boshlandi: {minutes} daqiqa.")
    await callback.message.edit_text(
        "🛡 <b>Raid boshlandi</b>\n\n"
        f"Profil: <code>{profile_id}</code>\n"
        f"Reja: <b>{minutes} daqiqa</b>\n"
        "Adminlar va profil egasi ban qilinmaydi."
    )
    task = asyncio.create_task(
        run_mass_raid(client, profile_id, chat_id, minutes, callback.message.chat.id)
    )
    _raid_mass_tasks[task_key] = task


@dp.callback_query(F.data.startswith("admin_raid_del:"))
async def cb_admin_raid_del(callback: CallbackQuery):
    if is_clone_bot() or not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    chat_id = callback.data.split(":", 1)[1]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM raid_groups WHERE chat_id = ?", (chat_id,))
        await db.commit()
    groups = await get_raid_groups()
    listing = "\n".join(
        f"• {html.escape(group['title'])}" for group in groups
    ) or "Hali guruh tanlanmagan."
    await callback.message.edit_text(
        f"🛡 <b>Raid uchun ruxsatli guruhlar</b>\n\n{listing}",
        reply_markup=get_admin_raid_groups_keyboard(groups)
    )
    await callback.answer("✅ Guruh o'chirildi.")


@dp.callback_query(F.data == "admin_raid_users")
async def cb_admin_raid_users(callback: CallbackQuery):
    if is_clone_bot() or not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT id, fullname, username FROM users WHERE raid_enabled = 1 "
            "ORDER BY first_seen DESC"
        ) as cur:
            rows = await cur.fetchall()
    listing = "\n".join(
        f"• {html.escape(fullname or username or user_id)} "
        f"(<code>{user_id}</code>)" for user_id, fullname, username in rows
    ) or "Hozircha hech kimga raid ruxsati berilmagan."
    await callback.message.edit_text(
        "👤 <b>Raid userlari</b>\n\n"
        f"{listing}\n\n"
        "Ruxsat berish uchun user ID yozing. Olish uchun: <code>ID off</code>",
        reply_markup=get_admin_raid_users_keyboard()
    )


@dp.callback_query(F.data == "admin_raid_user_set")
async def cb_admin_raid_user_set(callback: CallbackQuery, state: FSMContext):
    if is_clone_bot() or not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    await callback.message.edit_text(
        "Raid ruxsati uchun user ID yuboring:\n"
        "Berish: <code>123456789</code>\n"
        "Olish: <code>123456789 off</code>",
        reply_markup=back_kb("admin_raid_users")
    )
    await state.set_state(UserStatesGroup.admin_raid_user)


@dp.message(StateFilter(UserStatesGroup.admin_raid_user), F.text)
async def admin_raid_user_input(message: Message, state: FSMContext):
    if is_clone_bot() or not is_panel_owner(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) not in {1, 2} or not parts[0].isdigit() or (
        len(parts) == 2 and parts[1].lower() != "off"
    ):
        await message.answer("Format: <code>USER_ID</code> yoki <code>USER_ID off</code>")
        return
    target_id = parts[0]
    enabled = 0 if len(parts) == 2 else 1
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT id FROM users WHERE id = ?", (target_id,)) as cur:
            exists = await cur.fetchone()
        if not exists:
            await message.answer("Bu user botdan hali foydalanmagan.")
            return
        await db.execute(
            "UPDATE users SET raid_enabled = ? WHERE id = ?",
            (enabled, target_id)
        )
        await db.commit()
    await state.clear()
    status = "berildi" if enabled else "olindi"
    try:
        await bot.send_message(
            int(target_id),
            f"🛡 Raid ruxsati {status}." if enabled else "🛡 Raid ruxsati bekor qilindi.",
            reply_markup=await get_user_main_keyboard(target_id)
        )
    except Exception:
        pass
    await message.answer(
        f"✅ <code>{target_id}</code> useridan raid ruxsati {status}.",
        reply_markup=get_admin_keyboard()
    )


BUTTON_EMOJI_PAGE_SIZE = 20


def get_button_emoji_targets_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    global _button_emoji_target_keys
    items = sorted(
        _button_catalog.items(),
        key=lambda item: (item[1].casefold(), item[0])
    )
    _button_emoji_target_keys = [key for key, _ in items]
    page_count = max(1, (len(items) + BUTTON_EMOJI_PAGE_SIZE - 1) // BUTTON_EMOJI_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    start = page * BUTTON_EMOJI_PAGE_SIZE
    kb = InlineKeyboardBuilder()
    for index, (key, label) in enumerate(
        items[start:start + BUTTON_EMOJI_PAGE_SIZE], start
    ):
        kb.row(InlineKeyboardButton(
            text=label[:55],
            callback_data=f"button_emoji_target:{index}",
            icon_custom_emoji_id=_button_custom_emoji_ids.get(key)
        ))
    if page_count > 1:
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton(
                text="⬅️", callback_data=f"button_emoji_targets_page:{page - 1}"
            ))
        navigation.append(InlineKeyboardButton(
            text=f"{page + 1}/{page_count}", callback_data="button_emoji_page_noop"
        ))
        if page + 1 < page_count:
            navigation.append(InlineKeyboardButton(
                text="➡️", callback_data=f"button_emoji_targets_page:{page + 1}"
            ))
        kb.row(*navigation)
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_custom_emoji"))
    return kb.as_markup()


def get_button_emoji_pack_keyboard(page: int, target_page: int) -> InlineKeyboardMarkup:
    page_count = max(
        1, (len(_custom_emoji_pack_cache) + BUTTON_EMOJI_PAGE_SIZE - 1)
        // BUTTON_EMOJI_PAGE_SIZE
    )
    page = max(0, min(page, page_count - 1))
    start = page * BUTTON_EMOJI_PAGE_SIZE
    kb = InlineKeyboardBuilder()
    buttons = [
        InlineKeyboardButton(
            text=alt or "⭐",
            callback_data=f"button_emoji_pick:{index}",
            icon_custom_emoji_id=str(document_id)
        )
        for index, (document_id, alt) in enumerate(
            _custom_emoji_pack_cache[start:start + BUTTON_EMOJI_PAGE_SIZE], start
        )
    ]
    for offset in range(0, len(buttons), 4):
        kb.row(*buttons[offset:offset + 4])
    if page_count > 1:
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton(
                text="⬅️", callback_data=f"button_emoji_pack_page:{page - 1}"
            ))
        navigation.append(InlineKeyboardButton(
            text=f"{page + 1}/{page_count}", callback_data="button_emoji_page_noop"
        ))
        if page + 1 < page_count:
            navigation.append(InlineKeyboardButton(
                text="➡️", callback_data=f"button_emoji_pack_page:{page + 1}"
            ))
        kb.row(*navigation)
    kb.row(InlineKeyboardButton(
        text="🗑 Emoji'ni olib tashlash", callback_data="button_emoji_clear"
    ))
    kb.row(InlineKeyboardButton(
        text="⬅️ Tugmalar", callback_data=f"button_emoji_targets_page:{target_page}"
    ))
    return kb.as_markup()


async def show_button_emoji_targets(callback: CallbackQuery, page: int = 0):
    if not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    if not _button_catalog:
        await callback.answer("Tugmalar ro'yxati hali bo'sh.", show_alert=True)
        return
    await callback.message.edit_text(
        "🎨 <b>Custom emoji qo'yiladigan tugmani tanlang</b>",
        reply_markup=get_button_emoji_targets_keyboard(page)
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_button_emojis")
async def cb_admin_button_emojis(callback: CallbackQuery):
    await show_button_emoji_targets(callback)


@dp.callback_query(F.data == "button_emoji_page_noop")
async def cb_button_emoji_page_noop(callback: CallbackQuery):
    if is_panel_owner(callback.from_user.id):
        await callback.answer()


@dp.callback_query(F.data.startswith("button_emoji_targets_page:"))
async def cb_button_emoji_targets_page(callback: CallbackQuery):
    try:
        page = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("❌ Sahifa noto'g'ri.", show_alert=True)
        return
    await show_button_emoji_targets(callback, page)


@dp.callback_query(F.data.startswith("button_emoji_target:"))
async def cb_button_emoji_target(callback: CallbackQuery, state: FSMContext):
    if not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    try:
        index = int(callback.data.rsplit(":", 1)[1])
        target_key = _button_emoji_target_keys[index]
    except (ValueError, IndexError):
        await callback.answer("❌ Tugma ro'yxati eskirgan. Qaytadan oching.", show_alert=True)
        return
    client = userbot_clients.get(str(current_panel_owner_id()))
    if not client:
        await callback.answer("Avval admin akkauntini botga ulang.", show_alert=True)
        return
    emojis = await get_custom_emoji_pack(client)
    if not emojis:
        await callback.answer("Custom emoji pack yuklanmadi yoki bo'sh.", show_alert=True)
        return

    target_page = index // BUTTON_EMOJI_PAGE_SIZE
    await state.update_data(button_emoji_target=target_key, button_emoji_target_page=target_page)
    label = html.escape(_button_catalog.get(target_key, target_key))
    await callback.message.edit_text(
        f"Tugma: <b>{label}</b>\nPack'dan emoji tanlang:",
        reply_markup=get_button_emoji_pack_keyboard(0, target_page)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("button_emoji_pack_page:"))
async def cb_button_emoji_pack_page(callback: CallbackQuery, state: FSMContext):
    if not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    try:
        page = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("❌ Sahifa noto'g'ri.", show_alert=True)
        return
    data = await state.get_data()
    await callback.message.edit_reply_markup(
        reply_markup=get_button_emoji_pack_keyboard(
            page, int(data.get("button_emoji_target_page", 0))
        )
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("button_emoji_pick:"))
async def cb_button_emoji_pick(callback: CallbackQuery, state: FSMContext):
    if not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    data = await state.get_data()
    target_key = data.get("button_emoji_target")
    try:
        index = int(callback.data.rsplit(":", 1)[1])
        document_id, alt = _custom_emoji_pack_cache[index]
    except (ValueError, IndexError):
        await callback.answer("❌ Emoji ro'yxati eskirgan. Qaytadan oching.", show_alert=True)
        return
    if not target_key or target_key not in _button_catalog:
        await callback.answer("❌ Tugma tanlovi eskirgan. Qaytadan oching.", show_alert=True)
        return

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"button_emoji:{target_key}", str(document_id))
        )
        await db.commit()
    _button_custom_emoji_ids[target_key] = str(document_id)
    label = html.escape(_button_catalog[target_key])
    target_page = int(data.get("button_emoji_target_page", 0))
    await callback.message.edit_text(
        f"✅ {html.escape(alt)} custom emoji <b>{label}</b> tugmasiga o'rnatildi.",
        reply_markup=get_button_emoji_targets_keyboard(target_page)
    )
    await callback.answer()


@dp.callback_query(F.data == "button_emoji_clear")
async def cb_button_emoji_clear(callback: CallbackQuery, state: FSMContext):
    if not is_panel_owner(callback.from_user.id):
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    data = await state.get_data()
    target_key = data.get("button_emoji_target")
    if not target_key:
        await callback.answer("❌ Tugma tanlanmagan.", show_alert=True)
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM bot_settings WHERE key = ?", (f"button_emoji:{target_key}",))
        await db.commit()
    _button_custom_emoji_ids.pop(target_key, None)
    target_page = int(data.get("button_emoji_target_page", 0))
    await callback.message.edit_text(
        "✅ Tugma custom emoji'si olib tashlandi.",
        reply_markup=get_button_emoji_targets_keyboard(target_page)
    )
    await callback.answer()

# — Kanallar boshqaruvi —
@dp.callback_query(F.data == "admin_channels")
async def cb_admin_channels(callback: CallbackQuery):
    if is_clone_bot() or not is_panel_owner(callback.from_user.id):
        return
    channels = await get_channels()
    text = "📢 <b>Obuna kanallari</b>\n\n"
    if channels:
        for i, ch in enumerate(channels, 1):
            text += f"{i}. {ch['username']}\n"
    else:
        text += "Hozircha kanallar yo'q."
    await callback.message.edit_text(text, reply_markup=get_admin_channels_keyboard(channels))

@dp.callback_query(F.data == "admin_add_channel")
async def cb_admin_add_channel(callback: CallbackQuery, state: FSMContext):
    if is_clone_bot() or not is_panel_owner(callback.from_user.id):
        return
    await callback.message.edit_text(
        "➕ Yangi kanal username kiriting (masalan: @my_channel):",
        reply_markup=back_kb("admin_channels")
    )
    await state.set_state(UserStatesGroup.admin_add_channel)

@dp.message(StateFilter(UserStatesGroup.admin_add_channel), F.text)
async def admin_add_channel_input(message: Message, state: FSMContext):
    if is_clone_bot() or not is_panel_owner(message.from_user.id):
        return
    raw = message.text.strip()
    raw = re.sub(r"^(?:https?://)?(?:www\.)?t\.me/", "", raw, flags=re.IGNORECASE)
    raw = raw.split("?", 1)[0].strip("/@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", raw):
        await message.answer("❌ Kanal username yoki t.me/username havolasini kiriting.")
        return
    channel_username = f"@{raw}"
    try:
        chat = await bot.get_chat(channel_username)
        if chat.type != "channel":
            await message.answer("❌ Bu username kanalga tegishli emas.")
            return
        bot_member = await bot.get_chat_member(chat_id=chat.id, user_id=(await bot.get_me()).id)
        status = getattr(bot_member.status, "value", bot_member.status)
        if status not in {"creator", "administrator"}:
            await message.answer("❌ Avval botni kanalga admin qiling.")
            return
        if not chat.username:
            await message.answer("❌ Faqat ochiq username'li kanal qo'shish mumkin.")
            return
        channel_username = f"@{chat.username}"
        url = f"https://t.me/{chat.username}"
    except Exception as exc:
        log.warning("Majburiy kanalni tekshirishda xato (%s): %s", channel_username, exc)
        await message.answer("❌ Kanal topilmadi. Username'ni va bot adminligini tekshiring.")
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO sub_channels VALUES (?, ?, ?)",
            (channel_username, url, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()
    await state.clear()
    await message.answer(
        f"✅ <b>{html.escape(channel_username)}</b> kanali qo'shildi!",
        reply_markup=get_admin_keyboard()
    )

@dp.callback_query(F.data.startswith("del_channel:"))
async def cb_del_channel(callback: CallbackQuery):
    if is_clone_bot() or not is_panel_owner(callback.from_user.id):
        return
    ch_username = callback.data.split(":", 1)[1]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM sub_channels WHERE channel_username = ?", (ch_username,))
        await db.commit()
    await callback.answer(f"✅ {ch_username} o'chirildi!", show_alert=True)
    channels = await get_channels()
    await callback.message.edit_text(
        "📢 <b>Obuna kanallari</b>",
        reply_markup=get_admin_channels_keyboard(channels)
    )

# — Foydalanuvchilar —
@dp.callback_query(F.data == "admin_users")
async def cb_admin_users(callback: CallbackQuery):
    if not is_panel_owner(callback.from_user.id):
        return
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            total = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE pro_until IS NOT NULL"
        ) as cur:
            pro_count = (await cur.fetchone())[0]
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📋 Ro'yxat (oxirgi 20)", callback_data="admin_user_list"))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_panel"))
    await callback.message.edit_text(
        f"👥 <b>Foydalanuvchilar</b>\n\n"
        f"Jami: <b>{total}</b>\n"
        f"PRO: <b>{pro_count}</b>",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data == "admin_user_list")
async def cb_admin_user_list(callback: CallbackQuery):
    if not is_panel_owner(callback.from_user.id):
        return
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT id, fullname, username, pro_until FROM users ORDER BY first_seen DESC LIMIT 20"
        ) as cur:
            rows = await cur.fetchall()
    text = "📋 <b>Oxirgi 20 foydalanuvchi:</b>\n\n"
    for uid, fullname, username, pro_until in rows:
        pro = "✅" if is_pro_user(pro_until) else "❌"
        uname_str = f"@{username}" if username else "—"
        text += f"{pro} <b>{fullname or '?'}</b> ({uname_str}) | ID: <code>{uid}</code>\n"
    await callback.message.edit_text(text, reply_markup=back_kb("admin_users"))

# — PRO berish —
@dp.callback_query(F.data == "admin_give_pro")
async def cb_admin_give_pro_start(callback: CallbackQuery, state: FSMContext):
    if not is_panel_owner(callback.from_user.id):
        return
    await callback.message.edit_text(
        "⭐ PRO berish uchun foydalanuvchi ID va kun sonini kiriting:\n"
        "Format: <code>ID kun</code>\nMasalan: <code>123456789 7</code>",
        reply_markup=back_kb("admin_panel")
    )
    await state.set_state(UserStatesGroup.admin_give_pro)

@dp.message(StateFilter(UserStatesGroup.admin_give_pro), F.text)
async def admin_give_pro_input(message: Message, state: FSMContext):
    if not is_panel_owner(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) != 2:
        await message.answer("❌ Format: <code>ID kun</code>")
        return
    target_id, days_str = parts
    try:
        days = int(days_str)
    except ValueError:
        await message.answer("❌ Kun soni raqam bo'lishi kerak.")
        return

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT pro_until FROM users WHERE id = ?", (target_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            await message.answer("❌ Foydalanuvchi topilmadi.")
            await state.clear()
            return
        current_pro = row[0]
        base_time = datetime.now(timezone.utc)
        if current_pro and is_pro_user(current_pro):
            base_time = datetime.fromisoformat(current_pro)
        now = datetime.now(timezone.utc)
        new_pro = (base_time + timedelta(days=days)).isoformat()
        raid_until = min(
            datetime.fromisoformat(new_pro), now + timedelta(days=7)
        ).isoformat()
        await db.execute(
            "UPDATE users SET pro_until = ?, raid_until = ?, notified_10m = 0 WHERE id = ?",
            (new_pro, raid_until, target_id)
        )
        await db.commit()

    # Bio dan reklamani o'chir (agar userbot ulangan bo'lsa)
    if target_id in userbot_clients:
        await set_ad_bio(userbot_clients[target_id], is_pro=True)

    try:
        await bot.send_message(
            int(target_id),
            f"🎉 <b>PRO tarif faollashtirildi!</b>\n\n"
            f"⏳ Muddat: <b>{days} kun</b>\n"
            f"✅ Profil bio'ngizdan reklama olib tashlandi."
        )
    except Exception:
        pass

    await state.clear()
    await message.answer(
        f"✅ Foydalanuvchi <code>{target_id}</code> ga {days} kunlik PRO berildi.",
        reply_markup=get_admin_keyboard()
    )

# — Xabar tarqatish —
@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_panel_owner(callback.from_user.id):
        return
    await callback.message.edit_text(
        "📣 Barcha foydalanuvchilarga yuboriladigan xabarni kiriting:",
        reply_markup=back_kb("admin_panel")
    )
    await state.set_state(UserStatesGroup.admin_broadcast)

@dp.message(StateFilter(UserStatesGroup.admin_broadcast), F.text)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if not is_panel_owner(message.from_user.id):
        return
    text = message.text
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT id FROM users") as cur:
            user_ids = [r[0] for r in await cur.fetchall()]

    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await bot.send_message(int(uid), text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await state.clear()
    await message.answer(
        f"📣 Xabar yuborildi!\n✅ Muvaffaqiyatli: <b>{sent}</b>\n❌ Muvaffaqiyatsiz: <b>{failed}</b>",
        reply_markup=get_admin_keyboard()
    )

# ─────────────────────────────────────────────
# KONKURS (GIVEAWAY)
# ─────────────────────────────────────────────

def normalize_contest_chat_reference(value: str) -> str | int | None:
    value = value.strip()
    if value.startswith(("https://", "http://")):
        parsed = urlparse(value)
        if parsed.netloc.lower() not in {"t.me", "www.t.me", "telegram.me"}:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return None
        if parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
            return int(f"-100{parts[1]}")
        value = parts[0]
    if value.lstrip("-").isdigit():
        return int(value)
    value = value.lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", value):
        return f"@{value}"
    return None


async def publish_auto_pro_contest(message: Message, state: FSMContext):
    data = await state.get_data()
    chat_reference = data["contest_chat_reference"]
    max_users = data["contest_max"]
    req_ch_list = data.get("contest_req_channels", [])
    prize_text = "7 kunlik PRO tarif"
    req_ch_str = ",".join(req_ch_list)

    try:
        chat = await bot.get_chat(chat_reference)
        if chat.type not in {"channel", "group", "supergroup"}:
            raise ValueError("Bu Telegram kanal yoki guruh emas.")
        bot_member = await bot.get_chat_member(chat_id=chat.id, user_id=(await bot.get_me()).id)
        status = getattr(bot_member.status, "value", bot_member.status)
        if status not in {"creator", "administrator"}:
            await state.clear()
            await message.answer(
                "❌ Bot bu kanal/guruhda admin emas. Botni admin qilib, konkursni qayta yarating.",
                reply_markup=get_admin_keyboard()
            )
            return
        if chat.type == "channel" and not getattr(bot_member, "can_post_messages", False):
            await state.clear()
            await message.answer(
                "❌ Bot admin, lekin kanalga xabar yuborish ruxsati yo'q.",
                reply_markup=get_admin_keyboard()
            )
            return
    except Exception as exc:
        log.warning("Konkurs chatini tekshirishda xato (%s): %s", chat_reference, exc)
        await state.clear()
        await message.answer(
            "❌ Kanal/guruh topilmadi yoki bot uni tekshira olmadi. Username, havola yoki ID ni tekshiring.",
            reply_markup=get_admin_keyboard()
        )
        return

    req_ch_display = "\n".join(f"• {html.escape(channel)}" for channel in req_ch_list) or "Yo'q"
    contest_text = (
        "🎉 <b>KONKURS BOSHLANDI!</b>\n\n"
        f"🏆 Sovrin: <b>{prize_text}</b>\n\n"
        f"👥 Kerakli ishtirokchilar soni: <b>{max_users}</b>\n"
        f"📢 Majburiy kanallar:\n{req_ch_display}\n\n"
        "✅ Ishtirok etish uchun pastdagi tugmani bosing!"
    )
    placeholder_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎟 Ishtirok etish", callback_data="join_contest_pending")
    ]])

    try:
        sent = await bot.send_message(chat.id, contest_text, reply_markup=placeholder_keyboard)
        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute(
                "INSERT INTO contests (chat_id, message_id, prize_text, max_users, req_channels, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?)",
                (str(chat.id), str(sent.message_id), prize_text, max_users, req_ch_str,
                 datetime.now(timezone.utc).isoformat())
            )
            contest_id = cursor.lastrowid
            await db.commit()

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🎟 Ishtirok etish",
                callback_data=f"join_contest:{contest_id}"
            )
        ]])
        await bot.edit_message_reply_markup(
            chat_id=chat.id, message_id=sent.message_id, reply_markup=keyboard
        )
        await state.clear()
        await message.answer(
            f"✅ Konkurs #{contest_id} <b>{html.escape(chat.title or str(chat.id))}</b> da boshlandi!\n\n"
            f"👥 Kerakli: {max_users} ishtirokchi\n"
            f"🏆 Sovrin: {prize_text} (g'olibga avtomatik beriladi)",
            reply_markup=get_admin_keyboard()
        )
    except Exception as exc:
        log.exception("Konkursni e'lon qilishda xato")
        await state.clear()
        await message.answer(
            "❌ Konkurs xabarini yuborib bo'lmadi. Botda xabar yuborish huquqi borligini tekshiring.",
            reply_markup=get_admin_keyboard()
        )

# ── Admin: konkurs yaratish ──
@dp.callback_query(F.data == "admin_contest_create")
async def cb_contest_create(callback: CallbackQuery, state: FSMContext):
    if not is_panel_owner(callback.from_user.id):
        return
    await state.clear()
    await callback.message.edit_text(
        "🎉 <b>Konkurs yaratish</b>\n\n"
        "1️⃣ Konkurs o'tkaziladigan kanal yoki guruh username ini kiriting:\n"
        "Username, t.me havolasi yoki chat ID yuboring. Bot u yerda admin bo'lishi kerak.\n"
        "Masalan: <code>@mening_kanalim</code> yoki <code>-1001234567890</code>",
        reply_markup=back_kb("admin_panel")
    )
    await state.set_state(UserStatesGroup.contest_channel)

@dp.message(StateFilter(UserStatesGroup.contest_channel), F.text)
async def contest_channel_input(message: Message, state: FSMContext):
    if not is_panel_owner(message.from_user.id):
        return
    chat_reference = normalize_contest_chat_reference(message.text)
    if chat_reference is None:
        await message.answer("❌ Username, t.me havolasi yoki raqamli chat ID yuboring.")
        return
    await state.update_data(contest_chat_reference=chat_reference)
    await message.answer(
        "2️⃣ Nechta ishtirokchi to'lganda g'olib tanlansin?\n"
        "(masalan: <code>100</code>)"
    )
    await state.set_state(UserStatesGroup.contest_max_users)

@dp.message(StateFilter(UserStatesGroup.contest_max_users), F.text)
async def contest_max_input(message: Message, state: FSMContext):
    if not is_panel_owner(message.from_user.id):
        return
    try:
        max_u = int(message.text.strip())
        if max_u < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Faqat musbat raqam kiriting.")
        return
    await state.update_data(contest_max=max_u)
    await message.answer(
        "3️⃣ Ishtirok etish uchun majburiy kanallar/guruhlar username larini kiriting.\n"
        "Har birini yangi qatordan yozing. Majburiy kanal bo'lmasa <code>yo'q</code> yozing.\n\n"
        "Masalan:\n<code>@kanal1\n@kanal2</code>"
    )
    await state.set_state(UserStatesGroup.contest_req_channels)

@dp.message(StateFilter(UserStatesGroup.contest_req_channels), F.text)
async def contest_req_ch_input(message: Message, state: FSMContext):
    if not is_panel_owner(message.from_user.id):
        return
    text = message.text.strip()
    if text.lower() in ("yo'q", "yoq", "no", "-"):
        req_channels = []
    else:
        req_channels = []
        for item in text.splitlines():
            if not item.strip():
                continue
            reference = normalize_contest_chat_reference(item)
            if reference is None:
                await message.answer(
                    f"❌ Noto'g'ri kanal/guruh: <code>{html.escape(item.strip())}</code>. "
                    "Username yoki ID yuboring."
                )
                return
            req_channels.append(str(reference))
    await state.update_data(contest_req_channels=req_channels)
    await publish_auto_pro_contest(message, state)

# ── Admin: aktiv konkurslar ro'yxati ──
@dp.callback_query(F.data == "admin_contests_list")
async def cb_admin_contests(callback: CallbackQuery):
    if not is_panel_owner(callback.from_user.id):
        return
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT id, chat_id, prize_text, max_users, status, "
            "(SELECT COUNT(*) FROM contest_participants WHERE contest_id = contests.id) as cnt "
            "FROM contests ORDER BY id DESC LIMIT 10"
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        await callback.message.edit_text(
            "Hech qanday konkurs yo'q.", reply_markup=back_kb("admin_panel")
        )
        return

    kb = InlineKeyboardBuilder()
    text = "🏆 <b>Konkurslar (oxirgi 10)</b>\n\n"
    for cid, chat_id, prize, max_u, status, cnt in rows:
        emoji = "🟢" if status == "active" else "🔴"
        text += f"{emoji} #{cid} | {chat_id}\n   {prize[:30]}...\n   👥 {cnt}/{max_u}\n\n"
        if status == "active":
            kb.row(
                InlineKeyboardButton(text=f"🏆 #{cid} g'olibni tanlash", callback_data=f"contest_finish:{cid}"),
                InlineKeyboardButton(text=f"❌ #{cid} bekor", callback_data=f"contest_cancel:{cid}")
            )
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_panel"))
    await callback.message.edit_text(text, reply_markup=kb.as_markup())

# ── Admin: g'olibni qo'lda tanlash ──
@dp.callback_query(F.data.startswith("contest_finish:"))
async def cb_contest_finish(callback: CallbackQuery):
    if not is_panel_owner(callback.from_user.id):
        return
    contest_id = int(callback.data.split(":")[1])
    await pick_winner(contest_id)
    await callback.answer("✅ G'olib tanlandi!", show_alert=True)
    # Ro'yxatni yangilash
    await cb_admin_contests(callback)

# ── Admin: konkursni bekor qilish ──
@dp.callback_query(F.data.startswith("contest_cancel:"))
async def cb_contest_cancel(callback: CallbackQuery):
    if not is_panel_owner(callback.from_user.id):
        return
    contest_id = int(callback.data.split(":")[1])
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE contests SET status = 'cancelled' WHERE id = ?", (contest_id,)
        )
        await db.commit()
    await callback.answer("❌ Konkurs bekor qilindi!", show_alert=True)
    await cb_admin_contests(callback)

# ── User: konkursga ishtirok etish ──
@dp.callback_query(F.data.startswith("join_contest:"))
async def cb_join_contest(callback: CallbackQuery):
    contest_id = int(callback.data.split(":")[1])
    user_id    = str(callback.from_user.id)

    async with aiosqlite.connect(DB_FILE) as db:
        # Konkurs mavjudligini tekshirish
        async with db.execute(
            "SELECT max_users, req_channels, status, prize_text, chat_id, message_id "
            "FROM contests WHERE id = ?", (contest_id,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            await callback.answer("❌ Konkurs topilmadi!", show_alert=True)
            return

        max_u, req_ch_str, status, prize_text, chat_id, message_id = row

        if status != "active":
            await callback.answer("❌ Bu konkurs yakunlangan!", show_alert=True)
            return

        # Allaqachon ishtirok etganmi?
        async with db.execute(
            "SELECT 1 FROM contest_participants WHERE contest_id = ? AND user_id = ?",
            (contest_id, user_id)
        ) as cur:
            already = await cur.fetchone()

        if already:
            await callback.answer("✅ Siz allaqachon ishtirok etgansiz!", show_alert=True)
            return

    # Majburiy kanallarga obunani tekshirish
    req_channels = [c for c in req_ch_str.split(",") if c] if req_ch_str else []
    not_joined = []
    for ch in req_channels:
        try:
            member = await bot.get_chat_member(chat_id=ch, user_id=int(user_id))
            if member.status in ["left", "kicked"]:
                not_joined.append(ch)
        except Exception:
            not_joined.append(ch)

    if not_joined:
        kb = InlineKeyboardBuilder()
        for ch in not_joined:
            url = f"https://t.me/{ch.lstrip('@')}"
            kb.row(InlineKeyboardButton(text=f"📢 {ch} ga a'zo bo'lish", url=url))
        kb.row(InlineKeyboardButton(
            text="✅ Obunani tekshirish",
            callback_data=f"join_contest:{contest_id}"
        ))
        await callback.answer("❌ Avval majburiy kanallarga a'zo bo'ling!", show_alert=True)
        try:
            await bot.send_message(
                callback.from_user.id,
                "⚠️ Konkursga ishtirok etish uchun quyidagi kanallarga a'zo bo'ling:",
                reply_markup=kb.as_markup()
            )
        except Exception:
            pass
        return

    # Ishtirokchini qo'shish
    fullname = callback.from_user.full_name or ""
    username = f"@{callback.from_user.username}" if callback.from_user.username else fullname

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO contest_participants (contest_id, user_id, username, fullname, joined_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (contest_id, user_id, username, fullname, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()

        # Joriy son
        async with db.execute(
            "SELECT COUNT(*) FROM contest_participants WHERE contest_id = ?", (contest_id,)
        ) as cur:
            cnt = (await cur.fetchone())[0]

    await callback.answer(
        f"🎟 Ishtirok etdingiz! Siz {cnt}-ishtirokchisiz.", show_alert=True
    )

    # Xabardagi tugma matnini yangilaymiz
    try:
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(
            text=f"🎟 Ishtirok etish ({cnt}/{max_u})",
            callback_data=f"join_contest:{contest_id}"
        ))
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=int(message_id),
            reply_markup=kb.as_markup()
        )
    except Exception:
        pass

    # Kerakli son to'ldimi? → g'olib tanlash
    if cnt >= max_u:
        await pick_winner(contest_id)

# ── G'olib tanlash ──
async def pick_winner(contest_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT status, chat_id, message_id, prize_text "
            "FROM contests WHERE id = ?", (contest_id,)
        ) as cur:
            row = await cur.fetchone()

        if not row or row[0] != "active":
            return

        _, chat_id, message_id, prize_text = row

        async with db.execute(
            "SELECT user_id, username, fullname "
            "FROM contest_participants WHERE contest_id = ?", (contest_id,)
        ) as cur:
            participants = await cur.fetchall()

        if not participants:
            return

        winner = random.choice(participants)
        winner_id, winner_username, winner_fullname = winner
        winner_display = winner_username if winner_username.startswith("@") else winner_fullname

        pro_awarded = bool(re.search(r"(?<!\w)pro(?!\w)", prize_text or "", re.IGNORECASE))
        if pro_awarded:
            now = datetime.now(timezone.utc)
            await db.execute(
                "INSERT OR IGNORE INTO users "
                "(id, fullname, username, pro_until, raid_until, notified_10m, first_seen) "
                "VALUES (?, ?, ?, NULL, NULL, 0, ?)",
                (winner_id, winner_fullname or "", winner_username or "", now.isoformat())
            )
            async with db.execute(
                "SELECT pro_until FROM users WHERE id = ?", (winner_id,)
            ) as cur:
                user_row = await cur.fetchone()
            base_time = now
            if user_row and is_pro_user(user_row[0]):
                base_time = datetime.fromisoformat(user_row[0])
            pro_until = base_time + timedelta(days=7)
            raid_until = min(pro_until, now + timedelta(days=7))
            await db.execute(
                "UPDATE users SET pro_until = ?, raid_until = ?, notified_10m = 0 WHERE id = ?",
                (pro_until.isoformat(), raid_until.isoformat(), winner_id)
            )

        await db.execute(
            "UPDATE contests SET status = 'finished', winner_id = ? WHERE id = ?",
            (winner_id, contest_id)
        )
        await db.commit()

    # Kanalga g'olib e'loni
    total = len(participants)
    announce = (
        f"🏆 <b>KONKURS YAKUNLANDI!</b>\n\n"
        f"🎉 G'olib: <b>{winner_display}</b>\n"
        f"🏅 Sovrin: {prize_text}\n\n"
        f"👥 Jami ishtirokchilar: {total} ta\n\n"
        f"Tabriklaymiz! 🎊"
    )
    try:
        await bot.send_message(chat_id, announce)
    except Exception as e:
        log.error(f"G'olib e'lonida xato: {e}")

    # G'olibga shaxsiy xabar
    try:
        await bot.send_message(
            int(winner_id),
            f"🎉 <b>Tabriklaymiz!</b>\n\n"
            f"Siz konkursda g'olib bo'ldingiz!\n"
            f"🏅 Sovrin: {prize_text}\n\n"
            + (
                "⭐ Sizga 7 kunlik PRO berildi. Raid panel shu muddatda ochiq."
                if pro_awarded
                else f"Mukofotni olish uchun {ADMIN_USERNAME} ga murojaat qiling."
            )
        )
    except Exception:
        pass

    # Adminga xabar
    try:
        await bot.send_message(
            current_panel_owner_id(),
            f"🏆 <b>Konkurs #{contest_id} yakunlandi!</b>\n\n"
            f"G'olib: {winner_display}\n"
            f"ID: <code>{winner_id}</code>\n"
            f"Jami ishtirokchilar: {total}"
        )
    except Exception:
        pass

# ─────────────────────────────────────────────
# STARTUP: userbot sessiyalarini yuklash
# ─────────────────────────────────────────────
async def load_existing_sessions():
    global bot_username
    me = await asyncio.wait_for(bot.get_me(), timeout=15)
    bot_username = me.username or ""

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT user_id, account_id, session FROM user_sessions"
        ) as cur:
            sessions = await cur.fetchall()

    semaphore = asyncio.Semaphore(5)

    async def load_session(uid, account_id, session_str):
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        keep_client = False
        try:
            async with semaphore:
                await asyncio.wait_for(client.connect(), timeout=15)
                authorized = await asyncio.wait_for(
                    client.is_user_authorized(), timeout=10
                )
                if authorized:
                    userbot_clients[uid] = client
                    await register_userbot_handlers(client, uid)
                    keep_client = True
                else:
                    async with aiosqlite.connect(DB_FILE) as db:
                        await db.execute(
                            "DELETE FROM user_sessions WHERE account_id = ?",
                            (account_id,),
                        )
                        await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Sessiya yuklashda xato (%s): %s", uid, exc)
        finally:
            if not keep_client:
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=5)
                except Exception:
                    pass

    await asyncio.gather(
        *(load_session(uid, account_id, session) for uid, account_id, session in sessions)
    )


async def poll_clone_updates(clone_bot: Bot, bot_id: int, owner_id: int, username: str):
    offset = None
    try:
        await clone_bot.delete_webhook(drop_pending_updates=False)
        while True:
            updates = await clone_bot.get_updates(
                offset=offset,
                timeout=30,
                allowed_updates=dp.resolve_used_update_types(),
            )
            for update in updates:
                offset = update.update_id + 1
                bot_token = _active_bot.set(clone_bot)
                owner_token = _active_clone_owner.set(owner_id)
                username_token = _active_bot_username.set(username)
                try:
                    await dp.feed_update(clone_bot, update)
                except Exception as exc:
                    log.warning("Clone update failed for bot %s: %s", bot_id, exc)
                finally:
                    _active_bot_username.reset(username_token)
                    _active_clone_owner.reset(owner_token)
                    _active_bot.reset(bot_token)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error("Clone polling stopped for bot %s: %s", bot_id, exc)


def start_registered_clone(bot_id: str, owner_id: int, username: str, token: str):
    numeric_bot_id = int(bot_id)
    if numeric_bot_id in clone_polling_tasks:
        return
    clone_bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    clone_bots[numeric_bot_id] = clone_bot
    clone_polling_tasks[numeric_bot_id] = asyncio.create_task(
        poll_clone_updates(clone_bot, numeric_bot_id, owner_id, username)
    )


async def load_registered_clones():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT bot_id, owner_id, username, token_encrypted FROM cloned_bots WHERE active = 1"
        ) as cur:
            rows = await cur.fetchall()
    if rows and not CLONE_TOKEN_ENCRYPTION_KEY:
        log.error("Clone bots are saved but CLONE_TOKEN_ENCRYPTION_KEY is not configured")
        return
    for bot_id, owner_id, username, encrypted_token in rows:
        try:
            token = clone_token_cipher().decrypt(encrypted_token.encode()).decode()
            start_registered_clone(bot_id, int(owner_id), username, token)
        except (InvalidToken, ValueError, RuntimeError):
            log.error("Could not decrypt token for clone bot %s", bot_id)


async def stop_registered_clones():
    tasks = list(clone_polling_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for clone_bot in clone_bots.values():
        await clone_bot.session.close()
    clone_polling_tasks.clear()
    clone_bots.clear()


async def health_server():
    """Render health checks uchun kichik HTTP endpoint."""
    async def handle_health(reader, writer):
        try:
            await reader.read(1024)
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                "Content-Length: 2\r\n"
                "Connection: close\r\n\r\n"
                "OK"
            )
            writer.write(response.encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    port = int(os.getenv("PORT", "10000"))
    server = await asyncio.start_server(handle_health, "0.0.0.0", port)
    async with server:
        await server.serve_forever()

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def register_common_keyboard_buttons():
    get_main_keyboard(has_pro=True)
    get_admin_keyboard()
    get_admin_channels_keyboard([])
    get_userbot_keyboard(None)
    get_settings_keyboard()
    get_utag_speed_keyboard(1.5)
    get_login_code_keyboard()
    get_auto_reply_keyboard(False)
    get_scrape_groups_keyboard("")
    get_scrape_count_keyboard()
    get_scrape_mode_keyboard()
    back_kb()


async def main():
    await init_db()
    asyncio.create_task(health_server())
    await load_existing_sessions()
    register_common_keyboard_buttons()
    await load_registered_clones()
    asyncio.create_task(pro_expiration_checker())
    asyncio.create_task(bio_watcher())
    try:
        await dp.start_polling(primary_bot, skip_updates=True)
    finally:
        await stop_registered_clones()

if __name__ == "__main__":
    asyncio.run(main())
