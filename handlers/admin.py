"""
handlers/admin.py
پنل کامل ادمین: آمار، لیست کاربران، جستجوی حرفه‌ای، شارژ کیف پول، ارسال
کانفیگ، تأیید/رد پرداخت کارت‌به‌کارت خرید سرویس، مدیریت تخفیف (ساخت گام‌به‌گام)،
مدیریت دعوت‌ها، پیام همگانی، بکاپ.

تمام handlerهای این فایل فقط برای ADMIN_ID فعال هستند.
"""

from datetime import datetime, timedelta
import asyncio
import html
import json
import logging
import os
import re

from aiogram import Router, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile

import database as db
import crypto
import alerts
import bot_info
from subscription import extract_meta, days_remaining, format_bytes, usage_bar, fetch_subscription_info, format_expire
from utils import parse_int_in_range, is_duplicate_action, now_tehran_naive, clean_numeric_id, STICKER_SECTION_LABELS, STICKER_FILES, STICKERS_DIR, invalidate_section_sticker_cache, send_notification_sticker
from states import AdminStates, UserStates
from config import (
    ADMIN_ID,
    DATABASE_PATH,
    AGENCY_VIP_DISCOUNT_PERCENT,
    REFERRAL_MIN_VOLUME_GB,
    FREE_TEST_PLAN_KEY,
)
from keyboards import (
    admin_panel_menu,
    admin_back_button,
    admin_discount_menu,
    admin_user_actions_keyboard,
    admin_purchase_notify_keyboard,
    admin_userlist_menu,
    admin_custom_order_notify_keyboard,
    admin_gaming_files_done_keyboard,
    gaming_ready_keyboard,
    config_delivery_keyboard,
    admin_services_list_keyboard,
    admin_service_detail_keyboard,
    admin_gaming_files_manage_keyboard,
    admin_order_queue_keyboard,
    admin_clear_orders_confirm_keyboard,
    admin_request_queue_menu,
    admin_pending_receipts_keyboard,
    admin_clear_receipts_confirm_keyboard,
    admin_purge_confirm_keyboard,
    admin_userlist_page_keyboard,
    admin_accounting_keyboard,
    discount_type_keyboard,
    discount_plans_select_keyboard,
    discount_plans_edit_keyboard,
    discount_detail_keyboard,
    discount_delete_confirm_keyboard,
    admin_agency_menu,
    admin_agent_row_keyboard,
    admin_agent_actions_keyboard,
    admin_vip_categories_keyboard,
    admin_vip_category_detail_keyboard,
    admin_vip_plan_detail_keyboard,
    admin_gaming_categories_keyboard,
    admin_gaming_category_detail_keyboard,
    admin_gaming_plan_detail_keyboard,
    admin_pm_cancel_keyboard,
    admin_referrers_page_keyboard,
    admin_referred_detail_keyboard,
    admin_guides_menu,
    admin_guide_detail_keyboard,
    admin_guide_delete_confirm_keyboard,
    admin_guide_cancel_keyboard,
    admin_error_logs_keyboard,
    admin_error_log_detail_keyboard,
    admin_error_logs_clear_confirm_keyboard,
    admin_stickers_menu,
    admin_sticker_detail_keyboard,
    admin_sticker_cancel_keyboard,
    admin_reply_keyboard,
    admin_botinfo_menu,
    admin_botinfo_field_keyboard,
    admin_botinfo_channels_menu,
)

plan_type = db.plan_type  # نسخه‌ی DB-aware (دسته‌بندی‌های VIP را هم می‌شناسد)

router = Router(name="admin")
logger = logging.getLogger(__name__)


