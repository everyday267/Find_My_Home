# Find My Home — 갈아타기 타이밍 트래커

내가 사는 아파트(우리집)를 기준으로 관심 단지들의 **매매·전월세 실거래가**와 **호가(매물)** 를 매일 수집해
SQLite에 저장하고, **우리집과의 가격차(갭)** 를 날짜별로 기록합니다.
갭이 과거 대비 얼마나 좁혀졌는지를 보고 갈아타기 타이밍을 판단하는 용도입니다.

## 수집 데이터

| 구분 | 출처 | 비고 |
|---|---|---|
| 매매 실거래 | 국토교통부 아파트 매매 실거래가 상세 자료 API (data.go.kr) | 계약 후 30일 내 신고 → 최근 3개월을 매일 재조회, 해제 거래 자동 제외 |
| 전월세 실거래 | 국토교통부 아파트 전월세 실거래가 자료 API | 전세(월세 0)와 월세 구분 |
| 호가 | 네이버 부동산 단지 매물 목록 | 비공식 엔드포인트, 같은 세대 중복 매물 제거 |

## 계산되는 지표 (단지별·일별, 설정한 전용면적 범위만)

- 매매 실거래 중위가(최근 N개월) / 최근 실거래가 / 건수
- 전세 실거래 중위가 / 전세가율
- 매매·전세 최저호가, 중위호가, 매물 수(증감 = 시장 분위기)

**갭(gaps 테이블)** = 비교단지 − 우리집, 비율 = 비교단지 / 우리집.
리포트에는 7일·30일 변화, 기록 기간 최저/최고, 현재 갭의 백분위가 함께 나오며
하위 20%면 `🟢 갭 축소 — 갈아타기 유리` 신호를 표시합니다.

## 빠른 시작

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 우리집·비교단지 입력
export DATA_GO_KR_SERVICE_KEY='공공데이터포털 인증키'

# 1) 실거래 단지명/면적 확인 (config의 apt_name, apt_seq 작성용)
python -m tracker find-apt 11710 --name 잠실 --month 202609

# 2) 최초 1회: 과거 24개월 실거래 수집 + 실거래 기반 갭 이력 재구성
python -m tracker backfill --months 24

