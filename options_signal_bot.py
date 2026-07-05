"""
원자재·외환·농산물·금속·에너지 옵션 매도 신호봇
─────────────────────────────────────────────
직접 투자를 하지 않고, 옵션 "매도(Sell)" 관점에서 기회가 되는 신호를
탐지하여 추천 사유와 함께 모바일 친화 HTML 보고서로 생성한다.

- 대상: 옵션이 상장된 원자재/외환/농산물/금속/에너지 ETF (선물 프록시)
- 만기: 차월물(다음 달) ~ 차차월물(다다음 달) 만기만 추천
- 전략: 풋매도(현금담보), 콜매도, 양매도(스트랭글)
- 실행: python options_signal_bot.py --generate options.html
        python options_signal_bot.py  →  http://127.0.0.1:8081 (로컬 확인용)
"""

import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date

import pytz

try:
    import yfinance as yf
    yf.set_tz_cache_location("/tmp")
except Exception:
    yf = None

# ── 유니버스: 옵션이 상장된 ETF (선물 시장 프록시) ─────────────────
UNIVERSE = {
    # key: (yfinance 심볼, 한글명, 카테고리, 기초자산 설명)
    "USO":  ("USO",  "WTI 원유",     "에너지",  "United States Oil Fund — WTI 원유 선물"),
    "UNG":  ("UNG",  "천연가스",     "에너지",  "United States Natural Gas Fund — 헨리허브 천연가스 선물"),
    "GLD":  ("GLD",  "금",           "금속",    "SPDR Gold Shares — 금 현물"),
    "SLV":  ("SLV",  "은",           "금속",    "iShares Silver Trust — 은 현물"),
    "CPER": ("CPER", "구리",         "금속",    "United States Copper Index Fund — 구리 선물"),
    "CORN": ("CORN", "옥수수",       "농산물",  "Teucrium Corn Fund — CBOT 옥수수 선물"),
    "WEAT": ("WEAT", "밀",           "농산물",  "Teucrium Wheat Fund — CBOT 밀 선물"),
    "SOYB": ("SOYB", "대두",         "농산물",  "Teucrium Soybean Fund — CBOT 대두 선물"),
    "DBA":  ("DBA",  "농산물 바스켓", "농산물",  "Invesco DB Agriculture Fund — 농산물 선물 바스켓"),
    "FXE":  ("FXE",  "유로 (EUR)",   "외환",    "Invesco CurrencyShares Euro Trust — EUR/USD"),
    "FXY":  ("FXY",  "엔 (JPY)",     "외환",    "Invesco CurrencyShares Japanese Yen — JPY/USD"),
    "FXA":  ("FXA",  "호주달러 (AUD)","외환",   "Invesco CurrencyShares Australian Dollar — AUD/USD"),
    "UUP":  ("UUP",  "달러인덱스",   "외환",    "Invesco DB US Dollar Index Bullish — 달러인덱스 선물"),
    "DBC":  ("DBC",  "원자재 종합",  "원자재",  "Invesco DB Commodity Index — 원자재 선물 종합"),
}

CATEGORY_COLORS = {
    "에너지":  ("cat-energy", "#68d391"),
    "금속":    ("cat-metal",  "#b794f4"),
    "농산물":  ("cat-grain",  "#9ae6b4"),
    "외환":    ("cat-fx",     "#63b3ed"),
    "원자재":  ("cat-comm",   "#f6ad55"),
}

RISK_FREE_RATE = 0.04          # 델타 추정용 무위험 금리 (근사)
MIN_OPEN_INTEREST = 50         # 최소 미결제약정 (유동성 필터)
MIN_BID = 0.05                 # 최소 매수호가 (프리미엄이 거의 없는 행사가 제외)
TARGET_DELTA = 0.30            # 매도 후보 행사가의 목표 델타 상한
SIGNAL_THRESHOLD = 55          # 이 점수 이상만 "추천 신호"로 표시


# ── 만기 윈도우: 차월물 ~ 차차월물 ──────────────────────────────
def expiry_window(today=None):
    """오늘 기준 다음 달 1일 ~ 다다음 달 말일 범위를 반환."""
    today = today or date.today()
    y, m = today.year, today.month
    # 차월물 시작 (다음 달 1일)
    m1, y1 = (m + 1, y) if m < 12 else (1, y + 1)
    # 차차월물의 다음 달 1일 (배타적 상한)
    m3, y3 = m1 + 2, y1
    while m3 > 12:
        m3 -= 12
        y3 += 1
    return date(y1, m1, 1), date(y3, m3, 1)


