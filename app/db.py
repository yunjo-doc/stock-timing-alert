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
