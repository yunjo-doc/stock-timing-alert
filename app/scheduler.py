# -*- coding: utf-8 -*-
"""분석 사이클: 관심종목 전체를 조회 -> 5개 요소 분석 -> DB 저장 -> 신호 변경시 알림"""

import uuid
from datetime import datetime, timedelta

from .data_source import naver, naver_world, upbit
from .analysis.signal_engine import analyze_stock
from .notify import kakao
from . import db
from . import dashboard_utils as du
from .billing import toss as toss_client
from .billing import kakaopay as kakaopay_client
from .billing.plans import get_plan

# market별 데이터소스: 국내증권(stock)은 네이버 금융, 해외증권(us_stock)은 네이버 해외증시,
# 가상자산(crypto)은 업비트
DATA_SOURCES = {"stock": naver, "us_stock": naver_world, "crypto": upbit}


def refresh_market_snapshot(cfg: dict = None):
    """대시보드 히어로 카드(증권/해외증권/가상자산 탭)에 쓰는 KOSPI·KOSDAQ 지수, S&P500·나스닥
    지수, BTC·ETH 참고가를 하루 2회(오전/오후)만 갱신해서 DB에 저장합니다. 방문할 때마다
    값이 바뀌어 보이지 않도록, 대시보드는 이 스냅샷만 읽고 외부 API를 직접 호출하지 않습니다."""
    try:
        indices = naver.get_market_indices()
        if indices:
            db.save_market_snapshot("kospi_kosdaq", indices)
    except Exception as e:
        print(f"[시장 지수 스냅샷 오류] {e}")

    try:
        us_indices = naver_world.get_market_indices()
        if us_indices:
            db.save_market_snapshot("us_indices", us_indices)
    except Exception as e:
        print(f"[해외 지수 스냅샷 오류] {e}")

    try:
        usd_prices = upbit.get_usd_reference_prices()
        if usd_prices:
            db.save_market_snapshot("usd_crypto", usd_prices)
    except Exception as e:
        print(f"[USD 참고시세 스냅샷 오류] {e}")


def _build_analysis_universe(cfg: dict, market: str):
    """분석 대상 = (증권일 때만) 관리자 기본 종목(config.json) + 모든 회원의 해당 market
    관심종목 합집합 (코드 기준 중복 제거)"""
    merged = {s["code"]: s for s in cfg["watch_list"]} if market == "stock" else {}
    for s in db.get_all_watched_stocks(market=market):
        merged.setdefault(s["code"], s)
    return list(merged.values())


def _analyze_stocks(market: str, stock_list: list, cfg: dict):
    """공통 분석 루프. market의 데이터소스로 stock_list(코드/이름 목록)를 분석해 저장하고,
    매수/매도 신호로 전환되면 알림을 보냅니다. 전체 유니버스 사이클과 개인 사용자의
    '지금 갱신' 둘 다 이 함수를 통해 동일한 로직으로 처리됩니다."""
    data_source = DATA_SOURCES[market]
    results = []

    for stock in stock_list:
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

        # 매수/매도 신호로 새로 진입할 때 알림 (HOLD->BUY, HOLD->SELL, BUY->SELL,
        # SELL->BUY, 그리고 관심종목 등록 후 첫 신호가 BUY/SELL인 경우도 포함).
        # 반대로 BUY/SELL -> HOLD(관망으로 빠지는 경우)는 알림을 보내지 않습니다.
        if curr_signal in ("BUY", "SELL") and curr_signal != prev_signal:
            message = _build_message(result, prev_signal)
            kakao.notify_all_connected_users(code, message, cfg)

        db.upsert_last_signal(code, curr_signal, result.get("final_score", 0))
        results.append(result)

    return results


def _analyze_market(cfg: dict, market: str):
    return _analyze_stocks(market, _build_analysis_universe(cfg, market), cfg)


def run_analysis_for_watchlist(cfg: dict, market: str, watchlist: list):
    """개인 사용자의 '지금 갱신' 버튼 전용. 관리자 기본 종목 + 전체 회원 관심종목을 모두
    도는 run_analysis_cycle()과 달리, 해당 사용자의 관심종목(해당 market)만 재분석합니다 -
    누구나 누를 수 있는 개인용 버튼이 전체 회원 데이터를 재분석하는 건 과도하고, 사이클
    전체가 끝나야 완료로 인식되는 폴링 방식과도 맞지 않기 때문입니다."""
    if market == "stock" and not du.is_kr_market_open():
        print("[국내 증권 시장 휴장 - 개인 갱신 건너뜀]")
        return []
    if market == "us_stock" and not du.is_us_market_open():
        print("[해외 증권 시장 휴장 - 개인 갱신 건너뜀]")
        return []

    results = _analyze_stocks(market, watchlist, cfg)
    db.save_market_snapshot("last_full_analysis", {"done": True})
    return results


