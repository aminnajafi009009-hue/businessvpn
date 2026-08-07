"""
bot.py
فایل اصلی اجرای ربات. تمام Routerهای پوشه‌ی handlers اینجا به Dispatcher
وصل می‌شوند. یک سرور Flask کوچک هم کنارش اجرا می‌شود تا Render سرویس را
"زنده" تشخیص بدهد (لازمه‌ی سرویس‌های نوع Web Service).
"""

import asyncio
import logging
import os
import threading

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent
from aiogram.exceptions import TelegramBadRequest
from aiogram.dispatcher.middlewares.base import BaseMiddleware

import database as db
import uniquepay
import alerts
import fsm_storage
from config import TOKEN, UNIQUEPAY_ENABLED, ADMIN_ID
from handlers import menu, start, wallet, profile, referral, plans, ticket, admin, panel_admin
from handlers.plans import finalize_online_payment, finalize_custom_online_payment
from handlers.wallet import finalize_wallet_charge_online_payment
from alerts import check_usage_alerts, CHECK_INTERVAL_SECONDS
from sentry_setup import init_sentry

# 🆕 ادغام تک-سرویسی: به‌جای اجرای bot.py و webapp_api.py به‌صورت دو Web Service
# جدا روی Render (که با محدودیت زمانی/تعداد دیپلوی رایگان گیت‌هاب مشکل ایجاد
# می‌کرد)، اپ Flask کامل Mini App (تمام endpointهای /api/...) همینجا ایمپورت
# و در همان ترد Flask زیر اجرا می‌شود؛ یعنی با یک‌بار دیپلوی (یک Web Service)،
# هم ربات (long polling، در event loop اصلی) و هم بک‌اند Mini App (در یک ترد
# جدا) هر دو زنده و فعال می‌شوند و هر دو به همان یک آدرس Render متصل‌اند.
from webapp_api import app as flask_app

