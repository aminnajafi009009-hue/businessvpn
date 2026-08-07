"""
uniquepay_sync.py
همان کلاینت uniquepay.py ولی نسخه‌ی sync (با requests) برای استفاده در
webapp_api.py که یک اپ Flask سینک است (بدون event loop آسنکرون).
منطق و پارامترها دقیقاً مطابق مستندات رسمی API یونیک‌پی و آینه‌ی uniquepay.py است.
"""

import logging
import uuid

import requests

from config import UNIQUEPAY_BASE_URL, UNIQUEPAY_BUSINESS_TOKEN, UNIQUEPAY_REDIRECT_URL

logger = logging.getLogger(__name__)

_TIMEOUT = 20

# مثل uniquepay.py (نسخه‌ی آسنکرون ربات) و shahrah.py: بدون User-Agent شبیه
# مرورگر، درخواست‌های requests به‌عنوان ترافیک بات شناسایی و توسط
# Cloudflare بلاک/چلنج می‌شوند و درگاه پرداخت آنلاین در Mini App باز نمی‌شود.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {UNIQUEPAY_BUSINESS_TOKEN}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": _USER_AGENT,
    }


def new_hash_id(prefix: str = "miniapp") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def create_invoice(hash_id: str, amount: int, redirect_url: str | None = None) -> dict | None:
    if not UNIQUEPAY_BUSINESS_TOKEN:
        return None

    payload = {
        "hashId": hash_id,
        "amount": str(int(amount)),
        "redirectUrl": redirect_url or UNIQUEPAY_REDIRECT_URL,
    }
    try:
        resp = requests.post(
            f"{UNIQUEPAY_BASE_URL}/api/create-invoice", data=payload, headers=_headers(), timeout=_TIMEOUT
        )
        data = resp.json()
    except requests.RequestException:
        logger.exception("خطا در ارتباط با UniquePay هنگام ساخت اینوویس (miniapp)")
        return None
    except ValueError:
        # پاسخ JSON نبود؛ احتمالاً صفحه‌ی چلنج Cloudflare یا خطای سرور برگشته.
        # برخلاف قبل، بدنه‌ی خام را لاگ می‌کنیم تا علت واقعی مخفی نماند.
        logger.error(
            "UniquePay create-invoice (miniapp) پاسخ غیر-JSON داد | status=%s body=%s",
            resp.status_code, resp.text[:500],
        )
        return None

    if not data or not data.get("status"):
        logger.warning("UniquePay create-invoice (miniapp) ناموفق بود: %s", data)
        return None
    return data


def check_invoice(hash_id: str) -> dict | None:
    if not UNIQUEPAY_BUSINESS_TOKEN:
        return None

    try:
        resp = requests.post(
            f"{UNIQUEPAY_BASE_URL}/api/check-invoice",
            data={"hashId": hash_id},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        data = resp.json()
    except requests.RequestException:
        logger.exception("خطا در ارتباط با UniquePay هنگام بررسی اینوویس (miniapp)")
        return None
    except ValueError:
        logger.error(
            "UniquePay check-invoice (miniapp) پاسخ غیر-JSON داد | status=%s body=%s",
            resp.status_code, resp.text[:500],
        )
        return None

    if not data or not data.get("status"):
        return None
    return data.get("invoice")
