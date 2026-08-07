"""
alerts.py
بررسی دوره‌ای مصرف و تاریخ انقضای سرویس‌های VIP و اطلاع‌رسانی خودکار به کاربر
وقتی ۸۰٪/۹۰٪ حجم مصرف شده یا ۲ روز به پایان سرویس مانده است.
این هشدارها فقط مخصوص سرویس‌های VIP هستند (طبق درخواست کاربر).
"""
import logging

import crypto
import database as db
import bot_info
from subscription import fetch_subscription_info, usage_bar, days_remaining
from keyboards import back_button
from utils import send_notification_sticker

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 1800  # هر ۳۰ دقیقه یک بار


async def check_usage_alerts(bot):
    """روی همه‌ی سرویس‌های VIP فعال حلقه می‌زند و در صورت لزوم هشدار می‌فرستد."""
    configs = db.get_active_vip_configs()
    for cfg in configs:
        try:
            await _check_single_config(bot, cfg)
        except Exception:
            logger.exception("خطا در بررسی هشدار مصرف برای سرویس %s", cfg.get("id"))


async def _check_single_config(bot, cfg):
    try:
        sub_link = crypto.decrypt_config(cfg["config"])
    except Exception:
        return
    if not sub_link.lower().startswith(("http://", "https://")):
        return

    usage = await fetch_subscription_info(sub_link)
    if not usage:
        return

    user = db.get_user_by_id(cfg["user_id"])
    if not user:
        return

    total = usage.get("total")
    used = (usage.get("upload") or 0) + (usage.get("download") or 0)

    if total:
        percent = min(100, round(used / total * 100))
        if percent >= 90 and not cfg.get("alert_90_sent"):
            await _send_usage_alert(bot, user, cfg, percent)
            db.set_config_alert_sent(cfg["id"], "alert_90_sent")
            db.set_config_alert_sent(cfg["id"], "alert_80_sent")
        elif percent >= 80 and not cfg.get("alert_80_sent"):
            await _send_usage_alert(bot, user, cfg, percent)
            db.set_config_alert_sent(cfg["id"], "alert_80_sent")

    expire_ts = usage.get("expire")
    if expire_ts:
        remaining = days_remaining(expire_ts)
        if remaining is not None and 0 <= remaining <= 2 and not cfg.get("alert_expiry_sent"):
            await _send_expiry_alert(bot, user, cfg, remaining)
            db.set_config_alert_sent(cfg["id"], "alert_expiry_sent")


async def _send_usage_alert(bot, user, cfg, percent):
    bar = usage_bar(percent)
    text = (
        "🔔 هشدار حجم مصرفی سرویس\n\n"
        f"📦 {cfg['plan']}\n\n"
        f"{bar}\n"
        f"✅ شما تا الان {percent}٪ از حجم سرویستون رو مصرف کردید.\n\n"
        "برای جلوگیری از قطعی سرویس، پیشنهاد می‌کنیم همین الان تمدید کنید 🔁"
    )
    sticker_key = "notif_usage_90" if percent >= 90 else "notif_usage_80"
    await _safe_send(bot, user, cfg, text, sticker_key=sticker_key)


async def _send_expiry_alert(bot, user, cfg, remaining):
    days_text = "امروز به پایان می‌رسه" if remaining == 0 else f"فقط {remaining} روز دیگه مونده"
    text = (
        "⏰ هشدار پایان سرویس\n\n"
        f"📦 {cfg['plan']}\n\n"
        f"🟨 سرویس شما {days_text}!\n\n"
        "برای جلوگیری از قطعی، همین الان تمدید کنید 🔁"
    )
    await _safe_send(bot, user, cfg, text, sticker_key="notif_expiry")


async def _safe_send(bot, user, cfg, text, sticker_key: str | None = None):
    try:
        if sticker_key:
            await send_notification_sticker(bot, int(user["telegram_id"]), sticker_key)
        await bot.send_message(
            int(user["telegram_id"]), text,
            reply_markup=back_button(f"viewconfig_{cfg['id']}", "📦 مشاهده سرویس"),
        )
    except Exception:
        logger.exception("ارسال هشدار مصرف به کاربر %s ناموفق بود", user.get("telegram_id"))


# ---------------------------------------------------------------------------
# 🛎 لاگ همه‌ی سفارش‌های نهایی‌شده (خرید/تمدید/تست رایگان/سرویس سفارشی) در
# کانال «اعتماد»، با قالب ثابت.
# ---------------------------------------------------------------------------
def _mask_telegram_id(telegram_id) -> str:
    """آیدی عددی را برای حفظ حریم خصوصی، در پیام کانال اعتماد به‌شکل ماسک‌شده
    نمایش می‌دهد؛ مثلاً 6512345515 → 65*****515 (۲ رقم اول + ۳ رقم آخر باقی می‌مانند)."""
    s = str(telegram_id or "-")
    if len(s) <= 5:
        return s
    return s[:2] + "*" * (len(s) - 5) + s[-3:]


