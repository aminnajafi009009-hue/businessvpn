"""
database.py
لایه‌ی کامل دسترسی به دیتابیس SQLite.
هیچ فایل دیگری در پروژه نباید مستقیماً sqlite3 را import کند؛
همه باید از طریق توابع همین فایل با دیتابیس کار کنند.
"""

import sqlite3
import threading
import secrets
import string
import os
import json
import logging
from contextlib import contextmanager
from datetime import datetime

from config import (
    DATABASE_PATH, REFERRAL_LOCK_AMOUNT, TURSO_DATABASE_URL, TURSO_AUTH_TOKEN,
    VIP_PLANS, GAMING_PLANS, FREE_TEST_PLAN_KEY, FREE_TEST_PLAN,
    SHAHRAH_BASE_URL, SHAHRAH_API_KEY, SHAHRAH_ENABLED,
    CUSTOM_BUILD_PRICE_PER_GB, CUSTOM_BUILD_PRICE_PER_30_DAYS,
    CUSTOM_BUILD_MIN_GB, CUSTOM_BUILD_MAX_GB, CUSTOM_BUILD_MIN_DAYS, CUSTOM_BUILD_MAX_DAYS,
)

_local = threading.local()
_lock = threading.Lock()  # برای جلوگیری از تداخل نوشتن همزمان

# اگر آدرس Turso تنظیم شده باشد، از دیتابیس ابری (دائمی) استفاده می‌کنیم؛
# در غیر این صورت، از فایل SQLite محلی (مناسب تست روی سیستم شخصی) استفاده می‌شود.
USE_TURSO = bool(TURSO_DATABASE_URL)


def get_connection():
    if not hasattr(_local, "conn"):
        if USE_TURSO:
            import libsql
            conn = libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
        else:
            db_dir = os.path.dirname(DATABASE_PATH)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
        _local.conn = conn
    return _local.conn


def _fetchone(cur):
    """یک سطر را از cursor می‌خواند و به دیکشنری تبدیل می‌کند.
    (به‌جای conn.row_factory چون libsql از آن پشتیبانی نمی‌کند.)"""
    row = cur.fetchone()
    if row is None:
        return None
    return {desc[0]: row[idx] for idx, desc in enumerate(cur.description)}


def _fetchall(cur):
    """تمام سطرها را از cursor می‌خواند و هرکدام را به دیکشنری تبدیل می‌کند."""
    rows = cur.fetchall()
    if not rows:
        return []
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in rows]


