"""
states.py
تمام Stateهای FSM ربات اینجا تعریف می‌شوند تا در همه‌ی handlerها
قابل import و استفاده باشند.
"""

from aiogram.fsm.state import State, StatesGroup


class UserStates(StatesGroup):
    waiting_custom_charge = State()
    waiting_charge_receipt = State()
    waiting_ticket_message = State()
    waiting_agency_request_message = State()  # درخواست نمایندگی (دکمه‌ی منوی پایین)
    waiting_broadcast = State()
    waiting_config = State()
    waiting_discount_code = State()
    waiting_discount_plan = State()          # کد تخفیف وارد شده هنگام خرید یک پلن خاص
    waiting_card_purchase_receipt = State()  # رسید پرداخت کارت‌به‌کارت برای خرید سرویس

    # «بساز سرویس خودت» و «تمدید سرویس» (هر دو از یک مسیر مشترک رد می‌شوند)
    waiting_custom_volume = State()
    waiting_custom_days = State()
    waiting_custom_name = State()
    waiting_custom_card_receipt = State()


class AdminStates(StatesGroup):
    waiting_custom_amount = State()
    waiting_search_user = State()
    waiting_config_text = State()
    waiting_discount_code_step = State()
    waiting_discount_percent_step = State()
    waiting_discount_uses_step = State()
    waiting_discount_value_step = State()   # مقدار تخفیف (درصد یا مبلغ ثابت)
    waiting_discount_plans_step = State()   # پلن‌های قابل‌اعمال (all یا لیست plan_key با کاما)
    waiting_discount_users_step = State()   # آیدی‌های عددی مجاز به استفاده (خالی/۰ یعنی همه)

    # ✏️ ویرایش یک کد تخفیف موجود (از صفحه‌ی جزئیات کد)
    waiting_discount_edit_value = State()   # ویرایش درصد/مبلغ
    waiting_discount_edit_uses = State()    # ویرایش تعداد استفاده‌ی باقی‌مانده
    waiting_discount_edit_users = State()   # ویرایش آیدی‌های مجاز
    waiting_discount_edit_min_order = State()   # ویرایش حداقل مبلغ سفارش
    waiting_discount_edit_max_per_user = State()  # ویرایش سقف استفاده‌ی هر کاربر
    waiting_discount_edit_expiry = State()  # ویرایش تاریخ انقضا

    # نمایندگی (تخفیف خودکار روی VIP برای یک آیدی عددی خاص)
    waiting_agent_id_step = State()
    waiting_agent_percent_step = State()
    waiting_agent_edit_percent = State()  # تغییر درصد تخفیف یک نماینده‌ی موجود (از داخل صفحه‌ی نماینده)

    # ویرایش نام/قیمت پلن‌های VIP و Gaming از پنل ادمین
    waiting_plan_edit_name = State()
    waiting_plan_edit_price = State()

    # 🗂 دسته‌بندی‌های VIP (بخش ۶) — افزودن دسته‌ی جدید و افزودن/ویرایش پلن داخل هر دسته
    waiting_vip_category_name = State()
    waiting_vip_category_description = State()   # 📝 ویرایش توضیح بالای دکمه‌های پلن‌های این دسته
    waiting_vip_plan_name = State()   # مرحله‌ی ۱ از ۴ افزودن پلن جدید
    waiting_vip_plan_price = State()  # مرحله‌ی ۲ از ۴
    waiting_vip_plan_gb = State()     # مرحله‌ی ۳ از ۴
    waiting_vip_plan_days = State()   # مرحله‌ی ۴ از ۴
    waiting_vip_plan_edit_name = State()
    waiting_vip_plan_edit_price = State()
    waiting_vip_plan_edit_gb = State()
    waiting_vip_plan_edit_days = State()

    # 🎮 دسته‌بندی‌های Gaming — دقیقاً مثل بخش VIP بالا
    waiting_gaming_category_name = State()
    waiting_gaming_plan_name = State()   # مرحله‌ی ۱ از ۴ افزودن پلن جدید
    waiting_gaming_plan_price = State()  # مرحله‌ی ۲ از ۴
    waiting_gaming_plan_gb = State()     # مرحله‌ی ۳ از ۴
    waiting_gaming_plan_days = State()   # مرحله‌ی ۴ از ۴
    waiting_gaming_plan_edit_name = State()
    waiting_gaming_plan_edit_price = State()
    waiting_gaming_plan_edit_gb = State()
    waiting_gaming_plan_edit_days = State()

    # ارسال کانفیگ VIP با کیوآرکد + لینک ساب (mirroring خودکار)
    waiting_send_qr_photo = State()
    waiting_send_qr_link = State()
    waiting_send_qr_manual = State()  # فقط اگر تشخیص خودکار از روی لینک شکست بخورد

    # ارسال کانفیگ گیمینگ (شناسه سرویس + ساب + آپلود چند فایل .conf)
    waiting_gaming_service_id = State()
    waiting_gaming_sub_link = State()
    waiting_gaming_files = State()

    # مدیریت سرویس‌های کاربران توسط ادمین (ادیت لینک ساب / کیوآرکد / افزودن فایل گیمینگ)
    waiting_edit_sublink = State()
    waiting_edit_qr = State()
    waiting_add_gaming_file = State()

    # 🔗 اتصال پنل‌های شاهراه/مرزبان/پاسارگارد — فقط برای وقتی که تشخیص خودکار
    # لینک ساب از پاسخ create/renew ممکن نشود و لازم باشد ادمین یک‌بار دستی
    # لینک را وارد کند. (قبلاً فقط مخصوص شاهراه بود؛ حالا برای هر سه نوع پنل.)
    waiting_panel_manual_link = State()

    # 🆕 چندپنلی: افزودن/ویرایش یک نمونه‌ی پنل (شاهراه/مرزبان/پاسارگارد)
    waiting_panel_name = State()          # مرحله‌ی ۱: برچسب دلخواه این نمونه
    waiting_panel_base_url = State()      # مرحله‌ی ۲: آدرس پایه
    waiting_panel_api_key = State()       # فقط شاهراه: کلید API
    waiting_panel_username = State()      # فقط مرزبان/پاسارگارد: نام کاربری ادمین
    waiting_panel_password = State()      # فقط مرزبان/پاسارگارد: رمز عبور ادمین
    waiting_panel_edit_field = State()    # ویرایش یک فیلد خاص از یک نمونه‌ی موجود

    # ✉️ پیام خصوصی ادمین به یک کاربر خاص (از بخش مدیریت کاربر/جستجوی)
    waiting_pm_message = State()

    # ↩️ پاسخ ادمین به یک تیکت پشتیبانی (state جدا از UserStates.waiting_ticket_message
    # تا اگر ادمین خودش هم یک تیکت عادی بزند، با این حالت قاطی نشود)
    waiting_ticket_reply = State()

    # 📚 مدیریت راهنما — افزودن/ویرایش محتوای هر قطعه راهنما (متن/عکس/فیلم)
    waiting_guide_title = State()
    waiting_guide_content = State()
    waiting_guide_edit_title = State()
    waiting_guide_edit_content = State()

    # 🎬 مدیریت استیکر/ویدیوی تستی هر بخش از منو (تست رایگان/خرید اشتراک/
    # انتخاب پلن/بساز کانفیگ)
    waiting_sticker_upload = State()

    # ℹ️ اطلاعات ربات (ویرایش متن خوش‌آمدگویی، شماره کارت، لینک پشتیبانی و ...)
    waiting_botinfo_value = State()
    waiting_botinfo_channel_add = State()

    # 🎁 تنظیم حجم/مدت (ساعت یا روز)/قیمت پلن تست رایگان
    waiting_free_test_settings = State()

    # 🧩 تنظیم قیمت/محدوده‌ی «بساز سرویس خودت»
    waiting_custom_build_settings = State()
