# -*- coding: utf-8 -*-
"""
AI 추천 종목 (스탠다드 플랜 이상, 국내증권 한정)

- 관심종목 분석(스케줄러, 30분 주기)과는 별개로, 코스피/코스닥 시가총액 상위 100개씩
  (app/data/kospi_kosdaq_universe.json)을 대상으로 별도 스캔을 돌려 BUY 신호 중
  final_score 상위 5개를 뽑습니다. 정기 스케줄러에 합치면 사이클이 너무 길어지므로,
  스탠다드 플랜 이상 사용자가 버튼을 클릭할 때(그리고 캐시가 1시간 넘게 지났을 때만)
  백그라운드 스레드에서 실행합니다.
- 빠른 조회를 위해 sise_day.naver를 여러 페이지 긁는 기존 get_daily_ohlcv() 대신,
  1회 요청으로 끝나는 naver.get_daily_ohlcv_fast()를 사용합니다.
- 스캔이 끝날 때마다 그날의 TOP5를 ai_recommend_events에 기록해 트랙레코드를 쌓습니다
  (performance.py의 signal_events와 동일한 방식: 기록은 가볍게, 수익률은 조회 시점에
  최신 시세로 계산 + 캐시).
"""

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .data_source import naver
from .analysis.signal_engine import analyze_stock
from . import db
from . import performance as perf

_DATA_PATH = Path(__file__).parent / "data" / "kospi_kosdaq_universe.json"
_UNIVERSE = json.loads(_DATA_PATH.read_text(encoding="utf-8"))

CACHE_TTL_SEC = 60 * 60  # 1시간
SNAPSHOT_KEYS = {"KOSPI": "ai_recommend_kospi", "KOSDAQ": "ai_recommend_kosdaq"}
GROUPS = ("KOSPI", "KOSDAQ")

_scan_locks = {group: threading.Lock() for group in GROUPS}


def is_scanning(group: str) -> bool:
    return _scan_locks[group].locked()


def get_cached(group: str):
    return db.get_market_snapshot(SNAPSHOT_KEYS[group])


def is_cache_fresh(snapshot) -> bool:
    if not snapshot:
        return False
    try:
        updated = datetime.fromisoformat(snapshot["updated_at"])
    except (KeyError, ValueError):
        return False
    return (datetime.now() - updated).total_seconds() < CACHE_TTL_SEC


def _to_card(result: dict) -> dict:
    """캐시에는 대시보드 카드 표시에 필요한 정보만 남깁니다 (전체 result는 용량이 큼)."""
    risk_detail = result.get("components", {}).get("risk", {}).get("detail", {})
    return {
        "code": result["code"],
        "name": result["name"],
        "final_score": result.get("final_score"),
        "current_price": result.get("current_price"),
        "reasons": result.get("reasons", [])[:2],
        "stop_loss": risk_detail.get("stop_loss"),
        "target_price": risk_detail.get("target_price"),
    }


def _scan_group(group: str, cfg: dict):
    """후보 종목을 전부 분석해서 BUY 신호 중 final_score 상위 5개를 캐시에 저장합니다."""
    candidates = _UNIVERSE.get(group, [])
    buy_results = []
    for stock in candidates:
        code, name = stock["code"], stock["name"]
        try:
            ohlc_rows = naver.get_daily_ohlcv_fast(code, cfg["lookback_days"])
            if not ohlc_rows:
                continue
            fundamental_data = naver.get_fundamental(code)
            result = analyze_stock(code, name, ohlc_rows, fundamental_data, cfg)
        except Exception as e:
            print(f"[AI 추천 스캔 오류] {group} {name}({code}): {e}")
            continue
        if result.get("signal") == "BUY":
            buy_results.append(result)

    buy_results.sort(key=lambda r: r.get("final_score") or 0, reverse=True)
    top5 = [_to_card(r) for r in buy_results[:5]]
    db.save_market_snapshot(SNAPSHOT_KEYS[group], top5)
    # 트랙레코드용 기록. 스캔이 실제로 도는 건 캐시가 1시간 지났을 때뿐이라(그룹당 최대
    # 5건) DB 부하는 무시할 수준입니다. 수익률은 여기서 계산하지 않고 조회 시점에 최신
    # 시세로 계산합니다(performance.py와 동일한 방식).
    db.save_ai_recommend_events(group, top5)
    print(f"[AI 추천 스캔 완료] {group}: 후보 {len(candidates)}개 중 BUY {len(buy_results)}개, 상위 {len(top5)}개 저장")


