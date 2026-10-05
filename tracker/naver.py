"""네이버 부동산 단지 매물(호가) 수집.

공식 API가 아니므로 응답 구조나 접근 정책이 바뀌면 동작하지 않을 수 있습니다.
네이버는 해외 IP와 브라우저가 아닌 클라이언트를 차단하므로, 국내 PC에서 헤드리스 브라우저(Playwright)로 수집합니다.
요청 간 지연(naver_delay_sec)을 두고 하루 1회 정도로만 사용하세요.
"""
from __future__ import annotations

import json
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


LAND_BASE = "https://new.land.naver.com"
LAND_ARTICLE_PATH = (
    "/api/articles/complex/{no}?realEstateType=APT&tradeType={trade}&tag=%3A%3A%3A%3A%3A%3A%3A%3A"
    "&rentPriceMin=0&rentPriceMax=900000000&priceMin=0&priceMax=900000000&areaMin=0&areaMax=900000000"
    "&oldBuildYears&recentlyBuildYears&minHouseHoldCount&maxHouseHoldCount&showArticle=false"
    "&sameAddressGroup=false&minMaintenanceCost&maxMaintenanceCost&priceType=RETAIL&directions="
    "&page={page}&complexNo={no}&buildingNos=&areaNos=&type=list&order=prc"
)


def parse_land_article(a: dict) -> dict:
    """new.land.naver.com /api/articles/complex 응답의 매물 1건 → 저장 형식."""
    return {
        "article_no": str(a.get("articleNo")),
        "trade_type": a.get("tradeTypeCode"),
        "price": parse_han_price(a.get("dealOrWarrantPrc")),
        "rent_price": parse_han_price(a.get("rentPrc")) or 0,
        "area_supply": _num(a.get("area1")),
        "area": _num(a.get("area2")),
        "floor_info": a.get("floorInfo"),
        "building": a.get("buildingName"),
        "direction": a.get("direction"),
        "confirm_date": a.get("articleConfirmYmd"),
        "description": a.get("articleFeatureDesc"),
    }


_FETCH_JS = """async ([url, auth]) => {
  const headers = {accept: 'application/json'};
  if (auth) headers['authorization'] = auth;
  const r = await fetch(url, {headers, credentials: 'include'});
  return {status: r.status, text: await r.text()};
}"""


class NaverBrowser:
    """헤드리스 브라우저로 네이버 부동산을 열고, 페이지 안에서 매물 API를 호출.

    네이버는 브라우저가 아닌 클라이언트의 API 요청을 차단하므로 실제 브라우저 세션을 사용한다.
    """

    def __init__(self, delay: float = 1.5, max_pages: int = 30, headless: bool = True):
        self.delay, self.max_pages, self.headless = delay, max_pages, headless
        self._pw = self._browser = self.page = None
        self.auth: str | None = None

    def __enter__(self) -> "NaverBrowser":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise NaverError("playwright가 설치되지 않음 (pip install -r requirements-naver.txt)") from e
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        ctx = self._browser.new_context(locale="ko-KR", user_agent=PROBE_PAGE_HEADERS["User-Agent"],
                                        viewport={"width": 1400, "height": 900})
        self.page = ctx.new_page()
        self.page.on("request", self._capture_auth)
        return self

    def __exit__(self, *exc) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def _capture_auth(self, request) -> None:
        auth = request.headers.get("authorization")
        if auth and "/api/" in request.url:
            self.auth = auth

    def open_complex(self, complex_no: str) -> None:
        """단지 페이지를 열어 쿠키와 인증 토큰을 확보."""
        try:
            self.page.goto(f"{LAND_BASE}/complexes/{complex_no}?a=APT&e=RETAIL",
                           wait_until="domcontentloaded", timeout=45000)
        except Exception as e:  # noqa: BLE001
            raise NaverError(f"단지 {complex_no} 페이지 열기 실패: {e}") from e
        for _ in range(30):
            if self.auth:
                break
            self.page.wait_for_timeout(500)

    def fetch_articles(self, complex_no: str, trade_types: str = "A1:B1:B2") -> list[dict]:
        if not self.auth:
            self.open_complex(complex_no)
        out: list[dict] = []
        for page_no in range(1, self.max_pages + 1):
            path = LAND_ARTICLE_PATH.format(no=complex_no, trade=trade_types.replace(":", "%3A"), page=page_no)
            try:
                res = self.page.evaluate(_FETCH_JS, [LAND_BASE + path, self.auth])
            except Exception as e:  # noqa: BLE001
                raise NaverError(f"단지 {complex_no} 매물 조회 실패(page {page_no}): {e}") from e
            if res["status"] == 401 and page_no == 1:
                self.auth = None  # 토큰 만료 → 페이지를 다시 열어 갱신 후 재시도
                self.open_complex(complex_no)
                res = self.page.evaluate(_FETCH_JS, [LAND_BASE + path, self.auth])
            if res["status"] != 200:
                raise NaverError(f"단지 {complex_no} 매물 조회 실패(page {page_no}): HTTP {res['status']} "
                                 f"{res['text'][:200]!r}")
            try:
                data = json.loads(res["text"])
            except ValueError as e:
                raise NaverError(f"단지 {complex_no} 응답 파싱 실패: {res['text'][:200]!r}") from e
            articles = data.get("articleList") or []
            out.extend(parse_land_article(a) for a in articles if a.get("articleNo"))
            if not data.get("isMoreData") or not articles:
                break
            time.sleep(self.delay)
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


def browser_probe(complex_no: str, wait_ms: int = 8000) -> list[str]:
    """실제 브라우저(Playwright)로 단지 매물 페이지를 열고 JSON 응답을 요약 (수집 방식 진단용)."""
    from playwright.sync_api import sync_playwright

    lines: list[str] = []
    urls = [
        f"https://fin.land.naver.com/complexes/{complex_no}?tab=article",
        f"https://new.land.naver.com/complexes/{complex_no}?a=APT&e=RETAIL",
    ]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="ko-KR", user_agent=PROBE_PAGE_HEADERS["User-Agent"],
                                  viewport={"width": 1400, "height": 900})
        for url in urls:
            page = ctx.new_page()
            seen: list[str] = []

            def on_response(resp, seen=seen):
                ct = resp.headers.get("content-type", "")
                if "json" not in ct:
                    return
                try:
                    body = resp.text()
                except Exception as e:  # noqa: BLE001
                    body = f"<읽기 실패 {e}>"
                req = resp.request
                post = (req.post_data or "")[:300] if req.method == "POST" else ""
                seen.append(f"  {req.method} {resp.status} {resp.url[:200]}"
                            + (f"\n    POST={post!r}" if post else "")
                            + f"\n    len={len(body)} body={body[:600]!r}")

            page.on("response", on_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(wait_ms)
                for _ in range(3):
                    page.mouse.wheel(0, 2000)
                    page.wait_for_timeout(1500)
                title = page.title()
            except Exception as e:  # noqa: BLE001
                title = f"<오류 {e}>"
            lines.append(f"[브라우저] {url} title={title!r} JSON응답 {len(seen)}개")
            lines.extend(seen[:40])
            page.close()
        browser.close()
    return lines
