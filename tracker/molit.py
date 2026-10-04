"""국토교통부 아파트 매매/전월세 실거래가 API (공공데이터포털 data.go.kr).

- 매매: 국토교통부_아파트 매매 실거래가 상세 자료 (RTMSDataSvcAptTradeDev)
- 전월세: 국토교통부_아파트 전월세 실거래가 자료 (RTMSDataSvcAptRent)
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from urllib.parse import unquote

import requests

TRADE_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"
RENT_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent"
PAGE_SIZE = 1000

# 신규(영문) 필드명과 구버전(한글) 필드명을 모두 지원
ALIASES: dict[str, tuple[str, ...]] = {
    "apt_name": ("aptNm", "아파트"),
    "apt_seq": ("aptSeq",),
    "apt_dong": ("aptDong",),
    "price": ("dealAmount", "거래금액"),
    "year": ("dealYear", "년"),
    "month": ("dealMonth", "월"),
    "day": ("dealDay", "일"),
    "area": ("excluUseAr", "전용면적"),
    "floor": ("floor", "층"),
    "umd_nm": ("umdNm", "법정동"),
    "jibun": ("jibun", "지번"),
    "cdeal_type": ("cdealType", "해제여부"),
    "cdeal_day": ("cdealDay", "해제사유발생일"),
    "dealing_gbn": ("dealingGbn", "거래유형"),
    "deposit": ("deposit", "보증금액"),
    "monthly_rent": ("monthlyRent", "월세금액"),
    "contract_type": ("contractType", "계약구분"),
    "contract_term": ("contractTerm", "계약기간"),
    "pre_deposit": ("preDeposit", "종전계약보증금"),
    "pre_monthly_rent": ("preMonthlyRent", "종전계약월세"),
}


class MolitError(Exception):
    pass


def _to_int(v: str | None) -> int | None:
    if v is None:
        return None
    v = v.replace(",", "").strip()
    if not v or v == "-":
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def _to_float(v: str | None) -> float | None:
    try:
        return float(v.replace(",", "").strip()) if v and v.strip() else None
    except ValueError:
        return None


def _normalize_item(el: ET.Element) -> dict:
    raw = {child.tag: (child.text or "").strip() for child in el}
    get = lambda key: next((raw[a] for a in ALIASES[key] if a in raw and raw[a] != ""), None)  # noqa: E731
    y, m, d = _to_int(get("year")), _to_int(get("month")), _to_int(get("day"))
    cdeal = (get("cdeal_type") or "").upper()
    return {
        "apt_name": get("apt_name"),
        "apt_seq": get("apt_seq"),
        "apt_dong": get("apt_dong") or "",
        "deal_date": f"{y:04d}-{m:02d}-{d:02d}" if y and m and d else None,
        "price": _to_int(get("price")),
        "area": _to_float(get("area")),
        "floor": _to_int(get("floor")) or 0,
        "umd_nm": get("umd_nm"),
        "jibun": get("jibun"),
        "cancelled": cdeal in ("O", "Y"),
        "cancel_date": get("cdeal_day"),
        "dealing_gbn": get("dealing_gbn"),
        "deposit": _to_int(get("deposit")),
        "monthly_rent": _to_int(get("monthly_rent")) or 0,
        "contract_type": get("contract_type"),
        "contract_term": get("contract_term"),
        "pre_deposit": _to_int(get("pre_deposit")),
        "pre_monthly_rent": _to_int(get("pre_monthly_rent")),
    }


def parse_response(xml_text: str) -> tuple[list[dict], int]:
    """XML 응답 → (정규화된 레코드 목록, totalCount)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise MolitError(f"XML 파싱 실패: {xml_text[:200]!r}") from e
    # 인증키 오류 등은 OpenAPI_ServiceResponse 형태로 옴
    auth_msg = root.findtext(".//returnAuthMsg")
    if auth_msg:
        raise MolitError(f"API 오류: {auth_msg} ({root.findtext('.//returnReasonCode')})")
    code = root.findtext(".//header/resultCode") or root.findtext(".//resultCode")
    if code not in (None, "00", "000"):
        raise MolitError(f"API 오류 {code}: {root.findtext('.//resultMsg')}")
    items = [_normalize_item(el) for el in root.iter("item")]
    total = _to_int(root.findtext(".//totalCount")) or len(items)
    return items, total


def normalize_service_key(key: str) -> str:
    # 공공데이터포털의 Encoding 키를 넣어도 동작하도록 디코딩 (requests가 다시 인코딩함)
    return unquote(key) if "%" in key else key


def fetch(url: str, service_key: str, lawd_cd: str, deal_ymd: str,
          session: requests.Session | None = None, retries: int = 3) -> list[dict]:
    session = session or requests.Session()
    key = normalize_service_key(service_key)
    out: list[dict] = []
    page = 1
    while True:
        params = {"serviceKey": key, "LAWD_CD": lawd_cd, "DEAL_YMD": deal_ymd,
                  "pageNo": page, "numOfRows": PAGE_SIZE}
        for attempt in range(retries):
            try:
                resp = session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                items, total = parse_response(resp.text)
                break
            except (requests.RequestException, MolitError):
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt)
        out.extend(items)
        if not items or len(out) >= total:
            return out
        page += 1


def fetch_trades(service_key: str, lawd_cd: str, deal_ymd: str, **kw) -> list[dict]:
    return [i for i in fetch(TRADE_URL, service_key, lawd_cd, deal_ymd, **kw)
            if i["deal_date"] and i["price"] and i["area"]]


def fetch_rents(service_key: str, lawd_cd: str, deal_ymd: str, **kw) -> list[dict]:
    return [i for i in fetch(RENT_URL, service_key, lawd_cd, deal_ymd, **kw)
            if i["deal_date"] and i["deposit"] is not None and i["area"]]
