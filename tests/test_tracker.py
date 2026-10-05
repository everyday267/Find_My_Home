import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from tracker import analysis, db, molit, naver, report
from tracker.config import Complex, Config, Settings

FX = Path(__file__).parent / "fixtures"


def make_cfg(tmp_path) -> Config:
    home = Complex(id="home", name="우리아파트 84", lawd_cd="11710", apt_names=["우리아파트"],
                   naver_complex_no="1", area_min=84, area_max=85.99, is_home=True)
    a = Complex(id="a", name="후보A 84", lawd_cd="11710", apt_names=["후보A"], naver_complex_no="2",
                area_min=84, area_max=85.99)
    return Config(home=home, targets=[a],
                  settings=Settings(db_path=str(tmp_path / "t.db"), report_dir=str(tmp_path / "reports")))


def test_parse_trade_xml():
    items, total = molit.parse_response((FX / "trade.xml").read_text())
    assert total == 5
    first = items[0]
    assert first["price"] == 150000 and first["deal_date"] == "2026-09-05"
    assert first["area"] == 84.97 and first["floor"] == 10 and not first["cancelled"]
    assert items[1]["cancelled"] is True


def test_parse_korean_rent_xml():
    items, _ = molit.parse_response((FX / "rent_korean.xml").read_text())
    assert items[0]["deposit"] == 80000 and items[0]["monthly_rent"] == 0
    assert items[1]["monthly_rent"] == 150
    assert items[0]["apt_name"] == "우리아파트"


def test_auth_error():
    with pytest.raises(molit.MolitError, match="SERVICE_KEY"):
        molit.parse_response((FX / "auth_error.xml").read_text())


def test_service_key_decoding():
    assert molit.normalize_service_key("abc%2Bdef%3D%3D") == "abc+def=="
    assert molit.normalize_service_key("abc+def==") == "abc+def=="


def test_name_matching_ignores_spaces():
    cx = Complex(id="a", name="x", lawd_cd="11710", apt_names=["후보A"], umd_nm="신천동")
    assert cx.matches_molit({"apt_name": "후보 A", "umd_nm": "신천동"})
    assert not cx.matches_molit({"apt_name": "후보 A", "umd_nm": "잠실동"})
    seq = Complex(id="b", name="x", lawd_cd="11710", apt_names=["무관"], apt_seq="11710-200")
    assert seq.matches_molit({"apt_name": "후보 A", "apt_seq": "11710-200"})


@pytest.mark.parametrize("text,expected", [("16억", 160000), ("17억 5,000", 175000), ("8,000", 8000),
                                           ("", None), ("가격문의", None)])
def test_han_price(text, expected):
    assert naver.parse_han_price(text) == expected


def test_fmt_won():
    assert report.fmt_won(153000) == "15억 3,000"
    assert report.fmt_won(-5000, signed=True) == "-5,000만"
    assert report.fmt_won(80000, signed=True) == "+8억"


