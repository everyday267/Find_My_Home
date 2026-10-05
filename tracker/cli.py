"""명령행 진입점: python -m tracker <command>"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from tracker import analysis, db, molit, naver, report
from tracker.config import Complex, Config, ConfigError, load_config, normalize_name

log = logging.getLogger("tracker")
KST = ZoneInfo("Asia/Seoul")


def today_kst() -> date:
    return datetime.now(KST).date()


def _service_key() -> str | None:
    return os.environ.get("DATA_GO_KR_SERVICE_KEY") or os.environ.get("MOLIT_SERVICE_KEY")


def collect_molit(conn, cfg: Config, months: list[str], seen: str) -> tuple[int, int, list[str]]:
    """설정된 단지의 실거래를 시군구·월 단위로 조회해 저장. (신규 매매, 신규 전월세, 오류) 반환."""
    key = _service_key()
    if not key:
        return 0, 0, ["DATA_GO_KR_SERVICE_KEY 환경변수가 없어 실거래 수집을 건너뜀"]
    by_lawd: dict[str, list] = defaultdict(list)
    for cx in cfg.complexes:
        by_lawd[cx.lawd_cd].append(cx)
    session = requests.Session()
    new_t = new_r = 0
    errors: list[str] = []
    matched: Counter = Counter()
    seen_names: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for lawd_cd, cxs in by_lawd.items():
        for ym in months:
            for kind, fetcher in (("매매", molit.fetch_trades), ("전월세", molit.fetch_rents)):
                try:
                    items = fetcher(key, lawd_cd, ym, session=session)
                except Exception as e:  # noqa: BLE001 - 한 건 실패가 전체를 멈추지 않도록
                    errors.append(f"{kind} {lawd_cd} {ym}: {e}")
                    continue
                for item in items:
                    seen_names[lawd_cd].add((item["apt_name"] or "", item["umd_nm"] or ""))
                    for cx in cxs:
                        if not cx.matches_molit(item):
                            continue
                        matched[cx.id] += 1
                        if kind == "매매":
                            new_t += db.upsert_trade(conn, cx.id, item, seen)
                        else:
                            new_r += db.upsert_rent(conn, cx.id, item, seen)
            conn.commit()
    failed_lawd = {e.split()[1] for e in errors}
    for cx in cfg.complexes:
        if not matched[cx.id] and seen_names[cx.lawd_cd] and cx.lawd_cd not in failed_lawd:
            errors.append(unmatched_hint(cx, seen_names[cx.lawd_cd]))
    return new_t, new_r, errors


def unmatched_hint(cx: Complex, names: set[tuple[str, str]]) -> str:
    """실거래가 한 건도 매칭되지 않은 단지에 대해 비슷한 단지명을 제안."""
    targets = [normalize_name(n) for n in cx.apt_names]
    scored = []
    for name, umd in names:
        if cx.umd_nm and umd and normalize_name(umd) != normalize_name(cx.umd_nm):
            continue
        score = max(SequenceMatcher(None, normalize_name(name), t).ratio() for t in targets)
        scored.append((score, name, umd))
    best = [f"{n}({u})" for sc, n, u in sorted(scored, reverse=True)[:5] if sc >= 0.4]
    hint = ", ".join(best) if best else "없음"
    return (f"[{cx.name}] 조회 기간 실거래가 0건 — apt_name {cx.apt_names} 확인 필요. "
            f"비슷한 단지명: {hint}")


def collect_naver(conn, cfg: Config, snapshot_date: str) -> list[str]:
    session = requests.Session()
    errors: list[str] = []
    for cx in cfg.complexes:
        if not cx.naver_complex_no:
            continue
        try:
            articles = naver.fetch_articles(cx.naver_complex_no, max_pages=cfg.settings.naver_max_pages,
                                            delay=cfg.settings.naver_delay_sec, session=session)
        except naver.NaverError as e:
            errors.append(str(e))
            continue
        db.replace_listings(conn, snapshot_date, cx.id, articles)
        conn.commit()
        log.info("%s: 매물 %d건", cx.name, len(articles))
    return errors


def cmd_run(cfg: Config, args) -> int:
    day = date.fromisoformat(args.date) if args.date else today_kst()
    ds = day.isoformat()
    conn = db.connect(cfg.settings.db_path)
    errors: list[str] = []
    if not args.skip_molit:
        months = analysis.recent_months(day, cfg.settings.fetch_months)
        new_t, new_r, errs = collect_molit(conn, cfg, months, ds)
        errors += errs
        log.info("실거래 신규: 매매 %d건, 전월세 %d건 (조회월 %s)", new_t, new_r, ",".join(months))
    if not args.skip_naver:
        errors += collect_naver(conn, cfg, ds)
    analysis.compute_day(conn, cfg, day)
    conn.commit()
    path = report.write_reports(conn, cfg, ds)
    log.info("리포트: %s", path)
    for e in errors:
        log.warning(e)
    # 부분 수집 실패는 경고로만 남김 (--strict 시 실패 처리)
    return 1 if errors and args.strict else 0


def cmd_backfill(cfg: Config, args) -> int:
    end = today_kst()
    conn = db.connect(cfg.settings.db_path)
    months = analysis.recent_months(end, args.months)
    new_t, new_r, errors = collect_molit(conn, cfg, months, end.isoformat())
    log.info("백필 실거래: 매매 %d건, 전월세 %d건 (%s ~ %s)", new_t, new_r, months[-1], months[0])
    for e in errors:
        log.warning(e)
    start = analysis.subtract_months(end, args.months).replace(day=1)
    n = analysis.rebuild_trade_history(conn, cfg, start, end, step_days=args.step_days)
    conn.commit()
    log.info("실거래 기반 지표 %d일치 재계산", n)
    report.write_reports(conn, cfg)
    return 1 if errors and not (new_t or new_r) else 0


def cmd_report(cfg: Config, args) -> int:
    conn = db.connect(cfg.settings.db_path)
    if args.recompute:
        day = date.fromisoformat(args.recompute)
        analysis.compute_day(conn, cfg, day)
        conn.commit()
    path = report.write_reports(conn, cfg, args.date)
    print(path or "데이터가 없습니다. 먼저 run 또는 backfill을 실행하세요.")
    return 0


def cmd_find_apt(args) -> int:
    """실거래 API에서 단지명/aptSeq/동/면적을 조회해 config 작성을 돕는다."""
    key = _service_key()
    if not key:
        print("DATA_GO_KR_SERVICE_KEY 환경변수를 설정하세요.", file=sys.stderr)
        return 2
    ym = args.month or today_kst().strftime("%Y%m")
    items = molit.fetch_trades(key, args.lawd_cd, ym) + molit.fetch_rents(key, args.lawd_cd, ym)
    groups: dict[tuple, Counter] = defaultdict(Counter)
    for i in items:
        if args.name and args.name.replace(" ", "") not in (i["apt_name"] or "").replace(" ", ""):
            continue
        groups[(i["apt_name"], i["apt_seq"] or "", i["umd_nm"] or "")][round(i["area"] or 0, 2)] += 1
    if not groups:
        print("결과 없음 (다른 --month 를 시도해보세요)")
        return 1
    print(f"{'단지명(apt_name)':<24} {'apt_seq':<14} {'법정동':<8} 전용면적(건수)")
    for (name, seq, umd), areas in sorted(groups.items()):
        a = ", ".join(f"{k:g}({v})" for k, v in sorted(areas.items()))
        print(f"{name:<24} {seq:<14} {umd:<8} {a}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tracker", description="아파트 갈아타기 타이밍 트래커")
    p.add_argument("-c", "--config", help="설정 파일 (기본: config.yaml 또는 $TRACKER_CONFIG)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="오늘자 실거래·호가 수집 → 지표/갭 저장 → 리포트 생성 (매일 실행)")
    r.add_argument("--date", help="기준일 YYYY-MM-DD (기본: 오늘, KST)")
    r.add_argument("--skip-molit", action="store_true", help="실거래 수집 생략")
    r.add_argument("--skip-naver", action="store_true", help="호가 수집 생략")
    r.add_argument("--strict", action="store_true", help="수집 오류가 하나라도 있으면 종료코드 1")

    b = sub.add_parser("backfill", help="과거 실거래 수집 및 실거래 기반 갭 이력 재구성 (최초 1회)")
    b.add_argument("--months", type=int, default=24)
    b.add_argument("--step-days", type=int, default=1, help="이력 계산 간격(일)")

    rp = sub.add_parser("report", help="저장된 데이터로 리포트만 다시 생성")
    rp.add_argument("--date", help="리포트 기준일 (기본: 최신)")
    rp.add_argument("--recompute", metavar="YYYY-MM-DD", help="해당 날짜 지표/갭을 다시 계산 (설정 변경 후)")

    f = sub.add_parser("find-apt", help="시군구의 단지명·aptSeq·면적 조회 (config 작성용)")
    f.add_argument("lawd_cd", help="법정동코드 앞 5자리 (예: 11680 강남구)")
    f.add_argument("--month", help="조회 계약월 YYYYMM (기본: 이번 달)")
    f.add_argument("--name", help="단지명 일부로 필터")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "find-apt":
        return cmd_find_apt(args)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    return {"run": cmd_run, "backfill": cmd_backfill, "report": cmd_report}[args.command](cfg, args)
