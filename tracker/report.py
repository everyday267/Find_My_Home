"""리포트 생성: reports/latest.md, CSV 이력, dashboard.html."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date
from pathlib import Path

from tracker import scenario
from tracker.analysis import GAP_METRICS, METRIC_LABELS, gap_series, gap_signal
from tracker.config import Config


def fmt_won(v: float | None, signed: bool = False) -> str:
    """만원 단위 → '12억 3,000' 형식."""
    if v is None:
        return "-"
    v = int(round(v))
    sign = ("+" if v > 0 else "-" if v < 0 else "±") if signed else ("-" if v < 0 else "")
    a = abs(v)
    eok, man = divmod(a, 10000)
    if eok and man:
        body = f"{eok}억 {man:,}"
    elif eok:
        body = f"{eok}억"
    else:
        body = f"{man:,}만"
    return sign + body


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def _metrics_for(conn: sqlite3.Connection, ds: str, complex_id: str) -> dict:
    rows = conn.execute("SELECT metric, value FROM metrics WHERE date=? AND complex_id=?", (ds, complex_id))
    return {r["metric"]: r["value"] for r in rows}


def latest_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(date) AS d FROM metrics").fetchone()
    return row["d"] if row else None


def _area_label(cx) -> str:
    lo = "" if cx.area_min is None else f"{cx.area_min:g}"
    hi = "" if cx.area_max is None else f"{cx.area_max:g}"
    return "전체" if not lo and not hi else f"{lo}~{hi}㎡"


def build_markdown(conn: sqlite3.Connection, cfg: Config, ds: str) -> str:
    lb = cfg.settings.trade_lookback_months
    lines = [
        f"# 갈아타기 트래커 리포트 — {ds}",
        "",
        f"기준 단지(우리집): **{cfg.home.name}** · 실거래 중위가는 최근 {lb}개월 계약 기준 · 금액 단위 만원",
        "",
        "## 단지별 현황",
        "",
        "| 단지 | 면적 | 매매 실거래 중위(건수) | 최근 실거래 | 매매 호가 최저 / 중위 (매물) | "
        "전세 실거래 중위(건수) | 전세 호가 중위 (매물) | 전세가율 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for cx in cfg.complexes:
        m = _metrics_for(conn, ds, cx.id)
        name = f"🏠 **{cx.name}**" if cx.is_home else cx.name
        last = conn.execute(
            "SELECT deal_date, price, floor FROM trades WHERE complex_id=? AND cancelled=0 AND deal_date<=? "
            "AND area BETWEEN ? AND ? ORDER BY deal_date DESC LIMIT 1",
            (cx.id, ds, cx.area_min or 0, cx.area_max or 1e9),
        ).fetchone()
        last_s = f"{fmt_won(last['price'])} ({last['deal_date'][2:]}, {last['floor']}층)" if last else "-"
        ask = (f"{fmt_won(m.get('sale_ask_min'))} / {fmt_won(m.get('sale_ask_median'))} "
               f"({int(m['sale_ask_count'])})" if m.get("sale_ask_count") is not None else "-")
        jask = (f"{fmt_won(m.get('jeonse_ask_median'))} ({int(m['jeonse_ask_count'])})"
                if m.get("jeonse_ask_count") is not None else "-")
        lines.append(
            f"| {name} | {_area_label(cx)} | {fmt_won(m.get('sale_trade_median'))} "
            f"({int(m.get('sale_trade_count') or 0)}) | {last_s} | {ask} | "
            f"{fmt_won(m.get('jeonse_trade_median'))} ({int(m.get('jeonse_trade_count') or 0)}) | {jask} | "
            f"{_pct(m.get('jeonse_ratio'))} |"
        )

    lines += [
        "",
        "## 우리집 대비 가격차 (비교단지 − 우리집)",
        "",
        "갭이 **작아질수록** 갈아타기에 필요한 추가 자금이 줄어듭니다. "
        "백분위는 기록된 전체 기간 중 현재 갭의 위치(낮을수록 유리)입니다.",
        "",
    ]
    for metric in ("sale_trade_median", "sale_ask_median"):
        lines += [
            f"### {METRIC_LABELS[metric]} 기준",
            "",
            "| 비교 단지 | 비교단지 가격 | 우리집 가격 | 갭 | 비율 | 7일 변화 | 30일 변화 | 기간 최저 / 최고 | 백분위 | 신호 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for t in cfg.targets:
            row = conn.execute(
                "SELECT * FROM gaps WHERE date=? AND target_id=? AND metric=?", (ds, t.id, metric)
            ).fetchone()
            sig = gap_signal(gap_series(conn, t.id, metric), ds)
            if not row or not sig:
                lines.append(f"| {t.name} | {fmt_won(row['target_value'] if row else None)} | "
                             f"{fmt_won(row['home_value'] if row else None)} | - | - | - | - | - | - | 데이터 없음 |")
                continue
            lines.append(
                f"| {t.name} | {fmt_won(row['target_value'])} | {fmt_won(row['home_value'])} | "
                f"**{fmt_won(row['gap'], signed=True)}** | {row['ratio']:.2f}x | {fmt_won(sig['d7'], True)} | "
                f"{fmt_won(sig['d30'], True)} | {fmt_won(sig['min'], True)} / {fmt_won(sig['max'], True)} | "
                f"{_pct(sig['percentile'])} ({sig['points']}일) | {sig['signal']} |"
            )
        lines.append("")

    lines += build_scenario_section(conn, cfg, ds)

    new_rows = []
    for cx in cfg.complexes:
        for r in conn.execute(
            "SELECT 'trade' AS kind, deal_date, price AS p1, 0 AS p2, area, floor, cancelled FROM trades "
            "WHERE complex_id=? AND (first_seen=? OR cancel_seen=?) "
            "UNION ALL SELECT 'rent', deal_date, deposit, monthly_rent, area, floor, 0 FROM rents "
            "WHERE complex_id=? AND first_seen=? ORDER BY deal_date DESC",
            (cx.id, ds, ds, cx.id, ds),
        ):
            if not cx.area_ok(r["area"]):
                continue
            if r["kind"] == "trade":
                kind, price = ("매매 해제" if r["cancelled"] else "매매"), fmt_won(r["p1"])
            else:
                kind = "월세" if r["p2"] else "전세"
                price = f"{fmt_won(r['p1'])}/{r['p2']}" if r["p2"] else fmt_won(r["p1"])
            new_rows.append(f"| {cx.name} | {kind} | {r['deal_date']} | {price} | {r['area']:g}㎡ | {r['floor']}층 |")
    lines += ["## 오늘 새로 확인된 실거래 신고", ""]
    if new_rows:
        lines += ["| 단지 | 구분 | 계약일 | 가격 | 전용 | 층 |", "|---|---|---|---|---|---|", *new_rows]
    else:
        lines.append("없음")
    lines += ["", "> 데이터: 국토교통부 실거래가 공개시스템 API(신고 기한 30일), 네이버 부동산 매물(호가). "
              "호가는 중복 매물을 제거한 값이며 실제 거래가와 다를 수 있습니다.", ""]
    return "\n".join(lines)


def build_scenario_section(conn: sqlite3.Connection, cfg: Config, ds: str) -> list[str]:
    fin = cfg.finance
    if not fin:
        return []
    rows = conn.execute("SELECT * FROM scenarios WHERE date=?", (ds,)).fetchall()
    if not rows:
        return []
    by = {(r["target_id"], r["scenario"], r["basis"]): r for r in rows}
    m = _metrics_for(conn, ds, cfg.home.id)
    on = date.fromisoformat(ds)
    rules = fin.rules
    dsr_now = scenario.dsr_loan_limit(fin, on)
    income_steps = sorted({str(i.get("start")) for i in fin.incomes if i.get("start")})
    dsr_txt = "소득 미입력(DSR 미반영)" if dsr_now is None else (
        f"DSR 한도 지금 {fmt_won(dsr_now)}" + "".join(
            f" → {d[:7]}부터 {fmt_won(scenario.dsr_loan_limit(fin, date.fromisoformat(d)))}" for d in income_steps))
    out = [
        "## 갈아타기 자금 시나리오",
        "",
        f"가정: 여유자금 {fmt_won(fin.cash)} · 연 저축 {fmt_won(fin.annual_savings)} · 현재 대출 "
        f"{fmt_won(fin.current_loan)} · 희망 대출 {fmt_won(fin.desired_loan)} · 가격 상승률 연 "
        f"{fin.price_growth_rate:.0%} · 대출 = min(희망액, LTV {rules.ltv:.0%}, 가격별 상한 15억↓ 6억 / 25억↓ 4억 / "
        f"25억↑ 2억, {dsr_txt} · 금리 {fin.loan_rate:.1%}+스트레스 {rules.stress_rate:.1%}, {fin.loan_years}년). "
        "**실거래 기준**은 실거래 중위가로 사고팔 때, **호가 기준**은 우리집·대상 모두 최저호가로 거래할 때입니다.",
        "",
        "### 우리집 매도 시점별 손에 남는 돈 (실거래 기준 가격)",
        "",
        "| 매도 시점 | 매도가 | 중개수수료 | 양도세 | 대출 상환 | 순자산 | 비고 |",
        "|---|---|---|---|---|---|---|",
    ]
    trade_home = m.get("sale_trade_median") or m.get("sale_ask_min")
    h = fin.home
    if trade_home:
        points = [("지금", on)]
        if h.acquisition_date:
            acq = date.fromisoformat(h.acquisition_date)
            for yrs, label in ((2, "2년 보유 후"), (3, "3년 보유 후")):
                when = scenario.add_months(acq, yrs * 12 + 1)
                if when > on:
                    points.append((f"{label} ({when:%Y-%m})", when))
        for label, when in points:
            hs = scenario.home_sale(trade_home, fin, when)
            out.append(f"| {label} | {fmt_won(hs.price)} | {fmt_won(hs.brokerage)} | "
                       f"{fmt_won(hs.cgt) if hs.cgt is not None else '?'} | {fmt_won(fin.current_loan)} | "
                       f"**{fmt_won(hs.net_equity)}** | {hs.cgt_note} |")
        steps = [0, 12, 24, 36, 60]
        caps = [(k, scenario.max_affordable_price(fin, trade_home, on, k)) for k in steps]
        out += ["", "**실거주 갈아타기로 살 수 있는 최대 매수가** (그 시점 양도세·DSR·저축 반영, 가격 변동 없음): "
                + " · ".join(f"{'지금' if k == 0 else f'{k // 12}년 후'} **{fmt_won(p)}**" for k, p in caps)]

    def cell(r) -> str:
        if r is None:
            return "- | -"
        return f"{fmt_won(r['surplus'], signed=True)} | {scenario.fmt_when(r['months_needed'], on, fin.horizon_years)}"

    out += [
        "",
        "### ① 실거주 갈아타기 (우리집 매도 → 매수 후 입주)",
        "",
        "필요 자기자본 = 매수가 + 취득세·중개·기타 − 신규 대출. 가용자금 = 우리집 매도 순자산 + 여유자금. "
        "여유(+)/부족(−)은 지금 바로 갈아탈 때, 가능 시점은 매월 저축·양도세 감소·소득 증가(DSR)를 반영해 처음 자금이 맞는 달입니다.",
        "",
        "| 단지 | 매수가(실거래) | 부대비용 | 대출 가능 | 필요 자기자본 | 여유/부족 (실거래) | 가능 시점 | "
        "여유/부족 (호가) | 가능 시점 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for t in cfg.targets:
        r, a = by.get((t.id, "live", "trade")), by.get((t.id, "live", "ask"))
        base = r or a
        if not base:
            continue
        out.append(f"| {t.name} | {fmt_won(base['price'])} | {fmt_won(base['costs'])} | {fmt_won(base['loan'])} | "
                   f"{fmt_won(base['required'])} | {cell(r)} | {cell(a)} |")

    out += [
        "",
        "### ② 갭투자 후 실거주 (우리집 거주 유지 → 전세 끼고 매수 → 나중에 우리집 팔고 입주)",
        "",
    ]
    if not rules.gap_investment_allowed:
        out += ["> ⚠️ **현재 규제상 불가**: 서울 아파트는 토지거래허가구역으로 실거주 목적 매수만 허가되며, "
                "임차인 있는 집 매수 시 실거주 유예도 무주택자에게만 적용됩니다(2027년 말까지). 아래는 규제 해제 시 참고용입니다.",
                ""]
    out += [
        "필요 현금 = 매수가 − 전세가 + 취득세(일시적 2주택 1주택 세율)·중개·기타. 매수 시 대출 불가, 여유자금만 사용. "
        "입주 시 부족액 = 전세금 반환액 − (우리집 매도 순자산 + 신규 대출 한도).",
        "",
        "| 단지 | 매수가 | 전세가 | 필요 현금 | 여유/부족 (실거래) | 가능 시점 | 여유/부족 (호가) | 가능 시점 | 입주 시 부족액 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for t in cfg.targets:
        r, a = by.get((t.id, "gap", "trade")), by.get((t.id, "gap", "ask"))
        base = r or a
        if not base:
            continue
        out.append(f"| {t.name} | {fmt_won(base['price'])} | {fmt_won(base['jeonse'])} | {fmt_won(base['required'])} | "
                   f"{cell(r)} | {cell(a)} | {fmt_won(base['movein_shortfall']) if base['movein_shortfall'] else '없음'} |")
    out += ["", "> 세금·대출은 2026년 10월 기준 단순화 계산입니다. DSR(소득 기준 대출 한도), 보유세, 이사비, "
            "대출 이자 변화는 반영하지 않았습니다. 실제 거래 전 세무사·은행 확인이 필요합니다.", ""]
    return out


def export_csv(conn: sqlite3.Connection, cfg: Config, out_dir: Path) -> None:
    names = {c.id: c.name for c in cfg.complexes}
    with open(out_dir / "scenario_history.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "target_id", "target_name", "scenario", "basis", "price", "jeonse", "costs", "loan",
                    "required", "available", "surplus", "months_needed", "movein_shortfall"])
        for r in conn.execute("SELECT * FROM scenarios ORDER BY date, target_id, scenario, basis"):
            w.writerow([r["date"], r["target_id"], names.get(r["target_id"], ""), scenario.SCENARIOS[r["scenario"]],
                        scenario.BASES[r["basis"]], *(r[k] for k in ("price", "jeonse", "costs", "loan", "required",
                                                                     "available", "surplus", "months_needed",
                                                                     "movein_shortfall"))])
    with open(out_dir / "gap_history.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "target_id", "target_name", "metric", "metric_label",
                    "target_value", "home_value", "gap", "ratio"])
        for r in conn.execute("SELECT * FROM gaps ORDER BY date, target_id, metric"):
            w.writerow([r["date"], r["target_id"], names.get(r["target_id"], ""), r["metric"],
                        METRIC_LABELS.get(r["metric"], ""), r["target_value"], r["home_value"], r["gap"],
                        None if r["ratio"] is None else round(r["ratio"], 4)])
    with open(out_dir / "metrics_history.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "complex_id", "complex_name", "metric", "metric_label", "value"])
        for r in conn.execute("SELECT * FROM metrics ORDER BY date, complex_id, metric"):
            w.writerow([r["date"], r["complex_id"], names.get(r["complex_id"], ""), r["metric"],
                        METRIC_LABELS.get(r["metric"], ""), r["value"]])


DASHBOARD_TEMPLATE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>갈아타기 트래커</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#fff;--fg:#1f2328;--muted:#656d76;--card:#f6f8fa;--border:#d0d7de}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--card:#161b22;--border:#30363d}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Apple SD Gothic Neo",sans-serif}
main{max-width:1100px;margin:0 auto}h1{font-size:1.4rem}p{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin:16px 0}
select{font-size:1rem;padding:4px 8px}canvas{max-height:380px}
</style></head><body><main>
<h1>갈아타기 트래커</h1>
<p>기준: <b id="home"></b> · 마지막 갱신 <span id="updated"></span> · 갭 = 비교단지 − 우리집 (만원)</p>
<label>지표 <select id="metric"></select></label>
<div class="card"><canvas id="gap"></canvas></div>
<div class="card"><canvas id="price"></canvas></div>
</main>
<script>
const DATA = __DATA__;
const sel = document.getElementById('metric');
document.getElementById('home').textContent = DATA.home;
document.getElementById('updated').textContent = DATA.updated;
for (const [k, label] of Object.entries(DATA.labels)) {
  const o = document.createElement('option'); o.value = k; o.textContent = label; sel.appendChild(o);
}
const fmt = v => v == null ? '-' : (Math.abs(v) >= 10000 ? (v/10000).toFixed(2) + '억' : Math.round(v).toLocaleString() + '만');
const opts = title => ({responsive:true, interaction:{mode:'index',intersect:false}, spanGaps:true,
  plugins:{title:{display:true,text:title}, tooltip:{callbacks:{label:c => c.dataset.label + ': ' + fmt(c.parsed.y)}}},
  scales:{x:{type:'category'}, y:{ticks:{callback:fmt}}}, elements:{point:{radius:0}}});
let gapChart, priceChart;
function render() {
  const m = sel.value, dates = DATA.dates;
  const gapSets = Object.entries(DATA.gaps[m] || {}).map(([name, s]) => ({label:name, data:dates.map(d => s[d] ?? null)}));
  const priceSets = Object.entries(DATA.values[m] || {}).map(([name, s]) => ({label:name, data:dates.map(d => s[d] ?? null),
    borderWidth: name === DATA.home ? 3 : 1.5}));
  gapChart?.destroy(); priceChart?.destroy();
  gapChart = new Chart(document.getElementById('gap'), {type:'line', data:{labels:dates, datasets:gapSets}, options:opts('우리집 대비 갭 — ' + DATA.labels[m])});
  priceChart = new Chart(document.getElementById('price'), {type:'line', data:{labels:dates, datasets:priceSets}, options:opts('단지별 ' + DATA.labels[m])});
}
sel.addEventListener('change', render); render();
</script></body></html>
"""


