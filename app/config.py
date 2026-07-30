# -*- coding: utf-8 -*-
"""
설정 로더

- 종목/임계값/가중치 등 일반 설정 -> config.json
- 카카오 토큰 등 민감정보 -> 환경변수(.env)로 관리 (GitHub에 올라가면 안 되는 값들)
"""

import json
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 카카오 REST API 키는 앱 전체가 공유하는 자격증명이라 환경변수로 관리합니다.
    # 사용자별 access_token/refresh_token은 이제 DB(users 테이블)에 저장됩니다 (app/auth.py, /account 참고).
    cfg.setdefault("kakao", {})
    cfg["kakao"]["rest_api_key"] = os.getenv("KAKAO_REST_API_KEY", cfg["kakao"].get("rest_api_key", ""))

    cfg["admin_token"] = os.getenv("ADMIN_TOKEN", "change-me")

    return cfg


def save_config(cfg: dict):
    """watch_list 등 일반 설정 변경사항을 config.json에 반영 (민감정보 제외하고 저장)"""
    safe_cfg = json.loads(json.dumps(cfg))  # deep copy
    safe_cfg.pop("admin_token", None)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(safe_cfg, f, ensure_ascii=False, indent=2)
