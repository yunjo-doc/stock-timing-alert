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
  GET  /complete-profile, POST /complete-profile  카카오 최초 가입 시 이름/전화번호 입력 (로그인 필요)
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
  POST /admin/members/{user_id}/profile             관리자가 회원 이름/전화번호 수정
  GET  /api/signals              최신 신호 JSON (외부 연동용)
  GET  /api/ai-recommend           AI 추천 종목 TOP5 JSON (코스피/코스닥, 프로 플랜 전용)
  GET  /api/ma-pullback              이동평균선 눌림목 전략 JSON (관심종목 기준, 로그인 필요)
  POST /api/waitlist/click             유료 플랜 오픈 알림 버튼 클릭 집계 (플랜별)
  POST /api/waitlist/signup            유료 플랜 오픈 알림 신청 (이메일 + 개인정보 동의)
"""

import json
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

# 콘솔 인코딩이 UTF-8이 아닌 환경(예: Windows cp949)에서 로그에 포함된 특수문자(—, ± 등)
# 때문에 UnicodeEncodeError로 분석 사이클이 중단되는 것을 방지합니다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from fastapi import FastAPI, Request, Form, Header, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

from .config import load_config, save_config, BASE_DIR
from .scheduler import (run_analysis_cycle, run_analysis_for_watchlist, run_billing_cycle,
                         refresh_market_snapshot, send_daily_digest,
                         build_promo_messages, send_promo_message)
from . import db
from . import dashboard_utils as du
from . import auth
from .notify import kakao as kakao_mod
from .data_source import naver as naver_mod
from .data_source import naver_world as naver_world_mod
from . import ai_recommend
from .analysis.ma_pullback import ma_pullback_signal
from .data_source import upbit as upbit_mod
from .billing import plans as billing_plans
from . import performance
from .billing import toss as toss_client
from .billing import kakaopay as kakaopay_client
from .billing import payapp as payapp_client

# 검색 데이터소스: 증권(stock)은 네이버 금융, 가상자산(crypto)은 업비트
SEARCH_SOURCES = {"stock": naver_mod, "us_stock": naver_world_mod, "crypto": upbit_mod}
VALID_MARKETS = ("stock", "us_stock", "crypto")

BILLING_PERIOD_DAYS = 30
RUN_NOW_COOLDOWN_SECONDS = 60

app = FastAPI(title="AlphaTiming")


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Render 헬스체크용. DB/외부 API를 건드리지 않고 즉시 응답해서, 배포 시 새 인스턴스가
    실제로 요청을 받을 준비가 됐는지 빠르게 판단할 수 있게 합니다."""
    return {"status": "ok"}


app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "dev-insecure-secret-change-me"))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "app", "templates"))
templates.env.filters["from_json"] = lambda s: __import__("json").loads(s) if s else []
templates.env.filters["price"] = du.format_price
templates.env.filters["market_price"] = du.format_market_price


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
    # 오늘 하루 신호 변화가 없어 알림을 한 번도 못 받은 회원에게, 매일 오후 4시(KST)에
    # 관심종목 현재 상태(HOLD뿐이어도)를 한 번은 요약해서 보내줍니다.
    _scheduler.add_job(lambda: send_daily_digest(load_config()), "cron", hour=16, minute=0,
                        timezone="Asia/Seoul", id="daily_digest", replace_existing=True)
    # 오픈채팅방에 붙여넣을 "오늘의 신호" 홍보 문구를 매일 장 마감 후 관리자 카카오톡으로 발송
    _scheduler.add_job(lambda: send_promo_message(load_config()), "cron", hour=16, minute=10,
                        timezone="Asia/Seoul", id="promo_message", replace_existing=True)
    _scheduler.start()
    # refresh_market_snapshot()은 네이버/CoinGecko로 나가는 블로킹 HTTP 호출이 있어서,
    # 여기서 동기 호출하면 그만큼 앱이 "준비 완료" 신호를 늦게 보내 배포/재시작 때마다
    # Render 헬스체크 실패(502) 구간이 길어집니다. 스레드로 분리해 서버가 바로 요청을
    # 받을 수 있게 하고, 지수/시세는 조금 늦게 채워지도록 합니다.
    threading.Thread(target=refresh_market_snapshot, daemon=True).start()
    print(f"[스케줄러 시작] {interval}분 주기로 자동 분석을, 24시간 주기로 정기결제를, "
          f"매일 09시/15시(KST)에 시장 지수 스냅샷을, 매일 16시(KST)에 일일 요약 알림을, "
          f"매일 16시 10분(KST)에 오픈채팅 공유 문구를 갱신/실행합니다.")


