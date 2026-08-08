"""
panels.py
لایه‌ی یکپارچه‌شده‌ی مشترک برای هر سه نوع پنل پشتیبانی‌شده (شاهراه/مرزبان/پاسارگارد).

هیچ‌جای دیگری از ربات (هندلرها/منطق فروش و...) نباید مستقیماً به shahrah.py /
 marzban_panel.py / pasargad_panel.py وصل شود؛ همه باید از همین دو تابع استفاده کنند تا بتوان
 همزمان از هر سه نوع پنل (و از چند نمونه همزمان از هر نوع) پشتیبانی کرد.

هر `panel` یک dict (ردیف جدول vpn_panels) است شامل کلیدهای:
id, panel_type ('shahrah'|'marzban'|'pasargad'), name, base_url, api_key, username, password, enabled.
"""

import shahrah
import marzban_panel
import pasargad_panel

PANEL_TYPE_LABELS = {
    "shahrah": "شاهراه",
    "marzban": "مرزبان",
    "pasargad": "پاسارگارد",
}

PANEL_TYPES = list(PANEL_TYPE_LABELS.keys())


def panel_label(panel: dict) -> str:
    type_label = PANEL_TYPE_LABELS.get(panel.get("panel_type"), panel.get("panel_type") or "?")
    return f"{type_label} — {panel.get('name') or ('#' + str(panel.get('id')))}"


def _client(panel: dict):
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        return shahrah
    if ptype == "marzban":
        return marzban_panel
    if ptype == "pasargad":
        return pasargad_panel
    return None


async def test_connection(panel: dict) -> tuple[bool, dict | None, str]:
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        return await shahrah.get_me(panel)
    if ptype == "marzban":
        return await marzban_panel.test_connection(panel)
    if ptype == "pasargad":
        return await pasargad_panel.test_connection(panel)
    return False, None, "نوع پنل نامعتبر."


async def get_catalog(panel: dict) -> tuple[list[dict], str]:
    """فهرست بسته/تمپلیت‌های قابل‌نگاشت روی این نمونه‌ی پنل را به یک قالب یکسان
    (idx/ref/name/label) برمی‌گرداند تا منوی نگاشت بدون توجه به نوع پنل یکسان باشد."""
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        ok, data, msg = await shahrah.get_plans(panel)
        if not ok:
            return [], msg
        items = []
        if isinstance(data, dict):
            items = data.get("items") or data.get("plans") or data.get("data") or []
        choices = []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            slug = it.get("slug") or it.get("planSlug")
            if not slug:
                continue
            name = it.get("name") or it.get("title") or slug
            label = f"📦 {name} ({slug})"
            if len(label) > 60:
                label = label[:57] + "..."
            choices.append({"idx": i, "ref": slug, "name": name, "label": label})
        if not choices:
            return [], "هیچ بسته‌ای در پاسخ /plans پیدا نشد."
        return choices, "موفق"

    client = marzban_panel if ptype == "marzban" else pasargad_panel if ptype == "pasargad" else None
    if client is None:
        return [], "نوع پنل نامعتبر."
    ok, data, msg = await client.get_templates(panel, force_refresh=True)
    if not ok:
        return [], msg
    items = data if isinstance(data, list) else []
    choices = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or it.get("id") is None:
            continue
        ref = str(it["id"])
        name = it.get("name") or f"Template {ref}"
        label = f"📦 {name} (id: {ref})"
        if len(label) > 60:
            label = label[:57] + "..."
        choices.append({"idx": i, "ref": ref, "name": name, "label": label})
    if not choices:
        return [], "هیچ تمپلیتی در پنل پیدا نشد. اول یک تمپلیت در خود پنل بساز."
    return choices, "موفق"


async def create_service(panel: dict, username: str, remote_ref: str, volume_gb=None, days=None):
    """(ok, link, remote_service_id, raw_data, message)"""
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        ok, data, msg = await shahrah.create_service(panel, remote_ref, username)
        if not ok:
            return False, None, None, data, msg
        link, slug = shahrah.extract_link_and_slug(data)
        return True, link, slug or username, data, msg

    client = marzban_panel if ptype == "marzban" else pasargad_panel if ptype == "pasargad" else None
    if client is None:
        return False, None, None, None, "نوع پنل نامعتبر."
    template_id = int(remote_ref)
    if volume_gb is not None or days is not None:
        ok, data, msg = await client.create_user_custom(panel, template_id, username, volume_gb, days)
    else:
        ok, data, msg = await client.create_user_from_template(panel, template_id, username)
    if not ok:
        return False, None, None, data, msg
    link, uname = client.extract_link_and_username(panel, data)
    return True, link, uname or username, data, msg


