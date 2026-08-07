"""
bot_info.py
«اطلاعات ربات» — مجموعه‌ی تنظیمات هویتی/کسب‌وکاری (نه تنظیمات فنی حساس) که هم
می‌توانند از .env (config.py) خوانده شوند و هم — بدون نیاز به ری‌دیپلوی —
از پنل ادمین (بخش «ℹ️ اطلاعات ربات») در دیتابیس بازنویسی/ویرایش شوند.

هر کلید ابتدا از جدول settings دیتابیس خوانده می‌شود؛ اگر چیزی برایش ذخیره
نشده باشد (هنوز ادمین ویرایشش نکرده)، مقدار پیش‌فرض از config.py (که خودش از
.env می‌آید) برگردانده می‌شود.

این ماژول مخصوص هویت/برندینگ و اطلاعات تماس کسب‌وکار است (نام ربات، متن
خوش‌آمد، شماره کارت، کانال‌های اجباری، لینک پشتیبانی و ...) — نه اطلاعات
محرمانه‌ی اتصال به سرویس‌های ثالث (مثل رمز پنل مرزبان یا گواهی پاسارگاد) که
همچنان فقط از طریق .env تنظیم می‌شوند.
"""

import json
import logging

import config
import database as db

logger = logging.getLogger(__name__)

_PREFIX = "botinfo_"
_DEFAULT_SUPPORT_URL = "https://t.me/businesss_support"
_CHANNELS_KEY = _PREFIX + "required_channels"

# کلید داخلی -> (مقدار پیش‌فرض ثابت یا None برای گرفتن از config.py، برچسب فارسی برای پنل ادمین)
_FIELDS = {
    "welcome_text": (None, "👋 متن خوش‌آمدگویی /start"),
    "card_number": (None, "💳 شماره کارت (برای پرداخت کارت‌به‌کارت)"),
    "card_holder": (None, "👤 نام صاحب کارت"),
    "support_url": (_DEFAULT_SUPPORT_URL, "👨‍💻 لینک پشتیبانی (آیدی/کانال تلگرام)"),
    "bot_username": (None, "🤖 یوزرنیم ربات (بدون @)"),
    "connection_guide_url": (None, "📘 لینک آموزش اتصال"),
    "order_log_channel_id": (None, "📋 آیدی عددی کانال لاگ سفارش‌ها"),
    "config_name_prefix": ("tg", "🏷 پیشوند نام کانفیگ‌های ساخته‌شده (فقط حروف/عدد انگلیسی و _)"),
}

_DEFAULT_WELCOME_TEXT = (
    "سلام {first_name} 👋 به Business VPN خوش اومدی\n\n"
    "اگر از قطعی، افت سرعت یا پینگ بالا خسته شدی، اینجا می‌تونی سرویس مناسب استفاده روزمره یا گیمینگ بگیری.\n\n"
    "🎁 اولین باره؟\n"
    "روی «تست رایگان» بزن؛ اول کیفیت را ببین، بعد تصمیم بگیر.\n\n"
    "🛒 آماده خریدی؟\n"
    "«خرید اشتراک» را انتخاب کن.\n\n"
    "⭐ برات سواله چطور میتونی به ما اعتماد کنی؟تجربه مشتریان قبل از خرید:\n"
    "@businesss_etemad\n\n"
    "💬 اگر برای انتخاب سرویس سؤال داری:\n"
    "@businesss_support"
)


def _default_for(key: str):
    if key == "card_number":
        return config.CARD_NUMBER
    if key == "card_holder":
        return config.CARD_HOLDER
    if key == "bot_username":
        return config.BOT_USERNAME
    if key == "connection_guide_url":
        return config.CONNECTION_GUIDE_URL
    if key == "order_log_channel_id":
        return str(config.ORDER_LOG_CHANNEL_ID)
    if key == "welcome_text":
        return _DEFAULT_WELCOME_TEXT
    default, _label = _FIELDS.get(key, (None, None))
    return default


