"""
handlers/referral.py
نمایش لینک دعوت اختصاصی، کد اختصاصی، و آمار دعوت دوستان
(تعداد دعوت، دعوت‌های موفق، مبلغ آزاد شده، مبلغ در انتظار).
"""

from aiogram import Router, F, types

import database as db
import bot_info
from utils import show_menu_with_sticker
from config import REFERRAL_LOCK_AMOUNT, REFERRAL_MIN_VOLUME_GB
from keyboards import referral_menu

router = Router(name="referral")


@router.callback_query(F.data == "referral")
async def referral(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer("ابتدا دستور /start را بزنید.", show_alert=True)
        return

    stats = db.get_referral_stats(user["id"])
    invite_link = f"https://t.me/{bot_info.get('bot_username')}?start={stats['invite_code']}"

    text = (
        f"👥 دعوت دوستان و کسب درآمد 💸\n\n"
        f"دوستانتو دعوت کن و به‌ازای هر دعوت موفق، {REFERRAL_LOCK_AMOUNT:,} تومان پاداش نقدی بگیر! 🎁\n\n"
        f"🔗 لینک اختصاصی شما:\n{invite_link}\n\n"
        f"🔑 کد اختصاصی: {stats['invite_code']}\n\n"
        f"👤 تعداد دعوت: {stats['invited_count']}\n"
        f"✅ دعوت‌های موفق: {stats['successful_invites']}\n"
        f"🔓 مبلغ آزاد شده: {stats['released_amount']:,} تومان\n"
        f"🔒 مبلغ در انتظار: {user['locked_wallet']:,} تومان\n\n"
        f"ℹ️ به‌ازای هر دوستی که با لینک شما عضو شود و یک خرید حجم {REFERRAL_MIN_VOLUME_GB} گیگ یا بیشتر "
        f"انجام دهد، {REFERRAL_LOCK_AMOUNT:,} تومان به‌صورت خودکار به کیف پول شما آزاد می‌شود. "
        f"(تست رایگان و خریدهای زیر {REFERRAL_MIN_VOLUME_GB} گیگ پاداش را آزاد نمی‌کنند)"
    )
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "referral", text, reply_markup=referral_menu())
    await callback.answer()