# 3) 매일: 실거래 + 호가 수집 → 지표/갭 저장 → 리포트
python -m tracker run
```

결과물:
- `data/tracker.db` — 전체 원천 데이터와 지표/갭 이력 (SQLite)
- `reports/latest.md` — 오늘 현황, 우리집 대비 갭, 신규 신고 실거래
- `reports/dashboard.html` — 대시보드 (브라우저로 열기)
  - **매매가 추이**: 최근 5년 개별 실거래(점) + 이동 중위 추세선, 지역별 패널에 우리집 함께 표시
  - **우리집 대비 차이**: 비교단지 추세선 − 우리집 추세선 (금액/비율)
  - **실거래 이력**: 선택한 단지의 개별 매매 실거래 목록 (최신순 또는 지역→단지→최신순), 직전 거래·우리집 시세 대비
  - 지역 차트를 누르면 전체 화면으로 열리고, 두 손가락·마우스 휠·＋/－ 버튼으로 확대·축소, 드래그로 이동
  - 왼쪽 체크박스로 지역 단위 일괄 선택/해제, 단지별 선택 (선택 상태는 브라우저에 저장)
- `reports/gap_history.csv`, `reports/metrics_history.csv` — 엑셀/구글시트용

## 준비물

1. **공공데이터포털 인증키**: data.go.kr에서 아래 두 API 활용신청 (자동승인)
   - 국토교통부_아파트 매매 실거래가 상세 자료
   - 국토교통부_아파트 전월세 실거래가 자료
   Encoding/Decoding 키 어느 쪽을 넣어도 됩니다.
2. **법정동코드 앞 5자리**(시군구): 예) 강남구 11680, 서초구 11650, 송파구 11710
3. **네이버 단지번호**: 네이버 부동산에서 단지를 열었을 때 URL `.../complexes/<번호>` 의 숫자
4. **전용면적 범위**: 같은 평형끼리 비교해야 의미가 있으므로 `area_min`/`area_max` 지정 (84타입 → 84~85.99)

## 매일 자동 실행 (GitHub Actions)

`.github/workflows/daily.yml` 이 매일 07:00(KST)에 두 잡을 순서대로 실행하고 `data/`, `reports/` 변경분을 커밋합니다.

| 잡 | 실행 위치 | 하는 일 |
|---|---|---|
| `molit` | GitHub 서버 | 국토부 실거래 수집 (`run --skip-naver`) |
| `naver` | 집 PC의 self-hosted runner | 네이버 호가 수집 (`run --skip-molit`) |

네이버 부동산은 해외 IP(GitHub 서버)를 차단하므로 호가는 국내 IP의 PC에서 수집합니다.
집 PC가 꺼져 있으면 `naver` 잡은 대기하다가 PC가 켜지면 실행됩니다(최대 24시간).
실거래는 PC와 관계없이 매일 수집됩니다.

수동 실행: Actions → daily-tracking → Run workflow → `run`(전체) / `naver`(호가만) / `backfill`(실거래 24개월 재수집)

### self-hosted runner 설치 (집 PC, 최초 1회)

1. PC에 [Python 3.11+](https://www.python.org/downloads/)과 Git 설치 (Windows는 설치 시 "Add python.exe to PATH" 체크)
2. 저장소 **Settings → Actions → Runners → New self-hosted runner** 에서 PC의 OS 선택
3. 화면에 나오는 **Download** 명령을 그대로 실행
4. **Configure** 단계의 `config` 명령을 실행 — 이어지는 질문(runner group, 이름 등)은 아무것도 입력하지 말고 **엔터만**
5. 부팅 시 자동 실행되도록 서비스로 등록
   - Windows: config 중 "run as service?" 질문에 `Y`
   - macOS: `./svc.sh install && ./svc.sh start` (sudo 없이)
   - Linux: `sudo ./svc.sh install && sudo ./svc.sh start`
6. Runners 목록에 초록색 **Idle** 로 보이면 완료 → Run workflow 에서 `naver` 로 테스트

> self-hosted runner는 저장소의 워크플로를 PC에서 실행하므로 **반드시 비공개(Private) 저장소**에서만 사용하세요.

## 아이패드·휴대폰에서 보기 (Cloudflare Pages)

매일 수집이 끝나면 대시보드를 **비밀번호로 암호화(AES-256)** 해서 `https://find-my-home.pages.dev` 에 배포합니다.
비밀번호 없이는 내용을 볼 수 없고, 검색엔진 수집도 막아둡니다. 한 번 입력하고 "이 기기에서 기억하기"를 체크하면 1년간 다시 묻지 않습니다.

필요한 GitHub Secrets (없으면 배포 단계만 건너뜀):

| 이름 | 값 |
|---|---|
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare 대시보드 → 계정 홈 → Account ID |
| `CLOUDFLARE_API_TOKEN` | My Profile → API Tokens → Create Custom Token, 권한 `Account · Cloudflare Pages · Edit` |
| `DASHBOARD_PASSWORD` | 대시보드 비밀번호 (12자 이상 권장, 다른 곳에서 쓰지 않는 것) |

## 갈아타기 자금 시나리오

`config.yaml`의 `finance` 항목(여유자금, 연 저축, 대출, 우리집 취득가 등)을 채우면 매일 리포트에 다음이 추가됩니다.

- **우리집 매도 시 순자산**: 매도가 − 중개수수료 − 양도세(1세대1주택 고가주택 일부과세·장특공) − 대출 상환
- **① 실거주 갈아타기**: 매수가 + 취득세·중개·기타 − 신규 대출(희망액과 규제 한도 중 작은 값) 대비 여유/부족, 저축으로 메우는 데 걸리는 기간, 지금 살 수 있는 최대 매수가
- **② 갭투자 후 실거주**: 매수가 − 전세가 + 부대비용(현금만), 입주 시 전세금 반환 부족액, 규제상 가능 여부
- 실거래 기준(중위가로 매매) / 호가 기준(양쪽 최저호가로 매매) 두 가지로 계산, 이력은 `reports/scenario_history.csv`
- 대출·세금 규칙은 2026년 10월 기준 기본값이며 `finance.rules`로 바꿀 수 있습니다 (DSR·보유세·이사비 미반영)

## 설정을 바꿨을 때

면적 범위·비교 기간 등을 바꾸면 원천 데이터는 그대로 두고 지표만 다시 계산하면 됩니다.

```bash
python -m tracker backfill --months 24      # 실거래 기반 이력 전체 재계산
python -m tracker report --recompute 2026-10-04   # 특정 날짜(호가 포함) 재계산
```

## 테스트

```bash
pip install pytest && python -m pytest -q
```
