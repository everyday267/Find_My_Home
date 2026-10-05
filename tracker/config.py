"""config.yaml 로딩 및 검증."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


# 법정동코드 앞 5자리 → 시군구명 (region 미지정 시 지역 분류에 사용)
SEOUL_GU = {
    "11110": "종로구", "11140": "중구", "11170": "용산구", "11200": "성동구", "11215": "광진구",
    "11230": "동대문구", "11260": "중랑구", "11290": "성북구", "11305": "강북구", "11320": "도봉구",
    "11350": "노원구", "11380": "은평구", "11410": "서대문구", "11440": "마포구", "11470": "양천구",
    "11500": "강서구", "11530": "구로구", "11545": "금천구", "11560": "영등포구", "11590": "동작구",
    "11620": "관악구", "11650": "서초구", "11680": "강남구", "11710": "송파구", "11740": "강동구",
}


def normalize_name(name: str | None) -> str:
    """단지명 비교용: 공백/괄호/특수문자 제거, 소문자화."""
    if not name:
        return ""
    return re.sub(r"[\s()\[\]·.,\-_]", "", str(name)).lower()


@dataclass
class Complex:
    id: str
    name: str
    lawd_cd: str  # 법정동코드 앞 5자리(시군구)
    apt_names: list[str] = field(default_factory=list)  # 국토부 실거래 aptNm 매칭용
    apt_seq: str | None = None  # 국토부 단지 일련번호(있으면 이름보다 우선)
    umd_nm: str | None = None  # 법정동명(동명 단지 구분용)
    naver_complex_no: str | None = None  # 네이버 부동산 단지번호(호가)
    area_min: float | None = None  # 전용면적(㎡) 하한
    area_max: float | None = None  # 전용면적(㎡) 상한
    is_home: bool = False
    region: str | None = None  # 대시보드 지역 분류 (없으면 시군구명)

    def __post_init__(self) -> None:
        if not self.region:
            self.region = SEOUL_GU.get(self.lawd_cd, self.lawd_cd)

    def area_ok(self, area: float | None) -> bool:
        if area is None:
            return False
        if self.area_min is not None and area < self.area_min:
            return False
        if self.area_max is not None and area > self.area_max:
            return False
        return True

    def matches_molit(self, item: dict) -> bool:
        """국토부 실거래 레코드가 이 단지인지 판단 (면적 필터는 저장 후 분석 단계에서 적용)."""
        if self.apt_seq and item.get("apt_seq"):
            return item["apt_seq"] == self.apt_seq
        if normalize_name(item.get("apt_name")) not in {normalize_name(n) for n in self.apt_names}:
            return False
        if self.umd_nm and item.get("umd_nm"):
            return normalize_name(item["umd_nm"]) == normalize_name(self.umd_nm)
        return True


@dataclass
class Settings:
    trade_lookback_months: int = 3  # 실거래 중위가 계산 기간
    fetch_months: int = 3  # 매일 재조회할 최근 계약월 수(신고기한 30일 + 해제신고 반영)
    naver_delay_sec: float = 1.5
    naver_max_pages: int = 30
    db_path: str = "data/tracker.db"
    report_dir: str = "reports"


@dataclass
class Rules:
    """대출·세금 규칙 (2026년 10월 기준 기본값, config의 finance.rules로 변경 가능)."""
    ltv: float = 0.40  # 규제지역(서울 전역) LTV
    # 주택가격별 주담대 상한 (만원): [가격 상한, 대출 상한], 마지막은 가격 상한 null
    loan_caps: list = field(default_factory=lambda: [[150000, 60000], [250000, 40000], [None, 20000]])
    gap_investment_allowed: bool = False  # 토지거래허가구역: 유주택자는 실거주 목적만 매수 허가
    temporary_two_house: bool = True  # 일시적 2주택(종전주택 기한 내 처분) → 1주택 세율·비과세 적용
    multi_house_acq_rate: float = 0.084  # 조정대상지역 2주택 취득세(8%) + 지방교육세(0.4%)
    high_price_threshold: int = 120000  # 1세대1주택 양도세 비과세 고가주택 기준 (만원)
    dsr_limit: float = 0.40  # 은행권 DSR 한도
    stress_rate: float = 0.03  # 수도권·규제지역 주담대 스트레스 금리 하한 (10.15 대책)
    brokerage_vat: float = 0.10  # 중개수수료 부가세
    misc_buy_rate: float = 0.002  # 법무사·채권할인·인지세 등 기타 매수 부대비용


@dataclass
class HomePurchase:
    acquisition_price: int | None = None  # 우리집 취득가 (만원) — 양도세 계산에 필요
    acquisition_date: str | None = None  # 취득일(잔금일) YYYY-MM-DD
    residence_start: str | None = None  # 실거주 시작일 YYYY-MM-DD
    acquisition_costs: int | None = None  # 취득 당시 취득세·중개비 등 필요경비 (만원, 없으면 추정)
    adjusted_area_at_acquisition: bool = False  # 취득 당시 조정대상지역이었으면 비과세에 2년 거주 요건


@dataclass
class Finance:
    cash: int = 0  # 현재 여유자금 (만원)
    annual_savings: int = 0  # 연간 저축액 (만원)
    current_loan: int = 0  # 우리집 현재 대출 잔액 (만원)
    desired_loan: int = 0  # 실거주 갈아타기 시 희망 대출액 (만원)
    horizon_years: int = 30
    price_growth_rate: float = 0.0  # 연 가격 상승률 가정 (모든 단지 동일)
    # 세전 연소득 목록 (DSR 계산): [{annual: 12000, start: null}, {annual: 4000, start: "2027-04-01"}]
    incomes: list = field(default_factory=list)
    # 연 저축액 변경 일정: [{start: "2027-04-01", annual_savings: 5000}]
    savings_changes: list = field(default_factory=list)
    other_debt_payment: int = 0  # 기존 주담대 외 다른 대출의 연간 원리금 (DSR 계산)
    loan_rate: float = 0.04  # 신규 주담대 금리 가정
    loan_years: int = 30  # 주담대 만기
    home: HomePurchase = field(default_factory=HomePurchase)
    rules: Rules = field(default_factory=Rules)


@dataclass
class Config:
    home: Complex
    targets: list[Complex]
    settings: Settings
    finance: Finance | None = None

    @property
    def complexes(self) -> list[Complex]:
        return [self.home, *self.targets]

    def get(self, complex_id: str) -> Complex:
        for c in self.complexes:
            if c.id == complex_id:
                return c
        raise KeyError(complex_id)


def _parse_complex(raw: dict, is_home: bool) -> Complex:
    where = "home" if is_home else f"targets[{raw.get('id', '?')}]"
    for key in ("id", "name", "lawd_cd"):
        if not raw.get(key):
            raise ConfigError(f"{where}: '{key}' 항목이 필요합니다")
    lawd_cd = str(raw["lawd_cd"]).strip()
    if not re.fullmatch(r"\d{5}", lawd_cd):
        raise ConfigError(f"{where}: lawd_cd는 법정동코드 앞 5자리 숫자여야 합니다 (예: 11680)")
    names = raw.get("apt_name") or raw["name"]
    if isinstance(names, str):
        names = [names]
    return Complex(
        id=str(raw["id"]),
        name=str(raw["name"]),
        lawd_cd=lawd_cd,
        apt_names=[str(n) for n in names],
        apt_seq=str(raw["apt_seq"]) if raw.get("apt_seq") else None,
        umd_nm=raw.get("umd_nm"),
        naver_complex_no=str(raw["naver_complex_no"]) if raw.get("naver_complex_no") else None,
        area_min=float(raw["area_min"]) if raw.get("area_min") is not None else None,
        area_max=float(raw["area_max"]) if raw.get("area_max") is not None else None,
        is_home=is_home,
        region=raw.get("region"),
    )


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("TRACKER_CONFIG", "config.yaml"))
    if not path.exists():
        raise ConfigError(f"{path} 파일이 없습니다. config.example.yaml을 복사해 수정하세요.")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "home" not in raw:
        raise ConfigError("config에 'home'(기준 단지)이 필요합니다")
    home = _parse_complex(raw["home"], is_home=True)
    targets = [_parse_complex(t, is_home=False) for t in raw.get("targets") or []]
    ids = [home.id, *(t.id for t in targets)]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ConfigError(f"중복된 id: {', '.join(sorted(dup))}")
    s = raw.get("settings") or {}
    known = Settings.__dataclass_fields__
    settings = Settings(**{k: v for k, v in s.items() if k in known})
    return Config(home=home, targets=targets, settings=settings, finance=_parse_finance(raw.get("finance")))


def _pick(cls, raw: dict | None):
    raw = raw or {}
    unknown = set(raw) - set(cls.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"finance 설정에 알 수 없는 항목: {', '.join(sorted(unknown))}")
    return cls(**raw)


def _parse_finance(raw: dict | None) -> Finance | None:
    if not raw:
        return None
    raw = dict(raw)
    home = _pick(HomePurchase, raw.pop("home", None))
    rules = _pick(Rules, raw.pop("rules", None))
    fin = _pick(Finance, raw)
    fin.home, fin.rules = home, rules
    return fin
