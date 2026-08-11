# -*- coding: utf-8 -*-
"""신호 트랙레코드(성과) 집계

signal_events 테이블의 BUY/SELL 전환 이벤트마다, 신호 시점 가격 대비 이후
5거래일/20거래일 수익률을 일봉 데이터로 계산합니다. 결과는 공개 성과 페이지
(/performance)에 그대로 노출됩니다.

집계 원칙 (신뢰가 상품이므로 반드시 지킬 것):
- 기록된 모든 이벤트를 집계합니다. 불리한 결과를 골라내거나 제외하지 않습니다.
- BUY 신호는 이후 상승(+)이면 적중, SELL 신호는 이후 하락(-)이면 적중입니다.
- 아직 5/20거래일이 지나지 않은 신호는 "집계 중"(None)으로 표시합니다.

일봉 조회가 종목당 1회씩 발생하므로 결과는 market_snapshot 캐시에 저장하고,
분석 사이클이 끝날 때마다 갱신 + 페이지 조회 시 TTL(6시간) 검사로 보충합니다.
"""

from datetime import datetime, timedelta

from . import db
from .data_source import naver, naver_world, upbit

CACHE_KEY = "performance_stats"
CACHE_TTL_HOURS = 6
WINDOWS = (5, 20)          # 신호 후 N거래일 수익률
DEFAULT_DAYS = 180         # 집계 대상 기간
MAX_EVENT_ROWS = 100       # 페이지에 보여줄 최근 이벤트 수


def _norm_date(value) -> str:
    """'2026-08-11T12:00:00' / '2026.08.11' / '20260811' -> '20260811'"""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[:8]


def _fetch_candles(code: str, market: str, lookback_days: int):
    try:
        if market == "crypto":
            return upbit.get_daily_ohlcv(code, min(lookback_days, 200))
        if market == "us_stock":
            return naver_world.get_daily_ohlcv(code, lookback_days)
        rows = naver.get_daily_ohlcv_fast(code, lookback_days)
        return rows or naver.get_daily_ohlcv(code, lookback_days)
    except Exception as e:
        print(f"[성과 집계] {code} 일봉 조회 실패: {e}")
        return []


def _pct(entry, price):
    if not entry or price is None:
        return None
    return round((price - entry) / entry * 100, 2)


def _evaluate_event(event: dict, candles: list):
    """이벤트 하나의 5/20거래일·현재 수익률과 적중 여부를 계산합니다."""
    entry_date = _norm_date(event["created_at"])
    idx = next((i for i, c in enumerate(candles) if _norm_date(c["date"]) >= entry_date), None)
    if idx is None:
        return None

    entry = event.get("price") or candles[idx]["close"]
    row = {
        "created_at": event["created_at"],
        "date": entry_date[:4] + "-" + entry_date[4:6] + "-" + entry_date[6:8],
        "code": event["code"],
        "name": event["name"],
        "market": event.get("market", "stock"),
        "signal": event["signal"],
        "entry_price": entry,
    }
    is_buy = event["signal"] == "BUY"
    for w in WINDOWS:
        j = idx + w
        ret = _pct(entry, candles[j]["close"]) if j < len(candles) else None
        row[f"ret_{w}"] = ret
        row[f"hit_{w}"] = None if ret is None else (ret > 0 if is_buy else ret < 0)
    row["ret_now"] = _pct(entry, candles[-1]["close"])
    return row


def _stats(items: list, window: int) -> dict:
    rets = [e[f"ret_{window}"] for e in items if e[f"ret_{window}"] is not None]
    hits = [e[f"hit_{window}"] for e in items if e[f"hit_{window}"] is not None]
    return {
        "n": len(rets),
        "avg": round(sum(rets) / len(rets), 2) if rets else None,
        "hit_rate": round(100 * sum(1 for h in hits if h) / len(hits), 1) if hits else None,
    }


def _summarize(evaluated: list) -> dict:
    buys = [e for e in evaluated if e["signal"] == "BUY"]
    sells = [e for e in evaluated if e["signal"] == "SELL"]
    return {
        "total": len(evaluated),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "buy": {str(w): _stats(buys, w) for w in WINDOWS},
        "sell": {str(w): _stats(sells, w) for w in WINDOWS},
    }


def compute(days: int = DEFAULT_DAYS) -> dict:
    events = db.get_signal_events(days=days)
    by_code = {}
    for ev in events:
        by_code.setdefault((ev["code"], ev.get("market", "stock")), []).append(ev)

    evaluated = []
    for (code, market), evs in by_code.items():
        oldest = min(_norm_date(e["created_at"]) for e in evs)
        try:
            span = (datetime.now() - datetime.strptime(oldest, "%Y%m%d")).days
        except ValueError:
            span = days
        candles = _fetch_candles(code, market, span + 60)
        if not candles:
            continue
        for ev in evs:
            row = _evaluate_event(ev, candles)
            if row:
                evaluated.append(row)

    evaluated.sort(key=lambda e: e["created_at"], reverse=True)
    return {
        "generated_at": datetime.now().isoformat(),
        "days": days,
        "summary": _summarize(evaluated),
        "events": evaluated[:MAX_EVENT_ROWS],
    }


def refresh_cache(days: int = DEFAULT_DAYS) -> dict:
    stats = compute(days)
    db.save_market_snapshot(CACHE_KEY, stats)
    return stats


def get_cached_or_compute() -> dict:
    snap = db.get_market_snapshot(CACHE_KEY)
    if snap:
        try:
            age = datetime.now() - datetime.fromisoformat(snap["updated_at"])
            if age < timedelta(hours=CACHE_TTL_HOURS):
                return snap["data"]
        except ValueError:
            pass
    return refresh_cache()
