"""
handlers/profile.py
پروفایل حرفه‌ای کاربر (👤 کاربران) و تاریخچه خرید.
زیرمنوهای کیف پول آزاد/مسدود، تاریخچه تراکنش، و لینک دعوت
در فایل‌های wallet.py و referral.py پیاده شده‌اند (روی همون callback_dataها).
"""

from aiogram import Router, F, types

import database as db
from utils import show_menu_with_sticker
from keyboards import profile_menu, back_button

router = Router(name="profile")


@router.callback_query(F.data == "profile")
async def profile(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("ابتدا دستور /start را بزنید.", show_alert=True)
        return

    configs_count = len(db.get_configs(user["id"]))

    text = (
        f"👤 پروفایل حرفه‌ای شما\n\n"
        f"📛 نام: {user['name']}\n"
        f"🆔 آیدی: {user['telegram_id']}\n\n"
        f"💰 موجودی قابل استفاده: {user['wallet']:,} تومان\n"
        f"🔒 موجودی در انتظار: {user['locked_wallet']:,} تومان\n\n"
        f"📦 تعداد سرویس: {configs_count}\n"
        f"🛒 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 تاریخ عضویت: {user['joined']}\n\n"
        f"👥 تعداد دعوت: {user['invited_count']} | دعوت موفق: {user['successful_invites']}"
    )
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "profile", text, reply_markup=profile_menu())
    await callback.answer()


@router.callback_query(F.data == "purchase_history")
async def purchase_history(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("ابتدا دستور /start را بزنید.", show_alert=True)
        return

    txs = db.get_transactions(user["id"], limit=50)
    purchases = [tx for tx in txs if tx["type"] == "purchase"]

    if not purchases:
        text = "🛒 شما هنوز خریدی انجام نداده‌اید."
    else:
        text = "🛒 تاریخچه خرید شما:\n\n"
        for tx in purchases[:15]:
            text += f"📦 {tx['description']} | {tx['amount']:,} تومان | {tx['created_at']}\n"

    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "purchase_history", text, reply_markup=back_button("profile", "🔙 بازگشت"))
    await callback.answer()