def run_analysis_cycle(cfg: dict):
    print(f"\n===== 분석 사이클 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====")
    results = []
    # 가상자산은 24시간 시장이라 항상 분석하지만, 국내/해외 증권은 각자의 정규장 시간이
    # 아니면 휴장 중인 지난 데이터를 계속 새로 저장하는 게 의미가 없어 건너뜁니다.
    if du.is_kr_market_open():
        results += _analyze_market(cfg, "stock")
    else:
        print("[국내 증권 시장 휴장 - 분석 건너뜀]")
    if du.is_us_market_open():
        results += _analyze_market(cfg, "us_stock")
    else:
        print("[해외 증권 시장 휴장 - 분석 건너뜀]")
    results += _analyze_market(cfg, "crypto")
    db.trim_signal_history(keep_per_code=20)  # DB 용량 최소화: 종목당 최근 20건만 유지
    # "지금 갱신" 버튼의 완료 감지용 마커. signals 테이블의 MAX(created_at)은 사이클 도중
    # 아무 종목이나 하나 저장될 때마다 계속 갱신되어(전체 종목 처리에 수 분~십수 분 걸림)
    # "완료됐다"고 오판하게 만들므로, 사이클 전체가 끝난 시점만 별도로 기록합니다.
    db.save_market_snapshot("last_full_analysis", {"done": True})
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
        # PayApp은 우리가 직접 청구하지 않고 PayApp이 자체적으로 주기 청구 후
        # feedbackurl로 통보합니다(/billing/payapp/feedback 참고). 여기서는 건너뜁니다.
        if sub.get("provider") == "payapp":
            continue

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
        item_name = f"AlphaTiming {plan['name']} 플랜 정기결제"
        try:
            if sub.get("provider") == "kakao":
                charge = kakaopay_client.charge_subscription(
                    sub["billing_key"], order_id, sub["customer_key"], item_name, plan["price"],
                )
                method = charge.get("payment_method_type", "card").lower()
            else:
                charge = toss_client.charge_billing(
                    sub["billing_key"], sub["customer_key"], plan["price"], order_id, item_name,
                )
                method = charge.get("method", "card")
        except (toss_client.TossError, kakaopay_client.KakaoPayError) as e:
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
                        "paid", method, "")
        if pending_plan_key:
            print(f"[플랜 다운그레이드 적용/결제 완료] user_id={sub['user_id']} -> {plan['key']}")
        else:
            print(f"[정기결제 완료] user_id={sub['user_id']} plan={sub['plan']}")


def _build_message(result: dict, prev_signal: str) -> str:
    """카카오 '나에게 보내기'는 텍스트 200자 제한이 있어서, 전환 여부를 바로 알 수 있는
    핵심 정보(전환 방향/현재가/손절·목표가/핵심 근거 1줄)만 담습니다. 상세 근거와 전체
    분석은 앱 대시보드에서 확인하도록 안내합니다."""
    signal_kr = {"BUY": "매수", "SELL": "매도", "HOLD": "관망"}
    prev_label = signal_kr.get(prev_signal, "신규")  # 관심종목 등록 후 첫 신호는 이전 신호가 없음
    lines = [
        f"[전환] {result['name']}({result['code']}) {prev_label}→{signal_kr[result['signal']]}",
        f"현재가 {du.format_price(result.get('current_price'))}원 · 종합점수 {result.get('final_score')}",
    ]
    risk_detail = result.get("components", {}).get("risk", {}).get("detail", {})
    if risk_detail:
        lines.append(
            f"손절 {du.format_price(risk_detail.get('stop_loss'))} "
            f"/ 목표 {du.format_price(risk_detail.get('target_price'))}"
        )
    reasons = result.get("reasons", [])
    if reasons:
        lines.append(f"[근거] {reasons[0]}")
    lines.append("자세히 보기: AlphaTiming 앱")
    return "\n".join(lines)


def _build_digest_message(rows: list) -> str:
    """오늘 하루 신호 전환 알림을 한 번도 못 받은(=조용했던) 회원에게 보내는 일일 요약.
    관심종목이 많으면 목록을 줄이고 '...외 N개'로 표시해 카카오 200자 제한을 지킵니다."""
    signal_kr = {"BUY": "매수", "SELL": "매도", "HOLD": "관망"}
    buy = sum(1 for r in rows if r["signal"] == "BUY")
    sell = sum(1 for r in rows if r["signal"] == "SELL")
    hold = sum(1 for r in rows if r["signal"] == "HOLD")

    lines = [f"[일일 요약] 관심종목 {len(rows)}개 · 매수{buy} 매도{sell} 관망{hold}"]
    shown = 0
    for r in rows:
        line = f"{r['name']} {signal_kr.get(r['signal'], r['signal'])}"
        # 남은 줄(footer 포함)까지 감안해 160자 안으로 유지
        if sum(len(l) + 1 for l in lines) + len(line) > 160:
            break
        lines.append(line)
        shown += 1
    if shown < len(rows):
        lines.append(f"...외 {len(rows) - shown}개")
    lines.append("오늘 신호 변화는 없었어요.")
    return "\n".join(lines)


def send_daily_digest(cfg: dict):
    """오늘 하루 알림을 한 번도 못 받은(신호 변화가 없어 조용했던) 카카오 연동 회원에게
    관심종목 현재 상태(HOLD뿐이어도)를 요약해서 보냅니다. 매일 16:00(KST)에 실행됩니다."""
    users = db.get_all_kakao_connected_users()
    if not users:
        return

    signals_by_code = {s["code"]: s for s in db.get_latest_signals_for_dashboard()}
    sent_count = 0
    skipped_count = 0

    for user in users:
        if db.has_notification_today(user["id"]):
            skipped_count += 1
            continue

        watchlist = db.get_user_watchlist(user["id"])
        rows = [signals_by_code[w["code"]] for w in watchlist if w["code"] in signals_by_code]
        if not rows:
            continue

        message = _build_digest_message(rows)
        if kakao.notify_user(user, "DIGEST", message, cfg):
            sent_count += 1

    print(f"[일일 요약] 대상 {len(users)}명 중 {sent_count}명 발송, {skipped_count}명은 오늘 이미 알림을 받아 제외")
