"""
marzban_panel.py
کلاینت آسنکرون (aiohttp) برای پنل‌های مرزبان (Marzban) — نسخه‌ی چندنمونه‌ای.

برخلاف marzban.py قدیمی (که فقط یک پنل ثابت از .env می‌خواند)، همه‌ی توابع این
فایل یک دیکشنری `panel` (یک ردیف از جدول vpn_panels شامل حداقل
id/base_url/username/password) می‌گیرند تا بتوان همزمان چند نمونه‌ی مستقل از
پنل مرزبان (هرکدام با آدرس/کاربری جدا) روی ربات فعال داشت.

توکن و لیست تمپلیت‌ها به‌ازای هر panel["id"] جداگانه کش می‌شوند تا نمونه‌های
مختلف با هم تداخل نکنند.
"""

import asyncio
import logging
import time

import aiohttp

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10)

# کش‌ها به ازای هر panel_id جداگانه نگه‌داری می‌شوند (چندنمونه‌ای).
_token_cache: dict[int, dict] = {}
_templates_cache: dict[int, dict] = {}
_sessions: dict[int, aiohttp.ClientSession] = {}
_session_lock = asyncio.Lock()

_MAX_PANEL_RETRIES = 2
_PANEL_RETRY_DELAY = 1.5


def _pid(panel: dict) -> int:
    return int(panel["id"])


async def _get_session(panel: dict) -> aiohttp.ClientSession:
    pid = _pid(panel)
    sess = _sessions.get(pid)
    if sess is not None and not sess.closed:
        return sess
    async with _session_lock:
        sess = _sessions.get(pid)
        if sess is None or sess.closed:
            connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300, keepalive_timeout=75)
            sess = aiohttp.ClientSession(timeout=_TIMEOUT, connector=connector)
            _sessions[pid] = sess
        return sess


async def _get_token(panel: dict):
    base_url = (panel.get("base_url") or "").rstrip("/")
    username = panel.get("username")
    password = panel.get("password")
    if not (base_url and username and password):
        return None, "اطلاعات اتصال این پنل مرزبان (آدرس/یوزرنیم/پسورد) کامل تنظیم نشده."

    pid = _pid(panel)
    cache = _token_cache.setdefault(pid, {"token": None, "expires_at": 0.0})
    now = time.monotonic()
    if cache["token"] and now < cache["expires_at"]:
        return cache["token"], None

    payload = {"username": username, "password": password, "grant_type": "password"}
    for attempt in range(_MAX_PANEL_RETRIES + 1):
        try:
            session = await _get_session(panel)
            async with session.post(f"{base_url}/api/admin/token", data=payload) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
                if resp.status != 200 or not isinstance(data, dict) or not data.get("access_token"):
                    detail = (data or {}).get("detail") if isinstance(data, dict) else None
                    return None, f"ورود به پنل مرزبان «{panel.get('name', '')}» ناموفق بود ({resp.status}): {detail or 'بدون جزئیات'}"
                token = data["access_token"]
                cache["token"] = token
                cache["expires_at"] = now + 20 * 60
                return token, None
        except Exception:
            if attempt < _MAX_PANEL_RETRIES:
                await asyncio.sleep(_PANEL_RETRY_DELAY)
                continue
            logger.exception("خطا در اتصال به پنل مرزبان هنگام دریافت توکن")
            return None, "خطا در برقراری ارتباط با پنل مرزبان (شبکه/سرور در دسترس نیست)."


