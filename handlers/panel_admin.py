"""
handlers/panel_admin.py
مدیریت یکپارچه‌شده‌ی هر سه نوع پنل (شاهراه / مرزبان / پاسارگارد).

⚠️ جایگزین handlers/shahrah_admin.py قدیمی (یکپنلی، فقط شاهراه). این ماژول:
- هر سه نوع پنل را هم‌زمان پشتیبانی می‌کند (هر سه در یک لحظه می‌توانند فعال باشند).
- هر نوع پنل می‌تواند چند نمونه (Instance) هم‌زمان داشته باشد (مدیریت در دکمه‌های جداگانه).
- نگاشت پلن/بسته در سطح "کدام نمونه‌ی پنل" انجام می‌شود (تا ادمین بتواند برای هر پلن/بسته تعیین
  کند دقیقاً از کدام نمونه‌ی پنل استفاده شود).

پلتفرم واقعی هر پنل فقط از ماژول panels.py صدا می‌شود (هیچ‌وقت مستقیم به shahrah.py /
marzban_panel.py / pasargad_panel.py وصل نمی‌شود) تا رفتار برای هر سه نوع یکسان بماند.

۰️⃣ قانون ارسال VIP بر اساس روش پرداخت (بدون تفاوت با قبل):
- کیف پول و پرداخت آنلاین: اگر پلن/دسته یک نگاشت فعال به یک نمونه‌ی پنل داشته باشد، سرویس
  بلافاصله و کاملاً خودکار از همون نمونه ساخته و برای مشتری ارسال می‌شود. اگر نگاشتی
  وجود ندارد یا نمونه‌ی مقصد غیرفعال است، دقیقاً متل قبل به ادمین اطلاع داده می‌شود تا خودش دستی
  ارسال کند.
- کارت‌به‌کارت: تفاوتی نکرده — بعد از تایید رسید توسط ادمین، دکمه‌ی «ارسال خودکار از پنل»
  همان مقصدی که برای این پلن نگاشت شده را نشان می‌دهد.

⚠️ Gaming کاملاً و عمداً خارج از این ماژول نگه داشته شده: ارسال کانفیگ گیمینگ همیشه ۱۰۰٪ دستی می‌ماند.
"""

import html
import json
import logging
from datetime import datetime, timedelta
from io import BytesIO

from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

import database as db
import crypto
import panels
import fsm_storage
import bot_info
from config import ADMIN_ID
from states import AdminStates
from keyboards import (
    admin_vpn_panel_types_keyboard,
    admin_vpn_panel_list_keyboard,
    admin_vpn_panel_detail_keyboard,
    admin_vpn_panel_delete_confirm_keyboard,
    admin_vpn_panel_edit_menu_keyboard,
    vpn_panel_back_keyboard,
    admin_vpn_panel_types_cancel_keyboard,
    admin_vpn_panel_map_menu_keyboard,
    vpn_map_category_pick_keyboard,
    vpn_map_vip_category_pick_keyboard,
    vpn_map_vip_plans_keyboard,
    vpn_catalog_pick_keyboard,
    config_delivery_keyboard,
)
from utils import is_duplicate_action, now_tehran_naive
from handlers.admin import _is_admin, _log_fulfilled_order

router = Router(name="panel_admin")
logger = logging.getLogger(__name__)

try:
    import qrcode
except ImportError:
    qrcode = None  # اگر نصب نباشد، لینک به‌جای عکس QR به‌صورت متنی فرستاده می‌شود.


def _make_qr_bytes(link: str) -> bytes:
    img = qrcode.make(link)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _pretty(data, limit: int = 1500) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False, indent=2)
    except Exception:
        text = str(data)
    if len(text) > limit:
        text = text[:limit] + "\n... (بریده‌شد)"
    return html.escape(text)