def get(key: str) -> str:
    """مقدار مؤثر فعلی یک فیلد «اطلاعات ربات» را برمی‌گرداند: اول از دیتابیس
    (اگر ادمین قبلاً از پنل ذخیره کرده)، وگرنه پیش‌فرض .env/config.py."""
    stored = db.get_setting(_PREFIX + key)
    if stored is not None and stored != "":
        return stored
    default = _default_for(key)
    return default if default is not None else ""


def set(key: str, value: str) -> None:
    if key not in _FIELDS:
        raise ValueError(f"فیلد نامعتبر برای اطلاعات ربات: {key}")
    db.set_setting(_PREFIX + key, value)


def labels() -> dict:
    return {k: v[1] for k, v in _FIELDS.items()}


def all_values() -> dict:
    return {k: get(k) for k in _FIELDS}


def get_welcome_text(first_name: str) -> str:
    template = get("welcome_text") or _DEFAULT_WELCOME_TEXT
    try:
        return template.format(first_name=first_name)
    except Exception:
        # اگر ادمین متنی بدون { } جای‌گذاری شده وارد کرده باشد، همان متن خام برگردانده می‌شود.
        return template


def get_support_url() -> str:
    """لینک پشتیبانی را به‌صورت یک URL معتبر برای دکمه‌ی شیشه‌ای تلگرام برمی‌گرداند.
    اگر ادمین فقط یوزرنیم (مثلاً "@mysupport" یا "mysupport") وارد کرده باشد،
    بدون http/https ذخیره نمی‌شود چون تلگرام برای چنین urlهایی خطای
    BUTTON_URL_INVALID برمی‌گرداند."""
    raw = (get("support_url") or "").strip()
    if not raw:
        return _DEFAULT_SUPPORT_URL
    if raw.startswith(("http://", "https://", "tg://")):
        return raw
    if raw.startswith("@"):
        raw = raw[1:]
    if raw.startswith("t.me/") or raw.startswith("telegram.me/") or raw.startswith("www."):
        return "https://" + raw
    return "https://t.me/" + raw


# ---------------------------------------------------------------------------
# کانال‌های عضویت اجباری — به‌صورت یک آرایه‌ی JSON در همان جدول settings
# ذخیره می‌شود؛ اگر ادمین چیزی تنظیم نکرده باشد، از REQUIRED_CHANNELS در
# config.py (که خودش می‌تواند از .env بیاید) استفاده می‌شود.
# ---------------------------------------------------------------------------
def get_required_channels() -> list:
    stored = db.get_setting(_CHANNELS_KEY)
    if stored:
        try:
            parsed = json.loads(stored)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            logger.exception("خطا در خواندن required_channels ذخیره‌شده در دیتابیس")
    return config.REQUIRED_CHANNELS


def set_required_channels(channels: list) -> None:
    db.set_setting(_CHANNELS_KEY, json.dumps(channels, ensure_ascii=False))


def add_required_channel(channel_id, name: str, url: str) -> None:
    # کانال‌های پیش‌فرض داخل config.py با id عددی (int) ذخیره می‌شوند، در حالی که
    # فرم افزودن از پنل ادمین channel_id را می‌تواند به‌صورت str بفرستد؛ مقایسه‌ی
    # مستقیم بین int و str همیشه False است، پس اینجا همیشه با str() مقایسه می‌کنیم.
    target = str(channel_id)
    channels = get_required_channels()
    channels = [c for c in channels if str(c.get("id")) != target]
    channels.append({"id": channel_id, "name": name, "url": url})
    set_required_channels(channels)


def remove_required_channel(channel_id) -> None:
    # دکمه‌ی حذف توی پنل ادمین، callback_data را به‌صورت رشته‌متن (str) می‌فرستد، درحالی
    # که کانال‌های پیش‌فرض داخل config.py با id عددی (int) ذخیره شده‌اند؛ مقایسه‌ی int != str
    # همیشه True است، پس اینجا با str() مقایسه می‌کنیم تا فارغ از نوع درست حذف شود.
    target = str(channel_id)
    channels = [c for c in get_required_channels() if str(c.get("id")) != target]
    set_required_channels(channels)