async def _request(panel: dict, method: str, path: str, json_body: dict | None = None, params: dict | None = None):
    token, err = await _get_token(panel)
    if err:
        return False, None, err

    base_url = (panel.get("base_url") or "").rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    status = None
    for attempt in range(_MAX_PANEL_RETRIES + 1):
        try:
            session = await _get_session(panel)
            async with session.request(
                method, f"{base_url}{path}", json=json_body, params=params, headers=headers
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
                status = resp.status
            break
        except Exception:
            if attempt < _MAX_PANEL_RETRIES:
                await asyncio.sleep(_PANEL_RETRY_DELAY)
                continue
            logger.exception("خطا در ارتباط با پنل مرزبان (%s %s)", method, path)
            return False, None, "خطا در برقراری ارتباط با پنل مرزبان (شبکه/سرور در دسترس نیست)."

    if status == 401:
        _token_cache.setdefault(_pid(panel), {})["token"] = None
        return False, data, "توکن نامعتبر/منقضی بود (401). لطفاً دوباره تلاش کنید."
    if status == 404:
        return False, data, f"مورد مورد نظر در پنل مرزبان پیدا نشد (404). مسیر: {path}"
    if status == 409:
        detail = (data or {}).get("detail") if isinstance(data, dict) else None
        return False, data, f"تداخل (409): {detail or 'این نام کاربری از قبل وجود دارد.'}"
    if status >= 400:
        detail = (data or {}).get("detail") if isinstance(data, dict) else None
        return False, data, f"خطای پنل مرزبان ({status}): {detail or data or 'بدون جزئیات'}"

    return True, data, "موفق"


async def test_connection(panel: dict):
    return await _request(panel, "GET", "/api/system")


async def get_system_stats(panel: dict):
    return await _request(panel, "GET", "/api/system")


async def get_templates(panel: dict, force_refresh: bool = False):
    pid = _pid(panel)
    cache = _templates_cache.setdefault(pid, {"items": None, "expires_at": 0.0})
    now = time.monotonic()
    if (not force_refresh) and cache.get("items") is not None and now < cache.get("expires_at", 0):
        return True, cache["items"], "موفق (cache)"
    ok, data, msg = await _request(panel, "GET", "/api/user_template")
    if ok:
        cache["items"] = data
        cache["expires_at"] = now + 1800
    return ok, data, msg


async def get_template(panel: dict, template_id: int):
    ok, items, msg = await get_templates(panel)
    if not ok:
        return False, None, msg
    items = items if isinstance(items, list) else []
    for it in items:
        if isinstance(it, dict) and str(it.get("id")) == str(template_id):
            return True, it, "موفق"
    ok, items, msg = await get_templates(panel, force_refresh=True)
    if ok:
        items = items if isinstance(items, list) else []
        for it in items:
            if isinstance(it, dict) and str(it.get("id")) == str(template_id):
                return True, it, "موفق"
    return False, None, f"تمپلیت با شناسه‌ی {template_id} در لیست تمپلیت‌های پنل پیدا نشد (NOT_FOUND)."


def _proxies_for_template(template: dict) -> dict:
    inbounds = template.get("inbounds") or {}
    if inbounds:
        return {protocol: {} for protocol in inbounds.keys()}
    return {"vless": {}}


def _group_ids_for_template(template: dict) -> list:
    group_ids = template.get("group_ids")
    if isinstance(group_ids, list) and group_ids:
        return group_ids
    groups = template.get("groups")
    if isinstance(groups, list) and groups:
        ids = []
        for g in groups:
            if isinstance(g, dict) and g.get("id") is not None:
                ids.append(g["id"])
            elif isinstance(g, (int, str)):
                ids.append(g)
        return ids
    return []


def _data_limit_bytes(volume_gb) -> int:
    try:
        gb = float(volume_gb or 0)
    except (TypeError, ValueError):
        gb = 0
    return int(gb * 1024 ** 3) if gb > 0 else 0


def _expire_from_days(days) -> int:
    # توجه: قبلاً با int(days) متد به عدد صحیح روز گرد می‌شد، که برای پلن‌های تست رایگان
    # ساعتی (مثلاً 0.5 روز = 12 ساعت) این مقدار را به 0 گرد می‌کرد و باعث می‌شد
    # سرویس بدون هیچ انقضایی (نامحدود) ساخته شود. با float مقدار اعشاری هم درست محاسبه می‌شود.
    try:
        d = float(days or 0)
    except (TypeError, ValueError):
        d = 0
    return int(time.time() + d * 86400) if d > 0 else 0


async def create_user_from_template(panel: dict, template_id: int, username: str):
    ok, template, msg = await get_template(panel, template_id)
    if not ok:
        return False, None, f"دریافت اطلاعات تمپلیت ناموفق بود: {msg}"

    data_limit = template.get("data_limit") or 0
    expire_duration = template.get("expire_duration") or 0
    expire = int(time.time()) + expire_duration if expire_duration else 0

    body = {
        "username": username,
        "proxies": _proxies_for_template(template),
        "inbounds": template.get("inbounds") or {},
        "expire": expire,
        "data_limit": data_limit,
        "data_limit_reset_strategy": "no_reset",
        "status": "active",
    }
    group_ids = _group_ids_for_template(template)
    if group_ids:
        body["group_ids"] = group_ids
    return await _request(panel, "POST", "/api/user", json_body=body)


async def create_user_custom(panel: dict, template_id: int, username: str, volume_gb, days):
    ok, template, msg = await get_template(panel, template_id)
    if not ok:
        return False, None, f"دریافت اطلاعات تمپلیت ناموفق بود: {msg}"

    body = {
        "username": username,
        "proxies": _proxies_for_template(template),
        "inbounds": template.get("inbounds") or {},
        "expire": _expire_from_days(days),
        "data_limit": _data_limit_bytes(volume_gb),
        "data_limit_reset_strategy": "no_reset",
        "status": "active",
    }
    group_ids = _group_ids_for_template(template)
    if group_ids:
        body["group_ids"] = group_ids
    return await _request(panel, "POST", "/api/user", json_body=body)


async def renew_user(panel: dict, username: str, template_id: int):
    ok, template, msg = await get_template(panel, template_id)
    if not ok:
        return False, None, f"دریافت اطلاعات تمپلیت ناموفق بود: {msg}"
    data_limit = template.get("data_limit") or 0
    expire_duration = template.get("expire_duration") or 0
    expire = int(time.time()) + expire_duration if expire_duration else 0
    body = {"data_limit": data_limit, "expire": expire, "status": "active"}
    return await _request(panel, "PUT", f"/api/user/{username}", json_body=body)


async def renew_user_custom(panel: dict, username: str, volume_gb, days):
    """تمدید سرویس موجود: برخلاف ساخت سرویس جدید، حجم و روزهای انتخاب‌شده به مقدار/تاریخ
    باقی‌مانده‌ی فعلی سرویس اضافه می‌شود (نه اینکه با یک مقدار کاملاً تازه
    از الان جایگزین شود)؛ یعنی تمدید واقعاً روی اعتبار قبلی کاربر
    می‌نشیند، نه اینکه آن را ریست کند."""
    ok, current, _ = await get_user(panel, username)
    current = current if ok and isinstance(current, dict) else {}
    current_data_limit = current.get("data_limit") or 0
    current_expire = current.get("expire") or 0

    add_bytes = _data_limit_bytes(volume_gb)
    if current_data_limit == 0 or add_bytes == 0:
        # سرویس فعلی از قبل نامحدود بوده یا حجم جدیدی برای اضافه‌شدن انتخاب نشده: نامحدود می‌ماند.
        new_data_limit = 0
    else:
        new_data_limit = current_data_limit + add_bytes

    add_seconds = 0
    try:
        d = float(days or 0)
        add_seconds = int(d * 86400) if d > 0 else 0
    except (TypeError, ValueError):
        add_seconds = 0
    if add_seconds == 0:
        new_expire = current_expire
    else:
        base = current_expire if current_expire and current_expire > int(time.time()) else int(time.time())
        new_expire = base + add_seconds

    body = {"data_limit": new_data_limit, "expire": new_expire, "status": "active"}
    return await _request(panel, "PUT", f"/api/user/{username}", json_body=body)


async def get_user(panel: dict, username: str):
    return await _request(panel, "GET", f"/api/user/{username}")


async def disable_user(panel: dict, username: str):
    return await _request(panel, "PUT", f"/api/user/{username}", json_body={"status": "disabled"})


async def enable_user(panel: dict, username: str):
    return await _request(panel, "PUT", f"/api/user/{username}", json_body={"status": "active"})


async def revoke_sub(panel: dict, username: str):
    return await _request(panel, "POST", f"/api/user/{username}/revoke_sub")


async def delete_user(panel: dict, username: str):
    return await _request(panel, "DELETE", f"/api/user/{username}")


def extract_link_and_username(panel: dict, payload: dict):
    if not isinstance(payload, dict):
        return None, None
    link = payload.get("subscription_url")
    if link and isinstance(link, str) and link.startswith("/"):
        link = (panel.get("base_url") or "").rstrip("/") + link
    username = payload.get("username")
    return link, username