@contextmanager
def transaction():
    conn = get_connection()
    cur = conn.cursor()
    with _lock:
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _migrate_legacy_shahrah_panel(cur):
    """مهاجرت یک‌باره: اگر پنل شاهراه در .env تنظیم شده (SHAHRAH_ENABLED) و هنوز
    هیچ ردیفی در vpn_panels از نوع 'shahrah' نباشد، یک نمونه‌ی پیش‌فرض برایش
    می‌سازد و تمام نگاشت‌های قدیمی shahrah_plan_map را به panel_plan_map کپی
    می‌کند (بدون حذف/تغییر جدول قدیمی) تا داده‌های قبلی از دست نروند و کاربر
    بدون هیچ تنظیم دستی اضافه‌ای، دقیقاً همان رفتار قبلی را ببیند."""
    now = _now()
    cur.execute("SELECT id FROM vpn_panels WHERE panel_type = 'shahrah' LIMIT 1")
    row = _fetchone(cur)
    panel_id = row["id"] if row else None

    if panel_id is None and SHAHRAH_ENABLED and SHAHRAH_BASE_URL:
        cur.execute(
            """INSERT INTO vpn_panels
                   (panel_type, name, base_url, api_key, username, password, enabled, sort_order, created_at)
               VALUES ('shahrah', ?, ?, ?, NULL, NULL, 1, 0, ?)""",
            ("شاهراه (پیش‌فرض از .env)", SHAHRAH_BASE_URL, SHAHRAH_API_KEY, now),
        )
        panel_id = cur.lastrowid

    if panel_id is None:
        return

    cur.execute("SELECT scope, scope_id, plan_slug, plan_name FROM shahrah_plan_map")
    legacy_rows = _fetchall(cur)
    for r in legacy_rows:
        cur.execute(
            """INSERT INTO panel_plan_map (scope, scope_id, panel_id, remote_ref, remote_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(scope, scope_id) DO NOTHING""",
            (r["scope"], r["scope_id"], panel_id, r["plan_slug"], r["plan_name"], now),
        )


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id         TEXT UNIQUE NOT NULL,
            name                TEXT NOT NULL,
            wallet              INTEGER NOT NULL DEFAULT 0,
            locked_wallet       INTEGER NOT NULL DEFAULT 0,
            total_purchase      INTEGER NOT NULL DEFAULT 0,
            joined              TEXT NOT NULL,
            referrer_id         INTEGER,
            invite_code         TEXT UNIQUE NOT NULL,
            invited_count       INTEGER NOT NULL DEFAULT 0,
            successful_invites  INTEGER NOT NULL DEFAULT 0,
            is_blocked          INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (referrer_id) REFERENCES users(id)
        )
    """)

    # مهاجرت برای دیتابیس‌های قدیمی که قبل از اضافه‌شدن قابلیت «مسدودسازی
    # کاربر» ساخته شده‌اند.
    try:
        cur.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER NOT NULL DEFAULT 0")
    except Exception as exc:
        # 🐛 فیکس: روی Turso/libsql خطای ستون تکراری، sqlite3.OperationalError نیست (ممکن است ValueError/کلاس دیگری باشد)، برای همین باید هر نوع خطایی را بگیریم و فقط بر اساس متن تشخیص بدهیم.
        if "duplicate column" not in str(exc).lower():
            logging.getLogger(__name__).warning("migration users.is_blocked خطای نامنخواسته: %s", exc)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            type        TEXT NOT NULL,
            amount      INTEGER NOT NULL,
            status      TEXT NOT NULL DEFAULT 'completed',
            description TEXT,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS configs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            plan        TEXT NOT NULL,
            config      TEXT NOT NULL,
            expiry      TEXT,
            created_at  TEXT NOT NULL,
            type        TEXT NOT NULL DEFAULT 'vip',
            service_id  TEXT,
            deleted     INTEGER NOT NULL DEFAULT 0,
            qr_file_id  TEXT,
            alert_80_sent     INTEGER NOT NULL DEFAULT 0,
            alert_90_sent     INTEGER NOT NULL DEFAULT 0,
            alert_expiry_sent INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # مهاجرت ستون‌های جدید برای دیتابیس‌هایی که قبل از اضافه‌شدن این قابلیت‌ها
    # ساخته شده‌اند (اگر ستون از قبل موجود باشد فقط خطا را نادیده می‌گیریم).
    for ddl in (
        "ALTER TABLE configs ADD COLUMN type TEXT NOT NULL DEFAULT 'vip'",
        "ALTER TABLE configs ADD COLUMN service_id TEXT",
        "ALTER TABLE configs ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE configs ADD COLUMN qr_file_id TEXT",
        "ALTER TABLE configs ADD COLUMN alert_80_sent INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE configs ADD COLUMN alert_90_sent INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE configs ADD COLUMN alert_expiry_sent INTEGER NOT NULL DEFAULT 0",
        # منشأ سرویس: 'manual' (ادمین دستی فرستاده) یا نام یک نوع پنل ('shahrah'/
        # 'marzban'/'pasargad') اگر به‌صورت خودکار از یک پنل ساخته شده باشد.
        # service_id در آن حالت همان slug/username سرویس در همان پنل است (برای
        # تمدید/فعال/غیرفعال‌کردن بعدی لازم است).
        "ALTER TABLE configs ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'",
        # 🆕 چندپنلی: دقیقاً کدام «نمونه‌ی پنل» (ردیف vpn_panels) این سرویس مشخص
        # را ساخته/مدیریت می‌کند. چون حالا ممکن است چند نمونه از یک نوع پنل
        # هم‌زمان فعال باشند، تمدید/فعال/غیرفعال‌سازی باید همیشه سراغ همان
        # نمونه‌ی دقیقی برود که سرویس رویش ساخته شده، نه هر نمونه‌ای که فعلاً
        # برای آن پلن نگاشت شده (که ممکن است بعداً توسط ادمین عوض شده باشد).
        "ALTER TABLE configs ADD COLUMN panel_id INTEGER",
    ):
        try:
            cur.execute(ddl)
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                logging.getLogger(__name__).warning("migration configs.* خطای نامنخواسته: %s", exc)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS gaming_files (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            config_id   INTEGER NOT NULL,
            file_id     TEXT NOT NULL,
            file_name   TEXT,
            caption     TEXT,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (config_id) REFERENCES configs(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id           INTEGER NOT NULL,
            plan_key          TEXT,
            plan_name         TEXT NOT NULL,
            order_type        TEXT NOT NULL,
            price             INTEGER NOT NULL,
            status            TEXT NOT NULL DEFAULT 'pending',
            target_config_id  INTEGER,
            created_at        TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # پرداخت‌های آنلاین (درگاه یونیک‌پی) — هر اینوویس ساخته‌شده تا زمان تایید
    # یا انقضا اینجا ردیابی می‌شود تا هم دکمه‌ی «بررسی پرداخت» و هم پولر
    # پس‌زمینه‌ی ربات بتوانند وضعیتش را چک کنند.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS online_payments (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            hash_id           TEXT UNIQUE NOT NULL,
            user_id           INTEGER NOT NULL,
            telegram_id       TEXT NOT NULL,
            kind              TEXT NOT NULL DEFAULT 'plan',
            plan_key          TEXT,
            plan_name         TEXT NOT NULL,
            order_type        TEXT NOT NULL DEFAULT 'vip',
            price             INTEGER NOT NULL,
            discount_code     TEXT,
            payment_link      TEXT,
            ref_id            TEXT,
            status            TEXT NOT NULL DEFAULT 'pending',
            order_id          INTEGER,
            extra             TEXT,
            created_at        TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key    TEXT PRIMARY KEY,
            value  TEXT
        )
    """)

    # 🐛 فیکس: قبلاً FSM (وضعیت مکالمه‌ی چند��رحله‌ای، مثل «در حال ساخت پلن VIP
    # جدید» یا «منتظر آپلود رسید») فقط در حافظه‌ی RAM (MemoryStorage) نگه
    # داشته می‌شد. اگر پروسه‌ی ربات هر دلیلی (دیپلوی مجدد، کرش، خواب رفتن
    # سرویس رایگان و...) ری‌استارت می‌شد، همه‌ی این وضعیت‌ها گم می‌شدند و
    # کاربر/ادمین وسط یک فرآیند چندمرحله‌ای بدون هیچ پیامی گیر می‌کرد. حالا
    # این وضعیت هم مثل بقیه‌ی داده‌ها در همان دیتابیس (SQLite/Turso) پایدار
    # ذخیره می‌شود؛ به fsm_storage.py (کلاس DBStorage) نگاه کنید.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fsm_storage (
            storage_key  TEXT PRIMARY KEY,
            state        TEXT,
            data         TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS discounts (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            code                TEXT UNIQUE NOT NULL,
            percent             INTEGER NOT NULL,
            uses                INTEGER NOT NULL,
            created_at          TEXT NOT NULL,
            discount_type       TEXT NOT NULL DEFAULT 'percent',
            amount              INTEGER NOT NULL DEFAULT 0,
            applicable_plans    TEXT,
            min_order_amount    INTEGER NOT NULL DEFAULT 0,
            max_uses_per_user   INTEGER NOT NULL DEFAULT 0,
            expires_at          TEXT,
            allowed_user_ids    TEXT
        )
    """)

    # مهاجرت برای دیتابیس‌های قدیمی (اگر ستون‌ها از قبل موجود باشند، خطا نادیده گرفته می‌شود).
    for ddl in (
        "ALTER TABLE discounts ADD COLUMN discount_type TEXT NOT NULL DEFAULT 'percent'",
        "ALTER TABLE discounts ADD COLUMN amount INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE discounts ADD COLUMN applicable_plans TEXT",
        "ALTER TABLE discounts ADD COLUMN min_order_amount INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE discounts ADD COLUMN max_uses_per_user INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE discounts ADD COLUMN expires_at TEXT",
        "ALTER TABLE discounts ADD COLUMN allowed_user_ids TEXT",
    ):
        try:
            cur.execute(ddl)
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                logging.getLogger(__name__).warning("migration discounts.* خطای نامنخواسته: %s", exc)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS discount_usages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            discount_id   INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            used_at       TEXT NOT NULL,
            FOREIGN KEY (discount_id) REFERENCES discounts(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # نمایندگی: آیدی عددی نماینده + درصد تخفیفی که روی محصولات VIP می‌گیرد.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id         TEXT UNIQUE NOT NULL,
            vip_discount_percent INTEGER NOT NULL DEFAULT 50,
            note                TEXT,
            created_at          TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id   INTEGER NOT NULL,
            invited_id    INTEGER NOT NULL UNIQUE,
            reward        INTEGER NOT NULL DEFAULT 0,
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT NOT NULL,
            FOREIGN KEY (referrer_id) REFERENCES users(id),
            FOREIGN KEY (invited_id) REFERENCES users(id)
        )
    """)

    # پنل ادمین → «صف درخواست‌ها» → «رسیدهای در انتظار تایید». چون رسیدهای
    # شارژ کیف پول و خرید کارت‌به‌کارت پلن ثابت هیچ ردی در جدول‌های دیگر
    # ندارند (فقط به‌صورت پیام تلگرامی با دکمه برای ادمین فوروارد می‌شوند)،
    # این جدول یک ردِ سبک از هر رسید ارسالی نگه می‌دارد تا بشود همه را در یک
    # لیست دید. این جدول صرفاً یک «مدل نمایشی» است و منطق تایید/رد واقعی
    # (که در جدول‌های wallet/orders انجام می‌شود) به آن وابسته نیست.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_receipts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            kind          TEXT NOT NULL,
            telegram_id   TEXT NOT NULL,
            user_id       INTEGER,
            label         TEXT NOT NULL,
            amount        INTEGER NOT NULL,
            extra         TEXT,
            plan_key      TEXT,
            discount_code TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT NOT NULL
        )
    """)

    # ⏱ فاکتورهای کارت‌به‌کارت در «انتظار پرداخت» (پلن ثابت / بساز سرویس خودت /
    # شارژ کیف پول). از لحظه‌ی نمایش شماره کارت + قیمت به کاربر ساخته می‌شود و
    # اگر ظرف INVOICE_EXPIRY_MINUTES دقیقه رسیدی برای آن ثبت نشود، توسط
    # invoice_expiry_loop (bot.py) / پس‌زمینه‌ی مشابه در Mini App به‌طور خودکار
    # منقضی و از دیتابیس حذف می‌شود (و به کاربر پیام داده می‌شود). پرداخت آنلاین
    # نیاز به این جدول ندارد چون خودش created_at دارد (به expire_due_online_payments
    # نگاه کنید).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER,
            telegram_id   TEXT NOT NULL,
            kind          TEXT NOT NULL,
            label         TEXT NOT NULL,
            price         INTEGER NOT NULL,
            payload       TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT NOT NULL,
            expires_at    TEXT NOT NULL
        )
    """)

    # مهاجرت امن رسیدهای نسخه‌های قدیمی.
    for ddl in (
        "ALTER TABLE pending_receipts ADD COLUMN plan_key TEXT",
        "ALTER TABLE pending_receipts ADD COLUMN discount_code TEXT",
    ):
        try:
            cur.execute(ddl)
        except Exception as exc:
            # فقط خطای «ستون از قبل وجود دارد» مجاز به نادیده‌گرفتن است.
            if "duplicate column" not in str(exc).lower():
                raise

    # Rate limit مشترک بین تمام workerهای Mini App.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_admin_actions (
            action_key  TEXT PRIMARY KEY,
            created_at  TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_rate_limits (
            bucket_key TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_api_rate_limits_key_time ON api_rate_limits(bucket_key, created_at)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS custom_orders (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id             INTEGER NOT NULL,
            volume_gb           INTEGER NOT NULL,
            days                INTEGER NOT NULL,
            custom_name         TEXT,
            price               INTEGER NOT NULL,
            order_type          TEXT NOT NULL DEFAULT 'new',
            target_config_id    INTEGER,
            status              TEXT NOT NULL DEFAULT 'pending',
            created_at          TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # ---------------------------------------------------------------------------
    # 🗂 دسته‌بندی‌های VIP و پلن‌های داخل هرکدام (بخش «۶» — قابل مدیریت کامل از
    # پنل ادمین: افزودن دسته‌ی جدید، افزودن/ویرایش/حذف پلن داخل هر دسته).
    # ---------------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vip_categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key         TEXT UNIQUE NOT NULL,
            name        TEXT NOT NULL,
            description TEXT,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        )
    """)

    # 🆕 migration: افزودن ستون description به دیتابیس‌های قبلی که این ستون را ندارند
    # (تا قبل از این بروزرسانی این فیلد وجود نداشت).
    try:
        cur.execute("ALTER TABLE vip_categories ADD COLUMN description TEXT")
    except Exception:
        pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS vip_plans (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_key     TEXT UNIQUE NOT NULL,
            category_id  INTEGER NOT NULL,
            name         TEXT NOT NULL,
            price        INTEGER NOT NULL,
            days         INTEGER NOT NULL DEFAULT 0,
            volume_gb    INTEGER NOT NULL DEFAULT 0,
            sort_order   INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL,
            FOREIGN KEY (category_id) REFERENCES vip_categories(id)
        )
    """)

    # فقط بار اول (وقتی دیتابیس کاملاً خالی از دسته‌بندی است) پلن‌های ثابت قدیمی
    # (VIP_PLANS در config.py) را به‌عنوان اولین دسته seed می‌کنیم تا چیزی از دست نرود.
    cur.execute("SELECT COUNT(*) AS c FROM vip_categories")
    if _fetchone(cur)["c"] == 0:
        now = _now()
        cur.execute(
            "INSERT INTO vip_categories (key, name, sort_order, created_at) VALUES (?, ?, ?, ?)",
            ("speed_unlimited", "🚀 پرسرعت و کاربر نامحدود", 0, now),
        )
        default_cat_id = cur.lastrowid
        for i, (key, plan) in enumerate(VIP_PLANS.items()):
            cur.execute(
                """INSERT INTO vip_plans
                   (plan_key, category_id, name, price, days, volume_gb, sort_order, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, default_cat_id, plan["name"], plan["price"], plan.get("days", 0),
                 plan.get("volume_gb", 0), i, now),
            )

    # ---------------------------------------------------------------------------
    # 🎮 دسته‌بندی‌های Gaming و پلن‌های داخل هرکدام — دقیقاً مثل VIP، از پنل ادمین
    # کامل قابل مدیریت است (افزودن دسته/پلن جدید، ویرایش، حذف، ترتیب نمایش).
    # ---------------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gaming_categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key         TEXT UNIQUE NOT NULL,
            name        TEXT NOT NULL,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS gaming_plans (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_key     TEXT UNIQUE NOT NULL,
            category_id  INTEGER NOT NULL,
            name         TEXT NOT NULL,
            price        INTEGER NOT NULL,
            days         INTEGER NOT NULL DEFAULT 0,
            volume_gb    INTEGER NOT NULL DEFAULT 0,
            sort_order   INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL,
            FOREIGN KEY (category_id) REFERENCES gaming_categories(id)
        )
    """)

    # فقط بار اول، پلن‌های ثابت قدیمی گیمینگ (GAMING_PLANS در config.py) را
    # به‌عنوان اولین دسته seed می‌کنیم تا چیزی از دست نرود.
    cur.execute("SELECT COUNT(*) AS c FROM gaming_categories")
    if _fetchone(cur)["c"] == 0:
        now = _now()
        cur.execute(
            "INSERT INTO gaming_categories (key, name, sort_order, created_at) VALUES (?, ?, ?, ?)",
            ("gaming_default", "🌐 سرورهای Gaming (WireGuard)", 0, now),
        )
        default_gaming_cat_id = cur.lastrowid
        for i, (key, plan) in enumerate(GAMING_PLANS.items()):
            cur.execute(
                """INSERT INTO gaming_plans
                   (plan_key, category_id, name, price, days, volume_gb, sort_order, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, default_gaming_cat_id, plan["name"], plan["price"], plan.get("days", 0),
                 plan.get("volume_gb", 0), i, now),
            )

    # ---------------------------------------------------------------------------
    # 🗺️ نگاشت دسته‌بندی‌های VIP/Gaming (و «بساز سرویس خودت») به یک planSlug
    # مشخص در پنل شاهراه. این‌که آن planSlug از کدام ترافیک (مثلاً «اقتصادی
    # تانل» یا «CDN اروان») تغذیه می‌شود، در خودِ پنل شاهراه هنگام تعریف بسته
    # مشخص می‌شود؛ اینجا فقط تعیین می‌کنیم که هر دسته‌بندی/محصول ما، سراغ کدام
    # بسته‌ی (planSlug) شاهراه برود.
    # scope: 'vip_category' | 'gaming_category' | 'custom_build'
    # scope_id: شناسه‌ی دسته در جدول مربوطه (برای custom_build همیشه 0)
    # ---------------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shahrah_plan_map (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope       TEXT NOT NULL,
            scope_id    INTEGER NOT NULL,
            plan_slug   TEXT NOT NULL,
            plan_name   TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE(scope, scope_id)
        )
    """)

    # ---------------------------------------------------------------------------
    # 🆕 چندپنلی (شاهراه + مرزبان + پاسارگارد، هرکدام با چند نمونه‌ی هم‌زمان)
    # ---------------------------------------------------------------------------
    # هر ردیف یک «نمونه‌ی پنل» مشخص است (مثلاً «مرزبان-آلمان» و «مرزبان-فرانسه»
    # هر دو panel_type='marzban' ولی دو ردیف/دو نمونه‌ی مستقل با اطلاعات ورود
    # جدا هستند). چند نمونه از هر نوع می‌توانند هم‌زمان enabled=1 باشند.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vpn_panels (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            panel_type  TEXT NOT NULL,           -- 'shahrah' | 'marzban' | 'pasargad'
            name        TEXT NOT NULL,            -- برچسب دلخواه ادمین، مثلاً «مرزبان آلمان»
            base_url    TEXT NOT NULL,
            api_key     TEXT,                     -- فقط شاهراه
            username    TEXT,                     -- فقط مرزبان/پاسارگارد
            password    TEXT,                     -- فقط مرزبان/پاسارگارد
            enabled     INTEGER NOT NULL DEFAULT 1,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        )
    """)

    # نگاشتِ عمومیِ «کدام بسته/سرویس از کدام نمونه‌ی پنل استفاده کند» — جایگزین
    # نسل‌جدیدِ shahrah_plan_map که فقط شاهراه را پوشش می‌داد. scope/scope_id
    # دقیقاً همان معنایی را دارند که در shahrah_plan_map داشتند
    # ('vip_category' | 'vip_plan' | 'custom_build' | 'free_test').
    # remote_ref یعنی: برای شاهراه → planSlug، برای مرزبان/پاسارگارد → شناسه‌ی
    # عددی تمپلیت (template id) در همان نمونه‌ی پنل.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS panel_plan_map (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope       TEXT NOT NULL,
            scope_id    INTEGER NOT NULL,
            panel_id    INTEGER NOT NULL,
            remote_ref  TEXT NOT NULL,
            remote_name TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE(scope, scope_id),
            FOREIGN KEY (panel_id) REFERENCES vpn_panels(id)
        )
    """)

    _migrate_legacy_shahrah_panel(cur)

    # ---------------------------------------------------------------------------
    # 📚 راهنما و آموزش‌ها — پنل ادمین می‌تواند هر تعداد آیتم راهنما (متن/عکس/
    # ویدیو/فایل) اضافه کند؛ همه‌ی این آیتم‌ها به‌صورت خودکار به‌عنوان دکمه‌ی
    # شیشه‌ای جدید در بخش «📚 راهنما» ربات (سمت کاربر) ظاهر می‌شوند.
    # ---------------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS guides (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            title         TEXT NOT NULL,
            content_type  TEXT NOT NULL DEFAULT 'text',
            body_text     TEXT,
            file_id       TEXT,
            sort_order    INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        )
    """)

    # ---------------------------------------------------------------------------
    # 🎬 استیکر/ویدیوی تستی هر بخش از منو (🎁 تست رایگان، 🛒 خرید اشتراک،
    # 🚀 انتخاب پلن VIP/Gaming، 🛠 بساز کانفیگ خودت). هر بخش یک ردیف دارد (کلید
    # section_key)؛ اگر ردیفی وجود نداشته باشد یعنی ادمین هنوز آن را سفارشی نکرده و
    # استیکر پیش‌فرض داخل پروژه نمایش داده می‌شود.
    # ---------------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS section_stickers (
            section_key  TEXT PRIMARY KEY,
            file_id      TEXT,
            is_enabled   INTEGER NOT NULL DEFAULT 1,
            updated_at   TEXT NOT NULL
        )
    """)

    # ---------------------------------------------------------------------------
    # 🦖 لاگ خطاها (همان رویدادهایی که به Sentry هم ارسال می‌شود) — برای اینکه
    # ادمین بدون نیاز به تنظیمات/دسترسی Sentry هم بتونه از داخل ایخود پنل ا��مین
    # رب��ت آخرین خطاها رو ببیند.
    # ---------------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS error_logs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            error_type    TEXT NOT NULL,
            message       TEXT,
            traceback     TEXT,
            context       TEXT,
            occurred_at   TEXT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_error_logs_occurred ON error_logs(occurred_at)")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_guides_sort ON guides(sort_order)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_invite_code ON users(invite_code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_configs_user ON configs(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_custom_orders_user ON custom_orders(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_gaming_files_config ON gaming_files(config_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_online_payments_hash ON online_payments(hash_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_online_payments_status ON online_payments(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_receipts_status ON pending_receipts(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_vip_plans_category ON vip_plans(category_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_gaming_plans_category ON gaming_plans(category_id)")

    # 🐛 مهاجرت خودکار / self-heal: نسخه‌های قدیمی‌تر این پروژه، پلن‌های
    # پیش‌فرض VIP_PLANS/GAMING_PLANS را بدون کلید volume_gb در config.py seed
    # می‌کردند و در نتیجه مقدار volume_gb این پلن‌ها در دیتابیس صفر ذخیره شده
    # بود (باعث می‌شد پاداش دعوت هیچ‌وقت برای خرید این پلن‌ها آزاد نشود).
    # اینجا فقط همان plan_keyهای شناخته‌شده‌ای که هنوز volume_gb=0 دارند را با
    # مقدار درست از config.py هماهنگ می‌کنیم؛ پلن‌هایی که ادمین بعداً دستی
    # ساخته/ویرایش کرده دست‌نخورده می‌مانند.
    for _key, _plan in VIP_PLANS.items():
        if _plan.get("volume_gb"):
            cur.execute(
                "UPDATE vip_plans SET volume_gb = ? WHERE plan_key = ? AND volume_gb = 0",
                (_plan["volume_gb"], _key),
            )
    for _key, _plan in GAMING_PLANS.items():
        if _plan.get("volume_gb"):
            cur.execute(
                "UPDATE gaming_plans SET volume_gb = ? WHERE plan_key = ? AND volume_gb = 0",
                (_plan["volume_gb"], _key),
            )

    conn.commit()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _generate_invite_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "BVPN" + "".join(secrets.choice(alphabet) for _ in range(5))
        cur = get_connection().cursor()
        cur.execute("SELECT 1 FROM users WHERE invite_code = ?", (code,))
        if _fetchone(cur) is None:
            return code


def get_user(telegram_id) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (str(telegram_id),))
    return _fetchone(cur)


def get_user_by_invite_code(code: str) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM users WHERE invite_code = ?", (code.upper(),))
    return _fetchone(cur)


def get_user_by_id(user_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    return _fetchone(cur)


def create_user(telegram_id, name: str, referrer_invite_code: str | None = None) -> dict:
    telegram_id = str(telegram_id)
    existing = get_user(telegram_id)
    if existing:
        return existing

    referrer = None
    if referrer_invite_code:
        referrer = get_user_by_invite_code(referrer_invite_code)
        if referrer and referrer["telegram_id"] == telegram_id:
            referrer = None

    invite_code = _generate_invite_code()

    with transaction() as cur:
        cur.execute(
            """INSERT INTO users (telegram_id, name, wallet, locked_wallet,
                                   total_purchase, joined, referrer_id, invite_code,
                                   invited_count, successful_invites)
               VALUES (?, ?, 0, 0, 0, ?, ?, ?, 0, 0)""",
            (telegram_id, name, _now(), referrer["id"] if referrer else None, invite_code),
        )
        new_user_id = cur.lastrowid

        if referrer:
            cur.execute(
                """INSERT INTO referrals (referrer_id, invited_id, reward, status, created_at)
                   VALUES (?, ?, ?, 'pending', ?)""",
                (referrer["id"], new_user_id, REFERRAL_LOCK_AMOUNT, _now()),
            )
            cur.execute(
                "UPDATE users SET invited_count = invited_count + 1 WHERE id = ?",
                (referrer["id"],),
            )
            cur.execute(
                "UPDATE users SET locked_wallet = locked_wallet + ? WHERE id = ?",
                (REFERRAL_LOCK_AMOUNT, referrer["id"]),
            )
            cur.execute(
                """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
                   VALUES (?, 'referral_locked', ?, 'pending', ?, ?)""",
                (referrer["id"], REFERRAL_LOCK_AMOUNT, "پاداش دعوت (در انتظار خرید واجد شرط)", _now()),
            )

    return get_user(telegram_id)


def update_user_name(telegram_id, name: str):
    with transaction() as cur:
        cur.execute("UPDATE users SET name = ? WHERE telegram_id = ?", (name, str(telegram_id)))


def get_all_users(limit: int | None = None) -> list[dict]:
    cur = get_connection().cursor()
    if limit:
        cur.execute("SELECT * FROM users ORDER BY id DESC LIMIT ?", (limit,))
    else:
        cur.execute("SELECT * FROM users ORDER BY id DESC")
    return _fetchall(cur)


def count_users() -> int:
    cur = get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users")
    return _fetchone(cur)["c"]


def count_active_users(days: int = 30) -> int:
    cur = get_connection().cursor()
    cur.execute(
        """SELECT COUNT(DISTINCT user_id) AS c FROM transactions
           WHERE created_at >= datetime('now', ?)""",
        (f"-{days} days",),
    )
    return _fetchone(cur)["c"]


def count_customers() -> int:
    """تعداد کاربرانی که حداقل یک خرید موفق داشته‌اند (مشتریان واقعی)."""
    cur = get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE total_purchase > 0")
    return _fetchone(cur)["c"]


def get_customers(limit: int = 30) -> list[dict]:
    """کاربرانی که حداقل یک خرید موفق داشته‌اند، مرتب‌شده بر اساس بیشترین خرید."""
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM users WHERE total_purchase > 0 ORDER BY total_purchase DESC LIMIT ?",
        (limit,),
    )
    return _fetchall(cur)