def trigger_scan_if_stale(group: str, cfg: dict) -> bool:
    """캐시가 없거나 1시간이 지났으면 백그라운드 스레드로 재스캔을 시작합니다.
    이미 스캔 중이면 중복 실행하지 않고 False를 반환합니다."""
    if is_cache_fresh(get_cached(group)):
        return False

    lock = _scan_locks[group]
    if not lock.acquire(blocking=False):
        return False  # 이미 스캔 중

    def _run():
        try:
            _scan_group(group, cfg)
        finally:
            lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return True


# ----------------------------------------------------------------------
# 트랙레코드 (지금까지 추천한 종목들의 실제 이후 수익률/승률)
# ----------------------------------------------------------------------
TRACK_CACHE_KEY = "ai_recommend_track_record"
TRACK_CACHE_TTL_HOURS = 6
TRACK_WINDOWS = (5, 20)


def _evaluate_pick(ev: dict, candles: list):
    entry_date = perf._norm_date(ev["created_at"])
    idx = next((i for i, c in enumerate(candles) if perf._norm_date(c["date"]) >= entry_date), None)
    if idx is None:
        return None
    entry = ev.get("price") or candles[idx]["close"]
    row = {
        "created_at": ev["created_at"],
        "date": f"{entry_date[:4]}-{entry_date[4:6]}-{entry_date[6:8]}",
        "group": ev["group_key"], "code": ev["code"], "name": ev["name"],
        "final_score": ev.get("final_score"), "entry_price": entry,
    }
    for w in TRACK_WINDOWS:
        j = idx + w
        ret = perf._pct(entry, candles[j]["close"]) if j < len(candles) else None
        row[f"ret_{w}"] = ret
        row[f"hit_{w}"] = None if ret is None else ret > 0  # AI 추천은 항상 BUY -> 상승이면 적중
    row["ret_now"] = perf._pct(entry, candles[-1]["close"])
    return row


def _window_stats(items: list, window: int) -> dict:
    rets = [e[f"ret_{window}"] for e in items if e[f"ret_{window}"] is not None]
    hits = [e[f"hit_{window}"] for e in items if e[f"hit_{window}"] is not None]
    return {
        "n": len(rets),
        "avg": round(sum(rets) / len(rets), 2) if rets else None,
        "hit_rate": round(100 * sum(1 for h in hits if h) / len(hits), 1) if hits else None,
    }


def compute_track_record(days: int = 180) -> dict:
    """기록된 모든 추천을 집계합니다 - 불리한 결과를 골라내지 않고 전부 포함합니다."""
    events = db.get_ai_recommend_events(days=days)
    by_code = {}
    for ev in events:
        by_code.setdefault(ev["code"], []).append(ev)

    evaluated = []
    for code, evs in by_code.items():
        oldest = min(perf._norm_date(e["created_at"]) for e in evs)
        try:
            span = (datetime.now() - datetime.strptime(oldest, "%Y%m%d")).days
        except ValueError:
            span = days
        candles = naver.get_daily_ohlcv_fast(code, span + 60) or naver.get_daily_ohlcv(code, span + 60)
        if not candles:
            continue
        for ev in evs:
            row = _evaluate_pick(ev, candles)
            if row:
                evaluated.append(row)

    evaluated.sort(key=lambda e: e["created_at"], reverse=True)
    return {
        "generated_at": datetime.now().isoformat(),
        "total": len(evaluated),
        "windows": {str(w): _window_stats(evaluated, w) for w in TRACK_WINDOWS},
        "events": evaluated[:100],
    }


def get_track_record() -> dict:
    snap = db.get_market_snapshot(TRACK_CACHE_KEY)
    if snap:
        try:
            age = datetime.now() - datetime.fromisoformat(snap["updated_at"])
            if age < timedelta(hours=TRACK_CACHE_TTL_HOURS):
                return snap["data"]
        except ValueError:
            pass
    stats = compute_track_record()
    db.save_market_snapshot(TRACK_CACHE_KEY, stats)
    return stats
