"""네이버 부동산 단지 매물(호가) 수집.

공식 API가 아니므로 응답 구조나 접근 정책이 바뀌면 동작하지 않을 수 있습니다.
요청 간 지연(naver_delay_sec)을 두고 하루 1회 정도로만 사용하세요.
"""
from __future__ import annotations

import re
import time

import requests

ARTICLE_URL = "https://m.land.naver.com/complex/getComplexArticleList"
TRADE_TYPES = {"A1": "매매", "B1": "전세", "B2": "월세"}
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                   "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
    "Referer": "https://m.land.naver.com/",
    "Accept": "application/json, text/plain, */*",
}


class NaverError(Exception):
    pass


def parse_han_price(text: str | None) -> int | None:
    """'12억 5,000' → 125000 (만원), '8,000' → 8000, '12억' → 120000."""
    if not text:
        return None
    t = str(text).replace(",", "").replace(" ", "")
    m = re.fullmatch(r"(?:(\d+)억)?(\d+)?(?:만)?(?:원)?", t)
    if not m or (m.group(1) is None and m.group(2) is None):
        return None
    return int(m.group(1) or 0) * 10000 + int(m.group(2) or 0)


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_article(a: dict) -> dict:
    price = a.get("prc")
    price = int(price) if isinstance(price, (int, float)) and price > 0 else parse_han_price(a.get("hanPrc"))
    rent = a.get("rentPrc")
    rent = int(rent) if isinstance(rent, (int, float)) else parse_han_price(rent)
    return {
        "article_no": str(a.get("atclNo")),
        "trade_type": a.get("tradTpCd"),
        "price": price,
        "rent_price": rent or 0,
        "area_supply": _num(a.get("spc1")),
        "area": _num(a.get("spc2")),
        "floor_info": a.get("flrInfo"),
        "building": a.get("bildNm"),
        "direction": a.get("direction"),
        "confirm_date": a.get("atclCfmYmd"),
        "description": a.get("atclFetrDesc"),
    }


def fetch_articles(complex_no: str, trade_types: str = "A1:B1:B2", max_pages: int = 30,
                   delay: float = 1.5, session: requests.Session | None = None) -> list[dict]:
    session = session or requests.Session()
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {"hscpNo": complex_no, "tradTpCd": trade_types, "order": "prc", "showR0": "N", "page": page}
        try:
            resp = session.get(ARTICLE_URL, params=params, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            raise NaverError(f"단지 {complex_no} 매물 조회 실패(page {page}): {e}") from e
        if not isinstance(data, dict):
            raise NaverError(f"단지 {complex_no} 예상치 못한 응답(page {page}): HTTP {resp.status_code} "
                             f"{resp.headers.get('content-type')} {resp.text[:200]!r}")
        result = data.get("result") or {}
        articles = result.get("list") or []
        out.extend(parse_article(a) for a in articles if a.get("atclNo"))
        if result.get("moreDataYn") != "Y" or not articles:
            break
        time.sleep(delay)
    return out


PROBE_PAGE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def probe(complex_no: str) -> list[str]:
    """여러 네이버 부동산 엔드포인트 응답을 요약 (수집 방식 진단용)."""
    s = requests.Session()
    lines: list[str] = []

    def hit(label: str, url: str, **kw) -> requests.Response | None:
        try:
            r = s.request(kw.pop("method", "GET"), url, timeout=20, **kw)
        except requests.RequestException as e:
            lines.append(f"[{label}] 오류 {e}")
            return None
        body = r.text.replace("\n", " ")
        lines.append(f"[{label}] HTTP {r.status_code} {r.headers.get('content-type')} len={len(r.text)} "
                     f"cookies={sorted(s.cookies.keys())} body={body[:300]!r}")
        return r

    params = {"hscpNo": complex_no, "tradTpCd": "A1", "order": "prc", "showR0": "N", "page": 1}
    hit("m.land 쿠키없음", ARTICLE_URL, params=params, headers=HEADERS)
    hit("m.land 메인", "https://m.land.naver.com/", headers=HEADERS)
    hit("m.land 단지페이지", f"https://m.land.naver.com/complex/info/{complex_no}", headers=HEADERS)
    hit("m.land 쿠키있음", ARTICLE_URL, params=params, headers=HEADERS)

    page = hit("new.land 단지페이지", f"https://new.land.naver.com/complexes/{complex_no}", headers=PROBE_PAGE_HEADERS)
    token = None
    if page is not None:
        m = re.search(r'"token"\s*:\s*"([A-Za-z0-9._-]+)"', page.text) or \
            re.search(r"token\s*[:=]\s*['\"]([A-Za-z0-9._-]{40,})['\"]", page.text)
        token = m.group(1) if m else None
        lines.append(f"[new.land 토큰] {'발견' if token else '없음'}")
    api = (f"https://new.land.naver.com/api/articles/complex/{complex_no}?realEstateType=APT&tradeType=A1"
           f"&order=prc&page=1&complexNo={complex_no}&type=list")
    api_headers = dict(PROBE_PAGE_HEADERS, Referer=f"https://new.land.naver.com/complexes/{complex_no}")
    hit("new.land api 인증없음", api, headers=api_headers)
    if token:
        hit("new.land api 토큰", api, headers=dict(api_headers, authorization=f"Bearer {token}"))

    hit("fin.land 단지페이지", f"https://fin.land.naver.com/complexes/{complex_no}", headers=PROBE_PAGE_HEADERS)
    fin_headers = dict(PROBE_PAGE_HEADERS, Referer=f"https://fin.land.naver.com/complexes/{complex_no}",
                       Origin="https://fin.land.naver.com")
    hit("fin.land 단지정보", f"https://fin.land.naver.com/front-api/v1/complex?complexNumber={complex_no}",
        headers=fin_headers)
    hit("fin.land 매물목록", "https://fin.land.naver.com/front-api/v1/complex/article/list", method="POST",
        headers=fin_headers, json={"complexNumber": complex_no, "tradeTypes": ["A1"], "pyeongTypes": [],
                                   "dongNumbers": [], "userChannelType": "PC", "articleSortType": "PRICE_ASC",
                                   "seed": "", "lastInfo": [], "size": 30})
    return lines