def get_customers_page(page: int = 0, per_page: int = 10) -> list[dict]:
    """صفحه‌ی مشخصی از مشتریانی که خرید داشته‌اند، مرتب‌شده بر اساس بیشترین خرید."""
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM users WHERE total_purchase > 0 ORDER BY total_purchase DESC LIMIT ? OFFSET ?",
        (per_page, page * per_page),
    )
    return _fetchall(cur)


def get_all_users_page(page: int = 0, per_page: int = 10) -> list[dict]:
    """صفحه‌ی مشخصی از همه‌ی کاربران، مرتب‌شده بر اساس بیشترین خرید."""
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM users ORDER BY total_purchase DESC, id DESC LIMIT ? OFFSET ?",
        (per_page, page * per_page),
    )
    return _fetchall(cur)


def get_transactions_page(user_id: int, page: int = 0, per_page: int = 10) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        (user_id, per_page, page * per_page),
    )
    return _fetchall(cur)


def add_to_wallet(user_id: int, amount: int, description: str, tx_type: str = "charge"):
    with transaction() as cur:
        cur.execute("UPDATE users SET wallet = wallet + ? WHERE id = ?", (amount, user_id))
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, ?, ?, 'completed', ?, ?)""",
            (user_id, tx_type, amount, description, _now()),
        )


def add_to_locked_wallet(user_id: int, amount: int, description: str):
    with transaction() as cur:
        cur.execute("UPDATE users SET locked_wallet = locked_wallet + ? WHERE id = ?", (amount, user_id))
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'referral_pending', ?, 'pending', ?, ?)""",
            (user_id, amount, description, _now()),
        )


def release_locked_wallet(user_id: int, amount: int, description: str = "آزادسازی پاداش دعوت"):
    """🐛 همان فیکس ریس‌کاندیشن deduct_from_wallet، اینجا برای locked_wallet."""
    with transaction() as cur:
        cur.execute(
            "UPDATE users SET locked_wallet = locked_wallet - ?, wallet = wallet + ? "
            "WHERE id = ? AND locked_wallet >= ?",
            (amount, amount, user_id, amount),
        )
        if (cur.rowcount or 0) == 0:
            raise ValueError("موجودی در انتظار کافی نیست.")
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'referral_release', ?, 'completed', ?, ?)""",
            (user_id, amount, description, _now()),
        )


def deduct_from_wallet(user_id: int, amount: int, description: str) -> bool:
    """🐛 فیکس ریس‌کاندیشن: نسخه‌ی قبلی ابتدا موجودی را با SELECT می‌خواند،
    در پایتون چک می‌کرد، و بعد UPDATE می‌زد. قفل داخلی این ماژول (_lock) فقط
    داخل یک پروسه اثر دارد؛ چون ربات (bot.py) و Mini App API (webapp_api.py)
    دو پروسه‌ی جدا هستند و هر دو روی همان دیتابیس کیف‌پول کم می‌کنند، دو
    خرید هم‌زمان (یکی از ربات، یکی از مینی‌اپ) می‌توانستند هر دو موجودی کافی
    را ببینند و هر دو کسر انجام شود (برداشت بیش از موجودی/overdraft).
    حالا چک و کسر در یک UPDATE شرطی اتمیک انجام می‌شود؛ خود SQLite تضمین
    می‌کند این عملیات به‌صورت غیرقابل‌تقسیم اجرا شود، حتی از پروسه‌های جدا."""
    with transaction() as cur:
        cur.execute(
            "UPDATE users SET wallet = wallet - ?, total_purchase = total_purchase + ? "
            "WHERE id = ? AND wallet >= ?",
            (amount, amount, user_id, amount),
        )
        if (cur.rowcount or 0) == 0:
            return False
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'purchase', ?, 'completed', ?, ?)""",
            (user_id, amount, description, _now()),
        )
    return True


def record_purchase(user_id: int, amount: int, description: str):
    """ثبت یک خرید موفق بدون کسر از کیف پول (برای پرداخت کارت‌به‌کارت)."""
    with transaction() as cur:
        cur.execute(
            "UPDATE users SET total_purchase = total_purchase + ? WHERE id = ?",
            (amount, user_id),
        )
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'purchase', ?, 'completed', ?, ?)""",
            (user_id, amount, description, _now()),
        )


def get_transactions(user_id: int, limit: int = 10) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    return _fetchall(cur)


def total_sales() -> int:
    cur = get_connection().cursor()
    cur.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM transactions WHERE type = 'purchase'")
    return _fetchone(cur)["s"]


def sales_since(days: int) -> int:
    cur = get_connection().cursor()
    cur.execute(
        """SELECT COALESCE(SUM(amount), 0) AS s FROM transactions
           WHERE type = 'purchase' AND created_at >= datetime('now', ?)""",
        (f"-{days} days",),
    )
    return _fetchone(cur)["s"]


def add_config(
    user_id: int,
    plan_name: str,
    encrypted_config: str,
    expiry: str | None,
    config_type: str = "vip",
    service_id: str | None = None,
    qr_file_id: str | None = None,
    source: str = "manual",
    panel_id: int | None = None,
):
    with transaction() as cur:
        cur.execute(
            """INSERT INTO configs (user_id, plan, config, expiry, created_at, type, service_id, qr_file_id, source, panel_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, plan_name, encrypted_config, expiry, _now(), config_type, service_id, qr_file_id, source, panel_id),
        )
        return cur.lastrowid


def update_config(
    config_id: int,
    plan_name: str,
    encrypted_config: str,
    expiry: str | None,
    service_id: str | None = None,
    qr_file_id: str | None = None,
    panel_id: int | None = None,
):
    """آپدیت یک سرویس موجود (برای تمدید سرویس)؛ آلارم‌های حجم/انقضا هم ریست می‌شوند."""
    with transaction() as cur:
        if panel_id is not None:
            cur.execute(
                """UPDATE configs SET plan = ?, config = ?, expiry = ?, created_at = ?, service_id = ?,
                   qr_file_id = COALESCE(?, qr_file_id), panel_id = ?,
                   deleted = 0, alert_80_sent = 0, alert_90_sent = 0, alert_expiry_sent = 0 WHERE id = ?""",
                (plan_name, encrypted_config, expiry, _now(), service_id, qr_file_id, panel_id, config_id),
            )
        elif qr_file_id is not None:
            cur.execute(
                """UPDATE configs SET plan = ?, config = ?, expiry = ?, created_at = ?, service_id = ?,
                   qr_file_id = ?, deleted = 0, alert_80_sent = 0, alert_90_sent = 0, alert_expiry_sent = 0 WHERE id = ?""",
                (plan_name, encrypted_config, expiry, _now(), service_id, qr_file_id, config_id),
            )
        elif service_id is not None:
            cur.execute(
                """UPDATE configs SET plan = ?, config = ?, expiry = ?, created_at = ?, service_id = ?,
                   deleted = 0, alert_80_sent = 0, alert_90_sent = 0, alert_expiry_sent = 0 WHERE id = ?""",
                (plan_name, encrypted_config, expiry, _now(), service_id, config_id),
            )
        else:
            cur.execute(
                """UPDATE configs SET plan = ?, config = ?, expiry = ?, created_at = ?,
                   deleted = 0, alert_80_sent = 0, alert_90_sent = 0, alert_expiry_sent = 0 WHERE id = ?""",
                (plan_name, encrypted_config, expiry, _now(), config_id),
            )


def update_config_link(config_id: int, encrypted_config: str):
    """فقط لینک ساب یک سرویس را عوض می‌کند (برای ادیت دستی توسط ادمین)."""
    with transaction() as cur:
        cur.execute(
            """UPDATE configs SET config = ?, alert_80_sent = 0, alert_90_sent = 0,
               alert_expiry_sent = 0 WHERE id = ?""",
            (encrypted_config, config_id),
        )


def set_config_qr(config_id: int, qr_file_id: str):
    with transaction() as cur:
        cur.execute("UPDATE configs SET qr_file_id = ? WHERE id = ?", (qr_file_id, config_id))


def set_config_deleted(config_id: int, deleted: bool):
    with transaction() as cur:
        cur.execute("UPDATE configs SET deleted = ? WHERE id = ?", (1 if deleted else 0, config_id))


def archive_expired_configs() -> int:
    """کانفیگ‌های منقضی‌شده (expiry < today) را به‌صورت خودکار آرشیو می‌کند (deleted=1).
    تعداد کانفیگ‌های آرشیو‌شده را برمی‌گرداند. (تاریخ امروز بر اساس ساعت تهران محاسبه می‌شود.)"""
    from utils import now_tehran
    today_str = now_tehran().strftime("%Y-%m-%d")
    with transaction() as cur:
        cur.execute(
            """UPDATE configs SET deleted = 1
               WHERE deleted = 0 AND expiry IS NOT NULL AND expiry != ''
               AND substr(expiry, 1, 10) < ?""",
            (today_str,),
        )
        return cur.rowcount


def delete_config_permanently(config_id: int):
    with transaction() as cur:
        cur.execute("DELETE FROM gaming_files WHERE config_id = ?", (config_id,))
        cur.execute("DELETE FROM configs WHERE id = ?", (config_id,))


def set_config_alert_sent(config_id: int, field: str):
    if field not in ("alert_80_sent", "alert_90_sent", "alert_expiry_sent"):
        return
    with transaction() as cur:
        cur.execute(f"UPDATE configs SET {field} = 1 WHERE id = ?", (config_id,))


def get_active_vip_configs() -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM configs WHERE type = 'vip' AND deleted = 0")
    return _fetchall(cur)


def add_gaming_file(config_id: int, file_id: str, file_name: str | None, caption: str | None):
    with transaction() as cur:
        cur.execute(
            """INSERT INTO gaming_files (config_id, file_id, file_name, caption, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (config_id, file_id, file_name, caption, _now()),
        )
        return cur.lastrowid


def delete_gaming_file(gaming_file_id: int):
    with transaction() as cur:
        cur.execute("DELETE FROM gaming_files WHERE id = ?", (gaming_file_id,))


def get_gaming_files(config_id: int) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM gaming_files WHERE config_id = ? ORDER BY id ASC", (config_id,))
    return _fetchall(cur)


# ---------------------------------------------------------------------------
# صف سفارشات (پیگیری خریدهای تأییدشده‌ای که هنوز کانفیگ‌شان ارسال نشده)
# ---------------------------------------------------------------------------
def create_order(user_id: int, plan_key: str | None, plan_name: str, order_type: str, price: int) -> int:
    with transaction() as cur:
        cur.execute(
            """INSERT INTO orders (user_id, plan_key, plan_name, order_type, price, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (user_id, plan_key, plan_name, order_type, price, _now()),
        )
        return cur.lastrowid


def set_order_status(order_id: int, status: str):
    with transaction() as cur:
        cur.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))


def get_order(order_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    return _fetchone(cur)


def get_pending_orders(limit: int = 30) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM orders WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,))
    return _fetchall(cur)


