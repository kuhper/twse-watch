"""第三階：反推具體數字。

對「明日」逐款計算觸發注意所需的臨界值——臨界收盤價、需放大的量、需維持的
週轉率——並對照次日漲跌幅限制(±10%)判斷是否「一日內可達」或「需連續數日」。

所有反推都建立在「市場/類股平均維持不變」的假設上（與規則引擎相同的近似），
此假設會明確寫進每個情境的 note。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .rules import Thresholds, LOTS_TO_SHARES

PRICE_LIMIT = 0.10  # 上市櫃普通股單日漲跌幅限制


@dataclass
class Scenario:
    key: str
    title: str
    target: str            # 人話描述的臨界條件
    reachable_next_day: Optional[bool]
    note: str = ""
    numbers: dict = None


def _avg60_vol(bars) -> Optional[float]:
    vols = [b.volume_shares for b in bars[-60:] if b.volume_shares is not None]
    return sum(vols) / len(vols) if len(vols) >= 5 else None


def reverse_scenarios(bars, shares_outstanding: Optional[float],
                      t: Thresholds = Thresholds()) -> list:
    out: list[Scenario] = []
    if len(bars) < 7 or bars[-1].close is None:
        return out
    today_close = bars[-1].close
    limit_up = round(today_close * (1 + PRICE_LIMIT), 2)
    limit_dn = round(today_close * (1 - PRICE_LIMIT), 2)

    # 明日的6日視窗基準 = 今日往前數第5根(因明日納入後，視窗為 bars[-5:]+明日)，
    # 基準收盤為該視窗前一日 = bars[-6]
    base_for_tmrw = bars[-6].close if bars[-6].close else None
    assume = "假設：市場/類股平均漲跌維持不變、無除權息等非交易因素。"

    # ---- 第1款：6日累積漲跌幅 > 30%（或 >25%且起迄價差≥50元）----
    if base_for_tmrw:
        up30 = round(base_for_tmrw * (1 + t.k1_cum6_a / 100), 2)
        dn30 = round(base_for_tmrw * (1 - t.k1_cum6_a / 100), 2)
        up25 = round(base_for_tmrw * (1 + t.k1_cum6_b / 100), 2)
        reach_up = up30 <= limit_up
        out.append(Scenario(
            "k1", "第一款 近6日累積漲跌幅",
            f"明日收盤 ≥ {up30} 元 → 近6日累積漲幅突破 +30%（達注意）；"
            f"或 ≥ {up25} 元且起迄價差≥50元亦達標。下跌方向：≤ {dn30} 元。",
            reach_up,
            f"明日漲停參考價約 {limit_up} 元。"
            + ("一日內即可達標。" if reach_up else "單日漲停仍不足，需連續上漲數日。")
            + " " + assume,
            {"target_close_up_30%": up30, "target_close_up_25%": up25,
             "target_close_dn_30%": dn30, "limit_up": limit_up}))

    # ---- 第11款：起迄兩日收盤價差 ≥ 100 元（高價股級距加碼）----
    if base_for_tmrw:
        thr = t.k11_price_diff
        # 以明日收盤估級距
        approx_close = limit_up
        if approx_close >= t.k11_tier_base:
            thr += int(approx_close // t.k11_tier_base) * t.k11_tier_add
        target_up = round(base_for_tmrw + thr, 2)
        reach = target_up <= limit_up
        out.append(Scenario(
            "k11", "第十一款 起迄兩日收盤價差",
            f"明日收盤 ≥ {target_up} 元（與6日視窗基準價差達 {thr:.0f} 元，且須為近6日最高收盤）。",
            reach,
            ("一日內可達。" if reach else "單日漲停不足，需連續上漲。") + " " + assume,
            {"price_diff_threshold": thr, "target_close": target_up}))

    # ---- 第4款：6日漲跌>25% 且 當日週轉率≥10% ----
    if shares_outstanding and base_for_tmrw:
        vol_for_10pct = t.k4_turnover / 100 * shares_outstanding
        price_25 = round(base_for_tmrw * (1 + t.k4_cum6 / 100), 2)
        out.append(Scenario(
            "k4", "第四款 漲幅+週轉率",
            f"需同時：明日收盤 ≥ {price_25} 元（近6日漲幅>25%）且 當日成交 ≥ "
            f"{vol_for_10pct/LOTS_TO_SHARES:,.0f} 張（週轉率≥10%）。",
            price_25 <= limit_up,
            f"週轉率10% ≈ {vol_for_10pct/LOTS_TO_SHARES:,.0f} 張成交量。" + " " + assume,
            {"target_close_25%": price_25, "volume_lots_for_10%_turnover": round(vol_for_10pct/LOTS_TO_SHARES)}))

    # ---- 第10款：6日累積週轉率>50% 且 當日≥10% ----
    if shares_outstanding:
        # 明日累積週轉率 = 最近5日週轉率合計 + 明日週轉率
        last5 = bars[-5:]
        s5 = 0.0
        ok = True
        for b in last5:
            if b.volume_shares is None:
                ok = False
                break
            s5 += b.volume_shares / shares_outstanding * 100
        if ok:
            need_tmrw_turnover = max(t.k10_turnover, t.k10_cum6_turnover - s5)
            need_lots = need_tmrw_turnover / 100 * shares_outstanding / LOTS_TO_SHARES
            out.append(Scenario(
                "k10", "第十款 累積週轉率",
                f"近5日累積週轉率已 {s5:.1f}%；明日週轉率需再 ≥ {need_tmrw_turnover:.1f}%"
                f"（約 {need_lots:,.0f} 張）使6日累積>50% 且當日≥10%。",
                None,
                "週轉率取決於成交量，無漲跌幅限制可單日達成。" + " " + assume,
                {"cum5_turnover_pct": round(s5, 1),
                 "need_tomorrow_turnover_pct": round(need_tmrw_turnover, 1),
                 "need_tomorrow_lots": round(need_lots)}))

    # ---- 第3/9款：量能放大 5 倍 ----
    avg60 = _avg60_vol(bars)
    if avg60:
        need_vol = 5 * avg60
        out.append(Scenario(
            "k3/k9", "第三/九款 量能放大",
            f"明日成交量 ≥ {need_vol/LOTS_TO_SHARES:,.0f} 張（達60日均量5倍）；"
            f"第三款另需近6日漲幅>25%，第九款另需6日均量亦達5倍。",
            None,
            f"60日均量約 {avg60/LOTS_TO_SHARES:,.0f} 張。" + " " + assume,
            {"avg60_lots": round(avg60/LOTS_TO_SHARES), "need_volume_lots": round(need_vol/LOTS_TO_SHARES)}))

    return out
