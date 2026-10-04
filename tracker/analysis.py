"""단지별 지표 계산 및 우리집 대비 가격차(gap) 기록."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from statistics import median

from tracker import db
from tracker.config import Complex, Config

METRIC_LABELS = {
    "sale_trade_median": "매매 실거래 중위가",
    "sale_trade_last": "매매 최근 실거래가",
    "sale_trade_count": "매매 실거래 건수",
    "jeonse_trade_median": "전세 실거래 중위가",
    "jeonse_trade_count": "전세 실거래 건수",
    "wolse_trade_count": "월세 실거래 건수",
    "jeonse_ratio": "전세가율",
    "sale_ask_min": "매매 최저호가",
    "sale_ask_median": "매매 중위호가",
    "sale_ask_count": "매매 매물수",
    "jeonse_ask_min": "전세 최저호가",
    "jeonse_ask_median": "전세 중위호가",
    "jeonse_ask_count": "전세 매물수",
    "wolse_ask_count": "월세 매물수",
}

# 우리집과 차이를 기록할 지표
GAP_METRICS = [
    "sale_trade_median",
    "sale_trade_last",
    "sale_ask_min",
    "sale_ask_median",
    "jeonse_trade_median",
    "jeonse_ask_median",
]


def subtract_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    m += 1
    # 말일 보정
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    raise ValueError(d)


def recent_months(today: date, n: int) -> list[str]:
    """오늘 포함 최근 n개 계약월 (YYYYMM), 최신순."""
    return [subtract_months(today.replace(day=1), i).strftime("%Y%m") for i in range(n)]


def _area_bounds(cx: Complex) -> tuple[float, float]:
    return (cx.area_min if cx.area_min is not None else 0.0,
            cx.area_max if cx.area_max is not None else 1e9)


def trade_metrics(conn: sqlite3.Connection, cx: Complex, as_of: date, lookback_months: int) -> dict:
    lo, hi = _area_bounds(cx)
    start = subtract_months(as_of, lookback_months).isoformat()
    end = as_of.isoformat()
    sales = conn.execute(
        "SELECT price, deal_date FROM trades WHERE complex_id=? AND cancelled=0 AND deal_date>? AND deal_date<=? "
        "AND area BETWEEN ? AND ? ORDER BY deal_date",
        (cx.id, start, end, lo, hi),
    ).fetchall()
    last = conn.execute(
        "SELECT price FROM trades WHERE complex_id=? AND cancelled=0 AND deal_date<=? AND area BETWEEN ? AND ? "
        "ORDER BY deal_date DESC, price DESC LIMIT 1",
        (cx.id, end, lo, hi),
    ).fetchone()
    rents = conn.execute(
        "SELECT deposit, monthly_rent FROM rents WHERE complex_id=? AND deal_date>? AND deal_date<=? "
        "AND area BETWEEN ? AND ?",
        (cx.id, start, end, lo, hi),
    ).fetchall()
    jeonse = [r["deposit"] for r in rents if not r["monthly_rent"]]
    m = {
        "sale_trade_median": median([r["price"] for r in sales]) if sales else None,
        "sale_trade_count": len(sales),
        "sale_trade_last": last["price"] if last else None,
        "jeonse_trade_median": median(jeonse) if jeonse else None,
        "jeonse_trade_count": len(jeonse),
        "wolse_trade_count": len(rents) - len(jeonse),
    }
    m["jeonse_ratio"] = (m["jeonse_trade_median"] / m["sale_trade_median"]
                         if m["jeonse_trade_median"] and m["sale_trade_median"] else None)
    return m


def dedupe_listings(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """같은 세대를 여러 중개사가 올린 중복 매물 제거."""
    seen, out = set(), []
    for r in rows:
        key = (r["trade_type"], r["building"], r["floor_info"], r["area"], r["price"], r["rent_price"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def ask_metrics(conn: sqlite3.Connection, cx: Complex, snapshot_date: str) -> dict:
    fetched = conn.execute(
        "SELECT 1 FROM listing_fetches WHERE snapshot_date=? AND complex_id=?", (snapshot_date, cx.id)
    ).fetchone()
    if not fetched:
        return {}
    lo, hi = _area_bounds(cx)
    rows = conn.execute(
        "SELECT * FROM listings WHERE snapshot_date=? AND complex_id=? AND area BETWEEN ? AND ?",
        (snapshot_date, cx.id, lo, hi),
    ).fetchall()
    rows = dedupe_listings(rows)
    sale = [r["price"] for r in rows if r["trade_type"] == "A1" and r["price"]]
    jeonse = [r["price"] for r in rows if r["trade_type"] == "B1" and r["price"]]
    wolse = [r for r in rows if r["trade_type"] == "B2"]
    return {
        "sale_ask_min": min(sale) if sale else None,
        "sale_ask_median": median(sale) if sale else None,
        "sale_ask_count": len(sale),
        "jeonse_ask_min": min(jeonse) if jeonse else None,
        "jeonse_ask_median": median(jeonse) if jeonse else None,
        "jeonse_ask_count": len(jeonse),
        "wolse_ask_count": len(wolse),
    }


def compute_day(conn: sqlite3.Connection, cfg: Config, day: date, include_asks: bool = True) -> None:
    """하루치 지표와 gap을 계산해 저장."""
    ds = day.isoformat()
    for cx in cfg.complexes:
        m = trade_metrics(conn, cx, day, cfg.settings.trade_lookback_months)
        if include_asks:
            m.update(ask_metrics(conn, cx, ds))
        db.upsert_metrics(conn, ds, cx.id, m)
    compute_gaps(conn, cfg, ds)


def _metric(conn: sqlite3.Connection, ds: str, complex_id: str, metric: str) -> float | None:
    row = conn.execute(
        "SELECT value FROM metrics WHERE date=? AND complex_id=? AND metric=?", (ds, complex_id, metric)
    ).fetchone()
    return row["value"] if row else None


def compute_gaps(conn: sqlite3.Connection, cfg: Config, ds: str) -> None:
    for t in cfg.targets:
        for metric in GAP_METRICS:
            tv, hv = _metric(conn, ds, t.id, metric), _metric(conn, ds, cfg.home.id, metric)
            if tv is None and hv is None:
                continue
            db.upsert_gap(conn, ds, t.id, metric, tv, hv)


def rebuild_trade_history(conn: sqlite3.Connection, cfg: Config, start: date, end: date,
                          step_days: int = 1) -> int:
    """과거 실거래 기반 지표/gap 재계산 (호가 지표는 수집된 날만 존재)."""
    n, d = 0, start
    while d <= end:
        compute_day(conn, cfg, d, include_asks=False)
        n += 1
        d += timedelta(days=step_days)
    return n


def gap_series(conn: sqlite3.Connection, target_id: str, metric: str) -> list[tuple[str, float]]:
    rows = conn.execute(
        "SELECT date, gap FROM gaps WHERE target_id=? AND metric=? AND gap IS NOT NULL ORDER BY date",
        (target_id, metric),
    ).fetchall()
    return [(r["date"], r["gap"]) for r in rows]


def gap_signal(series: list[tuple[str, float]], ds: str) -> dict:
    """현재 gap의 과거 대비 위치. gap이 작을수록(비교단지가 상대적으로 쌀수록) 갈아타기에 유리."""
    values = {d: v for d, v in series}
    cur = values.get(ds)
    if cur is None:
        return {}

    def ago(days: int) -> float | None:
        target = (date.fromisoformat(ds) - timedelta(days=days)).isoformat()
        past = [v for d, v in series if d <= target]
        return past[-1] if past else None

    hist = [v for d, v in series if d <= ds]
    pct = sum(1 for v in hist if v <= cur) / len(hist)
    w, m = ago(7), ago(30)
    if len(hist) < 10:
        signal = "⚪ 데이터 축적 중"
    elif pct <= 0.2:
        signal = "🟢 갭 축소(하위 20%) — 갈아타기 유리"
    elif pct >= 0.8:
        signal = "🔴 갭 확대(상위 20%)"
    else:
        signal = "⚪ 보통"
    return {
        "gap": cur,
        "d7": cur - w if w is not None else None,
        "d30": cur - m if m is not None else None,
        "min": min(hist),
        "max": max(hist),
        "percentile": pct,
        "points": len(hist),
        "signal": signal,
    }
