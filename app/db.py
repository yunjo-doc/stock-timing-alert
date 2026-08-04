# -*- coding: utf-8 -*-
"""
SQLite 저장소

- signals: 분석 결과 이력 (모든 분석 사이클 결과 누적)
- last_signal: 종목별 마지막 신호 (중복 알림 방지용)
- notifications: 카카오/로그 알림 이력

주의: Render.com 등 무료 플랜은 배포(재시작)시 파일시스템이 초기화될 수 있습니다.
장기간 데이터 보존이 중요하면 Render의 "Persistent Disk"(유료) 또는
Supabase/PlanetScale 같은 외부 DB 서비스 연동을 고려하세요.
"""

import sqlite3
import os
import hashlib
import secrets
from datetime import datetime
from contextlib import contextmanager

from .config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "app.db")


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                signal TEXT NOT NULL,
                final_score REAL,
                current_price REAL,
                reasons_json TEXT,
                z_score REAL,
                daily_return_pct REAL,
                probability_score REAL,
                market_temperature REAL,
                market_temp_score REAL,
                volume_ratio REAL,
                rsi REAL,
                trend_alignment TEXT,
                trend_score REAL,
                per REAL,
                pbr REAL,
                roe REAL,
                fundamental_score REAL,
                stop_loss REAL,
                target_price REAL,
                suggested_qty INTEGER,
                risk_score REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS last_signal (
                code TEXT PRIMARY KEY,
                signal TEXT,
                score REAL,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                kakao_access_token TEXT,
                kakao_refresh_token TEXT,
                kakao_connected_at TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                code TEXT,
                message TEXT,
                sent_via_kakao INTEGER,
                user_id INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT 'stock',
                added_at TEXT NOT NULL,
                UNIQUE(user_id, code)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                price INTEGER NOT NULL,
                status TEXT NOT NULL,
                billing_key TEXT,
                customer_key TEXT,
                current_period_start TEXT,
                current_period_end TEXT,
                created_at TEXT NOT NULL,
                canceled_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                subscription_id INTEGER,
                order_id TEXT NOT NULL,
                plan TEXT,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL,
                method TEXT,
                raw_response_json TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_snapshot (
                key TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()

    with get_conn() as conn:
        _ensure_column(conn, "users", "kakao_id", "TEXT")
        _ensure_column(conn, "users", "name", "TEXT")
        _ensure_column(conn, "users", "phone", "TEXT")
        _ensure_column(conn, "users", "is_active", "INTEGER DEFAULT 1")
        _ensure_column(conn, "users", "is_admin", "INTEGER DEFAULT 0")
        _ensure_column(conn, "user_watchlist", "market", "TEXT NOT NULL DEFAULT 'stock'")
        _ensure_column(conn, "subscriptions", "pending_plan", "TEXT")
        _ensure_column(conn, "subscriptions", "pending_price", "INTEGER")
        conn.commit()


def _ensure_column(conn, table: str, column: str, coltype: str):
    """기존 DB에 새 컬럼을 안전하게 추가 (이미 있으면 아무 것도 하지 않음)"""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def save_signal_result(result: dict):
    import json as _json
    comp = result.get("components", {})

    prob = comp.get("probability", {})
    zinfo = prob.get("detail", {}).get("zscore") or {}

    temp = comp.get("market_temperature", {})
    mt = temp.get("detail", {}).get("market_temperature") or {}
    ve = temp.get("detail", {}).get("volume_energy") or {}

    trend = comp.get("trend", {})
    tinfo = trend.get("detail") or {}

    fund = comp.get("fundamental", {})
    finfo = fund.get("detail") or {}

    risk = comp.get("risk", {})
    rinfo = risk.get("detail") or {}

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO signals (
                created_at, code, name, signal, final_score, current_price, reasons_json,
                z_score, daily_return_pct, probability_score,
                market_temperature, market_temp_score, volume_ratio,
                rsi, trend_alignment, trend_score,
                per, pbr, roe, fundamental_score,
                stop_loss, target_price, suggested_qty, risk_score
            ) VALUES (?,?,?,?,?,?,?, ?,?,?, ?,?,?, ?,?,?, ?,?,?,?, ?,?,?,?)""",
            (
                datetime.now().isoformat(), result["code"], result["name"], result["signal"],
                result.get("final_score"), result.get("current_price"),
                _json.dumps(result.get("reasons", []), ensure_ascii=False),
                zinfo.get("z"), zinfo.get("last_return_pct"), prob.get("score"),
                mt.get("temperature"), temp.get("score"), ve.get("ratio_vs_mean"),
                tinfo.get("rsi"), tinfo.get("alignment"), trend.get("score"),
                finfo.get("PER"), finfo.get("PBR"), finfo.get("ROE"), fund.get("score"),
                rinfo.get("stop_loss"), rinfo.get("target_price"), rinfo.get("suggested_qty"),
                risk.get("score"),
            ),
        )
        conn.commit()


def get_last_signal(code: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM last_signal WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None


def upsert_last_signal(code: str, signal: str, score: float):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO last_signal (code, signal, score, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(code) DO UPDATE SET signal=excluded.signal, score=excluded.score, updated_at=excluded.updated_at",
            (code, signal, score, datetime.now().isoformat()),
        )
        conn.commit()


def log_notification(code: str, message: str, sent_via_kakao: bool, user_id: int = None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO notifications (created_at, code, message, sent_via_kakao, user_id) VALUES (?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), code, message, int(sent_via_kakao), user_id),
        )
        conn.commit()


# ----------------------------------------------------------------------
# 사용자 계정 (이메일/비밀번호 로그인 + 사용자별 카카오 연동)
# ----------------------------------------------------------------------
def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()


def create_user(email: str, password: str, name: str = "", phone: str = ""):
    salt = secrets.token_hex(16)
    password_hash = _hash_password(password, salt)
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, salt, name, phone, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (email.strip().lower(), password_hash, salt, name.strip(), phone.strip(), datetime.now().isoformat()),
            )
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # 이미 존재하는 이메일


def get_user_by_email(email: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_kakao_id(kakao_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE kakao_id=?", (str(kakao_id),)).fetchone()
        return dict(row) if row else None


def create_user_from_kakao(kakao_id: str):
    """카카오 로그인 전용 계정 생성 (이메일/비밀번호 로그인은 사용하지 않는 임의 값으로 채움)"""
    salt = secrets.token_hex(16)
    password_hash = _hash_password(secrets.token_hex(16), salt)
    placeholder_email = f"kakao_{kakao_id}@kakao.local"
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, salt, kakao_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (placeholder_email, password_hash, salt, str(kakao_id), datetime.now().isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def update_user_profile(user_id: int, name: str, phone: str):
    """카카오 최초 가입 시 추가정보 입력, 또는 관리자의 회원 정보 수정에서 사용합니다."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET name=?, phone=? WHERE id=?",
            (name.strip(), phone.strip(), user_id),
        )
        conn.commit()


def promote_to_admin(user_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (user_id,))
        conn.commit()


def toggle_user_active(user_id: int) -> bool:
    """계정 활성/정지 상태를 뒤집고, 변경 후 상태(True=활성)를 반환"""
    with get_conn() as conn:
        row = conn.execute("SELECT is_active FROM users WHERE id=?", (user_id,)).fetchone()
        new_state = 0 if (row and row["is_active"]) else 1
        conn.execute("UPDATE users SET is_active=? WHERE id=?", (new_state, user_id))
        conn.commit()
        return bool(new_state)


def verify_password(user: dict, password: str) -> bool:
    return _hash_password(password, user["salt"]) == user["password_hash"]


def save_user_kakao_tokens(user_id: int, access_token: str, refresh_token: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET kakao_access_token=?, kakao_refresh_token=?, kakao_connected_at=? WHERE id=?",
            (access_token, refresh_token, datetime.now().isoformat(), user_id),
        )
        conn.commit()


def disconnect_user_kakao(user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET kakao_access_token=NULL, kakao_refresh_token=NULL, kakao_connected_at=NULL WHERE id=?",
            (user_id,),
        )
        conn.commit()


def get_all_kakao_connected_users():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE kakao_access_token IS NOT NULL AND kakao_access_token != ''"
        ).fetchall()
        return [dict(r) for r in rows]


