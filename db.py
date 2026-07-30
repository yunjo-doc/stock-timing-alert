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
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                code TEXT,
                message TEXT,
                sent_via_kakao INTEGER
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


def log_notification(code: str, message: str, sent_via_kakao: bool):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO notifications (created_at, code, message, sent_via_kakao) VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(), code, message, int(sent_via_kakao)),
        )
        conn.commit()


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
