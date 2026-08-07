"""
webapp_api.py
بک‌اند Flask برای Mini App تلگرام. کنار ربات (bot.py) و به‌صورت جدا اجرا می‌شود
و از همان database.py / crypto.py / config.py استفاده می‌کند تا دقیقاً همان
منطق و اعداد ربات تکرار شود (بدون هیچ رقم/قیمت جدید).

=============================================================================
نکات امنیتی مهم (لطفاً قبل از تغییر این فایل بخوانید):
=============================================================================
۱) هرگز به user_id ارسالی از سمت کلاینت (فرانت‌اند) اعتماد نکنید. تنها منبع
   قابل‌اعتماد برای «این درخواست از طرف کدام کاربر تلگرام است»، اعتبارسنجی
   initData با HMAC (تابع validate_init_data) است. هر endpoint حساس باید
   از دکوریتور @require_telegram_auth استفاده کند و از g.tg_user بخواند،
   نه از JSON بدنه‌ی درخواست.
۲) قیمت‌ها همیشه سمت سرور (از روی PLANS / فرمول ساخت سفارشی) محاسبه می‌شوند؛
   هیچ‌وقت قیمتی که کلاینت فرستاده مستقیم مصرف نمی‌شود.
۳) عکس رسید/کیوآرکد/فایل‌های گیمینگ هیچ‌وقت روی دیسک سرور ذخیره نمی‌شوند؛
   یا مستقیم به تلگرام پراکسی می‌شوند (رسید) یا از طریق getFile تلگرام
   استریم می‌شوند (کیوآرکد/فایل‌ها) تا هم توکن ربات لو نرود و هم فضای
   دیسک اشغال نشود.
۴) CORS فقط برای دامنه(های) مشخص‌شده در MINIAPP_ORIGIN باز می‌شود، نه '*'.
۵) روی endpointهای مالی (خرید، شارژ کیف پول) یک محدودیت نرخ ساده‌ی
   حافظه‌ای وجود دارد تا اسپم سفارش/رسید برای ادمین ارسال نشود. این
   محدودیت per-process است؛ اگر با چند worker/گونیکورن دیپلوی می‌کنید،
   برای محیط پروداکشن واقعی به Redis مهاجرت کنید.
=============================================================================
"""

import asyncio
import hashlib
import hmac
import io
import json
import logging
import re
import threading
import time
from functools import wraps
from urllib.parse import parse_qsl

import requests
from flask import Flask, g, jsonify, request, send_file

from sentry_setup import init_sentry
try:
    from sentry_sdk.integrations.flask import FlaskIntegration
    init_sentry(extra_integrations=[FlaskIntegration()])
except ImportError:
    init_sentry()

import crypto
import database as db
import bot_info
from utils import now_tehran_naive
import uniquepay_sync
import fsm_storage
from aiogram import Bot
from handlers.panel_admin import auto_fulfill_vip_via_panel, auto_fulfill_custom_via_panel
from datetime import datetime

from subscription import fetch_subscription_info, extract_configs, days_remaining, is_config_expired
from config import (
    ADMIN_ID,
    PLANS_INTRO_TEXT,
    REFERRAL_LOCK_AMOUNT,
    REFERRAL_MIN_VOLUME_GB,
    TOKEN,
    FREE_TEST_PLAN_KEY,
    UNIQUEPAY_ENABLED,
    ONLINE_PAYMENT_MIN_AMOUNT,
    MAX_WALLET_TOPUP,
)

# اجازه‌ی دامنه‌هایی که مجازند به این API درخواست بزنند (comma-separated در .env)
import os

# 🐛 فیکس: مقایسه‌ی Origin قبلاً دقیق (exact string match) بود، یعنی اگر کسی
# در MINIAPP_ORIGIN آدرس را با یک "/" اضافه در انتها ست می‌کرد (مثلاً چون از
# روی آدرس Menu Button بات‌فادر که خودش یک "/" انتهایی دارد کپی شده)، هیچ‌وقت
# با هدر Origin واقعی مرورگر (که هرگز "/" انتهایی ندارد) match نمی‌شد و CORS
# همیشه رد می‌شد؛ این باعث خطای "Failed to fetch" در فرانت‌اند می‌شد بدون هیچ
# پیام خطای واضحی. حالا "/" انتهایی هنگام مقایسه نادیده گرفته می‌شود.
MINIAPP_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("MINIAPP_ORIGIN", "").split(",") if o.strip()]

plan_type = db.plan_type  # نسخه‌ی DB-aware (دسته‌بندی‌های VIP را هم می‌شناسد)

# 🐛 فیکس: اگر این پروسه هیچ‌وقت fsm_storage.storage را مقداردهی نکند، مسیر
# نادر «سرویس در شاهراه ساخته شد ولی لینک ساب خودکار پیدا نشد» (که در
# auto_fulfill_vip_via_panel/_deliver_panel_link از ادمین می‌خواهد لینک را
# دستی پیست کند) اینجا کار نمی‌کند. چون DBStorage مستقیماً روی همان دیتابیس
# اصلی (نه RAM) ذخیره می‌کند، ساختن یک نمونه‌ی جدا از آن در این پروسه هم امن
# است و با پروسه‌ی اصلی ربات (bot.py) هماهنگ می‌ماند.
if fsm_storage.storage is None:
    fsm_storage.storage = fsm_storage.DBStorage()


def _run_async_with_temp_bot(coro_factory):
    """پُل sync→async برای صدا زدن توابع آسنکرون مخصوص ربات aiogram (مثل
    auto_fulfill_vip_via_panel/auto_fulfill_custom_via_panel در
    handlers/panel_admin.py) از داخل این پروسه‌ی Flask سینک. برای هر
    فراخوانی یک نمونه‌ی موقت Bot ساخته می‌شود و در پایان session‌اش بسته
    می‌شود تا نشتی اتصال ایجاد نشود. coro_factory تابعی است که با گرفتن
    نمونه‌ی Bot، یک coroutine برمی‌گرداند. اگر شاهراه غیرفعال باشد یا نگاشتی
    نباشد (یا هر خطای دیگری رخ دهد)، False برمی‌گردد تا مسیر همیشگی اطلاع
    دستی به ادمین دنبال شود و هیچ سفارشی گم نشود."""
    async def _go():
        tmp_bot = Bot(token=TOKEN)
        try:
            return await coro_factory(tmp_bot)
        finally:
            await tmp_bot.session.close()

    try:
        return asyncio.run(_go())
    except Exception:
        logging.getLogger(__name__).exception("خطا در ارسال خودکار از شاهراه (Mini App)")
        return False

TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"
_LATIN_NAME_RE = re.compile(r"^[A-Za-z0-9]+$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # حداکثر ۱۰ مگابایت آپلود


# =============================================================================
# اعتبارسنجی Telegram WebApp initData (رسمی، طبق مستندات تلگرام)
# =============================================================================
def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> dict | None:
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = parsed.get("auth_date")
    if not auth_date:
        return None
    try:
        if time.time() - int(auth_date) > max_age_seconds:
            return None
    except ValueError:
        return None

    user_json = parsed.get("user")
    if not user_json:
        return None
    try:
        return json.loads(user_json)
    except json.JSONDecodeError:
        return None


# =============================================================================
# بررسی عضویت اجباری در کانال‌ها (معادل sync همان check_membership در
# handlers/start.py)
# 🐛 فیکس: قبلاً Mini App اصلاً این شرط را چک نمی‌کرد و کاربری که هیچ‌وقت وارد
# ربات نشده و در کانال‌های اجباری عضو نشده، می‌توانست مستقیماً از این‌جا خرید
# کند و به همه‌ی امکانات دسترسی داشته باشد — یکی از قوانین کسب‌وکاری اصلی
# پروژه به‌طور کامل bypass می‌شد. حالا همان قانون این‌جا هم اعمال می‌شود.
# =============================================================================
def _check_membership_sync(user_id: int) -> list[dict]:
    not_joined = []
    for ch in bot_info.get_required_channels():
        try:
            resp = requests.get(
                f"{TELEGRAM_API}/getChatMember",
                params={"chat_id": ch["id"], "user_id": user_id},
                timeout=8,
            )
            data = resp.json()
            status = data.get("result", {}).get("status") if data.get("ok") else None
            if status not in ("member", "administrator", "creator"):
                not_joined.append(ch)
        except Exception:
            # مثل نسخه‌ی ربات: در صورت خطا (کوتاهی شبکه، ربات ادمین کانال
            # نیست و...) fail-closed عمل می‌کنیم، یعنی «عضو نشده» فرض می‌کنیم.
            not_joined.append(ch)
    return not_joined


def _serialize_channels(channels: list[dict]) -> list[dict]:
    return [{"name": c.get("name"), "url": c.get("url")} for c in channels]


def require_telegram_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        init_data = auth_header[4:] if auth_header.startswith("tma ") else request.headers.get("X-Telegram-Init-Data", "")

        tg_user = validate_init_data(init_data, TOKEN)
        if tg_user is None or "id" not in tg_user:
            return jsonify({"error": "invalid_auth"}), 401

        g.tg_user = tg_user

        if bot_info.get_required_channels():
            not_joined = _check_membership_sync(tg_user["id"])
            if not_joined:
                return jsonify({
                    "error": "channel_join_required",
                    "channels": _serialize_channels(not_joined),
                }), 403

        g.user = db.create_user(tg_user["id"], tg_user.get("first_name") or tg_user.get("username") or "کاربر")
        if g.user.get("is_blocked"):
            return jsonify({"error": "user_blocked"}), 403
        return f(*args, **kwargs)

    return wrapper


@app.route("/api/membership/status", methods=["GET"])
def api_membership_status():
    """endpoint سبک (بدون نیاز به ساخت کاربر) که فرانت‌اند مینی‌اپ قبل از هر
    درخواست دیگری صدا می‌زند تا اگر کاربر در کانال‌های اجباری عضو نبود، صفحه‌ی
    «عضویت در کانال» را نشان دهد، نه این‌که با یک خطای عمومی ۴۰۳ مواجه شود."""
    auth_header = request.headers.get("Authorization", "")
    init_data = auth_header[4:] if auth_header.startswith("tma ") else request.headers.get("X-Telegram-Init-Data", "")
    tg_user = validate_init_data(init_data, TOKEN)
    if tg_user is None or "id" not in tg_user:
        return jsonify({"error": "invalid_auth"}), 401

    not_joined = _check_membership_sync(tg_user["id"]) if bot_info.get_required_channels() else []
    return jsonify({"joined": not not_joined, "channels": _serialize_channels(not_joined)})


# =============================================================================
# 🐛 فیکس: محدودیت نرخ قبلاً فقط در حافظه‌ی همین پروسه ذخیره می‌شد (با ری‌استارت
# صفر می‌شد و بین Workerها/پروسه‌های متعدد مشترک نیست). حالا حذف شده و به
# db.consume_api_rate_limit جایگزین شده که دیتابیسی و مشترک بین تمام پروسه/Workerهاست.
# =============================================================================


def rate_limit(max_calls: int, period_seconds: int):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            key = f"{f.__name__}:{g.tg_user['id']}"
            if not db.consume_api_rate_limit(key, max_calls, period_seconds):
                return jsonify({"error": "rate_limited"}), 429
            return f(*args, **kwargs)

        return wrapper

    return decorator


# =============================================================================
# CORS (فقط برای دامنه‌های مجاز، نه '*')
# 🐛 فیکس: نسخه‌ی قبلی اگر متغیر محیطی MINIAPP_ORIGIN تنظیم نمی‌شد (که هیچ
# خطای startup هم نمی‌داد)، به‌طور خاموش هر Origin ای را مجاز می‌کرد
# («or not MINIAPP_ORIGINS» یعنی وقتی لیست خالی است، شرط همیشه True می‌شود)؛
# دقیقاً برخلاف چیزی که در کامنت بالای فایل ادعا شده («نه '*'»). حالا اگر
# تنظیم نشده باشد، پیش‌فرض امن «رد کردن» است، نه «قبول همه».
# =============================================================================
if not MINIAPP_ORIGINS:
    logging.getLogger(__name__).warning(
        "MINIAPP_ORIGIN تنظیم نشده است؛ درخواست‌های cross-origin به این API رد "
        "خواهند شد (CORS بسته است). آدرس دامنه‌ی Mini App خودتان را در .env با "
        "MINIAPP_ORIGIN=https://your-miniapp-domain ست کنید."
    )


@app.after_request
def add_cors_headers(resp):
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") in MINIAPP_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, X-Telegram-Init-Data, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def cors_preflight(_any):
    return "", 204


# =============================================================================
# کمک‌کننده‌های تماس مستقیم با Telegram Bot API (بدون وابستگی به aiogram،
# چون این فایل sync است و جدا از حلقه‌ی asyncio ربات اجرا می‌شود)
# =============================================================================
def tg_send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        response = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or "Telegram sendMessage failed")
        return True
    except requests.RequestException as e:
        logging.getLogger(__name__).warning("tg_send_message failed: %s", e)
    except (ValueError, RuntimeError) as e:
        logging.getLogger(__name__).warning("tg_send_message rejected: %s", e)
    return False


def tg_send_photo_bytes(chat_id, file_bytes, filename, caption=None, reply_markup=None):
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    files = {"photo": (filename, file_bytes)}
    try:
        response = requests.post(f"{TELEGRAM_API}/sendPhoto", data=data, files=files, timeout=30)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("description") or "Telegram sendPhoto failed")
        return True
    except requests.RequestException as e:
        logging.getLogger(__name__).warning("tg_send_photo_bytes failed: %s", e)
    except (ValueError, RuntimeError) as e:
        logging.getLogger(__name__).warning("tg_send_photo_bytes rejected: %s", e)
    return False


def _valid_receipt_photo(photo) -> bool:
    if photo is None:
        return False
    mime = (photo.mimetype or "").lower()
    return mime in {"image/jpeg", "image/png", "image/webp"}


def tg_get_file_bytes(file_id: str):
    """فایل را از تلگرام می‌گیرد و بایت خامش را برمی‌گرداند (بدون افشای توکن به کلاینت)."""
    try:
        r = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=15)
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        r2 = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{file_path}", timeout=30)
        r2.raise_for_status()
        return r2.content
    except (requests.RequestException, KeyError):
        return None


def inline_kb(*rows):
    """rows: هر سطر لیستی از (text, callback_data)."""
    return {"inline_keyboard": [[{"text": t, "callback_data": c} for t, c in row] for row in rows]}


def _calc_custom_price(volume_gb: int, days: int, telegram_id=None) -> tuple[int, bool]:
    """مثل نسخه‌ی ربات کلاسیک: اگر کاربر نماینده باشد، تخفیف نمایندگی‌اش
    روی قیمت «بساز سرویس خودت» هم اعمال می‌شود."""
    _cb = db.get_effective_custom_build_settings()
    price = volume_gb * _cb["price_per_gb"] + (days / 30) * _cb["price_per_30_days"]
    discount_applied = False
    if telegram_id is not None:
        agent = db.get_agent(telegram_id)
        if agent:
            price = price * (1 - agent["vip_discount_percent"] / 100)
            discount_applied = True
    return int(round(price)), discount_applied


def _serialize_plan(key, plan):
    return {
        "key": key,
        "name": plan["name"],
        "price": plan["price"],
        "days": plan.get("days", 0),
        "volume_gb": plan.get("volume_gb", 0),
        "type": plan_type(key),
    }


# =============================================================================
# پروفایل / کاربر
# =============================================================================
@app.route("/api/me", methods=["GET"])
@require_telegram_auth
def api_me():
    u = g.user
    configs_count = len(db.get_configs(u["id"]))
    agent = db.get_agent(g.tg_user["id"])
    return jsonify({
        "name": u["name"],
        "telegram_id": u["telegram_id"],
        "wallet": u["wallet"],
        "locked_wallet": u["locked_wallet"],
        "total_purchase": u["total_purchase"],
        "joined": u["joined"],
        "invited_count": u["invited_count"],
        "successful_invites": u["successful_invites"],
        "services_count": configs_count,
        "orders_enabled": db.is_orders_enabled(),
        "agent_discount_percent": agent["vip_discount_percent"] if agent else 0,
    })


