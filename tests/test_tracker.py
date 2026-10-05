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
