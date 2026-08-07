"""
utils.py
توابع کمکی کوچک و مشترک بین handlerها.
"""

import os
import time
import random
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram.types import FSInputFile

from keyboards import main_reply_keyboard

logger = logging.getLogger(__name__)

# سرور ربات (Render) با ساعت UTC کار می‌کند و همه‌ی رشده‌های زمانی ذخیره‌شده در
# دیتابیس (created_at/expires_at و ...) بر همین اساس هستند؛ برای اینکه چیزی که
# به کاربر نمایش داده می‌شود (نه چیزی که در فاکتورها/شمارش‌معکوس مقایسه می‌شود)
# همیشه بر طبق ساعت تهران باشد، این دو تابع کمکی فقط برای «نمایش» استفاده می‌شوند.
# 🐛 فیکس: قبلاً اینجا از pytz استفاده می‌شد که در requirements.txt نبود و در محیط دیپلوی (Render)
# باعتت ModuleNotFoundError: No module named 'pytz' می‌شد؛ به جای آن از zoneinfo (کتابخانه‌ی استاندارد پایتون 3.9+) استفاده شد تا وابستگی جدیدی لازم نباشد.
TEHRAN_TZ = ZoneInfo("Asia/Tehran")


def now_tehran() -> datetime:
    """اکنون را بر اساس ساعت تهران برمی‌گرداند (فقط برای نمایش به کاربر/ادمین؛
    نه برای مقایسه با زمان‌های ذخیره‌شده در دیتابیس که بر مبنای ساعت UTC سرور هستند)."""
    return datetime.now(timezone.utc).astimezone(TEHRAN_TZ)


def now_tehran_naive() -> datetime:
    """معادل now_tehran اما به‌صورت naive (بدون tzinfo)؛ برای مقایسه/تفریق با
    تاریخ‌های داخلیو‌شده‌ی بدون تایم‌زون (مثلاً تاریخ انقضای سرویس configs.expiry) لازم است."""
    return now_tehran().replace(tzinfo=None)


def utc_str_to_tehran(dt_str: str, fmt_in: str = "%Y-%m-%d %H:%M:%S"):
    """یک رشته‌ی زمانی که با ساعت UTC سرور ذخیره شده (مثل expires_at فاکتورها) را
    به یک datetime بر اساس ساعت تهران تبدیل می‌کند. در صورت خطا None برمی‌گرداند."""
    try:
        dt_utc = datetime.strptime(dt_str, fmt_in).replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(TEHRAN_TZ)
    except Exception:
        return None


_DIGIT_MAP = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹" "٠١٢٣٤٥٦٧٨٩",
    "0123456789" "0123456789",
)

# ---------------------------------------------------------------------------
# قفل سبک برای جلوگیری از اجرای دوباره‌ی یک عملیات مالی/سفارش در بازه‌ی کوتاه.
#
# چرا لازم است؟ اگر ربات به هر دلیلی (مثلاً خوابیدن سرویس رایگان Render) چند
# ثانیه/دقیقه بی‌پاسخ بماند، تلگرام همه‌ی تاچ‌هایی که کاربر پشت سر هم زده را
# صف می‌کند و وقتی ربات بیدار شد، همه را یک‌جا تحویل می‌دهد. هرکدام از این
# callbackهای صف‌شده، یک نسخه‌ی «قدیمی» از پیام (قبل از هر ویرایشی) همراه خودش
# دارد؛ پس چک‌کردن متن پیام (مثلاً «آیا قبلاً تأیید شده؟») برای تشخیص تکراری
# بودن کافی نیست، چون همه‌ی نسخه‌های صف‌شده متن قدیمی یکسانی دارند.
# این قفل با کلید مشخص (کاربر+نوع عملیات) و بدون هیچ await قبل از خودش صدا
# زده می‌شود تا واقعاً به‌عنوان یک بخش اتمیک (غیرقابل‌قطع توسط تسک دیگر asyncio)
# عمل کند.
# ---------------------------------------------------------------------------
_recent_actions: dict[str, float] = {}
_ACTION_COOLDOWN_SECONDS = 15.0