# =============================================================================
# پلن‌ها — VIP حالا دسته‌بندی‌شده (همان دسته‌بندی‌های ساخته‌شده در پنل ادمین)،
# Gaming مثل قبل یک لیست ساده است.
# =============================================================================
@app.route("/api/plans", methods=["GET"])
@require_telegram_auth
def api_plans():
    agent = db.get_agent(g.tg_user["id"])
    vip_categories = []
    for cat in db.get_vip_categories():
        plans = db.get_vip_plans(cat["id"])
        vip_categories.append({
            "key": cat["key"],
            "name": cat["name"],
            "plans": [_serialize_plan(p["plan_key"], p) for p in plans],
        })

    gaming_categories = []
    for cat in db.get_gaming_categories():
        plans = db.get_gaming_plans(cat["id"])
        gaming_categories.append({
            "key": cat["key"],
            "name": cat["name"],
            "plans": [_serialize_plan(p["plan_key"], p) for p in plans],
        })

    return jsonify({
        "vip_categories": vip_categories,
        "gaming_categories": gaming_categories,
        "orders_enabled": db.is_orders_enabled(),
        "agent_discount_percent": agent["vip_discount_percent"] if agent else 0,
        "online_payment_enabled": UNIQUEPAY_ENABLED,
        "intro_text": PLANS_INTRO_TEXT,
    })


# =============================================================================
# 🎁 تست رایگان — دقیقاً همان چیزی که در ربات با دکمه‌ی «🎁 تست رایگان» دیده
# می‌شود؛ یک پلن کوچک با قیمت نمادین، با همان مسیر خرید عادی (کیف پول/کارت).
# =============================================================================
@app.route("/api/free-trial", methods=["GET"])
@require_telegram_auth
def api_free_trial():
    plan = db.get_effective_plan(FREE_TEST_PLAN_KEY)
    if plan is None:
        return jsonify({"error": "plan_not_found"}), 404
    return jsonify({
        "plan": _serialize_plan(FREE_TEST_PLAN_KEY, plan),
        "wallet": g.user["wallet"],
        "orders_enabled": db.is_orders_enabled(),
    })


# =============================================================================
# محاسبه‌ی قیمت نهایی: بهترین حالت بین کد تخفیف کاربر و تخفیف خودکار نمایندگی
# (فقط روی VIP) — دقیقاً پورت‌شده از handlers/plans.py تا هیچ مغایرتی با ربات
# کلاسیک نداشته باشد.
# =============================================================================
def _compute_final_price(plan_key: str, plan: dict, tg_user_id, user_db_id: int, discount_code: str):
    price = plan["price"]

    code_price = price
    valid_code = None
    if discount_code:
        discount = db.get_discount(discount_code)
        if (
            discount
            and discount["uses"] > 0
            and not db.discount_is_expired(discount)
            and db.discount_applies_to_plan(discount, plan_key)
            and db.discount_allowed_for_user(discount, tg_user_id)
        ):
            over_cap = (
                discount.get("max_uses_per_user")
                and db.user_discount_uses(discount["id"], user_db_id) >= discount["max_uses_per_user"]
            )
            under_min = discount.get("min_order_amount") and price < discount["min_order_amount"]
            if not over_cap and not under_min:
                code_price = db.compute_discount(discount, price)
                valid_code = discount

    agent_price = price
    if plan_type(plan_key) == "vip":
        agent = db.get_agent(tg_user_id)
        if agent:
            agent_price = int(round(price * (1 - agent["vip_discount_percent"] / 100)))

    final_price = min(code_price, agent_price)
    winning_code = discount_code if (valid_code is not None and code_price <= agent_price) else None
    return final_price, winning_code


# =============================================================================
# کد تخفیف
# =============================================================================
@app.route("/api/discount/validate", methods=["POST"])
@require_telegram_auth
def api_discount_validate():
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").strip()
    plan_key = body.get("plan_key")
    if not code:
        return jsonify({"error": "code_required"}), 400

    discount = db.get_discount(code)
    if discount is None or discount["uses"] <= 0 or db.discount_is_expired(discount):
        return jsonify({"valid": False}), 200

    if plan_key and not db.discount_applies_to_plan(discount, plan_key):
        return jsonify({"valid": False, "reason": "wrong_plan"}), 200

    if not db.discount_allowed_for_user(discount, g.tg_user["id"]):
        return jsonify({"valid": False, "reason": "not_allowed"}), 200

    if discount.get("max_uses_per_user") and db.user_discount_uses(discount["id"], g.user["id"]) >= discount["max_uses_per_user"]:
        return jsonify({"valid": False, "reason": "user_limit_reached"}), 200

    if plan_key and discount.get("min_order_amount"):
        plan = db.get_effective_plan(plan_key)
        if plan and plan["price"] < discount["min_order_amount"]:
            return jsonify({"valid": False, "reason": "min_order_amount", "min_order_amount": discount["min_order_amount"]}), 200

    return jsonify({
        "valid": True,
        "percent": discount["percent"],
        "discount_type": discount.get("discount_type", "percent"),
        "amount": discount.get("amount", 0),
        "min_order_amount": discount.get("min_order_amount", 0),
        "expires_at": discount.get("expires_at"),
    })


# =============================================================================
# خرید پلن ثابت (کیف پول)
# =============================================================================
@app.route("/api/orders/wallet", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=8, period_seconds=60)
def api_order_wallet():
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403

    body = request.get_json(silent=True) or {}
    plan_key = body.get("plan_key")
    discount_code = (body.get("discount_code") or "").strip()

    plan = db.get_effective_plan(plan_key)
    if plan is None:
        return jsonify({"error": "plan_not_found"}), 404

    user = g.user

    # 🐛 فیکس: مینی‌اپ قبلاً محدودیت «هر کاربر فقط یک‌بار تست رایگان» را چک
    # نمی‌کرد (برخلاف ربات کلاسیک در handlers/plans.py)، پس کاربر می‌توانست از
    # مینی‌اپ به‌تعداد نامحدود «تست رایگان» بگیرد.
    if plan_key == FREE_TEST_PLAN_KEY and db.has_used_free_test(user["id"]):
        return jsonify({"error": "free_test_already_used"}), 409

    final_price, winning_code = _compute_final_price(plan_key, plan, g.tg_user["id"], user["id"], discount_code)

    if user["wallet"] < final_price:
        return jsonify({
            "error": "insufficient_balance",
            "price": final_price,
            "wallet": user["wallet"],
            "needed": final_price - user["wallet"],
        }), 402

    if not db.deduct_from_wallet(user["id"], final_price, f"خرید {plan['name']}"):
        return jsonify({"error": "insufficient_balance"}), 402

    if winning_code:
        db.use_discount(winning_code, user["id"])

    if plan.get("volume_gb", 0) >= REFERRAL_MIN_VOLUME_GB:
        try:
            db.complete_referral(user["id"])
        except ValueError:
            pass

    order_id = db.create_order(user["id"], plan_key, plan["name"], plan_type(plan_key), final_price)

    # 🐛 فیکس: خرید کیف‌پولی از مینی‌اپ همیشه فقط به ادمین اطلاع می‌داد و منتظر
    # ارسال دستی می‌ماند؛ برخلاف ربات کلاسیک (handlers/plans.py::pay_with_wallet)
    # که اگر شاهراه فعال و برای این پلن نگاشت شده باشد، سرویس بلافاصله و
    # کاملاً خودکار از پنل شاهراه ساخته و ارسال می‌شود. همان منطق اینجا هم با
    # auto_fulfill_vip_via_panel اجرا می‌شود؛ اگر نگاشتی نباشد یا ناموفق
    # باشد، دقیقاً مثل قبل به ادمین برای ارسال دستی اطلاع داده می‌شود.
    handled = False
    if plan_type(plan_key) in ("vip", "test"):
        handled = _run_async_with_temp_bot(
            lambda bot: auto_fulfill_vip_via_panel(bot, str(user["telegram_id"]), plan_key, order_id)
        )

    if handled:
        tg_send_message(
            ADMIN_ID,
            f"🛒 خرید جدید (کیف پول - Mini App) — به‌صورت خودکار از پنل شاهراه ساخته و ارسال شد ✅\n\n"
            f"👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n"
            f"📦 {plan['name']}\n💰 {final_price:,} تومان",
        )
    else:
        send_btn = ("🎮 ارسال کانفیگ گیمینگ", f"sendgaming_{user['telegram_id']}|{order_id}") \
            if plan_type(plan_key) == "gaming" else \
            ("🚀 ارسال کانفیگ VIP (QR)", f"sendvip_{user['telegram_id']}|{order_id}")

        tg_send_message(
            ADMIN_ID,
            f"🛒 خرید جدید (کیف پول - Mini App)!\n\n"
            f"👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n"
            f"📦 {plan['name']}\n💰 {final_price:,} تومان",
            reply_markup=inline_kb([send_btn]),
        )
    return jsonify({"success": True, "order_id": order_id, "price": final_price})


