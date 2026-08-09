# -*- coding: utf-8 -*-
"""
AI 추천 종목 (프로 플랜 전용, 국내증권 한정)

- 관심종목 분석(스케줄러, 30분 주기)과는 별개로, 코스피/코스닥 시가총액 상위 100개씩
  (app/data/kospi_kosdaq_universe.json)을 대상으로 별도 스캔을 돌려 BUY 신호 중
  final_score 상위 5개를 뽑습니다. 정기 스케줄러에 합치면 사이클이 너무 길어지므로,
  프로 플랜 사용자가 버튼을 클릭할 때(그리고 캐시가 1시간 넘게 지났을 때만) 백그라운드
  스레드에서 실행합니다.
- 빠른 조회를 위해 sise_day.naver를 여러 페이지 긁는 기존 get_daily_ohlcv() 대신,
  1회 요청으로 끝나는 naver.get_daily_ohlcv_fast()를 사용합니다.
"""

import json
import threading
from datetime import datetime
from pathlib import Path

from .data_source import naver
from .analysis.signal_engine import analyze_stock
from . import db

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
