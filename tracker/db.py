"""SQLite 저장소. 금액 단위는 모두 만원."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    complex_id   TEXT NOT NULL,
    deal_date    TEXT NOT NULL,
    price        INTEGER NOT NULL,
    area         REAL NOT NULL,
    floor        INTEGER NOT NULL DEFAULT 0,
    apt_dong     TEXT NOT NULL DEFAULT '',
    apt_name     TEXT,
    apt_seq      TEXT,
    umd_nm       TEXT,
    jibun        TEXT,
    dealing_gbn  TEXT,
    cancelled    INTEGER NOT NULL DEFAULT 0,
    cancel_date  TEXT,
    cancel_seen  TEXT,                 -- 해제 신고를 처음 확인한 날
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (complex_id, deal_date, area, floor, apt_dong, price)
);
CREATE TABLE IF NOT EXISTS rents (
    complex_id       TEXT NOT NULL,
    deal_date        TEXT NOT NULL,
    deposit          INTEGER NOT NULL,
    monthly_rent     INTEGER NOT NULL DEFAULT 0,
    area             REAL NOT NULL,
    floor            INTEGER NOT NULL DEFAULT 0,
    apt_name         TEXT,
    contract_type    TEXT,
    contract_term    TEXT,
    pre_deposit      INTEGER,
    pre_monthly_rent INTEGER,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    PRIMARY KEY (complex_id, deal_date, area, floor, deposit, monthly_rent)
);
-- 네이버 부동산 호가 스냅샷 (매일 1회)
CREATE TABLE IF NOT EXISTS listings (
    snapshot_date TEXT NOT NULL,
    complex_id    TEXT NOT NULL,
    article_no    TEXT NOT NULL,
    trade_type    TEXT NOT NULL,       -- A1 매매, B1 전세, B2 월세
    price         INTEGER,             -- 매매가 / 보증금
    rent_price    INTEGER,             -- 월세
    area_supply   REAL,
    area          REAL,                -- 전용면적
    floor_info    TEXT,
    building      TEXT,
    direction     TEXT,
    confirm_date  TEXT,
    description   TEXT,
    PRIMARY KEY (snapshot_date, complex_id, article_no)
);
CREATE TABLE IF NOT EXISTS listing_fetches (
    snapshot_date TEXT NOT NULL,
    complex_id    TEXT NOT NULL,
    article_count INTEGER NOT NULL,
    PRIMARY KEY (snapshot_date, complex_id)
);
CREATE TABLE IF NOT EXISTS metrics (
    date       TEXT NOT NULL,
    complex_id TEXT NOT NULL,
    metric     TEXT NOT NULL,
    value      REAL,
    PRIMARY KEY (date, complex_id, metric)
);
-- 우리집 대비 가격차 (gap = 비교단지 - 우리집)
CREATE TABLE IF NOT EXISTS gaps (
    date         TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    metric       TEXT NOT NULL,
    target_value REAL,
    home_value   REAL,
    gap          REAL,
    ratio        REAL,
    PRIMARY KEY (date, target_id, metric)
);
-- 갈아타기 자금 시나리오 결과 (live: 실거주 갈아타기, gap: 갭투자 / basis: trade 실거래, ask 호가)
CREATE TABLE IF NOT EXISTS scenarios (
    date        TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    scenario    TEXT NOT NULL,   -- live / gap
    basis       TEXT NOT NULL,   -- trade / ask
    price       REAL, jeonse REAL, costs REAL, loan REAL,
    required    REAL, available REAL, surplus REAL,
    years_needed INTEGER, movein_shortfall REAL,
    PRIMARY KEY (date, target_id, scenario, basis)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_trade(conn: sqlite3.Connection, complex_id: str, t: dict, seen: str) -> bool:
    """실거래(매매) 저장. 새 레코드면 True. 해제 여부는 갱신."""
    key = (complex_id, t["deal_date"], t["area"], t["floor"] or 0, t.get("apt_dong") or "", t["price"])
    cancelled = int(t.get("cancelled", False))
    exists = conn.execute(
        "SELECT cancelled, cancel_seen FROM trades "
        "WHERE complex_id=? AND deal_date=? AND area=? AND floor=? AND apt_dong=? AND price=?",
        key,
    ).fetchone()
    if exists:
        cancel_seen = exists["cancel_seen"] if exists["cancelled"] else (seen if cancelled else None)
        conn.execute(
            "UPDATE trades SET cancelled=?, cancel_date=?, cancel_seen=?, last_seen=? "
            "WHERE complex_id=? AND deal_date=? AND area=? AND floor=? AND apt_dong=? AND price=?",
            (cancelled, t.get("cancel_date"), cancel_seen if cancelled else None, seen, *key),
        )
        return False
    conn.execute(
        "INSERT INTO trades (complex_id, deal_date, area, floor, apt_dong, price, apt_name, apt_seq, umd_nm, "
        "jibun, dealing_gbn, cancelled, cancel_date, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (*key, t.get("apt_name"), t.get("apt_seq"), t.get("umd_nm"), t.get("jibun"), t.get("dealing_gbn"),
         cancelled, t.get("cancel_date"), seen, seen),
    )
    return True


def upsert_rent(conn: sqlite3.Connection, complex_id: str, r: dict, seen: str) -> bool:
    key = (complex_id, r["deal_date"], r["area"], r["floor"] or 0, r["deposit"], r.get("monthly_rent") or 0)
    exists = conn.execute(
        "SELECT 1 FROM rents WHERE complex_id=? AND deal_date=? AND area=? AND floor=? AND deposit=? AND monthly_rent=?",
        key,
    ).fetchone()
    if exists:
        conn.execute(
            "UPDATE rents SET last_seen=? WHERE complex_id=? AND deal_date=? AND area=? AND floor=? "
            "AND deposit=? AND monthly_rent=?",
            (seen, *key),
        )
        return False
    conn.execute(
        "INSERT INTO rents (complex_id, deal_date, area, floor, deposit, monthly_rent, apt_name, contract_type, "
        "contract_term, pre_deposit, pre_monthly_rent, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (*key, r.get("apt_name"), r.get("contract_type"), r.get("contract_term"), r.get("pre_deposit"),
         r.get("pre_monthly_rent"), seen, seen),
    )
    return True


def replace_listings(conn: sqlite3.Connection, snapshot_date: str, complex_id: str, articles: list[dict]) -> None:
    conn.execute("DELETE FROM listings WHERE snapshot_date=? AND complex_id=?", (snapshot_date, complex_id))
    conn.executemany(
        "INSERT OR REPLACE INTO listings (snapshot_date, complex_id, article_no, trade_type, price, rent_price, "
        "area_supply, area, floor_info, building, direction, confirm_date, description) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (snapshot_date, complex_id, a["article_no"], a["trade_type"], a.get("price"), a.get("rent_price"),
             a.get("area_supply"), a.get("area"), a.get("floor_info"), a.get("building"), a.get("direction"),
             a.get("confirm_date"), a.get("description"))
            for a in articles
        ],
    )
    conn.execute(
        "INSERT OR REPLACE INTO listing_fetches (snapshot_date, complex_id, article_count) VALUES (?,?,?)",
        (snapshot_date, complex_id, len(articles)),
    )


def upsert_metrics(conn: sqlite3.Connection, date: str, complex_id: str, values: dict[str, float | None]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO metrics (date, complex_id, metric, value) VALUES (?,?,?,?)",
        [(date, complex_id, k, v) for k, v in values.items()],
    )


def upsert_gap(conn: sqlite3.Connection, date: str, target_id: str, metric: str,
               target_value: float | None, home_value: float | None) -> None:
    gap = ratio = None
    if target_value is not None and home_value is not None:
        gap = target_value - home_value
        ratio = target_value / home_value if home_value else None
    conn.execute(
        "INSERT OR REPLACE INTO gaps (date, target_id, metric, target_value, home_value, gap, ratio) "
        "VALUES (?,?,?,?,?,?,?)",
        (date, target_id, metric, target_value, home_value, gap, ratio),
    )