def is_duplicate_action(key: str, cooldown: float = _ACTION_COOLDOWN_SECONDS) -> bool:
    """اگر همین کلید در `cooldown` ثانیه‌ی اخیر پردازش شده باشد True برمی‌گرداند.
    باید همیشه به‌عنوان اولین خط سینک هندلر (قبل از هر await) صدا زده شود."""
    now = time.monotonic()
    last = _recent_actions.get(key)
    _recent_actions[key] = now
    if len(_recent_actions) > 5000:
        cutoff = now - cooldown
        for k in [k for k, t in _recent_actions.items() if t < cutoff]:
            _recent_actions.pop(k, None)
    return last is not None and (now - last) < cooldown


def normalize_digits(text: str) -> str:
    """ارقام فارسی/عربی را به انگلیسی تبدیل می‌کند تا int() و isdigit() درست کار کنند."""
    if not text:
        return text
    return text.translate(_DIGIT_MAP)


# 🐛 فیکس: کیبورد فارسی/عربی گوشی‌ها (به‌خصوص iOS) اغلب هنگام تایپ عدد،
# نویسه‌های نامرئی جهت‌ساز/فرمت‌دهنده (RLM, LRM, ALM, ZWNJ, ZWSP, BOM) را
# قبل/بعد/وسط رقم‌ها اضافه می‌کنند. این نویسه‌ها با چشم دیده نمی‌شوند ولی باعث
# می‌شوند isdigit()/int() آن رشته شکست بخورد.
_INVISIBLE_CHARS_MAP = dict.fromkeys(
    ord(ch) for ch in "\u200b\u200c\u200d\u200e\u200f\u061c\ufeff\u202a\u202b\u202c\u202d\u202e"
)


def clean_numeric_id(text: str) -> str:
    """ورودی مثل آیدی عددی/مبلغ را پاک‌سازی می‌کند: ارقام فارسی/عربی را به انگلیسی
    تبدیل و نویسه‌های نامرئی جهت‌ساز/فرمت‌دهنده (RLM, LRM, ALM, ZWNJ, ZWSP, BOM و...) را حذف
    می‌کند تا isdigit()/int() روی مقداری که کاربران از کی‌بورد‌های فارسی/عربی تایپ می‌کنند
    (مثلاً آیدی عددی ادمین، مبلغ شارج، کد تخفیف) درست کار کند.
    """
    if not text:
        return text
    cleaned = text.translate(_INVISIBLE_CHARS_MAP)
    return normalize_digits(cleaned).strip()


def parse_int_in_range(text: str, min_value: int, max_value: int) -> int | None:
    """متن را به عدد صحیح تبدیل می‌کند و اگر در بازه‌ی مجاز نبود None برمی‌گرداند."""
    if not text:
        return None
    cleaned = normalize_digits(text).strip()
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    if not (min_value <= value <= max_value):
        return None
    return value


# ---------------------------------------------------------------------------
# 🎲 جلوگیری از مسدودی کارت بانکی به‌خاطر واریزی‌های زیاد با مبلغ کاملاً یکسان:
# وقتی خیلی از کاربران مختلف دقیقاً یک مبلغ ثابت (مثلاً همیشه ۵۸,۵۰۰ تومان) را
# به یک کارت شخصی واریز می‌کنند، سیستم‌های تشخیص تقلب بانک این را الگوی
# «پرداخت‌یاری/پول‌شویی» تشخیص می‌دهد و کارت را مسدود می‌کند. برای پیشگیری،
# به‌جای مبلغ ثابت هر پلن/شارژ، هر فاکتور کارت‌به‌کارت مبلغی کمی متفاوت (به‌طور
# رندوم ± ۱۰۰ تا ۱۵۰۰ تومان) نشان می‌دهد؛ همین مبلغِ رندوم‌شده (نه مبلغ پایه)
# در فاکتور ذخیره، به کاربر نمایش داده و برای تشخیص/تأیید رسید استفاده می‌شود.
def apply_random_amount_variation(price: int, min_variation: int = 100, max_variation: int = 1500) -> int:
    """مبلغ نهایی یک فاکتور کارت‌به‌کارت را برمی‌گرداند: مبلغ پایه به‌همراه یک
    افزایش یا کاهش تصادفی بین min_variation و max_variation تومان، تا مبلغ
    واریزی کاربران مختلف برای همان پلن/مبلغ شارژ، کاملاً یکسان نباشد."""
    if price is None or price <= 0:
        return price
    variation = random.randint(min_variation, max_variation)
    # اگر کم‌کردن مبلغ آن را منفی/خیلی کوچک می‌کرد، همیشه رو به بالا رندوم می‌کنیم.
    if random.random() < 0.5 and price - variation > 0:
        variation = -variation
    return price + variation


