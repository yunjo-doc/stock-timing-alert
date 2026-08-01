# -*- coding: utf-8 -*-
"""
FastAPI 메인 앱

라우트:
  GET  /                 대시보드 (종목별 최신 신호)
  GET  /notifications     알림 이력
  GET  /watchlist          관심종목 관리 페이지
  GET  /api/stocks/search    종목명/코드로 검색 (관심종목 추가 화면의 찾기 기능)
  POST /watchlist/add       종목 추가
  POST /watchlist/remove     종목 삭제
  POST /run-now              수동으로 즉시 분석 실행 (관리자 토큰 필요)
  GET  /signup, POST /signup   회원가입
  GET  /login, POST /login      로그인
  POST /logout                   로그아웃
  GET  /auth/kakao/login, /auth/kakao/callback  카카오 계정으로 로그인/가입
  GET  /account                    내 계정 (카카오 나에게 채팅 연동 관리)
  GET  /kakao/authorize              카카오 연동 인증 URL로 리다이렉트 (로그인 필요)
  GET  /kakao/callback                인증 후 콜백 -> 로그인한 사용자에 토큰 저장
  POST /kakao/disconnect                카카오 연동 해제
  GET  /pricing                          요금제 안내
  GET  /billing/checkout                  카드 등록(빌링키 발급) 화면 (로그인 필요)
  GET  /billing/success, /billing/fail     Toss 빌링 인증 콜백
  POST /billing/cancel                      구독 해지
  POST /billing/downgrade                    하위 플랜으로 변경 예약 (다음 결제일에 하위 플랜 금액으로 청구)
  GET  /admin/login, POST /admin/login       관리자 로그인 (이메일/비밀번호, 최초 1회는 관리자 코드로 승격)
  POST /admin/logout                          관리자 로그아웃
  GET  /admin/members                          회원현황(관심종목/매수·매도 제안일/구독) 대시보드
  POST /admin/members/{user_id}/plan             관리자가 회원 구독 플랜 한 단계 증가/감소 (direction=up/down)
  POST /admin/members/{user_id}/toggle-active      회원 계정 활성/정지 토글
  GET  /api/signals              최신 신호 JSON (외부 연동용)
"""

import os
import sys
import uuid
from datetime import datetime, timedelta
from urllib.parse import quote

# 콘솔 인코딩이 UTF-8이 아닌 환경(예: Windows cp949)에서 로그에 포함된 특수문자(—, ± 등)
# 때문에 UnicodeEncodeError로 분석 사이클이 중단되는 것을 방지합니다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from fastapi import FastAPI, Request, Form, Header, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

from .config import load_config, save_config, BASE_DIR
from .scheduler import run_analysis_cycle, run_billing_cycle, refresh_market_snapshot
from . import db
from . import dashboard_utils as du
from . import auth
from .notify import kakao as kakao_mod
from .data_source import naver as naver_mod
from .data_source import upbit as upbit_mod
from .billing import plans as billing_plans
from .billing import toss as toss_client

# 검색 데이터소스: 증권(stock)은 네이버 금융, 가상자산(crypto)은 업비트
SEARCH_SOURCES = {"stock": naver_mod, "crypto": upbit_mod}

BILLING_PERIOD_DAYS = 30
RUN_NOW_COOLDOWN_SECONDS = 60

app = FastAPI(title="AlphaTiming")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "dev-insecure-secret-change-me"))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "app", "templates"))
templates.env.filters["from_json"] = lambda s: __import__("json").loads(s) if s else []
templates.env.filters["price"] = du.format_price


def _tojson_attr(obj):
    """분석 피드 항목 클릭 시 JS로 상세 패널을 갱신하기 위해, dict를 HTML 속성에
    안전하게 담을 수 있도록 이스케이프된 JSON 문자열로 변환합니다.
    Markup으로 감싸지 않으면 Jinja autoescape가 이스케이프된 결과를 다시 한 번
    이스케이프해서(&quot; -> &amp;quot;) 브라우저가 원래 문자열로 복원하지 못합니다."""
    import html as _html
    import json as _json
    from markupsafe import Markup
    return Markup(_html.escape(_json.dumps(obj, ensure_ascii=False, default=str)))