# ---------------------------------------------------------------------------
# پرداخت آنلاین (درگاه یونیک‌پی — UniquePay)
# ---------------------------------------------------------------------------
def create_online_payment(
    user_id: int,
    telegram_id: str,
    hash_id: str,
    plan_name: str,
    price: int,
    order_type: str = "vip",
    plan_key: str | None = None,
    discount_code: str | None = None,
    payment_link: str | None = None,
    ref_id: str | None = None,
    kind: str = "plan",
    extra: str | None = None,
) -> int:
    with transaction() as cur:
        cur.execute(
            """INSERT INTO online_payments
               (hash_id, user_id, telegram_id, kind, plan_key, plan_name, order_type,
                price, discount_code, payment_link, ref_id, status, extra, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (hash_id, user_id, str(telegram_id), kind, plan_key, plan_name, order_type,
             price, discount_code, payment_link, ref_id, extra, _now()),
        )
        return cur.lastrowid


def get_online_payment_by_hash(hash_id: str) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM online_payments WHERE hash_id = ?", (hash_id,))
    return _fetchone(cur)


def get_online_payment(payment_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM online_payments WHERE id = ?", (payment_id,))
    return _fetchone(cur)


def _consume_discount_in_transaction(cur, code: str | None, user_id: int) -> bool:
    """نسخه‌ی داخلی use_discount برای استفاده داخل تراکنش‌های مالی بزرگ‌تر."""
    if not code:
        return False
    cur.execute("SELECT * FROM discounts WHERE code = ?", (code.upper(),))
    row = _fetchone(cur)
    if row is None or row["uses"] <= 0:
        return False
    max_per_user = int(row.get("max_uses_per_user") or 0)
    if max_per_user > 0:
        cur.execute(
            "SELECT COUNT(*) AS c FROM discount_usages WHERE discount_id = ? AND user_id = ?",
            (row["id"], user_id),
        )
        if _fetchone(cur)["c"] >= max_per_user:
            return False
    cur.execute("UPDATE discounts SET uses = uses - 1 WHERE id = ? AND uses > 0", (row["id"],))
    if (cur.rowcount or 0) == 0:
        return False
    cur.execute(
        "INSERT INTO discount_usages (discount_id, user_id, used_at) VALUES (?, ?, ?)",
        (row["id"], user_id, _now()),
    )
    return True


def finalize_online_plan_payment_atomic(payment_id: int) -> tuple[int | None, bool]:
    """Claim، ساخت سفارش، مصرف تخفیف و paid کردن پرداخت در یک تراکنش.
    خروجی (order_id, created_now) است."""
    with transaction() as cur:
        cur.execute("SELECT * FROM online_payments WHERE id = ?", (payment_id,))
        payment = _fetchone(cur)
        if payment is None:
            return None, False
        if payment["status"] == "paid":
            return payment.get("order_id"), False
        cur.execute(
            "UPDATE online_payments SET status = 'processing' WHERE id = ? AND status = 'pending'",
            (payment_id,),
        )
        if (cur.rowcount or 0) == 0:
            return None, False
        cur.execute(
            """INSERT INTO orders (user_id, plan_key, plan_name, order_type, price, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (payment["user_id"], payment["plan_key"], payment["plan_name"],
             payment["order_type"], payment["price"], _now()),
        )
        order_id = cur.lastrowid
        _consume_discount_in_transaction(cur, payment.get("discount_code"), payment["user_id"])
        cur.execute(
            "UPDATE online_payments SET status = 'paid', order_id = ? WHERE id = ? AND status = 'processing'",
            (order_id, payment_id),
        )
        if (cur.rowcount or 0) == 0:
            raise RuntimeError("finalize online plan lost payment claim")
        return order_id, True


def finalize_online_custom_payment_atomic(payment_id: int) -> tuple[int | None, bool]:
    with transaction() as cur:
        cur.execute("SELECT * FROM online_payments WHERE id = ?", (payment_id,))
        payment = _fetchone(cur)
        if payment is None:
            return None, False
        if payment["status"] == "paid":
            return payment.get("order_id"), False
        cur.execute(
            "UPDATE online_payments SET status = 'processing' WHERE id = ? AND status = 'pending'",
            (payment_id,),
        )
        if (cur.rowcount or 0) == 0:
            return None, False
        extra = json.loads(payment.get("extra") or "{}")
        volume, days = int(extra["volume"]), int(extra["days"])
        cur.execute(
            """INSERT INTO custom_orders
               (user_id, volume_gb, days, custom_name, price, order_type, target_config_id, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'paid', ?)""",
            (payment["user_id"], volume, days, extra.get("custom_name"), payment["price"],
             extra.get("order_type", "new"), extra.get("target_config_id"), _now()),
        )
        order_id = cur.lastrowid
        cur.execute(
            "UPDATE online_payments SET status = 'paid', order_id = ? WHERE id = ? AND status = 'processing'",
            (order_id, payment_id),
        )
        if (cur.rowcount or 0) == 0:
            raise RuntimeError("finalize online custom lost payment claim")
        return order_id, True


def finalize_online_wallet_payment_atomic(payment_id: int) -> tuple[int | None, bool]:
    with transaction() as cur:
        cur.execute("SELECT * FROM online_payments WHERE id = ?", (payment_id,))
        payment = _fetchone(cur)
        if payment is None:
            return None, False
        if payment["status"] == "paid":
            return payment_id, False
        cur.execute(
            "UPDATE online_payments SET status = 'processing' WHERE id = ? AND status = 'pending'",
            (payment_id,),
        )
        if (cur.rowcount or 0) == 0:
            return None, False
        if int(payment["price"]) <= 0:
            raise ValueError("invalid wallet charge amount")
        cur.execute("UPDATE users SET wallet = wallet + ? WHERE id = ?", (payment["price"], payment["user_id"]))
        if (cur.rowcount or 0) == 0:
            raise ValueError("wallet charge user not found")
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'charge', ?, 'completed', ?, ?)""",
            (payment["user_id"], payment["price"], "شارژ کیف پول (پرداخت آنلاین)", _now()),
        )
        cur.execute(
            "UPDATE online_payments SET status = 'paid', order_id = NULL WHERE id = ? AND status = 'processing'",
            (payment_id,),
        )
        if (cur.rowcount or 0) == 0:
            raise RuntimeError("finalize wallet charge lost payment claim")
        return payment_id, True


def recover_stuck_online_payments() -> int:
    """بازیابی داده‌های processing باقی‌مانده از نسخه‌های قدیمی/کرش قبلی."""
    with transaction() as cur:
        cur.execute("UPDATE online_payments SET status = 'pending' WHERE status = 'processing'")
        return cur.rowcount or 0


def claim_online_payment_for_finalize(payment_id: int) -> bool:
    """🐛 فیکس ریس‌کاندیشن: قبلاً finalize_online_payment فقط با یک شرط ساده
    در پایتون (status == 'paid' and order_id) چک می‌کرد که آیا قبلاً پردازش
    شده یا نه؛ چون این تابع هم از پولر پس‌زمینه‌ی ربات (bot.py) و هم از
    endpoint وضعیت پرداخت Mini App (webapp_api.py، در یک پروسه‌ی کاملاً جدا)
    صدا زده می‌شود، دو فراخوانی هم‌زمان می‌توانستند هر دو تشخیص «هنوز پردازش
    نشده» بدهند و هر دو یک سفارش/سرویس جداگانه برای همون یک پرداخت بسازند.

    این تابع با یک UPDATE شرطی اتمیک (status='pending' → 'processing')
    تضمین می‌کند که از بین چند فراخوانی هم‌زمان، فقط دقیقاً یکی برنده شود؛
    فقط همان فراخوانی باید ادامه‌ی مسیر (ساخت سفارش/ارسال کانفیگ) را انجام
    دهد. بقیه باید False دریافت کنند و کاری نکنند."""
    with transaction() as cur:
        cur.execute(
            "UPDATE online_payments SET status = 'processing' WHERE id = ? AND status = 'pending'",
            (payment_id,),
        )
        claimed = (cur.rowcount or 0) > 0
    return claimed


def get_pending_online_payments(limit: int = 50) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM online_payments WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,)
    )
    return _fetchall(cur)


def mark_online_payment_paid(payment_id: int, order_id: int | None):
    with transaction() as cur:
        cur.execute(
            "UPDATE online_payments SET status = 'paid', order_id = ? WHERE id = ?",
            (order_id, payment_id),
        )


def set_online_payment_status(payment_id: int, status: str):
    with transaction() as cur:
        cur.execute("UPDATE online_payments SET status = ? WHERE id = ?", (status, payment_id))


# ---------------------------------------------------------------------------
# ⏱ فاکتورهای مهلت‌دار (30 دقیقه‌ای) برای پرداخت کارت‌به‌کارت
# (پلن/بساز-سرویس/شارژ کیف‌پول)‌‌ — هم در ربات (FSM state) و هم در Mini App
# (که بین دو درخواست بی‌حالت است و نمی‌تواند به FSM تکیه کند) استفاده می‌شود.
# پرداخت آنلاین (تابل online_payments) از همین مهلت استفاده می‌کند ولی رد جداگانه‌ای
# ندارد (به expire_due_online_payments نگاه کنید).
# ---------------------------------------------------------------------------
INVOICE_EXPIRY_MINUTES = 30


def online_payment_expires_at(created_at: str, minutes: int = INVOICE_EXPIRY_MINUTES) -> str:
    """زمان واقعی انقضای یک پرداخت آنلاین را از روی زمان ساخت (created_at) محاسبه می‌کند.
    جدول online_payments فقط created_at را دارد (نه expires_at)، پس این تابع همان
    مقداری که توسط expire_due_online_payments برای حذف واقعی استفاده می‌شود را برمی‌��رداند
    تا به مینی‌اپ (برای شمارش‌معکوس واقعی) برگردانده شود، نه created_at."""
    from datetime import timedelta
    dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    return (dt + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def create_invoice(user_id, telegram_id, kind: str, label: str, price: int, payload: dict | None = None,
                    minutes: int = INVOICE_EXPIRY_MINUTES) -> dict:
    """یک «فاکتور» جدید برای مرحله‌ی کارت‌به‌کارت (از همان لحظه‌یی که شماره‌ی
    کارت و قیمت نهایی به کاربر نمایش داده می‌شود) می‌سازد. مهلت این فاکتور
    دقیقاً "minutes" دقیقه (پیش‌فرض: 30) است. پس از این مدت، اگر هیچ رسیدی برایش
    ثبت نشود (consume_invoice صدا زده نشود)، توسط invoice_expiry_loop حذف می‌شود."""
    now = datetime.now()
    from datetime import timedelta
    created_at = now.strftime("%Y-%m-%d %H:%M:%S")
    expires_at = (now + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    with transaction() as cur:
        cur.execute(
            "INSERT INTO invoices (user_id, telegram_id, kind, label, price, payload, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (user_id, telegram_id, kind, label, price, payload_json, created_at, expires_at),
        )
        invoice_id = cur.lastrowid
    return get_invoice(invoice_id)


def get_invoice(invoice_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
    return _fetchone(cur)


def consume_invoice(invoice_id: int) -> dict | None:
    """وقتی کاربر رسید را واقعاً ارسال می‌کند صدا زده می‌شود: اگر فاکتور هنوز
    pending و منقضی نشده باشد، اتمیکاً وضعیتش را 'submitted' می‌کند (تا فرایند
    پس‌زمینه‌ی پاک‌سازی فاکتورهای منقضی دیگر آن را حذف نکند) و ردیف را برمی‌گرداند، وگرنه None (یعنی وجود ندارد یا
    از قبل منقضی/مصرف‌شده است — یعنی همین مهلت 30 دقیقه‌ای به پایان رسیده)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with transaction() as cur:
        cur.execute(
            "UPDATE invoices SET status = 'submitted' WHERE id = ? AND status = 'pending' AND expires_at > ?",
            (invoice_id, now),
        )
        claimed = (cur.rowcount or 0) > 0
    if not claimed:
        return None
    return get_invoice(invoice_id)


def delete_invoice(invoice_id: int):
    with transaction() as cur:
        cur.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))


def expire_due_invoices() -> list[dict]:
    """فاکتورهای کارت‌به‌کارت (pending) که مهلت 30 دقیقه‌ای‌شان تمام شده و هنوز هیچ
    رسیدی برایشان ثبت نشده را از دیتابیس حذف می‌کند و لیستشان را (برای اطلاع‌رسانی
    به کاربر توسط invoice_expiry_loop) برمی‌گرداند."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM invoices WHERE status = 'pending' AND expires_at <= ?", (now,))
    rows = _fetchall(cur)
    for row in rows:
        with transaction() as cur2:
            cur2.execute("DELETE FROM invoices WHERE id = ? AND status = 'pending'", (row["id"],))
    return rows


def expire_due_online_payments() -> list[dict]:
    """پرداخت‌های آنلاین pending (پلن/بساز-سرویس/شارژ کیف‌پول — همه با kind
    یکتا در همین جدول هستند) که بیش از INVOICE_EXPIRY_MINUTES دقیقه از ساختشان گذشته را
    به‌صورت اتمیک claim (فقط اگر هنوز pending باشند و توسط پولر/دکمه‌ی «بررسی کن» در
    حال finalize قرار نگرفته‌اند) حذف و لیستشان را برمی‌گرداند."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(minutes=INVOICE_EXPIRY_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM online_payments WHERE status = 'pending' AND created_at <= ?", (cutoff,))
    candidates = _fetchall(cur)
    expired = []
    for row in candidates:
        with transaction() as cur2:
            cur2.execute("DELETE FROM online_payments WHERE id = ? AND status = 'pending'", (row["id"],))
            deleted = (cur2.rowcount or 0) > 0
        if deleted:
            expired.append(row)
    return expired


# ---------------------------------------------------------------------------
# ⚙️ تنظیمات کلی (key-value) — مثل روشن/خاموش بودن بخش سفارشات
# ---------------------------------------------------------------------------
def get_setting(key: str, default: str | None = None) -> str | None:
    cur = get_connection().cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    if row is None:
        return default
    return row[0]


def set_setting(key: str, value: str):
    with transaction() as cur:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------------
# 💾 FSM پایدار (state/data مکالمه‌ی چندمرحله‌ای) — پشتیبان fsm_storage.py
# ---------------------------------------------------------------------------
def fsm_get_state(storage_key: str) -> str | None:
    cur = get_connection().cursor()
    cur.execute("SELECT state FROM fsm_storage WHERE storage_key = ?", (storage_key,))
    row = cur.fetchone()
    return row[0] if row else None


def fsm_set_state(storage_key: str, state: str | None):
    with transaction() as cur:
        if state is None:
            cur.execute(
                "INSERT INTO fsm_storage (storage_key, state, data) VALUES (?, NULL, '{}') "
                "ON CONFLICT(storage_key) DO UPDATE SET state = NULL",
                (storage_key,),
            )
        else:
            cur.execute(
                "INSERT INTO fsm_storage (storage_key, state, data) VALUES (?, ?, '{}') "
                "ON CONFLICT(storage_key) DO UPDATE SET state = excluded.state",
                (storage_key, state),
            )


def fsm_get_data(storage_key: str) -> str | None:
    cur = get_connection().cursor()
    cur.execute("SELECT data FROM fsm_storage WHERE storage_key = ?", (storage_key,))
    row = cur.fetchone()
    return row[0] if row else None


def fsm_set_data(storage_key: str, data_json: str):
    with transaction() as cur:
        cur.execute(
            "INSERT INTO fsm_storage (storage_key, state, data) VALUES (?, NULL, ?) "
            "ON CONFLICT(storage_key) DO UPDATE SET data = excluded.data",
            (storage_key, data_json),
        )


def is_orders_enabled() -> bool:
    return get_setting("orders_enabled", "1") != "0"


def set_orders_enabled(enabled: bool):
    set_setting("orders_enabled", "1" if enabled else "0")


def get_configs(user_id: int, include_deleted: bool = False) -> list[dict]:
    cur = get_connection().cursor()
    if include_deleted:
        cur.execute("SELECT * FROM configs WHERE user_id = ? ORDER BY id DESC", (user_id,))
    else:
        cur.execute(
            "SELECT * FROM configs WHERE user_id = ? AND deleted = 0 ORDER BY id DESC", (user_id,)
        )
    return _fetchall(cur)


def get_configs_by_type(user_id: int, config_type: str, include_deleted: bool = False) -> list[dict]:
    cur = get_connection().cursor()
    if include_deleted:
        cur.execute(
            "SELECT * FROM configs WHERE user_id = ? AND type = ? ORDER BY id DESC",
            (user_id, config_type),
        )
    else:
        cur.execute(
            "SELECT * FROM configs WHERE user_id = ? AND type = ? AND deleted = 0 ORDER BY id DESC",
            (user_id, config_type),
        )
    return _fetchall(cur)