@app.on_event("shutdown")
def on_shutdown():
    _scheduler.shutdown(wait=False)


def _check_admin(token: str, cfg: dict):
    if token != cfg.get("admin_token"):
        raise HTTPException(status_code=401, detail="관리자 토큰이 올바르지 않습니다.")


def _is_admin_session(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


def _next_analysis_run_iso():
    """대시보드 카운트다운(JS new Date())이 서버가 어느 타임존에서 도는지와 무관하게
    정확히 파싱하도록, UTC 오프셋을 포함한 ISO 문자열로 반환합니다. 예전에는 tzinfo를
    지운 naive 문자열을 반환해서, 서버가 UTC로 도는 배포 환경(Render)에서는 브라우저가
    이를 로컬시간(KST)으로 오인해 실제보다 9시간 전(=이미 지난 시각)으로 표시되는
    문제가 있었습니다."""
    job = _scheduler.get_job("analysis_cycle")
    if job and job.next_run_time:
        return job.next_run_time.astimezone(timezone.utc).isoformat()
    return None


def _market_snapshot_context():
    """히어로 카드(국내증권/해외증권/가상자산 선택 탭)에 표시할 대표 지수/시세. 방문할 때마다
    값이 바뀌지 않도록 실시간 조회가 아니라, 스케줄러가 하루 2회 갱신해둔 스냅샷을 읽습니다."""
    kospi_kosdaq_snapshot = db.get_market_snapshot("kospi_kosdaq")
    us_indices_snapshot = db.get_market_snapshot("us_indices")
    usd_crypto_snapshot = db.get_market_snapshot("usd_crypto")
    return {
        "market_indices": kospi_kosdaq_snapshot["data"] if kospi_kosdaq_snapshot else None,
        "us_market_indices": us_indices_snapshot["data"] if us_indices_snapshot else None,
        "usd_prices": usd_crypto_snapshot["data"] if usd_crypto_snapshot else None,
    }


# ----------------------------------------------------------------------
# 대시보드
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, market: str = "stock"):
    user = auth.current_user(request)
    if not user:
        return templates.TemplateResponse(request, "landing.html", {
            **_market_snapshot_context(),
            "stock_market_open": du.is_kr_market_open(),
            "us_market_open": du.is_us_market_open(),
            "plans": [billing_plans.get_plan(k) for k in billing_plans.PLAN_ORDER],
            "billing_enabled": _billing_enabled(),
        })
    if auth.needs_profile_completion(user):
        return RedirectResponse(url="/complete-profile", status_code=303)
    market = market if market in VALID_MARKETS else "stock"

    cfg = load_config()
    all_signals = db.get_latest_signals_for_dashboard()
    my_codes = {w["code"] for w in db.get_user_watchlist(user["id"], market=market)}
    signals = [s for s in all_signals if s["code"] in my_codes]
    watch_count = len(my_codes)

    summary = du.market_summary(signals)
    featured = du.pick_featured_stock(signals)
    timeline = du.build_signal_timeline(featured, market) if featured else []

    bell = du.bell_curve_path(featured.get("z_score") if featured else 0)
    mb_percentile = None
    if featured and featured.get("volume_ratio") is not None:
        # 거래량 비율(평균 대비 배수)을 0~100 백분위로 근사 매핑 (0.5배->20, 1배->50, 2배 이상->90)
        ratio = featured["volume_ratio"]
        mb_percentile = max(0, min(100, 50 + (ratio - 1) * 40))
    mb = du.maxwell_boltzmann_path(mb_percentile)
    recent_alerts = db.get_recent_notifications(6, codes=my_codes)
    analysis_feed = db.get_recent_signals_for_codes(list(my_codes), limit=30)

    return templates.TemplateResponse(request, "index.html", {
        "signals": signals,
        "summary": summary,
        "featured": featured,
        "timeline": timeline,
        "bell": bell,
        "mb": mb,
        "recent_alerts": recent_alerts,
        "analysis_feed": analysis_feed,
        **_market_snapshot_context(),
        "stock_market_open": du.is_kr_market_open(),
        "us_market_open": du.is_us_market_open(),
        "base_url": "/",
        "watch_count": watch_count,
        "interval": cfg["schedule"]["interval_minutes"],
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "next_run": _next_analysis_run_iso(),
        "user": user,
        "market": market,
        "plan": billing_plans.get_plan(db.get_user_plan_key(user["id"])),
        "ai_recommend_available": billing_plans.is_at_least(db.get_user_plan_key(user["id"]), "standard"),
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
    if auth.needs_profile_completion(user):
        return RedirectResponse(url="/complete-profile", status_code=303)
    market = market if market in VALID_MARKETS else "stock"

    plan = billing_plans.get_plan(db.get_user_plan_key(user["id"]))
    watch_list = db.get_user_watchlist(user["id"], market=market)
    return templates.TemplateResponse(request, "watchlist.html", {
        "watch_list": watch_list,
        "user": user,
        "plan": plan,
        "market": market,
        **_market_snapshot_context(),
        "stock_market_open": du.is_kr_market_open(),
        "us_market_open": du.is_us_market_open(),
        "base_url": "/watchlist",
        "limit_label": billing_plans.stock_limit_label(plan),
        "at_limit": plan["stock_limit"] is not None and len(watch_list) >= plan["stock_limit"],
        "error": error,
    })


@app.post("/watchlist/add")
def watchlist_add(request: Request, code: str = Form(...), name: str = Form(...), market: str = Form("stock")):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/watchlist", status_code=303)
    market = market if market in VALID_MARKETS else "stock"

    plan = billing_plans.get_plan(db.get_user_plan_key(user["id"]))
    current_count = db.count_user_watchlist(user["id"], market=market)
    if plan["stock_limit"] is not None and current_count >= plan["stock_limit"]:
        label = {"crypto": "가상자산", "us_stock": "해외증권"}.get(market, "증권")
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
    market = market if market in VALID_MARKETS else "stock"
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
    user = db.get_user_by_id(user_id)
    if auth.needs_profile_completion(user):
        # 카카오 로그인은 닉네임 동의만 받아서 이름/전화번호가 비어있습니다.
        # 회원가입과 동일하게, 서비스를 쓰기 전에 이 정보를 먼저 받습니다.
        return RedirectResponse(url="/complete-profile", status_code=303)
    return RedirectResponse(url="/account", status_code=303)


# ----------------------------------------------------------------------
# 카카오 계정 연결 (이메일로 가입한 기존 계정에 카카오 로그인을 추가로 연결.
# 위 /auth/kakao/login 은 "가입/로그인"용이라 새 계정을 만들지만, 이건 지금
# 로그인되어 있는 계정에 kakao_id만 붙여서 다음부터 그 계정으로 카카오 로그인이
# 되게 합니다. 카카오 개발자 콘솔의 Redirect URI 목록에 이 콜백 주소도 등록해야 합니다.)
# ----------------------------------------------------------------------
@app.get("/account/kakao/link")
def kakao_link_start(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/account", status_code=303)

    cfg = load_config()
    rest_api_key = cfg["kakao"].get("rest_api_key")
    if not rest_api_key:
        return HTMLResponse("환경변수 KAKAO_REST_API_KEY가 설정되어 있지 않습니다.", status_code=400)

    redirect_uri = str(request.base_url).rstrip("/") + "/account/kakao/link/callback"
    url = kakao_mod.get_authorize_url(rest_api_key, redirect_uri, scope="profile_nickname")
    return RedirectResponse(url)


@app.get("/account/kakao/link/callback", response_class=HTMLResponse)
def kakao_link_callback(request: Request, code: str = ""):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/account", status_code=303)

    cfg = load_config()
    rest_api_key = cfg["kakao"].get("rest_api_key")
    redirect_uri = str(request.base_url).rstrip("/") + "/account/kakao/link/callback"

    try:
        token_data = kakao_mod.exchange_code_for_token(rest_api_key, code, redirect_uri, cfg["kakao"].get("client_secret", ""))
        profile = kakao_mod.get_user_profile(token_data["access_token"])
    except Exception as e:
        return RedirectResponse(url=f"/account?error={quote('카카오 계정 연결에 실패했습니다: ' + str(e))}", status_code=303)

    kakao_id = str(profile.get("id"))
    existing = db.get_user_by_kakao_id(kakao_id)
    if existing and existing["id"] != user["id"]:
        return RedirectResponse(
            url=f"/account?error={quote('이미 다른 계정에 연결된 카카오 계정입니다.')}", status_code=303
        )

    db.link_kakao_id(user["id"], kakao_id)
    return RedirectResponse(url="/account?kakao_linked=1", status_code=303)


# ----------------------------------------------------------------------
# 추가 정보 입력 (카카오 최초 가입 시 이름/전화번호가 비어있는 경우 강제로 받습니다)
# ----------------------------------------------------------------------
@app.get("/complete-profile", response_class=HTMLResponse)
def complete_profile_page(request: Request, error: str = ""):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/complete-profile", status_code=303)
    if not auth.needs_profile_completion(user):
        return RedirectResponse(url="/account", status_code=303)
    return templates.TemplateResponse(request, "complete_profile.html", {"user": user, "error": error})


@app.post("/complete-profile")
def complete_profile_submit(request: Request, name: str = Form(...), phone: str = Form(...)):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/complete-profile", status_code=303)
    if not name.strip() or not phone.strip():
        return RedirectResponse(
            url=f"/complete-profile?error={quote('이름과 전화번호를 모두 입력해주세요')}", status_code=303
        )
    db.update_user_profile(user["id"], name, phone)
    return RedirectResponse(url="/account", status_code=303)


# ----------------------------------------------------------------------
# 내 계정 (구독 현황 + 카카오 '나에게 채팅' 연동 관리)
# ----------------------------------------------------------------------
@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, subscribed: int = 0, canceled: int = 0, downgrade_scheduled: int = 0,
                  test_sent: int = 0, test_failed: int = 0, kakao_linked: int = 0, error: str = ""):
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
        "test_sent": test_sent, "test_failed": test_failed, "kakao_linked": kakao_linked, "error": error,
    })