# =============================================================================
# خرید پلن ثابت (کارت‌به‌کارت) — مرحله‌ی اول: اطلاعات کارت
# =============================================================================
@app.route("/api/orders/card/info", methods=["POST"])
@require_telegram_auth
def api_order_card_info():
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403

    body = request.get_json(silent=True) or {}
    plan_key = body.get("plan_key")
    discount_code = (body.get("discount_code") or "").strip()
    plan = db.get_effective_plan(plan_key)
    if plan is None:
        return jsonify({"error": "plan_not_found"}), 404

    # 🐛 فیکس: مثل کیف‌پول/پرداخت آنلاین، محدودیت «هر کاربر فقط یک‌بار تست
    # رایگان» باید همینجا هم چک شود، وگرنه کاربر می‌توانست با کارت‌به‌کارت هم
    # به‌تعداد نامحدود تست رایگان بگیرد.
    if plan_key == FREE_TEST_PLAN_KEY and db.has_used_free_test(g.user["id"]):
        return jsonify({"error": "free_test_already_used"}), 409

    final_price, _winning_code = _compute_final_price(plan_key, plan, g.tg_user["id"], g.user["id"], discount_code)

    invoice = db.create_invoice(
        user_id=g.user["id"], telegram_id=str(g.user["telegram_id"]),
        kind="plan_card", label=plan["name"], price=final_price,
    )
    return jsonify({
        "card_number": bot_info.get("card_number"), "card_holder": bot_info.get("card_holder"), "price": final_price,
        "invoice_id": invoice["id"], "expires_at": invoice["expires_at"],
    })


# =============================================================================
# پرداخت آنلاین (درگاه یونیک‌پی — کارت‌به‌کارت با تایید خودکار)
# =============================================================================
def _finalize_online_payment(payment: dict) -> int | None:
    """معادل نسخه‌ی ربات (handlers/plans.py::finalize_online_payment).
    🐛 فیکس ریس‌کاندیشن: این تابع در پروسه‌ی جدای webapp_api.py اجرا می‌شود
    در حالی که پولر پس‌زمینه‌ی ربات (bot.py) هم می‌تواند هم‌زمان همین پرداخت
    را finalize کند؛ چون این دو یک پروسه‌ی مشترک و قفل حافظه‌ای مشترک ندارند،
    یک if ساده کافی نبود. حالا با db.claim_online_payment_for_finalize یک
    claim اتمیک روی ردیف دیتابیس گرفته می‌شود تا از بین این دو مسیر، فقط
    یکی سفارش بسازد."""
    if payment["status"] == "paid" and payment.get("order_id"):
        return payment["order_id"]

    if not db.claim_online_payment_for_finalize(payment["id"]):
        fresh = db.get_online_payment(payment["id"])
        if fresh and fresh["status"] == "paid" and fresh.get("order_id"):
            return fresh["order_id"]
        return None

    try:
        order_id = db.create_order(
            payment["user_id"], payment["plan_key"], payment["plan_name"],
            payment["order_type"], payment["price"],
        )
        db.mark_online_payment_paid(payment["id"], order_id)

        if payment.get("discount_code"):
            try:
                db.use_discount(payment["discount_code"], payment["user_id"])
            except Exception:
                logging.getLogger(__name__).exception("خطا در مصرف کد تخفیف پس از پرداخت آنلاین (Mini App)")

        plan = db.get_effective_plan(payment["plan_key"]) if payment["plan_key"] else None
        if plan and plan.get("volume_gb", 0) >= REFERRAL_MIN_VOLUME_GB:
            try:
                db.complete_referral(payment["user_id"])
            except ValueError:
                pass
    except Exception:
        db.set_online_payment_status(payment["id"], "pending")
        raise

    # 🐛 فیکس: مثل کیف‌پول، پرداخت آنلاین (یونیک‌پی) از مینی‌اپ هم قرار بود طبق
    # منطق ربات کلاسیک (handlers/plans.py::finalize_online_payment)، اگر
    # شاهراه فعال و برای این پلن نگاشت شده باشد، سرویس را بلافاصله و کاملاً
    # خودکار از پنل شاهراه بسازد و ارسال کند؛ ولی اینجا این مسیر اصلاً صدا زده
    # نمی‌شد و همیشه فقط ادمین مطلع می‌شد.
    handled = False
    if payment["plan_key"] and plan_type(payment["plan_key"]) in ("vip", "test"):
        handled = _run_async_with_temp_bot(
            lambda bot: auto_fulfill_vip_via_panel(
                bot, payment["telegram_id"], payment["plan_key"], order_id
            )
        )

    if handled:
        tg_send_message(
            ADMIN_ID,
            f"🛒 خرید جدید (پرداخت آنلاین - Mini App) — به‌صورت خودکار از پنل شاهراه ساخته و ارسال شد ✅\n\n"
            f"🆔 {payment['telegram_id']}\n"
            f"📦 {payment['plan_name']}\n💰 {payment['price']:,} تومان",
        )
    else:
        is_gaming = payment["order_type"] == "gaming"
        send_btn = ("🎮 ارسال کانفیگ گیمینگ", f"sendgaming_{payment['telegram_id']}|{order_id}") \
            if is_gaming else \
            ("🚀 ارسال کانفیگ VIP (QR)", f"sendvip_{payment['telegram_id']}|{order_id}")

        tg_send_message(
            ADMIN_ID,
            f"🛒 خرید جدید (پرداخت آنلاین - Mini App)!\n\n"
            f"🆔 {payment['telegram_id']}\n"
            f"📦 {payment['plan_name']}\n💰 {payment['price']:,} تومان",
            reply_markup=inline_kb([send_btn]),
        )
    return order_id


@app.route("/api/orders/online/create", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=8, period_seconds=60)
def api_order_online_create():
    if not UNIQUEPAY_ENABLED:
        return jsonify({"error": "gateway_disabled"}), 400
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403

    body = request.get_json(silent=True) or {}
    plan_key = body.get("plan_key")
    discount_code = (body.get("discount_code") or "").strip()

    plan = db.get_effective_plan(plan_key)
    if plan is None:
        return jsonify({"error": "plan_not_found"}), 404

    user = g.user

    # 🐛 فیکس: مثل کیف‌پول/کارت‌به‌کارت، محدودیت «هر کاربر فقط یک‌بار تست
    # رایگان» باید همینجا هم چک شود، وگرنه کاربر می‌توانست از درگاه پرداخت
    # آنلاین هم به‌تعداد نامحدود تست رایگان بگیرد.
    if plan_key == FREE_TEST_PLAN_KEY and db.has_used_free_test(user["id"]):
        return jsonify({"error": "free_test_already_used"}), 409

    final_price, winning_code = _compute_final_price(plan_key, plan, g.tg_user["id"], user["id"], discount_code)

    # 🐛 فیکس: تا اینجا برای مبالف کم (≤ ONLINE_PAYMENT_MIN_AMOUNT) هیچ بررسی انجام نمی‌شد و درخواست مستقیماً به درگاه می‌رفت و تا خود درگاه با خطای ‌عمومی gateway_error رد می‌شد (مثل کیف‌پول)؛ باید همین جا بررسی شود تا فراند پیام دقیق amount_too_low را ببیند، نه یک خطای گنگ درگاه.
    if final_price and final_price <= ONLINE_PAYMENT_MIN_AMOUNT:
        return jsonify({"error": "amount_too_low", "min_amount": ONLINE_PAYMENT_MIN_AMOUNT}), 400

    hash_id = uniquepay_sync.new_hash_id("miniapp-plan")
    invoice = uniquepay_sync.create_invoice(hash_id, final_price)
    payment_link = invoice.get("paymentLink") if invoice else None
    if not payment_link:
        return jsonify({"error": "gateway_error"}), 502

    payment_id = db.create_online_payment(
        user_id=user["id"],
        telegram_id=str(user["telegram_id"]),
        hash_id=hash_id,
        plan_name=plan["name"],
        price=final_price,
        order_type=plan_type(plan_key),
        plan_key=plan_key,
        discount_code=winning_code,
        payment_link=payment_link,
        ref_id=str(invoice.get("refId")),
    )
    return jsonify({
        "payment_id": payment_id, "payment_link": payment_link, "price": final_price,
        "expires_at": db.online_payment_expires_at(db.get_online_payment(payment_id)["created_at"]),
        "expiry_minutes": db.INVOICE_EXPIRY_MINUTES,
    })


