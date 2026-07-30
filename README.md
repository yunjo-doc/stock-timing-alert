# 주식 투자 타이밍 알리미 (웹앱 버전)

정규분포(확률분포) + 맥스웰-볼츠만 응용(시장온도) + 추세 + 펀더멘털 + 리스크관리를
결합해 종목별 매수/매도 타이밍 신호를 보여주는 웹 대시보드입니다.

이전 버전(키움 OpenAPI+ 데스크톱 프로그램)에서 만든 5개 분석 로직을 그대로
재사용했고, 데이터 소스만 **네이버 금융 크롤링**으로 교체해 로그인 없이
서버에서 바로 동작하도록 만들었습니다.

> ⚠️ 네이버 금융 크롤링은 비공식 방식입니다. 네이버가 페이지 구조를 바꾸면
> `app/data_source/naver.py` 의 파싱 로직을 손봐야 할 수 있습니다.
> 이 신호는 통계적 참고 지표이며 투자 조언이 아닙니다.

## 화면 구성

- `/` : 대시보드 — 관심종목별 최신 신호(BUY/SELL/HOLD) 카드
- `/watchlist` : 관심종목 추가/삭제
- `/notifications` : 매수/매도 알림 이력
- `/api/signals` : JSON API (외부 연동용)

## 로컬에서 먼저 테스트해보기

```bash
git clone <이 저장소 주소>
cd <저장소 폴더>
pip install -r requirements.txt
cp .env.example .env      # ADMIN_TOKEN 등 값 채우기
uvicorn app.main:app --reload
```

브라우저에서 http://127.0.0.1:8000 접속.

첫 실행 시 아직 분석 데이터가 없으므로 "아직 분석 결과가 없습니다" 라고 나옵니다.
관리자 토큰으로 즉시 1회 실행해볼 수 있습니다:

```bash
curl -X POST http://127.0.0.1:8000/run-now -H "x-admin-token: <.env의 ADMIN_TOKEN 값>"
```

(스케줄러가 `config.json` 의 `schedule.interval_minutes` 주기로 자동으로도 실행합니다.)

## GitHub에 올리기

```bash
cd <이 폴더>
git init
git add .
git commit -m "주식 투자 타이밍 알리미 웹앱 최초 커밋"
git branch -M main
git remote add origin https://github.com/<본인계정>/<저장소이름>.git
git push -u origin main
```

`.gitignore` 에 `.env` 와 `app/data/`(로컬 DB 파일)가 이미 제외되어 있어
민감정보나 개인 데이터가 실수로 올라가지 않습니다.

## Render.com에 무료 배포하기

1. https://render.com 가입 (GitHub 계정으로 로그인 가능)
2. **New +** → **Web Service** → 방금 만든 GitHub 저장소 선택
3. Render가 저장소의 `render.yaml` 을 자동 인식합니다. 확인 후 **Apply**
4. 배포 전에 **Environment** 탭에서 아래 값을 설정하세요:
   - `ADMIN_TOKEN` : 아무 임의의 긴 문자열 (예: openssl rand -hex 16 결과)
   - `KAKAO_ENABLED` : 처음엔 `false`
   - (카카오 연동 시 나중에 `KAKAO_REST_API_KEY` 등 추가)
5. **Deploy** 클릭 → 몇 분 뒤 `https://<서비스이름>.onrender.com` 주소로 접속 가능

이후 GitHub `main` 브랜치에 push할 때마다 Render가 자동으로 재배포합니다.

> Render 무료 플랜은 일정 시간 트래픽이 없으면 인스턴스가 슬립 모드로
> 들어가고, 다음 요청 시 다시 깨어나는 데 수십 초가 걸릴 수 있습니다.
> 스케줄러도 슬립 중에는 동작하지 않으므로, 24시간 자동분석이 꼭 필요하면
> Render의 유료 플랜(슬립 없음)이나 Railway/Fly.io 등 다른 서비스를 고려하세요.

## 카카오톡 알림 연동 (사용자별 로그인 연동)