# ---------------------------------------------------------------------------
# ⏰ مدیریت فاکتور/مهلت پرداخت و نمایش پیشرفت مرحله‌ای در پیام‌های ربات


# ---------------------------------------------------------------------------
# ⏰ مدیریت فاکتور/مهلت پرداخت و نمایش پیشرفت مرحله‌ای در پیام‌های ربات

def format_deadline_time(expires_at: str) -> str:
    """از رشته YYYY-MM-DD HH:MM:SS (که با ساعت UTC سرور ذخیره شده) ساعت:دقیقه را
    بر اساس ساعت تهران برمی‌گرداند (مثلاً برای پیام «فاکتور تا فلان ساعت معتبر است»)."""
    dt_tehran = utc_str_to_tehran(expires_at)
    if dt_tehran is not None:
        return dt_tehran.strftime("%H:%M")
    try:
        return expires_at.split(" ")[1][:5]
    except Exception:
        return ""


def progress_bar(step: int, total: int) -> str:
    """یک نوار پیشرفت ایموجی‌ای ساده برای نمایش مرحله X از Y در پیام‌های مسیر خرید."""
    step = max(1, min(step, total))
    filled = "🟩" * step + "⬜️" * (total - step)
    label = filled + " مرحله " + str(step) + " از " + str(total)
    return label + chr(10) + chr(10)


# ---------------------------------------------------------------------------
# 🧪 تست: نمایش یک استیکر (ویدیویی) درست بالای یک منو، به‌ازای هر مرحله از
# مسیر خرید. هر بار که این تابع دوباره برای همان چت صدا زده شود، آخرین جفت
# «استیکر + پیام منو»یی که خودش قبلاً فرستاده حذف می‌شود و استیکر/منوی جدید
# جای آن‌ها می‌نشیند؛ یعنی همیشه استیکرِ مرحله‌ی فعلی، درست بالای منوی همان
# مرحله دیده می‌شود.
#
# عمداً این وضعیت (شناسه‌ی پیام استیکر/منوی قبلی) در یک دیکشنری ساده در حافظه
# نگه داشته می‌شود، نه در FSMContext؛ چون خیلی از handlerها همین وسط
# state.clear() صدا می‌زنند (برای پاک کردن state/دیتای قبلی) و اگر این
# اطلاعات را داخل state ذخیره می‌کردیم، با هر state.clear() گم می‌شد و دیگر
# نمی‌توانستیم پیام قبلی را برای حذف پیدا کنیم.
_last_sticker_menu: dict[int, dict[str, int | None]] = {}

# ---------------------------------------------------------------------------
# ⚡ کش‌های حافظه‌ای برای رفع کندی: قبلاً هر بار جابه‌جایی بین منوها باعث
# می‌شد (۱) فایل استیکر پیش‌فرض دوباره از روی دیسک به تلگرام آپلود شود و
# (۲) یک کوئری دیتابیس برای بررسی سفارشی‌سازی ادمین اجرا شود (که وقتی
# دیتابیس روی سرویس ابری/Turso است یعنی یک رفت‌وبرگشت شبکه‌ای اضافه). با کش
# کردن نتیجه‌ی این دو در حافظه، این هزینه‌ها از مسیر اصلی هر جابه‌جایی حذف
# می‌شوند. کش سفارشی‌سازی ادمین فقط با صدا زدن invalidate_section_sticker_cache
# (بعد از هر تغییر در پنل ادمین) باطل می‌شود.
_default_sticker_file_id_cache: dict[str, str] = {}
_section_sticker_override_cache: dict[str, dict | None] = {}
_section_sticker_override_cache_loaded: set[str] = set()


def invalidate_section_sticker_cache(section_key: str) -> None:
    """باید بعد از هر تغییر ادمین روی استیکر یک بخش (آپلود/غیرفعال/فعال/ریست)
    صدا زده شود تا کش حافظه‌ای به‌روز شود و تغییر بلافاصله برای کاربران اعمال شود."""
    _section_sticker_override_cache.pop(section_key, None)
    _section_sticker_override_cache_loaded.discard(section_key)


# نگاشت یک کلید کوتاه و معنادار (که در کد handlerها استفاده می‌شود) به نام
# فایل واقعی استیکر روی دیسک (پوشه‌ی stickers/ کنار همین پروژه).
STICKERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stickers")
STICKER_FILES = {
    "free_test": "test.webm",       # دکمه‌ی «🎁 تست رایگان»
    "buy_plans": "service.webm",    # دکمه‌ی «🛒 خرید اشتراک»
    "plan_select": "plan.webm",     # دکمه‌ی «🚀 سرور VIP» یا «🌐 سرور Gaming»
    "custom_build": "make.webm",    # دکمه‌ی «🚀 کانفیگ خودتو بساز»
}

