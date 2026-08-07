"""
subscription.py
دریافت زنده‌ی اطلاعات مصرف (حجم، تاریخ انقضا، نام سرویس) از روی لینک ساب کاربر،
بدون نیاز به هیچ دسترسی به دیتابیس یا API پنل.

توضیح فنی:
اکثر پنل‌های V2Ray/X-UI/Marzban/Hiddify و مشابه، وقتی یک درخواست GET به لینک ساب
زده شود (دقیقاً همان کاری که اپ‌های کلاینت مثل v2rayNG برای نمایش حجم باقی‌مانده
انجام می‌دهند)، یک هدر استاندارد به نام Subscription-Userinfo برمی‌گردانند؛
چیزی شبیه:
    upload=1073741824; download=2147483648; total=53687091200; expire=1751328000
همچنین بسیاری از پنل‌ها هدر Profile-Title را هم برمی‌گردانند که نام سرویس را
به‌صورت base64 دارد.

بعضی پنل‌ها (مثل Hiddify) به‌جای لینک خام ساب، یک لینک «نمایش در مرورگر»
(چیزی شبیه down.hplo.ir/view?...) می‌دهند که یک صفحه‌ی HTML برمی‌گرداند، نه
هدرهای بالا. در این حالت باید لینک ساب واقعی را از داخل همان صفحه پیدا کرد.
این ماژول این حالت را هم به‌صورت best-effort پوشش می‌دهد.

⚠️ توجه: چون این محیط به اینترنت دسترسی ندارد، این بخش قابل تست مستقیم روی
لینک‌های واقعی نبوده؛ اگر باز هم لینک‌های down.hplo.ir جواب ندادند، لطفاً یک
نمونه لینک واقعی (یا خروجی که مرورگر/curl از آن می‌گیرد) بفرست تا دقیق‌تر اصلاح شود.
"""

import base64
import re
from datetime import datetime

from utils import TEHRAN_TZ, now_tehran_naive

import aiohttp

# هدرهایی که شبیه یک کلاینت واقعی V2Ray/Clash هستند؛ خیلی از پنل‌ها بدون
# User-Agent مناسب، درخواست را رد می‌کنند یا صفحه‌ی HTML عادی برمی‌گردانند.
_CLIENT_HEADERS = {
    "User-Agent": "v2rayNG/1.8.29 (Linux; Android)",
    "Accept": "*/*",
}

_SUB_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

# اسکیم‌های پروتکل‌های کانفیگ تکی که ممکن است داخل بدنه‌ی یک لینک ساب باشند.
_CONFIG_SCHEMES = ("vmess://", "vless://", "trojan://", "ss://", "ssr://", "hysteria://", "hysteria2://", "hy2://", "tuic://")


async def _get(session: aiohttp.ClientSession, url: str):
    # اعتبارسنجی TLS برای حفاظت از لینک محرمانه اشتراک فعال است.
    async with session.get(url, allow_redirects=True) as resp:
        headers = dict(resp.headers)
        try:
            body = await resp.text(errors="ignore")
        except Exception:
            body = ""
        return resp.status, headers, body, str(resp.url)


def _looks_like_html(body: str) -> bool:
    head = (body or "").strip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<head" in head


def _decode_profile_title(headers: dict) -> str | None:
    raw = headers.get("Profile-Title") or headers.get("profile-title")
    if not raw:
        return None
    raw = raw.strip()
    if raw.lower().startswith("base64:"):
        raw = raw[7:]
    try:
        return base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", errors="ignore").strip()
    except Exception:
        return raw or None


def _parse_userinfo(headers: dict) -> dict | None:
    header = headers.get("Subscription-Userinfo") or headers.get("subscription-userinfo")
    if not header:
        return None
    info = {}
    for part in header.split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            try:
                info[key.strip()] = int(value.strip())
            except ValueError:
                pass
    return info if info else None


def _extract_name_from_body(body: str) -> str | None:
    """اگر بدنه یک لینک ساب خام باشد (base64 از چند کانفیگ)، تلاش می‌کند
    از remark (بعد از #) اولین کانفیگ، یک اسم دربیاورد."""
    if not body:
        return None
    candidate = body.strip()
    decoded = None
    try:
        decoded = base64.b64decode(candidate + "=" * (-len(candidate) % 4)).decode("utf-8", errors="ignore")
    except Exception:
        decoded = None

    text = decoded if decoded and ("://" in decoded) else (candidate if "://" in candidate else None)
    if not text:
        return None

    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if "#" in first_line:
        from urllib.parse import unquote
        remark = first_line.split("#", 1)[1].strip()
        remark = unquote(remark)
        if remark:
            return remark
    return None