이제 카카오 연동은 **각 로그인한 사용자가 본인 계정에서 직접** 연결합니다.
(서버 하나에 토큰 하나가 아니라, 여러 사람이 각자 로그인해서 각자의 카카오
'나와의 채팅'에 알림을 받을 수 있습니다.)

**앱 관리자가 최초 1회만 할 일:**
1. https://developers.kakao.com 에서 애플리케이션 생성
2. 카카오 로그인 활성화 + 동의항목에서 `talk_message` 체크
3. **플랫폼 설정 → Web** 에 사이트 도메인 추가 (예: `https://<서비스이름>.onrender.com`)
4. **카카오 로그인 → Redirect URI** 에 아래 주소 추가:
   `https://<서비스이름>.onrender.com/kakao/callback`
5. REST API 키를 Render 환경변수 `KAKAO_REST_API_KEY` 에 등록 후 재배포
6. Render 환경변수에 `SESSION_SECRET`(로그인 세션 암호화용 임의의 긴 문자열)도 함께 등록

**각 사용자가 할 일 (관리자 포함):**
1. 사이트 접속 → **로그인** → 없으면 **회원가입** (이메일/비밀번호)
2. 상단 네비게이션의 본인 이메일 클릭 → **내 계정** 페이지
3. **"카카오로 연동하기"** 버튼 클릭 → 카카오 로그인 화면에서 동의
4. 자동으로 `/account` 로 돌아오며 "연결됨" 표시로 바뀜
5. 이후 관심종목에서 매수/매도 신호가 뜨면 본인의 카카오톡 '나와의 채팅'으로 알림이 옵니다
6. 연동을 끊고 싶으면 같은 페이지에서 **"연동 해제"**

access_token은 약 6시간 후 만료되는데, 알림 전송 시점에 만료되어 있으면
refresh_token으로 서버가 자동 갱신을 시도합니다. 만약 refresh_token까지
만료(약 2달)됐다면 `/account` 에서 다시 연동해주세요.

## 폴더 구조

```
webapp/
├── app/
│   ├── main.py              # FastAPI 앱 (라우트)
│   ├── config.py             # 설정 로더 (config.json + 환경변수)
│   ├── db.py                  # SQLite 저장소 (신호 이력 + 사용자 계정/카카오 토큰)
│   ├── auth.py                  # 로그인 세션 헬퍼
│   ├── scheduler.py               # 분석 사이클 실행 로직
│   ├── dashboard_utils.py          # 대시보드 집계/차트(SVG) 생성
│   ├── data_source/naver.py         # 네이버 금융 크롤링
│   ├── analysis/                     # 확률/시장온도/추세/펀더멘털/리스크 (6개 모듈)
│   ├── notify/kakao.py                # 카카오톡 알림 (사용자별 전송)
│   ├── templates/                      # 대시보드/로그인/계정 HTML (Jinja2)
│   └── static/style.css                 # 스타일
├── config.json                # 종목/임계값 설정 (민감정보 없음)
├── requirements.txt
├── render.yaml                 # Render 배포 설정
├── Procfile                     # (Railway 등 대체 배포용)
├── .env.example
└── .gitignore
```

## 주의사항 / 한계

- **네이버 금융 크롤링은 비공식**입니다. 페이지 구조 변경 시 파싱이 깨질 수
  있으며, 상업적 서비스로 확장 시에는 증권사 정식 API나 유료 시세 데이터
  제공업체 이용을 권장합니다.
- 무료 배포 환경(Render 무료 플랜 등)은 재배포/슬립 시 SQLite 파일이
  초기화될 수 있습니다. 장기 이력 보존이 중요하면 외부 DB(Supabase 등)
  연동을 고려하세요.
- 실제 매수/매도 주문 자동 실행 기능은 포함되어 있지 않습니다. 신호와
  알림까지만 제공하며, 실제 투자 판단과 실행은 사용자 본인의 몫입니다.
- 이 프로그램의 신호는 통계 모델 기반 참고 지표이며 금융 자문이 아닙니다.
