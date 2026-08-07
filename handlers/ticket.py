"""
handlers/ticket.py
سیستم پشتیبانی ساده: کاربر پیام می‌فرستد، برای ادمین فوروارد می‌شود،
ادمین با ریپلای روی همان پیام پاسخ می‌دهد.
"""

from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext

import database as db
from utils import show_menu_with_sticker
from states import UserStates, AdminStates
from config import ADMIN_ID
from keyboards import back_button, ticket_reply_keyboard, support_menu

router = Router(name="ticket")


@router.callback_query(F.data == "support")
async def support_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "support", 
        "👨‍💻 پشتیبانی\n\nمی‌تونی مستقیم تیکت بزنی یا از کانال اصلی و پشتیبان استفاده کنی 👇",
        reply_markup=support_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "ticket")
async def ticket_start(callback: types.CallbackQuery, state: FSMContext):
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "ticket_write", 
        "👨‍💻 پیام خود را برای پشتیبانی بنویسید:",
        reply_markup=back_button("back", "🔙 انصراف"),
    )
    await state.set_state(UserStates.waiting_ticket_message)
    await callback.answer()


# ---------------------------------------------------------------------------
# پاسخ ادمین به تیکت (سمت ادمین)
# این handler باید قبل از handler عمومی پیام تیکت ثبت شود، چون فیلتر
# خاص‌تری دارد (فقط ADMIN_ID) و aiogram اولین handler منطبق را اجرا می‌کند.
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("replyticket_"))
async def admin_reply_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    target_uid = callback.data.replace("replyticket_", "")
    await state.update_data(reply_target=target_uid)
    await state.set_state(AdminStates.waiting_ticket_reply)
    await callback.message.answer(f"✏️ پاسخ خود را برای کاربر {target_uid} بنویسید:")
    await callback.answer()


@router.message(AdminStates.waiting_ticket_reply, F.from_user.id == ADMIN_ID)
async def admin_reply_send(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_uid = data.get("reply_target")

    if not target_uid:
        await state.clear()
        return

    try:
        await message.bot.send_message(
            int(target_uid), f"💬 پاسخ پشتیبانی:\n\n{message.text}"
        )
        await message.answer("✅ پاسخ ارسال شد.")
    except Exception:
        await message.answer("❌ ارسال پاسخ ناموفق بود (شاید کاربر ربات را بلاک کرده).")

    await state.clear()


@router.message(UserStates.waiting_ticket_message)
async def ticket_message(message: types.Message, state: FSMContext):
    uid = str(message.from_user.id)

    await message.bot.send_message(
        ADMIN_ID,
        f"🎫 تیکت جدید\n👤 {message.from_user.full_name}\n🆔 {uid}\n\n💬 {message.text}",
        reply_markup=ticket_reply_keyboard(uid),
    )
    await message.answer("✅ پیام شما برای پشتیبانی ارسال شد. به‌زودی پاسخ داده می‌شود.")
    await state.clear()
