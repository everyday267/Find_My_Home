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
    # 비과세: 2년 보유 (취득 당시 조정대상지역이면 2년 거주 추가)
    exempt = held >= 2 and (lived >= 2 or not h.adjusted_area_at_acquisition)
    if held < 3:
        ltcg = 0.0
    elif exempt and lived >= 2:  # 1세대1주택 장특공(표2): 보유 연 4% + 거주 연 4%, 각 최대 40%
        ltcg = min(int(held), 10) * 0.04 + min(int(lived), 10) * 0.04
    else:  # 일반 장특공(표1): 연 2%, 최대 30%
        ltcg = min(int(held) * 0.02, 0.30)
    if exempt:
        if sell_price <= rules.high_price_threshold:
            return 0.0, "1주택 비과세"
        gain *= (sell_price - rules.high_price_threshold) / sell_price
        note = f"고가주택 일부과세 (보유 {held:.1f}년, 장특공 {ltcg:.0%})"
    elif held < 2:
        note = f"보유 {held:.1f}년 — 2년 미만 단기 양도세율 {'70' if held < 1 else '60'}%"
    else:
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


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m + 1, day)
        except ValueError:
            continue
    raise ValueError(d)


def _active(items: list, on: date):
    return [i for i in items if not i.get("start") or date.fromisoformat(str(i["start"])) <= on]


def annual_income(fin: Finance, on: date) -> float:
    return sum(i["annual"] for i in _active(fin.incomes, on))


def dsr_loan_limit(fin: Finance, on: date) -> float | None:
    """DSR(스트레스 금리 적용, 원리금균등) 기준 최대 주담대. 소득 미입력이면 None."""
    income = annual_income(fin, on)
    if not income:
        return None
    budget = income * fin.rules.dsr_limit - fin.other_debt_payment
    if budget <= 0:
        return 0.0
    r, n = (fin.loan_rate + fin.rules.stress_rate) / 12, fin.loan_years * 12
    annual_payment_per_unit = 12 * r / (1 - (1 + r) ** -n)
    return budget / annual_payment_per_unit


def purchase_loan(fin: Finance, price: float, on: date) -> float:
    """희망 대출, LTV·가격별 상한, DSR 중 가장 작은 값."""
    limits = [fin.desired_loan, loan_limit(price, fin.rules)]
    dsr = dsr_loan_limit(fin, on)
    if dsr is not None:
        limits.append(dsr)
    return max(min(limits), 0)


def savings_between(fin: Finance, start: date, months: int) -> float:
    """start부터 months개월간 누적 저축 (저축액 변경 일정 반영)."""
    total = 0.0
    for k in range(months):
        on = add_months(start, k)
        annual = fin.annual_savings
        for ch in sorted(_active(fin.savings_changes, on), key=lambda c: str(c["start"])):
            annual = ch["annual_savings"]
        total += annual / 12
    return total


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
    months_needed: int | None  # 몇 개월 후 가능한지 (0=지금 가능, None=기간 내 불가)
    movein_shortfall: float | None = None  # 갭투: 입주 시 전세금 반환 부족액
    allowed: bool = True
    note: str = ""


def _months_to_afford(fin: Finance, on: date, need_at) -> int | None:
    """need_at(k, 날짜) = k개월 후 부족액(양수=부족, 저축 제외). 누적 저축으로 해소되는 최초 시점."""
    saved = 0.0
    for k in range(fin.horizon_years * 12 + 1):
        when = add_months(on, k)
        if k:
            saved += savings_between(fin, add_months(on, k - 1), 1)
        if need_at(k, when) - saved <= 0:
            return k
    return None


def _growth(fin: Finance, months: int) -> float:
    return (1 + fin.price_growth_rate) ** (months / 12)


