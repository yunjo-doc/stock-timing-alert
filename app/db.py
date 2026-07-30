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
        conn.commit()


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


def create_user(email: str, password: str):
    salt = secrets.token_hex(16)
    password_hash = _hash_password(password, salt)
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
                (email.strip().lower(), password_hash, salt, datetime.now().isoformat()),
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


def get_last_analysis_time():
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(created_at) as t FROM signals").fetchone()
        return row["t"] if row else None


def get_recent_notifications(limit=30):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ----------------------------------------------------------------------
# 사용자별 관심종목 (플랜별 개수 제한은 app/main.py, app/billing/plans.py 참고)
# ----------------------------------------------------------------------
def get_user_watchlist(user_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM user_watchlist WHERE user_id=? ORDER BY added_at", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def count_user_watchlist(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM user_watchlist WHERE user_id=?", (user_id,)
        ).fetchone()
        return row["c"] if row else 0


def add_user_watchlist(user_id: int, code: str, name: str) -> bool:
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO user_watchlist (user_id, code, name, added_at) VALUES (?, ?, ?, ?)",
                (user_id, code.strip(), name.strip(), datetime.now().isoformat()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # 이미 추가된 종목


def remove_user_watchlist(user_id: int, code: str):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM user_watchlist WHERE user_id=? AND code=?", (user_id, code)
        )
        conn.commit()


def get_all_watched_stocks():
    """모든 사용자의 관심종목을 코드 기준으로 중복 제거해 반환 (분석 스케줄러용)"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code, name FROM user_watchlist GROUP BY code ORDER BY code"
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


def get_all_members_for_admin():
    """회원별 이메일/가입일/구독플랜/관심종목/종목별 매수·매도 제안일을 모아서 반환"""
    with get_conn() as conn:
        users = [dict(r) for r in conn.execute("SELECT id, email, created_at FROM users ORDER BY id DESC").fetchall()]

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
        members.append({
            "id": u["id"],
            "email": u["email"],
            "created_at": u["created_at"],
            "plan": plan_key,
            "subscription": sub,
            "watchlist": wl,
            "watchlist_count": len(wl),
        })
    return members