def _find_embedded_sub_link(html: str) -> str | None:
    """در صفحات «نمایش در مرورگر» (مثل Hiddify) دنبال لینک ساب واقعی درون HTML/JS می‌گردد."""
    if not html:
        return None
    candidates = _SUB_URL_PATTERN.findall(html)
    # اولویت با لینک‌هایی که به نظر لینک ساب واقعی می‌رسند (نه فایل‌های استاتیک/آیکون)
    for url in candidates:
        low = url.lower()
        if any(bad in low for bad in [".png", ".jpg", ".css", ".js", ".ico", ".svg", ".woff"]):
            continue
        if any(good in low for good in ["/sub", "/api/", "sub/", "subscribe"]):
            return url.rstrip("\"'<>),.;")
    return None


async def fetch_subscription_info(sub_url: str) -> dict | None:
    """نسخه‌ی سازگار قبلی: فقط upload/download/total/expire را برمی‌گرداند."""
    meta = await extract_meta(sub_url)
    return meta.get("userinfo") if meta else None


async def extract_meta(sub_url: str, _depth: int = 0, _retry: int = 0) -> dict | None:
    """
    اطلاعات کامل یک لینک ساب را برمی‌گرداند:
        {
            "userinfo": {"upload":.., "download":.., "total":.., "expire":..} یا None,
            "name": "..." یا None,
            "final_url": "...",  # لینک نهایی بعد از دنبال کردن ریدایرکت‌ها
        }
    اگر لینک اصلاً قابل‌دسترسی نبود، None برمی‌گرداند.
    """
    if not sub_url or not sub_url.strip().lower().startswith(("http://", "https://")):
        return None

    try:
        timeout = aiohttp.ClientTimeout(total=20, connect=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=_CLIENT_HEADERS) as session:
            status, headers, body, final_url = await _get(session, sub_url.strip())

            userinfo = _parse_userinfo(headers)
            name = _decode_profile_title(headers)

            if userinfo or name:
                if not name:
                    name = _extract_name_from_body(body)
                return {"userinfo": userinfo, "name": name, "final_url": final_url}

            # اگر هدرهای موردنظر نیامدند و صفحه شبیه HTML بود (مثلاً صفحه‌ی
            # «نمایش در مرورگر» مثل down.hplo.ir/view)، دنبال لینک ساب واقعی
            # درون همان صفحه می‌گردیم و یک‌بار دیگر تلاش می‌کنیم.
            if _looks_like_html(body) and _depth == 0:
                embedded = _find_embedded_sub_link(body)
                if embedded and embedded != sub_url:
                    return await extract_meta(embedded, _depth=1)

            # حالت آخر: شاید بدنه خودش یک ساب خام base64 باشد بدون هدر مفید.
            name = _extract_name_from_body(body)
            if name:
                return {"userinfo": None, "name": name, "final_url": final_url}

            return None
    except Exception:
        # بعضی پنل‌ها (مخصوصاً لینک‌های میرور/نمایش‌در‌مرورگر) موقع اولین
        # درخواست کند یا موقتاً ناپایدار هستند؛ درست مثل extract_configs،
        # قبل از اعلام شکست نهایی یک‌بار دیگر تلاش می‌کنیم. این دقیقاً همان
        # نامتقارنی‌ای بود که باعث می‌شد یک لینک در مینی‌اپ (که extract_configs
        # را با retry صدا می‌زد) جواب بدهد ولی در ربات (که فقط extract_meta
        # بدون retry را صدا می‌زد) شکست بخورد.
        if _retry == 0:
            return await extract_meta(sub_url, _depth=_depth, _retry=1)
        return None


def _parse_configs(body: str) -> list[str]:
    """بدنه‌ی خام لینک ساب (معمولاً base64) را به لیست کانفیگ‌های تکی تبدیل می‌کند."""
    if not body:
        return []
    text = body.strip()
    try:
        decoded = base64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", errors="ignore")
    except Exception:
        decoded = None

    candidate = decoded if decoded and any(s in decoded for s in _CONFIG_SCHEMES) else text
    lines = [ln.strip() for ln in candidate.splitlines() if ln.strip()]
    return [ln for ln in lines if ln.startswith(_CONFIG_SCHEMES)]


