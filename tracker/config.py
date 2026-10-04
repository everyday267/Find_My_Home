"""config.yaml 로딩 및 검증."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


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
class Config:
    home: Complex
    targets: list[Complex]
    settings: Settings

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
    return Config(home=home, targets=targets, settings=settings)