templates.env.filters["tojson_attr"] = _tojson_attr
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "app", "static")), name="static")

_scheduler = BackgroundScheduler()


@app.on_event("startup")
def on_startup():
    db.init_db()
    cfg = load_config()
    interval = cfg["schedule"]["interval_minutes"]
    _scheduler.add_job(lambda: run_analysis_cycle(load_config()), "interval",
                        minutes=interval, id="analysis_cycle", replace_existing=True)
    _scheduler.add_job(run_billing_cycle, "interval",
                        hours=24, id="billing_cycle", replace_existing=True)
    # 히어로 카드의 지수/시세는 방문할 때마다 값이 바뀌어 보이지 않도록 하루 2회(오전 9시,
    # 오후 3시, KST)만 갱신합니다. 서버가 막 켜졌을 때 카드가 비어있지 않도록 시작 시 1회 즉시 실행.
    _scheduler.add_job(refresh_market_snapshot, "cron", hour="9,15", minute=0,
                        timezone="Asia/Seoul", id="market_snapshot", replace_existing=True)
    _scheduler.start()
    refresh_market_snapshot()
    print(f"[스케줄러 시작] {interval}분 주기로 자동 분석을, 24시간 주기로 정기결제를, "
          f"매일 09시/15시(KST)에 시장 지수 스냅샷을 갱신합니다.")


@app.on_event("shutdown")
def on_shutdown():
    _scheduler.shutdown(wait=False)


def _check_admin(token: str, cfg: dict):
    if token != cfg.get("admin_token"):
        raise HTTPException(status_code=401, detail="관리자 토큰이 올바르지 않습니다.")


def _is_admin_session(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


def _next_analysis_run_iso():
    job = _scheduler.get_job("analysis_cycle")
    if job and job.next_run_time:
        return job.next_run_time.astimezone().replace(tzinfo=None).isoformat()
    return None


# ----------------------------------------------------------------------
# 대시보드
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, market: str = "stock"):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/", status_code=303)
    market = market if market in ("stock", "crypto") else "stock"

    cfg = load_config()
    all_signals = db.get_latest_signals_for_dashboard()
    my_codes = {w["code"] for w in db.get_user_watchlist(user["id"], market=market)}
    signals = [s for s in all_signals if s["code"] in my_codes]
    watch_count = len(my_codes)

    summary = du.market_summary(signals)
    featured = du.pick_featured_stock(signals)
    timeline = du.build_signal_timeline(featured) if featured else []

    bell = du.bell_curve_path(featured.get("z_score") if featured else 0)
    mb_percentile = None
    if featured and featured.get("volume_ratio") is not None:
        # 거래량 비율(평균 대비 배수)을 0~100 백분위로 근사 매핑 (0.5배->20, 1배->50, 2배 이상->90)
        ratio = featured["volume_ratio"]
        mb_percentile = max(0, min(100, 50 + (ratio - 1) * 40))
    mb = du.maxwell_boltzmann_path(mb_percentile)
    recent_alerts = db.get_recent_notifications(6, codes=my_codes)
    analysis_feed = db.get_recent_signals_for_codes(list(my_codes), limit=30)

    # 히어로 카드(증권/가상자산 선택 탭)에 표시할 대표 지수/시세. 방문할 때마다 값이
    # 바뀌지 않도록 실시간 조회가 아니라, 스케줄러가 하루 2회 갱신해둔 스냅샷을 읽습니다.
    kospi_kosdaq_snapshot = db.get_market_snapshot("kospi_kosdaq")
    usd_crypto_snapshot = db.get_market_snapshot("usd_crypto")
    market_indices = kospi_kosdaq_snapshot["data"] if kospi_kosdaq_snapshot else None
    usd_prices = usd_crypto_snapshot["data"] if usd_crypto_snapshot else None

    return templates.TemplateResponse(request, "index.html", {
        "signals": signals,
        "summary": summary,
        "featured": featured,
        "timeline": timeline,
        "bell": bell,
        "mb": mb,
        "recent_alerts": recent_alerts,
        "analysis_feed": analysis_feed,
        "market_indices": market_indices,
        "usd_prices": usd_prices,
        "watch_count": watch_count,
        "interval": cfg["schedule"]["interval_minutes"],
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "next_run": _next_analysis_run_iso(),
        "user": user,
        "market": market,
        "plan": billing_plans.get_plan(db.get_user_plan_key(user["id"])),
    })


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    items = db.get_recent_notifications(50)
    return templates.TemplateResponse(request, "notifications.html", {"items": items, "user": auth.current_user(request)})


