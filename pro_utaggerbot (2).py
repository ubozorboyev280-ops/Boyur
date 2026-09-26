import asyncio
import html
import logging
import os
import re
import random
import zipfile
import aiosqlite
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
    ReplyKeyboardMarkup, KeyboardButton as TelegramKeyboardButton
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
    MessageEntityCustomEmoji,
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


API_ID    = int(required_env("API_ID"))
if not 1 <= API_ID <= 2_147_483_647:
    raise RuntimeError("API_ID must be the valid app ID from my.telegram.org, not a Telegram user ID")
API_HASH  = required_env("API_HASH")
BOT_TOKEN = required_env("BOT_TOKEN")
ADMIN_ID  = int(required_env("ADMIN_ID"))
if ADMIN_ID <= 0:
    raise RuntimeError("ADMIN_ID must be a positive Telegram user ID")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@owapro")
DB_FILE   = "database22.db"
CUSTOM_EMOJI_PACK = os.getenv("CUSTOM_EMOJI_PACK", "").strip()

AD_TEXT = "🤖 Powered by @Prime_utaggerbot 🚀"
BIO_AD_TEXT = "🤖 Powered by @Prime_utaggerbot 🚀"
AUTO_REPLY_AD = f"{AD_TEXT}\n🤖 Avto javob qilindi."
SOURCE_FILE = os.getenv("SOURCE_FILE", __file__)


def get_utag_command_help() -> str:
    return (
        "📌 <b>uTag buyruqlari</b> (guruhda yozing):\n"
        "• <code>.s</code> — uTag boshlash\n"
        "• <code>.r</code> — random uTag\n"
        "• <code>.f</code> — uTag to'xtatish"
    )

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp  = Dispatcher(storage=storage)

userbot_clients: dict[str, TelegramClient] = {}
_utag_tasks: dict[str, asyncio.Task] = {}
_utag_suppress_ad_tasks: set[asyncio.Task] = set()
_raid_locks: dict[str, asyncio.Lock] = {}
_auto_reply_cooldowns: dict[tuple[str, int], datetime] = {}
_scrape_group_choices: dict[str, dict[str, object]] = {}
_custom_emoji_pack_cache: list[tuple[int, str]] = []
_custom_emoji_pack_lock = asyncio.Lock()
_button_custom_emoji_ids: dict[str, str] = {}
_button_catalog: dict[str, str] = {}
_button_emoji_target_keys: list[str] = []
bot_username = ""


def _build_emoji_button(button_type, text: str, kwargs: dict):
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
    admin_custom_emoji_pack = State()
    admin_give_pro         = State()
    contest_channel        = State()
    contest_max_users      = State()
    contest_req_channels   = State()
    contest_prize_text     = State()

# ─────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────
async def init_db():
    global CUSTOM_EMOJI_PACK
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            fullname TEXT,
            username TEXT,
            referrer_id TEXT,
            pro_until TEXT,
            notified_10m INTEGER DEFAULT 0,
            first_seen TEXT
        )""")
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
        try:
            await db.execute(
                "ALTER TABLE scraped_users ADD COLUMN telegram_user_id TEXT"
            )
        except Exception:
            pass
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


def has_ban_rights(permissions) -> bool:
    admin_rights = getattr(permissions, "admin_rights", None)
    return bool(
        getattr(permissions, "is_creator", False)
        or getattr(admin_rights, "ban_users", False)
    )

async def check_subscriptions(user_id: int) -> tuple[bool | None, str | None]:
    channels = await get_channels()
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch["username"], user_id=user_id)
            subscribed = member.status in {"creator", "administrator", "member"}
            subscribed = subscribed or (
                member.status == "restricted" and bool(getattr(member, "is_member", False))
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
    user, custom_emoji: tuple[int, str] | None = None
) -> tuple[str, list[MessageEntityCustomEmoji]]:
    """Username va tasodifiy so'z bilan uTag, pack emoji entitysi bilan."""
    username = getattr(user, "username", None)
    if username:
        mention = f"@{username}"
    else:
        mention = get_display_name(user).replace("#", "").strip() or "do'stimiz"
    word = random.choice(RANDOM_UTAG_WORDS)
    emoji_text = custom_emoji[1] if custom_emoji else random.choice(RANDOM_UTAG_STICKERS)
    text = f"{mention}, {word} {emoji_text}"
    if not custom_emoji:
        return text, []

    emoji_length = len(emoji_text.encode("utf-16-le")) // 2
    text_length = len(text.encode("utf-16-le")) // 2
    entity = MessageEntityCustomEmoji(
        offset=text_length - emoji_length,
        length=emoji_length,
        document_id=custom_emoji[0],
    )
    return text, [entity]


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