def _admin_fsm(bot) -> FSMContext | None:
    """FSMContext مربوط به چت خود ادمین، برای زمانی که می‌خواهیم از یک مسیر
    قیرتعاملی (متل بعد از پرداخت کیف‌پول/آنلاین که در چت مشتری اتفاق می‌افتد)
    همان مکانیزم «لطفاً لینک رو دستی بفرست» را روی چت ادمین صدا بزنیم.
    اگر هنوز storage ثبت نشده باشد (مثلاً در تست) None برمی‌گرداند."""
    if fsm_storage.storage is None:
        return None
    return FSMContext(
        storage=fsm_storage.storage,
        key=StorageKey(bot_id=bot.id, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )


# ---------------------------------------------------------------------------
# 🤖 ارسال کاملاً خودکار بعد از پرداخت کیف‌پول/آنلاین (بدون دخالت ادمین)
# فقط VIP و «بساز سرویس خودت» — Gaming هرگز از این مسیر عبور نمی‌کند.
# اگر هیچ نمونه‌ی پنلی برای این پلن/بسته نگاشت نشده یا غیرفعال باشد، False برمی‌گرداند
# تا مسیر همیشگی (اطلاع دستی به ادمین) دنبال شود و هیچ سفارشی گم نشود.
# ---------------------------------------------------------------------------
async def auto_fulfill_vip_via_panel(bot, uid, plan_key: str, order_id: int | None) -> bool:
    mapping = db.get_panel_map_for_plan_key(plan_key)
    if not mapping or not mapping.get("enabled"):
        return False

    plan = db.get_effective_plan(plan_key)
    user = db.get_user(uid)
    if not plan or not user:
        return False

    panel = db.get_vpn_panel(mapping["panel_id"])
    if not panel or not panel.get("enabled"):
        return False

    username = f"tg{uid}_{int(datetime.now().timestamp())}"
    ok, link, remote_service_id, data, msg = await panels.create_service(
        panel, username, mapping["remote_ref"],
        volume_gb=plan.get("volume_gb"), days=plan.get("days"),
    )
    if not ok:
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ خرید VIP (کیف‌پول/پرداخت آنلاین) قرار بود خودکار از {panels.panel_label(panel)} ارسال شود ولی "
            f"ساخت سرویس در پنل ناموفق بود:\n{msg}\n"
            f"🔑 مرجع ارسال‌شده: {mapping['remote_ref']}\n\n"
            "لطفاً از دکمه‌ی ارسال دستی زیر همین سفارش استفاده کن. اگه خطا NOT_FOUND بود، احتمالاً باید "
            "این نگاشت رو دوباره از مدیریت این پنل تنظیم کنی.",
        )
        return False

    await bot.send_message(
        ADMIN_ID, f"📨 پاسخ پنل {panels.panel_label(panel)} (ارسال خودکار بعد از پرداخت):\n<pre>{_pretty(data)}</pre>",
        parse_mode="HTML",
    )

    snapshot = {"name": plan.get("name"), "volume_gb": plan.get("volume_gb"), "days": plan.get("days")}
    ctx = {"uid": uid, "plan_key": plan_key, "order_id": order_id, "order_kind": "plan",
           "panel_id": panel["id"], "panel_type": panel["panel_type"], "service_id": remote_service_id,
           "snapshot": snapshot}

    if not link:
        admin_state = _admin_fsm(bot)
        if admin_state:
            await admin_state.update_data(panel_pending_ctx=ctx)
            await admin_state.set_state(AdminStates.waiting_panel_manual_link)
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ سرویس در {panels.panel_label(panel)} ساخته شد (خودکار، بعد از پرداخت) ولی لینک ساب به‌صورت "
            "خودکار پیدا نشد.\nلطفاً لینک رو از پاسخ بالا کپی و همینجا برام بفرست:",
        )
        return True

    await _deliver_panel_link(bot, ctx, link)
    return True


async def auto_fulfill_custom_via_panel(bot, user: dict, order_id: int, volume, days, custom_name) -> bool:
    """معادل تابع بالا، ولی برای سفارش‌های «بساز سرویس خودت». چون این سفارش‌ها
    دسته‌بندی ثابت ندارند (حجم/مدت دلخواه مشتری‌ست)، یک نگاشت پیش‌فرض واحد
    (scope='custom_build') استفاده می‌شود که به یک نمونه‌ی پنل خاص (از هر سه نوع) اشاره می‌کند."""
    mapping = db.get_panel_plan_map_with_panel("custom_build", 0)
    if not mapping or not mapping.get("enabled"):
        return False

    panel = db.get_vpn_panel(mapping["panel_id"])
    if not panel or not panel.get("enabled"):
        return False

    username = f"tg{user['telegram_id']}_{int(datetime.now().timestamp())}"
    ok, link, remote_service_id, data, msg = await panels.create_service(
        panel, username, mapping["remote_ref"], volume_gb=volume, days=days,
    )
    if not ok:
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ سفارش «بساز سرویس خودت» (کیف‌پول/پرداخت آنلاین) قرار بود خودکار از {panels.panel_label(panel)} ارسال "
            f"شود ولی ساخت سرویس ناموفق بود:\n{msg}\n"
            f"🔑 مرجع ارسال‌شده: {mapping['remote_ref']}\n\n"
            "لطفاً از دکمه‌ی ارسال دستی این سفارش استفاده کن.",
        )
        return False

    await bot.send_message(
        ADMIN_ID, f"📨 پاسخ پنل {panels.panel_label(panel)} (ارسال خودکار بعد از پرداخت):\n<pre>{_pretty(data)}</pre>",
        parse_mode="HTML",
    )

    snapshot = {"name": custom_name or "سرویس سفارشی", "volume_gb": volume, "days": days}
    ctx = {"uid": user["telegram_id"], "plan_key": None, "order_id": order_id, "order_kind": "custom",
           "panel_id": panel["id"], "panel_type": panel["panel_type"], "service_id": remote_service_id,
           "snapshot": snapshot}

    if not link:
        admin_state = _admin_fsm(bot)
        if admin_state:
            await admin_state.update_data(panel_pending_ctx=ctx)
            await admin_state.set_state(AdminStates.waiting_panel_manual_link)
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ سرویس در {panels.panel_label(panel)} ساخته شد (خودکار، بعد از پرداخت) ولی لینک ساب به‌صورت "
            "خودکار پیدا نشد.\nلطفاً لینک رو از پاسخ بالا کپی و همینجا برام بفرست:",
        )
        return True

    await _deliver_panel_link(bot, ctx, link)
    return True


# ---------------------------------------------------------------------------
# 📂 قدم اول: انتخاب نوع پنل (همه هم‌زمان قابل مدیریت) و لیست نمونه‌ها
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_vpn_panels")
async def open_vpn_panel_types(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🖥 مدیریت پنل‌های VPN\n\n"
        "هر سه نوع پنل (شاهراه/مرزبان/پاسارگارد) می‌توانند هم‌زمان فعال باشند و هرکدام می‌تواند چند نمونه داشته باشد.\n"
        "یک نوع رو انتخاب کن:",
        reply_markup=admin_vpn_panel_types_keyboard(),
    )
    await callback.answer()


@router.message(F.text == "🖥 مدیریت پنل‌های VPN")
async def menu_admin_vpn_panels(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "🖥 مدیریت پنل‌های VPN\n\n"
        "هر سه نوع پنل (شاهراه/مرزبان/پاسارگارد) می‌توانند هم‌زمان فعال باشند و هرکدام می‌تواند چند نمونه داشته باشد.\n"
        "یک نوع رو انتخاب کن:",
        reply_markup=admin_vpn_panel_types_keyboard(),
    )


