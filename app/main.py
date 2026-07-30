# -*- coding: utf-8 -*-
"""
FastAPI 메인 앱

라우트:
  GET  /                 대시보드 (종목별 최신 신호)
  GET  /notifications     알림 이력
  GET  /watchlist          관심종목 관리 페이지
  POST /watchlist/add       종목 추가
  POST /watchlist/remove     종목 삭제
  POST /run-now              수동으로 즉시 분석 실행 (관리자 토큰 필요)
  GET  /signup, POST /signup   회원가입
  GET  /login, POST /login      로그인
  POST /logout                   로그아웃
  GET  /account                    내 계정 (카카오 나에게 채팅 연동 관리)
  GET  /kakao/authorize              카카오 연동 인증 URL로 리다이렉트 (로그인 필요)
  GET  /kakao/callback                인증 후 콜백 -> 로그인한 사용자에 토큰 저장
  POST /kakao/disconnect                카카오 연동 해제
  GET  /pricing                          요금제 안내
  GET  /billing/checkout                  카드 등록(빌링키 발급) 화면 (로그인 필요)
  GET  /billing/success, /billing/fail     Toss 빌링 인증 콜백
  POST /billing/cancel                      구독 해지
  GET  /admin/login, POST /admin/login       관리자 로그인 (ADMIN_TOKEN)
  POST /admin/logout                          관리자 로그아웃
  GET  /admin/members                          회원현황(관심종목/매수·매도 제안일/구독) 대시보드
  GET  /api/signals              최신 신호 JSON (외부 연동용)
"""

import os
import uuid
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import FastAPI, Request, Form, Header, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

from .config import load_config, save_config, BASE_DIR
from .scheduler import run_analysis_cycle, run_billing_cycle
from . import db
from . import dashboard_utils as du
from . import auth
from .notify import kakao as kakao_mod
from .billing import plans as billing_plans
from .billing import toss as toss_client

BILLING_PERIOD_DAYS = 30

app = FastAPI(title="AlphaTiming")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "dev-insecure-secret-change-me"))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "app", "templates"))
templates.env.filters["from_json"] = lambda s: __import__("json").loads(s) if s else []
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
    _scheduler.start()
    print(f"[스케줄러 시작] {interval}분 주기로 자동 분석을, 24시간 주기로 정기결제를 실행합니다.")


@app.on_event("shutdown")
def on_shutdown():
    _scheduler.shutdown(wait=False)


def _check_admin(token: str, cfg: dict):
    if token != cfg.get("admin_token"):
        raise HTTPException(status_code=401, detail="관리자 토큰이 올바르지 않습니다.")


def _is_admin_session(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


# ----------------------------------------------------------------------
# 대시보드
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cfg = load_config()
    all_signals = db.get_latest_signals_for_dashboard()
    user = auth.current_user(request)

    if user:
        my_codes = {w["code"] for w in db.get_user_watchlist(user["id"])}
        signals = [s for s in all_signals if s["code"] in my_codes]
        watch_count = len(my_codes)
    else:
        preview_codes = {s["code"] for s in cfg["watch_list"]}
        signals = [s for s in all_signals if s["code"] in preview_codes]
        watch_count = len(preview_codes)

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
    recent_alerts = db.get_recent_notifications(6)

    return templates.TemplateResponse(request, "index.html", {
        "signals": signals,
        "summary": summary,
        "featured": featured,
        "timeline": timeline,
        "bell": bell,
        "mb": mb,
        "recent_alerts": recent_alerts,
        "watch_count": watch_count,
        "interval": cfg["schedule"]["interval_minutes"],
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user": user,
        "plan": billing_plans.get_plan(db.get_user_plan_key(user["id"])) if user else None,
    })


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    items = db.get_recent_notifications(50)
    return templates.TemplateResponse(request, "notifications.html", {"items": items, "user": auth.current_user(request)})


# ----------------------------------------------------------------------
# 관심종목 관리
# ----------------------------------------------------------------------
@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, error: str = ""):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/watchlist", status_code=303)

    plan = billing_plans.get_plan(db.get_user_plan_key(user["id"]))
    watch_list = db.get_user_watchlist(user["id"])
    return templates.TemplateResponse(request, "watchlist.html", {
        "watch_list": watch_list,
        "user": user,
        "plan": plan,
        "limit_label": billing_plans.stock_limit_label(plan),
        "at_limit": plan["stock_limit"] is not None and len(watch_list) >= plan["stock_limit"],
        "error": error,
    })


@app.post("/watchlist/add")
def watchlist_add(request: Request, code: str = Form(...), name: str = Form(...)):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/watchlist", status_code=303)

    plan = billing_plans.get_plan(db.get_user_plan_key(user["id"]))
    current_count = db.count_user_watchlist(user["id"])
    if plan["stock_limit"] is not None and current_count >= plan["stock_limit"]:
        msg = f"{plan['name']} 플랜은 관심종목을 최대 {plan['stock_limit']}개까지 등록할 수 있습니다. 요금제를 업그레이드해주세요."
        return RedirectResponse(url=f"/watchlist?error={quote(msg)}", status_code=303)

    added = db.add_user_watchlist(user["id"], code, name)
    if not added:
        return RedirectResponse(url=f"/watchlist?error={quote('이미 등록된 종목입니다')}", status_code=303)
    return RedirectResponse(url="/watchlist", status_code=303)


