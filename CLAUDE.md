# CLAUDE.md

이 저장소에서 작업할 때 참고하는 가이드입니다.

## 프로젝트 개요

Yahoo Finance 데이터로 두 가지 HTML 보고서를 자동 생성하는 프로젝트입니다.

| 페이지 | 파일 | 내용 |
|---|---|---|
| `index.html` | `futures_report_web.py` | 글로벌 선물 시장 동향 보고서 (통화·귀금속·에너지·곡물) |
| `options.html` | `options_signal_bot.py` | 옵션 매도 신호봇 — 옵션 매도 기회 탐지 + 추천 사유 |

두 페이지는 **동일한 10개 선물 종목**을 대상으로 하며 현재가가 서로 일치해야 합니다.

## 대상 종목 (선물 10종)

| 카테고리 | 종목 | yfinance 심볼 | 행사가 간격 |
|---|---|---|---|
| 외환 | 호주달러 AUD/USD (6A) | `AUDUSD=X` | 0.005 |
| 외환 | 엔 USD/JPY (6J) | `USDJPY=X` | 1.0 |
| 외환 | 유로 EUR/USD (6E) | `EURUSD=X` | 0.005 |
| 금속 | 금 GC | `GC=F` | 25 |
| 금속 | 은 SI | `SI=F` | 0.5 |
| 에너지 | WTI 원유 CL | `CL=F` | 0.5 |
| 에너지 | 천연가스 NG | `NG=F` | 0.1 |
| 농산물 | 옥수수 ZC | `ZC=F` | 10 (¢) |
| 농산물 | 대두 ZS | `ZS=F` | 20 (¢) |
| 농산물 | 밀 ZW | `ZW=F` | 10 (¢) |

**중요**: 옵션 신호봇은 선물을 추종하는 ETF(GLD, USO 등)가 아니라 위 선물 티커를 그대로 써야 합니다. ETF는 방향은 같아도 가격이 달라 보고서와 불일치합니다. (과거 이 버그로 수정한 이력 있음 — PR #3)

## 옵션 신호봇 핵심 로직 (`options_signal_bot.py`)

- **직접 매매 없음** — 옵션 매도 관점의 기회 신호만 생성
- **만기 필터** — 차월물(다음 달) ~ 차차월물(다다음 달)만. 만기일은 셋째 금요일 대표치 (`third_friday`, `target_expiries`), 라벨은 개월수로 판정 (1=차월물, 2=차차월물)
- **전략 3종** — 풋매도(과매도+고변동성), 콜매도(과매수+고변동성), 양매도/스트랭글(횡보+변동성 고점)
- **지표** — pandas 없이 리스트 기반으로 계산: RSI(14, Wilder), 볼린저 %B(20, 2σ), SMA20/50, HV20(20일 연율화 역사변동성), HV 랭크(1년 백분위), 30일 등락, 52주 위치
- **신호 점수** 0~100, `SIGNAL_THRESHOLD = 55` 이상만 표시, 강신호 ≥ 75
- **행사가·프리미엄·델타는 블랙-76 모델 추정치** — 선물 옵션 실시간 체인이 무료 데이터로 없으므로 HV20을 IV 프록시로 사용. `_norm_cdf`는 `math.erf` 기반(scipy 미사용). ATM에서 거래소 행사가 간격만큼 바깥으로 이동하며 |델타| ≤ `TARGET_DELTA`(0.25), 상한 `MAX_DELTA`(0.30). 화면에 "추정치이니 주문 전 HTS 실제 호가 확인" 경고 명시
- `RISK_FREE_RATE = 0.04`
- CLI: `python options_signal_bot.py --generate options.html` / 로컬 서버는 인자 없이 실행 (포트 8081)

## 배포 (이중 구조)

1. **GitHub Actions → gh-pages** (`.github/workflows/generate_report.yml`)
   - `_site/index.html` + `_site/options.html` 생성 후 `peaceiris/actions-gh-pages@v4`로 배포
   - cron `10 21 * * 1-5` (UTC, 한국 새벽 6:10 평일). 수동 실행: Actions → Run workflow
2. **Vercel** (사용자 선호 배포처) — `futures_report_web.py`의 Flask 앱을 서버리스로 서빙
   - `pyproject.toml`에 **`[project]` 테이블(의존성 포함) + `[tool.vercel] entrypoint = "futures_report_web:app"`** 둘 다 필요
     - `[project]` 없으면 Vercel의 uv 빌드가 실패, `entrypoint` 없으면 Flask 앱이 둘(backend/app.py, futures_report_web.py)이라 모호성 에러
   - CDN 캐시로 서버리스 타임아웃 완화: 두 라우트 응답에 `Cache-Control: public, s-maxage=1800, stale-while-revalidate=86400`
   - `/` `/index.html` → 시장 보고서, `/options` `/options.html` → 신호봇 (`run_analysis` + `build_html`)

`backend/app.py`는 Railway용 트리거 UI(별개 Flask 앱). Vercel entrypoint와 무관하므로 건드리지 않음.

## 아이폰 지원

반응형 다크 HTML + `apple-mobile-web-app-capable` 메타 + data-URI apple-touch-icon(📡). 사용자가 Safari 공유 → "홈 화면에 추가"로 웹앱처럼 사용.

## 개발 환경 주의사항

- **샌드박스에서 Yahoo Finance 차단됨** (프록시가 curl CONNECT 403). 로컬에서 실데이터 불가 → 합성/모킹 데이터로 검증. 실데이터는 GitHub Actions/Vercel(무제한)에서 동작
- 오프라인 테스트: `scratchpad/test_bot.py` — 셋째 금요일·블랙-76·풋콜패리티·행사가 정렬·보고서 티커 가격 일치·HTML 렌더 검증
- pandas/scipy/numpy 미사용 (순수 파이썬 + math). 의존성: flask, yfinance, pytz(, requests)

## Git / 브랜치 규칙

- 개발 브랜치: `claude/commodity-options-bot-lkk1u4`
- 머지 후 후속 작업은 `git fetch origin main && git checkout -B claude/commodity-options-bot-lkk1u4 origin/main`로 최신 main에서 재시작 → `--force-with-lease` 푸시 → PR → squash 머지
- 커밋 메시지는 한글 요약 사용. PR 생성은 사용자가 명시적으로 요청할 때만
- 모델 식별자를 커밋/PR/코드/주석 등 저장소 산출물에 넣지 않음
