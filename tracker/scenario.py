"""갈아타기 자금 시나리오: 실거주 갈아타기 / 갭투자 후 실거주.

금액 단위는 모두 만원. 세율·대출 규정은 2026년 10월 기준 단순화 모델이며
DSR(소득 기준 한도), 보유세, 이사비 등은 반영하지 않는다. 실제 거래 전에는 세무사·은행 확인 필요.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from tracker.config import Complex, Config, Finance, Rules

SCENARIOS = {"live": "실거주 갈아타기", "gap": "갭투자 후 실거주"}
BASES = {"trade": "실거래 기준", "ask": "호가 기준"}

# 양도소득세 기본세율 (과세표준 상한, 세율, 누진공제) — 만원
_CGT_BRACKETS = [
    (1400, 0.06, 0), (5000, 0.15, 126), (8800, 0.24, 576), (15000, 0.35, 1544),
    (30000, 0.38, 1994), (50000, 0.40, 2594), (100000, 0.42, 3594), (None, 0.45, 6594),
]


# ---------------------------------------------------------------- 세금·수수료

def brokerage_fee(price: float, rules: Rules) -> float:
    """매매 중개수수료 상한 (2021.10 개편 요율) + 부가세."""
    if price < 5000:
        fee = min(price * 0.006, 25)
    elif price < 20000:
        fee = min(price * 0.005, 80)
    elif price < 90000:
        fee = price * 0.004
    elif price < 120000:
        fee = price * 0.005
    elif price < 150000:
        fee = price * 0.006
    else:
        fee = price * 0.007
    return fee * (1 + rules.brokerage_vat)


def acquisition_tax(price: float, rules: Rules, multi_house: bool = False) -> float:
    """주택 취득세 + 지방교육세 (전용 85㎡ 이하 → 농어촌특별세 비과세)."""
    if multi_house:
        return price * rules.multi_house_acq_rate
    eok = price / 10000
    if eok <= 6:
        rate, edu = 0.01, 0.001
    elif eok <= 9:
        rate = (eok * 2 / 3 - 3) / 100
        edu = rate * 0.1
    else:
        rate, edu = 0.03, 0.003
    return price * (rate + edu)


def _years_between(start: str | None, end: date) -> float:
    if not start:
        return 0.0
    return (end - date.fromisoformat(start)).days / 365.25


def capital_gains_tax(sell_price: float, fin: Finance, sell_costs: float, on: date) -> tuple[float | None, str]:
    """1세대1주택 양도소득세(지방소득세 포함). 취득가 미입력이면 (None, 사유)."""
    h, rules = fin.home, fin.rules
    if not h.acquisition_price:
        return None, "취득가 미입력 → 양도세 미반영"
    held = _years_between(h.acquisition_date, on)
    lived = _years_between(h.residence_start, on)
    acq_costs = h.acquisition_costs if h.acquisition_costs is not None else (
        acquisition_tax(h.acquisition_price, rules) + brokerage_fee(h.acquisition_price, rules))
    gain = sell_price - h.acquisition_price - acq_costs - sell_costs
    if gain <= 0:
        return 0.0, "양도차익 없음"
    exempt = held >= 2 and lived >= 2  # 조정대상지역 취득분은 2년 거주 요건
    if exempt:
        if sell_price <= rules.high_price_threshold:
            return 0.0, "1주택 비과세"
        gain *= (sell_price - rules.high_price_threshold) / sell_price
        ltcg = min(int(held), 10) * 0.04 + min(int(lived), 10) * 0.04 if held >= 3 else 0.0
        note = f"고가주택 일부과세 (보유 {held:.1f}년·거주 {lived:.1f}년, 장특공 {ltcg:.0%})"
    else:
        ltcg = min(int(held) * 0.02, 0.30) if held >= 3 else 0.0
        note = f"비과세 요건 미충족 (보유 {held:.1f}년·거주 {lived:.1f}년)"
    base = max(gain * (1 - ltcg) - 250, 0)
    if held < 1:
        tax = base * 0.70
    elif held < 2:
        tax = base * 0.60
    else:
        tax = next(base * r - d for cap, r, d in _CGT_BRACKETS if cap is None or base <= cap)
    return max(tax, 0) * 1.1, note


def loan_limit(price: float, rules: Rules) -> float:
    cap = next(c for limit, c in rules.loan_caps if limit is None or price <= limit)
    return min(price * rules.ltv, cap)


# ---------------------------------------------------------------- 시나리오

@dataclass
class HomeSale:
    price: float
    brokerage: float
    cgt: float | None
    cgt_note: str
    net_equity: float  # 매도가 − 대출상환 − 중개비 − 양도세


def home_sale(price: float, fin: Finance, on: date) -> HomeSale:
    fee = brokerage_fee(price, fin.rules)
    cgt, note = capital_gains_tax(price, fin, fee, on)
    return HomeSale(price, fee, cgt, note, price - fin.current_loan - fee - (cgt or 0))


def buy_costs(price: float, rules: Rules, multi_house: bool = False) -> float:
    return acquisition_tax(price, rules, multi_house) + brokerage_fee(price, rules) + price * rules.misc_buy_rate


@dataclass
class Result:
    scenario: str
    basis: str
    price: float  # 매수가
    jeonse: float | None  # 갭투 시 전세가
    costs: float  # 취득세+중개+기타
    loan: float  # 신규 대출
    required: float  # 필요 자기자본 (현금+집 매도 순자산)
    available: float  # 현재 가용자금
    surplus: float  # 여유(+)/부족(−)
    years_needed: int | None  # 몇 년 저축하면 가능한지 (0=지금 가능, None=기간 내 불가)
    movein_shortfall: float | None = None  # 갭투: 입주 시 전세금 반환 부족액
    allowed: bool = True
    note: str = ""


def _years_to_afford(fin: Finance, need_at) -> int | None:
    """need_at(t) = t년 후 (가격 상승 반영) 부족액(양수=부족). 저축 누적으로 해소되는 최초 연도."""
    for t in range(fin.horizon_years + 1):
        if need_at(t) - fin.annual_savings * t <= 0:
            return t
    return None


def live_scenario(fin: Finance, home_price: float, target_price: float, on: date, basis: str) -> Result:
    """우리집 매도 → 대상 단지 매수 후 실거주. 대출은 희망액과 규제 한도 중 작은 값."""
    def shortfall(t: int) -> float:
        g = (1 + fin.price_growth_rate) ** t
        sale = home_sale(home_price * g, fin, on)
        p = target_price * g
        loan = min(fin.desired_loan, loan_limit(p, fin.rules))
        return p + buy_costs(p, fin.rules) - loan - sale.net_equity - fin.cash

    sale = home_sale(home_price, fin, on)
    loan = min(fin.desired_loan, loan_limit(target_price, fin.rules))
    costs = buy_costs(target_price, fin.rules)
    required = target_price + costs - loan
    available = sale.net_equity + fin.cash
    note = "" if loan >= fin.desired_loan else f"대출 규제로 {loan / 10000:.1f}억까지만 가능"
    return Result("live", basis, target_price, None, costs, loan, required, available,
                  available - required, _years_to_afford(fin, shortfall), note=note)


def max_affordable_price(fin: Finance, home_price: float, on: date, years: int = 0) -> float:
    """우리집 매도 순자산 + 여유자금(+저축)으로 실거주 매수 가능한 최대 가격 (가격 상승 없음 가정).

    가격별 대출 상한 때문에 가격↑ 시 필요자금이 계단식으로 변하므로 100만원 단위로 탐색.
    """
    available = home_sale(home_price, fin, on).net_equity + fin.cash + fin.annual_savings * years
    best = 0.0
    for p in range(10000, 500001, 100):
        loan = min(fin.desired_loan, loan_limit(p, fin.rules))
        if p + buy_costs(p, fin.rules) - loan <= available:
            best = p
    return best


def gap_scenario(fin: Finance, home_price: float, target_price: float, jeonse: float, on: date,
                 basis: str) -> Result:
    """우리집 거주 유지 + 대상 단지를 전세 끼고 매수 → 이후 우리집 매도하고 입주.

    매수 시 대출 불가(임차인 거주·규제지역 추가 주택), 일시적 2주택이면 1주택 취득세율.
    """
    multi = not fin.rules.temporary_two_house

    def shortfall(t: int) -> float:
        g = (1 + fin.price_growth_rate) ** t
        p, j = target_price * g, jeonse * g
        return p - j + buy_costs(p, fin.rules, multi) - fin.cash

    costs = buy_costs(target_price, fin.rules, multi)
    required = target_price - jeonse + costs
    available = fin.cash
    # 입주 시: 우리집 매도 순자산 + 신규 대출로 전세금 반환
    sale = home_sale(home_price, fin, on)
    movein = jeonse - sale.net_equity - min(fin.desired_loan, loan_limit(target_price, fin.rules))
    allowed = fin.rules.gap_investment_allowed
    note = "" if allowed else "토지거래허가구역: 유주택자 갭투자 불가 (규제 해제 시 참고용)"
    return Result("gap", basis, target_price, jeonse, costs, 0, required, available, available - required,
                  _years_to_afford(fin, shortfall), movein_shortfall=max(movein, 0), allowed=allowed, note=note)


# ---------------------------------------------------------------- 계산·저장



def _prices(m: dict, basis: str) -> tuple[float | None, float | None]:
    """기준별 (매매가, 전세가). 호가 기준: 최저호가로 팔고 최저호가로 산다고 가정."""
    if basis == "trade":
        return m.get("sale_trade_median"), m.get("jeonse_trade_median")
    return m.get("sale_ask_min"), m.get("jeonse_ask_median") or m.get("jeonse_trade_median")


def compute(conn: sqlite3.Connection, cfg: Config, ds: str) -> list[tuple[Complex, Result]]:
    fin = cfg.finance
    if not fin:
        return []
    on = date.fromisoformat(ds)
    metrics: dict[str, dict] = {}
    for r in conn.execute("SELECT complex_id, metric, value FROM metrics WHERE date=?", (ds,)):
        metrics.setdefault(r["complex_id"], {})[r["metric"]] = r["value"]
    results = []
    for basis in BASES:
        home_price, _ = _prices(metrics.get(cfg.home.id, {}), basis)
        if not home_price:
            continue
        for t in cfg.targets:
            price, jeonse = _prices(metrics.get(t.id, {}), basis)
            if not price:
                continue
            res = [live_scenario(fin, home_price, price, on, basis)]
            if jeonse:
                res.append(gap_scenario(fin, home_price, price, jeonse, on, basis))
            for r in res:
                conn.execute(
                    "INSERT OR REPLACE INTO scenarios VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ds, t.id, r.scenario, r.basis, r.price, r.jeonse, r.costs, r.loan, r.required,
                     r.available, r.surplus, r.years_needed, r.movein_shortfall))
                results.append((t, r))
    return results


def fmt_years(y: int | None, horizon: int) -> str:
    if y is None:
        return f"{horizon}년 내 불가"
    return "✅ 지금 가능" if y == 0 else f"{y}년 후"