async def renew_service(panel: dict, service_id: str, remote_ref: str | None = None, volume_gb=None, days=None):
    """(ok, link, remote_service_id, raw_data, message)"""
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        ok, data, msg = await shahrah.renew_service(panel, service_id, remote_ref)
        if not ok:
            return False, None, service_id, data, msg
        link, slug = shahrah.extract_link_and_slug(data)
        return True, link, slug or service_id, data, msg

    client = marzban_panel if ptype == "marzban" else pasargad_panel if ptype == "pasargad" else None
    if client is None:
        return False, None, service_id, None, "نوع پنل نامعتبر."
    if remote_ref is not None:
        ok, data, msg = await client.renew_user(panel, service_id, int(remote_ref))
    else:
        ok, data, msg = await client.renew_user_custom(panel, service_id, volume_gb, days)
    if not ok:
        return False, None, service_id, data, msg
    link, uname = client.extract_link_and_username(panel, data)
    return True, link, uname or service_id, data, msg


async def disable_service(panel: dict, service_id: str) -> tuple[bool, str]:
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        ok, data, msg = await shahrah.disable_service(panel, service_id)
    elif ptype == "marzban":
        ok, data, msg = await marzban_panel.disable_user(panel, service_id)
    elif ptype == "pasargad":
        ok, data, msg = await pasargad_panel.disable_user(panel, service_id)
    else:
        return False, "نوع پنل نامعتبر."
    return ok, msg


async def enable_service(panel: dict, service_id: str) -> tuple[bool, str]:
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        ok, data, msg = await shahrah.enable_service(panel, service_id)
    elif ptype == "marzban":
        ok, data, msg = await marzban_panel.enable_user(panel, service_id)
    elif ptype == "pasargad":
        ok, data, msg = await pasargad_panel.enable_user(panel, service_id)
    else:
        return False, "نوع پنل نامعتبر."
    return ok, msg


async def regenerate_sub_link(panel: dict, service_id: str):
    """(ok, link, remote_service_id, raw_data, message) — فقط لینک ساب/توکن سرویس را عوض می‌کند بدون اینکه حجم یا تاریخ انقضای
    باقی‌مانده‌ی سرویس تغییر کند. برخلاف renew_service، اینجا هیچ پلان/حجم/روزی
    گرفته نمی‌شود چون قرار نیست چیزی اضافه یا جایگزین شود؛ فقط دسترسی قبلی
    (لینک قدیمی) قطع و یک لینک جدید صادر می‌شود."""
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        return False, None, service_id, None, "برای این نوع پنل امکان تغییر خودکار لینک بدون تغییر بسته وجود ندارد؛ لطفا با پشتیبانی تماس بگیرید."
    client = marzban_panel if ptype == "marzban" else pasargad_panel if ptype == "pasargad" else None
    if client is None:
        return False, None, service_id, None, "نوع پنل نامعتبر."
    ok, data, msg = await client.revoke_sub(panel, service_id)
    if not ok:
        return False, None, service_id, data, msg
    link, uname = client.extract_link_and_username(panel, data)
    return True, link, uname or service_id, data, msg


async def delete_service(panel: dict, service_id: str) -> tuple[bool, str]:
    """شاهراه API حذف مستقیم ندارد؛ برای این نوع فقط سرویس را گیر‌فعال می‌کنیم."""
    ptype = panel.get("panel_type")
    if ptype == "shahrah":
        ok, data, msg = await shahrah.disable_service(panel, service_id)
        return ok, msg
    elif ptype == "marzban":
        ok, data, msg = await marzban_panel.delete_user(panel, service_id)
    elif ptype == "pasargad":
        ok, data, msg = await pasargad_panel.delete_user(panel, service_id)
    else:
        return False, "نوع پنل نامعتبر."
    return ok, msg