# ----------------------------------------------------------------------
# 요금제 / 구독 결제 (Toss Payments 정기결제 — 빌링키 발급 후 매월 자동 청구)
# ----------------------------------------------------------------------
@app.get("/performance", response_class=HTMLResponse)
def performance_page(request: Request):
    """공개 트랙레코드 — 모든 BUY/SELL 신호의 이후 5/20거래일 수익률과 적중률.
    로그인 없이 볼 수 있습니다 (신뢰 구축이 목적이므로 일부러 공개)."""
    stats = performance.get_cached_or_compute()
    return templates.TemplateResponse(request, "performance.html", {
        "stats": stats,
        "user": auth.current_user(request),
    })


def _billing_enabled() -> bool:
    """유료 결제 오픈 여부. 유사투자자문업 신고가 끝날 때까지 결제 진입점을 잠가둡니다.
    Render 환경변수 BILLING_ENABLED=true 로 바꾸면 코드 수정 없이 유료 플랜이 열립니다."""
    return os.getenv("BILLING_ENABLED", "false").strip().lower() in ("1", "true", "yes", "y")


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
        "billing_enabled": _billing_enabled(),
    })


@app.post("/api/waitlist/click")
def waitlist_click(plan: str = Form(...)):
    """'오픈 알림 받기' 버튼을 누른(모달을 연) 시점을 플랜별로 집계합니다. 유사투자자문업
    신고 절차를 시작할지 판단하는 핵심 지표라, 신청 완료 여부와 무관하게 남깁니다."""
    if plan in billing_plans.PLAN_ORDER:
        db.log_waitlist_click(plan)
    return JSONResponse({"ok": True})


