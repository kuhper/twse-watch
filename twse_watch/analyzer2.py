"""整合版引擎（analyzer2）：在 analyzer 之上，把第三階『明日臨界值』直接接到
『明日若達標是否進入處置』的判定。

法規鏈（第6條第1項）：
  第1款：連續3個營業日 達注意 → 處置。
  第2款：連續5個營業日 / 最近10個營業日內6個 / 最近30個營業日內12個營業日，
        經依第4條第1項『第1款至第8款』發布交易資訊 → 處置。
  ⇒ 款9~13 的注意公告不計入第2款累積。
處置措施：30日內第一次＝次一營業日起10個營業日人工約5分鐘撮合、單筆≥10張或
  多筆累計≥30張預收款券；第二次(含)以上＝約20分鐘撮合、全面預收款券。

判定邏輯：
  明日若達某款(限款1~8)臨界 → 再被列注意一次 → 累積次數 +1 → 比對處置門檻。
"""
from __future__ import annotations

from . import source as src
from .analyzer import (compute_metrics, evaluate, reverse, thresholds_for,
                       MARKET_LABEL, _r)
from .dispo import assess, DISPOSITION_RULES, RULE_LABEL

# 第6條第1項第2款只計第4條第1項『第1~8款』
COUNTABLE_KEYS = {"k1", "k2", "k3", "k4", "k5", "k6", "k7", "k8"}
# 反推情境中屬於款1~8、且可換算臨界價的
REVERSE_COUNTABLE = {"k1", "k3", "k4"}


def _disposition_measures(prior_count: int) -> str:
    if prior_count >= 1:
        return ("（最近30營業日內第二次(含)以上處置：約每20分鐘撮合一次，"
                "並對所有投資人預收全部買進價金／賣出證券）")
    return ("（最近30營業日內第一次處置：次一營業日起10個營業日，人工約每5分鐘撮合一次，"
            "單筆≥10交易單位或多筆累計≥30交易單位者預收款券）")


def project_disposition(dview, scenarios) -> dict:
    """回傳處置投影；同時把後果文字注入各款1~8情境的 note。"""
    proj = {"available": False, "leads_to_disposition_tomorrow": None,
            "binding_rule": None, "current_count": dview.attention_count,
            "window": dview.attention_window, "messages": []}

    # 已在處置中
    if dview.official_disposition:
        proj["available"] = True
        proj["messages"].append("已處於官方處置期間，無需投影。")
        return proj

    N = dview.attention_count
    dist = dview.distance_to_disposition or {}

    # 推估是否曾於近30日處置（第一次/第二次措施）
    prior_disp = 0
    for note in (dview.notes or []):
        if "處置紀錄" in note:
            prior_disp = 1
    measures = _disposition_measures(prior_disp)

    # 無官方累積次數：只能定性
    if N is None or not dist:
        proj["messages"].append(
            "官方尚未顯示累積次數。若今日的注意屬第4條第1~8款，則今日為累積第1次（或延續既有累積）；"
            "需連續或在視窗內累積達標才會處置。")
        # 仍標示哪些明日情境屬可計入款別
        for s in scenarios:
            if s["key"] in REVERSE_COUNTABLE:
                s["note"] += "　▶ 此款屬第1~8款，明日達標將計入處置累積次數。"
        return proj

    proj["available"] = True
    binding_label, remaining = min(dist.items(), key=lambda kv: kv[1])
    proj["binding_rule"] = binding_label
    proj["remaining"] = remaining
    new_count = N + 1
    new_remaining = remaining - 1  # 明日再 +1

    if remaining <= 0:
        proj["leads_to_disposition_tomorrow"] = True
        proj["messages"].append("累積已達「%s」標準，預期即將公告處置%s。" % (binding_label, measures))
    elif new_remaining <= 0:
        proj["leads_to_disposition_tomorrow"] = True
        proj["messages"].append(
            "明日若達下列任一款(第1~8款)臨界並再被列注意（第 %d 次），即觸發「%s」處置標準，"
            "預期當日盤後公告、次一營業日起執行%s。" % (new_count, binding_label, measures))
    else:
        proj["leads_to_disposition_tomorrow"] = False
        proj["messages"].append(
            "明日即使再被列注意（第 %d 次），距「%s」仍差 %d 次，尚不會立即處置。"
            % (new_count, binding_label, new_remaining))

    # 注入到款1~8的明日情境
    for s in scenarios:
        if s["key"] in REVERSE_COUNTABLE:
            if proj["leads_to_disposition_tomorrow"]:
                s["note"] += ("　▶ 若明日達此臨界 → 注意第 %d 次 → 觸發「%s」→ 預期次一營業日起處置%s。"
                              % (new_count, binding_label, measures))
            else:
                s["note"] += ("　▶ 若明日達此臨界 → 注意第 %d 次（距「%s」仍差 %d 次）。"
                              % (new_count, binding_label, new_remaining))
    return proj


MARKET_FULL = MARKET_LABEL


def analyze(stock_no: str, months: int = 6) -> dict:
    stock_no = stock_no.strip()
    sd = src.fetch_stock(stock_no, months=months)
    t = thresholds_for(sd.market)

    result = {
        "code": sd.code, "name": sd.name, "market": sd.market,
        "market_label": MARKET_FULL.get(sd.market, sd.market),
        "warnings": list(sd.warnings),
        "as_of": sd.bars[-1].date.isoformat() if sd.bars else None,
        "shares_outstanding": sd.shares_outstanding,
        "threshold_note": "本檔以 %s 門檻評估（款三累積漲跌門檻 %.0f%%）。"
                          % (MARKET_FULL.get(sd.market, sd.market), t.k3_cum6),
    }
    if not sd.bars:
        result.update({"stage": "NO_DATA", "headline": "查無日成交資料，無法分析。",
                       "criteria": [], "reverse_scenarios": [],
                       "disposition": {"official": None, "distance_to_disposition": {}, "notes": []},
                       "disposition_projection": {"available": False, "messages": []}})
        return result

    m = compute_metrics(sd.bars, sd.shares_outstanding)
    crits = evaluate(m, t, market_avg=0.0)
    triggered = [c for c in crits if c["triggered"]]
    today_is_attention = len(triggered) > 0
    dview = assess(stock_no, today_is_attention)
    scenarios = reverse(sd.bars, m, t)

    # 整合：處置投影（並注入情境 note）
    proj = project_disposition(dview, scenarios)

    headline = dview.headline
    if proj.get("leads_to_disposition_tomorrow") and dview.stage != "DISPOSED":
        headline += "　⚠ 明日達標即進入處置（見第三階各款臨界與後果）。"

    result.update({
        "stage": "DISPOSED" if proj.get("leads_to_disposition_tomorrow") and dview.stage == "DISPOSED" else dview.stage,
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
        "reverse_scenarios": scenarios,
        "stage_explanation": {
            "stage1": "抓官方既成注意/處置公告；已公告處置日期即結束。",
            "stage2": "公式化注意/處置標準，判斷今日踩到哪幾款、距處置還差幾次。",
            "stage3": "滾動視窗反推明日臨界值，並判定『明日達標是否進入處置』。"},
        "disclaimer": "本工具依官方公開資料與作業要點推算（累積漲跌＝每日漲跌幅加總；含市場/類股平均近似），"
                      "僅供研究參考，實際以證交所/櫃買中心公告為準，非投資建議。",
    })
    return result
