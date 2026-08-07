"""
handlers/start.py
دستور /start، بررسی عضویت اجباری در کانال‌ها، و پردازش لینک دعوت اختصاصی
(/start BVPNXXXXX).

نکته: منطق بررسی عضویت کانال‌ها (check_membership) دست‌نخورده باقی مانده.
"""

import logging

from aiogram import Router, F, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.enums import ChatMemberStatus

import database as db
import bot_info
from utils import show_menu_with_sticker
from keyboards import (
    join_channels_keyboard,
    main_reply_keyboard,
    admin_reply_keyboard,
)
from config import ADMIN_ID, REFERRAL_LOCK_AMOUNT, REFERRAL_MIN_VOLUME_GB

router = Router(name="start")
logger = logging.getLogger(__name__)


async def check_membership(bot, user_id: int) -> list:
    not_joined = []
    for ch in bot_info.get_required_channels():
        try:
            member = await bot.get_chat_member(ch["id"], user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                not_joined.append(ch)
        except Exception as e:
            logger.error(f"check_membership failed for channel {ch['id']}: {e}")
            not_joined.append(ch)
    return not_joined


def _ensure_user(telegram_id, full_name: str, referrer_code: str | None = None):
    """کاربر را اگر وجود نداشت می‌سازد؛ کد دعوت معتبر را هم پاس می‌دهد.
    این تابع فقط باید بعد از تأیید عضویت کاربر در کانال‌های اجباری صدا زده شود،
    چون همین‌جا رکورد دعوت ساخته و ۴۰,۰۰۰ تومان در کیف پول مسدود معرف قفل می‌شود."""
    return db.create_user(telegram_id, full_name, referrer_invite_code=referrer_code)


async def _notify_referrer_of_new_join(bot, user: dict):
    """
    وقتی عضویت یک کاربر تازه (که از لینک دعوت وارد شده) در کانال‌ها تأیید می‌شود،
    یک پیام حاوی آیدی و نام او برای معرفش ارسال می‌شود تا بداند چه کسی از طریق
    لینک او وارد ربات شده است.
    """
    if not user or not user.get("referrer_id"):
        return

    referrer = db.get_user_by_id(user["referrer_id"])
    if referrer is None:
        return

    try:
        await bot.send_message(
            int(referrer["telegram_id"]),
            f"🎉 یک عضو جدید از طریق لینک دعوت شما وارد ربات شد و عضویتش تأیید شد!\n\n"
            f"👤 نام: {user['name']}\n"
            f"🆔 آیدی: `{user['telegram_id']}`\n\n"
            f"💰 پس از اینکه این کاربر یک خرید حجم {REFERRAL_MIN_VOLUME_GB} گیگ یا بیشتر انجام دهد، "
            f"{REFERRAL_LOCK_AMOUNT:,} تومان به‌صورت خودکار به کیف پول شما آزاد می‌شود.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"failed to notify referrer {referrer['telegram_id']}: {e}")


def _welcome_text(first_name: str) -> str:
    return bot_info.get_welcome_text(first_name)


def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


@router.message(Command("start"))
async def start(message: types.Message, command: CommandObject, state: FSMContext):
    user_id = message.from_user.id
    not_joined = await check_membership(message.bot, user_id)

    referrer_code = command.args.strip() if command.args else None
    # کد دعوت را تا زمان تأیید عضویت کاربر در کانال‌ها نگه می‌داریم تا رسماً
    # ثبت نشود و پاداش معرف زودتر از موعد قفل نشود.
    if referrer_code:
        await state.update_data(pending_referrer_code=referrer_code)

    if not_joined:
        # 🐛 فیکس: قبلاً اینجا فقط یک پیام متنی بدون استیکر فرستاده می‌شد، برای
        # همین وقتی که کاربر برای اولین بار /start می‌زد و هنوز عضو کانال‌ها نشده، استیکر
        # «شروع با /start» اصلاً دیده نمی‌شد (فقط بعد از تأیید عضویت در check_join). حالا
        # همین استیکر درست بالای لیست کانال‌های اجباری هم نشان داده می‌شود.
        # show_main_keyboard=False عمداً پاس داده شده چون عضویت کاربر هنوز تأیید نشده و نباید منوی
        # دائمی پایین صفحه زودتر از موعد فعال شود.
        await show_menu_with_sticker(
            message.bot, message.chat.id, "start_welcome",
            "⚠️ برای استفاده از ربات ابتدا در کانال‌های زیر عضو شوید:",
            reply_markup=join_channels_keyboard(not_joined),
            show_main_keyboard=False,
        )
        return

    if db.is_user_blocked(user_id):
        await message.answer("🚫 دسترسی شما به ربات مسدود شده است. در صورت وجود ابهام با پشتیبانی در ارتباط باشید.")
        return

    data = await state.get_data()
    referrer_code = referrer_code or data.get("pending_referrer_code")
    existed_before = db.get_user(user_id) is not None
    user = _ensure_user(user_id, message.from_user.full_name, referrer_code)
    await state.update_data(pending_referrer_code=None)

    if not existed_before:
        await _notify_referrer_of_new_join(message.bot, user)

    if _is_admin(user_id):
        await message.answer(
            "👨‍💻 به پنل مدیریت خوش آمدید!\n\nهمه‌ی امکانات مدیریتی از منوی پایین صفحه قابل دسترسی است ✅",
            reply_markup=admin_reply_keyboard(),
        )
        return

    await show_menu_with_sticker(
        message.bot, message.chat.id, "start_welcome",
        _welcome_text(message.from_user.first_name), reply_markup=main_reply_keyboard(),
    )


@router.callback_query(F.data == "check_join")
async def check_join(callback: types.CallbackQuery, state: FSMContext):
    not_joined = await check_membership(callback.bot, callback.from_user.id)
    if not_joined:
        await callback.answer("❌ هنوز در همه کانال‌ها عضو نشدید!", show_alert=True)
        return

    if db.is_user_blocked(callback.from_user.id):
        await callback.message.edit_text("🚫 دسترسی شما به ربات مسدود شده است.")
        await callback.answer()
        return

    # فقط همین‌جا (بعد از تأیید واقعی عضویت) کاربر رسماً ثبت و پاداش معرف قفل می‌شود.
    data = await state.get_data()
    referrer_code = data.get("pending_referrer_code")
    existed_before = db.get_user(callback.from_user.id) is not None
    user = _ensure_user(callback.from_user.id, callback.from_user.full_name, referrer_code)
    await state.update_data(pending_referrer_code=None)

    if not existed_before:
        await _notify_referrer_of_new_join(callback.bot, user)

    if _is_admin(callback.from_user.id):
        await callback.message.edit_text("👨‍💻 به پنل مدیریت خوش آمدید! همه‌ی امکانات مدیریتی از منوی پایین صفحه قابل دسترسی است ✅")
        await callback.message.answer("منوی مدیریتی فعال شد:", reply_markup=admin_reply_keyboard())
    else:
        await callback.message.edit_text(_welcome_text(callback.from_user.first_name))
        await show_menu_with_sticker(
            callback.bot, callback.message.chat.id, "join_confirmed",
            "منوی اصلی در پایین صفحه فعال شد ✅", reply_markup=main_reply_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "back")
async def go_back(callback: types.CallbackQuery):
    """بازگشت از زیرمنوهای اینلاین؛ دیگر منوی اصلی اینلاین دوباره ارسال نمی‌شود؛
    تمام مسیرها از طریق همین منوی دائمی پایین صفحه در دسترس است.

    🐛 فیکس: قبلاً اینجا فقط متن پیام فعلی ویرایش می‌شد، پس اگر بالای همان منو یک
    استیکر وجود داشت، روی صفحه باقی می‌ماند. حالا از show_menu_with_sticker استفاده
    می‌شود تا همزمان با بستن منو، استیکرش هم حذف شود و منوی دائمی پایین صفحه
    هم دوباره تازه/فعال شود."""
    if _is_admin(callback.from_user.id):
        await callback.message.edit_text("👨‍💻 بازگشت به منوی اصلی — از منوی پایین صفحه ادامه دهید ✅")
    else:
        await show_menu_with_sticker(
            callback.bot, callback.message.chat.id, None,
            "👋 بازگشت به منوی اصلی — از منوی پایین صفحه ادامه دهید ✅",
            reply_markup=main_reply_keyboard(),
        )
    await callback.answer()