@app.post("/api/waitlist/signup")
def waitlist_signup(email: str = Form(...), plan: str = Form(...),
                     privacy_consent: str = Form(""), marketing_consent: str = Form("")):
    if plan not in billing_plans.PLAN_ORDER:
        raise HTTPException(status_code=400, detail="잘못된 플랜입니다.")
    if privacy_consent != "on":
        raise HTTPException(status_code=400, detail="개인정보 수집·이용에 동의해주세요.")
    email = email.strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="올바른 이메일 주소를 입력해주세요.")
    db.add_waitlist_signup(email, plan, marketing_consent == "on")
    return JSONResponse({"ok": True})


@app.get("/billing/checkout", response_class=HTMLResponse)
def billing_checkout(request: Request, plan: str = ""):
    if not _billing_enabled():
        return RedirectResponse(url="/pricing", status_code=303)
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


# ----------------------------------------------------------------------
# 카카오페이 정기결제 (Toss와 달리 결제창이 별도 사이트로 리디렉션되는 방식)
# ----------------------------------------------------------------------
@app.get("/billing/checkout/kakao")
def billing_checkout_kakao(request: Request, plan: str = ""):
    if not _billing_enabled():
        return RedirectResponse(url="/pricing", status_code=303)
    user = auth.current_user(request)
    if not user:
        next_url = quote(f"/billing/checkout/kakao?plan={plan}")
        return RedirectResponse(url=f"/login?next={next_url}", status_code=303)

    plan_info = billing_plans.get_plan(plan)
    if plan_info["key"] == "free" or plan_info["price"] <= 0:
        return RedirectResponse(url="/pricing", status_code=303)

    if not kakaopay_client.is_configured():
        return HTMLResponse(
            "환경변수 KAKAO_PAY_CID / KAKAO_PAY_SECRET_KEY가 설정되어 있지 않습니다. Render 환경변수를 확인해주세요.",
            status_code=400,
        )

    order_id = f"sub-{user['id']}-{uuid.uuid4().hex[:12]}"
    partner_user_id = f"user-{user['id']}"
    base = str(request.base_url).rstrip("/")
    try:
        ready = kakaopay_client.ready_subscription(
            order_id, partner_user_id, f"AlphaTiming {plan_info['name']} 플랜 구독", plan_info["price"],
            approval_url=f"{base}/billing/kakao/approve?plan={plan_info['key']}&order_id={order_id}",
            fail_url=f"{base}/billing/fail",
            cancel_url=f"{base}/billing/fail?message={quote('결제가 취소되었습니다')}",
        )
    except kakaopay_client.KakaoPayError as e:
        return RedirectResponse(url=f"/billing/fail?message={quote(str(e))}", status_code=303)

    return RedirectResponse(url=ready["next_redirect_pc_url"], status_code=303)