def build_dashboard(conn: sqlite3.Connection, cfg: Config, ds: str) -> str:
    names = {c.id: c.name for c in cfg.complexes}
    dates = [r["date"] for r in conn.execute("SELECT DISTINCT date FROM metrics ORDER BY date")]
    gaps: dict = {m: {} for m in GAP_METRICS}
    for r in conn.execute("SELECT date, target_id, metric, gap FROM gaps WHERE gap IS NOT NULL"):
        if r["metric"] in gaps and r["target_id"] in names:
            gaps[r["metric"]].setdefault(names[r["target_id"]], {})[r["date"]] = r["gap"]
    values: dict = {m: {} for m in GAP_METRICS}
    for r in conn.execute("SELECT date, complex_id, metric, value FROM metrics WHERE value IS NOT NULL"):
        if r["metric"] in values and r["complex_id"] in names:
            values[r["metric"]].setdefault(names[r["complex_id"]], {})[r["date"]] = r["value"]
    labels = {m: METRIC_LABELS[m] for m in GAP_METRICS}
    # 시나리오 여유(+)/부족(−) 추이: '갭' 차트에 표시
    for r in conn.execute("SELECT date, target_id, scenario, basis, surplus FROM scenarios"):
        key = f"scenario:{r['scenario']}:{r['basis']}"
        labels.setdefault(key, f"{scenario.SCENARIOS[r['scenario']]} 여유/부족 ({scenario.BASES[r['basis']]})")
        if r["target_id"] in names:
            gaps.setdefault(key, {}).setdefault(names[r["target_id"]], {})[r["date"]] = r["surplus"]
    data = {"home": cfg.home.name, "updated": ds, "dates": dates,
            "labels": labels, "gaps": gaps, "values": values}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return DASHBOARD_TEMPLATE.replace("__DATA__", payload)


def write_reports(conn: sqlite3.Connection, cfg: Config, ds: str | None = None) -> Path | None:
    ds = ds or latest_date(conn)
    if not ds:
        return None
    out = Path(cfg.settings.report_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.md").write_text(build_markdown(conn, cfg, ds), encoding="utf-8")
    (out / "dashboard.html").write_text(build_dashboard(conn, cfg, ds), encoding="utf-8")
    export_csv(conn, cfg, out)
    return out / "latest.md"