def expiry_label(exp_date, today=None):
    """만기일이 차월물인지 차차월물인지 한글 라벨."""
    today = today or date.today()
    months_ahead = (exp_date.year - today.year) * 12 + (exp_date.month - today.month)
    return {1: "차월물", 2: "차차월물"}.get(months_ahead, f"{months_ahead}개월 후")


# ── 지표 계산 (pandas 없이 리스트 기반, 의존성 최소화) ──────────────
def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def _bollinger_pct_b(closes, period=20, num_std=2.0):
    if len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    var = sum((c - mean) ** 2 for c in window) / period
    std = math.sqrt(var)
    if std == 0:
        return 0.5
    upper, lower = mean + num_std * std, mean - num_std * std
    return (closes[-1] - lower) / (upper - lower)


def _sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _hist_vol(closes, period=20):
    """연환산 역사적 변동성 (%)"""
    if len(closes) < period + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - period, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0
    return math.sqrt(var * 252) * 100


def _hv_rank(closes, period=20):
    """현재 HV20이 지난 1년 HV20 분포에서 몇 %ile인지 (IV 환경 프록시)"""
    if len(closes) < period + 30:
        return None
    hvs = []
    for end in range(period + 1, len(closes) + 1):
        hv = _hist_vol(closes[:end], period)
        if hv is not None:
            hvs.append(hv)
    if len(hvs) < 10:
        return None
    current = hvs[-1]
    below = sum(1 for h in hvs if h < current)
    return below / len(hvs) * 100


def compute_indicators(closes):
    if not closes or len(closes) < 30:
        return None
    latest = closes[-1]
    sma20, sma50 = _sma(closes, 20), _sma(closes, 50)
    chg_5d = (latest / closes[-6] - 1) * 100 if len(closes) >= 6 else None
    chg_30d = (latest / closes[-22] - 1) * 100 if len(closes) >= 22 else None
    hi_52w, lo_52w = max(closes), min(closes)
    return {
        "price": latest,
        "rsi": _rsi(closes),
        "pct_b": _bollinger_pct_b(closes),
        "sma20": sma20,
        "sma50": sma50,
        "hv20": _hist_vol(closes, 20),
        "hv_rank": _hv_rank(closes),
        "chg_5d": chg_5d,
        "chg_30d": chg_30d,
        "pos_52w": (latest - lo_52w) / (hi_52w - lo_52w) * 100 if hi_52w > lo_52w else 50,
        "closes": closes[-30:],
    }


# ── 블랙-숄즈 델타 (scipy 없이 math.erf 사용) ─────────────────────
def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_delta(spot, strike, dte_years, iv, is_call, r=RISK_FREE_RATE):
    if spot <= 0 or strike <= 0 or dte_years <= 0 or not iv or iv <= 0:
        return None
    d1 = (math.log(spot / strike) + (r + iv * iv / 2) * dte_years) / (iv * math.sqrt(dte_years))
    return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1


