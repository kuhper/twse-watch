"""指標計算：把日成交序列換算成注意標準需要的各種數值。

設計原則：所有計算都以「由舊到新排序的 bars」為輸入，最後一根為當日。
凡需要全市場/類股平均值的款別，本工具以可設定的代理值近似（見 market_avg），
並在結果中明確標示此假設，避免把近似當成精確。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Metrics:
    """當日各項指標快照。None 代表資料不足無法計算。"""
    close: Optional[float] = None
    open_ref: Optional[float] = None           # 當日開盤參考價(以前一日收盤近似)
    cum6_return_pct: Optional[float] = None     # 最近6營業日累積收盤漲跌%
    base6_close: Optional[float] = None         # 6日累積的基準收盤價
    span2_diff_in6: Optional[float] = None      # 6日內起迄兩日收盤價差(高-基準, 元)
    ret30_pct: Optional[float] = None           # 最近30營業日起迄漲跌%
    ret60_pct: Optional[float] = None
    ret90_pct: Optional[float] = None
    turnover_pct: Optional[float] = None        # 當日週轉率%
    cum6_turnover_pct: Optional[float] = None   # 最近6營業日累積週轉率%
    vol_amp: Optional[float] = None             # 當日量 / 最近60日均量
    avg6_vol_amp: Optional[float] = None        # 最近6日均量 / 最近60日均量
    pe: Optional[float] = None
    pb: Optional[float] = None
    amount: Optional[float] = None              # 當日成交金額
    volume_shares: Optional[float] = None
    is6_high: Optional[bool] = None             # 當日收盤是否為近6日最高
    is6_low: Optional[bool] = None


def _closes(bars) -> list:
    return [b.close for b in bars]


def cum_return_over_window(bars, win: int) -> tuple[Optional[float], Optional[float]]:
    """最近 win 個營業日(含當日)累積收盤漲跌%。
    基準 = 視窗前一日(第 win+1 根)的收盤；不足則用視窗第一根。
    回傳 (漲跌%, 基準收盤)。"""
    if len(bars) == 0:
        return None, None
    today = bars[-1].close
    if today is None:
        return None, None
    if len(bars) >= win + 1 and bars[-(win + 1)].close:
        base = bars[-(win + 1)].close
    elif len(bars) >= win and bars[-win].close:
        base = bars[-win].close
    else:
        base = bars[0].close
    if not base:
        return None, None
    return (today / base - 1) * 100, base


def compute_metrics(bars, shares_outstanding: Optional[float]) -> Metrics:
    m = Metrics()
    if not bars:
        return m
    cur = bars[-1]
    m.close = cur.close
    m.amount = cur.amount
    m.volume_shares = cur.volume_shares
    m.pe = cur.pe
    m.pb = cur.pb
    m.open_ref = bars[-2].close if len(bars) >= 2 else cur.open

    # 6 日累積漲跌
    m.cum6_return_pct, m.base6_close = cum_return_over_window(bars, 6)
    # 30/60/90 日起迄
    m.ret30_pct, _ = cum_return_over_window(bars, 30)
    m.ret60_pct, _ = cum_return_over_window(bars, 60)
    m.ret90_pct, _ = cum_return_over_window(bars, 90)

    # 6 日內起迄兩日收盤價差（以視窗最高/最低收盤對基準）
    win6 = bars[-6:]
    closes6 = [b.close for b in win6 if b.close is not None]
    if closes6 and m.close is not None:
        m.is6_high = m.close >= max(closes6)
        m.is6_low = m.close <= min(closes6)
        if m.base6_close:
            m.span2_diff_in6 = m.close - m.base6_close

    # 週轉率
    if shares_outstanding and cur.volume_shares:
        m.turnover_pct = cur.volume_shares / shares_outstanding * 100
        # 6 日累積週轉率
        s = 0.0
        ok = True
        for b in bars[-6:]:
            if b.volume_shares is None:
                ok = False
                break
            s += b.volume_shares / shares_outstanding * 100
        m.cum6_turnover_pct = s if ok else None

    # 量能放大（當日量 / 最近60日均量）
    vols = [b.volume_shares for b in bars[-60:] if b.volume_shares is not None]
    if len(vols) >= 5 and cur.volume_shares:
        avg60 = sum(vols) / len(vols)
        if avg60 > 0:
            m.vol_amp = cur.volume_shares / avg60
            vols6 = [b.volume_shares for b in bars[-6:] if b.volume_shares is not None]
            if vols6:
                m.avg6_vol_amp = (sum(vols6) / len(vols6)) / avg60
    return m