# ----------------------------------------------------------------------
# 관심종목 관리
# ----------------------------------------------------------------------
@app.get("/api/stocks/search")
def api_stocks_search(request: Request, q: str = "", market: str = "stock"):
    if not auth.current_user(request):
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    source = SEARCH_SOURCES.get(market, naver_mod)
    return source.search_stocks(q, limit=15)


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, error: str = "", market: str = "stock"):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/watchlist", status_code=303)
    market = market if market in ("stock", "crypto") else "stock"

    plan = billing_plans.get_plan(db.get_user_plan_key(user["id"]))
    watch_list = db.get_user_watchlist(user["id"], market=market)
    return templates.TemplateResponse(request, "watchlist.html", {
        "watch_list": watch_list,
        "user": user,
        "plan": plan,
        "market": market,
        "limit_label": billing_plans.stock_limit_label(plan),
        "at_limit": plan["stock_limit"] is not None and len(watch_list) >= plan["stock_limit"],
        "error": error,
    })


@app.post("/watchlist/add")
def watchlist_add(request: Request, code: str = Form(...), name: str = Form(...), market: str = Form("stock")):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/watchlist", status_code=303)
    market = market if market in ("stock", "crypto") else "stock"

    plan = billing_plans.get_plan(db.get_user_plan_key(user["id"]))
    current_count = db.count_user_watchlist(user["id"], market=market)
    if plan["stock_limit"] is not None and current_count >= plan["stock_limit"]:
        label = "가상자산" if market == "crypto" else "증권"
        msg = f"{plan['name']} 플랜은 {label} 관심종목을 최대 {plan['stock_limit']}개까지 등록할 수 있습니다. 요금제를 업그레이드해주세요."
        return RedirectResponse(url=f"/watchlist?market={market}&error={quote(msg)}", status_code=303)

    added = db.add_user_watchlist(user["id"], code, name, market=market)
    if not added:
        return RedirectResponse(
            url=f"/watchlist?market={market}&error={quote('이미 등록된 종목입니다')}", status_code=303)
    return RedirectResponse(url=f"/watchlist?market={market}", status_code=303)


@app.post("/watchlist/remove")
def watchlist_remove(request: Request, code: str = Form(...), market: str = Form("stock")):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/watchlist", status_code=303)
    market = market if market in ("stock", "crypto") else "stock"
    db.remove_user_watchlist(user["id"], code, market=market)
    return RedirectResponse(url=f"/watchlist?market={market}", status_code=303)


# ----------------------------------------------------------------------
# 회원가입 / 로그인 / 로그아웃
# ----------------------------------------------------------------------
@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, error: str = ""):
    if auth.current_user(request):
        return RedirectResponse(url="/account", status_code=303)
    return templates.TemplateResponse(request, "signup.html", {"error": error, "user": None})