# عنوان فارسی قابل‌نمایش هر بخش، برای استفاده در پنل مدیریت استیکرها (handlers/admin.py).
STICKER_SECTION_LABELS = {
    "start_welcome": "👋 شروع با /start",
    "join_confirmed": "✅ تایید عضویت در کانال‌ها",
    "free_test": "🎁 تست رایگان",
    "buy_plans": "🛒 خرید اشتراک",
    "plan_select": "🚀 انتخاب پلن VIP / Gaming",
    "custom_build": "🛠 بساز کانفیگ خودت",
    "my_configs_empty": "📱 سرویس‌های من (بدون سرویس)",
    "my_configs_has": "📱 سرویس‌های من (دارای سرویس)",
    "wallet": "💰 کیف پول",
    "referral": "👥 دعوت دوستان",
    "profile": "👤 پروفایل من",
    "guides_empty": "📚 راهنما (بدون محتوا)",
    "guides_has": "📚 راهنما (دارای محتوا)",
    "support": "👨‍💻 پشتیبانی",
    "agency_request": "🤝 درخواست نمایندگی",
    "vip_category_list": "🚀 ليست پلان‌های دسته VIP",
    "gaming_category_list": "🌐 ليست پلان‌های دسته Gaming",
    "discount_code_entry": "🎟 ورود کد تخفیف",
    "my_configs_list_empty": "📋 ليست سرویس‌های یک دسته (خالی)",
    "my_configs_list_has": "📋 ليست سرویس‌های یک دسته (دارای سرویس)",
    "config_detail": "📦 جزئیات یک سرویس",
    "config_delete_confirm": "🗑 تایید حذف سرویس",
    "wallet_free": "💰 موجودی آزاد (قابل استفاده)",
    "wallet_locked": "🔒 موجودی مسدود (در انتظار)",
    "wallet_transactions": "📋 تراکنش‌های کیف پول",
    "wallet_charge": "💵 شارژ کیف پول",
    "purchase_history": "🛒 تاریخچه خرید",
    "ticket_write": "✍️ نوشتن پیام تیکت",
    "plan_payment_method": "💳 انتخاب روش پرداخت (خرید پلن)",
    "plan_pay_wallet": "👛 پرداخت با کیف پول (خرید پلن)",
    "plan_pay_online": "🌐 پرداخت آنلاین (خرید پلن)",
    "plan_pay_card": "💳 پرداخت کارت‌به‌کارت (خرید پلن)",
    "cbuild_payment_method": "💳 انتخاب روش پرداخت (بساز کانفیگ خودت)",
    "cbuild_pay_wallet": "👛 پرداخت با کیف پول (بساز کانفیگ خودت)",
    "cbuild_pay_online": "🌐 پرداخت آنلاین (بساز کانفیگ خودت)",
    "cbuild_pay_card": "💳 پرداخت کارت‌به‌کارت (بساز کانفیگ خودت)",
    "walletcharge_method": "💳 انتخاب روش شارژ کیف پول",
    "walletcharge_pay_card": "💳 شارژ با کارت‌به‌کارت",
    "walletcharge_pay_online": "🌐 شارژ آنلاین کیف پول",
    # 🔔 پیام‌های اطلاع‌رسانی (نه منو): این کلیدها استیکر پیش‌فرض ندارند و
    # فقط وقتی ادمین از پنل ادمین برایشان چیزی آپلود/فعال کند نمایش داده
    # می‌شوند (send_notification_sticker پایین همین فایل).
    "notif_personal_message": "✉️ پیام شخصی ادمین به کاربر",
    "notif_broadcast": "📢 پیام همگانی",
    "notif_expiry": "⏰ هشدار پایان سرویس",
    "notif_usage_80": "🔔 هشدار مصرف ۸۰٪ حجم",
    "notif_usage_90": "🔥 هشدار اتمام حجم (۹۰٪)",
    "notif_wallet_charge": "💳 شارژ کیف پول توسط ادمین",
    "notif_service_delivery": "📦 ارسال سرویس توسط ادمین",
    "notif_purchase_approved": "✅ تایید پرداخت کارت‌به‌کارت",
    "notif_receipt_rejected": "❌ رد رسید (خرید/شارژ)",
}