def test_subtract_months_end_of_month():
    assert analysis.subtract_months(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert analysis.recent_months(date(2026, 1, 15), 3) == ["202601", "202512", "202511"]


def _load(conn, cfg, seen="2026-10-04"):
    trades, _ = molit.parse_response((FX / "trade.xml").read_text())
    rents, _ = molit.parse_response((FX / "rent_korean.xml").read_text())
    for cx in cfg.complexes:
        for t in trades:
            if cx.matches_molit(t):
                db.upsert_trade(conn, cx.id, t, seen)
        for r in rents:
            if cx.matches_molit(r):
                db.upsert_rent(conn, cx.id, r, seen)
    data = json.loads((FX / "naver.json").read_text())
    articles = [naver.parse_land_article(a) for a in data["articleList"]]
    db.replace_listings(conn, seen, "home", articles)
    db.replace_listings(conn, seen, "a", [dict(x, price=x["price"] + 70000) for x in articles if x["price"]])


def test_end_to_end(tmp_path):
    cfg = make_cfg(tmp_path)
    conn = db.connect(cfg.settings.db_path)
    _load(conn, cfg)
    day = date(2026, 10, 4)
    analysis.compute_day(conn, cfg, day)

    m = {r["metric"]: r["value"] for r in conn.execute("SELECT * FROM metrics WHERE complex_id='home'")}
    # 해제거래(17억)·59㎡ 제외 → 15억만 남음
    assert m["sale_trade_median"] == 150000 and m["sale_trade_count"] == 1
    assert m["jeonse_trade_median"] == 80000 and m["wolse_trade_count"] == 1
    # 중복 매물(1,2) 제거 → 16억, 17.5억
    assert m["sale_ask_count"] == 2 and m["sale_ask_min"] == 160000
    assert m["sale_ask_median"] == 167500

    g = {r["metric"]: r for r in conn.execute("SELECT * FROM gaps WHERE target_id='a'")}
    assert g["sale_trade_median"]["gap"] == 80000
    assert g["sale_ask_min"]["gap"] == 70000
    assert g["jeonse_trade_median"]["gap"] == 30000

    path = report.write_reports(conn, cfg, day.isoformat())
    md = path.read_text()
    assert "+8억" in md and "후보A 84" in md and "오늘 새로 확인된 실거래" in md
    assert (path.parent / "gap_history.csv").exists()
    assert "Chart" in (path.parent / "dashboard.html").read_text()


def test_upsert_is_idempotent_and_tracks_cancel(tmp_path):
    cfg = make_cfg(tmp_path)
    conn = db.connect(":memory:")
    t = {"deal_date": "2026-09-01", "price": 150000, "area": 84.97, "floor": 3, "apt_dong": ""}
    assert db.upsert_trade(conn, "home", t, "2026-10-01") is True
    assert db.upsert_trade(conn, "home", t, "2026-10-02") is False
    db.upsert_trade(conn, "home", dict(t, cancelled=True), "2026-10-03")
    db.upsert_trade(conn, "home", dict(t, cancelled=True), "2026-10-04")
    row = conn.execute("SELECT * FROM trades").fetchone()
    assert row["cancelled"] == 1 and row["cancel_seen"] == "2026-10-03" and row["first_seen"] == "2026-10-01"
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_gap_signal():
    start = date(2026, 1, 1)
    series = [((start + timedelta(days=i)).isoformat(), 100000 - i * 1000) for i in range(30)]
    sig = analysis.gap_signal(series, series[-1][0])
    assert sig["percentile"] == pytest.approx(1 / 30)
    assert sig["d7"] == -7000 and sig["signal"].startswith("🟢")
    assert analysis.gap_signal(series, "2030-01-01") == {}


def test_unmatched_hint_suggests_similar_names():
    from tracker.cli import unmatched_hint
    cx = Complex(id="x", name="래미안금호하이리버", lawd_cd="11200", apt_names=["래미안금호하이리버"], umd_nm="금호동2가")
    msg = unmatched_hint(cx, {("래미안하이리버", "금호동2가"), ("래미안옥수리버젠", "옥수동"), ("금호자이1차", "금호동2가")})
    assert "래미안하이리버(금호동2가)" in msg and "옥수리버젠" not in msg


def test_parse_land_article():
    data = json.loads((FX / "naver.json").read_text())
    a = [naver.parse_land_article(x) for x in data["articleList"]]
    assert a[0]["price"] == 160000 and a[0]["area"] == 84 and a[0]["building"] == "101동"
    assert a[2]["price"] == 175000
    assert a[4]["trade_type"] == "B2" and a[4]["price"] == 30000 and a[4]["rent_price"] == 150


class FakeBrowser:
    def __init__(self, *a, fail=False, **kw):
        self.calls, self.fail, self.closed = [], fail, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def fetch_articles(self, no):
        self.calls.append(no)
        if self.fail:
            raise naver.NaverError(f"{no} blocked")
        data = json.loads((FX / "naver.json").read_text())
        return [naver.parse_land_article(x) for x in data["articleList"]]


def test_collect_naver_saves_listings(tmp_path, monkeypatch):
    from tracker import cli
    cfg = make_cfg(tmp_path)
    fake = FakeBrowser()
    monkeypatch.setattr(naver, "NaverBrowser", lambda **kw: fake)
    conn = db.connect(":memory:")
    assert cli.collect_naver(conn, cfg, "2026-10-05") == []
    assert fake.calls == ["1", "2"] and fake.closed
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 12


def test_naver_stops_after_consecutive_failures(tmp_path, monkeypatch):
    from tracker import cli
    cfg = make_cfg(tmp_path)
    cfg.targets += [Complex(id=f"t{i}", name=f"t{i}", lawd_cd="11710", naver_complex_no=str(i)) for i in range(5)]
    fake = FakeBrowser(fail=True)
    monkeypatch.setattr(naver, "NaverBrowser", lambda **kw: fake)
    errors = cli.collect_naver(db.connect(":memory:"), cfg, "2026-10-05")
    assert len(fake.calls) == 3 and "중단" in errors[-1] and fake.closed


def _fin(**kw):
    from tracker.config import Finance, HomePurchase
    home = HomePurchase(acquisition_price=80000, acquisition_date="2022-01-27",
                        residence_start="2022-01-27", acquisition_costs=3000)
    return Finance(cash=10000, annual_savings=3000, current_loan=55000, desired_loan=55000, home=home, **kw)


def test_acquisition_tax_and_fees():
    from tracker import scenario as sc
    r = _fin().rules
    assert sc.acquisition_tax(150000, r) == pytest.approx(4950)  # 9억 초과 3% + 교육세 0.3%
    assert sc.acquisition_tax(75000, r) == pytest.approx(1650)  # 7.5억 → 2% + 0.2%
    assert sc.acquisition_tax(200000, r, multi_house=True) == pytest.approx(16800)
    assert sc.brokerage_fee(200000, r) == pytest.approx(1540)  # 0.7% + VAT
    assert sc.loan_limit(200000, r) == 40000 and sc.loan_limit(140000, r) == 56000
    assert sc.loan_limit(300000, r) == 20000


def test_capital_gains_tax_high_price_one_house():
    from tracker import scenario as sc
    tax, note = sc.capital_gains_tax(152000, _fin(), 1170, date(2026, 10, 5))
    assert tax == pytest.approx(1944, abs=2) and "고가주택" in note
    assert sc.capital_gains_tax(110000, _fin(), 900, date(2026, 10, 5))[0] == 0
    from tracker.config import Finance
    assert sc.capital_gains_tax(152000, Finance(), 0, date(2026, 10, 5))[0] is None


def test_live_scenario_uses_regulated_loan():
    from tracker import scenario as sc
    fin = _fin()
    res = sc.live_scenario(fin, 152000, 200000, date(2026, 10, 5), "trade")
    assert res.loan == 40000 and "대출 한도" in res.note
    assert res.surplus < 0 and res.months_needed is not None
    # 여유 = 매도 순자산 + 현금 − (매수가 + 부대비용 − 대출)
    sale = sc.home_sale(152000, fin, date(2026, 10, 5))
    assert res.surplus == pytest.approx(sale.net_equity + 10000 - (200000 + res.costs - 40000))
    assert sc.max_affordable_price(fin, 152000, date(2026, 10, 5)) < 200000


def test_gap_scenario_flags_regulation():
    from tracker import scenario as sc
    res = sc.gap_scenario(_fin(), 152000, 200000, 80000, date(2026, 10, 5), "trade")
    assert not res.allowed and res.loan == 0
    assert res.required == pytest.approx(120000 + sc.buy_costs(200000, _fin().rules))


def _my_fin():
    from tracker.config import Finance, HomePurchase
    home = HomePurchase(acquisition_price=117000, acquisition_date="2025-08-31", residence_start="2025-08-31")
    return Finance(cash=10000, annual_savings=3000, current_loan=55000, desired_loan=55000, home=home,
                   incomes=[{"annual": 12000, "start": None}, {"annual": 4000, "start": "2027-04-01"}])


def test_dsr_limit_with_stress_rate_and_spouse_income():
    from tracker import scenario as sc
    fin = _my_fin()
    assert sc.dsr_loan_limit(fin, date(2026, 10, 5)) == pytest.approx(60124, rel=1e-3)
    assert sc.dsr_loan_limit(fin, date(2027, 4, 1)) == pytest.approx(80166, rel=1e-3)
    from tracker.config import Finance
    assert sc.dsr_loan_limit(Finance(), date(2026, 10, 5)) is None
    assert sc.purchase_loan(fin, 140000, date(2026, 10, 5)) == pytest.approx(55000)  # 희망액이 최소


def test_short_term_sale_tax_drops_after_two_years():
    from tracker import scenario as sc
    fin = _my_fin()
    now, _ = sc.capital_gains_tax(152000, fin, 1170, date(2026, 10, 5))
    later, note = sc.capital_gains_tax(152000, fin, 1170, date(2027, 9, 1))
    assert now == pytest.approx(19190, rel=0.01)  # 보유 2년 미만 60%
    assert later == pytest.approx(930, rel=0.02) and "고가주택" in note


def test_months_to_afford_reflects_tax_timing():
    from tracker import scenario as sc
    fin = _my_fin()
    on = date(2026, 10, 5)
    res = sc.live_scenario(fin, 152000, 150000, on, "trade")
    # 지금은 단기 양도세 때문에 부족하지만 2년 보유(2027-09) 이후 가능
    assert res.surplus < 0 and res.months_needed is not None
    assert sc.add_months(on, res.months_needed) >= date(2027, 8, 31)
    assert sc.fmt_when(res.months_needed, on, 30).endswith(f"({sc.add_months(on, res.months_needed):%Y-%m})")


def test_dashboard_data_groups_by_region(tmp_path):
    cfg = make_cfg(tmp_path)
    conn = db.connect(cfg.settings.db_path)
    _load(conn, cfg)
    analysis.compute_day(conn, cfg, date(2026, 10, 4))
    data = report.build_dashboard_data(conn, cfg, "2026-10-04")
    by = {c["id"]: c for c in data["complexes"]}
    assert by["home"]["home"] and by["a"]["region"] == "송파구" and by["a"]["slot"] == 1
    # 84㎡ 필터·해제거래 제외 → 우리집 15억 1건
    assert [t[1] for t in data["trades"]["home"]] == [150000]
    html = report.build_dashboard(conn, cfg, "2026-10-04")
    assert "__DATA__" not in html and '"region":"송파구"' in html