@app.post("/signup")
def signup_submit(request: Request, email: str = Form(...), password: str = Form(...), password2: str = Form(...),
                   name: str = Form(""), phone: str = Form("")):
    if password != password2:
        return RedirectResponse(url="/signup?error=" + "비밀번호가 일치하지 않습니다", status_code=303)
    if len(password) < 6:
        return RedirectResponse(url="/signup?error=" + "비밀번호는 6자 이상이어야 합니다", status_code=303)

    user_id = db.create_user(email, password, name, phone)
    if user_id is None:
        return RedirectResponse(url="/signup?error=" + "이미 가입된 이메일입니다", status_code=303)

    auth.login_user(request, user_id)
    return RedirectResponse(url="/account", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = "", next: str = "/account"):
    if auth.current_user(request):
        return RedirectResponse(url=next, status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": error, "next": next, "user": None})


@app.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/account")):
    user = db.get_user_by_email(email)
    if not user or not db.verify_password(user, password):
        return RedirectResponse(url=f"/login?error=이메일 또는 비밀번호가 올바르지 않습니다&next={next}", status_code=303)
    if user.get("is_active") == 0:
        return RedirectResponse(url=f"/login?error={quote('정지된 계정입니다. 관리자에게 문의해주세요.')}&next={next}", status_code=303)
    auth.login_user(request, user["id"])
    return RedirectResponse(url=next, status_code=303)


@app.post("/logout")
def logout_submit(request: Request):
    auth.logout_user(request)
    return RedirectResponse(url="/", status_code=303)


# ----------------------------------------------------------------------
# 카카오 계정으로 로그인/가입 (계정에 카카오 채팅 알림을 연동하는 /kakao/authorize 와는 별개)
# ----------------------------------------------------------------------
@app.get("/auth/kakao/login")
def kakao_login_start(request: Request):
    cfg = load_config()
    rest_api_key = cfg["kakao"].get("rest_api_key")
    if not rest_api_key:
        return HTMLResponse("환경변수 KAKAO_REST_API_KEY가 설정되어 있지 않습니다.", status_code=400)

    redirect_uri = str(request.base_url).rstrip("/") + "/auth/kakao/callback"
    url = kakao_mod.get_authorize_url(rest_api_key, redirect_uri, scope="profile_nickname")
    return RedirectResponse(url)


@app.get("/auth/kakao/callback", response_class=HTMLResponse)
def kakao_login_callback(request: Request, code: str = ""):
    cfg = load_config()
    rest_api_key = cfg["kakao"].get("rest_api_key")
    redirect_uri = str(request.base_url).rstrip("/") + "/auth/kakao/callback"

    try:
        token_data = kakao_mod.exchange_code_for_token(rest_api_key, code, redirect_uri, cfg["kakao"].get("client_secret", ""))
        profile = kakao_mod.get_user_profile(token_data["access_token"])
    except Exception as e:
        return RedirectResponse(url=f"/login?error={quote('카카오 로그인에 실패했습니다: ' + str(e))}", status_code=303)

    kakao_id = str(profile.get("id"))
    user = db.get_user_by_kakao_id(kakao_id)
    user_id = user["id"] if user else db.create_user_from_kakao(kakao_id)

    auth.login_user(request, user_id)
    return RedirectResponse(url="/account", status_code=303)


# ----------------------------------------------------------------------
# 내 계정 (구독 현황 + 카카오 '나에게 채팅' 연동 관리)
# ----------------------------------------------------------------------
@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, subscribed: int = 0, canceled: int = 0, downgrade_scheduled: int = 0,
                  test_sent: int = 0, test_failed: int = 0):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/account", status_code=303)

    plan = billing_plans.get_plan(db.get_user_plan_key(user["id"]))
    subscription = db.get_active_subscription(user["id"])
    pending_plan = billing_plans.get_plan(subscription["pending_plan"]) if subscription and subscription.get("pending_plan") else None
    payments = db.get_user_payments(user["id"])
    return templates.TemplateResponse(request, "account.html", {
        "user": user, "plan": plan, "subscription": subscription, "payments": payments,
        "pending_plan": pending_plan,
        "subscribed": subscribed, "canceled": canceled, "downgrade_scheduled": downgrade_scheduled,
        "test_sent": test_sent, "test_failed": test_failed,
    })