def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def _reply_with_user_actions(target, text: str, uid, is_blocked: bool, *, edit: bool = False):
    """پیام همراه با کیبورد اقدامات کاربر (admin_user_actions_keyboard) را می‌فرستد یا ویرایش می‌کند.
    اگر تلگرام به‌خاطر تنظیمات حریم‌خصوصی محدودتر همان کاربر خاص، دکمه‌ی «رفتن به پیوی کاربر»
    (لینک tg://user) را رد کند (خطای BUTTON_USER_PRIVACY_RESTRICTED)، به‌جای کرش کردن کل پیام،
    همان پیام را بدون آن دکمه‌ی خاص دوباره می‌فرستد؛ برای بقیه‌ی کاربران دکمه همچنان نمایش داده می‌شود."""
    try:
        kb = admin_user_actions_keyboard(uid, is_blocked)
        if edit:
            await target.edit_text(text, reply_markup=kb)
        else:
            await target.answer(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "BUTTON_USER_PRIVACY_RESTRICTED" in str(e):
            kb = admin_user_actions_keyboard(uid, is_blocked, show_pm_link=False)
            if edit:
                await target.edit_text(text, reply_markup=kb)
            else:
                await target.answer(text, reply_markup=kb)
        else:
            raise


_RECEIPTS_QUEUE_MARKER = "🧾 رسیدهای در انتظار تایید"


async def _finish_receipt_message(message: types.Message, note: str, queue_refresh=None):
    """پیام رسید (چه پیام متنی معمولی از ربات کلاسیک، چه پیام عکس+کپشن از
    Mini App که دکمه‌ها مستقیم روی خودِ عکس هستند) را با یک خط نتیجه
    (تأیید/رد) نهایی می‌کند و دکمه‌ها را حذف می‌کند.
    توجه: روی پیام‌های عکس‌دار، edit_text خطا می‌دهد (تلگرام برای عکس‌ها
    caption دارد نه text)؛ برای همین باید edit_caption صدا زده شود.

    queue_refresh: یک تابع async بدون آرگومان. اگر همین دکمه‌ی تأیید/رد از
    داخل پیام «صف درخواست‌ها → رسیدهای در انتظار تایید» زده شده باشد (نه از
    پیام تک‌رسیدیِ اصلی)، به‌جای خالی‌کردن دکمه‌های کل لیست، همان لیست
    رفرش می‌شود تا آیتم‌های دیگرِ هنوز-در-انتظار از بین نروند."""
    text_or_caption = message.caption if message.photo else message.text
    if queue_refresh is not None and text_or_caption and text_or_caption.startswith(_RECEIPTS_QUEUE_MARKER):
        await queue_refresh()
        return
    empty_kb = types.InlineKeyboardMarkup(inline_keyboard=[])
    if message.photo:
        await message.edit_caption(caption=(message.caption or "") + note, reply_markup=empty_kb)
    else:
        await message.edit_text((message.text or "") + note, reply_markup=empty_kb)


def _gb_from_bytes(num_bytes) -> float | None:
    if not num_bytes:
        return None
    try:
        return round(int(num_bytes) / (1024 ** 3), 1)
    except (TypeError, ValueError):
        return None


@router.message(Command("admin"))
async def admin_entry(message: types.Message):
    if not _is_admin(message.from_user.id):
        return  # کاربر عادی هیچ پاسخی نمی‌گیرد (نه حتی پیام خطا) - امنیتی
    await message.answer("👨‍💻 پنل مدیریت:", reply_markup=admin_panel_menu(db.is_orders_enabled()))


@router.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text("👨‍💻 پنل مدیریت:", reply_markup=admin_panel_menu(db.is_orders_enabled()))
    await callback.answer()


@router.message(F.text == "📥 صف درخواست‌ها")
async def menu_admin_request_queue(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    order_count = len(db.get_pending_orders(limit=200)) + len(db.get_pending_custom_orders(limit=200))
    receipt_count = len(db.get_pending_receipts(limit=200)) + len(db.get_pending_custom_order_receipts(limit=200))
    await message.answer(
        "📥 صف درخواست‌ها\n\nچه چیزی رو می‌خوای بررسی کنی؟ 👇",
        reply_markup=admin_request_queue_menu(order_count, receipt_count),
    )


@router.message(F.text == "🤝 نمایندگی (تخفیف VIP)")
async def menu_admin_agency(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    agents = db.get_all_agents()
    text = (
        "🤝 هنوز هیچ نماینده‌ای ثبت نشده.\n\nبرای افزودن، دکمه‌ی زیر را بزنید 👇"
        if not agents else
        "🤝 نمایندگان فعلی (تخفیف خودکار روی VIP)\n\nروی هرکدام بزنید تا مثل بخش «کاربران» مدیریتش کنید 👇"
    )
    await message.answer(text, reply_markup=admin_agency_menu(agents))


@router.message(F.text == "🗂 دسته‌بندی‌های VIP")
async def menu_admin_vip_categories(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "🗂 دسته‌بندی‌های VIP\n\n"
        "این دسته‌ها همان چیزی هستند که کاربر موقع «خرید اشتراک → سرور VIP» می‌بیند.\n"
        "برای مدیریت پلن‌های داخل هر دسته، روی آن بزنید 👇",
        reply_markup=admin_vip_categories_keyboard(),
    )


@router.message(F.text == "🎮 دسته‌بندی‌های Gaming")
async def menu_admin_gaming_categories(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "🎮 دسته‌بندی‌های Gaming\n\n"
        "این دسته‌ها همان چیزی هستند که کاربر موقع «خرید اشتراک → سرور Gaming» می‌بیند.\n"
        "برای مدیریت پلن‌های داخل هر دسته، روی آن بزنید 👇",
        reply_markup=admin_gaming_categories_keyboard(),
    )


@router.message(F.text == "🔴 خاموش کردن سفارشات")
async def menu_admin_orders_off(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    db.set_orders_enabled(False)
    users = db.get_all_users()
    sent, failed = 0, 0
    status_msg = await message.answer(f"⏳ در حال اطلاع‌رسانی به {len(users)} کاربر...")
    for u in users:
        try:
            await message.bot.send_message(
                int(u["telegram_id"]),
                "🔴 ربات به دلیل حجم سفارشات بالا موقتاً بسته می‌باشد.\n\nروشن شدن دوباره‌ی آن اطلاع‌رسانی خواهد شد.",
            )
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(f"🔴 بخش سفارشات خاموش شد. اطلاع‌رسانی به {sent} نفر موفق، {failed} نفر ناموفق.")
    await message.answer("👨‍💻 پنل مدیریت:", reply_markup=admin_reply_keyboard(False))


@router.message(F.text == "🟢 روشن کردن سفارشات")
async def menu_admin_orders_on(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    db.set_orders_enabled(True)
    users = db.get_all_users()
    sent, failed = 0, 0
    status_msg = await message.answer(f"⏳ در حال اطلاع‌رسانی به {len(users)} کاربر...")
    for u in users:
        try:
            await message.bot.send_message(
                int(u["telegram_id"]),
                "🟢 ربات مجدداً فعال شد!\n\nبا زدن /start می‌توانید دوباره سفارش ثبت کنید.",
            )
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(f"🟢 بخش سفارشات روشن شد. اطلاع‌رسانی به {sent} نفر موفق، {failed} نفر ناموفق.")
    await message.answer("👨‍💻 پنل مدیریت:", reply_markup=admin_reply_keyboard(True))


def _error_logs_text(logs: list, total: int) -> str:
    if not logs:
        return "🦖 لاگ خطاها (Sentry)\n\n✅ تا این لحظه هیچ خطای ثبت نشده."
    return f"🦖 لاگ خطاها (Sentry) — {total} خطای ثبت‌شده\n\nروی هرکدام بزنید تا جزئیاتش رو ببینید 👇"


async def _open_error_logs(target, edit: bool = False):
    logs = db.get_error_logs(limit=15)
    total = db.count_error_logs()
    text = _error_logs_text(logs, total)
    kb = admin_error_logs_keyboard(logs)
    if edit:
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.message(F.text == "🦖 لاگ خطاها (Sentry)")
async def menu_admin_error_logs(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    await _open_error_logs(message, edit=False)


@router.callback_query(F.data == "errlogrefresh")
async def admin_error_logs_refresh(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _open_error_logs(callback, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("errlogdetail_"))
async def admin_error_log_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    log_id = int(callback.data.split("_", 1)[1])
    log = db.get_error_log(log_id)
    if log is None:
        await callback.answer("❌ یافت نشد.", show_alert=True)
        return
    tb = html.escape(str(log.get("traceback") or "")[:3500])
    text = (
        f"⚠️ {html.escape(str(log['error_type']))}\n"
        f"🕐 {html.escape(str(log.get('occurred_at') or ''))}\n\n"
        f"📝 {html.escape(str(log.get('message') or ''))}\n\n"
        f"<pre>{tb}</pre>"
    )
    await callback.message.edit_text(text, reply_markup=admin_error_log_detail_keyboard())
    await callback.answer()


@router.callback_query(F.data == "errlogclear")
async def admin_error_logs_clear_ask(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🗑 مطمئنید می‌خواهید همه‌ی لاگ ها پاک شوند؟", reply_markup=admin_error_logs_clear_confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "errlogclearconfirm")
async def admin_error_logs_clear_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    db.clear_error_logs()
    await _open_error_logs(callback, edit=True)
    await callback.answer("✅ پاک شد.")


@router.callback_query(F.data == "errlogsentryguide")
async def admin_error_logs_sentry_guide(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🔗 راهنمای فعال‌سازی Sentry\n\n"
        "1️⃣ اگر متن SENTRY_DSN رو در فایل .env تنظیم کرده باشید، خطاها همزمان به داشبورد Sentry هم ارسال می‌شوند.\n"
        "2️⃣ اگر تنظیم نکرده باشید، هیچ ایرادیه‌ای نیست و فقط همین لیست داخل پنل ادمین قابل استفاده است.\n"
        "3️⃣ هر خطای گرفته‌نشده اینجا هم ذخیره می‌شود (حداکثر 500 خطای اخیر).",
        reply_markup=admin_error_log_detail_keyboard(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 📊 آمار
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    text = (
        f"📊 آمار ربات\n\n"
        f"💰 فروش امروز: {db.sales_since(1):,} تومان\n"
        f"💰 فروش هفته: {db.sales_since(7):,} تومان\n"
        f"💰 فروش ماه: {db.sales_since(30):,} تومان\n"
        f"💰 کل فروش: {db.total_sales():,} تومان\n\n"
        f"👥 تعداد کاربران: {db.count_users()}\n"
        f"🟢 کاربران فعال (۳۰ روز اخیر): {db.count_active_users(30)}"
    )
    await callback.message.edit_text(text, reply_markup=admin_back_button())
    await callback.answer()


# ---------------------------------------------------------------------------
# 👥 لیست کاربران
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_userlist")
async def admin_user_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    text = (
        f"👥 مدیریت کاربران\n\n"
        f"👥 کل کاربران ثبت‌نامی: {db.count_users()}\n"
        f"🟢 مشتریانی که خرید داشته‌اند: {db.count_customers()}\n\n"
        f"یکی از گزینه‌های زیر را انتخاب کنید 👇"
    )
    await callback.message.edit_text(text, reply_markup=admin_userlist_menu())
    await callback.answer()


async def _render_userlist_page(callback: types.CallbackQuery, list_kind: str, page: int):
    per_page = 10
    if list_kind == "active":
        users = db.get_customers_page(page, per_page)
        total = db.count_customers()
        title = "🟢 مشتریان فعال (خریدکرده)"
    else:
        users = db.get_all_users_page(page, per_page)
        total = db.count_users()
        title = "👥 کل کاربران"

    has_next = (page + 1) * per_page < total
    if not users and page == 0:
        text = f"{title}\n\nهنوز هیچ کاربری در این لیست نیست."
    else:
        start = page * per_page + 1
        text = f"{title} — {total} نفر (مرتب‌شده بر اساس بیشترین خرید)\nنمایش {start} تا {start + len(users) - 1}:\n\n"
        text += "برای مدیریت هرکدام روی نامش بزن 👇"

    await callback.message.edit_text(
        text, reply_markup=admin_userlist_page_keyboard(users, page, has_next, list_kind)
    )
    await callback.answer()


@router.callback_query(F.data == "admin_userlist_active")
async def admin_userlist_active(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _render_userlist_page(callback, "active", 0)


@router.callback_query(F.data == "admin_userlist_all")
async def admin_userlist_all(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _render_userlist_page(callback, "all", 0)


@router.callback_query(F.data.startswith("userpage_"))
async def admin_userlist_page_nav(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    _, list_kind, page_str = callback.data.split("_")
    await _render_userlist_page(callback, list_kind, int(page_str))


@router.callback_query(F.data.startswith("useropen_"))
async def admin_user_open(callback: types.CallbackQuery):
    """با زدن روی هرکدام از کاربران در لیست، مستقیم وارد صفحه‌ی مدیریت همان کاربر می‌شویم
    (همان صفحه‌ای که از طریق «🔍 جستجوی حرفه‌ای» هم بازش می‌شد)."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    uid = callback.data.replace("useropen_", "")
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    stats = db.get_referral_stats(user["id"])
    text = (
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        f"👛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود: {user['locked_wallet']:,} تومان\n"
        f"🛒 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 عضویت: {user['joined']}\n\n"
        f"🔗 کد دعوت: {user['invite_code']}\n"
        f"👥 دعوت: {stats['invited_count']} | موفق: {stats['successful_invites']}"
    )
    await _reply_with_user_actions(
        callback.message, text, user["telegram_id"], db.is_user_blocked(user["telegram_id"]), edit=True
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 📒 حسابداری کاربر — تراکنش‌ها (کیف پول، خرید، شارژ، منشأ پول) با صفحه‌بندی
# ---------------------------------------------------------------------------
def _tx_type_label(tx_type: str) -> str:
    return {
        "charge": "💳 شارژ (تأیید کارت‌به‌کارت/دستی توسط ادمین)",
        "purchase": "🛒 خرید سرویس (کسر از کیف پول)",
        "referral_locked": "🔒 پاداش دعوت (در انتظار آزادسازی)",
        "referral_release": "🔓 آزادسازی پاداش دعوت",
        "referral_pending": "🔒 پاداش دعوت (در انتظار)",
    }.get(tx_type, f"📄 {tx_type}")


@router.callback_query(F.data.startswith("accounting_"))
async def admin_user_accounting(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    _, uid, page_str = callback.data.split("_")
    page = int(page_str)
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    per_page = 10
    txs = db.get_transactions_page(user["id"], page, per_page)
    has_next = len(txs) == per_page

    text = (
        f"📒 حسابداری کاربر {user['name']} (🆔 {user['telegram_id']})\n\n"
        f"👛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود (پاداش دعوت در انتظار): {user['locked_wallet']:,} تومان\n"
        f"🛒 مجموع خرید: {user['total_purchase']:,} تومان\n\n"
        f"📋 تراکنش‌ها (صفحه {page + 1}):\n\n"
    )
    if not txs:
        text += "— تراکنشی در این صفحه نیست —"
    else:
        for tx in txs:
            sign = "+" if tx["amount"] >= 0 and tx["type"] in ("charge", "referral_release") else "-"
            text += (
                f"{_tx_type_label(tx['type'])}\n"
                f"{sign}{abs(tx['amount']):,} تومان | {tx['status']}\n"
                f"📝 {tx.get('description') or '-'}\n"
                f"🕐 {tx['created_at']}\n\n"
            )

    await callback.message.edit_text(text, reply_markup=admin_accounting_keyboard(uid, page, has_next))
    await callback.answer()


# ---------------------------------------------------------------------------
# 🔍 جستجوی حرفه‌ای
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_search")
async def admin_search_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🔍 آیدی عددی یا کد دعوت کاربر را ارسال کنید:", reply_markup=admin_back_button()
    )
    await state.set_state(AdminStates.waiting_search_user)
    await callback.answer()


@router.message(AdminStates.waiting_search_user)
async def admin_search_result(message: types.Message, state: FSMContext):
    query = message.text.strip()

    user = db.get_user(query) if query.isdigit() else db.get_user_by_invite_code(query)
    if user is None:
        await message.answer("❌ کاربری با این مشخصات یافت نشد.", reply_markup=admin_back_button())
        return

    stats = db.get_referral_stats(user["id"])
    text = (
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        f"👛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود: {user['locked_wallet']:,} تومان\n"
        f"🛒 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 عضویت: {user['joined']}\n\n"
        f"🔗 کد دعوت: {user['invite_code']}\n"
        f"👥 دعوت: {stats['invited_count']} | موفق: {stats['successful_invites']}"
    )
    await _reply_with_user_actions(
        message, text, user["telegram_id"], db.is_user_blocked(user["telegram_id"]), edit=False
    )
    await state.clear()


# ---------------------------------------------------------------------------
# 💳 شارژ کیف پول (تأیید/رد رسید + شارژ دستی)
# دسترسی از طریق «🔍 جستجوی حرفه‌ای» ← دکمه «💰 شارژ دستی» (برای بهینه شدن فضای منو)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("approve_"))
async def approve_charge(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    if is_duplicate_action(f"approvecharge_{callback.data}") or not db.claim_admin_action(f"approvecharge_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return

    _, uid, amount_str = callback.data.split("_")
    amount = int(amount_str)

    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    db.add_to_wallet(user["id"], amount, "شارژ کیف پول (تأیید رسید)")
    try:
        db.resolve_pending_receipt("charge", uid, amount)
    except Exception:
        pass
    await _finish_receipt_message(
        callback.message, "\n\n✅ تأیید و شارژ شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        await send_notification_sticker(callback.bot, int(uid), "notif_wallet_charge")
        await callback.bot.send_message(int(uid), f"✅ شارژ {amount:,} تومانی شما تأیید شد.")
    except Exception:
        pass
    await callback.answer("✅ شارژ شد.")


@router.callback_query(F.data.startswith("reject_"))
async def reject_charge(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    if is_duplicate_action(f"rejectcharge_{callback.data}") or not db.claim_admin_action(f"rejectcharge_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return

    uid = callback.data.replace("reject_", "")
    try:
        db.resolve_pending_receipt("charge", uid)
    except Exception:
        pass
    await _finish_receipt_message(
        callback.message, "\n\n❌ رد شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        await send_notification_sticker(callback.bot, int(uid), "notif_receipt_rejected")
        await callback.bot.send_message(int(uid), "❌ متأسفانه رسید شما تأیید نشد. با پشتیبانی تماس بگیرید.")
    except Exception:
        pass
    await callback.answer("❌ رد شد.")


@router.callback_query(F.data.startswith("custom_"))
async def custom_charge_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    uid = callback.data.replace("custom_", "")
    await state.update_data(charge_target=uid)
    await state.set_state(AdminStates.waiting_custom_amount)
    await callback.message.answer(f"💵 مبلغ شارژ برای کاربر {uid} را به تومان ارسال کنید:")
    await callback.answer()


@router.message(AdminStates.waiting_custom_amount)
async def custom_charge_apply(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد ارسال کنید.")
        return

    data = await state.get_data()
    uid = data.get("charge_target")
    amount = int(message.text)

    user = db.get_user(uid)
    if user is None:
        await message.answer("❌ کاربر یافت نشد.")
        await state.clear()
        return

    db.add_to_wallet(user["id"], amount, "شارژ دستی توسط ادمین")
    try:
        db.resolve_pending_receipt("charge", uid)
    except Exception:
        pass
    await message.answer(f"✅ {amount:,} تومان به کیف پول کاربر {uid} اضافه شد.")
    try:
        await send_notification_sticker(message.bot, int(uid), "notif_wallet_charge")
        await message.bot.send_message(int(uid), f"✅ کیف پول شما {amount:,} تومان شارژ شد.")
    except Exception:
        pass
    await state.clear()


# ---------------------------------------------------------------------------
# 💳 تأیید/رد رسید خرید کارت‌به‌کارت (سرویس)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("approvepay|"))
async def approve_purchase(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    if is_duplicate_action(f"approvepay_{callback.data}") or not db.claim_admin_action(f"approvepay_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return

    _, uid, plan_key, price_str = callback.data.split("|")
    price = int(price_str)
    plan = db.get_effective_plan(plan_key)
    user = db.get_user(uid)
    if user is None or plan is None:
        await callback.answer("❌ کاربر یا پلن یافت نشد.", show_alert=True)
        return

    if plan_key == FREE_TEST_PLAN_KEY and db.has_used_free_test(user["id"]):
        await callback.answer(
            "⚠️ این کاربر قبلاً از «تست رایگان» استفاده کرده؛ هر کاربر فقط یک‌بار می‌تواند این پلن را بگیرد.",
            show_alert=True,
        )
        return

    # 🐛 فیکس: کد تخفیف کارت‌به‌کارت حالا فقط همین‌جا (تأیید ادمین) مصرف
    # می‌شود، نه هنگام ارسال رسید؛ اگر ادمین رد کند سهم کد تخفیف مصرف نمی‌شود.
    pending = None
    try:
        pending = db.find_pending_receipt("plan_card", uid, price)
    except Exception:
        pending = None

    db.record_purchase(user["id"], price, f"خرید {plan['name']} (کارت به کارت)")

    if pending and pending.get("discount_code"):
        try:
            db.use_discount(pending["discount_code"], user["id"])
        except Exception:
            logging.getLogger(__name__).exception("خطا در مصرف کد تخفیف کارت‌به‌کارت")

    if plan.get("volume_gb", 0) >= REFERRAL_MIN_VOLUME_GB:
        try:
            db.complete_referral(user["id"])
        except ValueError:
            pass

    order_id = db.create_order(user["id"], plan_key, plan["name"], plan_type(plan_key), price)
    try:
        db.resolve_pending_receipt("plan_card", uid, price)
    except Exception:
        pass

    await _finish_receipt_message(
        callback.message, "\n\n✅ تأیید شد و خرید ثبت شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        _confirm_text = (
            f"✅ پرداخت شما تأیید شد!\n\n"
            f"📦 {plan['name']}\nسرویس شما به‌زودی ارسال می‌شود."
        )
        # fix: به کاربر بگوییم کد تخفیفش همین تأیید مصرف شده تا گیج نشود چرا دیگر قابل‌استفاده نیست.
        if pending and pending.get("discount_code"):
            _confirm_text += f"\n🎟 کد تخفیف {pending['discount_code']} برای این خرید مصرف شد."
        await send_notification_sticker(callback.bot, int(uid), "notif_purchase_approved")
        await callback.bot.send_message(int(uid), _confirm_text)
    except Exception:
        pass
    await callback.message.answer(
        "📤 برای ارسال کانفیگ این خرید:", reply_markup=admin_purchase_notify_keyboard(uid, plan_key, order_id)
    )
    await callback.answer("✅ تأیید شد.")


@router.callback_query(F.data.startswith("rejectpay|"))
async def reject_purchase(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    if is_duplicate_action(f"rejectpay_{callback.data}") or not db.claim_admin_action(f"rejectpay_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return

    _, uid = callback.data.split("|")
    try:
        db.resolve_pending_receipt("plan_card", uid)
    except Exception:
        pass
    await _finish_receipt_message(
        callback.message, "\n\n❌ رد شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        await send_notification_sticker(callback.bot, int(uid), "notif_receipt_rejected")
        await callback.bot.send_message(int(uid), "❌ متأسفانه رسید پرداخت شما تأیید نشد. با پشتیبانی تماس بگیرید.")
    except Exception:
        pass
    await callback.answer("❌ رد شد.")


# ---------------------------------------------------------------------------
# 🛠 تأیید/رد رسید کارت‌به‌کارت برای «بساز سرویس خودت» / «تمدید سرویس»
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("approvecustom_"))
async def approve_custom_order(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    order_id = int(callback.data.replace("approvecustom_", ""))
    order = db.get_custom_order(order_id)
    if order is None:
        await callback.answer("❌ سفارش یافت نشد.", show_alert=True)
        return
    if order.get("status") != "pending":
        await callback.answer("⚠️ این سفارش قبلاً پردازش شده.", show_alert=True)
        return

    user = db.get_user_by_id(order["user_id"])
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    db.record_purchase(user["id"], order["price"], "خرید سرویس سفارشی (کارت به کارت)")
    db.set_custom_order_status(order_id, "paid")

    if order.get("volume_gb", 0) >= REFERRAL_MIN_VOLUME_GB:
        try:
            db.complete_referral(user["id"])
        except ValueError:
            pass

    await _finish_receipt_message(
        callback.message, "\n\n✅ تأیید شد و خرید ثبت شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        await callback.bot.send_message(
            int(user["telegram_id"]),
            "✅ پرداخت شما تأیید شد!\nسرویس شما به‌زودی ساخته و ارسال می‌شود.",
        )
    except Exception:
        pass
    await callback.message.answer(
        "📤 برای ارسال کانفیگ این سفارش:", reply_markup=admin_custom_order_notify_keyboard(order_id)
    )
    await callback.answer("✅ تأیید شد.")


@router.callback_query(F.data.startswith("rejectcustom_"))
async def reject_custom_order(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    order_id = int(callback.data.replace("rejectcustom_", ""))
    order = db.get_custom_order(order_id)
    if order and order.get("status") != "pending":
        await callback.answer("⚠️ این سفارش قبلاً پردازش شده.", show_alert=True)
        return
    db.set_custom_order_status(order_id, "rejected")
    await _finish_receipt_message(
        callback.message, "\n\n❌ رد شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    if order:
        user = db.get_user_by_id(order["user_id"])
        if user:
            try:
                await callback.bot.send_message(
                    int(user["telegram_id"]), "❌ متأسفانه رسید پرداخت شما تأیید نشد. با پشتیبانی تماس بگیرید."
                )
            except Exception:
                pass
    await callback.answer("❌ رد شد.")


async def _log_fulfilled_order(
    bot, user: dict, *, plan_order_id=None, custom_order_id=None,
    target_config_id=None, service_id=None, service_name: str = "-",
    package_text: str = "-", expiry_text: str = "-",
):
    """پیام لاگ استاندارد سفارش را برای «کانال اعتماد» می‌سازد و ارسال می‌کند."""
    label = "🛒 خرید جدید"
    amount_text = "-"

    if plan_order_id:
        order = db.get_order(plan_order_id)
        if order:
            amount_text = f"{order['price']:,} تومان" if order["price"] else "رایگان"
            if order.get("order_type") == "test":
                label = "🎁 تست رایگان"
            elif target_config_id:
                label = "🔁 تمدید سرویس"
            else:
                label = "🛒 خرید جدید"
    elif custom_order_id:
        order = db.get_custom_order(custom_order_id)
        if order:
            amount_text = f"{order['price']:,} تومان" if order["price"] else "رایگان"
            label = "🔁 تمدید سرویس" if order.get("order_type") == "renew" else "🛠 سرویس سفارشی جدید"
    elif target_config_id:
        label = "🔁 تمدید سرویس"

    username = await alerts.fetch_username(bot, user["telegram_id"])
    await alerts.log_order_to_channel(
        bot,
        order_label=label,
        user=user,
        username=username,
        service_id=service_id,
        service_name=service_name,
        package_text=package_text,
        amount_text=amount_text,
        expiry_text=expiry_text,
    )


# ---------------------------------------------------------------------------
# 📤 ارسال کانفیگ — عکس کیوآرکد + لینک ساب (نام/حجم/مدت به‌صورت خودکار
# از روی خود لینک تشخیص داده می‌شود؛ اگر تشخیص خودکار جواب نداد، به‌صورت
# دستی از ادمین پرسیده می‌شود)
# ---------------------------------------------------------------------------
async def _start_send_flow(callback: types.CallbackQuery, state: FSMContext, uid: str,
                            target_config_id: int | None, order_id: int | None,
                            plan_order_id: int | None = None, hint: str = ""):
    await state.update_data(
        send_target_uid=uid,
        send_target_config_id=target_config_id,
        send_order_id=order_id,
        send_plan_order_id=plan_order_id,
        qr_file_id=None,
    )
    await state.set_state(AdminStates.waiting_send_qr_photo)
    await callback.message.answer(
        f"📤 ارسال کانفیگ برای کاربر {uid}{hint}\n\n📸 اول عکس کیوآرکد سرویس رو ارسال کن:"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sendvip_"))
async def send_config_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    raw = callback.data.replace("sendvip_", "")
    uid, _, plan_order_id = raw.partition("|")
    await _start_send_flow(
        callback, state, uid, target_config_id=None, order_id=None,
        plan_order_id=int(plan_order_id) if plan_order_id else None,
    )


@router.callback_query(F.data.startswith("sendcustomorder_"))
async def send_custom_order_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    order_id = int(callback.data.replace("sendcustomorder_", ""))
    order = db.get_custom_order(order_id)
    if order is None:
        await callback.answer("❌ سفارش یافت نشد.", show_alert=True)
        return

    user = db.get_user_by_id(order["user_id"])
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    hint = f" (سفارش #{order_id} — {order['volume_gb']} گیگ / {order['days']} روز)"
    await _start_send_flow(
        callback, state, user["telegram_id"],
        target_config_id=order["target_config_id"], order_id=order_id, hint=hint,
    )


@router.message(AdminStates.waiting_send_qr_photo, F.photo)
async def send_config_qr_received(message: types.Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(qr_file_id=file_id)
    await state.set_state(AdminStates.waiting_send_qr_link)
    await message.answer("🔗 حالا لینک ساب (Subscription) این سرویس رو ارسال کن:")


@router.message(AdminStates.waiting_send_qr_photo)
async def send_config_qr_wrong_format(message: types.Message):
    await message.answer("📸 لطفاً عکس کیوآرکد سرویس رو ارسال کن (نه متن).")


@router.message(AdminStates.waiting_send_qr_link)
async def send_config_link_received(message: types.Message, state: FSMContext):
    sub_link = (message.text or "").strip()
    if not sub_link.lower().startswith(("http://", "https://")):
        await message.answer("❌ این یک لینک معتبر نیست؛ لطفاً لینک ساب رو با http یا https ارسال کن:")
        return

    data = await state.get_data()
    order_id = data.get("send_order_id")
    order = db.get_custom_order(order_id) if order_id else None

    await message.answer("⏳ در حال تشخیص خودکار اطلاعات از روی لینک...")
    meta = await extract_meta(sub_link)
    userinfo = (meta or {}).get("userinfo") or {}
    fetched_name = (meta or {}).get("name")

    if order:
        # اطلاعات حجم/مدت از خود سفارش (که کاربر برایش پول پرداخت کرده) قابل‌اعتمادتر است
        volume_gb = order["volume_gb"]
        days = order["days"]
        name = fetched_name or order.get("custom_name") or "کاربر"
        await state.update_data(send_volume_gb=volume_gb, send_days=days, send_name=name, send_sub_link=sub_link)
        await _finalize_send(message, state)
        return

    volume_gb = _gb_from_bytes(userinfo.get("total"))
    days = days_remaining(userinfo.get("expire"))

    if volume_gb is not None and days is not None and fetched_name:
        await state.update_data(send_volume_gb=volume_gb, send_days=days, send_name=fetched_name, send_sub_link=sub_link)
        await _finalize_send(message, state)
        return

    # تشخیص خودکار کامل نبود؛ از ادمین می‌خواهیم دستی وارد کند
    await state.update_data(
        send_sub_link=sub_link,
        send_volume_gb=volume_gb,
        send_days=days,
        send_name=fetched_name,
    )
    await state.set_state(AdminStates.waiting_send_qr_manual)
    known = []
    if fetched_name:
        known.append(f"نام: {fetched_name}")
    if volume_gb is not None:
        known.append(f"حجم: {volume_gb} گیگ")
    if days is not None:
        known.append(f"مدت: {days} روز")
    known_text = ("\n✅ همین مقدار از روی لینک تشخیص داده شد: " + " | ".join(known)) if known else ""
    await message.answer(
        "⚠️ تشخیص خودکار کامل از روی این لینک ممکن نشد (احتمالاً این پنل هدر استاندارد ساب رو برنمی‌گردونه)."
        + known_text
        + "\n\nلطفاً این ۳ مورد رو هرکدام در یک خط، به همین ترتیب بفرست:\n"
        "نام کاربری سرویس\nحجم به گیگ (فقط عدد)\nمدت به روز (فقط عدد)\n\nمثال:\naminvpn1\n50\n30"
    )


@router.message(AdminStates.waiting_send_qr_manual)
async def send_config_manual_input(message: types.Message, state: FSMContext):
    lines = [l.strip() for l in (message.text or "").splitlines() if l.strip()]
    if len(lines) < 3:
        await message.answer("❌ باید دقیقاً ۳ خط بفرستی: نام / حجم (گیگ) / مدت (روز). دوباره امتحان کن:")
        return

    name = lines[0]
    volume_gb = parse_int_in_range(lines[1], 1, 100000)
    days = parse_int_in_range(lines[2], 1, 100000)
    if volume_gb is None or days is None:
        await message.answer("❌ خط دوم و سوم باید فقط عدد باشند (حجم و مدت). دوباره امتحان کن:")
        return

    await state.update_data(send_name=name, send_volume_gb=volume_gb, send_days=days)
    await _finalize_send(message, state)


async def _finalize_send(message: types.Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get("send_target_uid")
    qr_file_id = data.get("qr_file_id")
    sub_link = data.get("send_sub_link")
    target_config_id = data.get("send_target_config_id")
    order_id = data.get("send_order_id")
    plan_order_id = data.get("send_plan_order_id")
    name = data.get("send_name") or "کاربر"
    volume_gb = data.get("send_volume_gb")
    days = data.get("send_days")

    user = db.get_user(uid)
    if user is None or qr_file_id is None or sub_link is None:
        await message.answer("❌ مشکلی پیش آمد؛ لطفاً از ابتدا (📸 عکس کیوآرکد) دوباره امتحان کن.")
        await state.clear()
        return

    volume_text = f"{volume_gb} گیگابایت" if volume_gb is not None else "نامشخص"
    days_text = f"{days} روز" if days is not None else "نامحدود"

    caption = (
        "✅ سرویس با موفقیت ایجاد شد\n\n"
        f"👤 نام کاربری سرویس : {name}\n"
        "🇺🇳 لوکیشن: مولتی لوکیشن+تانل\n"
        f"⏳ مدت زمان: {days_text}\n"
        f"🗜 حجم سرویس: {volume_text}\n"
        "👤 تعداد کاربر:نامحدود\n\n"
        "لینک اتصال:\n"
        f"{sub_link}\n\n"
        "🧑‍🦯 شما میتوانید شیوه اتصال را با فشردن دکمه زیر دریافت کنید."
    )

    expiry_date = None
    if days is not None:
        expiry_date = (now_tehran_naive() + timedelta(days=days)).strftime("%Y-%m-%d")

    encrypted = crypto.encrypt_config(sub_link)
    plan_name = f"{name} | {volume_text} | {days_text}"

    if target_config_id:
        db.update_config(target_config_id, plan_name, encrypted, expiry=expiry_date, qr_file_id=qr_file_id)
    else:
        db.add_config(user["id"], plan_name, encrypted, expiry=expiry_date, config_type="vip", qr_file_id=qr_file_id)

    if order_id:
        db.set_custom_order_status(order_id, "fulfilled")
    if plan_order_id:
        db.set_order_status(plan_order_id, "fulfilled")

    try:
        await send_notification_sticker(message.bot, int(uid), "notif_service_delivery")
        await message.bot.send_photo(
            int(uid),
            qr_file_id,
            caption=caption,
            reply_markup=config_delivery_keyboard(bot_info.get("connection_guide_url")),
        )
        await message.answer("✅ کانفیگ برای کاربر ارسال شد.")
    except Exception as e:
        await message.answer(f"⚠️ سرویس ذخیره شد ولی ارسال پیام به کاربر ناموفق بود: {e}")

    await _log_fulfilled_order(
        message.bot, user, plan_order_id=plan_order_id, custom_order_id=order_id,
        target_config_id=target_config_id, service_name=name,
        package_text=f"{volume_text} | {days_text}", expiry_text=expiry_date or "نامحدود",
    )

    await state.clear()


# ---------------------------------------------------------------------------
# 🎮 ارسال کانفیگ گیمینگ (WireGuard) — بدون کیوآرکد/mirroring/تمدید:
# ادمین ابتدا شناسه سرویس و لینک ساب را وارد می‌کند، سپس هر تعداد فایل
# .conf که بخواهد آپلود می‌کند (هرکدام می‌تواند کپشن/لوکیشن جدا داشته باشد)
# و در پایان با زدن دکمه «✅ پایان ارسال فایل‌ها» همه‌ی فایل‌ها یک‌جا برای
# کاربر ارسال می‌شوند - دقیقاً مطابق فرمت فایلی که در کانال نمونه دیده می‌شود.
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("sendgaming_"))
async def send_gaming_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    raw = callback.data.replace("sendgaming_", "")
    uid, _, plan_order_id = raw.partition("|")
    await state.update_data(
        gaming_target_uid=uid, gaming_files=[],
        gaming_plan_order_id=int(plan_order_id) if plan_order_id else None,
    )
    await state.set_state(AdminStates.waiting_gaming_service_id)
    await callback.message.answer(
        f"🎮 ارسال کانفیگ گیمینگ برای کاربر {uid}\n\n"
        f"🆔 اول شناسه سرویس رو ارسال کن:"
    )
    await callback.answer()


@router.message(AdminStates.waiting_gaming_service_id)
async def gaming_service_id_received(message: types.Message, state: FSMContext):
    service_id = (message.text or "").strip()
    if not service_id:
        await message.answer("❌ شناسه سرویس نمی‌تواند خالی باشد؛ دوباره ارسال کن:")
        return

    await state.update_data(gaming_service_id=service_id)
    await state.set_state(AdminStates.waiting_gaming_sub_link)
    await message.answer("🔗 حالا لینک ساب (Subscription) این سرویس رو ارسال کن:")


@router.message(AdminStates.waiting_gaming_sub_link)
async def gaming_sub_link_received(message: types.Message, state: FSMContext):
    sub_link = (message.text or "").strip()
    if not sub_link:
        await message.answer("❌ لینک ساب نمی‌تواند خالی باشد؛ دوباره ارسال کن:")
        return

    await state.update_data(gaming_sub_link=sub_link)
    await state.set_state(AdminStates.waiting_gaming_files)
    await message.answer(
        "✅ عالی! حالا فایل‌های کانفیگ (.conf) رو یکی‌یکی ارسال کن.\n"
        "می‌تونی برای هر فایل، همراه با خودِ فایل یک کپشن (مثلاً نام لوکیشن) هم بفرستی.\n\n"
        "وقتی همه‌ی فایل‌ها رو فرستادی، دکمه‌ی زیر رو بزن 👇",
        reply_markup=admin_gaming_files_done_keyboard(),
    )


@router.message(AdminStates.waiting_gaming_files, F.document)
async def gaming_file_received(message: types.Message, state: FSMContext):
    data = await state.get_data()
    files = data.get("gaming_files", [])
    files.append({
        "file_id": message.document.file_id,
        "file_name": message.document.file_name,
        "caption": (message.caption or "").strip() or None,
    })
    await state.update_data(gaming_files=files)
    await message.answer(
        f"✅ فایل «{message.document.file_name}» ثبت شد. ({len(files)} فایل تا الان)\n"
        f"فایل بعدی رو بفرست یا دکمه‌ی «✅ پایان ارسال فایل‌ها» رو بزن.",
        reply_markup=admin_gaming_files_done_keyboard(),
    )


@router.message(AdminStates.waiting_gaming_files)
async def gaming_file_wrong_format(message: types.Message):
    await message.answer(
        "📎 لطفاً فایل کانفیگ (.conf) رو به‌صورت داکیومنت ارسال کن، یا اگر تمومه دکمه‌ی "
        "«✅ پایان ارسال فایل‌ها» رو بزن."
    )


@router.callback_query(AdminStates.waiting_gaming_files, F.data == "gamingfiles_done")
async def gaming_files_done(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    data = await state.get_data()
    uid = data.get("gaming_target_uid")
    service_id = data.get("gaming_service_id")
    sub_link = data.get("gaming_sub_link")
    files = data.get("gaming_files", [])
    plan_order_id = data.get("gaming_plan_order_id")

    user = db.get_user(uid) if uid else None
    if user is None or not service_id or not sub_link:
        await callback.answer("❌ مشکلی پیش آمد؛ لطفاً از ابتدا دوباره امتحان کن.", show_alert=True)
        await state.clear()
        return

    if not files:
        await callback.answer("❌ هنوز هیچ فایلی ارسال نکردی. حداقل یک فایل بفرست.", show_alert=True)
        return

    encrypted_sub = crypto.encrypt_config(sub_link)
    plan_name = f"گیمینگ | شناسه: {service_id}"
    config_id = db.add_config(
        user["id"], plan_name, encrypted_sub, expiry=None,
        config_type="gaming", service_id=service_id,
    )
    for f in files:
        db.add_gaming_file(config_id, f["file_id"], f["file_name"], f["caption"])

    if plan_order_id:
        db.set_order_status(plan_order_id, "fulfilled")

    ready_text = (
        "✅ کانفیگ شما آماده شد!\n\n"
        f"🆔 شناسه سرویس: {service_id}\n\n"
        f"🔗 لینک ساب (Subscription) شما؛ می‌توانید حجم مصرفی‌تان را از داخل آن مدیریت کنید:\n\n"
        f"`{sub_link}`\n\n"
        "برای دریافت فایل‌های کانفیگ سرویس، دکمه‌ی زیر رو بزن 👇"
    )
    try:
        await send_notification_sticker(callback.bot, int(uid), "notif_service_delivery")
        await callback.bot.send_message(
            int(uid),
            ready_text,
            parse_mode="Markdown",
            reply_markup=gaming_ready_keyboard(config_id),
        )
        await callback.message.answer(
            f"✅ سرویس گیمینگ ثبت شد و پیام آماده‌سازی برای کاربر ارسال شد. ({len(files)} فایل ذخیره شد)"
        )
    except Exception as e:
        await callback.message.answer(f"⚠️ سرویس ذخیره شد ولی ارسال پیام به کاربر ناموفق بود: {e}")

    await _log_fulfilled_order(
        callback.bot, user, plan_order_id=plan_order_id, service_id=service_id,
        service_name=plan_name, package_text="گیمینگ (WireGuard)", expiry_text="نامحدود",
    )

    await state.clear()
    await callback.answer()


# ---------------------------------------------------------------------------
# 📥 صف سفارشات — لیست خریدهای تأییدشده‌ای که هنوز کانفیگ‌شان ارسال نشده،
# چه خرید پلن معمولی (VIP/گیمینگ) و چه سفارش سفارشی/تمدید.
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_orders_off")
async def admin_orders_off(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    db.set_orders_enabled(False)
    users = db.get_all_users()
    sent, failed = 0, 0
    status_msg = await callback.message.answer(f"⏳ در حال اطلاع‌رسانی به {len(users)} کاربر...")
    for u in users:
        try:
            await callback.bot.send_message(
                int(u["telegram_id"]),
                "🔴 ربات به دلیل حجم سفارشات بالا موقتاً بسته می‌باشد.\n\nروشن شدن دوباره‌ی آن اطلاع‌رسانی خواهد شد.",
            )
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(f"🔴 بخش سفارشات خاموش شد. اطلاع‌رسانی به {sent} نفر موفق، {failed} نفر ناموفق.")
    await callback.message.edit_text("👨‍💻 پنل مدیریت:", reply_markup=admin_panel_menu(db.is_orders_enabled()))
    await callback.answer()


@router.callback_query(F.data == "admin_orders_on")
async def admin_orders_on(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    db.set_orders_enabled(True)
    users = db.get_all_users()
    sent, failed = 0, 0
    status_msg = await callback.message.answer(f"⏳ در حال اطلاع‌رسانی به {len(users)} کاربر...")
    for u in users:
        try:
            await callback.bot.send_message(
                int(u["telegram_id"]),
                "🟢 ربات مجدداً فعال شد!\n\nبا زدن /start می‌توانید دوباره سفارش ثبت کنید.",
            )
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(f"🟢 بخش سفارشات روشن شد. اطلاع‌رسانی به {sent} نفر موفق، {failed} نفر ناموفق.")
    await callback.message.edit_text("👨‍💻 پنل مدیریت:", reply_markup=admin_panel_menu(db.is_orders_enabled()))
    await callback.answer()


async def _render_order_queue(callback: types.CallbackQuery):
    pending = db.get_pending_orders(limit=25)
    for o in pending:
        u = db.get_user_by_id(o["user_id"])
        o["telegram_id"] = u["telegram_id"] if u else ""
    pending_custom = db.get_pending_custom_orders(limit=25)

    if not pending and not pending_custom:
        text = "📦 سفارش‌های در انتظار\n\n✅ در حال حاضر هیچ سفارش در انتظار ارسالی وجود ندارد."
    else:
        text = f"📦 سفارش‌های در انتظار — {len(pending) + len(pending_custom)} مورد در انتظار ارسال\n\nروی هرکدوم بزن تا مسیر ارسالش شروع بشه 👇"

    await callback.message.edit_text(text, reply_markup=admin_order_queue_keyboard(pending, pending_custom))


@router.callback_query(F.data == "admin_request_queue")
async def admin_request_queue(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    order_count = len(db.get_pending_orders(limit=200)) + len(db.get_pending_custom_orders(limit=200))
    receipt_count = len(db.get_pending_receipts(limit=200)) + len(db.get_pending_custom_order_receipts(limit=200))
    await callback.message.edit_text(
        "📥 صف درخواست‌ها\n\nچه چیزی رو می‌خوای بررسی کنی؟ 👇",
        reply_markup=admin_request_queue_menu(order_count, receipt_count),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_order_queue")
async def admin_order_queue(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _render_order_queue(callback)
    await callback.answer()


# ---------------------------------------------------------------------------
# 🧾 رسیدهای در انتظار تایید — همه‌ی رسیدهای شارژ کیف پول و خرید کارت‌به‌کارت
# (پلن ثابت + بساز سرویس خودت) که هنوز ادمین تایید/رد نکرده، در یک لیست.
# تایید/رد از همینجا دقیقاً همان مسیر همیشگی (پیام فوروارد‌شده در چت ادمین)
# را صدا می‌زند، فقط یک راه میان‌بر برای دیدن همه‌چیز یک‌جاست.
# ---------------------------------------------------------------------------
async def _render_pending_receipts(callback: types.CallbackQuery):
    receipts = db.get_pending_receipts(limit=25)
    custom_receipts = db.get_pending_custom_order_receipts(limit=25)
    for co in custom_receipts:
        u = db.get_user_by_id(co["user_id"])
        co["telegram_id"] = u["telegram_id"] if u else ""

    total = len(receipts) + len(custom_receipts)
    if total == 0:
        text = "🧾 رسیدهای در انتظار تایید\n\n✅ در حال حاضر هیچ رسید در انتظار تاییدی وجود ندارد."
    else:
        text = f"🧾 رسیدهای در انتظار تایید — {total} مورد\n\nروی ✅ برای تایید یا ❌ برای رد بزن 👇"

    await callback.message.edit_text(text, reply_markup=admin_pending_receipts_keyboard(receipts, custom_receipts))


@router.callback_query(F.data == "admin_pending_receipts")
async def admin_pending_receipts(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _render_pending_receipts(callback)
    await callback.answer()


@router.callback_query(F.data == "clearreceipts_confirm")
async def clear_receipts_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "⚠️ مطمئنی می‌خوای همه‌ی رسیدهای این لیست رو بررسی‌شده علامت بزنی؟\n"
        "(توجه: این کار فقط لیست رو خالی می‌کنه؛ اگه هنوز به کاربری تایید/رد اعلام نکردی، "
        "پیام اصلی رسیدش همچنان توی چتت هست و باید از همونجا اقدام کنی.)",
        reply_markup=admin_clear_receipts_confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "clearreceipts_do")
async def clear_receipts_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    db.dismiss_all_pending_receipts()
    await _render_pending_receipts(callback)
    await callback.answer("🧹 لیست خالی شد.")


@router.callback_query(F.data.startswith("dismissorder_"))
async def dismiss_order(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    order_id = int(callback.data.replace("dismissorder_", ""))
    db.set_order_status(order_id, "dismissed")
    await _render_order_queue(callback)
    await callback.answer("🗑 از صف پاک شد.")


@router.callback_query(F.data.startswith("dismisscustomorder_"))
async def dismiss_custom_order(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    order_id = int(callback.data.replace("dismisscustomorder_", ""))
    db.set_custom_order_status(order_id, "dismissed")
    await _render_order_queue(callback)
    await callback.answer("🗑 از صف پاک شد.")


@router.callback_query(F.data == "clearorders_confirm")
async def clear_orders_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "⚠️ مطمئنی می‌خوای همه‌ی سفارش‌های این صف رو پاک کنی؟\n"
        "(این کار فقط سفارش‌ها رو از صف حذف می‌کنه؛ اگه کانفیگ کسی رو نفرستادی، دیگه اینجا یادآوریش نمی‌مونه.)",
        reply_markup=admin_clear_orders_confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "clearorders_do")
async def clear_orders_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    pending = db.get_pending_orders(limit=1000)
    pending_custom = db.get_pending_custom_orders(limit=1000)
    for o in pending:
        db.set_order_status(o["id"], "dismissed")
    for co in pending_custom:
        db.set_custom_order_status(co["id"], "dismissed")
    await _render_order_queue(callback)
    await callback.answer(f"🧹 {len(pending) + len(pending_custom)} سفارش پاک شد.")


# ---------------------------------------------------------------------------
# 📦 مشاهده و مدیریت سرویس‌های یک کاربر (لیست/حذف نرم/ادیت لینک ساب و کیوآرکد/
# مدیریت فایل‌های گیمینگ) — همه از طریق «🔍 جستجوی حرفه‌ای»
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("useractions_"))
async def admin_user_actions_back(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    uid = callback.data.replace("useractions_", "")
    await _reply_with_user_actions(
        callback.message, f"👤 مدیریت کاربر {uid}", uid, db.is_user_blocked(uid), edit=True
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# ✉️ پیام خصوصی ادمین به یک کاربر خاص (متن/عکس/فیلم/فوروارد)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("pm_"))
async def admin_pm_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    uid = callback.data.replace("pm_", "")
    await state.set_state(AdminStates.waiting_pm_message)
    await state.update_data(pm_target_uid=uid)
    await callback.message.edit_text(
        f"✉️ پیامی که می‌خواهید به کاربر {uid} ارسال شود را بفرستید (متن، عکس، فیلم یا فوروارد هم پذیرفه است):",
        reply_markup=admin_pm_cancel_keyboard(uid),
    )
    await callback.answer()


@router.message(AdminStates.waiting_pm_message)
async def admin_pm_send(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    uid = data.get("pm_target_uid")
    if not uid:
        await state.clear()
        return
    try:
        await send_notification_sticker(message.bot, int(uid), "notif_personal_message")
        await message.bot.copy_message(int(uid), message.chat.id, message.message_id)
        await _reply_with_user_actions(message, "✅ پیام خصوصی برای کاربر ارسال شد.", uid, db.is_user_blocked(uid), edit=False)
    except Exception:
        await _reply_with_user_actions(message, "❌ ارسال پیام ناموفق بود (ممکن است کاربر ربات را مسدود کرده باشد).", uid, db.is_user_blocked(uid), edit=False)
    await state.clear()


# ---------------------------------------------------------------------------
# 🚫 مسدود/رفع مسدودیت کاربر — کاربر‌های مسدود نمی‌توانند از ربات استفاده کنند
# (بررسی در middleware/start.py)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("toggleblock_"))
async def admin_toggle_block(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    uid = callback.data.replace("toggleblock_", "")
    currently_blocked = db.is_user_blocked(uid)
    db.set_user_blocked(uid, not currently_blocked)

    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return
    stats = db.get_referral_stats(user["id"])
    text = (
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        f"💛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود: {user['locked_wallet']:,} تومان\n"
        f"🛍 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 عضویت: {user['joined']}\n\n"
        f"🔗 کد دعوت: {user['invite_code']}\n"
        f"👥 دعوت: {stats['invited_count']} | موفق: {stats['successful_invites']}\n\n"
        + ("🚫 وضعیت: مسدود" if not currently_blocked else "✅ وضعیت: رفع مسدودیت شد")
    )
    await _reply_with_user_actions(callback.message, text, user["telegram_id"], not currently_blocked, edit=True)
    await callback.answer("🚫 کاربر مسدود شد." if not currently_blocked else "✅ مسدودیت برداشته شد.")


@router.callback_query(F.data.startswith("svcs_"))
async def admin_view_user_services(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    uid = callback.data.replace("svcs_", "")
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    configs = db.get_configs(user["id"], include_deleted=True)
    if not configs:
        await callback.message.edit_text(
            f"📦 کاربر {uid} هنوز هیچ سرویسی نداره.",
            reply_markup=admin_back_button(),
        )
    else:
        await callback.message.edit_text(
            f"📦 سرویس‌های کاربر {uid}\n\n❌ یعنی توسط خودِ کاربر حذف شده (ولی برای شما همچنان قابل‌مشاهده‌ست).\n\nروی هرکدوم بزن برای جزئیات و مدیریت 👇",
            reply_markup=admin_services_list_keyboard(configs, uid),
        )
    await callback.answer()


def _remaining_days_from_date_str(date_str) -> int | None:
    """تعداد روز باقی‌مانده تا انقضا را از یک رشته تاریخ به فرمت YYYY-MM-DD محاسبه می‌کند."""
    if not date_str:
        return None
    try:
        exp_date = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        delta = exp_date - now_tehran_naive()
        total_seconds = delta.total_seconds()
        if total_seconds <= 0:
            return 0
        # گرد به بالا تا سرویس‌های فعال با کمتر از ۲۴ ساعت باقیمانده به‌اشتباه «منقضی‌شده» نمایش داده نشوند.
        return -(-int(total_seconds) // 86400)
    except Exception:
        return None


async def _service_detail_text(cfg: dict) -> str:
    """
    توجه: قبلاً این متن با parse_mode="Markdown" (نسخه‌ی قدیمی مارک‌داون
    تلگرام) فرستاده می‌شد و cfg['plan'] بدون هیچ escape‌ای مستقیم داخل متن
    قرار می‌گرفت. نسخه‌ی قدیمی Markdown تلگرام امکان escape کردن کاراکترهای
    خاص رو نداره؛ پس اگر نام پلن یک زیرخط (_) تک و جفت‌نشده داشت (مثل
    "Businesss_vpn - 1090174")، پارسر اون رو شروع ایتالیک در نظر می‌گرفت و
    چون بسته نمی‌شد، کل درخواست ویرایش پیام با خطای "can't parse entities"
    رد می‌شد و صفحه‌ی جزئیات سرویس اصلاً نمایش داده نمی‌شد.
    راه‌حل: استفاده از HTML به‌جای Markdown، چون HTML یک تابع escape رسمی و
    قابل‌اعتماد داره (html.escape) و این مشکل اصلاً پیش نمیاد.
    """
    icon = "🚀" if cfg.get("type", "vip") == "vip" else "🎮"
    status = "❌ حذف‌شده (توسط کاربر یا ادمین)" if cfg.get("deleted") else "✅ فعال"
    try:
        sub_preview = crypto.decrypt_config(cfg["config"])
    except Exception:
        sub_preview = "⚠️ خطا در رمزگشایی"

    plan_safe = html.escape(str(cfg.get("plan", "")))
    created_safe = html.escape(str(cfg.get("created_at", "")))
    sub_preview_safe = html.escape(sub_preview)

    text = (
        f"{icon} {plan_safe}\n\n"
        f"📌 وضعیت: {status}\n"
        f"📆 تاریخ ایجاد: {created_safe}\n"
    )
    if cfg.get("service_id"):
        text += f"🆔 شناسه سرویس: {html.escape(str(cfg['service_id']))}\n"

    usage = None
    sub_url = sub_preview if sub_preview.lower().startswith(("http://", "https://")) else None
    if cfg.get("type", "vip") == "vip" and sub_url:
        try:
            usage = await fetch_subscription_info(sub_url)
        except Exception:
            usage = None

    if usage:
        total = usage.get("total")
        used = (usage.get("upload") or 0) + (usage.get("download") or 0)
        remaining_bytes = (total - used) if total else None

        text += "\n📊 وضعیت مصرف (لحظه‌ای):\n"
        if total:
            text += f"   • حجم کل: {html.escape(format_bytes(total))}\n"
        text += f"   • مصرف‌شده: {html.escape(format_bytes(used))}\n"
        if remaining_bytes is not None:
            text += f"   • باقی‌مانده: {html.escape(format_bytes(remaining_bytes))}\n"
        if total:
            percent = min(100, round(used / total * 100))
            text += f"\n{usage_bar(percent)} {percent}٪ مصرف شده\n"
        text += f"\n⏰ تاریخ انقضا: {html.escape(format_expire(usage.get('expire')))}\n"
        remaining_days = days_remaining(usage.get("expire"))
        if remaining_days is not None:
            text += "⛔️ منقضی شده\n" if remaining_days <= 0 else f"⌛️ زمان باقی‌مانده: {remaining_days} روز\n"
    elif cfg.get("expiry"):
        text += f"⏰ انقضا: {html.escape(str(cfg['expiry']))}\n"
        remaining_days = _remaining_days_from_date_str(cfg.get("expiry"))
        if remaining_days is not None:
            if remaining_days <= 0:
                text += "⌛️ زمان باقی‌مانده: ⛔️ منقضی شده\n"
            else:
                text += f"⌛️ زمان باقی‌مانده: {remaining_days} روز\n"

    text += f"\n🔗 لینک ساب فعلی:\n<code>{sub_preview_safe}</code>\n"
    if cfg.get("type", "vip") == "vip":
        text += f"\n🎫 کیوآرکد: {'ثبت شده ✅' if cfg.get('qr_file_id') else 'ثبت نشده ❌'}"
    else:
        files = db.get_gaming_files(cfg["id"])
        text += f"\n📁 تعداد فایل کانفیگ: {len(files)}"
    return text


async def _render_service_detail(callback: types.CallbackQuery, cfg_id: int):
    cfg = db.get_config_by_id(cfg_id)
    if cfg is None:
        await callback.answer("❌ سرویس یافت نشد.", show_alert=True)
        return
    owner = db.get_user_by_id(cfg["user_id"])
    uid = owner["telegram_id"] if owner else ""

    text = await _service_detail_text(cfg)
    kb = admin_service_detail_keyboard(cfg, uid)
    # -----------------------------------------------------------------
    # علت اصلی خطای "خطایی پیش آمد..." روی این صفحه: نام پلن (cfg['plan'])
    # می‌تونه هرچیزی باشه (مثلاً چیزی شبیه "Businesss_vpn - 1090174") و اگر
    # داخلش یک زیرخط (_) تکی و جفت‌نشده باشه، پارسر Markdown قدیمی تلگرام
    # اون رو شروع ایتالیک در نظر می‌گیره و چون بسته نمی‌شه، کل پیام با خطای
    # "can't parse entities" رد می‌شه. قبلاً هیچ try/except اینجا نبود، پس
    # این خطا مستقیم می‌رفت به هندلر سراسری و کاربر فقط "خطایی پیش آمد" رو
    # می‌دید بدون اینکه جزئیات سرویس اصلاً نمایش داده بشه. حالا اگر مارک‌داون
    # شکست بخوره، همون متن رو بدون فرمت (parse_mode=None) دوباره می‌فرستیم.
    # -----------------------------------------------------------------
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        if "message is not modified" in str(e).lower():
            pass
        else:
            logger.exception("خطا در نمایش جزئیات سرویس ادمین برای cfg_id=%s", cfg_id)
            try:
                await callback.message.edit_text(text, parse_mode=None, reply_markup=kb)
            except Exception:
                logger.exception("خطا در fallback بدون فرمت برای جزئیات سرویس cfg_id=%s", cfg_id)
                await callback.answer("❌ خطا در نمایش جزئیات سرویس. دوباره تلاش کنید.", show_alert=True)
                return
    await callback.answer()


@router.callback_query(F.data.startswith("svcdetail_"))
async def admin_service_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcdetail_", ""))
    await _render_service_detail(callback, cfg_id)


@router.callback_query(F.data.startswith("svcdelete_"))
async def admin_service_delete(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcdelete_", ""))
    db.set_config_deleted(cfg_id, True)
    await _render_service_detail(callback, cfg_id)


@router.callback_query(F.data.startswith("svcrestore_"))
async def admin_service_restore(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcrestore_", ""))
    db.set_config_deleted(cfg_id, False)
    await _render_service_detail(callback, cfg_id)


@router.callback_query(F.data.startswith("svcpurge_"))
async def admin_service_purge_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcpurge_", ""))
    await callback.message.edit_text(
        "⚠️ این کار غیرقابل بازگشته و کل اطلاعات این سرویس (شامل فایل‌های گیمینگ) برای همیشه پاک می‌شه.\n\nمطمئنی؟",
        reply_markup=admin_purge_confirm_keyboard(cfg_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("svcpurgeconfirm_"))
async def admin_service_purge_apply(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcpurgeconfirm_", ""))
    cfg = db.get_config_by_id(cfg_id)
    owner = db.get_user_by_id(cfg["user_id"]) if cfg else None
    uid = owner["telegram_id"] if owner else ""
    db.delete_config_permanently(cfg_id)
    await callback.message.edit_text("✅ سرویس برای همیشه حذف شد.", reply_markup=admin_back_button())
    await callback.answer()


@router.callback_query(F.data.startswith("svcedit_link_"))
async def admin_service_edit_link_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcedit_link_", ""))
    await state.update_data(edit_config_id=cfg_id)
    await state.set_state(AdminStates.waiting_edit_sublink)
    await callback.message.answer("🔗 لینک ساب جدید این سرویس رو ارسال کن:")
    await callback.answer()


@router.message(AdminStates.waiting_edit_sublink)
async def admin_service_edit_link_apply(message: types.Message, state: FSMContext):
    new_link = (message.text or "").strip()
    if not new_link.lower().startswith(("http://", "https://")):
        await message.answer("❌ این یک لینک معتبر نیست؛ لطفاً لینک رو با http یا https ارسال کن:")
        return

    data = await state.get_data()
    cfg_id = data.get("edit_config_id")
    cfg = db.get_config_by_id(cfg_id) if cfg_id else None
    if cfg is None:
        await message.answer("❌ سرویس یافت نشد.")
        await state.clear()
        return

    db.update_config_link(cfg_id, crypto.encrypt_config(new_link))
    await message.answer("✅ لینک ساب سرویس بروزرسانی شد.", reply_markup=admin_back_button())
    await state.clear()


@router.callback_query(F.data.startswith("svcedit_qr_"))
async def admin_service_edit_qr_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcedit_qr_", ""))
    await state.update_data(edit_config_id=cfg_id)
    await state.set_state(AdminStates.waiting_edit_qr)
    await callback.message.answer("🖼 عکس کیوآرکد جدید این سرویس رو ارسال کن:")
    await callback.answer()


@router.message(AdminStates.waiting_edit_qr, F.photo)
async def admin_service_edit_qr_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cfg_id = data.get("edit_config_id")
    cfg = db.get_config_by_id(cfg_id) if cfg_id else None
    if cfg is None:
        await message.answer("❌ سرویس یافت نشد.")
        await state.clear()
        return

    db.set_config_qr(cfg_id, message.photo[-1].file_id)
    await message.answer("✅ عکس کیوآرکد سرویس بروزرسانی شد.", reply_markup=admin_back_button())
    await state.clear()


@router.message(AdminStates.waiting_edit_qr)
async def admin_service_edit_qr_wrong_format(message: types.Message):
    await message.answer("📸 لطفاً عکس کیوآرکد رو ارسال کن (نه متن).")


@router.callback_query(F.data.startswith("svcfiles_"))
async def admin_service_files_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcfiles_", ""))
    files = db.get_gaming_files(cfg_id)
    text = f"📁 فایل‌های کانفیگ این سرویس ({len(files)} فایل)\n\nبرای حذف یک فایل، روش بزن:" if files else \
        "📁 این سرویس هنوز هیچ فایلی نداره."
    await callback.message.edit_text(text, reply_markup=admin_gaming_files_manage_keyboard(cfg_id, files))
    await callback.answer()


@router.callback_query(F.data.startswith("svcfiledel_"))
async def admin_service_file_delete(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    _, file_id_str, cfg_id_str = callback.data.split("_")
    db.delete_gaming_file(int(file_id_str))
    files = db.get_gaming_files(int(cfg_id_str))
    text = f"📁 فایل‌های کانفیگ این سرویس ({len(files)} فایل)\n\nبرای حذف یک فایل، روش بزن:" if files else \
        "📁 این سرویس هنوز هیچ فایلی نداره."
    await callback.message.edit_text(text, reply_markup=admin_gaming_files_manage_keyboard(int(cfg_id_str), files))
    await callback.answer("🗑 حذف شد.")


@router.callback_query(F.data.startswith("svcaddfile_"))
async def admin_service_add_file_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcaddfile_", ""))
    await state.update_data(addfile_config_id=cfg_id, addfile_new_files=[])
    await state.set_state(AdminStates.waiting_add_gaming_file)
    await callback.message.answer(
        "📁 فایل‌های .conf جدید رو یکی‌یکی بفرست (هرکدوم می‌تونه کپشن لوکیشن هم داشته باشه)."
        " وقتی تموم شد دکمه‌ی زیر رو بزن 👇",
        reply_markup=admin_gaming_files_done_keyboard(f"addfiledone_{cfg_id}"),
    )
    await callback.answer()


@router.message(AdminStates.waiting_add_gaming_file, F.document)
async def admin_service_add_file_received(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cfg_id = data.get("addfile_config_id")
    files = data.get("addfile_new_files", [])
    files.append({
        "file_id": message.document.file_id,
        "file_name": message.document.file_name,
        "caption": (message.caption or "").strip() or None,
    })
    await state.update_data(addfile_new_files=files)
    await message.answer(
        f"✅ فایل «{message.document.file_name}» ثبت شد. ({len(files)} فایل تا الان) فایل بعدی رو بفرست یا پایان رو بزن.",
        reply_markup=admin_gaming_files_done_keyboard(f"addfiledone_{cfg_id}"),
    )


@router.message(AdminStates.waiting_add_gaming_file)
async def admin_service_add_file_wrong_format(message: types.Message):
    await message.answer("📎 لطفاً فایل کانفیگ (.conf) رو به‌صورت داکیومنت ارسال کن.")


@router.callback_query(AdminStates.waiting_add_gaming_file, F.data.startswith("addfiledone_"))
async def admin_service_add_file_done(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    data = await state.get_data()
    cfg_id = data.get("addfile_config_id")
    new_files = data.get("addfile_new_files", [])
    cfg = db.get_config_by_id(cfg_id) if cfg_id else None
    if cfg is None:
        await callback.answer("❌ سرویس یافت نشد.", show_alert=True)
        await state.clear()
        return
    if not new_files:
        await callback.answer("❌ هنوز فایلی نفرستادی.", show_alert=True)
        return

    for f in new_files:
        db.add_gaming_file(cfg_id, f["file_id"], f["file_name"], f["caption"])

    owner = db.get_user_by_id(cfg["user_id"])
    if owner:
        try:
            await callback.bot.send_message(
                int(owner["telegram_id"]),
                f"📁 فایل‌های کانفیگ جدید به سرویس «{cfg['plan']}» شما اضافه شد.\n"
                "برای دریافت، وارد «سرویس‌های من» ← «سرویس‌های گیمینگ من» بشو و دکمه‌ی دریافت فایل‌ها رو بزن.",
            )
        except Exception:
            pass

    await callback.message.answer(f"✅ {len(new_files)} فایل جدید به سرویس اضافه و به کاربر اطلاع‌رسانی شد.")
    await state.clear()
    await callback.answer()


# ---------------------------------------------------------------------------
# 🎟 مدیریت تخفیف (ساخت کد تخفیف به‌صورت گام‌به‌گام)
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_discount")
async def admin_discount_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    discounts = db.get_all_discounts()
    if not discounts:
        text = "🎟 هیچ کد تخفیفی هنوز ثبت نشده.\n\nبرای ساخت کد جدید، دکمه‌ی زیر را بزنید 👇"
    else:
        text = "🎟 کدهای تخفیف فعال:\n\nبرای مشاهده و ویرایش جزئیات هر کد، روی آن بزنید 👇"

    await callback.message.edit_text(text, reply_markup=admin_discount_menu(discounts))
    await callback.answer()


def _discount_detail_text(d: dict) -> str:
    if d.get("discount_type") == "amount":
        value_text = f"💵 {d['amount']:,} تومان"
    else:
        value_text = f"💯 {d['percent']}٪"

    plans_raw = d.get("applicable_plans")
    if not plans_raw:
        plans_text = "همه‌ی پلن‌ها"
    else:
        all_plans = db.get_all_plans()
        keys = json.loads(plans_raw)
        plans_text = ", ".join(all_plans.get(k, {}).get("name", k) for k in keys) or "همه‌ی پلن‌ها"

    users_raw = d.get("allowed_user_ids")
    if not users_raw:
        users_text = "همه‌ی کاربران"
    else:
        ids = json.loads(users_raw)
        users_text = "، ".join(f"`{i}`" for i in ids)

    extra = ""
    if d.get("min_order_amount"):
        extra += f"\n💰 حداقل مبلغ سفارش: {d['min_order_amount']:,} تومان"
    if d.get("max_uses_per_user"):
        extra += f"\n🔂 سقف استفاده برای هر کاربر: {d['max_uses_per_user']}"
    if d.get("expires_at"):
        extra += f"\n⏰ انقضا: {d['expires_at']}"

    return (
        f"🎟 کد تخفیف: `{d['code']}`\n"
        f"{value_text}\n"
        f"🔁 تعداد استفاده‌ی باقی‌مانده: {d['uses']}\n"
        f"🎯 پلن‌های مجاز: {plans_text}\n"
        f"👤 کاربران مجاز: {users_text}"
        f"{extra}"
    )


@router.callback_query(F.data.startswith("discdetail_"))
async def admin_discount_detail(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    discount_id = int(callback.data.replace("discdetail_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        _discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("discdelete_"))
async def admin_discount_delete_ask(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discdelete_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        f"❗️ آیا از حذف کد `{d['code']}` مطمئن هستید؟",
        parse_mode="Markdown",
        reply_markup=discount_delete_confirm_keyboard(discount_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("discdeleteconfirm_"))
async def admin_discount_delete_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discdeleteconfirm_", ""))
    db.delete_discount_by_id(discount_id)
    await callback.answer("✅ کد تخفیف حذف شد.", show_alert=True)
    discounts = db.get_all_discounts()
    text = "🎟 کدهای تخفیف فعال:\n\nبرای مشاهده و ویرایش جزئیات هر کد، روی آن بزنید 👇" if discounts else \
        "🎟 هیچ کد تخفیفی هنوز ثبت نشده.\n\nبرای ساخت کد جدید، دکمه‌ی زیر را بزنید 👇"
    await callback.message.edit_text(text, reply_markup=admin_discount_menu(discounts))


# --- ویرایش مقدار تخفیف (درصد/مبلغ) ---
@router.callback_query(F.data.startswith("discedit_value_"))
async def admin_discount_edit_value_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_value_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    label = "درصد جدید را وارد کنید (بین ۱ تا ۱۰۰)" if d.get("discount_type") != "amount" else \
        "مبلغ ثابت جدید را به تومان وارد کنید"
    await callback.message.edit_text(f"✏️ {label}:", reply_markup=admin_back_button())
    await state.set_state(AdminStates.waiting_discount_edit_value)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_value)
async def admin_discount_edit_value_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    d = db.get_discount_by_id(discount_id) if discount_id else None
    if d is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد وارد کنید:")
        return
    value = int(message.text)
    if d.get("discount_type") == "amount":
        if value <= 0:
            await message.answer("❌ مبلغ باید بزرگ‌تر از صفر باشد:")
            return
        db.update_discount(discount_id, amount=value)
    else:
        if not (1 <= value <= 100):
            await message.answer("❌ درصد باید بین ۱ تا ۱۰۰ باشد:")
            return
        db.update_discount(discount_id, percent=value)

    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ مقدار تخفیف بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id))


# --- ویرایش تعداد استفاده‌ی باقی‌مانده ---
@router.callback_query(F.data.startswith("discedit_uses_"))
async def admin_discount_edit_uses_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_uses_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "✏️ تعداد دفعات مجاز باقی‌مانده‌ی این کد را وارد کنید (مثلاً 50):",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_uses)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_uses)
async def admin_discount_edit_uses_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if not message.text or not message.text.isdigit() or int(message.text) < 0:
        await message.answer("❌ لطفاً یک عدد صحیح غیرمنفی وارد کنید:")
        return
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    db.update_discount(discount_id, uses=int(message.text))
    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ تعداد استفاده بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id))


# --- ویرایش حداقل مبلغ سفارش ---
@router.callback_query(F.data.startswith("discedit_minorder_"))
async def admin_discount_edit_minorder_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_minorder_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "✏️ حداقل مبلغ سفارش (به تومان) برای استفاده از این کد را وارد کنید.\n"
        "برای برداشتن محدودیت، عدد 0 را ارسال کنید.",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_min_order)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_min_order)
async def admin_discount_edit_minorder_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد وارد کنید:")
        return
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    db.update_discount(discount_id, min_order_amount=int(message.text))
    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ حداقل مبلغ سفارش بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id))


# --- ویرایش سقف استفاده‌ی هر کاربر ---
@router.callback_query(F.data.startswith("discedit_maxuser_"))
async def admin_discount_edit_maxuser_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_maxuser_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "✏️ سقف تعداد دفعات استفاده‌ی هر کاربر از این کد را وارد کنید.\n"
        "برای بی‌محدودیت‌کردن، عدد 0 را ارسال کنید.",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_max_per_user)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_max_per_user)
async def admin_discount_edit_maxuser_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد وارد کنید:")
        return
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    db.update_discount(discount_id, max_uses_per_user=int(message.text))
    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ سقف استفاده‌ی هر کاربر بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id))


# --- ویرایش تاریخ انقضا ---
@router.callback_query(F.data.startswith("discedit_expiry_"))
async def admin_discount_edit_expiry_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_expiry_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "✏️ تاریخ انقضا را به‌فرمت `YYYY-MM-DD` (مثلاً 2026-12-31) وارد کنید.\n"
        "برای برداشتن انقضا (کد همیشه معتبر باشد)، عدد 0 را ارسال کنید.",
        parse_mode="Markdown",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_expiry)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_expiry)
async def admin_discount_edit_expiry_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    raw = (message.text or "").strip()
    if raw == "0":
        db.update_discount(discount_id, expires_at=None)
    else:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            await message.answer("❌ فرمت نامعتبر است؛ به‌صورت YYYY-MM-DD وارد کنید (یا 0 برای حذف انقضا):")
            return
        db.update_discount(discount_id, expires_at=parsed.strftime("%Y-%m-%d 23:59:59"))

    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ تاریخ انقضا بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id))


# --- ویرایش کاربران مجاز ---
@router.callback_query(F.data.startswith("discedit_users_"))
async def admin_discount_edit_users_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_users_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "👤 آیدی‌های عددی تلگرام مجاز به استفاده از این کد را وارد کنید "
        "(هرکدام با کاما، فاصله یا خط جدید جدا شود).\n\n"
        "برای برداشتن محدودیت (باز کردن کد برای همه‌ی کاربران) عدد 0 را ارسال کنید.",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_users)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_users)
async def admin_discount_edit_users_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    raw = (message.text or "").strip()
    ids = [p for p in re.split(r"[\s,،]+", raw) if p]

    if not ids or ids == ["0"]:
        db.update_discount(discount_id, allowed_user_ids=None)
        summary = "بدون محدودیت (همه‌ی کاربران)"
    else:
        if not all(p.lstrip("-").isdigit() for p in ids):
            await message.answer("❌ فقط آیدی‌های عددی معتبر وارد کنید (یا 0 برای باز کردن برای همه):")
            return
        ids = sorted(set(ids))
        db.update_discount(discount_id, allowed_user_ids=ids)
        summary = "، ".join(f"`{i}`" for i in ids)

    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer(f"✅ کاربران مجاز بروزرسانی شد: {summary}", parse_mode="Markdown", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id))


# --- ویرایش پلن‌های مجاز ---
@router.callback_query(F.data.startswith("discedit_plans_"))
async def admin_discount_edit_plans_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_plans_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    selected = db._discount_plans(d) or []
    await state.update_data(edit_discount_id=discount_id, edit_discount_plans=selected)
    await callback.message.edit_text(
        "🎯 پلن‌های مجاز برای این کد را انتخاب کنید (هرکدام را بزنید تا انتخاب/لغو شود):",
        reply_markup=discount_plans_edit_keyboard(discount_id, selected),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("discplaned_"))
async def admin_discount_edit_plans_toggle(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    # discplaned_<id>_<key|all|done>
    rest = callback.data.replace("discplaned_", "")
    discount_id_str, _, key = rest.partition("_")
    discount_id = int(discount_id_str)
    data = await state.get_data()
    selected = data.get("edit_discount_plans", [])

    if key == "all":
        selected = []
    elif key == "done":
        db.update_discount(discount_id, applicable_plans=selected or None)
        await state.clear()
        d = db.get_discount_by_id(discount_id)
        await callback.message.edit_text(
            _discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id)
        )
        await callback.answer("✅ پلن‌های مجاز ذخیره شد.")
        return
    else:
        selected = [p for p in selected if p != key] if key in selected else selected + [key]

    await state.update_data(edit_discount_plans=selected)
    await callback.message.edit_reply_markup(reply_markup=discount_plans_edit_keyboard(discount_id, selected))
    await callback.answer()


@router.callback_query(F.data == "new_discount")
async def new_discount_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "🎟 ساخت کد تخفیف جدید — مرحله ۱ از ۶\n\n"
        "✏️ کد تخفیف مورد نظر را بدون فاصله وارد کنید (مثلاً SUMMER20):",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_code_step)
    await callback.answer()


@router.message(AdminStates.waiting_discount_code_step)
async def new_discount_code_input(message: types.Message, state: FSMContext):
    code = message.text.strip().upper() if message.text else ""
    if not code or " " in code:
        await message.answer("❌ کد نامعتبر است؛ بدون فاصله دوباره وارد کنید:")
        return
    await state.update_data(new_discount_code=code)
    await message.answer(
        "🎟 مرحله ۲ از ۶\n\nنوع تخفیف را انتخاب کنید:", reply_markup=discount_type_keyboard()
    )


@router.callback_query(F.data.startswith("disctype_"), AdminStates.waiting_discount_code_step)
async def new_discount_type_chosen(callback: types.CallbackQuery, state: FSMContext):
    disc_type = callback.data.replace("disctype_", "")
    await state.update_data(new_discount_type=disc_type, new_discount_plans=[])
    label = "درصد تخفیف را وارد کنید (عددی بین ۱ تا ۱۰۰، مثلاً 20)" if disc_type == "percent" else \
        "مبلغ ثابت تخفیف را به تومان وارد کنید (مثلاً 20000)"
    await callback.message.edit_text(f"🎟 مرحله ۳ از ۶\n\n💯 {label}:")
    await state.set_state(AdminStates.waiting_discount_value_step)
    await callback.answer()


@router.message(AdminStates.waiting_discount_value_step)
async def new_discount_value_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    disc_type = data.get("new_discount_type", "percent")
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد وارد کنید:")
        return
    value = int(message.text)
    if disc_type == "percent" and not (1 <= value <= 100):
        await message.answer("❌ درصد باید بین ۱ تا ۱۰۰ باشد:")
        return
    if disc_type == "amount" and value <= 0:
        await message.answer("❌ مبلغ باید بزرگ‌تر از صفر باشد:")
        return

    await state.update_data(new_discount_value=value)
    await message.answer(
        "🎟 مرحله ۴ از ۶\n\n"
        "🎯 این کد روی کدام پلن‌ها اعمال شود؟ (هرکدام را بزنید تا انتخاب/لغو شود؛ "
        "اگر «همه‌ی پلن‌ها» را بزنید، هیچ محدودیتی نخواهد داشت):",
        reply_markup=discount_plans_select_keyboard([]),
    )


@router.callback_query(F.data.startswith("discplan_"), AdminStates.waiting_discount_value_step)
async def new_discount_plan_toggle(callback: types.CallbackQuery, state: FSMContext):
    key = callback.data.replace("discplan_", "")
    data = await state.get_data()
    selected = data.get("new_discount_plans", [])

    if key == "all":
        selected = []
    elif key == "done":
        await state.update_data(new_discount_plans=selected)
        await callback.message.edit_text(
            "🎟 مرحله ۵ از ۶\n\n"
            "👤 این کد فقط برای چه کاربرانی مجاز باشد؟\n\n"
            "آیدی‌های عددی تلگرام را وارد کنید (هرکدام با کاما، فاصله یا خط جدید جدا شود).\n"
            "اگر می‌خواهید همه‌ی کاربران بتوانند از این کد استفاده کنند، عدد 0 را ارسال کنید."
        )
        await state.set_state(AdminStates.waiting_discount_users_step)
        await callback.answer()
        return
    else:
        selected = [p for p in selected if p != key] if key in selected else selected + [key]

    await state.update_data(new_discount_plans=selected)
    await callback.message.edit_reply_markup(reply_markup=discount_plans_select_keyboard(selected))
    await callback.answer()


@router.message(AdminStates.waiting_discount_users_step)
async def new_discount_users_input(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    ids = [p for p in re.split(r"[\s,،]+", raw) if p]

    if not ids or ids == ["0"]:
        await state.update_data(new_discount_users=None)
    elif all(p.lstrip("-").isdigit() for p in ids):
        await state.update_data(new_discount_users=sorted(set(ids)))
    else:
        await message.answer("❌ فقط آیدی‌های عددی معتبر وارد کنید (یا 0 برای باز کردن برای همه):")
        return

    await message.answer(
        "🎟 مرحله ۶ از ۶\n\n🔁 تعداد دفعات مجاز استفاده از این کد را وارد کنید (مثلاً 50):"
    )
    await state.set_state(AdminStates.waiting_discount_uses_step)


@router.message(AdminStates.waiting_discount_uses_step)
async def new_discount_uses_input(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("❌ لطفاً یک عدد صحیح مثبت وارد کنید:")
        return

    data = await state.get_data()
    code = data.get("new_discount_code")
    disc_type = data.get("new_discount_type", "percent")
    value = data.get("new_discount_value", 0)
    plans = data.get("new_discount_plans") or None
    allowed_users = data.get("new_discount_users") or None
    uses = int(message.text)

    try:
        db.create_discount(
            code,
            percent=value if disc_type == "percent" else 0,
            uses=uses,
            discount_type=disc_type,
            amount=value if disc_type == "amount" else 0,
            applicable_plans=plans,
            allowed_user_ids=allowed_users,
        )
        value_text = f"{value}٪" if disc_type == "percent" else f"{value:,} تومان"
        all_plans = db.get_all_plans()
        plans_text = "همه‌ی پلن‌ها" if not plans else ", ".join(all_plans.get(p, {}).get("name", p) for p in plans)
        users_text = "همه‌ی کاربران" if not allowed_users else "، ".join(f"`{i}`" for i in allowed_users)
        await message.answer(
            f"✅ کد تخفیف جدید با موفقیت ساخته شد! 🎉\n\n"
            f"🎟 کد: `{code}`\n💯 مقدار: {value_text}\n🎯 پلن‌ها: {plans_text}\n"
            f"👤 کاربران مجاز: {users_text}\n🔁 تعداد استفاده: {uses}",
            parse_mode="Markdown",
            reply_markup=admin_discount_menu(db.get_all_discounts()),
        )
    except Exception:
        await message.answer("❌ این کد قبلاً ثبت شده.", reply_markup=admin_discount_menu(db.get_all_discounts()))
    await state.clear()


# ---------------------------------------------------------------------------
# 🤝 مدیریت دعوت‌ها
# ---------------------------------------------------------------------------
REFERRERS_PER_PAGE = 10


def _referrers_page_text(page: int, total: int) -> str:
    if total == 0:
        return "🤝 هنوز هیچ دعوتی ثبت نشده."
    return (
        f"🤝 مدیریت دعوت‌شده‌ها — مرتب‌شده بر اساس بیشترین دعوت\n\n"
        f"👥 تعداد کل دعوت‌کننده‌ها: {total}\n\n"
        f"روی هر کدام بزنید تا لیست دعوت‌شده‌هایش و وضعیت کیف پولش رو ببینید 👇"
    )


@router.callback_query(F.data == "admin_referrals")
async def admin_referrals(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _render_referrers_page(callback, 0)
    await callback.answer()


async def _render_referrers_page(callback: types.CallbackQuery, page: int):
    total = db.count_referrers()
    users = db.get_referrers_page(page, REFERRERS_PER_PAGE)
    has_next = total > (page + 1) * REFERRERS_PER_PAGE
    await callback.message.edit_text(
        _referrers_page_text(page, total),
        reply_markup=admin_referrers_page_keyboard(users, page, has_next),
    )


@router.callback_query(F.data.startswith("refpage_"))
async def admin_referrers_page_nav(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    page = int(callback.data.replace("refpage_", ""))
    await _render_referrers_page(callback, page)
    await callback.answer()


@router.callback_query(F.data.startswith("refdetail_"))
async def admin_referrer_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    _, uid, page_str = callback.data.split("_")
    page = int(page_str)

    referrer = db.get_user(uid)
    if referrer is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    invited = db.get_referred_users(referrer["id"])
    text = (
        f"🤝 دعوت‌شده‌های {referrer['name']} (🆔 {referrer['telegram_id']})\n\n"
        f"💛 کیف پول آزاد دعوت‌کننده: {referrer['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود دعوت‌کننده (در انتظار): {referrer['locked_wallet']:,} تومان\n"
        f"👥 تعداد دعوت: {referrer['invited_count']} | ✅ موفق: {referrer['successful_invites']}\n\n"
        f"📋 لیست افراد دعوت‌شده:\n"
    )
    if not invited:
        text += "— هنوز هیچ کاربری ثبت نشده."
    else:
        for i, u in enumerate(invited, 1):
            reward = u.get("referral_reward") or 0
            status = u.get("referral_status") or "-"
            text += (
                f"{i}. {u['name']} | 🆔 {u['telegram_id']} | "
                f"🎁 پاداش: {reward:,} تومان | وضعیت: {status}\n"
            )

    await callback.message.edit_text(text, reply_markup=admin_referred_detail_keyboard(uid, page))
    await callback.answer()


# ---------------------------------------------------------------------------
# 🤝 نمایندگی — با ثبت آیدی عددی یک فرد، همه‌ی خریدهای VIP بعدی او به‌صورت
# خودکار با درصد تعیین‌شده تخفیف می‌خورد (بدون نیاز به وارد کردن کد تخفیف).
# ---------------------------------------------------------------------------
def _user_detail_text(user: dict) -> str:
    """همان متن استاندارد صفحه‌ی «مدیریت کاربر» (بخش کاربران)؛ برای اینکه صفحه‌ی
    نماینده هم دقیقاً همین محیط را نشان دهد، این تابع مشترک استفاده می‌شود."""
    stats = db.get_referral_stats(user["id"])
    return (
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        f"👛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود: {user['locked_wallet']:,} تومان\n"
        f"🛒 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 عضویت: {user['joined']}\n\n"
        f"🔗 کد دعوت: {user['invite_code']}\n"
        f"👥 دعوت: {stats['invited_count']} | موفق: {stats['successful_invites']}"
    )


@router.callback_query(F.data == "admin_agency")
async def admin_agency_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    agents = db.get_all_agents()
    text = (
        "🤝 هنوز هیچ نماینده‌ای ثبت نشده.\n\nبرای افزودن، دکمه‌ی زیر را بزنید 👇"
        if not agents else
        "🤝 نمایندگان فعلی (تخفیف خودکار روی VIP)\n\nروی هرکدام بزنید تا مثل بخش «کاربران» مدیریتش کنید 👇"
    )

    await callback.message.edit_text(text, reply_markup=admin_agency_menu(agents))
    await callback.answer()


@router.callback_query(F.data == "new_agent")
async def new_agent_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🤝 افزودن نماینده — مرحله ۱ از ۲\n\n"
        "🆔 آیدی عددی (Telegram ID) فرد را ارسال کنید:",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_agent_id_step)
    await callback.answer()


@router.message(AdminStates.waiting_agent_id_step)
async def new_agent_id_input(message: types.Message, state: FSMContext):
    tid = (message.text or "").strip()
    if not tid.isdigit():
        await message.answer("❌ آیدی عددی نامعتبر است؛ فقط عدد ارسال کنید:")
        return
    await state.update_data(new_agent_id=tid)
    await message.answer(
        f"🧾 مرحله ۲ از ۲\n\n💯 درصد تخفیف VIP برای این نماینده را وارد کنید "
        f"(پیش‌فرض پیشنهادی: {AGENCY_VIP_DISCOUNT_PERCENT}):"
    )
    await state.set_state(AdminStates.waiting_agent_percent_step)


@router.message(AdminStates.waiting_agent_percent_step)
async def new_agent_percent_input(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit() or not (1 <= int(message.text) <= 100):
        await message.answer("❌ لطفاً یک عدد بین ۱ تا ۱۰۰ وارد کنید:")
        return
    data = await state.get_data()
    tid = data.get("new_agent_id")
    percent = int(message.text)
    db.add_agent(tid, percent)
    await message.answer(
        f"✅ نماینده ثبت شد!\n\n🆔 {tid}\n💯 تخفیف VIP: {percent}٪\n\n"
        f"از این به بعد، خریدهای VIP این آیدی به‌صورت خودکار {percent}٪ تخفیف می‌خورد.",
        reply_markup=admin_agency_menu(db.get_all_agents()),
    )
    await state.clear()


@router.callback_query(F.data.startswith("deleteagent_"))
async def delete_agent(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    tid = callback.data.replace("deleteagent_", "")
    db.remove_agent(tid)
    await callback.answer("✅ نماینده حذف شد.")
    await admin_agency_list(callback)


# --- 👤 باز کردن صفحه‌ی یک نماینده — دقیقاً همان صفحه‌ی «مدیریت کاربر»
# (بخش کاربران)، به‌علاوه‌ی دکمه‌ی «💯 تغییر درصد تخفیف نمایندگی» ---
@router.callback_query(F.data.startswith("agentopen_"))
async def admin_agent_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    tid = callback.data.replace("agentopen_", "")
    agent = db.get_agent(tid)
    if agent is None:
        await callback.answer("❌ این نماینده یافت نشد.", show_alert=True)
        return

    user = db.get_user(tid)
    if user is None:
        await callback.message.edit_text(
            f"🤝 نماینده 🆔 {tid} | 💯 {agent['vip_discount_percent']}٪\n\n"
            "⚠️ این آیدی هنوز ربات را /start نزده؛ اطلاعات کاربری‌ای برایش ثبت نشده.",
            reply_markup=admin_agent_actions_keyboard(tid),
        )
        await callback.answer()
        return

    text = _user_detail_text(user) + f"\n\n🤝 درصد تخفیف نمایندگی (VIP): {agent['vip_discount_percent']}٪"
    await callback.message.edit_text(text, reply_markup=admin_agent_actions_keyboard(tid))
    await callback.answer()


@router.callback_query(F.data.startswith("editagentpercent_"))
async def admin_agent_edit_percent_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    tid = callback.data.replace("editagentpercent_", "")
    if db.get_agent(tid) is None:
        await callback.answer("❌ این نماینده یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_agent_id=tid)
    await state.set_state(AdminStates.waiting_agent_edit_percent)
    await callback.message.edit_text("💯 درصد تخفیف جدید (بین ۱ تا ۱۰۰) را ارسال کنید:")
    await callback.answer()


@router.message(AdminStates.waiting_agent_edit_percent)
async def admin_agent_edit_percent_apply(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit() or not (1 <= int(message.text) <= 100):
        await message.answer("❌ لطفاً یک عدد بین ۱ تا ۱۰۰ وارد کنید:")
        return
    data = await state.get_data()
    tid = data.get("edit_agent_id")
    agent = db.get_agent(tid) if tid else None
    if agent is None:
        await message.answer("❌ مشکلی پیش آمد؛ از ابتدا امتحان کنید.", reply_markup=admin_agency_menu(db.get_all_agents()))
        await state.clear()
        return

    percent = int(message.text)
    db.add_agent(tid, percent, agent.get("note"))
    await message.answer(
        f"✅ درصد تخفیف نماینده به‌روزرسانی شد:\n🆔 {tid}\n💯 {percent}٪",
        reply_markup=admin_agent_actions_keyboard(tid),
    )
    await state.clear()


# ---------------------------------------------------------------------------
# 🗂 دسته‌بندی‌های VIP — می‌توان هر تعداد دسته و داخل هرکدام هر تعداد پلن اضافه
# کرد؛ همه‌شان خودکار در «🛒 خرید اشتراک → 🚀 سرور VIP» برای کاربر ظاهر می‌شوند.
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_vip_categories")
async def admin_vip_categories_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🗂 دسته‌بندی‌های VIP\n\n"
        "این دسته‌ها همان چیزی هستند که کاربر موقع «خرید اشتراک → سرور VIP» می‌بیند.\n"
        "برای مدیریت پلن‌های داخل هر دسته، روی آن بزنید 👇",
        reply_markup=admin_vip_categories_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "newvipcat")
async def admin_new_vip_category_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_vip_category_name)
    await callback.message.edit_text("🗂 نام دسته‌بندی جدید را ارسال کنید (مثلاً «💎 حجم بالای ویژه»):")
    await callback.answer()


@router.message(AdminStates.waiting_vip_category_name)
async def admin_new_vip_category_apply(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام نمی‌تواند خالی باشد؛ دوباره ارسال کنید:")
        return
    cat = db.create_vip_category(name)
    await message.answer(
        f"✅ دسته‌بندی «{name}» ساخته شد!\n\nحالا می‌توانید از داخل همین دسته، پلن اضافه کنید 👇",
        reply_markup=admin_vip_category_detail_keyboard(cat["key"]),
    )
    await state.clear()


@router.callback_query(F.data.startswith("vipcatdesc_"))
async def admin_edit_vip_category_desc_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("vipcatdesc_", "")
    if db.get_vip_category(category_key) is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_vip_category_key=category_key)
    await state.set_state(AdminStates.waiting_vip_category_description)
    await callback.message.edit_text(
        "📝 متن توضیح جدید این دسته را ارسال کنید — همین متن بالای دکمه‌های شیشه‌ای پلن‌های این دسته به کاربر نمایش داده می‌شود.\n\nبرای حذف توضیح فعلی، کلمه‌ی «حذف» را بفرستید:"
    )
    await callback.answer()


@router.message(AdminStates.waiting_vip_category_description)
async def admin_edit_vip_category_desc_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    category_key = data.get("edit_vip_category_key")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await message.answer("❌ مشکلی پیش آمد؛ از ابتدا امتحان کنید.")
        await state.clear()
        return
    text = (message.text or "").strip()
    description = None if text == "حذف" else text
    db.update_vip_category_description(cat["id"], description)
    if description:
        confirm_text = "✅ توضیح این دسته ذخیره شد و از این به بعد بالای دکمه‌های پلن‌های این دسته نمایش داده می‌شود."
    else:
        confirm_text = "✅ توضیح این دسته حذف شد."
    await message.answer(confirm_text, reply_markup=admin_vip_category_detail_keyboard(category_key))
    await state.clear()


@router.callback_query(F.data.startswith("admincat_"))
async def admin_vip_category_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("admincat_", "")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    plans = db.get_vip_plans(cat["id"])
    if cat.get("description"):
        desc_note = f"\n\n📝 توضیح فعلی:\n{cat['description']}"
    else:
        desc_note = "\n\n📝 هنوز توضیحی برای این دسته ثبت نشده."
    text = f"🗂 {cat['name']}\n\n📦 تعداد پلن: {len(plans)}{desc_note}\n\nبرای مدیریت هر پلن روی آن بزنید 👇"
    await callback.message.edit_text(text, reply_markup=admin_vip_category_detail_keyboard(category_key))
    await callback.answer()


@router.callback_query(F.data.startswith("delvipcat_"))
async def admin_delete_vip_category(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("delvipcat_", "")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    ok = db.delete_vip_category(cat["id"])
    if not ok:
        await callback.answer("❌ این دسته پلن دارد؛ اول همه‌ی پلن‌هایش را حذف کنید.", show_alert=True)
        return
    await callback.answer("✅ دسته حذف شد.")
    await admin_vip_categories_list(callback)


# --- افزودن پلن جدید به یک دسته (۴ مرحله: نام / قیمت / حجم گیگ / مدت روز) ---
@router.callback_query(F.data.startswith("newvipplan_"))
async def admin_new_vip_plan_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("newvipplan_", "")
    if db.get_vip_category(category_key) is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    await state.update_data(new_vip_plan_category=category_key)
    await state.set_state(AdminStates.waiting_vip_plan_name)
    await callback.message.edit_text(
        "📦 افزودن پلن جدید — مرحله ۱ از ۴\n\n✏️ نام پلن را ارسال کنید (مثلاً «۲۰۰ گیگ | کاربر و زمان ∞»):"
    )
    await callback.answer()


@router.message(AdminStates.waiting_vip_plan_name)
async def admin_new_vip_plan_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام نمی‌تواند خالی باشد؛ دوباره ارسال کنید:")
        return
    await state.update_data(new_vip_plan_name=name)
    await state.set_state(AdminStates.waiting_vip_plan_price)
    await message.answer("📦 مرحله ۲ از ۴\n\n💰 قیمت را به تومان (فقط عدد) ارسال کنید:")


@router.message(AdminStates.waiting_vip_plan_price)
async def admin_new_vip_plan_price(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد ارسال کنید:")
        return
    await state.update_data(new_vip_plan_price=int(message.text))
    await state.set_state(AdminStates.waiting_vip_plan_gb)
    await message.answer(
        "📦 مرحله ۳ از ۴\n\n🗜 حجم را به گیگابایت ارسال کنید (اگر نامحدود است، عدد 0 را بفرستید):"
    )


@router.message(AdminStates.waiting_vip_plan_gb)
async def admin_new_vip_plan_gb(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (برای نامحدود، 0):")
        return
    await state.update_data(new_vip_plan_gb=int(message.text))
    await state.set_state(AdminStates.waiting_vip_plan_days)
    await message.answer("📦 مرحله ۴ از ۴\n\n⏳ مدت را به روز ارسال کنید (اگر نامحدود است، عدد 0 را بفرستید):")


@router.message(AdminStates.waiting_vip_plan_days)
async def admin_new_vip_plan_days(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (برای نامحدود، 0):")
        return
    data = await state.get_data()
    category_key = data.get("new_vip_plan_category")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await message.answer("❌ مشکلی پیش آمد؛ از ابتدا امتحان کنید.")
        await state.clear()
        return

    days = int(message.text)
    name = data.get("new_vip_plan_name")
    price = data.get("new_vip_plan_price")
    volume_gb = data.get("new_vip_plan_gb", 0)

    plan_key = db.add_vip_plan(cat["id"], name, price, days=days, volume_gb=volume_gb)
    await message.answer(
        f"✅ پلن جدید اضافه شد! 🎉\n\n📦 {name}\n💰 {price:,} تومان\n🗜 "
        f"{volume_gb if volume_gb else 'نامحدود'} گیگ\n⏳ {days if days else 'نامحدود'} روز",
        reply_markup=admin_vip_category_detail_keyboard(category_key),
    )
    await state.clear()


# --- مشاهده/ویرایش/حذف یک پلن مشخص ---
@router.callback_query(F.data.startswith("vipplan_"))
async def admin_vip_plan_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    plan_key = callback.data.replace("vipplan_", "")
    plan = db.get_vip_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    cat = db.get_vip_category(plan["category_id"])
    text = (
        f"📦 {plan['name']}\n\n"
        f"💰 قیمت: {plan['price']:,} تومان\n"
        f"🗜 حجم: {plan['volume_gb'] if plan['volume_gb'] else 'نامحدود'} گیگ\n"
        f"⏳ مدت: {plan['days'] if plan['days'] else 'نامحدود'} روز\n"
        f"🗂 دسته: {cat['name'] if cat else '-'}"
    )
    await callback.message.edit_text(
        text, reply_markup=admin_vip_plan_detail_keyboard(plan_key, cat["key"] if cat else "")
    )
    await callback.answer()


def _vip_plan_edit_starter(field_state, prompt: str, prefix: str):
    async def handler(callback: types.CallbackQuery, state: FSMContext):
        if not _is_admin(callback.from_user.id):
            await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        plan_key = callback.data.replace(prefix, "")
        if db.get_vip_plan(plan_key) is None:
            await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
            return
        await state.update_data(edit_vip_plan_key=plan_key)
        await state.set_state(field_state)
        await callback.message.edit_text(prompt)
        await callback.answer()
    return handler


router.callback_query(F.data.startswith("vipplanname_"))(
    _vip_plan_edit_starter(AdminStates.waiting_vip_plan_edit_name, "✏️ نام جدید پلن را ارسال کنید:", "vipplanname_")
)
router.callback_query(F.data.startswith("vipplanprice_"))(
    _vip_plan_edit_starter(AdminStates.waiting_vip_plan_edit_price, "💰 قیمت جدید را به تومان (فقط عدد) ارسال کنید:", "vipplanprice_")
)
router.callback_query(F.data.startswith("vipplangb_"))(
    _vip_plan_edit_starter(AdminStates.waiting_vip_plan_edit_gb, "🗜 حجم جدید را به گیگ (فقط عدد، 0 = نامحدود) ارسال کنید:", "vipplangb_")
)
router.callback_query(F.data.startswith("vipplandays_"))(
    _vip_plan_edit_starter(AdminStates.waiting_vip_plan_edit_days, "⏳ مدت جدید را به روز (فقط عدد، 0 = نامحدود) ارسال کنید:", "vipplandays_")
)


async def _after_vip_plan_edit(message: types.Message, state: FSMContext, plan_key: str, success_text: str):
    plan = db.get_vip_plan(plan_key)
    cat = db.get_vip_category(plan["category_id"]) if plan else None
    await message.answer(
        success_text,
        reply_markup=admin_vip_plan_detail_keyboard(plan_key, cat["key"] if cat else ""),
    )
    await state.clear()


@router.message(AdminStates.waiting_vip_plan_edit_name)
async def admin_vip_plan_edit_name_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_vip_plan_key")
    new_name = (message.text or "").strip()
    if not plan_key or not new_name:
        await message.answer("❌ متن نامعتبر است؛ دوباره ارسال کنید:")
        return
    db.update_vip_plan(plan_key, name=new_name)
    await _after_vip_plan_edit(message, state, plan_key, f"✅ نام پلن به‌روزرسانی شد:\n📦 {new_name}")


@router.message(AdminStates.waiting_vip_plan_edit_price)
async def admin_vip_plan_edit_price_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_vip_plan_key")
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد ارسال کنید:")
        return
    price = int(message.text)
    db.update_vip_plan(plan_key, price=price)
    await _after_vip_plan_edit(message, state, plan_key, f"✅ قیمت پلن به‌روزرسانی شد:\n💰 {price:,} تومان")


@router.message(AdminStates.waiting_vip_plan_edit_gb)
async def admin_vip_plan_edit_gb_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_vip_plan_key")
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (0 = نامحدود):")
        return
    volume_gb = int(message.text)
    db.update_vip_plan(plan_key, volume_gb=volume_gb)
    await _after_vip_plan_edit(
        message, state, plan_key, f"✅ حجم پلن به‌روزرسانی شد:\n🗜 {volume_gb if volume_gb else 'نامحدود'} گیگ"
    )


@router.message(AdminStates.waiting_vip_plan_edit_days)
async def admin_vip_plan_edit_days_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_vip_plan_key")
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (0 = نامحدود):")
        return
    days = int(message.text)
    db.update_vip_plan(plan_key, days=days)
    await _after_vip_plan_edit(
        message, state, plan_key, f"✅ مدت پلن به‌روزرسانی شد:\n⏳ {days if days else 'نامحدود'} روز"
    )


@router.callback_query(F.data.startswith("delvipplan_"))
async def admin_delete_vip_plan(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    plan_key = callback.data.replace("delvipplan_", "")
    plan = db.get_vip_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    cat = db.get_vip_category(plan["category_id"])
    db.delete_vip_plan(plan_key)
    await callback.answer("✅ پلن حذف شد.")
    if cat:
        callback.data = f"admincat_{cat['key']}"
        await admin_vip_category_detail(callback)
    else:
        await admin_vip_categories_list(callback)


# --- ↕️ تغییر ترتیب دسته‌بندی‌ها/پلن‌های VIP ---
@router.callback_query(F.data.startswith("movevipcat_"))
async def admin_move_vip_category(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    raw = callback.data.replace("movevipcat_", "")
    category_key, _, direction = raw.rpartition("_")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    db.move_vip_category(cat["id"], direction)
    await callback.answer()
    await admin_vip_categories_list(callback)


@router.callback_query(F.data.startswith("movevipplan_"))
async def admin_move_vip_plan(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    raw = callback.data.replace("movevipplan_", "")
    plan_key, _, direction = raw.rpartition("_")
    plan = db.get_vip_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    db.move_vip_plan(plan["id"], direction)
    await callback.answer()
    cat = db.get_vip_category(plan["category_id"])
    if cat:
        callback.data = f"admincat_{cat['key']}"
        await admin_vip_category_detail(callback)


# ---------------------------------------------------------------------------
# 🎮 دسته‌بندی‌های Gaming — دقیقاً مشابه بخش VIP بالا: می‌توان هر تعداد دسته و
# داخل هرکدام هر تعداد پلن اضافه کرد؛ همه‌شان خودکار در
# «🛒 خرید اشتراک → 🌐 سرور Gaming» برای کاربر ظاهر می‌شوند.
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_gaming_categories")
async def admin_gaming_categories_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🎮 دسته‌بندی‌های Gaming\n\n"
        "این دسته‌ها همان چیزی هستند که کاربر موقع «خرید اشتراک → سرور Gaming» می‌بیند.\n"
        "برای مدیریت پلن‌های داخل هر دسته، روی آن بزنید 👇",
        reply_markup=admin_gaming_categories_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "newgamingcat")
async def admin_new_gaming_category_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_gaming_category_name)
    await callback.message.edit_text("🎮 نام دسته‌بندی جدید را ارسال کنید (مثلاً «⚡️ گیمینگ سریع»):")
    await callback.answer()


@router.message(AdminStates.waiting_gaming_category_name)
async def admin_new_gaming_category_apply(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام نمی‌تواند خالی باشد؛ دوباره ارسال کنید:")
        return
    cat = db.create_gaming_category(name)
    await message.answer(
        f"✅ دسته‌بندی «{name}» ساخته شد!\n\nحالا می‌توانید از داخل همین دسته، پلن اضافه کنید 👇",
        reply_markup=admin_gaming_category_detail_keyboard(cat["key"]),
    )
    await state.clear()


@router.callback_query(F.data.startswith("admingamingcat_"))
async def admin_gaming_category_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("admingamingcat_", "")
    cat = db.get_gaming_category(category_key)
    if cat is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    plans = db.get_gaming_plans(cat["id"])
    text = f"🎮 {cat['name']}\n\n📦 تعداد پلن: {len(plans)}\n\nبرای مدیریت هر پلن روی آن بزنید 👇"
    await callback.message.edit_text(text, reply_markup=admin_gaming_category_detail_keyboard(category_key))
    await callback.answer()


@router.callback_query(F.data.startswith("delgamingcat_"))
async def admin_delete_gaming_category(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("delgamingcat_", "")
    cat = db.get_gaming_category(category_key)
    if cat is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    ok = db.delete_gaming_category(cat["id"])
    if not ok:
        await callback.answer("❌ این دسته پلن دارد؛ اول همه‌ی پلن‌هایش را حذف کنید.", show_alert=True)
        return
    await callback.answer("✅ دسته حذف شد.")
    await admin_gaming_categories_list(callback)


# --- افزودن پلن جدید به یک دسته گیمینگ (۴ مرحله: نام / قیمت / حجم گیگ / مدت روز) ---
@router.callback_query(F.data.startswith("newgamingplan_"))
async def admin_new_gaming_plan_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("newgamingplan_", "")
    if db.get_gaming_category(category_key) is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    await state.update_data(new_gaming_plan_category=category_key)
    await state.set_state(AdminStates.waiting_gaming_plan_name)
    await callback.message.edit_text(
        "📦 افزودن پلن جدید — مرحله ۱ از ۴\n\n✏️ نام پلن را ارسال کنید (مثلاً «۱۰۰ گیگ گیمینگ یک ماهه»):"
    )
    await callback.answer()


@router.message(AdminStates.waiting_gaming_plan_name)
async def admin_new_gaming_plan_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام نمی‌تواند خالی باشد؛ دوباره ارسال کنید:")
        return
    await state.update_data(new_gaming_plan_name=name)
    await state.set_state(AdminStates.waiting_gaming_plan_price)
    await message.answer("📦 مرحله ۲ از ۴\n\n💰 قیمت را به تومان (فقط عدد) ارسال کنید:")


@router.message(AdminStates.waiting_gaming_plan_price)
async def admin_new_gaming_plan_price(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد ارسال کنید:")
        return
    await state.update_data(new_gaming_plan_price=int(message.text))
    await state.set_state(AdminStates.waiting_gaming_plan_gb)
    await message.answer(
        "📦 مرحله ۳ از ۴\n\n🗜 حجم را به گیگابایت ارسال کنید (اگر نامحدود است، عدد 0 را بفرستید):"
    )


@router.message(AdminStates.waiting_gaming_plan_gb)
async def admin_new_gaming_plan_gb(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (برای نامحدود، 0):")
        return
    await state.update_data(new_gaming_plan_gb=int(message.text))
    await state.set_state(AdminStates.waiting_gaming_plan_days)
    await message.answer("📦 مرحله ۴ از ۴\n\n⏳ مدت را به روز ارسال کنید (اگر نامحدود است، عدد 0 را بفرستید):")


@router.message(AdminStates.waiting_gaming_plan_days)
async def admin_new_gaming_plan_days(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (برای نامحدود، 0):")
        return
    data = await state.get_data()
    category_key = data.get("new_gaming_plan_category")
    cat = db.get_gaming_category(category_key)
    if cat is None:
        await message.answer("❌ مشکلی پیش آمد؛ از ابتدا امتحان کنید.")
        await state.clear()
        return

    days = int(message.text)
    name = data.get("new_gaming_plan_name")
    price = data.get("new_gaming_plan_price")
    volume_gb = data.get("new_gaming_plan_gb", 0)

    plan_key = db.add_gaming_plan(cat["id"], name, price, days=days, volume_gb=volume_gb)
    await message.answer(
        f"✅ پلن جدید اضافه شد! 🎉\n\n📦 {name}\n💰 {price:,} تومان\n🗜 "
        f"{volume_gb if volume_gb else 'نامحدود'} گیگ\n⏳ {days if days else 'نامحدود'} روز",
        reply_markup=admin_gaming_category_detail_keyboard(category_key),
    )
    await state.clear()


# --- مشاهده/ویرایش/حذف یک پلن گیمینگ مشخص ---
@router.callback_query(F.data.startswith("gamingplan_"))
async def admin_gaming_plan_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    plan_key = callback.data.replace("gamingplan_", "")
    plan = db.get_gaming_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    cat = db.get_gaming_category(plan["category_id"])
    text = (
        f"📦 {plan['name']}\n\n"
        f"💰 قیمت: {plan['price']:,} تومان\n"
        f"🗜 حجم: {plan['volume_gb'] if plan['volume_gb'] else 'نامحدود'} گیگ\n"
        f"⏳ مدت: {plan['days'] if plan['days'] else 'نامحدود'} روز\n"
        f"🎮 دسته: {cat['name'] if cat else '-'}"
    )
    await callback.message.edit_text(
        text, reply_markup=admin_gaming_plan_detail_keyboard(plan_key, cat["key"] if cat else "")
    )
    await callback.answer()


def _gaming_plan_edit_starter(field_state, prompt: str, prefix: str):
    async def handler(callback: types.CallbackQuery, state: FSMContext):
        if not _is_admin(callback.from_user.id):
            await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        plan_key = callback.data.replace(prefix, "")
        if db.get_gaming_plan(plan_key) is None:
            await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
            return
        await state.update_data(edit_gaming_plan_key=plan_key)
        await state.set_state(field_state)
        await callback.message.edit_text(prompt)
        await callback.answer()
    return handler


router.callback_query(F.data.startswith("gamingplanname_"))(
    _gaming_plan_edit_starter(AdminStates.waiting_gaming_plan_edit_name, "✏️ نام جدید پلن را ارسال کنید:", "gamingplanname_")
)
router.callback_query(F.data.startswith("gamingplanprice_"))(
    _gaming_plan_edit_starter(AdminStates.waiting_gaming_plan_edit_price, "💰 قیمت جدید را به تومان (فقط عدد) ارسال کنید:", "gamingplanprice_")
)
router.callback_query(F.data.startswith("gamingplangb_"))(
    _gaming_plan_edit_starter(AdminStates.waiting_gaming_plan_edit_gb, "🗜 حجم جدید را به گیگ (فقط عدد، 0 = نامحدود) ارسال کنید:", "gamingplangb_")
)
router.callback_query(F.data.startswith("gamingplandays_"))(
    _gaming_plan_edit_starter(AdminStates.waiting_gaming_plan_edit_days, "⏳ مدت جدید را به روز (فقط عدد، 0 = نامحدود) ارسال کنید:", "gamingplandays_")
)


async def _after_gaming_plan_edit(message: types.Message, state: FSMContext, plan_key: str, success_text: str):
    plan = db.get_gaming_plan(plan_key)
    cat = db.get_gaming_category(plan["category_id"]) if plan else None
    await message.answer(
        success_text,
        reply_markup=admin_gaming_plan_detail_keyboard(plan_key, cat["key"] if cat else ""),
    )
    await state.clear()


@router.message(AdminStates.waiting_gaming_plan_edit_name)
async def admin_gaming_plan_edit_name_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_gaming_plan_key")
    new_name = (message.text or "").strip()
    if not plan_key or not new_name:
        await message.answer("❌ متن نامعتبر است؛ دوباره ارسال کنید:")
        return
    db.update_gaming_plan(plan_key, name=new_name)
    await _after_gaming_plan_edit(message, state, plan_key, f"✅ نام پلن به‌روزرسانی شد:\n📦 {new_name}")


@router.message(AdminStates.waiting_gaming_plan_edit_price)
async def admin_gaming_plan_edit_price_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_gaming_plan_key")
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد ارسال کنید:")
        return
    price = int(message.text)
    db.update_gaming_plan(plan_key, price=price)
    await _after_gaming_plan_edit(message, state, plan_key, f"✅ قیمت پلن به‌روزرسانی شد:\n💰 {price:,} تومان")


@router.message(AdminStates.waiting_gaming_plan_edit_gb)
async def admin_gaming_plan_edit_gb_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_gaming_plan_key")
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (0 = نامحدود):")
        return
    volume_gb = int(message.text)
    db.update_gaming_plan(plan_key, volume_gb=volume_gb)
    await _after_gaming_plan_edit(
        message, state, plan_key, f"✅ حجم پلن به‌روزرسانی شد:\n🗜 {volume_gb if volume_gb else 'نامحدود'} گیگ"
    )


@router.message(AdminStates.waiting_gaming_plan_edit_days)
async def admin_gaming_plan_edit_days_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_gaming_plan_key")
    if not message.text or not message.text.isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (0 = نامحدود):")
        return
    days = int(message.text)
    db.update_gaming_plan(plan_key, days=days)
    await _after_gaming_plan_edit(
        message, state, plan_key, f"✅ مدت پلن به‌روزرسانی شد:\n⏳ {days if days else 'نامحدود'} روز"
    )


@router.callback_query(F.data.startswith("delgamingplan_"))
async def admin_delete_gaming_plan(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    plan_key = callback.data.replace("delgamingplan_", "")
    plan = db.get_gaming_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    cat = db.get_gaming_category(plan["category_id"])
    db.delete_gaming_plan(plan_key)
    await callback.answer("✅ پلن حذف شد.")
    if cat:
        callback.data = f"admingamingcat_{cat['key']}"
        await admin_gaming_category_detail(callback)
    else:
        await admin_gaming_categories_list(callback)


# --- ↕️ تغییر ترتیب دسته‌بندی‌ها/پلن‌های Gaming ---
@router.callback_query(F.data.startswith("movegamingcat_"))
async def admin_move_gaming_category(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    raw = callback.data.replace("movegamingcat_", "")
    category_key, _, direction = raw.rpartition("_")
    cat = db.get_gaming_category(category_key)
    if cat is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    db.move_gaming_category(cat["id"], direction)
    await callback.answer()
    await admin_gaming_categories_list(callback)


@router.callback_query(F.data.startswith("movegamingplan_"))
async def admin_move_gaming_plan(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    raw = callback.data.replace("movegamingplan_", "")
    plan_key, _, direction = raw.rpartition("_")
    plan = db.get_gaming_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    db.move_gaming_plan(plan["id"], direction)
    await callback.answer()
    cat = db.get_gaming_category(plan["category_id"])
    if cat:
        callback.data = f"admingamingcat_{cat['key']}"
        await admin_gaming_category_detail(callback)


# ---------------------------------------------------------------------------
# 📢 پیام همگانی
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "📢 پیامی که می‌خواهید برای همه کاربران ارسال شود را بفرستید (متن، عکس، فیلم یا یک پیام فوروارد‌شده هم می‌توانید بفرستید):",
        reply_markup=admin_back_button(),
    )
    await state.set_state(UserStates.waiting_broadcast)
    await callback.answer()


@router.message(UserStates.waiting_broadcast, F.from_user.id == ADMIN_ID)
async def admin_broadcast_send(message: types.Message, state: FSMContext):
    """پیام همگانی به همه‌ی کاربران؛ copy_message هر نوع پیامی (متن، عکس،
    فیلم، فوروارد‌شده) را عیناً ارسال می‌کند؛ کاربران مسدود حذف می‌شوند و بین
    هر ارسال یک مکث کوتاه تاخیر برای جلوگیری از محدودیت flood تلگرام گذارده می‌شود."""
    users = [u for u in db.get_all_users() if not db.is_user_blocked(u["telegram_id"])]
    sent, failed = 0, 0

    status_msg = await message.answer(f"📢 در حال ارسال به {len(users)} کاربر...")

    for u in users:
        try:
            await send_notification_sticker(message.bot, int(u["telegram_id"]), "notif_broadcast")
            await message.bot.copy_message(int(u["telegram_id"]), message.chat.id, message.message_id)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status_msg.edit_text(f"✅ ارسال شد به {sent} نفر. ناموفق: {failed} نفر.")
    await state.clear()


# ---------------------------------------------------------------------------
# 📚 مدیریت راهنما و اموزش‌ها — افزودن/ویرایش/حذف/تغییر ترتیب (متن/عکس/فیلم)
# ---------------------------------------------------------------------------
def _guide_detail_text(guide: dict) -> str:
    text = f"📚 {guide['title']}"
    if guide.get("body_text"):
        text += f"\n\n{guide['body_text']}"
    return text


async def _send_guide_detail(target: types.Message, guide_id: int):
    guide = db.get_guide(guide_id)
    if guide is None:
        await target.answer("❌ این راهنما دیگر موجود نیست.")
        return
    guides = db.get_guides()
    idx = next((i for i, g in enumerate(guides) if g["id"] == guide_id), 0)
    caption = _guide_detail_text(guide)
    kb = admin_guide_detail_keyboard(guide_id, idx, len(guides))
    try:
        if guide["content_type"] == "photo" and guide.get("file_id"):
            await target.answer_photo(guide["file_id"], caption=caption, reply_markup=kb)
        elif guide["content_type"] == "video" and guide.get("file_id"):
            await target.answer_video(guide["file_id"], caption=caption, reply_markup=kb)
        else:
            await target.answer(caption, reply_markup=kb)
    except Exception:
        await target.answer(caption, reply_markup=kb)


@router.callback_query(F.data == "admin_guides")
async def admin_guides_list(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    guides = db.get_guides()
    text = (
        f"📚 مدیریت راهنما و اموزش‌ها\n\nتعداد: {len(guides)}\n\n"
        "از اینجا می‌تونید راهنما/آموزش جدید اضافه کنید یا موردهای موجود را ویرایش کنید:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_guides_menu(guides))
    except Exception:
        await callback.message.answer(text, reply_markup=admin_guides_menu(guides))
    await callback.answer()


@router.callback_query(F.data == "guidenew")
async def admin_guide_new_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_guide_title)
    await callback.message.answer(
        "📚 عنوان راهنما/آموزش جدید را بفرستید:",
        reply_markup=admin_guide_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_guide_title)
async def admin_guide_new_title(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("❌ عنوان نمی‌تواند خالی باشد. دوباره بفرستید:")
        return
    await state.update_data(guide_new_title=title)
    await state.set_state(AdminStates.waiting_guide_content)
    await message.answer(
        "📝 حالا محتوای این راهنما را بفرستید (متن، عکس با کپشن، یا فیلم با کپشن):",
        reply_markup=admin_guide_cancel_keyboard(),
    )


@router.message(AdminStates.waiting_guide_content)
async def admin_guide_new_content(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    title = data.get("guide_new_title")
    if not title:
        await state.clear()
        return

    if message.photo:
        content_type, file_id, body_text = "photo", message.photo[-1].file_id, (message.caption or "")
    elif message.video:
        content_type, file_id, body_text = "video", message.video.file_id, (message.caption or "")
    else:
        content_type, file_id, body_text = "text", None, (message.text or "")

    if not body_text and not file_id:
        await message.answer("❌ محتوا نمی‌تواند خالی باشد. دوباره بفرستید:")
        return

    guide = db.create_guide(title=title, content_type=content_type, body_text=body_text or None, file_id=file_id)
    await state.clear()
    await message.answer(f"✅ راهنمای «{title}» اضافه شد.")
    await _send_guide_detail(message, guide["id"])


@router.callback_query(F.data.startswith("guideadminopen_"))
async def admin_guide_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guideadminopen_", ""))
    await _send_guide_detail(callback.message, guide_id)
    await callback.answer()


@router.callback_query(F.data.startswith("guidemove_"))
async def admin_guide_move(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    raw = callback.data.replace("guidemove_", "")
    guide_id_str, _, direction = raw.rpartition("_")
    guide_id = int(guide_id_str)
    db.move_guide(guide_id, direction)
    await _send_guide_detail(callback.message, guide_id)
    await callback.answer("↕️ ترتیب به‌روزرسانی شد.")


@router.callback_query(F.data.startswith("guideeditname_"))
async def admin_guide_edit_name_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guideeditname_", ""))
    guide = db.get_guide(guide_id)
    if guide is None:
        await callback.answer("❌ این راهنما دیگر موجود نیست.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_guide_edit_title)
    await state.update_data(guide_edit_id=guide_id)
    await callback.message.answer(
        f"✏️ عنوان جدید برای «{guide['title']}» را بفرستید:",
        reply_markup=admin_guide_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_guide_edit_title)
async def admin_guide_edit_name_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    guide_id = data.get("guide_edit_id")
    if not guide_id:
        await state.clear()
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("❌ عنوان نمی‌تواند خالی باشد. دوباره بفرستید:")
        return
    db.update_guide(guide_id, title=title)
    await state.clear()
    await message.answer("✅ عنوان به‌روزرسانی شد.")
    await _send_guide_detail(message, guide_id)


@router.callback_query(F.data.startswith("guideeditcontent_"))
async def admin_guide_edit_content_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guideeditcontent_", ""))
    guide = db.get_guide(guide_id)
    if guide is None:
        await callback.answer("❌ این راهنما دیگر موجود نیست.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_guide_edit_content)
    await state.update_data(guide_edit_id=guide_id)
    await callback.message.answer(
        f"📝 محتوای جدید برای «{guide['title']}» را بفرستید (متن، عکس با کپشن، یا فیلم با کپشن):",
        reply_markup=admin_guide_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_guide_edit_content)
async def admin_guide_edit_content_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    guide_id = data.get("guide_edit_id")
    if not guide_id:
        await state.clear()
        return

    if message.photo:
        content_type, file_id, body_text = "photo", message.photo[-1].file_id, (message.caption or "")
    elif message.video:
        content_type, file_id, body_text = "video", message.video.file_id, (message.caption or "")
    else:
        content_type, file_id, body_text = "text", None, (message.text or "")

    if not body_text and not file_id:
        await message.answer("❌ محتوا نمی‌تواند خالی باشد. دوباره بفرستید:")
        return

    db.update_guide(guide_id, content_type=content_type, body_text=body_text, file_id=file_id)
    await state.clear()
    await message.answer("✅ محتوا به‌روزرسانی شد.")
    await _send_guide_detail(message, guide_id)


@router.callback_query(F.data.startswith("guidedelete_"))
async def admin_guide_delete_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guidedelete_", ""))
    guide = db.get_guide(guide_id)
    if guide is None:
        await callback.answer("❌ این راهنما دیگر موجود نیست.", show_alert=True)
        return
    await callback.message.answer(
        f"❗️ آیا از حذف «{guide['title']}» مطمئن هستید؟",
        reply_markup=admin_guide_delete_confirm_keyboard(guide_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("guidedeleteconfirm_"))
async def admin_guide_delete_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guidedeleteconfirm_", ""))
    guide = db.get_guide(guide_id)
    title = guide["title"] if guide else ""
    db.delete_guide(guide_id)
    guides = db.get_guides()
    await callback.message.answer(
        f"✅ راهنمای «{title}» حذف شد.\n\n📚 مدیریت راهنما و اموزش‌ها\n\nتعداد: {len(guides)}",
        reply_markup=admin_guides_menu(guides),
    )
    await callback.answer("🗑 حذف شد.")


# ---------------------------------------------------------------------------
# 🎬 مدیریت استیکر/ویدیوی تستی هر بخش از منو
# ---------------------------------------------------------------------------
_STICKER_SECTION_ORDER = list(STICKER_SECTION_LABELS.keys())


def _sticker_status(section_key: str):
    """وضعیت فعلی یک بخش را برمی‌گرداند: (is_enabled, has_custom, file_id_or_None)."""
    row = db.get_section_sticker(section_key)
    if row is None:
        return True, False, None  # پیش‌فرض: فعال، بدون سفارشی‌سازی
    return bool(row["is_enabled"]), True, row.get("file_id")


async def _send_sticker_section_detail(target: types.Message, section_key: str):
    label = STICKER_SECTION_LABELS.get(section_key, section_key)
    is_enabled, has_custom, file_id = _sticker_status(section_key)

    if is_enabled:
        status_line = "✅ فعال — سفارشی (آپلود‌شده توسط ادمین)" if has_custom else "➖ فعال — استیکر پیش‌فرض پروژه"
    else:
        status_line = "🛑 غیرفعال — هیچ استیکری نشان داده نمی‌شود"

    text = f"🎬 {label}\n\nوضعیت: {status_line}"
    kb = admin_sticker_detail_keyboard(section_key, has_custom=has_custom, is_enabled=is_enabled)

    try:
        if is_enabled:
            if file_id:
                await target.answer_sticker(file_id)
            else:
                filename = STICKER_FILES.get(section_key)
                if filename:
                    await target.answer_sticker(FSInputFile(os.path.join(STICKERS_DIR, filename)))
    except Exception:
        logger.exception("خطا در پیش‌نمایش استیکر بخش '%s'", section_key)

    await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "admin_stickers")
async def admin_stickers_list(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    sections = []
    for key in _STICKER_SECTION_ORDER:
        is_enabled, has_custom, _fid = _sticker_status(key)
        if not is_enabled:
            emoji = "🛑"
        elif has_custom:
            emoji = "✅"
        else:
            emoji = "➖"
        sections.append({"key": key, "label": STICKER_SECTION_LABELS.get(key, key), "status_emoji": emoji})
    text = (
        "🎬 مدیریت استیکرهای منو\n\n"
        "✅ = سفارشی‌شده، ➖ = پیش‌فرض پروژه، 🛑 = غیرفعال\n\n"
        "یکی از بخش‌ها رو انتخاب کن:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_stickers_menu(sections))
    except Exception:
        await callback.message.answer(text, reply_markup=admin_stickers_menu(sections))
    await callback.answer()


@router.callback_query(F.data.startswith("stickeropen_"))
async def admin_sticker_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    section_key = callback.data.replace("stickeropen_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    await _send_sticker_section_detail(callback.message, section_key)
    await callback.answer()


@router.callback_query(F.data.startswith("stickerset_"))
async def admin_sticker_set_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    section_key = callback.data.replace("stickerset_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_sticker_upload)
    await state.update_data(sticker_section_key=section_key)
    label = STICKER_SECTION_LABELS.get(section_key, section_key)
    await callback.message.answer(
        f"📤 استیکر/فایل موردنظرت رو برای «{label}» بفرست.\n\n"
        "فقط یک استیکر متحرک (ویدیوی) معتبر تلگرام قابل قبوله؛ هر ویدیوی معمولی رو تلگرام به‌عنوان استیکر قبول نمی‌کنه.",
        reply_markup=admin_sticker_cancel_keyboard(section_key),
    )
    await callback.answer()


@router.message(AdminStates.waiting_sticker_upload)
async def admin_sticker_upload_receive(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    section_key = data.get("sticker_section_key")
    if not section_key:
        await state.clear()
        return

    file_id = None
    if message.sticker:
        file_id = message.sticker.file_id
    elif message.document:
        file_id = message.document.file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.animation:
        file_id = message.animation.file_id

    if not file_id:
        await message.answer("❌ فقط استیکر یا فایل/ویدیوی قابل قبوله. دوباره بفرست یا انصراف بده:")
        return

    db.set_section_sticker(section_key, file_id)
    invalidate_section_sticker_cache(section_key)
    await state.clear()
    label = STICKER_SECTION_LABELS.get(section_key, section_key)
    await message.answer(f"✅ استیکر «{label}» ذخیره شد.")
    await _send_sticker_section_detail(message, section_key)


@router.callback_query(F.data.startswith("stickeroff_"))
async def admin_sticker_off(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    section_key = callback.data.replace("stickeroff_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    db.set_section_sticker_enabled(section_key, False)
    invalidate_section_sticker_cache(section_key)
    await _send_sticker_section_detail(callback.message, section_key)
    await callback.answer("🛑 غیرفعال شد.")


@router.callback_query(F.data.startswith("stickeron_"))
async def admin_sticker_on(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    section_key = callback.data.replace("stickeron_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    db.set_section_sticker_enabled(section_key, True)
    invalidate_section_sticker_cache(section_key)
    await _send_sticker_section_detail(callback.message, section_key)
    await callback.answer("✅ فعال شد.")


@router.callback_query(F.data.startswith("stickerreset_"))
async def admin_sticker_reset(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    section_key = callback.data.replace("stickerreset_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    db.reset_section_sticker(section_key)
    invalidate_section_sticker_cache(section_key)
    await _send_sticker_section_detail(callback.message, section_key)
    await callback.answer("♻️ به حالت پیش‌فرض برگشت.")


# ---------------------------------------------------------------------------
# 💾 بکاپ
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_backup")
async def admin_backup(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    try:
        if db.USE_TURSO:
            export_path = "/tmp/backup_export.json"
            db.export_backup_json(export_path)
            backup_file = FSInputFile(export_path, filename="backup.json")
            caption = "💾 بکاپ دیتابیس (Turso — JSON)"
        else:
            backup_file = FSInputFile(DATABASE_PATH)
            caption = "💾 بکاپ دیتابیس"
        await callback.message.answer_document(backup_file, caption=caption)
    except Exception:
        await callback.message.answer("❌ خطا در ساخت بکاپ.")
    await callback.answer()


# ---------------------------------------------------------------------------
# ℹ️ اطلاعات ربات (قالب فروشی)
# ---------------------------------------------------------------------------
def _botinfo_status_text():
    values = bot_info.all_values()
    labels = bot_info.labels()
    lines = ["ℹ️ اطلاعات ربات\n", "برای ویرایش روی هر کدام بزنید:\n"]
    for key, label in labels.items():
        val = values.get(key) or "—"
        lines.append(f"• {label}: {val}")
    channels = bot_info.get_required_channels()
    lines.append(f"\n• کانال‌های اجباری: {len(channels)} عدد")
    return "\n".join(lines)


@router.callback_query(F.data == "botinfo_open")
async def admin_botinfo_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    try:
        await callback.message.edit_text(_botinfo_status_text(), reply_markup=admin_botinfo_menu())
    except TelegramBadRequest:
        await callback.message.answer(_botinfo_status_text(), reply_markup=admin_botinfo_menu())
    await callback.answer()


@router.message(F.text == "ℹ️ اطلاعات ربات")
async def admin_botinfo_open_msg(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(_botinfo_status_text(), reply_markup=admin_botinfo_menu())


@router.callback_query(F.data.startswith("botinfo_edit_"))
async def admin_botinfo_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.replace("botinfo_edit_", "")
    labels = bot_info.labels()
    if key not in labels:
        await callback.answer("❌ یافت نشد.", show_alert=True)
        return
    await state.update_data(botinfo_key=key)
    await state.set_state(AdminStates.waiting_botinfo_value)
    current = bot_info.get(key) or "—"
    await callback.message.answer(
        f"✏️ مقدار جدید برای «{labels[key]}» را بفرستید.\nمقدار فعلی: {current}",
        reply_markup=admin_botinfo_field_keyboard(key),
    )
    await callback.answer()


@router.message(AdminStates.waiting_botinfo_value)
async def admin_botinfo_edit_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("botinfo_key")
    labels = bot_info.labels()
    if not key or key not in labels:
        await state.clear()
        await message.answer("❌ خطای داخلی. دوباره تلاش کنید.")
        return
    value = (message.text or "").strip()
    if key == "config_name_prefix":
        cleaned = re.sub(r"[^A-Za-z0-9_]+", "", value)
        if not cleaned:
            await message.answer(
                "❌ پیشوند باید فقط از حروف/عدد انگلیسی و زیرخط (_) تشکیل شده باشد؛ دوباره وارد کن:"
            )
            return
        value = cleaned
    bot_info.set(key, value)
    await state.clear()
    await message.answer(f"✅ «{labels[key]}» به‌روز شد.", reply_markup=admin_botinfo_menu())


@router.callback_query(F.data == "botinfo_channels")
async def admin_botinfo_channels_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    channels = bot_info.get_required_channels()
    text = "📢 کانال‌های عضویت اجباری\n\nروی هرکدام بزنید تا حذف شود." if channels else "📢 هیچ کانال اجباری ثبت نشده."
    try:
        await callback.message.edit_text(text, reply_markup=admin_botinfo_channels_menu(channels))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=admin_botinfo_channels_menu(channels))
    await callback.answer()


@router.callback_query(F.data == "botinfo_channel_add")
async def admin_botinfo_channel_add_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_botinfo_channel_add)
    await callback.message.answer(
        "➕ افزودن کانال اجباری\n\nفرمت زیر را ارسال کنید (با | جدا شده):\nآیدی/یوزرنیم کانال | نام نمایشی | لینک دعوت\nمثال: -1001234567890 | کانال ما | https://t.me/mychannel",
        reply_markup=admin_botinfo_field_keyboard(""),
    )
    await callback.answer()


@router.message(AdminStates.waiting_botinfo_channel_add)
async def admin_botinfo_channel_add_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    parts = [p.strip() for p in (message.text or "").split("|")]
    if len(parts) != 3 or not parts[0]:
        await message.answer("❌ فرمت نادرست. دوباره تلاش کنید یا /cancel بزنید.")
        return
    raw_chat_id, name, url = parts
    raw_chat_id = clean_numeric_id(raw_chat_id)

    stripped_at = raw_chat_id[1:] if raw_chat_id.startswith("@") else raw_chat_id
    chat_id_input = raw_chat_id if raw_chat_id.startswith("@") or raw_chat_id.startswith("-") or raw_chat_id.lstrip("-").isdigit() else "@" + stripped_at
    try:
        chat = await message.bot.get_chat(chat_id_input)
    except Exception as e:
        await message.answer(
            "❌ ربات نتوانست این کانال را پیدا کند. ممکن است:\n"
            "• فرمت آیدی اشتباه باشد (برای کانال باید با «-100» شروع شود، مثل -1001234567890)\n"
            "• ربات هنوز به این کانال اضافه/عضو نشده باشد\n\n"
            f"خطای دقیق: {e}\n\nدوباره تلاش کنید یا /cancel بزنید."
        )
        return

    try:
        bot_member = await message.bot.get_chat_member(chat.id, message.bot.id)
        if bot_member.status not in ("administrator", "creator"):
            await message.answer(
                "⚠️ ربات عضو این کانال هست ولی «ادمین» نیست. برای اینکه ربات بتواند عضویت کاربرها را در این کانال ببیند، باید ربات را در آن کانال «ادمین» کنی (نه فقط عضو)، بعد دوباره همین پیام را بفرست."
            )
            return
    except Exception as e:
        await message.answer(f"❌ بررسی وضعیت عضویت ربات در این کانال با خطا مواجه شد: {e}\n\nدوباره تلاش کنید یا /cancel بزنید.")
        return

    bot_info.add_required_channel(chat.id, name, url)
    await state.clear()
    channels = bot_info.get_required_channels()
    await message.answer(
        f"✅ کانال اضافه شد و تایید شد که ربات به‌درستی در آن ادمین است (آیدی واقعی: {chat.id}).",
        reply_markup=admin_botinfo_channels_menu(channels),
    )


@router.callback_query(F.data.startswith("botinfo_channel_del_"))
async def admin_botinfo_channel_del(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    chat_id = callback.data.replace("botinfo_channel_del_", "")
    bot_info.remove_required_channel(chat_id)
    channels = bot_info.get_required_channels()
    text = "📢 کانال‌های عضویت اجباری" if channels else "📢 هیچ کانال اجباری ثبت نشده."
    try:
        await callback.message.edit_text(text, reply_markup=admin_botinfo_channels_menu(channels))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=admin_botinfo_channels_menu(channels))
    await callback.answer("✅ حذف شد.")


# ---------------------------------------------------------------------------
# 🎁 تنظیم حجم/مدت (ساعت یا روز) پلن «تست رایگان» از پنل ادمین
# ---------------------------------------------------------------------------
FREE_TEST_MIN_VOLUME_MB = 50
FREE_TEST_MAX_VOLUME_MB = 10240
FREE_TEST_MIN_HOURS = 1
FREE_TEST_MAX_HOURS = 168
FREE_TEST_MIN_DAYS = 1
FREE_TEST_MAX_DAYS = 30
FREE_TEST_MIN_PRICE = 0
FREE_TEST_MAX_PRICE = 5_000_000

_FREE_TEST_UNIT_ALIASES = {
    "ساعت": "hours", "ساعتی": "hours", "ساعته": "hours", "h": "hours", "hour": "hours", "hours": "hours",
    "روز": "days", "روزی": "days", "روزه": "days", "d": "days", "day": "days", "days": "days",
}


def _parse_free_test_unit(text: str):
    return _FREE_TEST_UNIT_ALIASES.get((text or "").strip().lower())


def _free_test_settings_text() -> str:
    plan = db.get_effective_free_test_plan()
    price = plan.get("price", 0)
    price_label = "رایگان" if price == 0 else f"{price:,} تومان"
    return (
        "🎁 تنظیم پلن «تست رایگان»\n\n"
        f"مقدار فعلی: {plan['name']} — قیمت: {price_label}\n\n"
        "برای تغییر، حجم (مگابایت)، مقدار مدت، واحد مدت (ساعت/روز) و قیمت (تومان) را با | جدا و ارسال کنید.\n"
        f"محدوده‌ی مجاز: حجم بین {FREE_TEST_MIN_VOLUME_MB} تا {FREE_TEST_MAX_VOLUME_MB} مگابایت، "
        f"در حالت ساعتی بین {FREE_TEST_MIN_HOURS} تا {FREE_TEST_MAX_HOURS} ساعت، "
        f"در حالت روزانه بین {FREE_TEST_MIN_DAYS} تا {FREE_TEST_MAX_DAYS} روز، "
        f"قیمت بین {FREE_TEST_MIN_PRICE} (رایگان) تا {FREE_TEST_MAX_PRICE:,} تومان.\n"
        "مثال ساعتی: 500 | 12 | ساعت | 1500\n"
        "مثال روزانه: 1024 | 3 | روز | 2000\n\n"
        "برای انصراف /cancel بزنید."
    )


@router.message(F.text == "🎁 تنظیم تست رایگان")
async def admin_free_test_settings_open(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_free_test_settings)
    await message.answer(_free_test_settings_text())


@router.message(AdminStates.waiting_free_test_settings)
async def admin_free_test_settings_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        await message.answer("❌ لغو شد.", reply_markup=admin_reply_keyboard())
        return
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 4:
        await message.answer(
            "❌ فرمت نادرست. مثال درست: 500 | 12 | ساعت | 1500\n\nدوباره تلاش کنید یا /cancel بزنید."
        )
        return
    volume_str, duration_str, unit_str, price_str = parts
    unit = _parse_free_test_unit(unit_str)
    if unit is None:
        await message.answer("❌ واحد مدت نامعتبر است؛ فقط «ساعت» یا «روز» مجاز است.\n\nدوباره تلاش کنید یا /cancel بزنید.")
        return
    volume_mb = parse_int_in_range(volume_str, FREE_TEST_MIN_VOLUME_MB, FREE_TEST_MAX_VOLUME_MB)
    if unit == "hours":
        duration_value = parse_int_in_range(duration_str, FREE_TEST_MIN_HOURS, FREE_TEST_MAX_HOURS)
    else:
        duration_value = parse_int_in_range(duration_str, FREE_TEST_MIN_DAYS, FREE_TEST_MAX_DAYS)
    price = parse_int_in_range(price_str, FREE_TEST_MIN_PRICE, FREE_TEST_MAX_PRICE)
    if volume_mb is None or duration_value is None or price is None:
        unit_range = f"{FREE_TEST_MIN_HOURS} تا {FREE_TEST_MAX_HOURS} ساعت" if unit == "hours" else f"{FREE_TEST_MIN_DAYS} تا {FREE_TEST_MAX_DAYS} روز"
        await message.answer(
            f"❌ مقدار نامعتبر. حجم باید بین {FREE_TEST_MIN_VOLUME_MB} تا {FREE_TEST_MAX_VOLUME_MB} مگابایت، "
            f"مدت باید بین {unit_range} و "
            f"قیمت باید بین {FREE_TEST_MIN_PRICE} تا {FREE_TEST_MAX_PRICE:,} تومان باشد.\n\nدوباره تلاش کنید یا /cancel بزنید."
        )
        return
    try:
        db.set_free_test_override(volume_mb, duration_value, unit, price)
    except Exception as e:
        logger.exception("خطا در ذخیره تنظیمات تست رایگان")
        await message.answer(f"❌ ذخیره تنظیمات ناموفق بود: {e}")
        return
    await state.clear()
    plan = db.get_effective_free_test_plan()
    price_label = "رایگان" if price == 0 else f"{price:,} تومان"
    await message.answer(
        f"✅ پلن «تست رایگان» به‌روزرسانی شد: {plan['name']} — قیمت: {price_label}",
        reply_markup=admin_reply_keyboard(),
    )


# ---------------------------------------------------------------------------
# 🧩 تنظیم قیمت/محدوده‌ی «بساز سرویس خودت» از پنل ادمین
# ---------------------------------------------------------------------------
CUSTOM_BUILD_SETTINGS_MIN_PRICE = 0
CUSTOM_BUILD_SETTINGS_MAX_PRICE = 1_000_000
CUSTOM_BUILD_SETTINGS_MIN_GB_BOUND = 1
CUSTOM_BUILD_SETTINGS_MAX_GB_BOUND = 10_000
CUSTOM_BUILD_SETTINGS_MIN_DAYS_BOUND = 1
CUSTOM_BUILD_SETTINGS_MAX_DAYS_BOUND = 3650


def _custom_build_settings_text() -> str:
    s = db.get_effective_custom_build_settings()
    return (
        "🧩 تنظیم «بساز سرویس خودت»\n\n"
        f"مقدار فعلی: هر گیگابایت {s['price_per_gb']:,} تومان + هر ۳۰ روز {s['price_per_30_days']:,} تومان\n"
        f"محدوده‌ی حجم: {s['min_gb']} تا {s['max_gb']} گیگابایت — محدوده‌ی روز: {s['min_days']} تا {s['max_days']} روز\n\n"
        "برای تغییر، شش مقدار زیر را با | جدا و به ترتیب ارسال کنید:\n"
        "قیمت هر گیگ | قیمت هر ۳۰ روز | حداقل گیگ | حداکثر گیگ | حداقل روز | حداکثر روز\n"
        f"محدوده‌ی مجاز: قیمت‌ها بین {CUSTOM_BUILD_SETTINGS_MIN_PRICE} تا {CUSTOM_BUILD_SETTINGS_MAX_PRICE:,} تومان، "
        f"حجم‌ها بین {CUSTOM_BUILD_SETTINGS_MIN_GB_BOUND} تا {CUSTOM_BUILD_SETTINGS_MAX_GB_BOUND}، "
        f"روزها بین {CUSTOM_BUILD_SETTINGS_MIN_DAYS_BOUND} تا {CUSTOM_BUILD_SETTINGS_MAX_DAYS_BOUND}.\n"
        "مثال: 5000 | 5000 | 5 | 1000 | 30 | 1000\n\n"
        "برای انصراف /cancel بزنید."
    )


@router.message(F.text == "🧩 تنظیم بساز سرویس خودت")
async def admin_custom_build_settings_open(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_custom_build_settings)
    await message.answer(_custom_build_settings_text())


@router.message(AdminStates.waiting_custom_build_settings)
async def admin_custom_build_settings_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        await message.answer("❌ لغو شد.", reply_markup=admin_reply_keyboard())
        return
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 6:
        await message.answer(
            "❌ فرمت نادرست. مثال درست: 5000 | 5000 | 5 | 1000 | 30 | 1000\n\nدوباره تلاش کنید یا /cancel بزنید."
        )
        return
    price_gb_str, price_30d_str, min_gb_str, max_gb_str, min_days_str, max_days_str = parts
    price_per_gb = parse_int_in_range(price_gb_str, CUSTOM_BUILD_SETTINGS_MIN_PRICE, CUSTOM_BUILD_SETTINGS_MAX_PRICE)
    price_per_30_days = parse_int_in_range(price_30d_str, CUSTOM_BUILD_SETTINGS_MIN_PRICE, CUSTOM_BUILD_SETTINGS_MAX_PRICE)
    min_gb = parse_int_in_range(min_gb_str, CUSTOM_BUILD_SETTINGS_MIN_GB_BOUND, CUSTOM_BUILD_SETTINGS_MAX_GB_BOUND)
    max_gb = parse_int_in_range(max_gb_str, CUSTOM_BUILD_SETTINGS_MIN_GB_BOUND, CUSTOM_BUILD_SETTINGS_MAX_GB_BOUND)
    min_days = parse_int_in_range(min_days_str, CUSTOM_BUILD_SETTINGS_MIN_DAYS_BOUND, CUSTOM_BUILD_SETTINGS_MAX_DAYS_BOUND)
    max_days = parse_int_in_range(max_days_str, CUSTOM_BUILD_SETTINGS_MIN_DAYS_BOUND, CUSTOM_BUILD_SETTINGS_MAX_DAYS_BOUND)
    if None in (price_per_gb, price_per_30_days, min_gb, max_gb, min_days, max_days):
        await message.answer(
            "❌ مقدار نامعتبر. دوباره بررسی کنید همه‌ی مقادیر در محدوده‌های مجاز باشند.\n\nدوباره تلاش کنید یا /cancel بزنید."
        )
        return
    if max_gb < min_gb:
        await message.answer("❌ حداکثر گیگ نمی‌تواند کمتر از حداقل گیگ باشد.\n\nدوباره تلاش کنید یا /cancel بزنید.")
        return
    if max_days < min_days:
        await message.answer("❌ حداکثر روز نمی‌تواند کمتر از حداقل روز باشد.\n\nدوباره تلاش کنید یا /cancel بزنید.")
        return
    try:
        db.set_custom_build_override(price_per_gb, price_per_30_days, min_gb, max_gb, min_days, max_days)
    except Exception as e:
        logger.exception("خطا در ذخیره تنظیمات بساز سرویس خودت")
        await message.answer(f"❌ ذخیره تنظیمات ناموفق بود: {e}")
        return
    await state.clear()
    await message.answer(
        f"✅ تنظیمات «بساز سرویس خودت» به‌روزرسانی شد:\n"
        f"هر گیگ {price_per_gb:,} تومان + هر ۳۰ روز {price_per_30_days:,} تومان\n"
        f"حجم: {min_gb} تا {max_gb} گیگ — روز: {min_days} تا {max_days} روز",
        reply_markup=admin_reply_keyboard(),
    )


# ---------------------------------------------------------------------------
# 🩺 سلامت ربات — داشبورد یکجای سلامت سیستم برای ادمین (ویژگی اضافه)
# ---------------------------------------------------------------------------
@router.message(F.text == "🩺 سلامت ربات")
async def admin_health_check(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    lines = ["🩺 سلامت ربات\n"]

    # دیتابیس
    try:
        user_count = db.count_users()
        lines.append(f"✅ دیتابیس: سالم — {user_count} کاربر")
    except Exception as e:
        lines.append(f"❌ دیتابیس: خطا — {e}")

    # پنل‌های VPN
    try:
        panels = db.list_vpn_panels()
        if panels:
            enabled = [p for p in panels if p.get("enabled")]
            disabled = [p for p in panels if not p.get("enabled")]
            type_labels = {"shahrah": "شاهراه", "marzban": "مرزبان", "pasargad": "پاسارگاد"}
            lines.append(f"\n🖥 پنل‌های VPN: {len(panels)} عدد ببت‌شده ({len(enabled)} فعال / {len(disabled)} غیرفعال)")
            for p in panels:
                status_icon = "🟢" if p.get("enabled") else "⚪️"
                ptype = type_labels.get(p.get("panel_type"), p.get("panel_type"))
                lines.append(f"  {status_icon} {p.get('name')} ({ptype})")
        else:
            lines.append("\n⚠️ پنل‌های VPN: هیچ پنلی ثبت نشده")
    except Exception as e:
        lines.append(f"\n❌ پنل‌های VPN: خطا — {e}")

    # صف کارهای معوق
    try:
        pending_receipts = len(db.get_pending_receipts(limit=1000))
        pending_orders = len(db.get_pending_orders(limit=1000))
        pending_custom = len(db.get_pending_custom_orders(limit=1000))
        pending_custom_receipts = len(db.get_pending_custom_order_receipts(limit=1000))
        total_pending = pending_receipts + pending_orders + pending_custom + pending_custom_receipts
        icon = "⚠️" if total_pending > 0 else "✅"
        lines.append(
            f"\n{icon} صف کارها: رسید در انتظار {pending_receipts} | سفارش در انتظار ارسال {pending_orders} | "
            f"بساز-خودت پرداخت‌شده {pending_custom} | بساز-خودت رسید در انتظار {pending_custom_receipts}"
        )
    except Exception as e:
        lines.append(f"\n❌ صف کارها: خطا — {e}")

    # اطلاعات ربات (فیلدهای خالی مهم)
    try:
        values = bot_info.all_values()
        important_keys = ["card_number", "card_holder", "support_url", "bot_username"]
        labels = bot_info.labels()
        missing = [labels.get(k, k) for k in important_keys if not values.get(k)]
        if missing:
            lines.append(f"\n⚠️ اطلاعات ربات: فیلدهای خالی مهم: {', '.join(missing)}")
        else:
            lines.append("\n✅ اطلاعات ربات: همه‌ی فیلدهای مهم پر شده‌اند")
        channels = bot_info.get_required_channels()
        lines.append(f"📢 کانال‌های اجباری: {len(channels)} عدد")
    except Exception as e:
        lines.append(f"\n❌ اطلاعات ربات: خطا — {e}")

    # تنظیمات تست رایگان و بساز سرویس خودت
    try:
        ft = db.get_effective_free_test_plan()
        cb = db.get_effective_custom_build_settings()
        lines.append(f"\n🎁 تست رایگان فعلی: {ft['name']} — {ft['price']:,} تومان")
        lines.append(
            f"🧩 بساز سرویس خودت: هر گیگ {cb['price_per_gb']:,} + هر ۳۰ روز {cb['price_per_30_days']:,} تومان "
            f"({cb['min_gb']}-{cb['max_gb']} گیگ / {cb['min_days']}-{cb['max_days']} روز)"
        )
    except Exception as e:
        lines.append(f"\n❌ تنظیمات فروش: خطا — {e}")

    # وضعیت سفارش‌ها (باز/بسته)
    try:
        orders_state = "🟢 باز" if db.is_orders_enabled() else "🔴 بسته"
        lines.append(f"\n🛍 وضعیت سفارش‌ها: {orders_state}")
    except Exception as e:
        lines.append(f"\n❌ وضعیت سفارش‌ها: خطا — {e}")

    await message.answer("\n".join(lines), reply_markup=admin_reply_keyboard())
