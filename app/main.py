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
  GET  /api/signals              최신 신호 JSON (외부 연동용)
"""

import os
from datetime import datetime

from fastapi import FastAPI, Request, Form, Header, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

from .config import load_config, save_config, BASE_DIR
from .scheduler import run_analysis_cycle
from . import db
from . import dashboard_utils as du
from . import auth
from .notify import kakao as kakao_mod

app = FastAPI(title="주식 투자 타이밍 알리미")
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
    _scheduler.start()
    print(f"[스케줄러 시작] {interval}분 주기로 자동 분석을 실행합니다.")


@app.on_event("shutdown")
def on_shutdown():
    _scheduler.shutdown(wait=False)


def _check_admin(token: str, cfg: dict):
    if token != cfg.get("admin_token"):
        raise HTTPException(status_code=401, detail="관리자 토큰이 올바르지 않습니다.")


# ----------------------------------------------------------------------
# 대시보드
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cfg = load_config()
    signals = db.get_latest_signals_for_dashboard()

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
        "watch_count": len(cfg["watch_list"]),
        "interval": cfg["schedule"]["interval_minutes"],
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user": auth.current_user(request),
    })


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    items = db.get_recent_notifications(50)
    return templates.TemplateResponse(request, "notifications.html", {"items": items, "user": auth.current_user(request)})


# ----------------------------------------------------------------------
# 관심종목 관리
# ----------------------------------------------------------------------
@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request):
    cfg = load_config()
    return templates.TemplateResponse(request, "watchlist.html", {
        "watch_list": cfg["watch_list"], "user": auth.current_user(request),
    })


@app.post("/watchlist/add")
def watchlist_add(code: str = Form(...), name: str = Form(...)):
    cfg = load_config()
    if not any(s["code"] == code for s in cfg["watch_list"]):
        cfg["watch_list"].append({"code": code.strip(), "name": name.strip()})
        save_config(cfg)
    return RedirectResponse(url="/watchlist", status_code=303)


@app.post("/watchlist/remove")
def watchlist_remove(code: str = Form(...)):
    cfg = load_config()
    cfg["watch_list"] = [s for s in cfg["watch_list"] if s["code"] != code]
    save_config(cfg)
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
    return templates.TemplateResponse(request, "login.html", {"error": error, "next": next, "user": None})


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
# 내 계정 (카카오 '나에게 채팅' 연동 관리)
# ----------------------------------------------------------------------
@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/account", status_code=303)
    return templates.TemplateResponse(request, "account.html", {"user": user})


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