def get_kakao_connected_users_watching(code: str):
    """관심종목 개수로 과금하는 구조이므로, 신호 알림도 해당 종목을 실제로
    관심종목에 등록한 회원에게만 보냅니다 (등록하지 않은 회원까지 받으면
    구독 유인이 사라집니다)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT u.* FROM users u
               INNER JOIN user_watchlist w ON w.user_id = u.id
               WHERE w.code = ? AND u.kakao_access_token IS NOT NULL AND u.kakao_access_token != ''""",
            (code,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_latest_signals_for_dashboard():
    """각 종목의 가장 최근 분석 결과 1건씩 반환"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT s.* FROM signals s
            INNER JOIN (
                SELECT code, MAX(id) as max_id FROM signals GROUP BY code
            ) latest ON s.code = latest.code AND s.id = latest.max_id
            ORDER BY s.final_score DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_recent_signals_for_codes(codes: list, limit: int = 30):
    """관심종목들의 분석 이력을 최신순으로 반환 (신호가 갱신될 때마다 계속 쌓이는 피드용).
    최신 1건만 보여주는 get_latest_signals_for_dashboard()와 달리, 이전 분석 결과도
    사라지지 않고 오래된 순서로 계속 아래에 남아있습니다."""
    if not codes:
        return []
    placeholders = ",".join("?" for _ in codes)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM signals WHERE code IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            list(codes) + [limit],
        ).fetchall()
        return [dict(r) for r in rows]


def trim_signal_history(keep_per_code: int = 20):
    """DB 용량을 최소로 유지하기 위해, 종목(code)별로 최근 keep_per_code건만 남기고
    나머지 이전 분석 이력은 삭제합니다. '분석 피드'는 최근 이력만 보여주므로 정상 동작합니다."""
    with get_conn() as conn:
        conn.execute(
            """DELETE FROM signals WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (PARTITION BY code ORDER BY id DESC) as rn
                    FROM signals
                ) WHERE rn > ?
            )""",
            (keep_per_code,),
        )
        conn.commit()


def get_last_analysis_time():
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(created_at) as t FROM signals").fetchone()
        return row["t"] if row else None


def get_recent_notifications(limit=30, codes=None):
    """codes를 지정하면 해당 종목코드의 알림만 반환합니다(대시보드의 증권/가상자산 탭별
    Recent Alerts가 다른 시장 종목의 알림을 섞어 보여주지 않도록 필터링할 때 사용)."""
    with get_conn() as conn:
        if codes is not None:
            codes = list(codes)
            if not codes:
                return []
            placeholders = ",".join("?" for _ in codes)
            rows = conn.execute(
                f"SELECT * FROM notifications WHERE code IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                codes + [limit],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def has_notification_today(user_id: int) -> bool:
    """이 회원이 오늘(자정 이후) 알림을 한 건이라도 받았는지 여부.
    일일 요약 알림 대상(오늘 아무 알림도 못 받은 회원)을 고를 때 사용합니다."""
    today_start = datetime.now().strftime("%Y-%m-%dT00:00:00")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM notifications WHERE user_id=? AND created_at >= ? LIMIT 1",
            (user_id, today_start),
        ).fetchone()
        return row is not None


# ----------------------------------------------------------------------
# 사용자별 관심종목 (증권 stock / 가상자산 crypto, 플랜별 개수 제한은 market별 독립 적용)
# ----------------------------------------------------------------------
def get_user_watchlist(user_id: int, market: str = None):
    with get_conn() as conn:
        if market:
            rows = conn.execute(
                "SELECT * FROM user_watchlist WHERE user_id=? AND market=? ORDER BY added_at",
                (user_id, market),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM user_watchlist WHERE user_id=? ORDER BY added_at", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def count_user_watchlist(user_id: int, market: str = "stock") -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM user_watchlist WHERE user_id=? AND market=?", (user_id, market)
        ).fetchone()
        return row["c"] if row else 0


def add_user_watchlist(user_id: int, code: str, name: str, market: str = "stock") -> bool:
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO user_watchlist (user_id, code, name, market, added_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, code.strip(), name.strip(), market, datetime.now().isoformat()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # 이미 추가된 종목


def remove_user_watchlist(user_id: int, code: str, market: str = "stock"):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM user_watchlist WHERE user_id=? AND code=? AND market=?", (user_id, code, market)
        )
        conn.commit()


def get_all_watched_stocks(market: str = "stock"):
    """모든 사용자의 관심종목을 시장(market)별로 코드 기준 중복 제거해 반환 (분석 스케줄러용)"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code, name FROM user_watchlist WHERE market=? GROUP BY code ORDER BY code", (market,)
        ).fetchall()
        return [dict(r) for r in rows]