# ----------------------------------------------------------------------
# 요금제 / 구독 결제 (Toss Payments 정기결제 — 빌링키 발급 후 매월 자동 청구)
# ----------------------------------------------------------------------
@app.get("/pricing", response_class=HTMLResponse)
def pricing_page(request: Request):
    user = auth.current_user(request)
    current_plan = db.get_user_plan_key(user["id"]) if user else "free"
    current_index = billing_plans.PLAN_ORDER.index(current_plan) if current_plan in billing_plans.PLAN_ORDER else 0
    subscription = db.get_active_subscription(user["id"]) if user else None
    return templates.TemplateResponse(request, "pricing.html", {
        "user": user,
        "plans": [billing_plans.get_plan(k) for k in billing_plans.PLAN_ORDER],
        "plan_order": billing_plans.PLAN_ORDER,
        "current_plan": current_plan,
        "current_index": current_index,
        "subscription": subscription,
        "pending_plan_key": subscription.get("pending_plan") if subscription else None,
    })


@app.get("/billing/checkout", response_class=HTMLResponse)
def billing_checkout(request: Request, plan: str = ""):
    user = auth.current_user(request)
    if not user:
        next_url = quote(f"/billing/checkout?plan={plan}")
        return RedirectResponse(url=f"/login?next={next_url}", status_code=303)

    plan_info = billing_plans.get_plan(plan)
    if plan_info["key"] == "free" or plan_info["price"] <= 0:
        return RedirectResponse(url="/pricing", status_code=303)

    cfg = load_config()
    client_key = cfg["toss"]["client_key"]
    if not client_key:
        return HTMLResponse(
            "환경변수 TOSS_CLIENT_KEY가 설정되어 있지 않습니다. Render 환경변수를 확인해주세요.",
            status_code=400,
        )

    customer_key = f"user-{user['id']}"
    base = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(request, "billing_checkout.html", {
        "user": user,
        "plan": plan_info,
        "client_key": client_key,
        "customer_key": customer_key,
        "success_url": f"{base}/billing/success?plan={plan_info['key']}",
        "fail_url": f"{base}/billing/fail",
    })


@app.get("/billing/success", response_class=HTMLResponse)
def billing_success(request: Request, authKey: str = "", customerKey: str = "", plan: str = ""):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/pricing", status_code=303)

    plan_info = billing_plans.get_plan(plan)
    if not authKey or not customerKey:
        return RedirectResponse(url=f"/billing/fail?message={quote('인증 정보가 올바르지 않습니다')}", status_code=303)

    try:
        issued = toss_client.issue_billing_key(authKey, customerKey)
        billing_key = issued["billingKey"]

        order_id = f"sub-{user['id']}-{uuid.uuid4().hex[:12]}"
        charge = toss_client.charge_billing(
            billing_key, customerKey, plan_info["price"], order_id,
            f"AlphaTiming {plan_info['name']} 플랜 구독",
        )
    except toss_client.TossError as e:
        return RedirectResponse(url=f"/billing/fail?message={quote(str(e))}", status_code=303)

    now = datetime.now()
    period_end = now + timedelta(days=BILLING_PERIOD_DAYS)
    sub_id = db.create_subscription(
        user["id"], plan_info["key"], plan_info["price"], billing_key, customerKey,
        now.isoformat(), period_end.isoformat(),
    )
    db.log_payment(user["id"], sub_id, order_id, plan_info["key"], plan_info["price"],
                    "paid", charge.get("method", "card"), "")

    return RedirectResponse(url="/account?subscribed=1", status_code=303)


@app.get("/billing/fail", response_class=HTMLResponse)
def billing_fail(request: Request, message: str = "결제가 취소되었거나 실패했습니다."):
    return templates.TemplateResponse(request, "billing_fail.html", {
        "user": auth.current_user(request), "message": message,
    })


