"""整合版 v0.4（analyzer3）：在 v0.3 之上補上第十三款（當沖比）實算，
並對『連續3日』處置做自算投影（不依賴官方 notetrans 即時快照）。

要點：
* 第十三款屬第4條第1項第13款，當沖明細來自 TWSE TWTB4U（見 daytrade.py）。
* 處置第6條第1項第2款（10日內6/30日內12）只計第1~8款，故第十三款不計入該兩項；
  但『連續3日』（第1款）一般計入各款，故以 daytrade 自算最近連續達第十三款日數來投影。
"""
from __future__ import annotations

from . import source as src
from .analyzer import compute_metrics, evaluate, reverse, thresholds_for, MARKET_LABEL, _r
from .dispo import assess
from .analyzer2 import project_disposition, _disposition_measures
from .daytrade import evaluate_k13, consecutive_k13_days


def _k13_reverse_scenario(k13: dict) -> dict:
    m = k13.get("metrics", {})
    return {
        "key": "k13", "title": "第十三款 當沖比",
        "target": "明日需『當日當沖比』與『近6日當沖比』同維持 > 60%（且當沖量>5000張、成交額>5億）。",
        "reachable_next_day": None,
        "note": ("最新基準日 %s：當日 %s%%、近6日 %s%%。當沖比取決於隔日當沖力道，"
                 "非價格可反推。") % (m.get("as_of", "-"), m.get("day_ratio"), m.get("cum6_ratio")),
        "numbers": m,
    }


def _k13_disposition_projection(consec: int, today_k13: bool) -> dict:
    """以自算的連續達第十三款日數，投影『連續3日』處置。"""
    proj = {"available": True, "rule": "連續3日", "consecutive_days": consec,
            "leads_to_disposition_tomorrow": None, "messages": []}
    if consec >= 3:
        proj["leads_to_disposition_tomorrow"] = True
        proj["messages"].append(
            "自算最近已『連續 %d 個營業日』達第十三款，符合『連續3個營業日』處置標準，預期公告處置。" % consec)
    elif consec == 2:
        proj["leads_to_disposition_tomorrow"] = True
        proj["messages"].append(
            "自算已連續 2 個營業日達第十三款；明日若仍維持當日及近6日當沖比 >60%，"
            "即連續3日 → 觸發處置（次一營業日起，第一次處置：人工約5分鐘撮合＋10/30張預收款券）。")
    elif consec == 1:
        proj["messages"].append(
            "自算最近 1 個營業日達第十三款；連續3日才處置，尚差 2 日（明日續達為連續2日）。")
    else:
        proj["messages"].append("最新基準日未達第十三款。")
    proj["messages"].append("註：此為以當沖明細自行回算，官方以實際公告為準；"
                            "第十三款不計入『最近10日內6次/30日內12次』(限第1~8款)。")
    return proj


def analyze(stock_no: str, months: int = 6) -> dict:
    stock_no = stock_no.strip()
    sd = src.fetch_stock(stock_no, months=months)
    t = thresholds_for(sd.market)

    result = {
        "code": sd.code, "name": sd.name, "market": sd.market,
        "market_label": MARKET_LABEL.get(sd.market, sd.market),
        "warnings": list(sd.warnings),
        "as_of": sd.bars[-1].date.isoformat() if sd.bars else None,
        "shares_outstanding": sd.shares_outstanding,
        "threshold_note": "本檔以 %s 門檻評估（款三累積漲跌門檻 %.0f%%）。"
                          % (MARKET_LABEL.get(sd.market, sd.market), t.k3_cum6),
    }
    if not sd.bars:
        result.update({"stage": "NO_DATA", "headline": "查無日成交資料，無法分析。",
                       "criteria": [], "reverse_scenarios": [],
                       "disposition": {"official": None, "distance_to_disposition": {}, "notes": []},
                       "disposition_projection": {"available": False, "messages": []}})
        return result

    m = compute_metrics(sd.bars, sd.shares_outstanding)
    crits = evaluate(m, t, market_avg=0.0)

    # === 補第十三款（僅上市，TWTB4U 為上市資料）===
    k13_proj = None
    if sd.market == "TWSE":
        k13 = evaluate_k13(sd.bars, stock_no, sd.shares_outstanding)
        crits = [k13 if c["key"] == "k13" else c for c in crits]
        consec = consecutive_k13_days(sd.bars, stock_no, sd.shares_outstanding)
        k13_proj = _k13_disposition_projection(consec, k13["triggered"])
    else:
        for c in crits:
            if c["key"] == "k13":
                c["detail"] = "當沖明細目前僅串接上市(TWSE)；上櫃請參官方公告。"

    triggered = [c for c in crits if c["triggered"]]
    today_is_attention = len(triggered) > 0
    dview = assess(stock_no, today_is_attention)
    scenarios = reverse(sd.bars, m, t)

    # 第十三款的反推情境
    k13c = next((c for c in crits if c["key"] == "k13"), None)
    if k13c and k13c["status"] in ("triggered", "not_triggered", "excluded") and k13c.get("metrics"):
        scenarios.append(_k13_reverse_scenario(k13c))

    proj = project_disposition(dview, scenarios)   # 款1-8 累積投影

    headline = dview.headline
    if proj.get("leads_to_disposition_tomorrow") and dview.stage != "DISPOSED":
        headline += "　⚠ 明日達標即進入處置（款1~8累積）。"
    if k13_proj and k13_proj.get("leads_to_disposition_tomorrow"):
        headline += "　⚠ 第十三款連續達標逼近『連續3日』處置。"
    elif k13c and k13c["triggered"]:
        headline += "　• 今日達第十三款（當沖比）。"

    result.update({
        "stage": dview.stage,
        "headline": headline,
        "metrics": {"close": m.close, "cum6_sum": _r(m.cum6_sum),
                    "oldest6_return": _r(m.oldest6_return), "vol_amp": _r(m.vol_amp),
                    "turnover_pct": _r(m.turnover_pct), "cum6_turnover_pct": _r(m.cum6_turnover_pct),
                    "span6_diff": _r(m.span6_diff), "ret30": _r(m.ret30),
                    "ret60": _r(m.ret60), "ret90": _r(m.ret90), "pe": m.pe, "pb": m.pb},
        "criteria": crits,
        "triggered_criteria": [c["key"] for c in triggered],
        "today_is_attention": today_is_attention,
        "disposition": {
            "official": dview.official_disposition,
            "attention_count": dview.attention_count,
            "attention_window": dview.attention_window,
            "distance_to_disposition": dview.distance_to_disposition,
            "notes": dview.notes},
        "disposition_projection": proj,
        "disposition_projection_k13": k13_proj,
        "reverse_scenarios": scenarios,
        "disclaimer": "本工具依官方公開資料與作業要點推算（累積漲跌＝每日漲跌幅加總；當沖比依 TWTB4U；"
                      "含市場/類股平均近似），僅供研究參考，實際以證交所/櫃買中心公告為準，非投資建議。",
    })
    return result
