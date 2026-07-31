# -*- coding: utf-8 -*-
"""분석 사이클: 관심종목 전체를 조회 -> 5개 요소 분석 -> DB 저장 -> 신호 변경시 알림"""

import uuid
from datetime import datetime, timedelta

from .data_source import naver, upbit
from .analysis.signal_engine import analyze_stock
from .notify import kakao
from . import db
from .billing import toss as toss_client
from .billing.plans import get_plan

# market별 데이터소스: 증권(stock)은 네이버 금융, 가상자산(crypto)은 업비트
DATA_SOURCES = {"stock": naver, "crypto": upbit}


def _build_analysis_universe(cfg: dict, market: str):
    """분석 대상 = (증권일 때만) 관리자 기본 종목(config.json) + 모든 회원의 해당 market
    관심종목 합집합 (코드 기준 중복 제거)"""
    merged = {s["code"]: s for s in cfg["watch_list"]} if market == "stock" else {}
    for s in db.get_all_watched_stocks(market=market):
        merged.setdefault(s["code"], s)
    return list(merged.values())


def _analyze_market(cfg: dict, market: str):
    data_source = DATA_SOURCES[market]
    results = []

    for stock in _build_analysis_universe(cfg, market):
        code, name = stock["code"], stock["name"]
        try:
            ohlc_rows = data_source.get_daily_ohlcv(code, cfg["lookback_days"])
            fundamental_data = data_source.get_fundamental(code)
        except Exception as e:
            print(f"[에러] {name}({code}) 데이터 조회 실패: {e}")
            continue

        if not ohlc_rows:
            print(f"[경고] {name}({code}) 시세 데이터를 가져오지 못했습니다.")
            continue

        result = analyze_stock(code, name, ohlc_rows, fundamental_data, cfg)
        db.save_signal_result(result)
        print(f"[{market}][{name}({code})] 신호: {result['signal']} (점수 {result.get('final_score')})")

        prev = db.get_last_signal(code)
        prev_signal = prev["signal"] if prev else None
        curr_signal = result["signal"]

        if curr_signal in ("BUY", "SELL") and curr_signal != prev_signal:
            message = _build_message(result)
            kakao.notify_all_connected_users(code, message, cfg)

        db.upsert_last_signal(code, curr_signal, result.get("final_score", 0))
        results.append(result)

    return results


def run_analysis_cycle(cfg: dict):
    print(f"\n===== 분석 사이클 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====")
    results = _analyze_market(cfg, "stock") + _analyze_market(cfg, "crypto")
    db.trim_signal_history(keep_per_code=20)  # DB 용량 최소화: 종목당 최근 20건만 유지
    print("===== 분석 사이클 종료 =====\n")
    return results


BILLING_PERIOD_DAYS = 30


def run_billing_cycle():
    """결제 주기가 끝난 구독을 저장된 빌링키로 자동 청구 (매일 실행)"""
    due = db.get_subscriptions_due_for_renewal()
    if not due:
        return
    print(f"[정기결제] 청구 대상 {len(due)}건")

    for sub in due:
        pending_plan_key = sub.get("pending_plan")

        # 하위 플랜(무료)으로 예약된 다운그레이드는 결제 없이 구독을 종료합니다.
        if pending_plan_key and get_plan(pending_plan_key)["price"] <= 0:
            db.cancel_pending_downgrade_to_free(sub["id"])
            print(f"[플랜 다운그레이드] user_id={sub['user_id']} plan={sub['plan']} -> free (청구 없음)")
            continue

        # 예약된 다운그레이드가 있으면 이번 결제 주기부터 하위 플랜 금액으로 청구합니다.
        plan = get_plan(pending_plan_key) if pending_plan_key else get_plan(sub["plan"])
        order_prefix = "downgrade" if pending_plan_key else "renew"
        order_id = f"{order_prefix}-{sub['user_id']}-{uuid.uuid4().hex[:12]}"
        try:
            charge = toss_client.charge_billing(
                sub["billing_key"], sub["customer_key"], plan["price"], order_id,
                f"AlphaTiming {plan['name']} 플랜 정기결제",
            )
        except toss_client.TossError as e:
            print(f"[정기결제 실패] user_id={sub['user_id']} plan={sub['plan']}: {e}")
            db.mark_subscription_past_due(sub["id"])
            db.log_payment(sub["user_id"], sub["id"], order_id, plan["key"], plan["price"],
                            "failed", "card", str(e))
            continue

        now = datetime.now()
        period_end = now + timedelta(days=BILLING_PERIOD_DAYS)
        db.renew_subscription_with_plan(sub["id"], plan["key"], plan["price"],
                                         now.isoformat(), period_end.isoformat())
        db.log_payment(sub["user_id"], sub["id"], order_id, plan["key"], plan["price"],
                        "paid", charge.get("method", "card"), "")
        if pending_plan_key:
            print(f"[플랜 다운그레이드 적용/결제 완료] user_id={sub['user_id']} -> {plan['key']}")
        else:
            print(f"[정기결제 완료] user_id={sub['user_id']} plan={sub['plan']}")


def _build_message(result: dict) -> str:
    signal_kr = {"BUY": "매수", "SELL": "매도"}.get(result["signal"], result["signal"])
    lines = [
        f"[{signal_kr} 신호] {result['name']} ({result['code']})",
        f"현재가: {result.get('current_price')}원 / 종합점수: {result.get('final_score')}",
    ]
    risk_detail = result.get("components", {}).get("risk", {}).get("detail", {})
    if risk_detail:
        lines.append(f"손절가: {risk_detail.get('stop_loss')} / 목표가: {risk_detail.get('target_price')}")
    lines.append("--- 근거 ---")
    lines.extend(result.get("reasons", [])[:3])
    lines.append("\n(테스트 버전 신호이며, 투자 판단과 책임은 본인에게 있습니다.)")
    return "\n".join(lines)