@app.get("/billing/kakao/approve", response_class=HTMLResponse)
def billing_kakao_approve(request: Request, plan: str = "", order_id: str = "", pg_token: str = ""):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/pricing", status_code=303)

    plan_info = billing_plans.get_plan(plan)
    if not order_id or not pg_token:
        return RedirectResponse(url=f"/billing/fail?message={quote('인증 정보가 올바르지 않습니다')}", status_code=303)

    partner_user_id = f"user-{user['id']}"
    try:
        approved = kakaopay_client.approve_subscription(order_id, partner_user_id, pg_token)
    except kakaopay_client.KakaoPayError as e:
        return RedirectResponse(url=f"/billing/fail?message={quote(str(e))}", status_code=303)

    sid = approved["sid"]
    now = datetime.now()
    period_end = now + timedelta(days=BILLING_PERIOD_DAYS)
    sub_id = db.create_subscription(
        user["id"], plan_info["key"], plan_info["price"], sid, partner_user_id,
        now.isoformat(), period_end.isoformat(), provider="kakao",
    )
    db.log_payment(user["id"], sub_id, order_id, plan_info["key"], plan_info["price"],
                    "paid", approved.get("payment_method_type", "card").lower(), "")

    return RedirectResponse(url="/account?subscribed=1", status_code=303)