async def _delete_messages_in_background(bot, chat_id: int, message_ids: list[int]) -> None:
    """پیام‌های قدیمی (استیکر/منوی مرحله‌ی قبل) را در پس‌زمینه حذف می‌کند
    تا کاربر منتظر تمام‌شدن حذف نماند و منوی جدید هرچه سریع‌تر ظاهر شود
    (قبلاً این دو delete_message قبل از فرستادن منوی جدید، به‌صورت بلاک‌کننده اجرا
    می‌شدند و کاربر منتظر دو رفت‌وبرگشت اضافه به تلگرام می‌ماند)."""
    for msg_id in message_ids:
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass


def _get_section_sticker_override(sticker_key: str) -> dict | None:
    """نسخه‌ی کش‌شده‌ی db.get_section_sticker؛ قبلاً این کوئری روی هر جابه‌جایی
    منو (حتی وقتی ادمین چیزی سفارشی نکرده بود) اجرا می‌شد و به‌خصوص وقتی دیتابیس
    روی سرویس ابری (Turso) است، کندی محسوسی اضافه می‌کرد."""
    if sticker_key in _section_sticker_override_cache_loaded:
        return _section_sticker_override_cache.get(sticker_key)
    override = None
    try:
        import database as db  # lazy import: از وابستگی حلقوی بین ماژول‌ها جلوگیری می‌شود
        override = db.get_section_sticker(sticker_key)
    except Exception:
        logger.exception("خطا در خواندن تنظیمات استیکر بخش '%s' از دیتابیس", sticker_key)
    _section_sticker_override_cache[sticker_key] = override
    _section_sticker_override_cache_loaded.add(sticker_key)
    return override


async def show_menu_with_sticker(
    bot,
    chat_id: int,
    sticker_key: str | None,
    text: str,
    reply_markup=None,
    parse_mode: str | None = None,
    show_main_keyboard: bool = True,
):
    """یک پیام منوی تازه می‌فرستد (همیشه پیام جدید، نه ویرایش پیام قبلی) و اگر
    sticker_key داده شده باشد، درست بالای همان منو یک استیکر می‌فرستد.

    🚀 پرفورمنس: اگر استیکر پیش‌فرض (غیرسفارشی) باشد، فقط دفعه‌ی اول از روی دیسک
    آپلود می‌شود و file_id برگشتی‌شده تلگرام در حافظه کش می‌شود؛ دفعات بعدی فقط
    همان file_id را می‌فرستد (بدون خواندن دوباره‌ی فایل از دیسک)؛ همین بزرگ‌ترین عامل کندی قبلی بود.

    ✅ منوی دائمی پایین صفحه (main_reply_keyboard) همراه با هر استیکری که اینجا فرستاده
    می‌شود دوباره تازه می‌شود؛ قبلاً فقط در /start فرستاده می‌شد و با حذف همان پیام در
    جابه‌جایی بعدی از دید کاربر گم می‌شد و کاربر مجبور می‌شد دوباره /start بزند.

    ⚠️ show_main_keyboard=False: فقط برای صفحه‌ی «عضویت اجباری در کانال‌ها» (پیش از
    تأیید عضویت) استفاده شود؛ چون هیچ‌کدام از handlerهای منوی پایین صفحه، عضویت
    کاربر را دوباره چک نمی‌کنند، اگر آنجا هم منوی پایین صفحه فعال شود کاربرِ
    هنوز-عضونشده می‌تواند بدون عضویت واقعی از دکمه‌های پایین صفحه استفاده کند.

    پیام‌های قبلی (استیکر/منوی مرحله‌ی قبل) در پس‌زمینه حذف می‌شوند تا کاربر منتظر
    تمام‌شدن حذف نماند و منوی جدید در سریع‌ترین حالت ممکن ظاهر شود. اگر sticker_key
    مقدار None باشد، فقط پیام منو (بدون استیکر جدید) فرستاده می‌شود؛ برای مرحله‌هایی
    که نباید استیکری در آن‌ها نمایش داده شود (مثلاً مرحله‌ی نهایی انتخاب/انجام پرداخت).
    """
    prev = _last_sticker_menu.pop(chat_id, None)
    if prev:
        old_ids = [mid for mid in (prev.get("sticker_msg_id"), prev.get("menu_msg_id")) if mid]
        if old_ids:
            asyncio.create_task(_delete_messages_in_background(bot, chat_id, old_ids))

    new_sticker_msg_id = None
    if sticker_key:
        # ابتدا بررسی می‌شود که آیا ادمین از پنل ادمین برای این بخش چیزی سفارشی کرده
        # (استیکر خاص یا غیرفعال‌سازی کامل)؛ اگر چیزی سفارشی نشده باشد، از استیکر
        # پیش‌فرض داخل پروژه استفاده می‌شود (از کش در صورت موجود).
        override = _get_section_sticker_override(sticker_key)

        sticker_source = None  # ("file_id", value) یا ("path", value)
        if override is not None:
            if override.get("is_enabled") and override.get("file_id"):
                sticker_source = ("file_id", override["file_id"])
            # اگر ادمین صریحاً این بخش رو غیرفعال کرده باشد (is_enabled=0)، هیچ استیکری نشون داده نمی‌شه.
        else:
            cached_file_id = _default_sticker_file_id_cache.get(sticker_key)
            if cached_file_id:
                sticker_source = ("file_id", cached_file_id)
            else:
                filename = STICKER_FILES.get(sticker_key)
                if filename:
                    sticker_source = ("path", os.path.join(STICKERS_DIR, filename))

        if sticker_source:
            sticker_reply_markup = main_reply_keyboard() if show_main_keyboard else None
            try:
                if sticker_source[0] == "file_id":
                    sticker_msg = await bot.send_sticker(
                        chat_id, sticker=sticker_source[1], reply_markup=sticker_reply_markup,
                    )
                else:
                    sticker_msg = await bot.send_sticker(
                        chat_id, sticker=FSInputFile(sticker_source[1]), reply_markup=sticker_reply_markup,
                    )
                    # فقط برای استیکرهای پیش‌فرض (غیرسفارشی) کش می‌شود تا دفعه‌ی
                    # بعد به‌جای آپلود دوباره‌ی فایل از دیسک، همان file_id تلگرام مستقیم
                    # استفاده شود (بزرگ‌ترین عامل کندی قبلی همین بود).
                    if sticker_msg.sticker and sticker_msg.sticker.file_id:
                        _default_sticker_file_id_cache[sticker_key] = sticker_msg.sticker.file_id
                new_sticker_msg_id = sticker_msg.message_id
            except Exception:
                logger.exception("خطا در ارسال استیکر تست '%s'", sticker_key)

    menu_msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    _last_sticker_menu[chat_id] = {"sticker_msg_id": new_sticker_msg_id, "menu_msg_id": menu_msg.message_id}
    return menu_msg