async def log_order_to_channel(
    bot,
    *,
    order_label: str,
    user: dict,
    username: str | None,
    service_id: str | None,
    service_name: str | None,
    package_text: str,
    amount_text: str,
    expiry_text: str,
):
    from utils import now_tehran

    text = (
        f"{order_label}\n"
        f"👤 مشتری: {user.get('name', '-')}\n"
        f"🆔 Telegram ID: {_mask_telegram_id(user.get('telegram_id'))}\n"
        f"👤 نام سرویس: {service_name or '-'}\n"
        f"📦 بسته: {package_text}\n"
        f"💰 مبلغ: {amount_text}\n"
        f"📅 انقضا: {expiry_text}\n"
        f"⏰ زمان: {now_tehran().strftime('%Y-%m-%d %H:%M')} (به وقت تهران)"
    )
    try:
        await bot.send_message(int(bot_info.get("order_log_channel_id")), text)
    except Exception:
        logger.exception("ارسال لاگ سفارش به کانال اعتماد ناموفق بود")


async def fetch_username(bot, telegram_id) -> str | None:
    try:
        chat = await bot.get_chat(int(telegram_id))
        return chat.username
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 💳 مانیتورینگ سلامت درگاه پرداخت آنلاین (یونیک‌پی)
# پولر هر ۲۰ ثانیه وضعیت اینوویس‌های در انتظار را چک می‌کند؛ اگر خودِ درگاه
# قطعی/کند باشد، این چک‌ها پشت‌سرهم fail می‌شوند ولی قبلاً هیچ‌جا به ادمین
# اطلاع داده نمی‌شد (فقط لاگ Render که با هر ری‌استارت پاک می‌شود). این بخش
# نرخ fail را در یک بازه‌ی زمانی می‌سنجد و در صورت عبور از آستانه، یک‌بار به
# ادمین پیام می‌دهد (با cooldown تا اسپم نشود).
# ---------------------------------------------------------------------------
import time as _time

UNIQUEPAY_ALERT_COOLDOWN_SECONDS = 30 * 60  # حداقل ۳۰ دقیقه بین دو هشدار مشابه
UNIQUEPAY_FAILURE_RATE_THRESHOLD = 0.5      # اگر بیش از ۵۰٪ چک‌های یک چرخه fail شوند
UNIQUEPAY_MIN_SAMPLE = 3                    # حداقل تعداد نمونه برای معنادار بودن نرخ

_uniquepay_state = {
    "last_check_alert_at": 0.0,
    "last_create_alert_at": 0.0,
    "create_fail_streak": 0,
}


async def report_uniquepay_check_cycle(bot, admin_id, checked: int, failed: int):
    """بعد از هر چرخه‌ی کامل پولر (بررسی همه‌ی اینوویس‌های در انتظار) صدا زده
    می‌شود. اگر نرخ خطا از آستانه بیشتر باشد، یک هشدار (با cooldown) می‌فرستد."""
    if checked < UNIQUEPAY_MIN_SAMPLE or failed == 0:
        return
    rate = failed / checked
    if rate < UNIQUEPAY_FAILURE_RATE_THRESHOLD:
        return

    now = _time.time()
    if now - _uniquepay_state["last_check_alert_at"] < UNIQUEPAY_ALERT_COOLDOWN_SECONDS:
        return
    _uniquepay_state["last_check_alert_at"] = now

    text = (
        "⚠️ هشدار درگاه پرداخت آنلاین (یونیک‌پی)\n\n"
        f"در آخرین چرخه‌ی بررسی، {failed} از {checked} چک وضعیت اینوویس ({round(rate * 100)}٪) "
        "با خطا مواجه شد.\n\n"
        "احتمالاً یونیک‌پی قطعی یا کند شده. تا رفع مشکل، بهتره کاربرها رو به پرداخت "
        "کارت‌به‌کارت یا کیف پول راهنمایی کنی.\n\n"
        "(این هشدار حداکثر هر ۳۰ دقیقه یک‌بار فرستاده می‌شود.)"
    )
    try:
        await bot.send_message(admin_id, text)
    except Exception:
        logger.exception("ارسال هشدار قطعی یونیک‌پی به ادمین ناموفق بود")


async def report_uniquepay_create_failure(bot, admin_id):
    """هر بار که ساخت اینوویس (create_invoice) برای یک کاربر شکست بخورد صدا
    زده می‌شود. بعد از ۳ شکست پشت‌سرهم (بدون هیچ موفقیت میانی)، یک هشدار
    می‌فرستد؛ با موفقیت بعدی، شمارنده صفر می‌شود."""
    _uniquepay_state["create_fail_streak"] += 1
    if _uniquepay_state["create_fail_streak"] < 3:
        return

    now = _time.time()
    if now - _uniquepay_state["last_create_alert_at"] < UNIQUEPAY_ALERT_COOLDOWN_SECONDS:
        return
    _uniquepay_state["last_create_alert_at"] = now

    text = (
        "⚠️ هشدار درگاه پرداخت آنلاین (یونیک‌پی)\n\n"
        f"{_uniquepay_state['create_fail_streak']} کاربر پشت‌سرهم موفق به ساخت لینک پرداخت آنلاین نشدند "
        "و به کارت‌به‌کارت هدایت شدند.\n\n"
        "احتمالاً یونیک‌پی قطعی یا کند شده. بد نیست پنل یونیک‌پی رو چک کنی.\n\n"
        "(این هشدار حداکثر هر ۳۰ دقیقه یک‌بار فرستاده می‌شود.)"
    )
    try:
        await bot.send_message(admin_id, text)
    except Exception:
        logger.exception("ارسال هشدار قطعی یونیک‌پی به ادمین ناموفق بود")


def report_uniquepay_create_success():
    _uniquepay_state["create_fail_streak"] = 0