# ----------------------------------------------------------------------
# 구독 / 결제
# ----------------------------------------------------------------------
def get_active_subscription(user_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND status='active' "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def get_user_plan_key(user_id: int) -> str:
    sub = get_active_subscription(user_id)
    if not sub:
        return "free"
    if sub["current_period_end"] and sub["current_period_end"] < datetime.now().isoformat():
        return "free"
    return sub["plan"]


def create_subscription(user_id, plan, price, billing_key, customer_key, period_start, period_end):
    with get_conn() as conn:
        # 기존 활성 구독은 교체(취소) 처리
        conn.execute(
            "UPDATE subscriptions SET status='replaced', canceled_at=? WHERE user_id=? AND status='active'",
            (datetime.now().isoformat(), user_id),
        )
        cur = conn.execute(
            "INSERT INTO subscriptions (user_id, plan, price, status, billing_key, customer_key, "
            "current_period_start, current_period_end, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, plan, price, "active", billing_key, customer_key,
             period_start, period_end, datetime.now().isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def renew_subscription(subscription_id, period_start, period_end):
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET current_period_start=?, current_period_end=?, status='active' WHERE id=?",
            (period_start, period_end, subscription_id),
        )
        conn.commit()


def renew_subscription_with_plan(subscription_id, plan, price, period_start, period_end):
    """갱신 결제 성공 시 호출. 예약된 다운그레이드가 있었다면 이 시점에 실제 플랜을 반영하고
    예약 정보(pending_plan/pending_price)를 비웁니다."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET plan=?, price=?, current_period_start=?, current_period_end=?, "
            "status='active', pending_plan=NULL, pending_price=NULL WHERE id=?",
            (plan, price, period_start, period_end, subscription_id),
        )
        conn.commit()


def schedule_plan_change(subscription_id: int, new_plan: str, new_price: int):
    """상위->하위 플랜 변경 예약. 현재 결제 주기가 끝날 때(30일 후) 자동으로 적용됩니다."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET pending_plan=?, pending_price=? WHERE id=?",
            (new_plan, new_price, subscription_id),
        )
        conn.commit()