async def send_notification_sticker(bot, chat_id: int, sticker_key: str) -> None:
    """برای پیام‌های اطلاع‌رسانیِ تکی (نه مسیر منو) - مثل پیام شخصی ادمین،
    پیام همگانی، هشدار انقضا/مصرف سرویس، شارژ کیف پول توسط ادمین یا ارسال
    سرویس توسط ادمین - اگر ادمین از پنل ادمین (بخش «مدیریت استیکرها») برای
    همین sticker_key چیزی آپلود و فعال کرده باشد، همان استیکر را درست قبل از
    پیام اصلی می‌فرستد.

    برخلاف show_menu_with_sticker:
    - این کلیدها هیچ استیکر پیش‌فرض پروژه‌ای ندارند (در STICKER_FILES نیستند)؛
      یعنی تا وقتی ادمین چیزی آپلود نکند، هیچ استیکری فرستاده نمی‌شود و هیچ
      رفتار فعلی تغییر نمی‌کند.
    - هیچ پیام قبلی حذف نمی‌شود و کیبورد پایین صفحه دوباره فرستاده نمی‌شود،
      چون این پیام‌ها مستقل از مسیر منو و در هر لحظه‌ای ممکن است ارسال شوند.
    - هر خطایی (مثلاً کاربر ربات را بلاک کرده) بی‌صدا نادیده گرفته می‌شود تا
      ارسال پیام اصلی بعد از آن هیچ‌وقت به‌خاطر این استیکر متوقف نشود.
    """
    override = _get_section_sticker_override(sticker_key)
    if not override or not override.get("is_enabled") or not override.get("file_id"):
        return
    try:
        await bot.send_sticker(chat_id, sticker=override["file_id"])
    except Exception:
        logger.exception("خطا در ارسال استیکر اطلاع‌رسانی '%s'", sticker_key)