def _finalize_wallet_charge_online_payment(payment: dict) -> int | None:
    """معادل نسخه‌ی ربات (handlers/wallet.py::finalize_wallet_charge_online_payment).
    به‌جای ساخت سفارش، مستقیماً مبلف به کیف پول کاربر اضافه می‌شود. از همان
    claim اتمیک استفاده می‌شود تا با پولر پس‌زمینه‌ی ربات (bot.py) که هم‌زمان
    همین اینویس‌ها را چک می‌کند، تداخل/شارژ تکراری پیش نیاید."""
    if payment["status"] == "paid":
        return payment["id"]

    if not db.claim_online_payment_for_finalize(payment["id"]):
        fresh = db.get_online_payment(payment["id"])
        if fresh and fresh["status"] == "paid":
            return fresh["id"]
        return None

    try:
        db.add_to_wallet(payment["user_id"], payment["price"], "شارژ کیف پول (پرداخت آنلاین)")
        db.mark_online_payment_paid(payment["id"], None)
    except Exception:
        db.set_online_payment_status(payment["id"], "pending")
        raise

    tg_send_message(
        ADMIN_ID,
        f"💳 شارژ کیف پول (پرداخت آنلاین - Mini App)!\n\n"
        f"🆔 {payment['telegram_id']}\n💰 {payment['price']:,} تومان",
    )
    return payment["id"]


@app.route("/api/wallet/online/create", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=8, period_seconds=60)
def api_wallet_online_create():
    if not UNIQUEPAY_ENABLED:
        return jsonify({"error": "gateway_disabled"}), 400

    body = request.get_json(silent=True) or {}
    amount = body.get("amount")
    if not isinstance(amount, int) or amount <= 0:
        return jsonify({"error": "invalid_amount"}), 400
    if amount <= ONLINE_PAYMENT_MIN_AMOUNT:
        return jsonify({"error": "amount_too_low", "min_amount": ONLINE_PAYMENT_MIN_AMOUNT}), 400
    # 🐛 فیکس: قبلاً هیچ سقفی برای مبلف شارژ اونلاین وجود ندارشت.
    if amount > MAX_WALLET_TOPUP:
        return jsonify({"error": "amount_too_high", "max_amount": MAX_WALLET_TOPUP}), 400

    user = g.user
    hash_id = uniquepay_sync.new_hash_id("miniapp-charge")
    invoice = uniquepay_sync.create_invoice(hash_id, amount)
    payment_link = invoice.get("paymentLink") if invoice else None
    if not payment_link:
        return jsonify({"error": "gateway_error"}), 502

    payment_id = db.create_online_payment(
        user_id=user["id"],
        telegram_id=str(user["telegram_id"]),
        hash_id=hash_id,
        plan_name="شارژ کیف پول",
        price=amount,
        order_type="wallet_charge",
        kind="wallet_charge",
        payment_link=payment_link,
        ref_id=str(invoice.get("refId")),
    )
    return jsonify({
        "payment_id": payment_id, "payment_link": payment_link, "price": amount,
        "expires_at": db.online_payment_expires_at(db.get_online_payment(payment_id)["created_at"]),
        "expiry_minutes": db.INVOICE_EXPIRY_MINUTES,
    })


@app.route("/api/orders/online/status", methods=["GET"])
@require_telegram_auth
def api_order_online_status():
    payment_id = request.args.get("id", type=int)
    if not payment_id:
        return jsonify({"error": "invalid_request"}), 400

    payment = db.get_online_payment(payment_id)
    if payment is None:
        return jsonify({"error": "invoice_expired"}), 404
    if str(g.tg_user["id"]) != payment["telegram_id"]:
        return jsonify({"error": "forbidden"}), 403

    if payment["status"] == "paid":
        return jsonify({"status": "paid", "order_id": payment.get("order_id")})

    invoice = uniquepay_sync.check_invoice(payment["hash_id"])
    if invoice and invoice.get("isPaid"):
        if payment.get("kind") == "wallet_charge":
            result_id = _finalize_wallet_charge_online_payment(payment)
        else:
            result_id = _finalize_online_payment(payment)
        if result_id is None:
            # یک فراخوانی هم‌زمان دیگر (مثلاً پولر ربات) همین الان در حال
            # پردازش همین پرداخت است؛ کلاینت چند لحظه‌ی دیگر دوباره همین
            # endpoint را صدا می‌زند و آن‌موقع status=paid برمی‌گردد.
            return jsonify({"status": "pending"})
        return jsonify({"status": "paid", "order_id": result_id})

    return jsonify({"status": "pending"})


# =============================================================================
# خرید پلن ثابت (کارت‌به‌کارت) — مرحله‌ی دوم: آپلود عکس رسید (multipart)
# =============================================================================
@app.route("/api/orders/card/receipt", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=5, period_seconds=60)
def api_order_card_receipt():
    # 🐛 فیکس: برخلاف مرحلهٔ اطلاعات کارت، این endpoint قبلاً orders_enabled و محدودیت
    # تست رایگان را دوباره بررسی نمی‌کرد و می‌شد با زدن مستقیم این endpoint رسید/Pending
    # برای ادمین ساخت حتی وقتی سفارش‌ها بسته است یا این کاربر قبلاً تست رایگان گرفته.
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403

    plan_key = request.form.get("plan_key")
    discount_code = (request.form.get("discount_code") or "").strip()
    photo = request.files.get("receipt")
    invoice_id = request.form.get("invoice_id", type=int)

    plan = db.get_effective_plan(plan_key)
    if plan is None or photo is None:
        return jsonify({"error": "invalid_request"}), 400
    if not _valid_receipt_photo(photo):
        return jsonify({"error": "invalid_file_type"}), 400

    if plan_key == FREE_TEST_PLAN_KEY and db.has_used_free_test(g.user["id"]):
        return jsonify({"error": "free_test_already_used"}), 409

    if not invoice_id or db.consume_invoice(invoice_id) is None:
        return jsonify({"error": "invoice_expired"}), 409

    user = g.user
    final_price, winning_code = _compute_final_price(plan_key, plan, g.tg_user["id"], user["id"], discount_code)

    caption = (
        f"💳 رسید خرید کارت‌به‌کارت (Mini App)\n\n"
        f"👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n"
        f"📦 {plan['name']}\n💰 {final_price:,} تومان"
    )
    approve_kb = inline_kb([
        (f"✅ تأیید پرداخت ({final_price:,} ت)", f"approvepay|{user['telegram_id']}|{plan_key}|{final_price}"),
        ("❌ رد رسید", f"rejectpay|{user['telegram_id']}"),
    ])
    try:
        _receipt_id = db.create_pending_receipt(
            "plan_card", user["telegram_id"], user["id"], plan["name"], final_price,
            extra=plan_key, plan_key=plan_key, discount_code=winning_code,
        )
    except Exception:
        _receipt_id = None
        logging.getLogger(__name__).exception("خطا در ثبت رسید معلق خرید کارت به کارت (Mini App)")
    sent = tg_send_photo_bytes(ADMIN_ID, photo.read(), photo.filename or "receipt.jpg", caption=caption, reply_markup=approve_kb)
    if not sent:
        if _receipt_id is not None:
            try:
                db.delete_pending_receipt(_receipt_id)
            except Exception:
                logging.getLogger(__name__).exception("خطا در تمیزکاری رسید معلق پس از شکست ارسال")
        return jsonify({
            "error": "telegram_delivery_failed",
            "error_message": "ارسال رسید به ادمین موقتاً با خطا مواجه شد؛ لطفاً چند لحظه دیگر دوباره تلاش کنید.",
        }), 502

    # 🐛 فیکس: کد تخفیف کارت‌به‌کارت حالا با discount_code=winning_code همراه
    # رسید در دیتابیس ذخیره می‌شود و فقط هنگام تأیید ادمین (نه اینجا، نه هنگام
    # رد) مصرف خواهد شد؛ قبلاً این مقدار اصلاً ذخیره نمی‌شد.
    if invoice_id:
        db.delete_invoice(invoice_id)

    return jsonify({"success": True})