@app.post("/watchlist/remove")
def watchlist_remove(request: Request, code: str = Form(...)):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/watchlist", status_code=303)
    db.remove_user_watchlist(user["id"], code)
    return RedirectResponse(url="/watchlist", status_code=303)


# ----------------------------------------------------------------------
# 회원가입 / 로그인 / 로그아웃
# ----------------------------------------------------------------------
@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, error: str = ""):
    if auth.current_user(request):
        return RedirectResponse(url="/account", status_code=303)
    return templates.TemplateResponse(request, "signup.html", {"error": error, "user": None})


@app.post("/signup")
def signup_submit(request: Request, email: str = Form(...), password: str = Form(...), password2: str = Form(...)):
    if password != password2:
        return RedirectResponse(url="/signup?error=" + "비밀번호가 일치하지 않습니다", status_code=303)
    if len(password) < 6:
        return RedirectResponse(url="/signup?error=" + "비밀번호는 6자 이상이어야 합니다", status_code=303)

    user_id = db.create_user(email, password)
    if user_id is None:
        return RedirectResponse(url="/signup?error=" + "이미 가입된 이메일입니다", status_code=303)

    auth.login_user(request, user_id)
    return RedirectResponse(url="/account", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = "", next: str = "/account"):
    if auth.current_user(request):
        return RedirectResponse(url=next, status_code=303)
    bell = du.bell_curve_path(1.1, width=260, height=110)
    mb = du.maxwell_boltzmann_path(66, width=260, height=110)
    return templates.TemplateResponse(request, "login.html", {
        "error": error, "next": next, "user": None, "bell": bell, "mb": mb,
    })


@app.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/account")):
    user = db.get_user_by_email(email)
    if not user or not db.verify_password(user, password):
        return RedirectResponse(url=f"/login?error=이메일 또는 비밀번호가 올바르지 않습니다&next={next}", status_code=303)
    auth.login_user(request, user["id"])
    return RedirectResponse(url=next, status_code=303)


@app.post("/logout")
def logout_submit(request: Request):
    auth.logout_user(request)
    return RedirectResponse(url="/", status_code=303)


# ----------------------------------------------------------------------
# 내 계정 (구독 현황 + 카카오 '나에게 채팅' 연동 관리)
# ----------------------------------------------------------------------
@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, subscribed: int = 0, canceled: int = 0):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/account", status_code=303)

    plan = billing_plans.get_plan(db.get_user_plan_key(user["id"]))
    subscription = db.get_active_subscription(user["id"])
    payments = db.get_user_payments(user["id"])
    return templates.TemplateResponse(request, "account.html", {
        "user": user, "plan": plan, "subscription": subscription, "payments": payments,
        "subscribed": subscribed, "canceled": canceled,
    })


# ----------------------------------------------------------------------
# 요금제 / 구독 결제 (Toss Payments 정기결제 — 빌링키 발급 후 매월 자동 청구)
# ----------------------------------------------------------------------
@app.get("/pricing", response_class=HTMLResponse)
def pricing_page(request: Request):
    user = auth.current_user(request)
    current_plan = db.get_user_plan_key(user["id"]) if user else "free"
    return templates.TemplateResponse(request, "pricing.html", {
        "user": user,
        "plans": [billing_plans.get_plan(k) for k in billing_plans.PLAN_ORDER],
        "current_plan": current_plan,
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


# ----------------------------------------------------------------------
# 수동 실행 (관리자 전용)
# ----------------------------------------------------------------------
@app.post("/run-now")
def run_now(background_tasks: BackgroundTasks, x_admin_token: str = Header(default="")):
    cfg = load_config()
    _check_admin(x_admin_token, cfg)
    background_tasks.add_task(run_analysis_cycle, cfg)
    return JSONResponse({"started": True, "requested_at": datetime.now().isoformat()})


@app.get("/api/last-run")
def api_last_run():
    return {"last_run": db.get_last_analysis_time()}


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
        token_data = kakao_mod.exchange_code_for_token(rest_api_key, code, redirect_uri)
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


# ----------------------------------------------------------------------
# 관리자: 회원현황 (ADMIN_TOKEN 로그인 필요, /run-now 와 별개의 세션 기반 로그인)
# ----------------------------------------------------------------------
@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request, error: str = ""):
    if _is_admin_session(request):
        return RedirectResponse(url="/admin/members", status_code=303)
    return templates.TemplateResponse(request, "admin_login.html", {"error": error, "user": None})


@app.post("/admin/login")
def admin_login_submit(request: Request, token: str = Form(...)):
    cfg = load_config()
    if token != cfg.get("admin_token"):
        return RedirectResponse(url=f"/admin/login?error={quote('토큰이 올바르지 않습니다')}", status_code=303)
    request.session["is_admin"] = True
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
        "user": auth.current_user(request),
    })