def get_config_by_id(config_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM configs WHERE id = ?", (config_id,))
    return _fetchone(cur)


def get_discount(code: str) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM discounts WHERE code = ?", (code.upper(),))
    return _fetchone(cur)


def create_discount(
    code: str,
    percent: int = 0,
    uses: int = 1,
    discount_type: str = "percent",
    amount: int = 0,
    applicable_plans: list | None = None,
    min_order_amount: int = 0,
    max_uses_per_user: int = 0,
    expires_at: str | None = None,
    allowed_user_ids: list | None = None,
):
    """
    discount_type: 'percent' یا 'amount' (تخفیف درصدی یا مبلغ ثابت تومانی).
    applicable_plans: لیستی از plan_key ها که این کد رویشان اعمال می‌شود؛
                       None یا [] یعنی روی همه‌ی پلن‌ها قابل استفاده است.
    max_uses_per_user: 0 یعنی بدون محدودیت برای هر کاربر.
    expires_at: تاریخ/زمان انقضا به‌فرمت 'YYYY-MM-DD HH:MM:SS'؛ None یعنی بدون انقضا.
    allowed_user_ids: لیستی از آیدی عددی تلگرام که مجاز به استفاده از این کدند؛
                       None یا [] یعنی همه‌ی کاربران مجازند.
    """
    plans_json = json.dumps(applicable_plans) if applicable_plans else None
    users_json = json.dumps([str(u) for u in allowed_user_ids]) if allowed_user_ids else None
    with transaction() as cur:
        cur.execute(
            """INSERT INTO discounts
               (code, percent, uses, created_at, discount_type, amount,
                applicable_plans, min_order_amount, max_uses_per_user, expires_at, allowed_user_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code.upper(), percent, uses, _now(), discount_type, amount,
             plans_json, min_order_amount, max_uses_per_user, expires_at, users_json),
        )


def _discount_plans(discount: dict) -> list | None:
    raw = discount.get("applicable_plans")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def discount_plans(discount: dict) -> list | None:
    """نسخه‌ی public از _discount_plans برای استفاده‌ی بیرون از ماژول."""
    return _discount_plans(discount)


def discount_applies_to_plan(discount: dict, plan_key: str | None) -> bool:
    plans = _discount_plans(discount)
    if not plans:
        return True
    return plan_key in plans


def _discount_allowed_users(discount: dict) -> list | None:
    """لیستی از آیدی‌های عددی (به‌صورت رشته) که مجاز به استفاده‌اند؛ None یعنی بدون محدودیت."""
    raw = discount.get("allowed_user_ids")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if parsed else None
    except Exception:
        return None


def discount_allowed_for_user(discount: dict, telegram_id) -> bool:
    """بررسی می‌کند که این کد برای این آیدی عددی تلگرام خاص مجاز است یا خیر
    (اگر محدودیتی تعریف نشده باشد، برای همه مجاز است)."""
    allowed = _discount_allowed_users(discount)
    if not allowed:
        return True
    return str(telegram_id) in allowed


def discount_is_expired(discount: dict) -> bool:
    exp = discount.get("expires_at")
    if not exp:
        return False
    return _now() > exp


def user_discount_uses(discount_id: int, user_id: int) -> int:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT COUNT(*) AS c FROM discount_usages WHERE discount_id = ? AND user_id = ?",
        (discount_id, user_id),
    )
    return _fetchone(cur)["c"]


def compute_discount(discount: dict, price: int) -> int:
    """قیمت نهایی بعد از اعمال کد تخفیف (درصدی یا مبلغ ثابت) را برمی‌گرداند."""
    if discount.get("discount_type") == "amount":
        final_price = price - discount.get("amount", 0)
    else:
        final_price = int(round(price * (1 - discount.get("percent", 0) / 100)))
    return max(final_price, 0)


def use_discount(code: str, user_id: int | None = None) -> bool:
    """مصرف اتمیک کد تخفیف؛ هرگز uses را منفی نمی‌کند و محدودیت هر کاربر
    را داخل همان تراکنش کنترل می‌کند تا ربات و Mini App نتوانند هم‌زمان آن
    را دور بزنند."""
    with transaction() as cur:
        cur.execute("SELECT * FROM discounts WHERE code = ?", (code.upper(),))
        row = _fetchone(cur)
        if row is None or row["uses"] <= 0:
            return False
        if user_id is not None:
            max_per_user = int(row.get("max_uses_per_user") or 0)
            if max_per_user > 0:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM discount_usages WHERE discount_id = ? AND user_id = ?",
                    (row["id"], user_id),
                )
                if _fetchone(cur)["c"] >= max_per_user:
                    return False
        cur.execute(
            "UPDATE discounts SET uses = uses - 1 WHERE id = ? AND uses > 0",
            (row["id"],),
        )
        if (cur.rowcount or 0) == 0:
            return False
        if user_id is not None:
            cur.execute(
                "INSERT INTO discount_usages (discount_id, user_id, used_at) VALUES (?, ?, ?)",
                (row["id"], user_id, _now()),
            )
    return True


def get_all_discounts() -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM discounts ORDER BY id DESC")
    return _fetchall(cur)


def get_discount_by_id(discount_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM discounts WHERE id = ?", (discount_id,))
    return _fetchone(cur)


_DISCOUNT_EDITABLE_FIELDS = {
    "percent", "amount", "discount_type", "uses", "min_order_amount",
    "max_uses_per_user", "expires_at", "applicable_plans", "allowed_user_ids",
}


def update_discount(discount_id: int, **fields):
    """آپدیت جزئی یک کد تخفیف موجود؛ فقط کلیدهای مجاز در _DISCOUNT_EDITABLE_FIELDS پذیرفته می‌شوند.
    applicable_plans و allowed_user_ids باید لیست (یا None) باشند و اینجا خودکار به JSON تبدیل می‌شوند."""
    sets = []
    values = []
    for key, value in fields.items():
        if key not in _DISCOUNT_EDITABLE_FIELDS:
            continue
        if key in ("applicable_plans", "allowed_user_ids"):
            if key == "allowed_user_ids" and value:
                value = json.dumps([str(v) for v in value])
            else:
                value = json.dumps(value) if value else None
        sets.append(f"{key} = ?")
        values.append(value)
    if not sets:
        return
    values.append(discount_id)
    with transaction() as cur:
        cur.execute(f"UPDATE discounts SET {', '.join(sets)} WHERE id = ?", values)


def delete_discount(code: str):
    with transaction() as cur:
        cur.execute("DELETE FROM discounts WHERE code = ?", (code.upper(),))


def delete_discount_by_id(discount_id: int):
    with transaction() as cur:
        cur.execute("DELETE FROM discounts WHERE id = ?", (discount_id,))


# ---------------------------------------------------------------------------
# 🤝 نمایندگی — تخفیف ثابت (پیش‌فرض ۵۰٪) روی محصولات VIP برای آیدی‌های خاص
# ---------------------------------------------------------------------------
def add_agent(telegram_id, vip_discount_percent: int = 50, note: str | None = None):
    telegram_id = str(telegram_id)
    with transaction() as cur:
        cur.execute(
            """INSERT INTO agents (telegram_id, vip_discount_percent, note, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                    vip_discount_percent = excluded.vip_discount_percent,
                    note = excluded.note""",
            (telegram_id, vip_discount_percent, note, _now()),
        )


def get_agent(telegram_id) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM agents WHERE telegram_id = ?", (str(telegram_id),))
    return _fetchone(cur)


def remove_agent(telegram_id):
    with transaction() as cur:
        cur.execute("DELETE FROM agents WHERE telegram_id = ?", (str(telegram_id),))


def get_all_agents() -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM agents ORDER BY id DESC")
    return _fetchall(cur)


def get_referral(invited_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM referrals WHERE invited_id = ?", (invited_id,))
    return _fetchone(cur)


def complete_referral(invited_id: int):
    with transaction() as cur:
        cur.execute(
            "SELECT * FROM referrals WHERE invited_id = ? AND status = 'pending'",
            (invited_id,),
        )
        ref = _fetchone(cur)
        if ref is None:
            return

        reward = ref["reward"]

        cur.execute(
            "SELECT locked_wallet FROM users WHERE id = ?", (ref["referrer_id"],)
        )
        referrer_row = _fetchone(cur)
        if referrer_row is None or referrer_row["locked_wallet"] < reward:
            raise ValueError("موجودی قفل‌شده معرف برای آزادسازی کافی نیست.")

        cur.execute(
            "UPDATE referrals SET status = 'completed' WHERE id = ?",
            (ref["id"],),
        )
        cur.execute(
            "UPDATE users SET successful_invites = successful_invites + 1 WHERE id = ?",
            (ref["referrer_id"],),
        )
        cur.execute(
            "UPDATE users SET locked_wallet = locked_wallet - ?, wallet = wallet + ? WHERE id = ?",
            (reward, reward, ref["referrer_id"]),
        )
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'referral_release', ?, 'completed', ?, ?)""",
            (ref["referrer_id"], reward, "آزادسازی پاداش دعوت (خرید واجد شرط فرد دعوت‌شده)", _now()),
        )


def get_referral_stats(user_id: int) -> dict:
    user = get_user_by_id(user_id)
    cur = get_connection().cursor()
    cur.execute(
        "SELECT COALESCE(SUM(reward), 0) AS released FROM referrals WHERE referrer_id = ? AND status = 'completed'",
        (user_id,),
    )
    released = _fetchone(cur)["released"]
    cur.execute(
        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ? AND status = 'pending'",
        (user_id,),
    )
    pending_count = _fetchone(cur)["c"]
    return {
        "invite_code": user["invite_code"],
        "invited_count": user["invited_count"],
        "successful_invites": user["successful_invites"],
        "released_amount": released,
        "pending_count": pending_count,
    }


def create_custom_order(
    user_id: int,
    volume_gb: int,
    days: int,
    custom_name: str | None,
    price: int,
    order_type: str = "new",
    target_config_id: int | None = None,
) -> int:
    with transaction() as cur:
        cur.execute(
            """INSERT INTO custom_orders
               (user_id, volume_gb, days, custom_name, price, order_type, target_config_id, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (user_id, volume_gb, days, custom_name, price, order_type, target_config_id, _now()),
        )
        return cur.lastrowid


def get_custom_order(order_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM custom_orders WHERE id = ?", (order_id,))
    return _fetchone(cur)


def set_custom_order_status(order_id: int, status: str):
    with transaction() as cur:
        cur.execute("UPDATE custom_orders SET status = ? WHERE id = ?", (status, order_id))


def get_pending_custom_orders(limit: int = 30) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM custom_orders WHERE status = 'paid' ORDER BY id ASC LIMIT ?", (limit,))
    return _fetchall(cur)


def get_pending_custom_order_receipts(limit: int = 30) -> list[dict]:
    """سفارش‌های «بساز سرویس خودت» که با کارت‌به‌کارت ثبت شده‌اند ولی هنوز
    ادمین رسیدشان را تایید/رد نکرده (status='pending'؛ بعد از تایید 'paid'
    می‌شود و در صف سفارش‌های در انتظار ارسال ظاهر می‌شود)."""
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM custom_orders WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,))
    return _fetchall(cur)


# ---------------------------------------------------------------------------
# 🧾 رسیدهای در انتظار تایید (شارژ کیف پول + خرید کارت‌به‌کارت پلن ثابت)
# این جدول صرفاً برای نمایش یک‌جای همه‌ی رسیدهای بازبینی‌نشده در پنل ادمین
# است؛ خودِ تایید/رد از همان مسیرهای قبلی (پیام فوروارد‌شده در چت ادمین)
# انجام می‌شود. اگر resolve به هر دلیلی رد را پیدا نکند، تنها اثرش این است
# که آن رد قدیمی در همین لیست باقی می‌ماند؛ روی منطق واقعی شارژ/خرید هیچ
# اثری ندارد.
# ---------------------------------------------------------------------------
def create_pending_receipt(
    kind: str, telegram_id: str, user_id: int | None, label: str, amount: int,
    extra: str | None = None, plan_key: str | None = None,
    discount_code: str | None = None,
) -> int:
    with transaction() as cur:
        cur.execute(
            """INSERT INTO pending_receipts
               (kind, telegram_id, user_id, label, amount, extra, plan_key, discount_code, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (kind, str(telegram_id), user_id, label, amount, extra, plan_key, discount_code, _now()),
        )
        return cur.lastrowid


# fix: اگر ارسال رسید به ادمین در تلگرام شکست بخورد (مثلاً توکن مسدود/رد 403/400)،
# ردیف pending یتیم می‌ماند و تلاش بعدی کاربر ممکن است با همان ردیف قدیمی تداخل پیدا کند.
# این تابع برای همین پاکسازی اضافه شده است.
def delete_pending_receipt(receipt_id: int) -> None:
    with transaction() as cur:
        cur.execute("DELETE FROM pending_receipts WHERE id = ?", (receipt_id,))


def approve_charge_receipt_atomic(telegram_id: str, amount: int) -> bool:
    """رسید شارژ را فقط یک‌بار و در همان تراکنش شارژ می‌کند."""
    with transaction() as cur:
        cur.execute(
            """SELECT * FROM pending_receipts
               WHERE kind='charge' AND telegram_id=? AND amount=? AND status='pending'
               ORDER BY id DESC LIMIT 1""",
            (str(telegram_id), amount),
        )
        receipt = _fetchone(cur)
        if receipt is None:
            return False
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (str(telegram_id),))
        user = _fetchone(cur)
        if user is None:
            raise ValueError("user not found")
        cur.execute("UPDATE users SET wallet = wallet + ? WHERE id = ?", (amount, user["id"]))
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'charge', ?, 'completed', ?, ?)""",
            (user["id"], amount, "شارژ کیف پول (تأیید رسید)", _now()),
        )
        cur.execute("UPDATE pending_receipts SET status='resolved' WHERE id=? AND status='pending'", (receipt["id"],))
        if (cur.rowcount or 0) == 0:
            raise RuntimeError("receipt claim lost")
        return True


def approve_plan_receipt_atomic(
    telegram_id: str, plan_key: str, price: int, plan_name: str, order_type: str,
) -> int | None:
    """تأیید رسید، ثبت خرید، ساخت سفارش و مصرف تخفیف را اتمیک انجام می‌دهد."""
    with transaction() as cur:
        cur.execute(
            """SELECT * FROM pending_receipts
               WHERE kind='plan_card' AND telegram_id=? AND amount=? AND status='pending'
                 AND (plan_key=? OR (plan_key IS NULL AND extra=?))
               ORDER BY id DESC LIMIT 1""",
            (str(telegram_id), price, plan_key, plan_key),
        )
        receipt = _fetchone(cur)
        if receipt is None:
            return None
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (str(telegram_id),))
        user = _fetchone(cur)
        if user is None:
            raise ValueError("user not found")
        if plan_key == FREE_TEST_PLAN_KEY:
            cur.execute("SELECT 1 FROM orders WHERE user_id=? AND plan_key=? LIMIT 1", (user["id"], plan_key))
            if cur.fetchone() is not None:
                return None
        cur.execute("UPDATE users SET total_purchase = total_purchase + ? WHERE id = ?", (price, user["id"]))
        cur.execute(
            """INSERT INTO transactions (user_id, type, amount, status, description, created_at)
               VALUES (?, 'purchase', ?, 'completed', ?, ?)""",
            (user["id"], price, f"خرید {plan_name} (کارت به کارت)", _now()),
        )
        cur.execute(
            """INSERT INTO orders (user_id, plan_key, plan_name, order_type, price, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (user["id"], plan_key, plan_name, order_type, price, _now()),
        )
        order_id = cur.lastrowid
        _consume_discount_in_transaction(cur, receipt.get("discount_code"), user["id"])
        cur.execute("UPDATE pending_receipts SET status='resolved' WHERE id=? AND status='pending'", (receipt["id"],))
        if (cur.rowcount or 0) == 0:
            raise RuntimeError("receipt claim lost")
        return order_id


def claim_admin_action(action_key: str) -> bool:
    """محافظ دائمی (نه فقط RAM) در برابر پردازش تکراری اقدامات ادمین مثل
    تأیید/رد رسید. بر خلاف is_duplicate_action در utils.py (که فقط چند ثانیه
    در RAM همان پروسه معتبر است)، این تابع یک ردیف با کلید یکتا در دیتابیس
    ثبت می‌کند؛ بنابراین بین چند پروسه/Worker مشترک است و هرگز منقضی نمی‌شود.
    True فقط برای اولین فراخوانی با این کلید برمی‌گردد."""
    with transaction() as cur:
        try:
            cur.execute(
                "INSERT INTO processed_admin_actions (action_key, created_at) VALUES (?, ?)",
                (action_key, _now()),
            )
        except Exception as exc:
            # 🐛 فیکس: روی Turso/libsql خطای نقض قید UNIQUE هم ممکن است sqlite3.IntegrityError نباشد؛ برای همین با متن خطا تشخیص می‌دهیم.
            if "unique" not in str(exc).lower() and "constraint" not in str(exc).lower():
                raise
            return False
        return True


def find_pending_receipt(kind: str, telegram_id: str, amount: int | None = None) -> dict | None:
    """مثل resolve_pending_receipt جدیدترین رسید 'pending' منطبق را پیدا
    می‌کند، اما آن را resolved نمی‌کند؛ برای خواندن discount_code قبل از
    تصمیم‌گیری نهایی (تأیید/رد) استفاده می‌شود."""
    cur = get_connection().cursor()
    if amount is not None:
        cur.execute(
            """SELECT * FROM pending_receipts
               WHERE status = 'pending' AND kind = ? AND telegram_id = ? AND amount = ?
               ORDER BY id DESC LIMIT 1""",
            (kind, str(telegram_id), amount),
        )
    else:
        cur.execute(
            """SELECT * FROM pending_receipts
               WHERE status = 'pending' AND kind = ? AND telegram_id = ?
               ORDER BY id DESC LIMIT 1""",
            (kind, str(telegram_id)),
        )
    return _fetchone(cur)


def consume_api_rate_limit(bucket_key: str, max_calls: int, period_seconds: int) -> bool:
    """Rate limit دیتابیسی و مشترک بین workerها. True یعنی درخواست مجاز است."""
    import time as _time
    now = int(_time.time())
    cutoff = now - int(period_seconds)
    with transaction() as cur:
        cur.execute("DELETE FROM api_rate_limits WHERE created_at <= ?", (cutoff,))
        cur.execute("SELECT COUNT(*) AS c FROM api_rate_limits WHERE bucket_key=?", (bucket_key,))
        if _fetchone(cur)["c"] >= max_calls:
            return False
        cur.execute("INSERT INTO api_rate_limits(bucket_key, created_at) VALUES (?, ?)", (bucket_key, now))
        return True


def get_pending_receipts(limit: int = 30) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM pending_receipts WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,)
    )
    return _fetchall(cur)


