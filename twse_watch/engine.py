"""三階段引擎：串接資料層 → 指標 → 規則 → 處置認定 → 反推。"""
from __future__ import annotations

from dataclasses import asdict

from . import source as src
from .indicators import compute_metrics
from .rules import evaluate, Thresholds
try:  # disposition.py 為主；若該檔在某些檔案系統異常則改用乾淨備援 dispo.py
    from .disposition import assess
except (ValueError, SyntaxError, ImportError):
    from .dispo import assess
from .reverse import reverse_scenarios


def analyze(stock_no: str, months: int = 6) -> dict:
    stock_no = stock_no.strip()
    sd = src.fetch_stock(stock_no, months=months)

    result = {
        "code": sd.code,
        "name": sd.name,
        "market": sd.market,
        "market_label": {"TWSE": "上市", "TPEX": "上櫃", "EMERGING": "興櫃",
                         "UNKNOWN": "未知"}.get(sd.market, sd.market),
        "warnings": list(sd.warnings),
        "as_of": sd.bars[-1].date.isoformat() if sd.bars else None,
        "shares_outstanding": sd.shares_outstanding,
    }

    if not sd.bars:
        result["stage"] = "NO_DATA"
        result["headline"] = "查無日成交資料，無法分析。"
        return result

    # 指標 + 規則
    m = compute_metrics(sd.bars, sd.shares_outstanding)
    crits = evaluate(m, Thresholds(), market_avg=0.0,
                     shares_outstanding=sd.shares_outstanding)
    triggered = [c for c in crits if c.triggered]
    today_is_attention = len(triggered) > 0

    # 處置認定（第一、二階）
    dview = assess(stock_no, today_is_attention)

    # 第三階反推
    scenarios = reverse_scenarios(sd.bars, sd.shares_outstanding)

    result.update({
        "stage": dview.stage,
        "headline": dview.headline,
        "metrics": _metrics_public(m),
        "criteria": [asdict(c) for c in crits],
        "triggered_criteria": [c.key for c in triggered],
        "today_is_attention": today_is_attention,
        "disposition": {
            "official": dview.official_disposition,
            "attention_count": dview.attention_count,
            "attention_window": dview.attention_window,
            "distance_to_disposition": dview.distance_to_disposition,
            "notes": dview.notes,
        },
        "reverse_scenarios": [asdict(s) for s in scenarios],
        "stage_explanation": {
            "stage1": "抓官方既成注意/處置公告；已公告處置日期即結束。",
            "stage2": "將注意/處置標準公式化，判斷今日踩到哪幾款、距處置還差幾次。",
            "stage3": "反推明日臨界收盤價/週轉率/量能（市場與類股平均不變假設下）。",
        },
        "disclaimer": (
            "本工具依官方公開資料與『注意交易資訊暨處置作業要點』推算，"
            "含市場/類股平均之近似，僅供研究參考，實際以證交所/櫃買中心公告為準，非投資建議。"),
    })
    return result


def _metrics_public(m) -> dict:
    d = asdict(m)
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in d.items()}