# =============================================================================
# کیف پول
# =============================================================================
_TX_POSITIVE_TYPES = ("charge", "referral_release")


def _signed_tx_amount(tx: dict) -> int:
    """در دیتابیس amount همیشه مثبت (مقدار مطلق) ذخیره می‌شود؛ نوع تراکنش
    تعیین می‌کند که واقعاً واریز بوده (شارژ/آزادسازی پاداش → مثبت/سبز) یا
    خروج از حساب بوده (خرید، برداشت، پاداش در انتظار → منفی/قرمز)."""
    amount = abs(tx["amount"])
    return amount if tx["type"] in _TX_POSITIVE_TYPES else -amount


@app.route("/api/wallet", methods=["GET"])
@require_telegram_auth
def api_wallet():
    u = g.user
    txs = db.get_transactions(u["id"], limit=20)
    return jsonify({
        "wallet": u["wallet"],
        "locked_wallet": u["locked_wallet"],
        "card_number": bot_info.get("card_number"),
        "card_holder": bot_info.get("card_holder"),
        "min_purchase_gb": REFERRAL_MIN_VOLUME_GB,
        "online_payment_enabled": UNIQUEPAY_ENABLED,
        "online_payment_min_amount": ONLINE_PAYMENT_MIN_AMOUNT,
        "transactions": [
            {"type": tx["type"], "amount": _signed_tx_amount(tx), "description": tx["description"], "created_at": tx["created_at"]}
            for tx in txs
        ],
    })


@app.route("/api/wallet/card/create", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=8, period_seconds=60)
def api_wallet_card_create():
    body = request.get_json(silent=True) or {}
    amount = body.get("amount")
    if not isinstance(amount, int) or amount <= 0:
        return jsonify({"error": "invalid_amount"}), 400
    if amount > MAX_WALLET_TOPUP:
        return jsonify({"error": "amount_too_high", "max_amount": MAX_WALLET_TOPUP}), 400

    user = g.user
    invoice = db.create_invoice(
        user_id=user["id"],
        telegram_id=str(user["telegram_id"]),
        kind="wallet_card_miniapp",
        label="شارج کیف پول (Mini App)",
        price=amount,
    )
    return jsonify({
        "invoice_id": invoice["id"], "expires_at": invoice["expires_at"],
        "expiry_minutes": db.INVOICE_EXPIRY_MINUTES,
        "card_number": bot_info.get("card_number"), "card_holder": bot_info.get("card_holder"), "price": amount,
    })


@app.route("/api/wallet/topup", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=5, period_seconds=60)
def api_wallet_topup():
    amount_raw = request.form.get("amount")
    photo = request.files.get("receipt")
    invoice_id = request.form.get("invoice_id", type=int)

    if not amount_raw or not str(amount_raw).isdigit() or photo is None:
        return jsonify({"error": "invalid_request"}), 400
    if not _valid_receipt_photo(photo):
        return jsonify({"error": "invalid_file_type"}), 400
    amount = int(amount_raw)
    if amount <= 0:
        return jsonify({"error": "invalid_amount"}), 400
    # 🐛 فیکس: مبلف شارژ کارت‌به‌کارت را هم محدود کنیم.
    if amount > MAX_WALLET_TOPUP:
        return jsonify({"error": "amount_too_high", "max_amount": MAX_WALLET_TOPUP}), 400

    if not invoice_id or db.consume_invoice(invoice_id) is None:
        return jsonify({"error": "invoice_expired"}), 410

    user = g.user
    caption = f"📩 رسید شارژ (Mini App)\n👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n💰 {amount:,} تومان"
    approve_kb = inline_kb([
        (f"✅ تأیید {amount:,}", f"approve_{user['telegram_id']}_{amount}"),
        ("💵 مبلغ دلخواه", f"custom_{user['telegram_id']}"),
        ("❌ رد", f"reject_{user['telegram_id']}"),
    ])
    try:
        _receipt_id = db.create_pending_receipt("charge", user["telegram_id"], user["id"], "شارژ کیف پول (Mini App)", amount)
    except Exception:
        _receipt_id = None
        logging.getLogger(__name__).exception("خطا در ثبت رسید معلق شارژ کیف پول (Mini App)")
    sent = tg_send_photo_bytes(ADMIN_ID, photo.read(), photo.filename or "receipt.jpg", caption=caption, reply_markup=approve_kb)
    if not sent:
        if _receipt_id is not None:
            try:
                db.delete_pending_receipt(_receipt_id)
            except Exception:
                logging.getLogger(__name__).exception("خطا در تمیزکاری رسید معلق پس از شکست ارسال")
        return jsonify({
            "error": "telegram_delivery_failed",
            "error_message": "ارسال رسید به ادمین موقتاً با خطا مواجه شد؛ لطفاً چند لحظه دیگر دوباره تلاش کنید.",
        }), 502

    db.delete_invoice(invoice_id)
    return jsonify({"success": True})


# =============================================================================
# سرویس‌های من (VIP + گیمینگ)
# =============================================================================
@app.route("/api/services/<int:config_id>/configs", methods=["GET"])
@require_telegram_auth
def api_service_configs(config_id):
    """کانفیگ‌های تکی (vmess/vless/trojan/...) را از داخل لینک ساب استخراج
    می‌کند - همان قابلیتی که دکمه‌ی «دریافت کانفیگ‌های تکی» در ربات دارد."""
    cfg = _own_config_or_none(config_id, g.user["id"])
    if cfg is None:
        return jsonify({"error": "not_found"}), 404

    try:
        sub_link = crypto.decrypt_config(cfg["config"])
    except Exception:
        return jsonify({"error": "decrypt_failed"}), 500

    if not sub_link or not sub_link.lower().startswith(("http://", "https://")):
        return jsonify({"error": "no_sub_link"}), 400

    try:
        configs = asyncio.run(extract_configs(sub_link))
    except Exception:
        configs = None

    if configs is None:
        return jsonify({"error": "unreachable"}), 502

    return jsonify({"configs": configs})