@app.post("/billing/cancel")
def billing_cancel(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/account", status_code=303)

    sub = db.get_active_subscription(user["id"])
    if sub and sub.get("billing_key"):
        try:
            if sub.get("provider") == "kakao":
                kakaopay_client.inactivate_subscription(sub["billing_key"])
            elif sub.get("provider") == "payapp":
                payapp_client.cancel_subscription(sub["billing_key"])
        except (kakaopay_client.KakaoPayError, payapp_client.PayAppError):
            pass  # 결제사 쪽 해지가 실패해도 우리 쪽 구독 상태는 계속 취소 처리합니다.

    db.cancel_subscription(user["id"])
    return RedirectResponse(url="/account?canceled=1", status_code=303)


# ----------------------------------------------------------------------
# PayApp 정기결제 (등록 후에는 PayApp이 스스로 주기 청구하고 feedbackurl로만 통보합니다.
# 즉 우리 스케줄러가 직접 "청구"를 호출하지 않는, Toss/카카오페이와는 다른 방식입니다.)
# ----------------------------------------------------------------------
@app.get("/billing/checkout/payapp")
def billing_checkout_payapp(request: Request, plan: str = ""):
    if not _billing_enabled():
        return RedirectResponse(url="/pricing", status_code=303)
    user = auth.current_user(request)
    if not user:
        next_url = quote(f"/billing/checkout/payapp?plan={plan}")
        return RedirectResponse(url=f"/login?next={next_url}", status_code=303)

    plan_info = billing_plans.get_plan(plan)
    if plan_info["key"] == "free" or plan_info["price"] <= 0:
        return RedirectResponse(url="/pricing", status_code=303)

    if not payapp_client.is_configured():
        return HTMLResponse(
            "환경변수 PAYAPP_USERID / PAYAPP_LINKKEY가 설정되어 있지 않습니다. Render 환경변수를 확인해주세요.",
            status_code=400,
        )
    if not user.get("phone"):
        return RedirectResponse(url="/account?error=" + quote("PayApp 결제는 휴대전화번호 등록이 필요합니다."), status_code=303)

    base = str(request.base_url).rstrip("/")
    expire_date = (datetime.now() + timedelta(days=365 * 5)).strftime("%Y-%m-%d")
    try:
        registered = payapp_client.register_subscription(
            f"AlphaTiming {plan_info['name']} 플랜 구독", plan_info["price"], user["phone"],
            cycle_day=datetime.now().day,
            expire_date=expire_date,
            feedbackurl=f"{base}/billing/payapp/feedback",
            failurl=f"{base}/billing/fail",
            var1=str(user["id"]), var2=plan_info["key"],
        )
    except payapp_client.PayAppError as e:
        return RedirectResponse(url=f"/billing/fail?message={quote(str(e))}", status_code=303)

    return RedirectResponse(url=registered["payurl"], status_code=303)


@app.post("/billing/payapp/feedback")
async def billing_payapp_feedback(request: Request):
    form = await request.form()
    data = dict(form)

    if data.get("linkval") != payapp_client.linkval():
        return PlainTextResponse("FAIL", status_code=400)

    rebill_no = data.get("rebill_no")
    if not rebill_no:
        return PlainTextResponse("SUCCESS")  # 정기결제 건이 아니면 이 흐름에서는 처리하지 않음

    # pay_state (PayApp 결제통보): 4=결제완료, 8/32=요청취소(결제 전), 9/64=승인취소(환불),
    # 70/71=부분취소, 99=정기결제 승인 실패(2회차 이후)
    pay_state = data.get("pay_state", "")
    now = datetime.now()
    mul_no = data.get("mul_no", "")
    raw = json.dumps(data, ensure_ascii=False)
    existing = db.get_subscription_by_billing_key(rebill_no)

    if pay_state == "4":
        period_end = now + timedelta(days=BILLING_PERIOD_DAYS)
        if existing:
            db.renew_subscription(existing["id"], now.isoformat(), period_end.isoformat())
            db.log_payment(existing["user_id"], existing["id"], mul_no, existing["plan"],
                            int(data.get("price", existing["price"])), "paid", "card", raw)
        else:
            var1, var2 = data.get("var1", ""), data.get("var2", "")
            plan_info = billing_plans.get_plan(var2) if var2 else None
            if not var1.isdigit() or not plan_info:
                return PlainTextResponse("SUCCESS")
            user_id = int(var1)
            sub_id = db.create_subscription(
                user_id, plan_info["key"], plan_info["price"], rebill_no, f"user-{user_id}",
                now.isoformat(), period_end.isoformat(), provider="payapp",
            )
            db.log_payment(user_id, sub_id, mul_no, plan_info["key"], plan_info["price"], "paid", "card", raw)

    elif pay_state in ("9", "64") and existing:
        # 환불(승인취소): 구독을 즉시 종료하고, 이후 자동 청구가 계속되지 않도록 PayApp 쪽 정기결제도 해지
        db.cancel_subscription(existing["user_id"])
        try:
            payapp_client.cancel_subscription(rebill_no)
        except payapp_client.PayAppError:
            pass  # 이미 PayApp 쪽에서 해지된 건이면 실패할 수 있음 — 우리 쪽 취소는 유지
        db.log_payment(existing["user_id"], existing["id"], mul_no, existing["plan"],
                        -abs(int(data.get("price", existing["price"]) or 0)), "refunded", "card", raw)

    elif pay_state in ("70", "71") and existing:
        # 부분취소: 일부 금액만 환불된 것이므로 구독은 유지하고 이력만 남김
        db.log_payment(existing["user_id"], existing["id"], mul_no, existing["plan"],
                        -abs(int(data.get("cancel_price", data.get("price", 0)) or 0)), "partial_refund", "card", raw)

    elif pay_state == "99" and existing:
        # 정기결제 승인 실패(2회차 이후): 구독을 미납 상태로 표시 (관리자 화면에서 past_due로 확인 가능)
        db.mark_subscription_past_due(existing["id"])
        db.log_payment(existing["user_id"], existing["id"], mul_no, existing["plan"],
                        int(data.get("price", existing["price"]) or 0), "failed", "card", raw)

    # 8/32(결제 전 요청취소) 등 나머지 상태는 활성 구독에 영향 없음
    return PlainTextResponse("SUCCESS")


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
def run_now(request: Request, background_tasks: BackgroundTasks, market: str = "stock",
            x_admin_token: str = Header(default="")):
    cfg = load_config()
    user = auth.current_user(request)
    if not user and not _is_admin_session(request):
        _check_admin(x_admin_token, cfg)

    last_run = db.get_last_analysis_time()
    if last_run:
        elapsed = (datetime.now() - datetime.fromisoformat(last_run)).total_seconds()
        if elapsed < RUN_NOW_COOLDOWN_SECONDS:
            wait = int(RUN_NOW_COOLDOWN_SECONDS - elapsed)
            raise HTTPException(status_code=429, detail=f"{wait}초 후 다시 시도해주세요.")

    if user:
        # 개인 사용자 버튼이므로 전체 회원 데이터가 아니라 본인의 관심종목(해당 market)만
        # 재분석합니다. 전체 유니버스 재분석은 스케줄러(30분 주기)와 관리자 트리거 몫입니다.
        market = market if market in VALID_MARKETS else "stock"
        watchlist = db.get_user_watchlist(user["id"], market=market)
        background_tasks.add_task(run_analysis_for_watchlist, cfg, market, watchlist)
    else:
        background_tasks.add_task(run_analysis_cycle, cfg)
    return JSONResponse({"started": True, "requested_at": datetime.now().isoformat()})


@app.get("/api/last-run")
def api_last_run():
    """'지금 갱신' 폴링용 완료 신호. signals 테이블의 MAX(created_at)은 사이클 도중 아무
    종목이나 하나 저장될 때마다 계속 갱신되어 조기 완료로 오판되므로, 사이클(전체 또는
    개인 스코프) 전체가 끝난 시점에만 갱신되는 last_full_analysis 스냅샷을 사용합니다."""
    snapshot = db.get_market_snapshot("last_full_analysis")
    return {"last_run": snapshot["updated_at"] if snapshot else None, "next_run": _next_analysis_run_iso()}


# ----------------------------------------------------------------------
# API (JSON)
# ----------------------------------------------------------------------
@app.get("/api/signals")
def api_signals():
    return db.get_latest_signals_for_dashboard()


@app.get("/api/ai-recommend")
def api_ai_recommend(request: Request, group: str = "kospi"):
    """AI 추천 종목(코스피/코스닥 TOP5, 스탠다드 플랜 이상 전용). 캐시가 1시간 넘게 지났으면
    백그라운드에서 재스캔을 시작하고, 스캔이 끝날 때까지는 이전 캐시(있다면)와 함께
    scanning=true를 반환합니다 - 프론트에서 주기적으로 polling해 갱신합니다."""
    user = auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    if not billing_plans.is_at_least(db.get_user_plan_key(user["id"]), "standard"):
        raise HTTPException(status_code=403, detail="스탠다드 플랜부터 이용 가능한 기능입니다.")

    group_key = group.upper()
    if group_key not in ai_recommend.GROUPS:
        raise HTTPException(status_code=400, detail="지원하지 않는 종목군입니다.")

    ai_recommend.trigger_scan_if_stale(group_key, load_config())
    snapshot = ai_recommend.get_cached(group_key)
    return {
        "group": group_key,
        "scanning": ai_recommend.is_scanning(group_key),
        "updated_at": snapshot["updated_at"] if snapshot else None,
        "items": snapshot["data"] if snapshot else [],
    }


@app.get("/api/ma-pullback")
def api_ma_pullback(request: Request, market: str = "stock"):
    """이동평균선(10/20/50일) 정배열·역배열 + 눌림목 전략을 로그인한 사용자의 관심종목
    (해당 market)에 대해 계산합니다. 기존 5요소 신호 엔진과는 완전히 별개의 전략입니다.
    관심종목 수가 적어(플랜당 최대 몇~10여 개) AI 추천처럼 백그라운드 스캔+캐시 없이
    동기로 바로 계산해 반환합니다."""
    user = auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    market = market if market in VALID_MARKETS else "stock"

    data_source = SEARCH_SOURCES.get(market, naver_mod)
    fetch_ohlcv = getattr(data_source, "get_daily_ohlcv_fast", data_source.get_daily_ohlcv)
    watchlist = db.get_user_watchlist(user["id"], market=market)
    cfg = load_config()

    items = []
    for stock in watchlist:
        code, name = stock["code"], stock["name"]
        try:
            rows = fetch_ohlcv(code, cfg["lookback_days"])
            closes = [r["close"] for r in rows]
            result = ma_pullback_signal(closes, cfg)
        except Exception as e:
            print(f"[MA눌림목 오류] {name}({code}): {e}")
            continue
        items.append({"code": code, "name": name, **result})

    return {"market": market, "items": items}


# ----------------------------------------------------------------------
# 카카오톡 '나에게 채팅' 연동 (로그인한 사용자 본인 계정에 연결)
# ----------------------------------------------------------------------
@app.get("/kakao/authorize")
def kakao_authorize(request: Request, return_to: str = ""):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/account", status_code=303)

    cfg = load_config()
    rest_api_key = cfg["kakao"].get("rest_api_key")
    if not rest_api_key:
        return HTMLResponse("환경변수 KAKAO_REST_API_KEY 가 설정되어 있지 않습니다. Render 환경변수를 확인해주세요.", status_code=400)

    # 관리자 공유 문구 페이지에서 연동을 시작한 경우, OAuth 왕복 후 그 페이지로
    # 돌아가게 세션에 표시해둡니다 (redirect_uri는 카카오에 등록된 값 그대로 유지).
    if return_to == "admin":
        request.session["kakao_return_to"] = "admin"
    else:
        request.session.pop("kakao_return_to", None)

    redirect_uri = str(request.base_url) + "kakao/callback"
    url = kakao_mod.get_authorize_url(rest_api_key, redirect_uri)
    return RedirectResponse(url)


@app.get("/kakao/callback", response_class=HTMLResponse)
def kakao_callback(request: Request, code: str = ""):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/account", status_code=303)

    # 연동을 관리자 공유 문구 페이지에서 시작했으면 결과(성공/실패)도 그쪽으로 돌려보냅니다.
    dest = "/admin/promo" if request.session.pop("kakao_return_to", "") == "admin" else "/account"

    cfg = load_config()
    rest_api_key = cfg["kakao"].get("rest_api_key")
    redirect_uri = str(request.base_url) + "kakao/callback"
    try:
        token_data = kakao_mod.exchange_code_for_token(rest_api_key, code, redirect_uri, cfg["kakao"].get("client_secret", ""))
    except Exception as e:
        return HTMLResponse(f"카카오 연동에 실패했습니다: {e}", status_code=400)

    # "카카오톡 메시지 전송" 동의항목이 선택 동의로 설정되어 있으면, 사용자가 동의 화면에서
    # 그 항목 체크를 해제(또는 무시)한 채로도 로그인 자체는 성공할 수 있습니다. 그 경우
    # access_token은 발급되지만 메시지 전송 권한이 없어 실제 알림은 계속 실패하므로,
    # 여기서 발급된 scope에 talk_message가 포함됐는지 확인해 "연동됨"으로 잘못 표시하지 않게 합니다.
    granted_scopes = (token_data.get("scope") or "").split()
    if "talk_message" not in granted_scopes:
        return RedirectResponse(
            url=f"{dest}?error={quote('카카오톡 메시지 전송 권한에 동의하지 않아 연동이 완료되지 않았습니다. 다시 시도할 때 카카오 동의 화면에서 카카오톡 메시지 전송 항목에 동의해주세요.')}",
            status_code=303,
        )

    db.save_user_kakao_tokens(user["id"], token_data.get("access_token"), token_data.get("refresh_token"))
    return RedirectResponse(url=f"{dest}?connected=1", status_code=303)


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


@app.get("/admin/promo", response_class=HTMLResponse)
def admin_promo_page(request: Request, sent: str = "", connected: str = "", error: str = ""):
    """오픈채팅방에 붙여넣을 '오늘의 신호' 홍보 문구. 카카오 연동/발송까지 이 페이지에서
    전부 처리합니다 (관리자 워크플로우가 일반 사용자 페이지를 거치지 않도록)."""
    if not _is_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    user = auth.current_user(request)
    msgs = build_promo_messages()
    return templates.TemplateResponse(request, "admin_promo.html", {
        "msgs": msgs,
        "sent": sent,
        "connected": connected,
        "error": error,
        "user": user,
        "kakao_connected": bool(user and user.get("kakao_access_token")),
    })


@app.post("/admin/promo/send")
def admin_promo_send(request: Request):
    """'카카오로 보내기' 버튼 - 현재 로그인한 관리자 본인의 '나와의 채팅'으로 축약본 발송."""
    if not _is_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    user = auth.current_user(request)
    if not user or not user.get("kakao_access_token"):
        return RedirectResponse(url="/admin/promo?sent=no_kakao", status_code=303)
    msgs = build_promo_messages()
    ok = kakao_mod.notify_user(user, "PROMO", msgs["kakao"], load_config())
    return RedirectResponse(url=f"/admin/promo?sent={'ok' if ok else 'fail'}", status_code=303)


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
        "waitlist": db.get_waitlist_stats(),
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


@app.post("/admin/members/{user_id}/profile")
def admin_update_profile(request: Request, user_id: int, name: str = Form(""), phone: str = Form("")):
    """관리자가 카카오 가입 등으로 이름/전화번호가 비어있거나 잘못 들어간 회원 정보를 수정합니다."""
    if not _is_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    db.update_user_profile(user_id, name, phone)
    return RedirectResponse(url="/admin/members", status_code=303)
