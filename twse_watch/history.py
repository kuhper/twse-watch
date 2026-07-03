"""歷史公告序列引擎：逐日回算過去 N 個營業日「會被公告哪幾款」，
自算『累積第幾次／連續幾日』，不依賴官方 notetrans 即時快照。

每個營業日 D 是否會被公告：
* 當日型款（1,2,3,4,9,10,11）：以截至 D 的資料判定。
* T-1 型款（13 當沖）：以 D 的前一營業日資料判定（announcement on D refers to D-1）。

涵蓋限制（誠實標示）：
* 本工具實算 款1,2,3,4,9,10,11,13；款5,6,7,8,12 需額外明細未串接 → 自算次數會「偏少」。
* 款1~4 仍含市場/類股平均之近似、且未實作款2交叉除外 → 個別日可能「偏多」。
故自算次數為估計值，與官方可能有出入，僅供預警；最終以官方公告為準。
"""
from __future__ import annotations

from .analyzer import compute_metrics, evaluate, thresholds_for
from .daytrade import evaluate_k13

# 計入第6條第1項第2款（10日內6/30日內12）的款別：限第1~8款；本工具實算其中 1~4。
COUNTABLE_18_COMPUTED = {"k1", "k2", "k3", "k4"}
# 任一款（供『連續3日』與總覽）
ANY_COMPUTED = {"k1", "k2", "k3", "k4", "k9", "k10", "k11", "k13"}


def _price_keys_asof(bars_upto, market) -> set:
    if len(bars_upto) < 7:
        return set()
    m = compute_metrics(bars_upto, None if not bars_upto else getattr(bars_upto[-1], "_shares", None))
    # shares 不影響價格款；週轉率款(k4/k10)在無股數時為 insufficient，不計入
    crits = evaluate(m, thresholds_for(market), market_avg=0.0)
    return {c["key"] for c in crits if c["triggered"]}


def announcement_history(bars, code, shares, market="TWSE",
                         lookback=30, k13_lookback=10) -> list:
    """回傳最近 lookback 個營業日的逐日公告判定（由舊到新）。"""
    n = len(bars)
    out = []
    start = max(7, n - lookback)
    for i in range(start, n):
        upto = bars[:i + 1]
        # 價格/量/週轉款（截至 D=bars[i]）
        m = compute_metrics(upto, shares)
        price_keys = {c["key"] for c in evaluate(m, thresholds_for(market), market_avg=0.0)
                      if c["triggered"]}
        keys = set(price_keys)
        # 款13（當沖，T-1）：announcement on D 用 D-1，即 evaluate_k13(bars[:i])
        if market == "TWSE" and (n - i) <= k13_lookback and i >= 7:
            try:
                if evaluate_k13(bars[:i], code, shares)["triggered"]:
                    keys.add("k13")
            except Exception:
                pass
        announced = bool(keys & ANY_COMPUTED)
        out.append({
            "date": bars[i].date.isoformat(),
            "keys": sorted(keys),
            "announced": announced,
            "countable_18": bool(keys & COUNTABLE_18_COMPUTED),
        })
    return out


def self_counts(history: list) -> dict:
    """由逐日序列算出自算累積與連續。"""
    if not history:
        return {"available": False}
    # 連續（由最新往回，announced 連續數）
    consec = 0
    for d in reversed(history):
        if d["announced"]:
            consec += 1
        else:
            break
    last10 = history[-10:]
    last30 = history[-30:]
    c10 = sum(1 for d in last10 if d["countable_18"])
    c30 = sum(1 for d in last30 if d["countable_18"])
    announced_dates = [d["date"] for d in history if d["announced"]]
    return {
        "available": True,
        "consecutive_announced": consec,
        "count_10d_k18": c10,
        "count_30d_k18": c30,
        "announced_days_total": len(announced_dates),
        "last_announced": announced_dates[-1] if announced_dates else None,
        "distance": {
            "連續3個營業日": max(3 - consec, 0),
            "最近10日內6次": max(6 - c10, 0),
            "最近30日內12次": max(12 - c30, 0),
        },
    }