def _config_remaining_days(cfg, live_expire_ts=None):
    """تعداد روز باقی‌مانده تا انقضای سرویس - دقیقاً همان منطق handlers/plans.py:
    اول تاریخ انقضای زنده (از پنل مصرف) و در نبود آن، تاریخ انقضای ذخیره‌شده روی کانفیگ."""
    remaining_days = days_remaining(live_expire_ts) if live_expire_ts else None
    if remaining_days is None and cfg.get("expiry"):
        try:
            _exp_dt = datetime.strptime(str(cfg["expiry"])[:10], "%Y-%m-%d")
            _delta_seconds = (_exp_dt - now_tehran_naive()).total_seconds()
            # گرد به بالا تا سرویس‌های فعال با کمتر از ۲۴ ساعت باقیمانده به‌اشتباه «منقضی‌شده» نمایش داده نشوند.
            remaining_days = 0 if _delta_seconds <= 0 else -(-int(_delta_seconds) // 86400)
        except Exception:
            remaining_days = None
    return remaining_days


@app.route("/api/services/<int:config_id>/usage", methods=["GET"])
@require_telegram_auth
def api_service_usage(config_id):
    """اطلاعات مصرف زنده‌ی سرویس VIP - دقیقاً همان منطق subscription.py که ربات
    استفاده می‌کند (شامل دنبال‌کردن لینک‌های میرور مثل down.hplo.ir/view)."""
    cfg = _own_config_or_none(config_id, g.user["id"])
    if cfg is None:
        return jsonify({"error": "not_found"}), 404

    try:
        sub_link = crypto.decrypt_config(cfg["config"])
    except Exception:
        return jsonify({"error": "decrypt_failed"}), 500

    if not sub_link or not sub_link.lower().startswith(("http://", "https://")):
        return jsonify({"usage": None, "remaining_days": _config_remaining_days(cfg)})

    # subscription.py آسنکرون است (aiohttp)؛ چون این ویو Flask سینک است،
    # با یک event loop مجزا برای همین درخواست اجرا می‌شود.
    try:
        usage = asyncio.run(fetch_subscription_info(sub_link))
    except Exception:
        usage = None

    if not usage:
        return jsonify({"usage": None, "remaining_days": _config_remaining_days(cfg)})

    total = usage.get("total")
    upload = usage.get("upload") or 0
    download = usage.get("download") or 0
    used = upload + download
    remaining = (total - used) if total else None
    percent = min(100, round(used / total * 100)) if total else None

    return jsonify({
        "usage": {
            "total": total,
            "used": used,
            "remaining": remaining,
            "percent": percent,
            "expire": usage.get("expire"),
        },
        "remaining_days": _config_remaining_days(cfg, usage.get("expire")),
    })


@app.route("/api/services", methods=["GET"])
@require_telegram_auth
def api_services():
    u = g.user
    vip_configs = [c for c in db.get_configs_by_type(u["id"], "vip") if not is_config_expired(c)]
    gaming_configs = [c for c in db.get_configs_by_type(u["id"], "gaming") if not is_config_expired(c)]

    vip_out = []
    for cfg in vip_configs:
        try:
            sub_link = crypto.decrypt_config(cfg["config"])
        except Exception:
            sub_link = None
        vip_out.append({
            "id": cfg["id"], "plan": cfg["plan"], "created_at": cfg["created_at"],
            "expiry": cfg["expiry"], "sub_link": sub_link, "has_qr": bool(cfg["qr_file_id"]),
            "remaining_days": _config_remaining_days(cfg),
        })

    gaming_out = []
    for cfg in gaming_configs:
        try:
            sub_link = crypto.decrypt_config(cfg["config"])
        except Exception:
            sub_link = None
        files = db.get_gaming_files(cfg["id"])
        gaming_out.append({
            "id": cfg["id"], "plan": cfg["plan"], "created_at": cfg["created_at"],
            "service_id": cfg["service_id"], "sub_link": sub_link,
            "files": [{"id": f["id"], "name": f["file_name"], "caption": f["caption"]} for f in files],
        })

    return jsonify({"vip": vip_out, "gaming": gaming_out})


def _own_config_or_none(config_id: int, user_id: int):
    cfg = db.get_config_by_id(config_id)
    if cfg is None or cfg["user_id"] != user_id or cfg.get("deleted"):
        return None
    return cfg


@app.route("/api/services/<int:config_id>/qr", methods=["GET"])
@require_telegram_auth
def api_service_qr(config_id):
    cfg = _own_config_or_none(config_id, g.user["id"])
    if cfg is None or not cfg["qr_file_id"]:
        return jsonify({"error": "not_found"}), 404
    content = tg_get_file_bytes(cfg["qr_file_id"])
    if content is None:
        return jsonify({"error": "fetch_failed"}), 502
    return send_file(io.BytesIO(content), mimetype="image/jpeg")


@app.route("/api/services/<int:config_id>/files/<int:file_db_id>/download", methods=["GET"])
@require_telegram_auth
def api_service_file_download(config_id, file_db_id):
    cfg = _own_config_or_none(config_id, g.user["id"])
    if cfg is None:
        return jsonify({"error": "not_found"}), 404
    files = db.get_gaming_files(config_id)
    match = next((f for f in files if f["id"] == file_db_id), None)
    if match is None:
        return jsonify({"error": "not_found"}), 404
    content = tg_get_file_bytes(match["file_id"])
    if content is None:
        return jsonify({"error": "fetch_failed"}), 502
    return send_file(
        io.BytesIO(content), mimetype="application/octet-stream",
        as_attachment=True, download_name=match["file_name"] or "config.conf",
    )


@app.route("/api/services/<int:config_id>/delete", methods=["POST"])
@require_telegram_auth
def api_service_delete(config_id):
    cfg = _own_config_or_none(config_id, g.user["id"])
    if cfg is None:
        return jsonify({"error": "not_found"}), 404
    db.set_config_deleted(config_id, True)
    return jsonify({"success": True})


# =============================================================================
# تمدید سرویس VIP / سرویس سفارشی («کانفیگ خودتو بساز» — فقط VIP)
# =============================================================================
def _custom_build_common(body, order_type, target_config_id=None, require_name=True):
    volume = body.get("volume_gb")
    days = body.get("days")
    name = (body.get("name") or "").strip() or None

    _cb_limits = db.get_effective_custom_build_settings()
    if not isinstance(volume, int) or not (_cb_limits["min_gb"] <= volume <= _cb_limits["max_gb"]):
        return None, (jsonify({"error": "invalid_volume"}), 400)
    if not isinstance(days, int) or not (_cb_limits["min_days"] <= days <= _cb_limits["max_days"]):
        return None, (jsonify({"error": "invalid_days"}), 400)
    if require_name and order_type == "new":
        if not name or not _LATIN_NAME_RE.match(name):
            return None, (jsonify({"error": "invalid_name"}), 400)

    price, agent_discount_applied = _calc_custom_price(volume, days, g.tg_user["id"])
    return {
        "volume": volume, "days": days, "name": name, "price": price,
        "agent_discount_applied": agent_discount_applied,
    }, None


@app.route("/api/custom-build/limits", methods=["GET"])
@require_telegram_auth
def api_custom_build_limits():
    _cb = db.get_effective_custom_build_settings()
    return jsonify({
        "min_gb": _cb["min_gb"],
        "max_gb": _cb["max_gb"],
        "min_days": _cb["min_days"],
        "max_days": _cb["max_days"],
        "price_per_gb": _cb["price_per_gb"],
        "price_per_30_days": _cb["price_per_30_days"],
    })


@app.route("/api/custom-build/quote", methods=["POST"])
@require_telegram_auth
def api_custom_build_quote():
    body = request.get_json(silent=True) or {}
    data, err = _custom_build_common(body, body.get("order_type", "new"), require_name=False)
    if err:
        return err
    return jsonify({"price": data["price"], "agent_discount_applied": data["agent_discount_applied"]})


@app.route("/api/custom-build/wallet", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=8, period_seconds=60)
def api_custom_build_wallet():
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403

    body = request.get_json(silent=True) or {}
    order_type = body.get("order_type", "new")
    target_config_id = body.get("target_config_id")

    if order_type == "renew":
        cfg = _own_config_or_none(target_config_id, g.user["id"])
        if cfg is None:
            return jsonify({"error": "service_not_found"}), 404

    data, err = _custom_build_common(body, order_type, target_config_id)
    if err:
        return err

    user = g.user
    if user["wallet"] < data["price"]:
        return jsonify({
            "error": "insufficient_balance", "price": data["price"],
            "wallet": user["wallet"], "needed": data["price"] - user["wallet"],
        }), 402

    desc = "خرید سرویس سفارشی" if order_type == "new" else "تمدید سرویس"
    if not db.deduct_from_wallet(user["id"], data["price"], desc):
        return jsonify({"error": "insufficient_balance"}), 402

    order_id = db.create_custom_order(
        user["id"], data["volume"], data["days"], data["name"], data["price"], order_type, target_config_id
    )
    db.set_custom_order_status(order_id, "paid")

    if data["volume"] >= REFERRAL_MIN_VOLUME_GB:
        try:
            db.complete_referral(user["id"])
        except ValueError:
            pass

    label = "تمدید سرویس" if order_type == "renew" else "سرویس سفارشی جدید (بساز سرویس خودت - Mini App)"

    # 🐛 فیکس: مثل خرید VIP، «بساز سرویس خودت» از کیف‌پول در مینی‌اپ هم قرار بود
    # طبق منطق ربات کلاسیک (handlers/plans.py، فراخوانی auto_fulfill_custom_via_panel)
    # اگر شاهراه فعال و نگاشت پیش‌فرض custom_build تنظیم شده باشد، به‌صورت
    # کاملاً خودکار ساخته و ارسال شود؛ ولی اینجا این مسیر اصلاً صدا زده نمی‌شود.
    handled = _run_async_with_temp_bot(
        lambda bot: auto_fulfill_custom_via_panel(
            bot, user, order_id, data["volume"], data["days"], data["name"]
        )
    )

    if handled:
        tg_send_message(
            ADMIN_ID,
            f"🛠 {label} — به‌صورت خودکار از پنل شاهراه ساخته و ارسال شد ✅\n\n"
            f"👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n"
            f"📦 حجم: {data['volume']} گیگ\n⏳ مدت: {data['days']} روز\n"
            + (f"🔤 نام: {data['name']}\n" if data["name"] else "")
            + f"💰 {data['price']:,} تومان (پرداخت‌شده از کیف پول)\n🔢 شماره سفارش: {order_id}",
        )
    else:
        tg_send_message(
            ADMIN_ID,
            f"🛠 {label}!\n\n👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n"
            f"📦 حجم: {data['volume']} گیگ\n⏳ مدت: {data['days']} روز\n"
            + (f"🔤 نام: {data['name']}\n" if data["name"] else "")
            + f"💰 {data['price']:,} تومان (پرداخت‌شده از کیف پول)\n🔢 شماره سفارش: {order_id}",
            reply_markup=inline_kb([("📤 شروع ارسال کانفیگ", f"sendcustomorder_{order_id}")]),
        )
    return jsonify({"success": True, "order_id": order_id, "price": data["price"]})


@app.route("/api/custom-build/card/info", methods=["POST"])
@require_telegram_auth
def api_custom_build_card_info():
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403

    body = request.get_json(silent=True) or {}
    data, err = _custom_build_common(body, body.get("order_type", "new"), require_name=False)
    if err:
        return err
    invoice = db.create_invoice(
        user_id=g.user["id"], telegram_id=str(g.user["telegram_id"]),
        kind="custom_card", label="سرویس سفارشی", price=data["price"],
    )
    return jsonify({
        "card_number": bot_info.get("card_number"), "card_holder": bot_info.get("card_holder"), "price": data["price"],
        "invoice_id": invoice["id"], "expires_at": invoice["expires_at"],
    })


@app.route("/api/custom-build/card/receipt", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=5, period_seconds=60)
def api_custom_build_card_receipt():
    # 🐛 فیکس: مشابه مورد api_order_card_receipt، این endpoint هم قبلاً orders_enabled
    # را دوباره بررسی نمی‌کرد (فقط api_custom_build_card_info این بررسی را دارد).
    if not db.is_orders_enabled():
        return jsonify({"error": "orders_closed"}), 403

    order_type = request.form.get("order_type", "new")
    target_config_id = request.form.get("target_config_id")
    target_config_id = int(target_config_id) if target_config_id and target_config_id.isdigit() else None
    photo = request.files.get("receipt")
    invoice_id = request.form.get("invoice_id", type=int)
    if photo is not None and not _valid_receipt_photo(photo):
        return jsonify({"error": "invalid_file_type"}), 400

    try:
        volume = int(request.form.get("volume_gb", ""))
        days = int(request.form.get("days", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_request"}), 400
    name = (request.form.get("name") or "").strip() or None

    if order_type == "renew":
        cfg = _own_config_or_none(target_config_id, g.user["id"])
        if cfg is None:
            return jsonify({"error": "service_not_found"}), 404
    _cb_bounds = db.get_effective_custom_build_settings()
    if not (_cb_bounds["min_gb"] <= volume <= _cb_bounds["max_gb"]) or not (_cb_bounds["min_days"] <= days <= _cb_bounds["max_days"]):
        return jsonify({"error": "invalid_request"}), 400
    if order_type == "new" and (not name or not _LATIN_NAME_RE.match(name)):
        return jsonify({"error": "invalid_name"}), 400
    if photo is None:
        return jsonify({"error": "receipt_required"}), 400

    if not invoice_id or db.consume_invoice(invoice_id) is None:
        return jsonify({"error": "invoice_expired"}), 409

    price, _agent_discount_applied = _calc_custom_price(volume, days, g.tg_user["id"])
    user = g.user
    order_id = db.create_custom_order(user["id"], volume, days, name, price, order_type, target_config_id)

    label = "تمدید سرویس" if order_type == "renew" else "سرویس سفارشی جدید (بساز سرویس خودت - Mini App)"
    caption = (
        f"💳 رسید {label}\n\n👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n"
        f"📦 حجم: {volume} گیگ\n⏳ مدت: {days} روز\n"
        + (f"🔤 نام: {name}\n" if name else "")
        + f"💰 {price:,} تومان\n🔢 شماره سفارش: {order_id}"
    )
    approve_kb = inline_kb([
        ("✅ تأیید پرداخت", f"approvecustom_{order_id}"),
        ("❌ رد رسید", f"rejectcustom_{order_id}"),
    ])
    tg_send_photo_bytes(ADMIN_ID, photo.read(), photo.filename or "receipt.jpg", caption=caption, reply_markup=approve_kb)

    if invoice_id:
        db.delete_invoice(invoice_id)

    return jsonify({"success": True, "order_id": order_id, "price": price})


# =============================================================================
# دعوت دوستان
# =============================================================================
@app.route("/api/referral", methods=["GET"])
@require_telegram_auth
def api_referral():
    u = g.user
    stats = db.get_referral_stats(u["id"])
    return jsonify({
        "invite_link": f"https://t.me/{bot_info.get('bot_username')}?start={stats['invite_code']}",
        "invite_code": stats["invite_code"],
        "invited_count": stats["invited_count"],
        "successful_invites": stats["successful_invites"],
        "released_amount": stats["released_amount"],
        "locked_wallet": u["locked_wallet"],
        "reward_amount": REFERRAL_LOCK_AMOUNT,
        "min_purchase_gb": REFERRAL_MIN_VOLUME_GB,
    })


# =============================================================================
# پشتیبانی (تیکت متنی)
# =============================================================================
@app.route("/api/support/ticket", methods=["POST"])
@require_telegram_auth
@rate_limit(max_calls=5, period_seconds=60)
def api_support_ticket():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text_required"}), 400
    if len(text) > 2000:
        return jsonify({"error": "text_too_long"}), 400

    user = g.user
    tg_send_message(
        ADMIN_ID,
        f"🎫 تیکت جدید (Mini App)\n👤 {g.tg_user.get('first_name', '')}\n🆔 {user['telegram_id']}\n\n💬 {text}",
        reply_markup=inline_kb([("↩️ پاسخ", f"replyticket_{user['telegram_id']}")]),
    )
    return jsonify({"success": True})


@app.route("/api/support/info", methods=["GET"])
@require_telegram_auth
def api_support_info():
    return jsonify({"channel_url": bot_info.get_support_url(), "guide_url": bot_info.get("connection_guide_url")})


if __name__ == "__main__":
    db.init_db()
    # برای اجرای واقعی پروداکشن به‌جای این، از gunicorn استفاده کنید، مثلا:
    # gunicorn -w 2 -b 0.0.0.0:8000 webapp_api:app
    app.run(host="0.0.0.0", port=int(os.environ.get("WEBAPP_API_PORT", "8000")))
