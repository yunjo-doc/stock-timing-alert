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
  GET  /kakao/authorize        카카오 연동 인증 URL로 리다이렉트
  GET  /kakao/callback          인증 후 콜백 -> access/refresh token 발급 및 화면 표시
  GET  /api/signals              최신 신호 JSON (외부 연동용)
"""

import os
from datetime import datetime

from fastapi import FastAPI, Request, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler

from .config import load_config, save_config, BASE_DIR
from .scheduler import run_analysis_cycle
from . import db
from .notify import kakao as kakao_mod

app = FastAPI(title="주식 투자 타이밍 알리미")
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
    return templates.TemplateResponse(request, "index.html", {
        "signals": signals,
        "watch_count": len(cfg["watch_list"]),
        "interval": cfg["schedule"]["interval_minutes"],
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    items = db.get_recent_notifications(50)
    return templates.TemplateResponse(request, "notifications.html", {"items": items})


# ----------------------------------------------------------------------
# 관심종목 관리
# ----------------------------------------------------------------------
@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request):
    cfg = load_config()
    return templates.TemplateResponse(request, "watchlist.html", {"watch_list": cfg["watch_list"]})


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
# 수동 실행 (관리자 전용)
# ----------------------------------------------------------------------
@app.post("/run-now")
def run_now(x_admin_token: str = Header(default="")):
    cfg = load_config()
    _check_admin(x_admin_token, cfg)
    results = run_analysis_cycle(cfg)
    return JSONResponse({"ran": True, "count": len(results)})


# ----------------------------------------------------------------------
# API (JSON)
# ----------------------------------------------------------------------
@app.get("/api/signals")
def api_signals():
    return db.get_latest_signals_for_dashboard()


# ----------------------------------------------------------------------
# 카카오톡 연동 도우미
# ----------------------------------------------------------------------
@app.get("/kakao/authorize")
def kakao_authorize(request: Request):
    cfg = load_config()
    rest_api_key = cfg["kakao"].get("rest_api_key")
    if not rest_api_key:
        return HTMLResponse("환경변수 KAKAO_REST_API_KEY 가 설정되어 있지 않습니다.", status_code=400)
    redirect_uri = str(request.base_url) + "kakao/callback"
    url = kakao_mod.get_authorize_url(rest_api_key, redirect_uri)
    return RedirectResponse(url)


@app.get("/kakao/callback", response_class=HTMLResponse)
def kakao_callback(request: Request, code: str = ""):
    cfg = load_config()
    rest_api_key = cfg["kakao"].get("rest_api_key")
    redirect_uri = str(request.base_url) + "kakao/callback"
    try:
        token_data = kakao_mod.exchange_code_for_token(rest_api_key, code, redirect_uri)
    except Exception as e:
        return HTMLResponse(f"토큰 발급 실패: {e}", status_code=400)

    return templates.TemplateResponse(request, "kakao_token.html", {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
    })