def resolve_pending_receipt(kind: str, telegram_id: str, amount: int | None = None):
    """جدیدترین رسید 'pending' منطبق را resolved می‌کند. اگر amount داده نشود
    (مثل شارژ با مبلف دلخواه توسط ادمین)، فقط بر اساس kind+telegram_id
    جدیدترین را می‌بندد. best-effort است و نباید هیچ‌وقت صدا زدنش خطا پرتاب کند
    (تماس‌گیرنده هم آن را در try/except صدا می‌زند)."""
    cur = get_connection().cursor()
    if amount is not None:
        cur.execute(
            """SELECT id FROM pending_receipts
               WHERE status = 'pending' AND kind = ? AND telegram_id = ? AND amount = ?
               ORDER BY id DESC LIMIT 1""",
            (kind, str(telegram_id), amount),
        )
    else:
        cur.execute(
            """SELECT id FROM pending_receipts
               WHERE status = 'pending' AND kind = ? AND telegram_id = ?
               ORDER BY id DESC LIMIT 1""",
            (kind, str(telegram_id)),
        )
    row = _fetchone(cur)
    if row is None:
        return
    with transaction() as tcur:
        tcur.execute("UPDATE pending_receipts SET status = 'resolved' WHERE id = ?", (row["id"],))


def dismiss_all_pending_receipts():
    with transaction() as cur:
        cur.execute("UPDATE pending_receipts SET status = 'resolved' WHERE status = 'pending'")


# ---------------------------------------------------------------------------
# ✏️ ویرایش نام/قیمت پلن‌های VIP و Gaming از پنل ادمین
# مقادیر اصلی در config.py ثابت هستند؛ اگر ادمین چیزی را عوض کند، اینجا (در
# جدول settings، کلید 'plan_overrides') ذخیره می‌شود و روی مقدار اصلی اولویت دارد.
# ---------------------------------------------------------------------------
_PLAN_OVERRIDES_KEY = "plan_overrides"


def get_plan_overrides() -> dict:
    raw = get_setting(_PLAN_OVERRIDES_KEY, "{}")
    try:
        return json.loads(raw) or {}
    except Exception:
        return {}


def set_plan_override(plan_key: str, name: str | None = None, price: int | None = None):
    overrides = get_plan_overrides()
    entry = overrides.get(plan_key, {})
    if name is not None:
        entry["name"] = name
    if price is not None:
        entry["price"] = price
    overrides[plan_key] = entry
    set_setting(_PLAN_OVERRIDES_KEY, json.dumps(overrides, ensure_ascii=False))