def cancel_pending_downgrade_to_free(subscription_id: int):
    """하위 플랜(무료)으로의 예약 다운그레이드가 결제 주기 종료 시점에 적용될 때, 청구 없이 구독을 종료합니다."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET status='canceled', canceled_at=?, pending_plan=NULL, pending_price=NULL "
            "WHERE id=?",
            (datetime.now().isoformat(), subscription_id),
        )
        conn.commit()


def mark_subscription_past_due(subscription_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET status='past_due' WHERE id=?", (subscription_id,)
        )
        conn.commit()


def cancel_subscription(user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET status='canceled', canceled_at=? WHERE user_id=? AND status IN ('active','past_due')",
            (datetime.now().isoformat(), user_id),
        )
        conn.commit()


def admin_set_subscription(user_id: int, plan: str, price: int):
    """관리자가 결제 없이 직접 회원 플랜을 변경 (billing_key 없음 -> 자동 정기결제 대상에서 제외됨)"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET status='replaced', canceled_at=? WHERE user_id=? AND status='active'",
            (datetime.now().isoformat(), user_id),
        )
        conn.execute(
            "INSERT INTO subscriptions (user_id, plan, price, status, billing_key, customer_key, "
            "current_period_start, current_period_end, created_at) VALUES (?,?,?,?,NULL,NULL,?,NULL,?)",
            (user_id, plan, price, "active", datetime.now().isoformat(), datetime.now().isoformat()),
        )
        conn.commit()