@app.post("/billing/cancel")
def billing_cancel(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/account", status_code=303)
    db.cancel_subscription(user["id"])
    return RedirectResponse(url="/account?canceled=1", status_code=303)


@app.post("/billing/downgrade")
def billing_downgrade(request: Request, plan: str = Form(...)):
    """상위 플랜 -> 하위 플랜 변경 예약. 현재 결제 주기(다음 결제일)가 끝날 때까지는 기존 플랜을
    그대로 이용하고, 다음 결제일에 하위 플랜 금액으로 자동 청구됩니다(무료 플랜이면 청구 없이 종료)."""
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/pricing", status_code=303)

    sub = db.get_active_subscription(user["id"])
    if not sub:
        return RedirectResponse(url="/pricing", status_code=303)

    current_key = sub["plan"]
    target_plan = billing_plans.get_plan(plan)
    order = billing_plans.PLAN_ORDER
    if current_key not in order or target_plan["key"] not in order:
        return RedirectResponse(url="/pricing", status_code=303)
    if order.index(target_plan["key"]) >= order.index(current_key):
        # 하위 플랜이 아니면(=상위 플랜/동일 플랜) 이 라우트로 처리하지 않음
        return RedirectResponse(url="/pricing", status_code=303)

    db.schedule_plan_change(sub["id"], target_plan["key"], target_plan["price"])
    return RedirectResponse(url="/account?downgrade_scheduled=1", status_code=303)


# ----------------------------------------------------------------------
# 수동 실행 (로그인한 회원 누구나 가능, 남용 방지를 위해 쿨다운 적용)
# ----------------------------------------------------------------------
@app.post("/run-now")
def run_now(request: Request, background_tasks: BackgroundTasks, x_admin_token: str = Header(default="")):
    cfg = load_config()
    if not auth.current_user(request) and not _is_admin_session(request):
        _check_admin(x_admin_token, cfg)

    last_run = db.get_last_analysis_time()
    if last_run:
        elapsed = (datetime.now() - datetime.fromisoformat(last_run)).total_seconds()
        if elapsed < RUN_NOW_COOLDOWN_SECONDS:
            wait = int(RUN_NOW_COOLDOWN_SECONDS - elapsed)
            raise HTTPException(status_code=429, detail=f"{wait}초 후 다시 시도해주세요.")

    background_tasks.add_task(run_analysis_cycle, cfg)
    return JSONResponse({"started": True, "requested_at": datetime.now().isoformat()})


@app.get("/api/last-run")
def api_last_run():
    return {"last_run": db.get_last_analysis_time(), "next_run": _next_analysis_run_iso()}


# ----------------------------------------------------------------------
# API (JSON)
# ----------------------------------------------------------------------
@app.get("/api/signals")
def api_signals():
    return db.get_latest_signals_for_dashboard()


# ----------------------------------------------------------------------
# 카카오톡 '나에게 채팅' 연동 (로그인한 사용자 본인 계정에 연결)
# ----------------------------------------------------------------------
@app.get("/kakao/authorize")
def kakao_authorize(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/account", status_code=303)

    cfg = load_config()
    rest_api_key = cfg["kakao"].get("rest_api_key")
    if not rest_api_key:
        return HTMLResponse("환경변수 KAKAO_REST_API_KEY 가 설정되어 있지 않습니다. Render 환경변수를 확인해주세요.", status_code=400)

    redirect_uri = str(request.base_url) + "kakao/callback"
    url = kakao_mod.get_authorize_url(rest_api_key, redirect_uri)
    return RedirectResponse(url)


@app.get("/kakao/callback", response_class=HTMLResponse)
def kakao_callback(request: Request, code: str = ""):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/account", status_code=303)

    cfg = load_config()
    rest_api_key = cfg["kakao"].get("rest_api_key")
    redirect_uri = str(request.base_url) + "kakao/callback"
    try:
        token_data = kakao_mod.exchange_code_for_token(rest_api_key, code, redirect_uri, cfg["kakao"].get("client_secret", ""))
    except Exception as e:
        return HTMLResponse(f"카카오 연동에 실패했습니다: {e}", status_code=400)

    db.save_user_kakao_tokens(user["id"], token_data.get("access_token"), token_data.get("refresh_token"))
    return RedirectResponse(url="/account?connected=1", status_code=303)


@app.post("/kakao/disconnect")
def kakao_disconnect(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/account", status_code=303)
    db.disconnect_user_kakao(user["id"])
    return RedirectResponse(url="/account?disconnected=1", status_code=303)


@app.post("/kakao/test-notify")
def kakao_test_notify(request: Request):
    """연동 화면의 '테스트 알림 보내기' 버튼 — 본인 카카오톡으로만 테스트 메시지 1건 발송."""
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/account", status_code=303)
    full_user = db.get_user_by_id(user["id"])
    if not full_user or not full_user.get("kakao_access_token"):
        return RedirectResponse(url="/account?test_failed=1", status_code=303)

    cfg = load_config()
    sent = kakao_mod.send_test_message(full_user, cfg)
    return RedirectResponse(url=f"/account?{'test_sent=1' if sent else 'test_failed=1'}", status_code=303)


# ----------------------------------------------------------------------
# 관리자: 회원현황 (ADMIN_TOKEN 로그인 필요, /run-now 와 별개의 세션 기반 로그인)
# ----------------------------------------------------------------------
@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request, error: str = ""):
    if _is_admin_session(request):
        return RedirectResponse(url="/admin/members", status_code=303)
    return templates.TemplateResponse(request, "admin_login.html", {"error": error, "user": None})


@app.post("/admin/login")
def admin_login_submit(request: Request, email: str = Form(...), password: str = Form(...), admin_code: str = Form("")):
    cfg = load_config()
    user = db.get_user_by_email(email)
    if not user or not db.verify_password(user, password):
        return RedirectResponse(url=f"/admin/login?error={quote('이메일 또는 비밀번호가 올바르지 않습니다')}", status_code=303)

    if not user.get("is_admin"):
        if admin_code and admin_code == cfg.get("admin_token"):
            db.promote_to_admin(user["id"])
        else:
            return RedirectResponse(
                url=f"/admin/login?error={quote('관리자 권한이 없는 계정입니다. 최초 1회는 관리자 코드가 필요합니다.')}",
                status_code=303,
            )

    request.session["is_admin"] = True
    auth.login_user(request, user["id"])
    return RedirectResponse(url="/admin/members", status_code=303)


@app.post("/admin/logout")
def admin_logout(request: Request):
    request.session.pop("is_admin", None)
    return RedirectResponse(url="/admin/login", status_code=303)


@app.get("/admin/members", response_class=HTMLResponse)
def admin_members_page(request: Request):
    if not _is_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    members = db.get_all_members_for_admin()
    return templates.TemplateResponse(request, "admin_members.html", {
        "members": members,
        "plans": billing_plans.PLANS,
        "plan_order": billing_plans.PLAN_ORDER,
        "user": auth.current_user(request),
    })


@app.post("/admin/members/{user_id}/plan")
def admin_change_plan(request: Request, user_id: int, direction: str = Form(...)):
    if not _is_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    current_key = db.get_user_plan_key(user_id)
    idx = billing_plans.PLAN_ORDER.index(current_key) if current_key in billing_plans.PLAN_ORDER else 0
    if direction == "up":
        idx = min(idx + 1, len(billing_plans.PLAN_ORDER) - 1)
    elif direction == "down":
        idx = max(idx - 1, 0)
    new_key = billing_plans.PLAN_ORDER[idx]
    new_plan = billing_plans.get_plan(new_key)
    db.admin_set_subscription(user_id, new_key, new_plan["price"])
    return RedirectResponse(url="/admin/members", status_code=303)


@app.post("/admin/members/{user_id}/toggle-active")
def admin_toggle_active(request: Request, user_id: int):
    if not _is_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    db.toggle_user_active(user_id)
    return RedirectResponse(url="/admin/members", status_code=303)