def clear_plan_override(plan_key: str):
    overrides = get_plan_overrides()
    if plan_key in overrides:
        del overrides[plan_key]
        set_setting(_PLAN_OVERRIDES_KEY, json.dumps(overrides, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 🗂 دسته‌بندی‌های VIP — هرکدام می‌تواند هر تعداد پلن داشته باشد. کاملاً از پنل
# ادمین قابل مدیریت است (افزودن دسته/پلن جدید، ویرایش، حذف)، بدون نیاز به هیچ
# تغییری در کد. این جدول‌ها منبع اصلی پلن‌های VIP هستند (نه دیکشنری VIP_PLANS
# در config.py که فقط برای seed اولیه استفاده شد).
# ---------------------------------------------------------------------------
def _slugify_key(prefix: str, name: str) -> str:
    return f"{prefix}_{secrets.token_hex(3)}"


def get_vip_categories() -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM vip_categories ORDER BY sort_order ASC, id ASC")
    return _fetchall(cur)


def get_vip_category(key_or_id) -> dict | None:
    cur = get_connection().cursor()
    if isinstance(key_or_id, int) or (isinstance(key_or_id, str) and key_or_id.isdigit()):
        cur.execute("SELECT * FROM vip_categories WHERE id = ?", (int(key_or_id),))
        row = _fetchone(cur)
        if row:
            return row
    cur.execute("SELECT * FROM vip_categories WHERE key = ?", (str(key_or_id),))
    return _fetchone(cur)


def create_vip_category(name: str) -> dict:
    key = _slugify_key("cat", name)
    with transaction() as cur:
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM vip_categories")
        sort_order = _fetchone(cur)["n"]
        cur.execute(
            "INSERT INTO vip_categories (key, name, sort_order, created_at) VALUES (?, ?, ?, ?)",
            (key, name, sort_order, _now()),
        )
    return get_vip_category(key)


def rename_vip_category(category_id: int, name: str):
    with transaction() as cur:
        cur.execute("UPDATE vip_categories SET name = ? WHERE id = ?", (name, category_id))


def update_vip_category_description(category_id: int, description: str | None):
    """توضیح یک دسته‌بندی VIP را که بالای دکمه‌های شیشه‌ای پلن‌های همان دسته
    به کاربر نمایش داده می‌شود، به‌روز می‌کند. مقدار None یعنی توضیحی ثبت نشود/حذف شود."""
    with transaction() as cur:
        cur.execute("UPDATE vip_categories SET description = ? WHERE id = ?", (description, category_id))


def delete_vip_category(category_id: int) -> bool:
    """اگر دسته خالی از پلن باشد حذف می‌شود و True برمی‌گرداند؛ اگر پلن داشته باشد
    حذف نمی‌شود (باید اول پلن‌هایش حذف/منتقل شوند) و False برمی‌گردد."""
    cur = get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c FROM vip_plans WHERE category_id = ?", (category_id,))
    if _fetchone(cur)["c"] > 0:
        return False
    with transaction() as cur:
        cur.execute("DELETE FROM vip_categories WHERE id = ?", (category_id,))
    return True


def get_vip_plans(category_id: int) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM vip_plans WHERE category_id = ? ORDER BY sort_order ASC, id ASC", (category_id,)
    )
    return _fetchall(cur)


def get_vip_plan(plan_key: str) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM vip_plans WHERE plan_key = ?", (plan_key,))
    return _fetchone(cur)


def add_vip_plan(category_id: int, name: str, price: int, days: int = 0, volume_gb: int = 0) -> str:
    plan_key = _slugify_key("vip", name)
    with transaction() as cur:
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM vip_plans WHERE category_id = ?",
                    (category_id,))
        sort_order = _fetchone(cur)["n"]
        cur.execute(
            """INSERT INTO vip_plans
               (plan_key, category_id, name, price, days, volume_gb, sort_order, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (plan_key, category_id, name, price, days, volume_gb, sort_order, _now()),
        )
    return plan_key


def update_vip_plan(plan_key: str, name: str | None = None, price: int | None = None,
                     days: int | None = None, volume_gb: int | None = None):
    fields, values = [], []
    for col, val in (("name", name), ("price", price), ("days", days), ("volume_gb", volume_gb)):
        if val is not None:
            fields.append(f"{col} = ?")
            values.append(val)
    if not fields:
        return
    values.append(plan_key)
    with transaction() as cur:
        cur.execute(f"UPDATE vip_plans SET {', '.join(fields)} WHERE plan_key = ?", values)


def delete_vip_plan(plan_key: str):
    with transaction() as cur:
        cur.execute("DELETE FROM vip_plans WHERE plan_key = ?", (plan_key,))


def get_all_vip_plans_flat() -> dict:
    """همه‌ی پلن‌های VIP (از همه‌ی دسته‌ها) را به‌شکل {plan_key: plan_dict} برمی‌گرداند."""
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM vip_plans")
    return {row["plan_key"]: row for row in _fetchall(cur)}


# ---------------------------------------------------------------------------
# 🎮 دسته‌بندی‌های Gaming و پلن‌های داخل هرکدام — کاملاً مشابه VIP (بالاتر در
# همین فایل)، از پنل ادمین قابل افزودن/ویرایش/حذف/تغییر ترتیب است.
# ---------------------------------------------------------------------------
def get_gaming_categories() -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM gaming_categories ORDER BY sort_order ASC, id ASC")
    return _fetchall(cur)


def get_gaming_category(key_or_id) -> dict | None:
    cur = get_connection().cursor()
    if isinstance(key_or_id, int) or (isinstance(key_or_id, str) and key_or_id.isdigit()):
        cur.execute("SELECT * FROM gaming_categories WHERE id = ?", (int(key_or_id),))
        row = _fetchone(cur)
        if row:
            return row
    cur.execute("SELECT * FROM gaming_categories WHERE key = ?", (str(key_or_id),))
    return _fetchone(cur)


def create_gaming_category(name: str) -> dict:
    key = _slugify_key("gcat", name)
    with transaction() as cur:
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM gaming_categories")
        sort_order = _fetchone(cur)["n"]
        cur.execute(
            "INSERT INTO gaming_categories (key, name, sort_order, created_at) VALUES (?, ?, ?, ?)",
            (key, name, sort_order, _now()),
        )
    return get_gaming_category(key)


def rename_gaming_category(category_id: int, name: str):
    with transaction() as cur:
        cur.execute("UPDATE gaming_categories SET name = ? WHERE id = ?", (name, category_id))


def delete_gaming_category(category_id: int) -> bool:
    """اگر دسته خالی از پلن باشد حذف می‌شود و True برمی‌گرداند؛ اگر پلن داشته باشد
    حذف نمی‌شود (باید اول پلن‌هایش حذف/منتقل شوند) و False برمی‌گردد."""
    cur = get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c FROM gaming_plans WHERE category_id = ?", (category_id,))
    if _fetchone(cur)["c"] > 0:
        return False
    with transaction() as cur:
        cur.execute("DELETE FROM gaming_categories WHERE id = ?", (category_id,))
    return True


def get_gaming_plans(category_id: int) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM gaming_plans WHERE category_id = ? ORDER BY sort_order ASC, id ASC", (category_id,)
    )
    return _fetchall(cur)


def get_gaming_plan(plan_key: str) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM gaming_plans WHERE plan_key = ?", (plan_key,))
    return _fetchone(cur)


def add_gaming_plan(category_id: int, name: str, price: int, days: int = 0, volume_gb: int = 0) -> str:
    plan_key = _slugify_key("gaming", name)
    with transaction() as cur:
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM gaming_plans WHERE category_id = ?",
                    (category_id,))
        sort_order = _fetchone(cur)["n"]
        cur.execute(
            """INSERT INTO gaming_plans
               (plan_key, category_id, name, price, days, volume_gb, sort_order, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (plan_key, category_id, name, price, days, volume_gb, sort_order, _now()),
        )
    return plan_key


def update_gaming_plan(plan_key: str, name: str | None = None, price: int | None = None,
                        days: int | None = None, volume_gb: int | None = None):
    fields, values = [], []
    for col, val in (("name", name), ("price", price), ("days", days), ("volume_gb", volume_gb)):
        if val is not None:
            fields.append(f"{col} = ?")
            values.append(val)
    if not fields:
        return
    values.append(plan_key)
    with transaction() as cur:
        cur.execute(f"UPDATE gaming_plans SET {', '.join(fields)} WHERE plan_key = ?", values)


def delete_gaming_plan(plan_key: str):
    with transaction() as cur:
        cur.execute("DELETE FROM gaming_plans WHERE plan_key = ?", (plan_key,))


def get_all_gaming_plans_flat() -> dict:
    """همه‌ی پلن‌های Gaming (از همه‌ی دسته‌ها) را به‌شکل {plan_key: plan_dict} برمی‌گرداند."""
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM gaming_plans")
    return {row["plan_key"]: row for row in _fetchall(cur)}


# ---------------------------------------------------------------------------
# ↕️ تغییر ترتیب نمایش (بالا/پایین) — هم برای دسته‌بندی‌ها و هم پلن‌های داخل
# هرکدام، هم برای VIP و هم Gaming؛ یک تابع عمومی برای هر ۴ حالت.
# ---------------------------------------------------------------------------
def _move_row(table: str, row_id: int, direction: str, group_col: str | None = None) -> bool:
    cur = get_connection().cursor()
    group_val = None
    if group_col:
        cur.execute(f"SELECT {group_col} AS g FROM {table} WHERE id = ?", (row_id,))
        row = _fetchone(cur)
        if row is None:
            return False
        group_val = row["g"]

    if group_col:
        cur.execute(
            f"SELECT id, sort_order FROM {table} WHERE {group_col} = ? ORDER BY sort_order ASC, id ASC",
            (group_val,),
        )
    else:
        cur.execute(f"SELECT id, sort_order FROM {table} ORDER BY sort_order ASC, id ASC")
    rows = _fetchall(cur)

    idx = next((i for i, r in enumerate(rows) if r["id"] == row_id), None)
    if idx is None:
        return False

    if direction == "up":
        if idx == 0:
            return False
        other = rows[idx - 1]
    elif direction == "down":
        if idx == len(rows) - 1:
            return False
        other = rows[idx + 1]
    else:
        return False

    with transaction() as cur:
        cur.execute(f"UPDATE {table} SET sort_order = ? WHERE id = ?", (other["sort_order"], rows[idx]["id"]))
        cur.execute(f"UPDATE {table} SET sort_order = ? WHERE id = ?", (rows[idx]["sort_order"], other["id"]))
    return True


def move_vip_category(category_id: int, direction: str) -> bool:
    return _move_row("vip_categories", category_id, direction)


def move_vip_plan(plan_id: int, direction: str) -> bool:
    return _move_row("vip_plans", plan_id, direction, "category_id")


def move_gaming_category(category_id: int, direction: str) -> bool:
    return _move_row("gaming_categories", category_id, direction)


def move_gaming_plan(plan_id: int, direction: str) -> bool:
    return _move_row("gaming_plans", plan_id, direction, "category_id")


def plan_type(plan_key: str) -> str:
    """'vip' / 'gaming' / 'test' را برای یک plan_key از روی داده‌ی واقعی دیتابیس
    تشخیص می‌دهد؛ نسخه‌ی درستِ config.plan_type."""
    if plan_key == FREE_TEST_PLAN_KEY:
        return "test"
    if get_vip_plan(plan_key) is not None:
        return "vip"
    if get_gaming_plan(plan_key) is not None:
        return "gaming"
    return "vip"


def has_used_free_test(user_id: int) -> bool:
    """آیا این کاربر قبلاً (با هر روش پرداختی و در هر وضعیتی) پلن «تست رایگان»
    را دریافت کرده است؟ سفارش تست فقط زمانی در جدول orders ثبت می‌شود که
    پرداخت واقعاً انجام/تأیید شده باشد (کیف‌پول: بلافاصله پس از کسر موجودی،
    آنلاین: پس از تأیید بانک، کارت‌به‌کارت: پس از تأیید ادمین)؛ پس وجود حتی
    یک ردیف با این plan_key یعنی کاربر یک‌بار از تست رایگان استفاده کرده و
    نباید بار دیگر بتواند آن را بخرد."""
    cur = get_connection().cursor()
    cur.execute(
        "SELECT 1 FROM orders WHERE user_id = ? AND plan_key = ? LIMIT 1",
        (user_id, FREE_TEST_PLAN_KEY),
    )
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# 🎁 تنظیم حجم/مدت/قیمت پلن «تست رایگان» از پنل ادمین — مقدار پیش‌فرض همان
# FREE_TEST_PLAN در config.py است؛ اگر ادمین مقدار جدیدی تنظیم کند، اینجا (در
# جدول settings، کلید 'free_test_override') ذخیره می‌شود و روی مقدار اصلی اولویت
# دارد. مدت اعتبار هم به‌صورت ساعت و هم به‌صورت روز قابل تنظیم است (duration_unit).
# ---------------------------------------------------------------------------
_FREE_TEST_OVERRIDE_KEY = "free_test_override"


def get_free_test_override() -> dict | None:
    raw = get_setting(_FREE_TEST_OVERRIDE_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "volume_mb" in data and "duration_value" in data:
            return data
    except Exception:
        pass
    return None


def set_free_test_override(volume_mb: int, duration_value: float, duration_unit: str, price: int) -> None:
    set_setting(
        _FREE_TEST_OVERRIDE_KEY,
        json.dumps(
            {"volume_mb": volume_mb, "duration_value": duration_value, "duration_unit": duration_unit, "price": price},
            ensure_ascii=False,
        ),
    )


def get_effective_free_test_plan() -> dict:
    """پلن «تست رایگان» واقعی را برمی‌گرداند: اگر ادمین از پنل مقدار جدیدی
    (حجم/مدت/قیمت) تنظیم کرده باشد همان استفاده می‌شود، وگرنه مقدار پیش‌فرض
    FREE_TEST_PLAN در config.py استفاده می‌شود. نام پلن همیشه به‌صورت خودکار از روی
    حجم/مدت فعلی ساخته می‌شود تا با تغییر این مقادیر، متن نمایشی هم درست/به‌روز بماند.

    نکته‌ی مهم: پلن["days"] همیشه برحسب روز (ممکن است اعشاری باشد اگر واحد ساعت
    باشد) برمی‌گردد تا منطق محاسبه‌ی انقضای سرویس (panels.create_service/_expire_from_days
    که از float days هم پشتیبانی می‌کند) بدون تغییر باقی بماند."""
    plan = dict(FREE_TEST_PLAN)
    override = get_free_test_override()
    if override:
        volume_mb = override["volume_mb"]
        duration_value = override["duration_value"]
        duration_unit = override.get("duration_unit") or "days"
        days = (duration_value / 24) if duration_unit == "hours" else duration_value
        plan["days"] = days
        plan["volume_gb"] = volume_mb / 1024
        if override.get("price") is not None:
            plan["price"] = override["price"]
    else:
        days = plan.get("days", 7)
        volume_mb = round(plan.get("volume_gb", 1) * 1024)
        duration_value = days
        duration_unit = "days"

    if volume_mb < 1024:
        volume_label = f"{volume_mb} مگابایت"
    else:
        gb_value = volume_mb / 1024
        volume_label = f"{gb_value:.0f} گیگ" if gb_value == int(gb_value) else f"{gb_value:.2f} گیگ"

    if duration_unit == "hours":
        dv = int(duration_value) if float(duration_value) == int(duration_value) else duration_value
        duration_label = f"{dv} ساعته"
    else:
        dv = int(duration_value) if float(duration_value) == int(duration_value) else duration_value
        duration_label = f"{dv} روزه"

    plan["name"] = f"{volume_label} {duration_label}"
    plan["duration_value"] = duration_value
    plan["duration_unit"] = duration_unit
    return plan


# ---------------------------------------------------------------------------
# 🧩 تنظیم قیمت/محدوده‌ی «بساز سرویس خودت» از پنل ادمین — مقدار پیش‌فرض همان
# CUSTOM_BUILD_* در config.py است؛ اگر ادمین مقدار جدیدی تنظیم کند، اینجا (در
# جدول settings، کلید 'custom_build_override') ذخیره می‌شود و روی مقدار اصلی اولویت
# دارد.
# ---------------------------------------------------------------------------
_CUSTOM_BUILD_OVERRIDE_KEY = "custom_build_override"


def get_custom_build_override() -> dict | None:
    raw = get_setting(_CUSTOM_BUILD_OVERRIDE_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "price_per_gb" in data:
            return data
    except Exception:
        pass
    return None


def set_custom_build_override(
    price_per_gb: int,
    price_per_30_days: int,
    min_gb: int,
    max_gb: int,
    min_days: int,
    max_days: int,
) -> None:
    set_setting(
        _CUSTOM_BUILD_OVERRIDE_KEY,
        json.dumps(
            {
                "price_per_gb": price_per_gb,
                "price_per_30_days": price_per_30_days,
                "min_gb": min_gb,
                "max_gb": max_gb,
                "min_days": min_days,
                "max_days": max_days,
            },
            ensure_ascii=False,
        ),
    )


def get_effective_custom_build_settings() -> dict:
    """تنظیمات واقعی «بساز سرویس خودت» را برمی‌گرداند: اگر ادمین از پنل مقدار
    جدیدی تنظیم کرده باشد همان استفاده می‌شود، وگرنه مقدار پیش‌فرض
    CUSTOM_BUILD_* در config.py."""
    settings = {
        "price_per_gb": CUSTOM_BUILD_PRICE_PER_GB,
        "price_per_30_days": CUSTOM_BUILD_PRICE_PER_30_DAYS,
        "min_gb": CUSTOM_BUILD_MIN_GB,
        "max_gb": CUSTOM_BUILD_MAX_GB,
        "min_days": CUSTOM_BUILD_MIN_DAYS,
        "max_days": CUSTOM_BUILD_MAX_DAYS,
    }
    override = get_custom_build_override()
    if override:
        settings.update({k: v for k, v in override.items() if v is not None})
    return settings


def get_all_plans() -> dict:
    """همه‌ی پلن‌های واقعاً موجود (VIP + Gaming از دیتابیس + پلن تست) را در یک
    دیکشنری برمی‌گرداند. جای‌گزین PLANS ثابت در config.py."""
    result = {}
    result.update(get_all_vip_plans_flat())
    result.update(get_all_gaming_plans_flat())
    result[FREE_TEST_PLAN_KEY] = get_effective_free_test_plan()
    return result


def get_effective_plan(plan_key: str) -> dict | None:
    """نسخه‌ی نهایی/واقعی یک پلن را برمی‌گرداند: پلن تست با درنظرگرفتن تنظیمات ادمین
    (اگر موجود باشد)، پلن VIP و Gaming هر دو مستقیماً از دیتابیس (چون منبع اصلی هستند)."""
    if plan_key == FREE_TEST_PLAN_KEY:
        return get_effective_free_test_plan()

    vip_plan = get_vip_plan(plan_key)
    if vip_plan is not None:
        return dict(vip_plan)

    gaming_plan = get_gaming_plan(plan_key)
    if gaming_plan is not None:
        return dict(gaming_plan)

    return None


def get_effective_plans(base_plans: dict) -> dict:
    """base_plans (فعلاً فقط GAMING_PLANS از config.py) را با تغییرات ذخیره‌شده‌ی
    ادمین (نام/قیمت) ترکیب می‌کند، بدون این‌که خود config.py را تغییر دهد.
    (برای VIP دیگر استفاده نمی‌شود؛ VIP مستقیماً از جدول vip_plans خوانده می‌شود.)"""
    overrides = get_plan_overrides()
    result = {}
    for key, plan in base_plans.items():
        merged = dict(plan)
        if key in overrides:
            merged.update({k: v for k, v in overrides[key].items() if v is not None})
        result[key] = merged
    return result


# ---------------------------------------------------------------------------
# 🔗 نگاشت دسته‌بندی‌ها به planSlug پنل شاهراه
# ---------------------------------------------------------------------------
def set_shahrah_plan_map(scope: str, scope_id: int, plan_slug: str, plan_name: str | None = None):
    with transaction() as cur:
        cur.execute(
            """INSERT INTO shahrah_plan_map (scope, scope_id, plan_slug, plan_name, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(scope, scope_id) DO UPDATE SET
                   plan_slug = excluded.plan_slug, plan_name = excluded.plan_name""",
            (scope, scope_id, plan_slug, plan_name, _now()),
        )


def get_shahrah_plan_map(scope: str, scope_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute(
        "SELECT * FROM shahrah_plan_map WHERE scope = ? AND scope_id = ?", (scope, scope_id)
    )
    return _fetchone(cur)


def delete_shahrah_plan_map(scope: str, scope_id: int):
    with transaction() as cur:
        cur.execute("DELETE FROM shahrah_plan_map WHERE scope = ? AND scope_id = ?", (scope, scope_id))


def list_shahrah_plan_maps(scope: str | None = None) -> list[dict]:
    cur = get_connection().cursor()
    if scope:
        cur.execute("SELECT * FROM shahrah_plan_map WHERE scope = ?", (scope,))
    else:
        cur.execute("SELECT * FROM shahrah_plan_map")
    return _fetchall(cur)


def get_shahrah_plan_map_for_plan_key(plan_key: str) -> dict | None:
    """با گرفتن plan_key یک پلن VIP، اول نگاشت اختصاصیِ خودِ همین پلن (بر اساس
    id دقیق پلن، نه دسته‌بندی) را چک می‌کند — چون هر پلن ممکن است حجم/مدت
    متفاوتی داشته باشد و باید به بسته‌ی متناظرش در شاهراه وصل شود.
    اگر پلن نگاشت اختصاصی نداشت، به‌صورت fallback نگاشت سطح دسته‌بندی (رفتار
    قدیمی‌تر، برای وقتی که ادمین فقط یک نگاشت پیش‌فرض برای کل دسته گذاشته)
    برگردانده می‌شود تا نگاشت‌های قبلی از کار نیفتند.

    پلن «تست رایگان» چون در جدول vip_plans نیست (یک پلن ثابت جداگانه در
    config.py است)، یک نگاشت سراسری مستقل با scope="free_test" دارد.

    عمداً: پلن‌های Gaming اینجا اصلاً بررسی نمی‌شوند. طبق تصمیم صریح، بخش
    Gaming کاملاً جدا نگه داشته می‌شود و هیچ‌وقت به پنل شاهراه وصل نمی‌شود؛
    ارسال کانفیگ گیمینگ همیشه ۱۰۰٪ دستی باقی می‌ماند."""
    if plan_key == FREE_TEST_PLAN_KEY:
        return get_shahrah_plan_map("free_test", 0)

    vip_plan = get_vip_plan(plan_key)
    if vip_plan is not None:
        plan_map = get_shahrah_plan_map("vip_plan", vip_plan["id"])
        if plan_map:
            return plan_map
        return get_shahrah_plan_map("vip_category", vip_plan["category_id"])

    return None


# ---------------------------------------------------------------------------
# 🆕 چندپنلی: مدیریت نمونه‌های پنل (شاهراه/مرزبان/پاسارگارد) + نگاشت عمومی
# ---------------------------------------------------------------------------
def create_vpn_panel(panel_type: str, name: str, base_url: str, api_key: str | None = None,
                      username: str | None = None, password: str | None = None) -> int:
    with transaction() as cur:
        cur.execute(
            """INSERT INTO vpn_panels (panel_type, name, base_url, api_key, username, password, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            (panel_type, name, base_url, api_key, username, password, _now()),
        )
        return cur.lastrowid


def get_vpn_panel(panel_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM vpn_panels WHERE id = ?", (panel_id,))
    return _fetchone(cur)


def list_vpn_panels(panel_type: str | None = None, enabled_only: bool = False) -> list[dict]:
    cur = get_connection().cursor()
    query = "SELECT * FROM vpn_panels"
    conditions = []
    params: list = []
    if panel_type:
        conditions.append("panel_type = ?")
        params.append(panel_type)
    if enabled_only:
        conditions.append("enabled = 1")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY sort_order ASC, id ASC"
    cur.execute(query, params)
    return _fetchall(cur)


def update_vpn_panel(panel_id: int, name: str | None = None, base_url: str | None = None,
                      api_key: str | None = None, username: str | None = None,
                      password: str | None = None, enabled: bool | None = None) -> None:
    """فقط فیلدهای غیرNone آپدیت می‌شوند (برای پاک کردن رشته‌ای مثل رمز عبور، از یک رشته‌‌
    خالی جداگانه مثل clear_password=True استفاده نمی‌شود، فقط مقدار جدید می‌فرستیم)."""
    fields = []
    params: list = []
    if name is not None:
        fields.append("name = ?")
        params.append(name)
    if base_url is not None:
        fields.append("base_url = ?")
        params.append(base_url)
    if api_key is not None:
        fields.append("api_key = ?")
        params.append(api_key)
    if username is not None:
        fields.append("username = ?")
        params.append(username)
    if password is not None:
        fields.append("password = ?")
        params.append(password)
    if enabled is not None:
        fields.append("enabled = ?")
        params.append(1 if enabled else 0)
    if not fields:
        return
    params.append(panel_id)
    with transaction() as cur:
        cur.execute(f"UPDATE vpn_panels SET {', '.join(fields)} WHERE id = ?", params)


def delete_vpn_panel(panel_id: int) -> None:
    with transaction() as cur:
        cur.execute("DELETE FROM panel_plan_map WHERE panel_id = ?", (panel_id,))
        cur.execute("DELETE FROM vpn_panels WHERE id = ?", (panel_id,))


# ---------------------------------------------------------------------------
# 🆕 نگاشت عمومی پلن/بسته → نمونه‌ی پنل (هر سه نوع پنل)
# ---------------------------------------------------------------------------
def set_panel_plan_map(scope: str, scope_id: int, panel_id: int, remote_ref: str, remote_name: str | None = None):
    with transaction() as cur:
        cur.execute(
            """INSERT INTO panel_plan_map (scope, scope_id, panel_id, remote_ref, remote_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(scope, scope_id) DO UPDATE SET
                   panel_id = excluded.panel_id, remote_ref = excluded.remote_ref, remote_name = excluded.remote_name""",
            (scope, scope_id, panel_id, remote_ref, remote_name, _now()),
        )


def get_panel_plan_map(scope: str, scope_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM panel_plan_map WHERE scope = ? AND scope_id = ?", (scope, scope_id))
    return _fetchone(cur)


def get_panel_plan_map_with_panel(scope: str, scope_id: int) -> dict | None:
    """مانند get_panel_plan_map ولی اطلاعات نمونه‌ی پنل (نوع/آدرس/اطلاعات ورود) را هم
    برمی‌گرداند تا لایه ارتباط با پنل بتواند مستقیم درخواست بزند."""
    cur = get_connection().cursor()
    cur.execute(
        """SELECT m.scope, m.scope_id, m.remote_ref, m.remote_name, m.panel_id,
                  p.panel_type, p.name AS panel_name, p.base_url, p.api_key, p.username, p.password, p.enabled
           FROM panel_plan_map m JOIN vpn_panels p ON p.id = m.panel_id
           WHERE m.scope = ? AND m.scope_id = ?""",
        (scope, scope_id),
    )
    return _fetchone(cur)


def delete_panel_plan_map(scope: str, scope_id: int):
    with transaction() as cur:
        cur.execute("DELETE FROM panel_plan_map WHERE scope = ? AND scope_id = ?", (scope, scope_id))


# fix: وقتی ادمین یک نگاشت پیش‌فرض برای کل یک دسته‌بندی VIP تنظیم می‌کند
# (scope='vip_category')، باید نگاشت‌های اختصاصیِ قدیمی‌ترِ تک‌تک پلن‌های همان
# دسته (scope='vip_plan') هم پاک شوند. چون در get_panel_map_for_plan_key نگاشت
# اختصاصی پلن همیشه اولویت دارد، بدون این پاک‌سازی، تنظیم پیش‌فرض جدید دسته هیچ
# اثری روی پلن‌هایی که از قبل نگاشت اختصاصی داشتند نمی‌گذاشت (مثلاً هنوز از
# پنل شاهراه‌ی قدیمی استفاده می‌شد با اینکه پیش‌فرض دسته به پاسارگارد تغییر کرده بود).
def clear_panel_plan_overrides_for_category(category_id: int):
    plan_ids = [p["id"] for p in get_vip_plans(category_id)]
    if not plan_ids:
        return
    placeholders = ",".join("?" for _ in plan_ids)
    with transaction() as cur:
        cur.execute(
            f"DELETE FROM panel_plan_map WHERE scope = 'vip_plan' AND scope_id IN ({placeholders})",
            plan_ids,
        )


def list_panel_plan_maps(scope: str | None = None) -> list[dict]:
    cur = get_connection().cursor()
    if scope:
        cur.execute("SELECT * FROM panel_plan_map WHERE scope = ?", (scope,))
    else:
        cur.execute("SELECT * FROM panel_plan_map")
    return _fetchall(cur)


def get_panel_map_for_plan_key(plan_key: str) -> dict | None:
    """نسخه‌ی عمومی‌شده‌ی get_shahrah_plan_map_for_plan_key که هر سه نوع پنل را پوشش
    می‌دهد و نتیجه را همراه با اطلاعات کامل همان نمونه‌ی پنل (برای اتصال/ساخت) برمی‌گرداند."""
    if plan_key == FREE_TEST_PLAN_KEY:
        return get_panel_plan_map_with_panel("free_test", 0)

    vip_plan = get_vip_plan(plan_key)
    if vip_plan is not None:
        plan_map = get_panel_plan_map_with_panel("vip_plan", vip_plan["id"])
        if plan_map:
            return plan_map
        return get_panel_plan_map_with_panel("vip_category", vip_plan["category_id"])

    return None


def export_backup_json(path: str):
    """
    تمام جدول‌های دیتابیس را به یک فایل JSON خروجی می‌گیرد.
    وقتی از Turso استفاده می‌شود (بدون فایل محلی)، دکمه‌ی «💾 بکاپ» ادمین
    از همین تابع برای ساخت فایل بکاپ استفاده می‌کند.
    """
    conn = get_connection()
    cur = conn.cursor()
    tables = ["users", "transactions", "configs", "discounts", "discount_usages", "agents",
              "referrals", "custom_orders", "gaming_files", "orders", "vip_categories", "vip_plans"]
    data = {}
    for table in tables:
        cur.execute(f"SELECT * FROM {table}")
        data[table] = _fetchall(cur)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# 🚫 مسدودسازی کاربر (پیشنهاد خود AI — تا ادمین بتواند ترول‌زن/مزاحمه‌گر بی‌ادب
# را بدون خروج از دیتابیس بلاکه کند: کاربر مسدودشده دیگر نمی‌تواند با ربات کار کند
# و قبل از هر هندلر اصلی بلاک می‌شود (در handlers بررسی می‌شود).
# ---------------------------------------------------------------------------
def set_user_blocked(telegram_id, blocked: bool):
    with transaction() as cur:
        cur.execute(
            "UPDATE users SET is_blocked = ? WHERE telegram_id = ?",
            (1 if blocked else 0, str(telegram_id)),
        )


def is_user_blocked(telegram_id) -> bool:
    user = get_user(telegram_id)
    if not user:
        return False
    return bool(user.get("is_blocked"))


# ---------------------------------------------------------------------------
# 👥 لیست کامل دعوت‌کنندگان (بر اساس بیشترین تعداد دعوت) + لیست افرادی که
# هر نفر دعوت کرده — برای بخش جدید «🌟 مدیریت دعوت‌شده‌ها» در پنل ادمین.
# ---------------------------------------------------------------------------
def count_referrers() -> int:
    """تعداد کاربرانی که حداقل یک نفر دعوت کرده‌اند."""
    cur = get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE invited_count > 0")
    return _fetchone(cur)["c"]


def get_referrers_page(page: int = 0, per_page: int = 10) -> list[dict]:
    """صفحه‌ای از کاربرانی که حداقل یک نفر دعوت کرده‌اند، مرتب‌شده بر اساس
    بیشترین تعداد دعوت (invited_count)."""
    cur = get_connection().cursor()
    cur.execute(
        """SELECT * FROM users WHERE invited_count > 0
           ORDER BY invited_count DESC, successful_invites DESC, id DESC
           LIMIT ? OFFSET ?""",
        (per_page, page * per_page),
    )
    return _fetchall(cur)


def get_referred_users(referrer_id: int) -> list[dict]:
    """لیست همه‌ی کاربرانی که توسط referrer_id دعوت شده‌اند، به‌همراه وضعیت و پاداش
    هر دعوت (از جدول referrals) و اطلاعات اصلی خود کاربر (از جدول users)."""
    cur = get_connection().cursor()
    cur.execute(
        """SELECT u.*, r.reward AS referral_reward, r.status AS referral_status,
                  r.created_at AS referral_created_at
           FROM referrals r
           JOIN users u ON u.id = r.invited_id
           WHERE r.referrer_id = ?
           ORDER BY r.created_at DESC""",
        (referrer_id,),
    )
    return _fetchall(cur)


# ---------------------------------------------------------------------------
# 📚 مدیریت راهنماها / آموزش‌ها — CRUD کامل برای پنل ادمین
# ---------------------------------------------------------------------------
def create_guide(title: str, content_type: str = "text", body_text: str | None = None,
                  file_id: str | None = None) -> dict:
    with transaction() as cur:
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) AS m FROM guides")
        next_order = _fetchone(cur)["m"] + 1
        cur.execute(
            """INSERT INTO guides (title, content_type, body_text, file_id, sort_order, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title, content_type, body_text, file_id, next_order, _now()),
        )
        new_id = cur.lastrowid
    return get_guide(new_id)


def get_guides() -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM guides ORDER BY sort_order ASC, id ASC")
    return _fetchall(cur)


def get_guide(guide_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM guides WHERE id = ?", (guide_id,))
    return _fetchone(cur)


def update_guide(guide_id: int, title: str | None = None, content_type: str | None = None,
                  body_text: str | None = None, file_id: str | None = None):
    guide = get_guide(guide_id)
    if not guide:
        return
    with transaction() as cur:
        cur.execute(
            """UPDATE guides SET title = ?, content_type = ?, body_text = ?, file_id = ?
               WHERE id = ?""",
            (
                title if title is not None else guide["title"],
                content_type if content_type is not None else guide["content_type"],
                body_text if body_text is not None else guide["body_text"],
                file_id if file_id is not None else guide["file_id"],
                guide_id,
            ),
        )


def delete_guide(guide_id: int):
    with transaction() as cur:
        cur.execute("DELETE FROM guides WHERE id = ?", (guide_id,))


def move_guide(guide_id: int, direction: str):
    """جابجایی جایگاه یک آیتم راهنما در لیست (direction: 'up' یا 'down')، با
    جابجایی sort_order با ایتم همسایه."""
    guides = get_guides()
    idx = next((i for i, g in enumerate(guides) if g["id"] == guide_id), None)
    if idx is None:
        return
    if direction == "up" and idx > 0:
        other = guides[idx - 1]
    elif direction == "down" and idx < len(guides) - 1:
        other = guides[idx + 1]
    else:
        return
    current = guides[idx]
    with transaction() as cur:
        cur.execute("UPDATE guides SET sort_order = ? WHERE id = ?", (other["sort_order"], current["id"]))
        cur.execute("UPDATE guides SET sort_order = ? WHERE id = ?", (current["sort_order"], other["id"]))


# ---------------------------------------------------------------------------
# 🎬 مدیریت استیکر/ویدیوی تستی هر بخش از منو (پنل ادمین)
# ---------------------------------------------------------------------------
def get_section_sticker(section_key: str) -> dict | None:
    """ردیف سفارشی‌شده‌ی این بخش را برمی‌گرداند؛ None یعنی ادمین هنوز آن را سفارشی
    نکرده و باید از استیکر پیش‌فرض داخل پروژه استفاده شود."""
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM section_stickers WHERE section_key = ?", (section_key,))
    return _fetchone(cur)


def get_all_section_stickers() -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM section_stickers")
    return _fetchall(cur)


def set_section_sticker(section_key: str, file_id: str):
    """یک استیکر/ویدیوی سفارشی برای این بخش ثبت می‌کند و آن را (دوباره) فعال می‌کند."""
    with transaction() as cur:
        cur.execute(
            """INSERT INTO section_stickers (section_key, file_id, is_enabled, updated_at)
               VALUES (?, ?, 1, ?)
               ON CONFLICT(section_key) DO UPDATE SET
                   file_id = excluded.file_id,
                   is_enabled = 1,
                   updated_at = excluded.updated_at""",
            (section_key, file_id, _now()),
        )


def set_section_sticker_enabled(section_key: str, enabled: bool):
    """این بخش را فعال/غیرفعال می‌کند؛ فایل استیکر موجود (اگر باشد) حفظ می‌شود."""
    existing = get_section_sticker(section_key)
    file_id = existing["file_id"] if existing else None
    with transaction() as cur:
        cur.execute(
            """INSERT INTO section_stickers (section_key, file_id, is_enabled, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(section_key) DO UPDATE SET
                   is_enabled = excluded.is_enabled,
                   updated_at = excluded.updated_at""",
            (section_key, file_id, 1 if enabled else 0, _now()),
        )


def reset_section_sticker(section_key: str):
    """رکورد سفارشی این بخش را کامل حذف می‌کند تا به حالت پیش‌فرض (استیکر داخل
    پروژه) برگردد."""
    with transaction() as cur:
        cur.execute("DELETE FROM section_stickers WHERE section_key = ?", (section_key,))


# ---------------------------------------------------------------------------
# 🦖 لاگ خطاها (همان رویدادهایی که به Sentry هم ارسال می‌شود)
# ---------------------------------------------------------------------------
def log_error(error_type: str, message: str | None = None, traceback_text: str | None = None, context: str | None = None):
    """یک خطای تازه رو محلی ثبت می‌کند (همزمان با ارسال به Sentry توسط global_error_handler)
    تا ادمین بدون نیاز به داشبورد Sentry هم بتونه از داخل پنل ادمین ربات ببینه‌شون.
    هر خطایی در این تابع نباید توقف کنه اجرای اصلی ربات رو، پس همیشه در try/except محافظت‌شده فراخوانده میشه."""
    try:
        with transaction() as cur:
            cur.execute(
                "INSERT INTO error_logs (error_type, message, traceback, context, occurred_at) VALUES (?, ?, ?, ?, ?)",
                (error_type, (message or "")[:2000], (traceback_text or "")[:8000], (context or "")[:500], _now()),
            )
            # فقط آخرین ۵۰۰ خطا رو نگه دار (تا دیتابیس بی‌نهایت بزرگ نشه)
            cur.execute(
                "DELETE FROM error_logs WHERE id NOT IN (SELECT id FROM error_logs ORDER BY id DESC LIMIT 500)"
            )
    except Exception:
        pass


def get_error_logs(limit: int = 20, offset: int = 0) -> list[dict]:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM error_logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
    return _fetchall(cur)


def get_error_log(log_id: int) -> dict | None:
    cur = get_connection().cursor()
    cur.execute("SELECT * FROM error_logs WHERE id = ?", (log_id,))
    return _fetchone(cur)


def count_error_logs() -> int:
    cur = get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c FROM error_logs")
    row = cur.fetchone()
    return row[0] if row else 0


def clear_error_logs():
    with transaction() as cur:
        cur.execute("DELETE FROM error_logs")