async def extract_configs(sub_url: str, _depth: int = 0, _retry: int = 0) -> list[str] | None:
    """کانفیگ‌های تکی (vmess/vless/trojan/...) را از داخل یک لینک ساب استخراج
    می‌کند، حتی اگر پشت یک صفحه‌ی میرور «نمایش در مرورگر» مثل down.hplo.ir/view
    پنهان شده باشد (همان منطق extract_meta برای پیدا کردن لینک واقعی).

    این تابع دقیقاً همان چیزی است که هم ربات (mirrorconfigs_) و هم مینی‌اپ
    (api/services/<id>/configs) استفاده می‌کنند؛ اگر روی مینی‌اپ جواب می‌دهد ولی
    روی ربات نه، معمولاً به‌خاطر timeout کوتاه یا یک تلاش ناموفق تک‌مرحله‌ای است
    (که اینجا با افزایش timeout و یک retry خودکار جبران شده).

    خروجی:
        None            → لینک اصلاً قابل‌دسترسی نبود (حتی بعد از retry)
        []              → لینک باز شد ولی هیچ کانفیگی داخلش پیدا نشد
        [کانفیگ, ...]   → کانفیگ‌های پیداشده
    """
    if not sub_url or not sub_url.strip().lower().startswith(("http://", "https://")):
        return None

    try:
        timeout = aiohttp.ClientTimeout(total=20, connect=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=_CLIENT_HEADERS) as session:
            status, headers, body, final_url = await _get(session, sub_url.strip())

            configs = _parse_configs(body)
            if configs:
                return configs

            if _looks_like_html(body) and _depth == 0:
                embedded = _find_embedded_sub_link(body)
                if embedded and embedded != sub_url:
                    return await extract_configs(embedded, _depth=1)

            return []
    except Exception:
        # بعضی پنل‌ها موقع اولین درخواست کند/ناپایدار هستند (timeout یا قطعی
        # موقت اتصال)؛ قبل از اعلام شکست نهایی، یک‌بار دیگر تلاش می‌کنیم.
        if _retry == 0:
            return await extract_configs(sub_url, _depth=_depth, _retry=1)
        return None


def format_bytes(num_bytes) -> str:
    """بایت را به شکل خوانا مثل «۱۲.۴ گیگابایت» تبدیل می‌کند."""
    if num_bytes is None:
        return "نامشخص"
    try:
        num_bytes = int(num_bytes)
    except (TypeError, ValueError):
        return "نامشخص"

    gb = num_bytes / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} گیگابایت"
    mb = num_bytes / (1024 ** 2)
    return f"{mb:.0f} مگابایت"


def format_expire(expire_ts) -> str:
    """تایم‌استمپ انقضا را به تاریخ خوانا تبدیل می‌کند."""
    if not expire_ts:
        return "نامحدود"
    try:
        dt = datetime.fromtimestamp(int(expire_ts), tz=TEHRAN_TZ).replace(tzinfo=None)
        return dt.strftime("%Y/%m/%d")
    except Exception:
        return "نامشخص"


def usage_bar(percent, length: int = 10) -> str:
    """نوار پیشرفت مصرف با ایموجی؛ مثل 🟩🟩🟩🟩🟩🟩⬜⬜⬜⬜ ۶۰٪"""
    try:
        percent = max(0, min(100, float(percent)))
    except (TypeError, ValueError):
        percent = 0
    filled = round(length * percent / 100)
    color = "🟥" if percent >= 90 else ("🟨" if percent >= 80 else "🟩")
    return color * filled + "⬜" * (length - filled)


def days_remaining(expire_ts) -> int | None:
    if not expire_ts:
        return None
    try:
        dt = datetime.fromtimestamp(int(expire_ts), tz=TEHRAN_TZ).replace(tzinfo=None)
        delta = dt - now_tehran_naive()
        total_seconds = delta.total_seconds()
        if total_seconds <= 0:
            return 0
        # به‌جای floor ساده‌ی .days (که برای هر باقیمانده‌ی کمتر از ۲۴ ساعت صفر
        # برمی‌گشت و باعث می‌شد سرویس‌های فعال با چند ساعت باقیمانده به‌اشتباه
        # «منقضی‌شده» نمایش داده شوند)، به بالا گرد می‌کنیم تا حداقل ۱ روز نشان بدهد.
        return -(-int(total_seconds) // 86400)
    except Exception:
        return None


def is_config_expired(cfg: dict) -> bool:
    """بررسی اینکه آیا یک کانفیگ منقضی شده است یا خیر.
    اگر expiry تنظیم نشده باشد (None) False برمی‌گرداند (نامحدود فرض می‌شود).
    """
    expiry = cfg.get("expiry")
    if not expiry:
        return False
    try:
        exp_dt = datetime.strptime(str(expiry)[:10], "%Y-%m-%d")
        return exp_dt < now_tehran_naive()
    except Exception:
        return False