def get_main_keyboard(has_pro: bool = False) -> InlineKeyboardMarkup:
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
    if has_pro:
        kb.row(InlineKeyboardButton(text="🛡 Raid panel", callback_data="raid_panel"))
    return kb.as_markup()


async def user_has_active_pro(user_id: int | str) -> bool:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT pro_until FROM users WHERE id = ?", (str(user_id),)
        ) as cur:
            row = await cur.fetchone()
    return is_pro_user(row[0] if row else None)


async def get_user_main_keyboard(user_id: int | str) -> InlineKeyboardMarkup:
    return get_main_keyboard(await user_has_active_pro(user_id))

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
                                    "⭐ PRO muddati tugadi. Raid panel endi yopildi.",
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
        async with db.execute("SELECT id FROM users WHERE id = ?", (uid,)) as cursor:
            user_exists = await cursor.fetchone()

        fullname = message.from_user.full_name or ""
        username = message.from_user.username or ""

        if not user_exists:
            await db.execute(
                "INSERT INTO users (id, fullname, username, referrer_id, pro_until, notified_10m, first_seen) VALUES (?, ?, ?, ?, ?, 0, ?)",
                (uid, fullname, username, referrer_id, None, datetime.now(timezone.utc).isoformat())
            )
            await db.commit()
        else:
            await db.execute(
                "UPDATE users SET fullname = ?, username = ? WHERE id = ?",
                (fullname, username, uid)
            )
            await db.commit()

        if not user_exists and referrer_id:
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
                await db.execute(
                    "UPDATE users SET pro_until = ?, notified_10m = 0 WHERE id = ?",
                    (new_pro, referrer_id)
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


@dp.callback_query(F.data == "raid_panel")
async def cb_raid_panel(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    if not await user_has_active_pro(uid):
        await callback.message.edit_reply_markup(
            reply_markup=await get_user_main_keyboard(uid)
        )
        await callback.answer("Raid panel uchun faol PRO kerak.", show_alert=True)
        return

    groups = await get_raid_groups()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT used_at FROM raid_usage WHERE user_id = ?", (uid,)
        ) as cur:
            row = await cur.fetchone()
    limit_text = "Raid limiti: haftasiga 1 ta ban amali."
    if row:
        try:
            used_at = datetime.fromisoformat(row[0])
            if used_at.tzinfo is None:
                used_at = used_at.replace(tzinfo=timezone.utc)
            available_at = used_at + timedelta(days=7)
            if datetime.now(timezone.utc) < available_at:
                limit_text = (
                    "Keyingi raid amali: "
                    f"<b>{available_at.strftime('%Y-%m-%d %H:%M')} UTC</b> dan keyin."
                )
        except ValueError:
            pass

    group_text = "\n".join(
        f"• {html.escape(group['title'])}" for group in groups
    ) or "Hozircha admin paneldan guruh tanlanmagan."
    await callback.message.edit_text(
        "🛡 <b>Raid panel (PRO)</b>\n\n"
        f"Ruxsatli guruhlar:\n{group_text}\n\n"
        f"{limit_text}\n"
        "Guruhda <code>.ban @username</code> yoki <code>.mban @username</code> "
        "yozing. Amallar faqat ruxsatli ro'yxatdagi va profilingiz ban huquqiga ega guruhlarda bajariladi.",
        reply_markup=back_kb("main_menu")
    )
    await callback.answer()

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

@dp.callback_query(F.data == "settings_my_ref")
async def cb_my_ref(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    await callback.message.edit_text(
        f"📢 <b>Sizning referal havolangiz:</b>\n\n"
        f"<code>https://t.me/{bot_username}?start={uid}</code>\n\n"
        "3 ta do'stingizni taklif qiling — <b>3 kunlik PRO tarif</b> oling!",
        reply_markup=back_kb("btn_settings")
    )

# ─────────────────────────────────────────────
# PRO PANEL
# ─────────────────────────────────────────────
@dp.callback_query(F.data == "btn_pro_info")
async def cb_pro_info(callback: CallbackQuery):
    uid = str(callback.from_user.id)
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
        f"🔗 Referal havolangiz:\n<code>https://t.me/{bot_username}?start={uid}</code>"
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
        tagged = 0
        seen_usernames: set[str] = set()

        for user in participants:
            if uid not in _utag_tasks or _utag_tasks[uid].done():
                break  # To'xtatildi
            if user.id == me.id or user.bot:
                continue
            username = getattr(user, "username", None)
            username_key = username.casefold() if username else ""
            if not username_key or username_key in seen_usernames:
                continue
            seen_usernames.add(username_key)
            try:
                if random_mode:
                    custom_emoji = random.choice(custom_emojis) if custom_emojis else None
                    tag_text, entities = make_random_utag_text(user, custom_emoji)
                else:
                    tag_text = make_text_unique(f"@{username}")
                    entities = []
                try:
                    await client.send_message(
                        chat, tag_text, formatting_entities=entities or None
                    )
                except (PremiumAccountRequiredError, EntityBoundsInvalidError):
                    if not entities:
                        raise
                    await client.send_message(chat, tag_text)
                tagged += 1
                await asyncio.sleep(delay)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 5)
            except (UserPrivacyRestrictedError, PeerFloodError):
                continue
            except Exception as e:
                log.error(f"Tag xatosi: {e}")
                continue

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

    @client.on(events.NewMessage(pattern=r'^[./](r|ru)$', incoming=False, outgoing=True))
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
        if not await user_has_active_pro(uid):
            await event.respond("Raid uchun faol PRO kerak.")
            return

        lock = _raid_locks.setdefault(uid, asyncio.Lock())
        async with lock:
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute(
                    "SELECT used_at FROM raid_usage WHERE user_id = ?", (uid,)
                ) as cur:
                    usage = await cur.fetchone()
            if usage:
                try:
                    used_at = datetime.fromisoformat(usage[0])
                    if used_at.tzinfo is None:
                        used_at = used_at.replace(tzinfo=timezone.utc)
                    available_at = used_at + timedelta(days=7)
                    if datetime.now(timezone.utc) < available_at:
                        await event.respond(
                            "Raid limiti haftasiga 1 marta. Keyingi amal: "
                            f"{available_at.strftime('%Y-%m-%d %H:%M')} UTC dan keyin."
                        )
                        return
                except ValueError:
                    pass

            groups = await get_raid_groups()
            if not groups:
                await event.respond("Raid guruhlari admin paneldan hali tanlanmagan.")
                return
            username = event.pattern_match.group(1)
            try:
                target = await client.get_entity(username)
                me = await client.get_me()
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
                    entity = await client.get_entity(int(group["chat_id"]))
                    permissions = await client.get_permissions(entity, me)
                    if not has_ban_rights(permissions):
                        no_rights.append(group["title"])
                        continue
                    await client.edit_permissions(entity, target, view_messages=False)
                    banned_groups.append(group["title"])
                except FloodWaitError as exc:
                    log.warning("Raid ban FloodWait (%s): %s", group["title"], exc)
                    break
                except Exception as exc:
                    log.warning("Raid ban failed in %s: %s", group["title"], exc)

            if banned_groups:
                used_at = datetime.now(timezone.utc).isoformat()
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute(
                        "INSERT INTO raid_usage (user_id, used_at) VALUES (?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET used_at = excluded.used_at",
                        (uid, used_at)
                    )
                    await db.commit()
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

    usernames = [u.strip() for u in message.text.split("\n") if u.strip()]
    await message.answer(f"⏳ {len(usernames)} ta foydalanuvchiga xabar yuborilmoqda...")

    sent = 0
    failed = 0
    for uname in usernames:
        try:
            await client.send_message(uname, make_text_unique(text))
            sent += 1
            await asyncio.sleep(2)
        except Exception as e:
            failed += 1
            log.error(f"Xabar yuborishda xato {uname}: {e}")

    await message.answer(
        f"✅ Yuborildi: <b>{sent}</b>\n❌ Muvaffaqiyatsiz: <b>{failed}</b>",
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
    kb.row(InlineKeyboardButton(text="📢 Kanallarni boshqarish", callback_data="admin_channels"))
    kb.row(InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="admin_users"))
    kb.row(InlineKeyboardButton(text="⭐ PRO berish", callback_data="admin_give_pro"))
    kb.row(InlineKeyboardButton(text="🎉 Konkurs yaratish", callback_data="admin_contest_create"))
    kb.row(InlineKeyboardButton(text="🏆 Aktiv konkurslar", callback_data="admin_contests_list"))
    kb.row(InlineKeyboardButton(text="📣 Xabar tarqatish", callback_data="admin_broadcast"))
    kb.row(InlineKeyboardButton(text="😀 Custom emoji pack", callback_data="admin_custom_emoji"))
    kb.row(InlineKeyboardButton(text="🛡 Raid guruhlarini tanlash", callback_data="admin_raid_groups"))
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

@dp.message(Command("admin"), F.chat.type == "private")
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
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
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    try:
        await callback.message.edit_text("🔐 <b>Admin Panel</b>", reply_markup=get_admin_keyboard())
    except Exception:
        await callback.answer()


@dp.callback_query(F.data == "admin_custom_emoji")
async def cb_admin_custom_emoji(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
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
    if callback.from_user.id != ADMIN_ID:
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
    if message.from_user.id != ADMIN_ID:
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
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    groups = await get_raid_groups()
    listing = "\n".join(
        f"• {html.escape(group['title'])}" for group in groups
    ) or "Hali guruh tanlanmagan."
    await callback.message.edit_text(
        "🛡 <b>Raid uchun ruxsatli guruhlar</b>\n\n"
        f"{listing}\n\n"
        "Faqat admin userbot akkauntida ban huquqi bor guruhlarni qo'shish mumkin.",
        reply_markup=get_admin_raid_groups_keyboard(groups)
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_raid_add_group")
async def cb_admin_raid_add_group(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    if str(ADMIN_ID) not in userbot_clients:
        await callback.answer("Avval admin userbot akkauntini ulang.", show_alert=True)
        return
    await callback.message.edit_text(
        "Guruhning @username, t.me havolasi yoki ID'sini yuboring. "
        "Admin akkauntida ban huquqi tekshiriladi.",
        reply_markup=back_kb("admin_raid_groups")
    )
    await state.set_state(UserStatesGroup.admin_raid_add_group)


@dp.message(StateFilter(UserStatesGroup.admin_raid_add_group), F.text)
async def admin_raid_add_group_input(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    client = userbot_clients.get(str(ADMIN_ID))
    if not client:
        await message.answer("❌ Avval admin userbot akkauntini ulang.")
        await state.clear()
        return
    try:
        entity = await client.get_entity(message.text.strip())
        is_group = isinstance(entity, Chat) or bool(getattr(entity, "megagroup", False))
        if not is_group:
            await message.answer("❌ Bu oddiy guruh yoki superguruh emas.")
            return
        me = await client.get_me()
        permissions = await client.get_permissions(entity, me)
        if not has_ban_rights(permissions):
            await message.answer("❌ Admin akkauntida bu guruhda ban huquqi yo'q.")
            return
        chat_id = str(get_peer_id(entity))
        title = get_display_name(entity) or str(chat_id)
    except Exception as exc:
        log.warning("Raid guruhini tekshirishda xato: %s", exc)
        await message.answer(
            "❌ Guruhni ochib bo'lmadi. Akkaunt guruhga a'zo va ban huquqiga ega ekanini tekshiring."
        )
        return

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO raid_groups (chat_id, title, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title",
            (chat_id, title, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()
    await state.clear()
    groups = await get_raid_groups()
    await message.answer(
        f"✅ <b>{html.escape(title)}</b> raid guruhlariga qo'shildi.",
        reply_markup=get_admin_raid_groups_keyboard(groups)
    )


@dp.callback_query(F.data.startswith("admin_raid_del:"))
async def cb_admin_raid_del(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
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
    if callback.from_user.id != ADMIN_ID:
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
    if callback.from_user.id == ADMIN_ID:
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
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    try:
        index = int(callback.data.rsplit(":", 1)[1])
        target_key = _button_emoji_target_keys[index]
    except (ValueError, IndexError):
        await callback.answer("❌ Tugma ro'yxati eskirgan. Qaytadan oching.", show_alert=True)
        return
    client = userbot_clients.get(str(ADMIN_ID))
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
    if callback.from_user.id != ADMIN_ID:
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
    if callback.from_user.id != ADMIN_ID:
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
    if callback.from_user.id != ADMIN_ID:
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
    if callback.from_user.id != ADMIN_ID:
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
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.edit_text(
        "➕ Yangi kanal username kiriting (masalan: @my_channel):",
        reply_markup=back_kb("admin_channels")
    )
    await state.set_state(UserStatesGroup.admin_add_channel)

@dp.message(StateFilter(UserStatesGroup.admin_add_channel), F.text)
async def admin_add_channel_input(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    raw = message.text.strip()
    if not raw.startswith("@"):
        raw = "@" + raw
    url = f"https://t.me/{raw.lstrip('@')}"
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO sub_channels VALUES (?, ?, ?)",
            (raw, url, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()
    await state.clear()
    await message.answer(f"✅ <b>{raw}</b> kanali qo'shildi!", reply_markup=get_admin_keyboard())

@dp.callback_query(F.data.startswith("del_channel:"))
async def cb_del_channel(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
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
    if callback.from_user.id != ADMIN_ID:
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
    if callback.from_user.id != ADMIN_ID:
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
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.edit_text(
        "⭐ PRO berish uchun foydalanuvchi ID va kun sonini kiriting:\n"
        "Format: <code>ID kun</code>\nMasalan: <code>123456789 7</code>",
        reply_markup=back_kb("admin_panel")
    )
    await state.set_state(UserStatesGroup.admin_give_pro)

@dp.message(StateFilter(UserStatesGroup.admin_give_pro), F.text)
async def admin_give_pro_input(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
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
        new_pro = (base_time + timedelta(days=days)).isoformat()
        await db.execute(
            "UPDATE users SET pro_until = ?, notified_10m = 0 WHERE id = ?",
            (new_pro, target_id)
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
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.edit_text(
        "📣 Barcha foydalanuvchilarga yuboriladigan xabarni kiriting:",
        reply_markup=back_kb("admin_panel")
    )
    await state.set_state(UserStatesGroup.admin_broadcast)

@dp.message(StateFilter(UserStatesGroup.admin_broadcast), F.text)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
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

# ── Admin: konkurs yaratish ──
@dp.callback_query(F.data == "admin_contest_create")
async def cb_contest_create(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await callback.message.edit_text(
        "🎉 <b>Konkurs yaratish</b>\n\n"
        "1️⃣ Konkurs o'tkaziladigan kanal yoki guruh username ini kiriting:\n"
        "(masalan: <code>@mening_kanalim</code>)",
        reply_markup=back_kb("admin_panel")
    )
    await state.set_state(UserStatesGroup.contest_channel)

@dp.message(StateFilter(UserStatesGroup.contest_channel), F.text)
async def contest_channel_input(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    raw = message.text.strip()
    if not raw.startswith("@"):
        raw = "@" + raw
    await state.update_data(contest_channel=raw)
    await message.answer(
        "2️⃣ Nechta ishtirokchi to'lganda g'olib tanlansin?\n"
        "(masalan: <code>100</code>)"
    )
    await state.set_state(UserStatesGroup.contest_max_users)

@dp.message(StateFilter(UserStatesGroup.contest_max_users), F.text)
async def contest_max_input(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
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
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip()
    if text.lower() in ("yo'q", "yoq", "no", "-"):
        req_channels = []
    else:
        req_channels = [c.strip() if c.strip().startswith("@") else "@" + c.strip()
                        for c in text.split("\n") if c.strip()]
    await state.update_data(contest_req_channels=req_channels)
    await message.answer(
        "4️⃣ Sovrin matnini kiriting (konkurs xabarida ko'rinadi):\n\n"
        "Masalan: <i>G'olib 500,000 so'm pul mukofoti oladi!</i>"
    )
    await state.set_state(UserStatesGroup.contest_prize_text)

@dp.message(StateFilter(UserStatesGroup.contest_prize_text), F.text)
async def contest_prize_input(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    channel     = data["contest_channel"]
    max_u       = data["contest_max"]
    req_ch_list = data.get("contest_req_channels", [])
    prize_text  = message.text.strip()
    req_ch_str  = ",".join(req_ch_list) if req_ch_list else ""

    # Konkurs xabarini kanalga yubor
    req_ch_display = "\n".join(f"• {c}" for c in req_ch_list) if req_ch_list else "Yo'q"
    contest_text = (
        f"🎉 <b>KONKURS BOSHLANDI!</b>\n\n"
        f"🏆 Sovrin: {prize_text}\n\n"
        f"👥 Kerakli ishtirokchilar soni: <b>{max_u}</b>\n"
        f"📢 Majburiy kanallar:\n{req_ch_display}\n\n"
        f"✅ Ishtirok etish uchun pastdagi tugmani bosing!"
    )

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🎟 Ishtirok etish", callback_data="join_contest_PLACEHOLDER"))

    # Avval bot kanalda bor-yo'qligini tekshiramiz
    try:
        chat = await bot.get_chat(channel)
        real_chat_id = str(chat.id)
    except Exception as e:
        await state.clear()
        await message.answer(
            f"❌ Kanal topilmadi: <code>{e}</code>\n\n"
            "Tekshiring:\n"
            "• Username to'g'rimi? (masalan: <code>@kanalim</code>)\n"
            "• Bot kanalda member sifatida bormi?",
            reply_markup=get_admin_keyboard()
        )
        return

    # Kanalga xabar yuborish
    try:
        sent = await bot.send_message(real_chat_id, contest_text, reply_markup=kb.as_markup())
        message_id = str(sent.message_id)

        # DB ga saqlash
        async with aiosqlite.connect(DB_FILE) as db:
            cur = await db.execute(
                "INSERT INTO contests (chat_id, message_id, prize_text, max_users, req_channels, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?)",
                (real_chat_id, message_id, prize_text, max_u, req_ch_str,
                 datetime.now(timezone.utc).isoformat())
            )
            contest_id = cur.lastrowid
            await db.commit()

        # Tugmani contest_id bilan yangilash
        kb2 = InlineKeyboardBuilder()
        kb2.row(InlineKeyboardButton(
            text="🎟 Ishtirok etish",
            callback_data=f"join_contest:{contest_id}"
        ))
        await bot.edit_message_reply_markup(
            chat_id=real_chat_id,
            message_id=int(message_id),
            reply_markup=kb2.as_markup()
        )

        await state.clear()
        await message.answer(
            f"✅ Konkurs #{contest_id} <b>{channel}</b> kanaliga yuborildi!\n\n"
            f"👥 Kerakli: {max_u} ishtirokchi\n"
            f"🏆 Sovrin: {prize_text}",
            reply_markup=get_admin_keyboard()
        )
    except Exception as e:
        await state.clear()
        await message.answer(
            f"❌ Kanalga xabar yuborib bo'lmadi!\n\n"
            f"Telegram xatosi: <code>{e}</code>\n\n"
            "Tekshiring:\n"
            "• Bot kanalda <b>admin</b> sifatida qo'shilganmi?\n"
            "• Adminga <b>«Xabar yuborish»</b> ruxsati berilganmi?\n"
            "• Kanal shaxsiy (private) bo'lsa, username emas, ID kerak",
            reply_markup=get_admin_keyboard()
        )

# ── Admin: aktiv konkurslar ro'yxati ──
@dp.callback_query(F.data == "admin_contests_list")
async def cb_admin_contests(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
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
    if callback.from_user.id != ADMIN_ID:
        return
    contest_id = int(callback.data.split(":")[1])
    await pick_winner(contest_id)
    await callback.answer("✅ G'olib tanlandi!", show_alert=True)
    # Ro'yxatni yangilash
    await cb_admin_contests(callback)

# ── Admin: konkursni bekor qilish ──
@dp.callback_query(F.data.startswith("contest_cancel:"))
async def cb_contest_cancel(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
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
            f"Mukofotni olish uchun {ADMIN_USERNAME} ga murojaat qiling."
        )
    except Exception:
        pass

    # Adminga xabar
    try:
        await bot.send_message(
            ADMIN_ID,
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
    me = await bot.get_me()
    bot_username = me.username or ""

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id, session FROM user_sessions") as cur:
            sessions = await cur.fetchall()

    for uid, session_str in sessions:
        try:
            client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                userbot_clients[uid] = client
                await register_userbot_handlers(client, uid)
            else:
                # Sessiya eskirgan — o'chir
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute("DELETE FROM user_sessions WHERE user_id = ?", (uid,))
                    await db.commit()
        except Exception as e:
            log.error(f"Sessiya yuklashda xato ({uid}): {e}")


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
    await load_existing_sessions()
    register_common_keyboard_buttons()
    asyncio.create_task(pro_expiration_checker())
    asyncio.create_task(bio_watcher())
    asyncio.create_task(health_server())
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