# ── 데이터 조회 ────────────────────────────────────────────────
def fetch_history(symbol, retries=2):
    for attempt in range(retries + 1):
        try:
            hist = yf.Ticker(symbol).history(period="1y")
            if not hist.empty:
                return [float(c) for c in hist["Close"].tolist()]
        except Exception:
            pass
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_chains(symbol, window_start, window_end, retries=2):
    """차월물~차차월물 범위의 만기별 옵션 체인 목록을 반환."""
    results = []
    for attempt in range(retries + 1):
        try:
            tkr = yf.Ticker(symbol)
            expirations = tkr.options or []
            break
        except Exception:
            expirations = []
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    for exp_str in expirations:
        try:
            exp = datetime.strptime(exp_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if not (window_start <= exp < window_end):
            continue
        try:
            chain = tkr.option_chain(exp_str)
            results.append({
                "expiry": exp,
                "calls": chain.calls.to_dict("records"),
                "puts": chain.puts.to_dict("records"),
            })
        except Exception:
            continue
    return results


# ── 매도 후보 행사가 선정 ──────────────────────────────────────
def _mid(row):
    bid = row.get("bid") or 0
    ask = row.get("ask") or 0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    last = row.get("lastPrice") or 0
    return last if last > 0 else None


def pick_short_candidate(rows, spot, expiry, is_call, today=None):
    """목표 델타(≤0.30) 부근의 OTM 행사가 중 프리미엄이 가장 좋은 후보 선택."""
    today = today or date.today()
    dte = (expiry - today).days
    if dte <= 0:
        return None
    t_years = dte / 365.0
    best = None
    for row in rows:
        strike = row.get("strike")
        iv = row.get("impliedVolatility")
        oi = row.get("openInterest") or 0
        bid = row.get("bid") or 0
        if not strike or not iv or iv <= 0.01:
            continue
        if is_call and strike <= spot:
            continue
        if not is_call and strike >= spot:
            continue
        if oi < MIN_OPEN_INTEREST or bid < MIN_BID:
            continue
        delta = bs_delta(spot, strike, t_years, iv, is_call)
        if delta is None:
            continue
        abs_delta = abs(delta)
        if abs_delta > TARGET_DELTA or abs_delta < 0.08:
            continue
        premium = _mid(row)
        if not premium:
            continue
        # 연환산 프리미엄 수익률 (행사가 대비 담보 기준)
        yield_pct = premium / strike * 100
        annualized = yield_pct * 365 / dte
        cand = {
            "strike": strike, "premium": premium, "delta": delta,
            "iv": iv * 100, "oi": int(oi), "dte": dte,
            "yield_pct": yield_pct, "annualized": annualized,
            "otm_pct": abs(strike - spot) / spot * 100,
            "expiry": expiry,
        }
        # 델타 0.20~0.30 구간에서 연환산 수익률 최대인 것을 선호
        if best is None or cand["annualized"] > best["annualized"]:
            best = cand
    return best


# ── 신호 스코어링 + 추천 사유 생성 ─────────────────────────────
def score_put_sell(ind):
    """풋매도: 과매도 반등 구간 + 높은 변동성 환경에서 유리."""
    score, reasons = 0, []
    rsi, pct_b, hv_rank = ind.get("rsi"), ind.get("pct_b"), ind.get("hv_rank")
    price, sma50 = ind.get("price"), ind.get("sma50")
    chg_30d = ind.get("chg_30d")

    if rsi is not None:
        if rsi < 35:
            score += 30
            reasons.append(f"RSI {rsi:.0f} — 과매도 구간. 단기 낙폭 과대로 추가 하락 여력 제한적, OTM 풋매도에 유리")
        elif rsi < 45:
            score += 18
            reasons.append(f"RSI {rsi:.0f} — 매도 우위 구간에서 안정화 중. 하방 지지 형성 시 풋매도 진입 영역")
    if pct_b is not None and pct_b < 0.25:
        score += 20
        reasons.append(f"볼린저밴드 %B {pct_b:.2f} — 밴드 하단 근접. 통계적으로 되돌림(반등) 확률이 높은 위치")
    if hv_rank is not None and hv_rank > 60:
        score += 25
        reasons.append(f"변동성 랭크 {hv_rank:.0f}%ile — 1년 내 상위권 변동성. 옵션 프리미엄이 두껍게 형성되어 매도자에게 유리")
    elif hv_rank is not None and hv_rank > 40:
        score += 12
        reasons.append(f"변동성 랭크 {hv_rank:.0f}%ile — 평균 이상 변동성으로 프리미엄 수취 매력 존재")
    if price is not None and sma50 is not None and price > sma50 * 0.97:
        score += 15
        reasons.append("가격이 50일 이동평균선 부근/상단 유지 — 중기 추세 붕괴 없음, 급락 리스크 상대적으로 낮음")
    if chg_30d is not None and chg_30d < -12:
        score -= 20
        reasons.append(f"⚠ 최근 30일 {chg_30d:.1f}% 급락 — 하락 추세 지속 위험. 풋매도 손실 확대 가능성 유의")
    return score, reasons


def score_call_sell(ind):
    """콜매도: 과매수 피로 구간 + 높은 변동성 환경에서 유리."""
    score, reasons = 0, []
    rsi, pct_b, hv_rank = ind.get("rsi"), ind.get("pct_b"), ind.get("hv_rank")
    pos_52w, chg_30d = ind.get("pos_52w"), ind.get("chg_30d")

    if rsi is not None:
        if rsi > 68:
            score += 30
            reasons.append(f"RSI {rsi:.0f} — 과매수 구간. 단기 상승 피로 누적으로 추가 상승 여력 제한적, OTM 콜매도에 유리")
        elif rsi > 58:
            score += 18
            reasons.append(f"RSI {rsi:.0f} — 매수 우위 구간 후반부. 상승 탄력 둔화 시 콜매도 진입 영역")
    if pct_b is not None and pct_b > 0.75:
        score += 20
        reasons.append(f"볼린저밴드 %B {pct_b:.2f} — 밴드 상단 근접. 통계적으로 되돌림(조정) 확률이 높은 위치")
    if hv_rank is not None and hv_rank > 60:
        score += 25
        reasons.append(f"변동성 랭크 {hv_rank:.0f}%ile — 1년 내 상위권 변동성. 옵션 프리미엄이 두껍게 형성되어 매도자에게 유리")
    elif hv_rank is not None and hv_rank > 40:
        score += 12
        reasons.append(f"변동성 랭크 {hv_rank:.0f}%ile — 평균 이상 변동성으로 프리미엄 수취 매력 존재")
    if pos_52w is not None and pos_52w > 85:
        score += 10
        reasons.append(f"52주 밴드 상단 {pos_52w:.0f}% 위치 — 고점 부담 구간, 상방 저항 예상")
    if chg_30d is not None and chg_30d > 12:
        score -= 15
        reasons.append(f"⚠ 최근 30일 +{chg_30d:.1f}% 급등 — 강한 상승 모멘텀 지속 위험. 콜매도 손실 확대 가능성 유의")
    return score, reasons


def score_strangle(ind):
    """양매도(스트랭글): 방향성 없는 횡보 + 높은 변동성 환경에서 유리."""
    score, reasons = 0, []
    rsi, pct_b, hv_rank = ind.get("rsi"), ind.get("pct_b"), ind.get("hv_rank")
    chg_30d = ind.get("chg_30d")

    if rsi is not None and 42 <= rsi <= 58:
        score += 20
        reasons.append(f"RSI {rsi:.0f} — 중립 구간. 뚜렷한 방향성 없이 횡보 중으로 양매도에 적합한 환경")
    if pct_b is not None and 0.3 <= pct_b <= 0.7:
        score += 15
        reasons.append(f"볼린저밴드 %B {pct_b:.2f} — 밴드 중앙부 위치. 레인지 장세 지속 가능성")
    if hv_rank is not None and hv_rank > 65:
        score += 35
        reasons.append(f"변동성 랭크 {hv_rank:.0f}%ile — 변동성 고점권. 향후 변동성 수축(프리미엄 소멸) 시 양매도 수익 극대화")
    if chg_30d is not None and abs(chg_30d) < 4:
        score += 10
        reasons.append(f"최근 30일 등락 {chg_30d:+.1f}% — 추세 없는 박스권 흐름 확인")
    if chg_30d is not None and abs(chg_30d) > 10:
        score -= 15
        reasons.append(f"⚠ 최근 30일 {chg_30d:+.1f}% — 방향성 강한 장세. 양매도 한쪽 다리 손실 위험 유의")
    return score, reasons


def analyze_symbol(key, today=None):
    """한 종목에 대해 지표 계산 → 체인 조회 → 전략별 신호 생성."""
    symbol, name_kr, category, desc = UNIVERSE[key]
    today = today or date.today()
    closes = fetch_history(symbol)
    if not closes:
        return {"key": key, "name": name_kr, "category": category, "error": "시세 조회 실패"}
    ind = compute_indicators(closes)
    if not ind:
        return {"key": key, "name": name_kr, "category": category, "error": "데이터 부족"}

    w_start, w_end = expiry_window(today)
    chains = fetch_chains(symbol, w_start, w_end)

    strategies = []
    put_score, put_reasons = score_put_sell(ind)
    call_score, call_reasons = score_call_sell(ind)
    str_score, str_reasons = score_strangle(ind)

    for chain in chains:
        exp = chain["expiry"]
        put_cand = pick_short_candidate(chain["puts"], ind["price"], exp, is_call=False, today=today)
        call_cand = pick_short_candidate(chain["calls"], ind["price"], exp, is_call=True, today=today)

        if put_cand and put_score >= SIGNAL_THRESHOLD:
            strategies.append(_make_signal("풋매도", put_score, put_reasons, put_cand, ind, today))
        if call_cand and call_score >= SIGNAL_THRESHOLD:
            strategies.append(_make_signal("콜매도", call_score, call_reasons, call_cand, ind, today))
        if put_cand and call_cand and str_score >= SIGNAL_THRESHOLD:
            combo = {
                "strike": None, "put": put_cand, "call": call_cand,
                "premium": put_cand["premium"] + call_cand["premium"],
                "delta": None, "iv": (put_cand["iv"] + call_cand["iv"]) / 2,
                "oi": min(put_cand["oi"], call_cand["oi"]),
                "dte": put_cand["dte"], "expiry": exp,
                "yield_pct": (put_cand["premium"] + call_cand["premium"]) / ind["price"] * 100,
                "annualized": ((put_cand["premium"] + call_cand["premium"]) / ind["price"] * 100) * 365 / put_cand["dte"],
                "otm_pct": None,
            }
            strategies.append(_make_signal("양매도", str_score, str_reasons, combo, ind, today))

    # 같은 전략이 두 만기 모두에서 나오면 점수·연환산 기준 상위 1개만
    dedup = {}
    for s in strategies:
        k = s["strategy"]
        if k not in dedup or s["candidate"]["annualized"] > dedup[k]["candidate"]["annualized"]:
            dedup[k] = s

    return {
        "key": key, "symbol": symbol, "name": name_kr, "category": category,
        "desc": desc, "indicators": ind, "signals": sorted(dedup.values(), key=lambda s: -s["score"]),
        "chains_found": len(chains),
    }


def _make_signal(strategy, score, reasons, candidate, ind, today):
    reasons = list(reasons)
    exp = candidate["expiry"]
    label = expiry_label(exp, today)
    reasons.append(
        f"{label} {exp.strftime('%m/%d')} 만기 (잔존 {candidate['dte']}일) — "
        f"세타(시간가치 소멸)가 본격화되는 30~90일 구간으로 옵션 매도에 적정한 만기"
    )
    if candidate.get("annualized"):
        reasons.append(
            f"프리미엄 수익률 {candidate['yield_pct']:.2f}% (연환산 약 {candidate['annualized']:.1f}%) — "
            f"수취 프리미엄 대비 담보 효율 양호"
        )
    if candidate.get("delta") is not None:
        reasons.append(
            f"델타 {abs(candidate['delta']):.2f} — 만기 내 행사가 도달 확률 약 {abs(candidate['delta'])*100:.0f}% 수준의 보수적 OTM 행사가"
        )
    return {
        "strategy": strategy, "score": min(int(score), 100), "reasons": reasons,
        "candidate": candidate, "expiry_label": label,
    }


def run_analysis(today=None):
    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        fs = {ex.submit(analyze_symbol, key, today): key for key in UNIVERSE}
        for f in as_completed(fs, timeout=240):
            try:
                results.append(f.result(timeout=90))
            except Exception as e:
                key = fs[f]
                _, name_kr, category, _ = UNIVERSE[key]
                results.append({"key": key, "name": name_kr, "category": category, "error": str(e)})
    order = list(UNIVERSE.keys())
    results.sort(key=lambda r: order.index(r["key"]) if r.get("key") in order else 99)
    return results


# ── HTML 렌더링 ────────────────────────────────────────────────
STYLE = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, 'Apple SD Gothic Neo', 'Segoe UI', sans-serif; background: #0f1117; color: #e2e8f0; line-height: 1.65; -webkit-text-size-adjust: 100%; }
.header { background: linear-gradient(135deg, #1a1f2e 0%, #0f1117 100%); border-bottom: 1px solid #2d3748; padding: 28px 20px 22px; }
.header h1 { font-size: 21px; font-weight: 700; color: #f7fafc; letter-spacing: -0.5px; }
.header .subtitle { margin-top: 6px; font-size: 12.5px; color: #718096; }
.header .date-badge { display: inline-block; margin-top: 10px; background: #2d3748; border: 1px solid #4a5568; border-radius: 20px; padding: 4px 14px; font-size: 12px; color: #a0aec0; }
.nav-link { display: inline-block; margin-top: 10px; margin-left: 8px; font-size: 12px; color: #63b3ed; text-decoration: none; border: 1px solid #2d3748; border-radius: 20px; padding: 4px 14px; }
.banner { background: linear-gradient(90deg, #1a2744 0%, #1a1f2e 100%); border-left: 4px solid #63b3ed; margin: 18px 16px 0; border-radius: 8px; padding: 13px 16px; font-size: 13px; color: #bee3f8; }
.banner strong { color: #63b3ed; }
.warn-banner { background: #1f1a10; border-left: 4px solid #ecc94b; margin: 12px 16px 0; border-radius: 8px; padding: 12px 16px; font-size: 12px; color: #d6bc6b; }
.section-title { font-size: 11px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: #4a5568; padding: 26px 20px 10px; }
.cards { padding: 8px 16px 8px; display: grid; grid-template-columns: 1fr; gap: 14px; }
@media (min-width: 760px) { .cards { grid-template-columns: 1fr 1fr; padding: 8px 32px; } }
.sig-card { background: #1a1f2e; border: 1px solid #2d3748; border-radius: 14px; padding: 18px; }
.sig-card.strong { border-color: #2f6b4f; box-shadow: 0 0 0 1px #1c4532 inset; }
.sig-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
.sig-name { font-size: 16px; font-weight: 700; color: #f7fafc; }
.sig-sym { font-size: 11px; color: #718096; margin-left: 6px; }
.badges { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.badge { font-size: 10.5px; padding: 3px 9px; border-radius: 10px; font-weight: 700; white-space: nowrap; }
.cat-fx { background: #1a365d; color: #63b3ed; }
.cat-metal { background: #322659; color: #b794f4; }
.cat-energy { background: #14321f; color: #68d391; }
.cat-grain { background: #1c4532; color: #9ae6b4; }
.cat-comm { background: #3a2a12; color: #f6ad55; }
.strat-put { background: #1c4532; color: #68d391; }
.strat-call { background: #742a2a; color: #fc8181; }
.strat-strangle { background: #3a2a12; color: #f6ad55; }
.exp-badge { background: #2d3748; color: #a0aec0; }
.score-row { display: flex; align-items: center; gap: 10px; margin: 12px 0 4px; }
.score-bar { flex: 1; height: 7px; background: #2d3748; border-radius: 4px; overflow: hidden; }
.score-fill { height: 100%; border-radius: 4px; }
.score-num { font-size: 13px; font-weight: 700; min-width: 58px; text-align: right; }
.kv-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 12px 0; }
.kv { background: #0f1117; border: 1px solid #2d3748; border-radius: 10px; padding: 8px 10px; }
.kv .k { font-size: 10px; color: #718096; letter-spacing: 0.5px; }
.kv .v { font-size: 14px; font-weight: 700; color: #f7fafc; margin-top: 2px; }
.kv .v small { font-size: 10px; color: #718096; font-weight: 400; }
.reasons { margin-top: 10px; }
.reasons-title { font-size: 11px; font-weight: 700; color: #63b3ed; letter-spacing: 1px; margin-bottom: 6px; }
.reason { font-size: 12.5px; color: #a0aec0; padding: 5px 0 5px 12px; border-left: 2px solid #2d3748; margin-bottom: 4px; }
.reason.warn { border-left-color: #744210; color: #d6bc6b; }
.watch-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.watch-table th { text-align: left; padding: 8px 10px; color: #718096; font-weight: 600; border-bottom: 2px solid #2d3748; background: #1a1f2e; white-space: nowrap; }
.watch-table td { padding: 8px 10px; border-bottom: 1px solid #1a202c; white-space: nowrap; }
.table-wrap { padding: 8px 16px 20px; overflow-x: auto; }
.footer { padding: 18px 20px 40px; font-size: 11px; color: #4a5568; border-top: 1px solid #1a202c; margin-top: 24px; }
.empty-note { padding: 20px; text-align: center; color: #718096; font-size: 13px; }
"""


def _score_color(score):
    if score >= 75:
        return "#68d391"
    if score >= 60:
        return "#ecc94b"
    return "#a0aec0"


def _strat_badge(strategy):
    cls = {"풋매도": "strat-put", "콜매도": "strat-call", "양매도": "strat-strangle"}[strategy]
    icon = {"풋매도": "🟢 PUT 매도", "콜매도": "🔴 CALL 매도", "양매도": "🟠 양매도"}[strategy]
    return f'<span class="badge {cls}">{icon}</span>'


def render_signal_card(asset, sig):
    ind = asset["indicators"]
    cand = sig["candidate"]
    cat_cls = CATEGORY_COLORS.get(asset["category"], ("cat-comm",))[0]
    color = _score_color(sig["score"])
    strong_cls = " strong" if sig["score"] >= 75 else ""

    if sig["strategy"] == "양매도":
        strike_html = (f'<div class="kv"><div class="k">행사가 (풋/콜)</div>'
                       f'<div class="v">{cand["put"]["strike"]:g} / {cand["call"]["strike"]:g}</div></div>')
        delta_html = (f'<div class="kv"><div class="k">델타 (풋/콜)</div>'
                      f'<div class="v">{abs(cand["put"]["delta"]):.2f} / {abs(cand["call"]["delta"]):.2f}</div></div>')
    else:
        otm = f'<small> (OTM {cand["otm_pct"]:.1f}%)</small>' if cand.get("otm_pct") is not None else ""
        strike_html = f'<div class="kv"><div class="k">행사가</div><div class="v">{cand["strike"]:g}{otm}</div></div>'
        delta_html = f'<div class="kv"><div class="k">델타</div><div class="v">{abs(cand["delta"]):.2f}</div></div>'

    reasons_html = "".join(
        f'<div class="reason{" warn" if r.startswith("⚠") else ""}">{r}</div>' for r in sig["reasons"]
    )

    return f"""
  <div class="sig-card{strong_cls}">
    <div class="sig-head">
      <div><span class="sig-name">{asset["name"]}</span><span class="sig-sym">{asset["symbol"]}</span><br>
        <span style="font-size:11px;color:#718096;">현재가 ${ind["price"]:,.2f} · {asset["desc"]}</span></div>
      <div class="badges">
        <span class="badge {cat_cls}">{asset["category"]}</span>
        {_strat_badge(sig["strategy"])}
        <span class="badge exp-badge">{sig["expiry_label"]} {cand["expiry"].strftime("%m/%d")}</span>
      </div>
    </div>
    <div class="score-row">
      <div class="score-bar"><div class="score-fill" style="width:{sig["score"]}%;background:{color};"></div></div>
      <div class="score-num" style="color:{color};">신호 {sig["score"]}점</div>
    </div>
    <div class="kv-grid">
      {strike_html}
      <div class="kv"><div class="k">예상 프리미엄</div><div class="v">${cand["premium"]:.2f}<small> /주</small></div></div>
      <div class="kv"><div class="k">수익률 (연환산)</div><div class="v">{cand["yield_pct"]:.2f}%<small> ({cand["annualized"]:.0f}%/yr)</small></div></div>
      {delta_html}
      <div class="kv"><div class="k">내재변동성</div><div class="v">{cand["iv"]:.0f}%</div></div>
      <div class="kv"><div class="k">잔존일수 / OI</div><div class="v">{cand["dte"]}일<small> / {cand["oi"]:,}</small></div></div>
    </div>
    <div class="reasons">
      <div class="reasons-title">📋 추천 사유</div>
      {reasons_html}
    </div>
  </div>"""


def render_watch_row(asset):
    ind = asset.get("indicators")
    cat_cls = CATEGORY_COLORS.get(asset["category"], ("cat-comm",))[0]
    if asset.get("error") or not ind:
        return (f'<tr><td style="color:#e2e8f0;font-weight:600;">{asset["name"]}</td>'
                f'<td><span class="badge {cat_cls}">{asset["category"]}</span></td>'
                f'<td colspan="5" style="color:#718096;">{asset.get("error", "데이터 없음")}</td></tr>')
    rsi = f'{ind["rsi"]:.0f}' if ind.get("rsi") is not None else "—"
    pct_b = f'{ind["pct_b"]:.2f}' if ind.get("pct_b") is not None else "—"
    hvr = f'{ind["hv_rank"]:.0f}%' if ind.get("hv_rank") is not None else "—"
    chg = ind.get("chg_30d")
    chg_html = (f'<span style="color:{"#68d391" if chg > 0 else "#fc8181" if chg < 0 else "#a0aec0"};">{chg:+.1f}%</span>'
                if chg is not None else "—")
    status = "신호 대기" if not asset.get("signals") else f'✅ 신호 {len(asset["signals"])}건'
    return (f'<tr><td style="color:#e2e8f0;font-weight:600;">{asset["name"]} '
            f'<span style="color:#4a5568;font-size:10px;">{asset["symbol"]}</span></td>'
            f'<td><span class="badge {cat_cls}">{asset["category"]}</span></td>'
            f'<td style="text-align:right;font-weight:700;color:#f7fafc;">${ind["price"]:,.2f}</td>'
            f'<td>{chg_html}</td><td>{rsi}</td><td>{pct_b}</td><td>{hvr}</td>'
            f'<td style="color:{"#68d391" if asset.get("signals") else "#718096"};">{status}</td></tr>')


def build_html(results, today=None, gen_seconds=None):
    today = today or date.today()
    kst = pytz.timezone("Asia/Seoul")
    now_str = datetime.now(kst).strftime("%Y년 %m월 %d일 %H:%M KST")
    w_start, w_end = expiry_window(today)

    all_signals = []
    for asset in results:
        for sig in asset.get("signals", []):
            all_signals.append((asset, sig))
    all_signals.sort(key=lambda x: -x[1]["score"])

    if all_signals:
        top = all_signals[0]
        banner = (f'<div class="banner"><strong>📡 오늘의 신호 {len(all_signals)}건</strong> — '
                  f'최상위: {top[0]["name"]} {_strat_badge(top[1]["strategy"])} '
                  f'신호 {top[1]["score"]}점 · {top[1]["expiry_label"]} '
                  f'{top[1]["candidate"]["expiry"].strftime("%m/%d")} 만기</div>')
        cards_html = "".join(render_signal_card(a, s) for a, s in all_signals)
    else:
        banner = ('<div class="banner"><strong>📡 오늘의 신호 없음</strong> — '
                  '현재 기준을 충족하는 옵션 매도 기회가 없습니다. 아래 관찰 리스트에서 지표를 확인하세요.</div>')
        cards_html = '<div class="empty-note">조건(신호 55점 이상 + 유동성 충족 행사가 존재)을 만족하는 종목이 없습니다.<br>변동성이 낮은 장세에서는 신호가 드물게 발생합니다 — 이는 정상입니다.</div>'

    watch_rows = "".join(render_watch_row(a) for a in results)
    gen_note = f" · 생성 {gen_seconds:.0f}초" if gen_seconds else ""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="옵션 신호봇">
<link rel="apple-touch-icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📡</text></svg>">
<title>옵션 매도 신호봇 — 원자재·외환·농산물·금속·에너지</title>
<style>{STYLE}</style>
</head>
<body>

<div class="header">
  <h1>📡 옵션 매도 신호봇</h1>
  <div class="subtitle">원자재 · 외환 · 농산물 · 금속 · 에너지 — 차월물~차차월물 옵션 매도 기회 탐지</div>
  <div class="date-badge">🗓 {now_str}{gen_note}</div>
  <a class="nav-link" href="./index.html">📊 시장 동향 보고서 →</a>
</div>

{banner}

<div class="warn-banner">⚠ 본 신호는 자동 생성된 참고 정보이며 투자 권유가 아닙니다. 옵션 매도는 수취 프리미엄 대비 큰 손실이 발생할 수 있는 전략입니다 (풋매도: 행사가까지 하락 시 손실, 콜매도: 무제한 상방 리스크). 실제 매매 전 반드시 직접 검증하세요.</div>

<div class="section-title">🎯 매도 신호 ({_window_label(w_start, w_end)} 만기)</div>
<div class="cards">
{cards_html}
</div>

<div class="section-title">👁 전체 관찰 리스트 ({len(results)}종목)</div>
<div class="table-wrap">
  <table class="watch-table">
    <thead><tr>
      <th>자산</th><th>분류</th><th style="text-align:right;">현재가</th><th>30일</th>
      <th>RSI</th><th>%B</th><th>변동성랭크</th><th>상태</th>
    </tr></thead>
    <tbody>{watch_rows}</tbody>
  </table>
</div>

<div class="section-title">📖 신호 해석 가이드</div>
<div class="cards">
  <div class="sig-card">
    <div class="reasons">
      <div class="reason">🟢 <strong>풋매도</strong> — 과매도 + 높은 변동성 구간에서 OTM 풋을 매도해 프리미엄 수취. 기초자산이 행사가 아래로 떨어지지 않으면 프리미엄 전액이 수익.</div>
      <div class="reason">🔴 <strong>콜매도</strong> — 과매수 + 높은 변동성 구간에서 OTM 콜을 매도. 기초자산이 행사가 위로 오르지 않으면 프리미엄 전액이 수익.</div>
      <div class="reason">🟠 <strong>양매도(스트랭글)</strong> — 방향성 없는 횡보 + 변동성 고점 구간에서 OTM 풋·콜 동시 매도. 가격이 두 행사가 사이에 머물면 양쪽 프리미엄 모두 수익.</div>
      <div class="reason"><strong>신호 점수</strong> — RSI·볼린저밴드·변동성 랭크·추세를 종합한 0~100점. 55점 이상만 표시하며 75점 이상은 강한 신호로 강조.</div>
      <div class="reason"><strong>델타</strong> — 만기 시 행사가 도달(ITM) 확률의 근사치. 0.30 이하의 보수적 OTM 행사가만 추천.</div>
      <div class="reason"><strong>만기 선택</strong> — 차월물~차차월물(약 30~90일)만 추천. 시간가치 소멸(세타)이 가장 효율적으로 작동하는 구간.</div>
    </div>
  </div>
</div>

<div class="footer">
  본 보고서는 Yahoo Finance 데이터를 바탕으로 자동 생성되었습니다. 종목은 선물 시장을 추종하는 옵션 상장 ETF 기준입니다.
  신호는 기술적 지표 기반 참고 자료이며, 투자 손실에 대한 책임은 투자자 본인에게 있습니다.
</div>

</body>
</html>"""


def _window_label(w_start, w_end):
    # w_end는 배타적 상한(다다음달+1월 1일)이므로 마지막 포함 월은 그 전 달
    last_m = w_end.month - 1 or 12
    last_y = w_end.year if w_end.month != 1 else w_end.year - 1
    return f"{w_start.year}년 {w_start.month}월 ~ {last_y}년 {last_m}월"


# ── 실행 엔트리 ────────────────────────────────────────────────
def generate_report_file(output_path="options.html", today=None):
    t0 = time.time()
    results = run_analysis(today)
    html = build_html(results, today, gen_seconds=time.time() - t0)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    n_signals = sum(len(a.get("signals", [])) for a in results)
    print(f"[OK] Options signal report saved → {output_path} ({n_signals} signals, {time.time()-t0:.0f}s)")


if __name__ == "__main__":
    if "--generate" in sys.argv:
        idx = sys.argv.index("--generate")
        out = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "options.html"
        generate_report_file(out)
    else:
        from flask import Flask, Response
        app = Flask(__name__)

        @app.route("/")
        def index():
            t0 = time.time()
            results = run_analysis()
            return Response(build_html(results, gen_seconds=time.time() - t0),
                            content_type="text/html; charset=utf-8")

        app.run(debug=True, host="127.0.0.1", port=8081)