def get_subscriptions_due_for_renewal():
    """오늘 기준으로 결제 주기가 끝난(=갱신 결제가 필요한) 활성 구독 목록"""
    today = datetime.now().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE status IN ('active','past_due') AND current_period_end <= ?",
            (today,),
        ).fetchall()
        return [dict(r) for r in rows]


def log_payment(user_id, subscription_id, order_id, plan, amount, status, method, raw_response: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO payments (user_id, subscription_id, order_id, plan, amount, status, method, "
            "raw_response_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, subscription_id, order_id, plan, amount, status, method,
             raw_response, datetime.now().isoformat()),
        )
        conn.commit()


def get_user_payments(user_id: int, limit=20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM payments WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]


# ----------------------------------------------------------------------
# 관리자용 회원 현황
# ----------------------------------------------------------------------
def get_last_signal_dates_for_codes(codes: list):
    """종목코드별 최근 매수 제안일 / 매도 제안일. {code: {"last_buy":..., "last_sell":...}}"""
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT code,
                    MAX(CASE WHEN signal='BUY' THEN created_at END) as last_buy,
                    MAX(CASE WHEN signal='SELL' THEN created_at END) as last_sell
                FROM signals
                WHERE code IN ({placeholders})
                GROUP BY code""",
            codes,
        ).fetchall()
        return {r["code"]: {"last_buy": r["last_buy"], "last_sell": r["last_sell"]} for r in rows}


def get_notifications_for_user(user_id: int, limit: int = 20):
    """관리자 화면에서 회원별 카카오 알림 발송 이력을 보여줄 때 사용합니다."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_members_for_admin():
    """회원별 이름/전화번호/이메일/가입일/구독플랜/관심종목/종목별 매수·매도 제안일을 모아서 반환"""
    with get_conn() as conn:
        users = [dict(r) for r in conn.execute(
            "SELECT id, email, name, phone, is_active, created_at, kakao_access_token FROM users ORDER BY id DESC"
        ).fetchall()]

    all_codes = set()
    watchlist_by_user = {}
    for u in users:
        wl = get_user_watchlist(u["id"])
        watchlist_by_user[u["id"]] = wl
        all_codes.update(s["code"] for s in wl)

    signal_dates = get_last_signal_dates_for_codes(list(all_codes))

    members = []
    for u in users:
        sub = get_active_subscription(u["id"])
        plan_key = get_user_plan_key(u["id"])
        wl = watchlist_by_user[u["id"]]
        for s in wl:
            s.update(signal_dates.get(s["code"], {"last_buy": None, "last_sell": None}))
        notifications = get_notifications_for_user(u["id"], limit=20)
        members.append({
            "id": u["id"],
            "email": u["email"],
            "name": u["name"] or "",
            "phone": u["phone"] or "",
            "is_active": u["is_active"] is None or bool(u["is_active"]),
            "created_at": u["created_at"],
            "plan": plan_key,
            "subscription": sub,
            "watchlist": wl,
            "watchlist_count": len(wl),
            "kakao_connected": bool(u.get("kakao_access_token")),
            "notifications": notifications,
            "notification_count": len(notifications),
        })
    return members


# ----------------------------------------------------------------------
# 대시보드 히어로 카드용 시장 지수 스냅샷 (하루 2회, 오전/오후 갱신)
# ----------------------------------------------------------------------
def save_market_snapshot(key: str, data: dict):
    import json as _json
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO market_snapshot (key, data_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at",
            (key, _json.dumps(data, ensure_ascii=False), datetime.now().isoformat()),
        )
        conn.commit()


def get_market_snapshot(key: str):
    import json as _json
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM market_snapshot WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        return {"data": _json.loads(row["data_json"]), "updated_at": row["updated_at"]}