def live_scenario(fin: Finance, home_price: float, target_price: float, on: date, basis: str) -> Result:
    """우리집 매도 → 대상 단지 매수 후 실거주. 매도 시점에 따라 양도세, 소득에 따라 DSR이 달라진다."""
    def shortfall(k: int, when: date) -> float:
        g = _growth(fin, k)
        sale = home_sale(home_price * g, fin, when)
        p = target_price * g
        return p + buy_costs(p, fin.rules) - purchase_loan(fin, p, when) - sale.net_equity - fin.cash

    sale = home_sale(home_price, fin, on)
    loan = purchase_loan(fin, target_price, on)
    costs = buy_costs(target_price, fin.rules)
    required = target_price + costs - loan
    available = sale.net_equity + fin.cash
    note = "" if loan >= fin.desired_loan else f"대출 한도 {loan / 10000:.1f}억"
    return Result("live", basis, target_price, None, costs, loan, required, available,
                  available - required, _months_to_afford(fin, on, shortfall), note=note)


def max_affordable_price(fin: Finance, home_price: float, on: date, months: int = 0) -> float:
    """months개월 후 우리집을 팔고(그 시점 양도세·DSR) 실거주 매수 가능한 최대 가격 (가격 변동 없음 가정).

    가격별 대출 상한 때문에 가격↑ 시 필요자금이 계단식으로 변하므로 100만원 단위로 탐색.
    """
    when = add_months(on, months)
    available = home_sale(home_price, fin, when).net_equity + fin.cash + savings_between(fin, on, months)
    best = 0.0
    for p in range(10000, 500001, 100):
        if p + buy_costs(p, fin.rules) - purchase_loan(fin, p, when) <= available:
            best = p
    return best


def gap_scenario(fin: Finance, home_price: float, target_price: float, jeonse: float, on: date,
                 basis: str) -> Result:
    """우리집 거주 유지 + 대상 단지를 전세 끼고 매수 → 이후 우리집 매도하고 입주.

    매수 시 대출 불가(임차인 거주·규제지역 추가 주택), 일시적 2주택이면 1주택 취득세율.
    """
    multi = not fin.rules.temporary_two_house

    def shortfall(k: int, when: date) -> float:
        g = _growth(fin, k)
        p, j = target_price * g, jeonse * g
        return p - j + buy_costs(p, fin.rules, multi) - fin.cash

    costs = buy_costs(target_price, fin.rules, multi)
    required = target_price - jeonse + costs
    available = fin.cash
    # 입주 시: 우리집 매도 순자산 + 신규 대출로 전세금 반환
    sale = home_sale(home_price, fin, on)
    movein = jeonse - sale.net_equity - purchase_loan(fin, target_price, on)
    allowed = fin.rules.gap_investment_allowed
    note = "" if allowed else "토지거래허가구역: 유주택자 갭투자 불가 (규제 해제 시 참고용)"
    return Result("gap", basis, target_price, jeonse, costs, 0, required, available, available - required,
                  _months_to_afford(fin, on, shortfall), movein_shortfall=max(movein, 0), allowed=allowed,
                  note=note)


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
    if fin.home.acquisition_date and on < date.fromisoformat(fin.home.acquisition_date):
        return []  # 우리집 취득 이전 날짜는 양도세 계산이 성립하지 않음
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
                    "INSERT OR REPLACE INTO scenarios (date, target_id, scenario, basis, price, jeonse, costs, "
                    "loan, required, available, surplus, months_needed, movein_shortfall) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ds, t.id, r.scenario, r.basis, r.price, r.jeonse, r.costs, r.loan, r.required,
                     r.available, r.surplus, r.months_needed, r.movein_shortfall))
                results.append((t, r))
    return results


def fmt_when(months: int | None, on: date, horizon_years: int) -> str:
    if months is None:
        return f"{horizon_years}년 내 불가"
    if months == 0:
        return "✅ 지금 가능"
    y, m = divmod(months, 12)
    span = (f"{y}년 " if y else "") + (f"{m}개월" if m else "")
    return f"{span.strip()} 후 ({add_months(on, months):%Y-%m})"