init_sentry()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class BlockedUserMiddleware(BaseMiddleware):
    """اعمال مسدودی روی تمام پیام‌ها و callbackها، نه فقط /start."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and user.id != ADMIN_ID and db.is_user_blocked(user.id):
            text = "🚫 دسترسی شما به ربات مسدود شده است. با پشتیبانی در ارتباط باشید."
            if getattr(event, "answer", None):
                try:
                    if event.__class__.__name__ == "CallbackQuery":
                        await event.answer(text, show_alert=True)
                    else:
                        await event.answer(text)
                except Exception:
                    logger.exception("خطا در اعلام مسدودی به کاربر")
            return None
        return await handler(event, data)


# ---------------------------------------------------------------------------
# هندلر سراسری خطا — تا حالا اگر یک خطای پیش‌بینی‌نشده (باگ) وسط پردازش یک
# callback (مثلاً دکمه‌ی پرداخت) رخ می‌داد، چون هیچ‌جا callback.answer() صدا
# زده نمی‌شد، دکمه برای کاربر برای همیشه در حالت لودینگ/فریز می‌ماند و نه
# پیامی به کاربر می‌رسید و نه به ادمین — و ردیابی علتش هم سخت بود چون فقط در
# لاگ‌ها گم می‌شد. این هندلر تضمین می‌کند که:
# ۱) خطا با جزئیات کامل (traceback) در لاگ ثبت شود تا بعداً قابل پیگیری باشد.
# ۲) کاربر همیشه یک پیام/آلارم دریافت کند و دکمه از حالت لودینگ خارج شود،
#    به‌جای این‌که تا ابد فریز بماند.
# ---------------------------------------------------------------------------
async def global_error_handler(event: ErrorEvent):
    # 🐛 فیکس: خطای کاملاً بی‌ضرر و متداول تلگرام “محتوا تفاوتی ندارد” (message is not modified) را باید به‌طور جدایی مدیریت کرد: وقتی یک دکمه دوبار زده می‌شود یا handler دوباره همان متن/دکمه را edit می‌کند، تلگرام این خطا را برمی‌گرداند ولی هیچ مشکلی برای کاربر رخ نداده؛ قبلاً هم به لاگ و هم Sentry به‌عنوان خطای جدی ثبت می‌شد و به کاربر هم پیام خطای گمراه‌کننده نمایش داده می‌شد.
    exc = event.exception
    if isinstance(exc, TelegramBadRequest) and "message is not modified" in str(exc).lower():
        logger.info("نادیده‌گرفتن خطای بی‌ضرر message-is-not-modified (کلیک دوباره/محتوای یکسان)")
        try:
            update = event.update
            if update.callback_query:
                try:
                    await update.callback_query.answer()
                except Exception:
                    pass
        except Exception:
            pass
        return True

    logger.exception(
        "خطای پیش‌بینی‌نشده هنگام پردازش آپدیت: %s", event.exception, exc_info=event.exception
    )
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(event.exception)
    except Exception:
        pass
    try:
        import traceback as _tb
        db.log_error(
            error_type=type(event.exception).__name__,
            message=str(event.exception),
            traceback_text="".join(_tb.format_exception(type(event.exception), event.exception, event.exception.__traceback__)),
            context="global_error_handler",
        )
    except Exception:
        pass
    update = event.update
    warning_text = "⚠️ خطایی پیش آمد. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید."
    try:
        if update.callback_query:
            try:
                await update.callback_query.answer(warning_text, show_alert=True)
            except Exception:
                # اگر این callback قبلاً یک‌بار answer شده باشد (مثلاً با پیام
                # "در حال دریافت..." قبل از شروع کار اصلی)، تلگرام دیگر اجازه‌ی
                # answer دوباره را نمی‌دهد و این except صدا زده می‌شود. قبلاً
                # همین‌جا خطا فقط لاگ می‌شد و کاربر هیچ پیامی نمی‌دید (دکمه فقط
                # در حالت لودینگ می‌ماند). حالا به‌جای answer، مستقیماً یک پیام
                # در همون چت می‌فرستیم تا کاربر همیشه یک نتیجه ببیند.
                logger.warning("امکان answer دوباره‌ی callback نبود؛ ارسال پیام مستقیم به چت.")
                if update.callback_query.message:
                    await update.callback_query.message.answer(warning_text)
        elif update.message:
            await update.message.answer(warning_text)
    except Exception:
        logger.exception("خطا حتی در تلاش برای اطلاع‌رسانی خطای اصلی به کاربر")
    return True


# ---------------------------------------------------------------------------
# Flask - همان اپ webapp_api (بک‌اند کامل Mini App) + یک مسیر health-check
# ساده برای اینکه Render سرویس را "زنده" تشخیص بدهد (لازمه‌ی Web Serviceها).
# ---------------------------------------------------------------------------
@flask_app.route("/")
def health_check():
    return "Bot + Mini App API هر دو در حال اجرا هستند ✅"


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    # threaded=True: تا درخواست‌های هم‌زمان Mini App (مثلاً چند کاربر) یکدیگر را بلاک نکنند
    # و همزمان با پاسخگویی ربات (در event loop اصلی asyncio) پردازش شوند.
    flask_app.run(host="0.0.0.0", port=port, threaded=True)


# ---------------------------------------------------------------------------
# aiogram - ربات اصلی
# ---------------------------------------------------------------------------
async def run_bot():
    db.init_db()
    recovered = db.recover_stuck_online_payments()
    if recovered:
        logger.warning("%d پرداخت processing قدیمی برای پردازش مجدد بازیابی شد.", recovered)
    logger.info("Database initialized.")

    bot = Bot(token=TOKEN)
    # 🐛 فیکس: قبلاً MemoryStorage (فقط RAM) بود، با هر ری‌استارت state گم می‌شد.
    dp = Dispatcher(storage=fsm_storage.DBStorage())
    dp.errors.register(global_error_handler)
    blocked_middleware = BlockedUserMiddleware()
    dp.message.outer_middleware(blocked_middleware)
    dp.callback_query.outer_middleware(blocked_middleware)

    fsm_storage.storage = dp.storage

    # ترتیب ثبت Routerها مهم است: handler خاص‌تر باید زودتر بیاید.
    # menu (منوی پایین صفحه) باید همیشه اول باشد تا دکمه‌های ثابت پایین صفحه
    # در هر شرایطی (حتی وسط یک FSM دیگر) همیشه در دسترس و فعال باشند.
    # admin باید بعد از آن باشد چون فیلتر سخت‌گیرانه‌تری (ADMIN_ID) دارد
    # و برخی callback_dataهای مشترک (مثل state یکسان) را زودتر می‌گیرد.
    dp.include_router(menu.router)
    dp.include_router(admin.router)
    dp.include_router(panel_admin.router)
    dp.include_router(start.router)
    dp.include_router(wallet.router)
    dp.include_router(profile.router)
    dp.include_router(referral.router)
    dp.include_router(plans.router)
    dp.include_router(ticket.router)

    asyncio.create_task(usage_alert_loop(bot))
    asyncio.create_task(invoice_expiry_loop(bot))
    asyncio.create_task(self_ping_loop())
    if UNIQUEPAY_ENABLED:
        asyncio.create_task(online_payment_poller(bot))

    logger.info("Bot starting polling...")
    await dp.start_polling(bot)


async def usage_alert_loop(bot: Bot):
    """هر ۳۰ دقیقه سرویس‌های VIP را برای هشدار ۸۰٪/۹۰٪ مصرف و ۲ روز به انقضا بررسی می‌کند.
    همچنین کانفیگ‌های منقضی‌شده را به‌صورت خودکار آرشیو می‌کند (deleted=1) تا
    از پنل کاربر حذف شوند."""
    while True:
        try:
            await check_usage_alerts(bot)
        except Exception:
            logger.exception("خطا در بررسی دوره‌ای هشدارهای مصرف/انقضا")
        try:
            archived = db.archive_expired_configs()
            if archived > 0:
                logger.info("کانفیگ‌های منقضی‌شده آرشیو شدند: %d مورد", archived)
        except Exception:
            logger.exception("خطا در آرشیو خودکار کانفیگ‌های منقضی‌شده")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


ONLINE_PAYMENT_POLL_SECONDS = 20


async def online_payment_poller(bot: Bot):
    """هر ۲۰ ثانیه اینوویس‌های در انتظار درگاه یونیک‌پی را چک می‌کند و به‌محض
    پرداخت‌شدن، بدون نیاز به این‌که کاربر دکمه‌ی «بررسی کن» را بزند، سفارش را
    خودکار ثبت کرده و به کاربر و ادمین اطلاع می‌دهد. این همان «تایید خودکار»
    درخواست‌شده برای درگاه پرداخت آنلاین است.
    علاوه‌براین، نرخ خطای هر چرخه را می‌سنجد و اگر درگاه قطعی/کند شده باشد
    (بیشتر چک‌ها fail شوند)، یک‌بار (با cooldown) به ادمین هشدار می‌دهد."""
    while True:
        checked = 0
        failed = 0
        try:
            pending = db.get_pending_online_payments(limit=50)
            for payment in pending:
                checked += 1
                try:
                    invoice = await uniquepay.check_invoice(payment["hash_id"])
                    if invoice and invoice.get("isPaid"):
                        payment_kind = payment.get("kind")
                        if payment_kind == "custom":
                            result = await finalize_custom_online_payment(bot, payment)
                        elif payment_kind == "wallet_charge":
                            result = await finalize_wallet_charge_online_payment(bot, payment)
                        else:
                            result = await finalize_online_payment(bot, payment)
                        if result is None:
                            # یک فراخوانی هم‌زمان دیگر (مثلاً مینی‌اپ) همین الان
                            # در حال پردازش این پرداخت است؛ برای جلوگیری از پیام
                            # تکراری به کاربر، این چرخه کاری انجام نمی‌دهد.
                            continue
                        try:
                            if payment_kind == "wallet_charge":
                                confirm_text = (
                                    f"✅ پرداخت آنلاین شما تأیید شد و کیف پول شما به مبلغ "
                                    f"{payment['price']:,} تومان شارژ شد."
                                )
                            else:
                                confirm_text = (
                                    f"✅ پرداخت آنلاین شما برای «{payment['plan_name']}» تأیید شد "
                                    f"و سفارش ثبت گردید. سرویس شما به‌زودی ارسال می‌شود."
                                )
                            await bot.send_message(int(payment["telegram_id"]), confirm_text)
                        except Exception:
                            logger.exception("ارسال پیام تایید پرداخت خودکار به کاربر ناموفق بود")
                except Exception:
                    failed += 1
                    logger.exception("خطا در بررسی خودکار اینوویس %s", payment.get("hash_id"))
            if checked:
                await alerts.report_uniquepay_check_cycle(bot, ADMIN_ID, checked, failed)
        except Exception:
            logger.exception("خطا در حلقه‌ی پولر پرداخت آنلاین")
        await asyncio.sleep(ONLINE_PAYMENT_POLL_SECONDS)


INVOICE_EXPIRY_POLL_SECONDS = 60


async def invoice_expiry_loop(bot: Bot):
    """هر ۶۰ ثانیه فاکتورهای کارت‌به‌کارت (پلن/بساز-سرویس/کیف‌پول) و پرداخت‌های آنلاین پرداخت‌نشده که مهلت ۳۰ دقیقه‌ایشان تمام شده را حذف می‌کند
    و به کاربر پیامی می‌فرستد که برای دوباره سفارش ثبت کند."""
    while True:
        try:
            expired_invoices = db.expire_due_invoices()
            for inv in expired_invoices:
                try:
                    await bot.send_message(
                        int(inv["telegram_id"]),
                        f"⏰ مهلت ۳۰ دقیقه‌ای پرداخت فاکتور تان برای «{inv['label']}» به پایان رسید و به‌طور خودکار منقضی شد. لطفاً دوباره از منوی سرویس‌ها سفارش تان را ثبت کنید."
                    )
                except Exception:
                    logger.exception("ارسال پیام انقضای فاکتور به کاربر ناموفق بود")
        except Exception:
            logger.exception("خطا در حلقه‌ی انقضای فاکتورها")
        try:
            expired_online = db.expire_due_online_payments()
            for pay in expired_online:
                try:
                    await bot.send_message(
                        int(pay["telegram_id"]),
                        f"⏰ مهلت ۳۰ دقیقه‌ای پرداخت این فاکتور به پایان رسیده و به‌طور خودکار منقضی شد. لطفاً دوباره از منوی سرویس‌ها سفارش تان را ثبت کنید."
                    )
                except Exception:
                    logger.exception("ارسال پیام انقضای پرداخت آنلاین به کاربر ناموفق بود")
        except Exception:
            logger.exception("خطا در حلقه‌ی انقضای پرداخت‌های آنلاین")
        await asyncio.sleep(INVOICE_EXPIRY_POLL_SECONDS)


SELF_PING_INTERVAL_SECONDS = 120  # ۲ دقیقه؛ زیر آستانهٔ خواب ۱۵ دقیقه‌ای Render رایگان


async def self_ping_loop():
    """هر ۲ دقیقه یک درخواست GET به آدرس خودش می‌زند تا در پلن رایگان Render
    (که بعد از ۱۵ دقیقه بی‌فعالیتی سرویس را می‌خواباند) ربات/مینی‌اپ همیشه بیدار بماند.
    آدرس را یا از SELF_PING_URL (اگر دستی ست شده باشد) یا از RENDER_EXTERNAL_URL
    (که خود Render به‌صورت خودکار در Web Serviceها ست می‌کند) می‌خواند.
    اگر هیچکدام تنظیم نشده باشد (مثلاً اجرای محلی/غیرRender)، حلقه بی‌ضرر غیرفعال می‌ماند."""
    ping_url = os.environ.get("SELF_PING_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not ping_url:
        logger.warning("SELF_PING_URL/RENDER_EXTERNAL_URL تنظیم نشده؛ حلقهٔ self-ping غیرفعال ماند.")
        return
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(SELF_PING_INTERVAL_SECONDS)
            try:
                async with session.get(ping_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    logger.info("self-ping انجام شد (وضعیت %s).", resp.status)
            except Exception:
                logger.exception("self-ping ناموفق بود؛ در چرخهٔ بعدی دوباره تلاش می‌شود.")


def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask keep-alive server started.")

    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