@router.callback_query(F.data.startswith("vpntype|"))
async def open_vpn_panel_type_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_type = callback.data.split("|")[1]
    if panel_type not in panels.PANEL_TYPES:
        await callback.answer("❌ نوع پنل نامعتبر.", show_alert=True)
        return
    instances = db.list_vpn_panels(panel_type=panel_type)
    label = panels.PANEL_TYPE_LABELS[panel_type]
    text = f"🖥 نمونه‌های پنل {label}"
    if not instances:
        text += "\n\nهنوز هیچ نمونه‌ای از این نوع اضافه نشده. می‌تونی چند نمونه هم‌زمان از این نوع اضافه کنی."
    await callback.message.edit_text(text, reply_markup=admin_vpn_panel_list_keyboard(panel_type, instances))
    await callback.answer()


@router.callback_query(F.data.startswith("vpndetail|"))
async def open_vpn_panel_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    status = "🟢 فعال" if panel.get("enabled") else "🔴 غیرفعال"
    text = (
        f"🖥 {panels.panel_label(panel)}\n"
        f"وضعیت: {status}\n"
        f"🌐 ادرس: <code>{html.escape(panel.get('base_url') or '')}</code>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_vpn_panel_detail_keyboard(panel))
    await callback.answer()


@router.callback_query(F.data.startswith("vpntest|"))
async def vpn_panel_test(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await callback.answer("⏳ در حال تست اتصال...")
    ok, data, msg = await panels.test_connection(panel)
    if not ok:
        await callback.message.answer(f"❌ اتصال ناموفق: {msg}", reply_markup=vpn_panel_back_keyboard(panel_id))
        return
    await callback.message.answer(
        f"✅ اتصال به {panels.panel_label(panel)} برقرار است.\n<pre>{_pretty(data)}</pre>",
        parse_mode="HTML", reply_markup=vpn_panel_back_keyboard(panel_id),
    )


@router.callback_query(F.data.startswith("vpntoggle|"))
async def vpn_panel_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    db.update_vpn_panel(panel_id, enabled=not panel.get("enabled"))
    panel = db.get_vpn_panel(panel_id)
    status = "🟢 فعال" if panel.get("enabled") else "🔴 غیرفعال"
    text = (
        f"🖥 {panels.panel_label(panel)}\n"
        f"وضعیت: {status}\n"
        f"🌐 ادرس: <code>{html.escape(panel.get('base_url') or '')}</code>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_vpn_panel_detail_keyboard(panel))
    await callback.answer("✅ وضعیت به‌روز شد.")


@router.callback_query(F.data.startswith("vpndelete|"))
async def vpn_panel_delete_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        f"⚠️ مطمئنی می‌خوای {panels.panel_label(panel)} حذف شود؟\n"
        "تمام نگاشت‌های پلن/بسته مربوط به این نمونه هم حذف می‌شوند (سرویس‌های قبلی سالم می‌مانند).",
        reply_markup=admin_vpn_panel_delete_confirm_keyboard(panel_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vpndeleteconfirm|"))
async def vpn_panel_delete(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    panel_type = panel["panel_type"]
    db.delete_vpn_panel(panel_id)
    instances = db.list_vpn_panels(panel_type=panel_type)
    await callback.message.edit_text(
        f"🗑 حذف شد. نمونه‌های فعلی پنل {panels.PANEL_TYPE_LABELS[panel_type]}:",
        reply_markup=admin_vpn_panel_list_keyboard(panel_type, instances),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# ➕ افزودن نمونه‌ی جدید از یک نوع پنل (FSM)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("vpnadd|"))
async def vpn_panel_add_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    panel_type = callback.data.split("|")[1]
    if panel_type not in panels.PANEL_TYPES:
        await callback.answer("❌ نوع پنل نامعتبر.", show_alert=True)
        return
    await state.update_data(new_panel_type=panel_type)
    await state.set_state(AdminStates.waiting_panel_name)
    await callback.message.edit_text(
        f"➕ افزودن پنل {panels.PANEL_TYPE_LABELS[panel_type]} جدید\n\n"
        "یک نام دلخواه برای این نمونه بفرست (فقط برای تشخیص خودت در لیست، مثلاً «سرور 1 المان»):",
        reply_markup=admin_vpn_panel_types_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_panel_name)
async def vpn_panel_add_name(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام خالی معتبر نیست. دوباره بفرست:")
        return
    await state.update_data(new_panel_name=name)
    await state.set_state(AdminStates.waiting_panel_base_url)
    await message.answer("🌐 ادرس پایه (base URL) این پنل رو بفرست (مثلاً https://panel.example.com):")


@router.message(AdminStates.waiting_panel_base_url)
async def vpn_panel_add_base_url(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    base_url = (message.text or "").strip()
    if not base_url.lower().startswith(("http://", "https://")):
        await message.answer("❌ این یک ادرس معتبر نیست؛ لطفاً با http یا https بفرست:")
        return
    data = await state.get_data()
    panel_type = data.get("new_panel_type")
    await state.update_data(new_panel_base_url=base_url.rstrip("/"))
    if panel_type == "shahrah":
        await state.set_state(AdminStates.waiting_panel_api_key)
        await message.answer("🔑 API Key این نمونه رو بفرست:")
    else:
        await state.set_state(AdminStates.waiting_panel_username)
        await message.answer("👤 نام کاربری این نمونه رو بفرست:")


@router.message(AdminStates.waiting_panel_api_key)
async def vpn_panel_add_api_key(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    api_key = (message.text or "").strip()
    if not api_key:
        await message.answer("❌ API Key خالی معتبر نیست. دوباره بفرست:")
        return
    data = await state.get_data()
    panel_id = db.create_vpn_panel(
        data["new_panel_type"], data["new_panel_name"], data["new_panel_base_url"], api_key=api_key,
    )
    await state.clear()
    panel = db.get_vpn_panel(panel_id)
    await message.answer(
        f"✅ پنل {panels.panel_label(panel)} اضافه شد.",
        reply_markup=admin_vpn_panel_detail_keyboard(panel),
    )


@router.message(AdminStates.waiting_panel_username)
async def vpn_panel_add_username(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    username = (message.text or "").strip()
    if not username:
        await message.answer("❌ نام کاربری خالی معتبر نیست. دوباره بفرست:")
        return
    await state.update_data(new_panel_username=username)
    await state.set_state(AdminStates.waiting_panel_password)
    await message.answer("🔐 رمز عبور این نمونه رو بفرست:")


@router.message(AdminStates.waiting_panel_password)
async def vpn_panel_add_password(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    password = (message.text or "").strip()
    if not password:
        await message.answer("❌ رمز عبور خالی معتبر نیست. دوباره بفرست:")
        return
    data = await state.get_data()
    panel_id = db.create_vpn_panel(
        data["new_panel_type"], data["new_panel_name"], data["new_panel_base_url"],
        username=data["new_panel_username"], password=password,
    )
    await state.clear()
    panel = db.get_vpn_panel(panel_id)
    await message.answer(
        f"✅ پنل {panels.panel_label(panel)} اضافه شد.",
        reply_markup=admin_vpn_panel_detail_keyboard(panel),
    )


# ---------------------------------------------------------------------------
# ✏️ ویرایش یک نمونه‌ی موجود
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("vpnedit|"))
async def vpn_panel_edit_menu(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        f"✏️ ویرایش {panels.panel_label(panel)}\nیک فیلد رو انتخاب کن:",
        reply_markup=admin_vpn_panel_edit_menu_keyboard(panel),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vpneditfield|"))
async def vpn_panel_edit_field_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, field = callback.data.split("|")
    panel_id = int(panel_id_str)
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    field_labels = {
        "name": "نام", "base_url": "ادرس پایه (base URL)", "api_key": "API Key",
        "username": "نام کاربری", "password": "رمز عبور",
    }
    await state.update_data(edit_panel_id=panel_id, edit_panel_field=field)
    await state.set_state(AdminStates.waiting_panel_edit_field)
    await callback.message.edit_text(
        f"✅ {field_labels.get(field, field)} جدید رو برای {panels.panel_label(panel)} بفرست:",
        reply_markup=vpn_panel_back_keyboard(panel_id),
    )
    await callback.answer()


@router.message(AdminStates.waiting_panel_edit_field)
async def vpn_panel_edit_field_received(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("❌ مقدار خالی معتبر نیست. دوباره بفرست:")
        return
    data = await state.get_data()
    panel_id = data.get("edit_panel_id")
    field = data.get("edit_panel_field")
    panel = db.get_vpn_panel(panel_id) if panel_id else None
    if not panel or not field:
        await message.answer("❌ مشکلی پیش آمد؛ لطفاً از ابتدا دوباره تلاش کن.")
        await state.clear()
        return
    if field == "base_url":
        value = value.rstrip("/")
    db.update_vpn_panel(panel_id, **{field: value})
    await state.clear()
    panel = db.get_vpn_panel(panel_id)
    await message.answer(
        f"✅ ذخیره شد. {panels.panel_label(panel)} به‌روز شد.",
        reply_markup=admin_vpn_panel_detail_keyboard(panel),
    )


# ---------------------------------------------------------------------------
# 🗂 نگاشت پلن/بسته → این نمونه‌ی مشخص از پنل
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("vpnmap|"))
async def vpn_panel_map_menu(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        f"🗂 نگاشت پلن/بسته به {panels.panel_label(panel)}\n\n"
        "کدام بخش رو می‌خوای به این نمونه وصل کنی؟",
        reply_markup=admin_vpn_panel_map_menu_keyboard(panel_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vpnmapvip|"))
async def vpn_map_vip(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    cats = db.get_vip_categories()
    if not cats:
        await callback.answer("هنوز هیچ دسته‌بندی VIP‌ای ساخته نشده.", show_alert=True)
        return
    await callback.message.edit_text(
        f"🗂 یک دسته‌بندی VIP رو انتخاب کن تا پلن‌های داخلش رو ببینی و برای هرکدوم جداگانه به {panels.panel_label(panel)} وصلشون کنی:",
        reply_markup=vpn_map_vip_category_pick_keyboard(cats, panel_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vpnmapvipcat|"))
async def vpn_map_vip_cat_pick(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, cat_id_str = callback.data.split("|")
    panel_id, cat_id = int(panel_id_str), int(cat_id_str)
    plans_list = db.get_vip_plans(cat_id)
    if not plans_list:
        await callback.answer("این دسته‌بندی هنوز هیچ پلنی نداره.", show_alert=True)
        return
    await callback.message.edit_text(
        "یک پلن رو انتخاب کن تا بسته/تمپلیت متناظرش از این نمونه پنل رو مشخص کنی:",
        reply_markup=vpn_map_vip_plans_keyboard(cat_id, plans_list, panel_id),
    )
    await callback.answer()


async def _open_catalog_picker(callback: types.CallbackQuery, state: FSMContext, panel: dict, panel_id: int,
                                scope: str, scope_id: int, prompt: str):
    await callback.answer("⏳ در حال دریافت لیست از پنل...")
    choices, msg = await panels.get_catalog(panel)
    if not choices:
        await callback.message.answer(f"❌ {msg}", reply_markup=vpn_panel_back_keyboard(panel_id))
        return
    await state.update_data(panel_map_scope=scope, panel_map_scope_id=scope_id,
                             panel_map_choices=choices, panel_map_panel_id=panel_id)
    await callback.message.answer(prompt, reply_markup=vpn_catalog_pick_keyboard(choices, panel_id))


@router.callback_query(F.data.startswith("vpnmapvipplan|"))
async def vpn_map_vip_plan_pick(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, cat_id_str, plan_id_str = callback.data.split("|")
    panel_id, plan_id = int(panel_id_str), int(plan_id_str)
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await _open_catalog_picker(
        callback, state, panel, panel_id, "vip_plan", plan_id,
        f"یک بسته/تمپلیت از {panels.panel_label(panel)} رو انتخاب کن تا به این پلن مشخص وصل بشه:",
    )


@router.callback_query(F.data.startswith("vpnmapcustom|"))
async def vpn_map_custom_build(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await _open_catalog_picker(
        callback, state, panel, panel_id, "custom_build", 0,
        f"🧩 این بسته/تمپلیت، پیش‌فرض همه‌ی سفارش‌های «بساز سرویس خودت» از {panels.panel_label(panel)} خواهد بود. یک بسته انتخاب کن:",
    )


@router.callback_query(F.data.startswith("vpnmapfreetest|"))
async def vpn_map_free_test(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    panel_id = int(callback.data.split("|")[1])
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await _open_catalog_picker(
        callback, state, panel, panel_id, "free_test", 0,
        f"🧪 این بسته/تمپلیت برای همه‌ی سفارش‌های «تست رایگان» از {panels.panel_label(panel)} استفاده خواهد شد. یک بسته کوچک انتخاب کن:",
    )


@router.callback_query(F.data.startswith("vpnmapcat|"))
async def vpn_map_cat_pick(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, scope, cat_id_str = callback.data.split("|")
    panel_id, cat_id = int(panel_id_str), int(cat_id_str)
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await _open_catalog_picker(
        callback, state, panel, panel_id, scope, cat_id,
        f"یک بسته/تمپلیت از {panels.panel_label(panel)} رو انتخاب کن تا به این دسته‌بندی وصل بشه:",
    )


@router.callback_query(F.data.startswith("vpncatalogpick|"))
async def vpn_catalog_pick_set(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, panel_id_str, idx_str = callback.data.split("|")
    panel_id, idx = int(panel_id_str), int(idx_str)
    data = await state.get_data()
    choices = data.get("panel_map_choices") or []
    scope = data.get("panel_map_scope")
    scope_id = data.get("panel_map_scope_id")
    stored_panel_id = data.get("panel_map_panel_id")
    chosen = next((c for c in choices if c["idx"] == idx), None)
    if not chosen or not scope or scope_id is None or stored_panel_id != panel_id:
        await callback.answer("❌ این انتخاب منقضی شده؛ دوباره از منو وارد شو.", show_alert=True)
        return

    db.set_panel_plan_map(scope, int(scope_id), panel_id, chosen["ref"], chosen["name"])

    # fix: اگر این یک نگاشت پیش‌فرض برای کل دسته بود (scope=vip_category)، نگاشت‌های اختصاصی
    # قدیمی‌تر تک‌تک پلن‌های همان دسته را هم پاک می‌کنیم تا پیش‌فرض جدید واقعاً
    # روی همه‌ی پلن‌های دسته اعمال شود (ورنه فقط برای پلن‌های بدون نگاشت اختصاصی).
    cleared_note = ""
    if scope == "vip_category":
        db.clear_panel_plan_overrides_for_category(int(scope_id))
        cleared_note = (
            "\n\n♻️ نگاشت‌های اختصاصی قدیمی پلن‌های این دسته (اگر وجود داشت) پاک شد تا این "
            "پیش‌فرض واقعاً روی همه‌ی پلن‌های این دسته اعمال شود."
        )

    panel = db.get_vpn_panel(panel_id)
    await callback.message.edit_text(
        f"✅ ذخیره شد: این بخش از این پس به «{chosen['name']}» (ref: {chosen['ref']}) از {panels.panel_label(panel)} وصل می‌شود.{cleared_note}",
        reply_markup=admin_vpn_panel_map_menu_keyboard(panel_id),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 📤 ارسال خودکار بعد از تایید رسید (کارت‌به‌کارت — بر اساس نگاشت ازقبل تعیین‌شده)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("panelsend|"))
async def panel_send_service(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    if is_duplicate_action(f"panelsend_{callback.data}"):
        await callback.answer("⚠️ این عملیات چند لحظه پیش انجام شد.", show_alert=True)
        return

    _, uid, plan_key, order_id_str = callback.data.split("|")
    order_id = int(order_id_str) if order_id_str and order_id_str != "0" else None

    plan = db.get_effective_plan(plan_key)
    user = db.get_user(uid)
    if not plan or not user:
        await callback.answer("❌ کاربر یا پلن یافت نشد.", show_alert=True)
        return

    mapping = db.get_panel_map_for_plan_key(plan_key)
    if not mapping or not mapping.get("enabled"):
        await callback.answer("❌ برای دسته‌بندی این پلن هنوز هیچ پنل فعالی نگاشت نشده.", show_alert=True)
        return
    panel = db.get_vpn_panel(mapping["panel_id"])
    if not panel:
        await callback.answer("❌ نمونه پنل نگاشت‌شده پیدا نشد.", show_alert=True)
        return

    await callback.answer(f"⏳ در حال ساخت سرویس در {panels.panel_label(panel)}...")
    username = f"tg{uid}_{int(datetime.now().timestamp())}"
    ok, link, remote_service_id, data, msg = await panels.create_service(
        panel, username, mapping["remote_ref"], volume_gb=plan.get("volume_gb"), days=plan.get("days"),
    )
    if not ok:
        await callback.message.answer(
            f"❌ ساخت سرویس در {panels.panel_label(panel)} ناموفق بود: {msg}\n\n"
            f"🔑 مرجع ارسال‌شده: <code>{html.escape(str(mapping['remote_ref']))}</code>\n"
            "اگه این خطا مربوط به پیدا‌نشدن بسته/تمپلیت باشد، از مدیریت همین نمونه پنل دوباره نگاشت کن.",
            parse_mode="HTML",
        )
        return

    await callback.message.answer(f"📨 پاسخ {panels.panel_label(panel)}:\n<pre>{_pretty(data)}</pre>", parse_mode="HTML")

    snapshot = {"name": plan.get("name"), "volume_gb": plan.get("volume_gb"), "days": plan.get("days")}
    ctx = {"uid": uid, "plan_key": plan_key, "order_id": order_id, "order_kind": "plan",
           "panel_id": panel["id"], "panel_type": panel["panel_type"], "service_id": remote_service_id,
           "snapshot": snapshot}

    if not link:
        await state.update_data(panel_pending_ctx=ctx)
        await state.set_state(AdminStates.waiting_panel_manual_link)
        await callback.message.answer(
            "⚠️ سرویس ساخته شد ولی نتونستم لینک ساب رو خودکار پیدا کنم.\n"
            "لطفاً لینک ساب رو از پاسخ بالا کپی و همینجا ارسال کن:"
        )
        return

    await _deliver_panel_link(callback.bot, ctx, link)


# ---------------------------------------------------------------------------
# 🧩 ساخت خودکار «بساز سرویس خودت» از یک پنل انتخابی (کارت‌به‌کارت)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("panelcustom_"))
async def panel_custom_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    order_id = int(callback.data.replace("panelcustom_", ""))
    order = db.get_custom_order(order_id)
    if order is None:
        await callback.answer("❌ سفارش یافت نشد.", show_alert=True)
        return
    enabled_panels = db.list_vpn_panels(enabled_only=True)
    if not enabled_panels:
        await callback.answer("❌ هیچ پنل فعالی وجود ندارد.", show_alert=True)
        return
    await state.update_data(panel_custom_order_id=order_id)
    buttons = [
        [types.InlineKeyboardButton(
            text=f"{panels.panel_label(p)}", callback_data=f"panelcustompanel|{order_id}|{p['id']}",
        )]
        for p in enabled_panels
    ]
    await callback.answer()
    await callback.message.answer(
        f"🧩 سفارش «بساز سرویس خودت» #{order_id} — {order['volume_gb']} گیگ / {order['days']} روز\n"
        "یکی از پنل‌های فعال رو انتخاب کن:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("panelcustompanel|"))
async def panel_custom_pick_panel(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, order_id_str, panel_id_str = callback.data.split("|")
    order_id, panel_id = int(order_id_str), int(panel_id_str)
    panel = db.get_vpn_panel(panel_id)
    if not panel:
        await callback.answer("❌ این نمونه پنل پیدا نشد.", show_alert=True)
        return
    await callback.answer("⏳ در حال دریافت لیست از پنل...")
    choices, msg = await panels.get_catalog(panel)
    if not choices:
        await callback.message.answer(f"❌ {msg}")
        return
    await state.update_data(panel_custom_order_id=order_id, panel_custom_panel_id=panel_id,
                             panel_custom_choices=choices)
    buttons = [
        [types.InlineKeyboardButton(text=c["label"], callback_data=f"panelcustompick|{order_id}|{panel_id}|{c['idx']}")]
        for c in choices
    ]
    await callback.message.answer(
        f"نزدیک‌ترین بسته/تمپلیت از {panels.panel_label(panel)} رو انتخاب کن:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("panelcustompick|"))
async def panel_custom_pick(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, order_id_str, panel_id_str, idx_str = callback.data.split("|")
    order_id, panel_id, idx = int(order_id_str), int(panel_id_str), int(idx_str)
    data_state = await state.get_data()
    choices = data_state.get("panel_custom_choices") or []
    chosen = next((c for c in choices if c["idx"] == idx), None)
    order = db.get_custom_order(order_id)
    panel = db.get_vpn_panel(panel_id)
    if not chosen or not order or not panel:
        await callback.answer("❌ این انتخاب منقضی شده؛ دوباره تلاش کن.", show_alert=True)
        return
    user = db.get_user_by_id(order["user_id"])
    if not user:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    await callback.answer(f"⏳ در حال ساخت سرویس در {panels.panel_label(panel)}...")
    username = f"tg{user['telegram_id']}_{int(datetime.now().timestamp())}"
    ok, link, remote_service_id, data, msg = await panels.create_service(
        panel, username, chosen["ref"], volume_gb=order["volume_gb"], days=order["days"],
    )
    if not ok:
        await callback.message.answer(
            f"❌ ساخت سرویس در {panels.panel_label(panel)} ناموفق بود: {msg}\n"
            f"🔑 مرجع ارسال‌شده: <code>{html.escape(chosen['ref'])}</code>",
            parse_mode="HTML",
        )
        return

    await callback.message.answer(f"📨 پاسخ {panels.panel_label(panel)}:\n<pre>{_pretty(data)}</pre>", parse_mode="HTML")

    snapshot = {"name": order.get("custom_name") or "سرویس سفارشی",
                "volume_gb": order["volume_gb"], "days": order["days"]}
    ctx = {"uid": user["telegram_id"], "plan_key": None, "order_id": order_id, "order_kind": "custom",
           "panel_id": panel["id"], "panel_type": panel["panel_type"], "service_id": remote_service_id,
           "snapshot": snapshot}

    if not link:
        await state.update_data(panel_pending_ctx=ctx)
        await state.set_state(AdminStates.waiting_panel_manual_link)
        await callback.message.answer(
            "⚠️ سرویس ساخته شد ولی نتونستم لینک ساب رو خودکار پیدا کنم.\n"
            "لطفاً لینک ساب رو از پاسخ بالا کپی و همینجا ارسال کن:"
        )
        return

    await _deliver_panel_link(callback.bot, ctx, link)


@router.message(AdminStates.waiting_panel_manual_link)
async def panel_manual_link_received(message: types.Message, state: FSMContext):
    link = (message.text or "").strip()
    if not link.lower().startswith(("http://", "https://")):
        await message.answer("❌ این یک لینک معتبر نیست؛ لطفاً لینک ساب رو با http یا https ارسال کن:")
        return
    data = await state.get_data()
    ctx = data.get("panel_pending_ctx")
    if not ctx:
        await message.answer("❌ مشکلی پیش آمد؛ لطفاً از ابتدا دکمه‌ی ارسال خودکار رو دوباره بزن:")
        await state.clear()
        return
    await _deliver_panel_link(message.bot, ctx, link)
    await state.clear()


async def _deliver_panel_link(bot, ctx: dict, link: str):
    """سرویس ساخته‌شده از هر یک از سه نوع پنل را در دیتابیس ذخیره و برای کاربر ارسال می‌کند
    (دقیقاً همان قالب/تجربه‌ی ارسال دستی، فقط بدون نیاز به آپلود دستی عکس/لینک)."""
    uid = ctx["uid"]
    plan_key = ctx.get("plan_key")
    order_id = ctx.get("order_id")
    order_kind = ctx.get("order_kind")
    panel_id = ctx.get("panel_id")
    panel_type = ctx.get("panel_type")
    service_id = ctx.get("service_id")
    snap = ctx.get("snapshot") or {}

    user = db.get_user(uid)
    if user is None:
        await bot.send_message(ADMIN_ID, "❌ کاربر یافت نشد؛ سرویس در پنل ساخته شد ولی ارسال نشد.")
        return

    name = snap.get("name") or "کاربر"
    volume_gb = snap.get("volume_gb")
    days = snap.get("days")
    volume_text = f"{volume_gb} گیگابایت" if volume_gb else "طبق بسته‌ی انتخابی"
    days_text = f"{days} روز" if days else "نامحدود"
    expiry_date = (now_tehran_naive() + timedelta(days=days)).strftime("%Y-%m-%d") if days else None

    caption = (
        "✅ سرویس با موفقیت ایجاد شد\n\n"
        f"👤 نام کاربری سرویس : {name}\n"
        "🇺🇳 لوکیشن: مولتی لوکیشن+تانل\n"
        f"⏳ مدت زمان: {days_text}\n"
        f"🗜 حجم سرویس: {volume_text}\n"
        "👤 تعداد کاربر:نامحدود\n\n"
        "لینک اتصال:\n"
        f"{link}\n\n"
        "🧑‍🦯 شما میتوانید شیوه اتصال را با فشردن دکمه زیر دریافت کنید."
    )

    encrypted = crypto.encrypt_config(link)
    plan_name = f"{name} | {volume_text} | {days_text}"
    config_type = db.plan_type(plan_key) if plan_key else "vip"

    config_id = db.add_config(
        user["id"], plan_name, encrypted, expiry=expiry_date,
        config_type=config_type, service_id=service_id, source=panel_type, panel_id=panel_id,
    )

    sent_photo_file_id = None
    try:
        if qrcode:
            photo = types.BufferedInputFile(_make_qr_bytes(link), filename="qr.png")
            sent = await bot.send_photo(
                int(uid), photo, caption=caption, reply_markup=config_delivery_keyboard(bot_info.get("connection_guide_url"))
            )
            if sent.photo:
                sent_photo_file_id = sent.photo[-1].file_id
        else:
            await bot.send_message(
                int(uid), caption, reply_markup=config_delivery_keyboard(bot_info.get("connection_guide_url"))
            )
        await bot.send_message(ADMIN_ID, "✅ سرویس به‌صورت خودکار ساخته و برای کاربر ارسال شد.")
    except Exception as e:
        await bot.send_message(ADMIN_ID, f"⚠️ سرویس ساخته و ذخیره شد ولی ارسال پیام به کاربر ناموفق بود: {e}")

    if sent_photo_file_id:
        db.set_config_qr(config_id, sent_photo_file_id)

    if order_kind == "plan" and order_id:
        db.set_order_status(order_id, "fulfilled")
    elif order_kind == "custom" and order_id:
        db.set_custom_order_status(order_id, "fulfilled")

    await _log_fulfilled_order(
        bot, user,
        plan_order_id=order_id if order_kind == "plan" else None,
        custom_order_id=order_id if order_kind == "custom" else None,
        service_id=service_id, service_name=name,
        package_text=f"{volume_text} | {days_text}", expiry_text=expiry_date or "نامدود",
    )


# ---------------------------------------------------------------------------
# 🔁 مدیریت سرویس‌های ساخته‌شده از هر یک از سه نوع پنل (از صفحه‌ی جزئیات سرویس)
# نمونه‌ی پنل هر سرویس از روی configs.panel_id دقیقاً همونی که سرویس روی اون ساخته شده پیدا می‌شود
# (حتی اگر بعداً نمونه‌های تکراری دیگه‌ای از همون نوع اضافه شوند).
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("panelrenew_"))
async def panel_renew_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    cfg_id = int(callback.data.replace("panelrenew_", ""))
    cfg = db.get_config_by_id(cfg_id)
    if not cfg or not cfg.get("panel_id") or not cfg.get("service_id"):
        await callback.answer("❌ این سرویس از هیچ پنلی ساخته نشده.", show_alert=True)
        return
    panel = db.get_vpn_panel(cfg["panel_id"])
    if not panel:
        await callback.answer("❌ نمونه پنل مربوط به این سرویس دیگر وجود ندارد.", show_alert=True)
        return

    await callback.answer(f"⏳ در حال دریافت لیست از {panels.panel_label(panel)}...")
    choices, msg = await panels.get_catalog(panel)
    if not choices:
        await callback.message.answer(f"❌ {msg}")
        return

    await state.update_data(panel_renew_cfg_id=cfg_id, panel_renew_choices=choices)
    buttons = [
        [types.InlineKeyboardButton(text=c["label"], callback_data=f"panelrenewpick|{cfg_id}|{c['idx']}")]
        for c in choices
    ]
    await callback.message.answer(
        f"یک بسته/تمپلیت از {panels.panel_label(panel)} برای تمدید این سرویس انتخاب کن:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("panelrenewpick|"))
async def panel_renew_pick(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    _, cfg_id_str, idx_str = callback.data.split("|")
    cfg_id, idx = int(cfg_id_str), int(idx_str)
    data_state = await state.get_data()
    choices = data_state.get("panel_renew_choices") or []
    chosen = next((c for c in choices if c["idx"] == idx), None)
    cfg = db.get_config_by_id(cfg_id) if cfg_id else None
    if not chosen or not cfg or not cfg.get("panel_id"):
        await callback.answer("❌ این انتخاب منقضی شده؛ دوباره تلاش کن.", show_alert=True)
        return
    panel = db.get_vpn_panel(cfg["panel_id"])
    if not panel:
        await callback.answer("❌ نمونه پنل پیدا نشد.", show_alert=True)
        return

    await callback.answer(f"⏳ در حال تمدید در {panels.panel_label(panel)}...")
    ok, link, remote_service_id, data, msg = await panels.renew_service(panel, cfg["service_id"], chosen["ref"])
    if not ok:
        await callback.message.answer(
            f"❌ تمدید ناموفق بود: {msg}\n"
            f"🔑 service id: <code>{html.escape(str(cfg['service_id']))}</code> | "
            f"مرجع ارسال‌شده: <code>{html.escape(chosen['ref'])}</code>",
            parse_mode="HTML",
        )
        return

    await callback.message.answer(f"📨 پاسخ {panels.panel_label(panel)}:\n<pre>{_pretty(data)}</pre>", parse_mode="HTML")
    new_service_id = remote_service_id or cfg["service_id"]
    if link:
        encrypted = crypto.encrypt_config(link)
        db.update_config(cfg_id, cfg["plan"], encrypted, expiry=cfg.get("expiry"), service_id=new_service_id,
                          panel_id=cfg["panel_id"])
        await callback.message.answer(
            "✅ سرویس تمدید شد و لینک جدید ذخیره شد.\n"
            "(اگر لینک ساب عوض شده، حتماً به کاربر هم اطلاع بده.)"
        )
    else:
        db.update_config(cfg_id, cfg["plan"], cfg["config"], expiry=cfg.get("expiry"), service_id=new_service_id,
                          panel_id=cfg["panel_id"])
        await callback.message.answer(
            "⚠️ تمدید در پنل انجام شد ولی لینک جدید به‌صورت خودکار پیدا نشد؛ لطفاً از «✒️ تقییر لینک ساب» برای ثبت دستی لینک استفاده کن."
        )


@router.callback_query(F.data.startswith("paneldisable_"))
async def panel_disable(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    cfg_id = int(callback.data.replace("paneldisable_", ""))
    cfg = db.get_config_by_id(cfg_id)
    if not cfg or not cfg.get("panel_id") or not cfg.get("service_id"):
        await callback.answer("❌ این سرویس از هیچ پنلی ساخته نشده.", show_alert=True)
        return
    panel = db.get_vpn_panel(cfg["panel_id"])
    if not panel:
        await callback.answer("❌ نمونه پنل مربوط پیدا نشد.", show_alert=True)
        return
    ok, msg = await panels.disable_service(panel, cfg["service_id"])
    await callback.answer(f"✅ در {panels.panel_label(panel)} غیرفعال شد." if ok else f"❌ {msg}", show_alert=True)


@router.callback_query(F.data.startswith("panelenable_"))
async def panel_enable(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    cfg_id = int(callback.data.replace("panelenable_", ""))
    cfg = db.get_config_by_id(cfg_id)
    if not cfg or not cfg.get("panel_id") or not cfg.get("service_id"):
        await callback.answer("❌ این سرویس از هیچ پنلی ساخته نشده.", show_alert=True)
        return
    panel = db.get_vpn_panel(cfg["panel_id"])
    if not panel:
        await callback.answer("❌ نمونه پنل مربوط پیدا نشد.", show_alert=True)
        return
    ok, msg = await panels.enable_service(panel, cfg["service_id"])
    await callback.answer(f"✅ در {panels.panel_label(panel)} فعال شد." if ok else f"❌ {msg}", show_alert=True)
