# -*- coding: utf-8 -*-
"""구독 플랜 정의

- free     : 무료, 관심종목 1개
- basic    : 월 5,000원, 관심종목 5개
- standard : 월 10,000원, 관심종목 10개
- pro      : 월 30,000원, 관심종목 10개 초과(무제한)
"""

PLANS = {
    "free": {
        "key": "free", "name": "Free", "price": 0,
        "stock_limit": 1,
        "desc": "종목 1개의 투자 타이밍을 알려드립니다.",
    },
    "basic": {
        "key": "basic", "name": "베이직", "price": 5000,
        "stock_limit": 5,
        "desc": "관심종목 5개까지 신호를 받아보세요.",
    },
    "standard": {
        "key": "standard", "name": "스탠다드", "price": 10000,
        "stock_limit": 10,
        "desc": "관심종목 10개까지 신호를 받아보세요.",
    },
    "pro": {
        "key": "pro", "name": "프로", "price": 30000,
        "stock_limit": None,  # 10개 초과, 사실상 무제한
        "desc": "관심종목 10개 초과, 무제한으로 신호를 받아보세요.",
    },
}

PLAN_ORDER = ["free", "basic", "standard", "pro"]


def get_plan(key: str) -> dict:
    return PLANS.get(key, PLANS["free"])


def is_paid_plan(key: str) -> bool:
    return key in PLANS and PLANS[key]["price"] > 0


def stock_limit_label(plan: dict) -> str:
    return "무제한" if plan["stock_limit"] is None else f"{plan['stock_limit']}개"
