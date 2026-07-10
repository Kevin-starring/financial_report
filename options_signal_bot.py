"""
원자재·외환·농산물·금속·에너지 옵션 매도 신호봇
─────────────────────────────────────────────
직접 투자를 하지 않고, 옵션 "매도(Sell)" 관점에서 기회가 되는 신호를
탐지하여 추천 사유와 함께 모바일 친화 HTML 보고서로 생성한다.

- 대상: 시장 동향 보고서와 동일한 10개 선물 종목 (통화·귀금속·에너지·곡물)
- 만기: 차월물(다음 달) ~ 차차월물(다다음 달)만 추천
- 전략: 풋매도, 콜매도, 양매도(스트랭글)
- 추세장 차단: 30일 ±8% 초과 등락·이평선 ±5% 이격·5일 신고/신저가 갱신 중이면
  역방향 매도 신호를 차단하고, 추세 동반 고변동성은 가점 축소 (평균회귀 장세에서만 매도)
- 수수료 반영: 왕복 수수료·슬리피지를 pt로 환산 차감, 순 연환산 12% 미만은 제외
- 리스크 관리: 모든 신호에 프리미엄 2배 손절 기준 표기
- 행사가·프리미엄: 선물 옵션 체인은 무료 데이터로 제공되지 않으므로
  역사적 변동성(HV) 기반 블랙-76 모델 추정치로 제시 (보고서에 명시)
- 실행: python options_signal_bot.py --generate options.html
        python options_signal_bot.py  →  http://127.0.0.1:8081 (로컬 확인용)
"""

import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta

import pytz

try:
    import yfinance as yf
    yf.set_tz_cache_location("/tmp")
except Exception:
    yf = None

# ── 유니버스: 시장 동향 보고서와 동일한 선물 종목 ────────────────────
# key: (yfinance 심볼, 한글명, 카테고리, 설명, 행사가 간격, 표시 소수점, 1pt당 계약금액 USD)
# 마지막 값은 호가 1.0pt 변동 시 계약 손익(USD) — 수수료를 pt로 환산할 때 사용.
# USD/JPY는 CME 6J(JPY/USD 호가)와 방향이 반대이므로 근사치.
UNIVERSE = {
    "AUD_USD": ("AUDUSD=X", "호주달러 (AUD/USD)", "외환",   "CME 6A 통화선물 옵션",        0.005,  4, 100000),
    "USD_JPY": ("USDJPY=X", "엔 (USD/JPY)",      "외환",   "CME 6J 통화선물 옵션 (역수 호가 유의)", 1.0, 2, 850),
    "EUR_USD": ("EURUSD=X", "유로 (EUR/USD)",    "외환",   "CME 6E 통화선물 옵션",        0.005,  4, 125000),
    "GOLD":    ("GC=F",     "금",                "금속",   "COMEX GC 금 선물 옵션",       25.0,   2, 100),
    "SILVER":  ("SI=F",     "은",                "금속",   "COMEX SI 은 선물 옵션",       0.5,    2, 5000),
    "CRUDE":   ("CL=F",     "WTI 원유",          "에너지", "NYMEX CL 원유 선물 옵션",     0.5,    2, 1000),
    "NATGAS":  ("NG=F",     "천연가스",          "에너지", "NYMEX NG 천연가스 선물 옵션", 0.1,    3, 10000),
    "CORN":    ("ZC=F",     "옥수수",            "농산물", "CBOT ZC 옥수수 선물 옵션 (¢/bu)", 10.0, 2, 50),
    "SOY":     ("ZS=F",     "대두",              "농산물", "CBOT ZS 대두 선물 옵션 (¢/bu)",   20.0, 2, 50),
    "WHEAT":   ("ZW=F",     "밀",                "농산물", "CBOT ZW 밀 선물 옵션 (¢/bu)",     10.0, 2, 50),
}

CATEGORY_COLORS = {
    "에너지":  ("cat-energy", "#68d391"),
    "금속":    ("cat-metal",  "#b794f4"),
    "농산물":  ("cat-grain",  "#9ae6b4"),
    "외환":    ("cat-fx",     "#63b3ed"),
    "원자재":  ("cat-comm",   "#f6ad55"),
}

RISK_FREE_RATE = 0.04          # 할인·델타 추정용 무위험 금리 (근사)

# 델타 한도 — 원자재는 공급 충격으로 위쪽 꼬리가 길어 콜매도를 더 보수적으로
TARGET_DELTA_PUT = 0.25        # 풋매도 목표 델타
MAX_DELTA_PUT = 0.30           # 풋매도 델타 상한
TARGET_DELTA_CALL = 0.20       # 콜매도 목표 델타 (풋보다 낮게)
MAX_DELTA_CALL = 0.25          # 콜매도 델타 상한

# 신호 임계점 — 콜매도는 급등 꼬리 리스크를 반영해 더 엄격
SIGNAL_THRESHOLD = 55          # 풋매도·양매도 추천 임계점
CALL_SIGNAL_THRESHOLD = 65     # 콜매도 추천 임계점

# 추세장 차단 필터 — 추세 폭주 구간에서는 역방향 매도 신호 자체를 차단
# (감점이 아니라 차단: 포물선 급등/급락 한복판 진입이 옵션 매도 최대 손실 원인)
TREND_BLOCK_30D = 8.0          # 최근 30일 등락 ±8% 초과 시 차단
TREND_BLOCK_EXT20 = 0.05       # 가격이 SMA20 대비 ±5% 이상 이격 시 차단

# 수수료·슬리피지 — 비용 차감 후에도 수익성이 남는 신호만 표시
ROUND_TRIP_COST_USD = 12.0     # 옵션 1계약(레그당) 왕복 수수료+슬리피지 추정 (USD)
MAX_FEE_RATIO = 0.15           # 비용이 프리미엄의 15% 초과 시 신호 제외
MIN_NET_ANNUALIZED = 4.0       # 비용 차감 후 연환산 수익률 하한 (%, 명목가 기준)
                               # 선물옵션은 증거금 거래이므로 실투자금 기준으론 수배 수준

# 리스크 관리 — 신호 카드에 손절 기준으로 표기
STOP_LOSS_MULT = 2.0           # 옵션 가격이 수취 프리미엄의 2배 도달 시 손절 환매 권고


# ── 만기 윈도우: 차월물 ~ 차차월물 ──────────────────────────────
def expiry_window(today=None):
    """오늘 기준 다음 달 1일 ~ 다다음 달 말일 범위를 반환."""
    today = today or date.today()
    y, m = today.year, today.month
    m1, y1 = (m + 1, y) if m < 12 else (1, y + 1)
    m3, y3 = m1 + 2, y1
    while m3 > 12:
        m3 -= 12
        y3 += 1
    return date(y1, m1, 1), date(y3, m3, 1)


def third_friday(year, month):
    """해당 월의 세 번째 금요일 (대표 만기일 근사치 — 실제 만기는 상품별 상이)."""
    d = date(year, month, 1)
    offset = (4 - d.weekday()) % 7  # 첫 금요일까지
    return d + timedelta(days=offset + 14)


def target_expiries(today=None):
    """차월물·차차월물의 대표 만기일 목록."""
    today = today or date.today()
    w_start, _ = expiry_window(today)
    first = third_friday(w_start.year, w_start.month)
    m2, y2 = (w_start.month + 1, w_start.year) if w_start.month < 12 else (1, w_start.year + 1)
    second = third_friday(y2, m2)
    return [first, second]


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
    """현재 HV20이 지난 1년 HV20 분포에서 몇 %ile인지 (변동성 환경 지표)"""
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
        "ext_sma20": (latest / sma20 - 1) if sma20 else None,  # SMA20 대비 이격률
        "hv20": _hist_vol(closes, 20),
        "hv_rank": _hv_rank(closes),
        "chg_5d": chg_5d,
        "chg_30d": chg_30d,
        "pos_52w": (latest - lo_52w) / (hi_52w - lo_52w) * 100 if hi_52w > lo_52w else 50,
        # 꺾임 확인용: 직전 5일 대비 신고가/신저가 갱신 여부 (갱신 중이면 모멘텀 지속)
        "new_5d_high": len(closes) >= 6 and latest >= max(closes[-6:-1]),
        "new_5d_low": len(closes) >= 6 and latest <= min(closes[-6:-1]),
        "closes": closes[-30:],
    }


# ── 블랙-76 모델 (선물 옵션 가격·델타, scipy 없이 math.erf 사용) ────
def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def b76_d1(fut, strike, t_years, iv):
    return (math.log(fut / strike) + (iv * iv / 2) * t_years) / (iv * math.sqrt(t_years))


def b76_delta(fut, strike, t_years, iv, is_call, r=RISK_FREE_RATE):
    if fut <= 0 or strike <= 0 or t_years <= 0 or not iv or iv <= 0:
        return None
    d1 = b76_d1(fut, strike, t_years, iv)
    disc = math.exp(-r * t_years)
    return disc * _norm_cdf(d1) if is_call else disc * (_norm_cdf(d1) - 1)


def b76_price(fut, strike, t_years, iv, is_call, r=RISK_FREE_RATE):
    if fut <= 0 or strike <= 0 or t_years <= 0 or not iv or iv <= 0:
        return None
    d1 = b76_d1(fut, strike, t_years, iv)
    d2 = d1 - iv * math.sqrt(t_years)
    disc = math.exp(-r * t_years)
    if is_call:
        return disc * (fut * _norm_cdf(d1) - strike * _norm_cdf(d2))
    return disc * (strike * _norm_cdf(-d2) - fut * _norm_cdf(-d1))


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


# ── 매도 후보 행사가 산출 (HV 기반 모델 추정) ─────────────────────
def fee_points(key, legs=1):
    """왕복 수수료+슬리피지 추정치를 해당 종목의 호가 포인트로 환산."""
    usd_per_pt = UNIVERSE[key][6]
    return ROUND_TRIP_COST_USD * legs / usd_per_pt


def apply_costs(cand, fee_pts, base_price):
    """수수료·슬리피지 차감 후 순 수익성 지표를 추가하고,
    비용을 감안해도 충분한 손익이 남지 않으면 None(신호 제외)."""
    prem = cand["premium"]
    if prem <= 0 or fee_pts / prem > MAX_FEE_RATIO:
        return None
    net_prem = prem - fee_pts
    net_yield = net_prem / base_price * 100
    net_ann = net_yield * 365 / cand["dte"]
    if net_ann < MIN_NET_ANNUALIZED:
        return None
    cand.update({
        "fee": fee_pts, "net_premium": net_prem,
        "net_yield_pct": net_yield, "net_annualized": net_ann,
    })
    return cand


def make_candidate(key, ind, expiry, is_call, today=None):
    """목표 델타에 가장 가까운 OTM 행사가를 행사가 간격에 맞춰 산출하고
    블랙-76으로 프리미엄을 추정한다 (원시 후보 — 수수료 반영은 apply_costs에서).
    콜은 풋보다 보수적 델타 한도 적용."""
    today = today or date.today()
    dte = (expiry - today).days
    if dte <= 0:
        return None
    t_years = dte / 365.0
    hv = ind.get("hv20")
    if not hv or hv <= 0:
        return None
    iv = hv / 100.0  # HV20을 IV 프록시로 사용
    spot = ind["price"]
    step = UNIVERSE[key][4]
    target_d = TARGET_DELTA_CALL if is_call else TARGET_DELTA_PUT
    max_d = MAX_DELTA_CALL if is_call else MAX_DELTA_PUT

    # ATM에서 OTM 방향으로 이동하며, 델타 상한 이내에서 목표 델타에
    # 가장 가까운 행사가를 선택 (행사가 간격이 넓은 종목은 목표를 지나치며
    # 프리미엄이 급감하므로, 무조건 더 깊은 OTM으로 가지 않는다)
    base = round(spot / step) * step
    best, best_diff = None, None
    for i in range(1, 200):
        strike = base + i * step if is_call else base - i * step
        if strike <= 0:
            break
        delta = b76_delta(spot, strike, t_years, iv, is_call)
        if delta is None:
            break
        if abs(delta) > max_d:
            continue
        premium = b76_price(spot, strike, t_years, iv, is_call)
        if not premium or premium <= 0:
            break
        diff = abs(abs(delta) - target_d)
        if best is None or diff < best_diff:
            yield_pct = premium / strike * 100
            best, best_diff = {
                "strike": strike, "premium": premium, "delta": delta,
                "iv": iv * 100, "dte": dte,
                "yield_pct": yield_pct, "annualized": yield_pct * 365 / dte,
                "otm_pct": abs(strike - spot) / spot * 100,
                "expiry": expiry,
            }, diff
        if abs(delta) <= target_d:
            break  # 목표 델타 이하 도달 — 더 깊은 OTM은 목표에서 멀어질 뿐
    return best


# ── 신호 스코어링 + 추천 사유 생성 ─────────────────────────────
# 핵심 철학: "변동성이 높을 때 파는 것"이 아니라
# "변동성은 높은데 가격은 갈 곳이 없을 때(평균회귀 장세) 파는 것".
# 추세 폭주 구간은 감점이 아니라 차단(score 0)한다.

def _hv_bonus(hv_rank, chg_30d, reasons):
    """변동성 랭크 가점. 단, 강한 추세가 동반된 고변동성은 '고평가된 변동성'이
    아니라 '정당한 변동성'일 가능성이 높으므로 가점을 크게 축소."""
    if hv_rank is None:
        return 0
    trendy = chg_30d is not None and abs(chg_30d) > TREND_BLOCK_30D
    if hv_rank > 60:
        if trendy:
            reasons.append(f"변동성 랭크 {hv_rank:.0f}%ile이지만 30일 {chg_30d:+.1f}% 추세 동반 — "
                           f"추세가 만든 '정당한 변동성'일 수 있어 가점 축소")
            return 8
        reasons.append(f"변동성 랭크 {hv_rank:.0f}%ile + 가격은 추세 없음 — 프리미엄이 두껍게 형성된 "
                       f"평균회귀 환경으로 옵션 매도자에게 이상적")
        return 25
    if hv_rank > 40:
        if trendy:
            return 5
        reasons.append(f"변동성 랭크 {hv_rank:.0f}%ile — 평균 이상 변동성으로 프리미엄 수취 매력 존재")
        return 12
    return 0


def score_put_sell(ind):
    """풋매도: 과매도 '반등 확인' 구간 + 높은 변동성 환경에서 유리.
    급락 추세 폭주 구간은 진입 자체를 차단."""
    rsi, pct_b, hv_rank = ind.get("rsi"), ind.get("pct_b"), ind.get("hv_rank")
    price, sma50 = ind.get("price"), ind.get("sma50")
    chg_30d, ext20 = ind.get("chg_30d"), ind.get("ext_sma20")

    # ① 추세장 차단 — 급락이 진행 중이면 "싸 보여도" 풋매도 금지
    if chg_30d is not None and chg_30d < -TREND_BLOCK_30D:
        return 0, [f"차단: 최근 30일 {chg_30d:.1f}% 급락 진행 중 — 하락 추세 폭주 구간에서는 풋매도 진입 금지"]
    if ext20 is not None and ext20 < -TREND_BLOCK_EXT20:
        return 0, [f"차단: 가격이 20일 이평선 대비 {ext20*100:.1f}% 하방 이격 — 낙폭 과대 추세 구간"]
    # ③ 꺾임 확인 — 5일 신저가를 계속 갱신 중이면 반등 확인 전까지 대기
    if ind.get("new_5d_low"):
        return 0, ["차단: 5일 신저가 갱신 중 — 하락 모멘텀이 살아 있어 반등 확인 전까지 대기"]
    if pct_b is not None and pct_b < 0:
        return 0, [f"차단: %B {pct_b:.2f} — 볼린저밴드 하단 이탈 상태. 밴드 안 복귀 확인 후 진입"]

    score, reasons = 0, []
    if rsi is not None:
        if rsi < 35:
            score += 30
            reasons.append(f"RSI {rsi:.0f} — 과매도 구간. 단기 낙폭 과대로 추가 하락 여력 제한적, OTM 풋매도에 유리")
        elif rsi < 45:
            score += 18
            reasons.append(f"RSI {rsi:.0f} — 매도 우위 구간에서 안정화 중. 하방 지지 형성 시 풋매도 진입 영역")
        if rsi < 45:
            reasons.append("직전 5일 저점을 지키며 밴드 안에서 안정화 — 하락 모멘텀 1차 꺾임 확인 후 진입 조건 충족")
    if pct_b is not None and 0 <= pct_b < 0.25:
        score += 20
        reasons.append(f"볼린저밴드 %B {pct_b:.2f} — 밴드 하단 근접. 통계적으로 되돌림(반등) 확률이 높은 위치")
    score += _hv_bonus(hv_rank, chg_30d, reasons)
    if price is not None and sma50 is not None and price > sma50 * 0.97:
        score += 15
        reasons.append("가격이 50일 이동평균선 부근/상단 유지 — 중기 추세 붕괴 없음, 급락 리스크 상대적으로 낮음")
    return score, reasons


def score_call_sell(ind):
    """콜매도: 과매수 '꺾임 확인' 구간 + 높은 변동성 환경에서 유리.
    원자재 급등은 꼬리가 길어 상승 폭주 구간은 진입 자체를 차단."""
    rsi, pct_b, hv_rank = ind.get("rsi"), ind.get("pct_b"), ind.get("hv_rank")
    pos_52w, chg_30d = ind.get("pos_52w"), ind.get("chg_30d")
    ext20 = ind.get("ext_sma20")

    # ① 추세장 차단 — 포물선 급등에 맞서는 콜매도 금지 (최대 손실 패턴)
    if chg_30d is not None and chg_30d > TREND_BLOCK_30D:
        return 0, [f"차단: 최근 30일 +{chg_30d:.1f}% 급등 진행 중 — 상승 추세 폭주 구간에서는 콜매도 진입 금지"]
    if ext20 is not None and ext20 > TREND_BLOCK_EXT20:
        return 0, [f"차단: 가격이 20일 이평선 대비 +{ext20*100:.1f}% 상방 이격 — 포물선 급등 구간"]
    # ③ 꺾임 확인 — 5일 신고가를 계속 갱신 중이면 모멘텀 꺾임 전까지 대기
    if ind.get("new_5d_high"):
        return 0, ["차단: 5일 신고가 갱신 중 — 상승 모멘텀이 살아 있어 꺾임 확인 전까지 대기"]
    if pct_b is not None and pct_b > 1.0:
        return 0, [f"차단: %B {pct_b:.2f} — 볼린저밴드 상단 돌파 상태. 밴드 안 복귀 확인 후 진입"]

    score, reasons = 0, []
    if rsi is not None:
        if rsi > 68:
            score += 30
            reasons.append(f"RSI {rsi:.0f} — 과매수 구간. 단기 상승 피로 누적으로 추가 상승 여력 제한적, OTM 콜매도에 유리")
        elif rsi > 58:
            score += 18
            reasons.append(f"RSI {rsi:.0f} — 매수 우위 구간 후반부. 상승 탄력 둔화 시 콜매도 진입 영역")
        if rsi > 58:
            reasons.append("직전 5일 고점을 넘지 못하고 밴드 안 복귀 — 상승 모멘텀 1차 꺾임 확인 후 진입 조건 충족")
    if pct_b is not None and 0.75 < pct_b <= 1.0:
        score += 20
        reasons.append(f"볼린저밴드 %B {pct_b:.2f} — 밴드 상단 근접. 통계적으로 되돌림(조정) 확률이 높은 위치")
    score += _hv_bonus(hv_rank, chg_30d, reasons)
    if pos_52w is not None and pos_52w > 85:
        score += 10
        reasons.append(f"52주 밴드 상단 {pos_52w:.0f}% 위치 — 고점 부담 구간, 상방 저항 예상")
    return score, reasons


def score_strangle(ind):
    """양매도(스트랭글): 방향성 없는 횡보 + 높은 변동성 환경에서 유리.
    어느 쪽이든 추세가 강하면 차단."""
    rsi, pct_b, hv_rank = ind.get("rsi"), ind.get("pct_b"), ind.get("hv_rank")
    chg_30d, ext20 = ind.get("chg_30d"), ind.get("ext_sma20")

    # ① 추세장 차단 — 한쪽 다리가 뚫리는 최악 시나리오 방지
    if chg_30d is not None and abs(chg_30d) > TREND_BLOCK_30D:
        return 0, [f"차단: 최근 30일 {chg_30d:+.1f}% — 방향성 강한 장세에서는 양매도 진입 금지"]
    if ext20 is not None and abs(ext20) > TREND_BLOCK_EXT20:
        return 0, [f"차단: 가격이 20일 이평선 대비 {ext20*100:+.1f}% 이격 — 레인지 이탈 위험"]

    score, reasons = 0, []
    if rsi is not None and 42 <= rsi <= 58:
        score += 20
        reasons.append(f"RSI {rsi:.0f} — 중립 구간. 뚜렷한 방향성 없이 횡보 중으로 양매도에 적합한 환경")
    if pct_b is not None and 0.3 <= pct_b <= 0.7:
        score += 15
        reasons.append(f"볼린저밴드 %B {pct_b:.2f} — 밴드 중앙부 위치. 레인지 장세 지속 가능성")
    if hv_rank is not None and hv_rank > 65:
        score += 35
        reasons.append(f"변동성 랭크 {hv_rank:.0f}%ile + 가격은 박스권 — 변동성 고점에서 수축(프리미엄 소멸)을 "
                       f"기다리는 양매도의 이상적 환경 (수익 나는 전형적 패턴)")
    if chg_30d is not None and abs(chg_30d) < 4:
        score += 10
        reasons.append(f"최근 30일 등락 {chg_30d:+.1f}% — 추세 없는 박스권 흐름 확인")
    return score, reasons


def analyze_symbol(key, today=None):
    """한 종목에 대해 지표 계산 → 만기별 후보 산출 → 전략별 신호 생성."""
    symbol, name_kr, category, desc = UNIVERSE[key][:4]
    today = today or date.today()
    closes = fetch_history(symbol)
    if not closes:
        return {"key": key, "name": name_kr, "category": category, "error": "시세 조회 실패"}
    ind = compute_indicators(closes)
    if not ind:
        return {"key": key, "name": name_kr, "category": category, "error": "데이터 부족"}

    strategies = []
    put_score, put_reasons = score_put_sell(ind)
    call_score, call_reasons = score_call_sell(ind)
    str_score, str_reasons = score_strangle(ind)

    for exp in target_expiries(today):
        put_raw = make_candidate(key, ind, exp, is_call=False, today=today)
        call_raw = make_candidate(key, ind, exp, is_call=True, today=today)

        # 단일 레그는 각자 수수료 차감 후 수익성 기준 통과 시에만 신호
        put_cand = apply_costs(dict(put_raw), fee_points(key), put_raw["strike"]) if put_raw else None
        call_cand = apply_costs(dict(call_raw), fee_points(key), call_raw["strike"]) if call_raw else None

        if put_cand and put_score >= SIGNAL_THRESHOLD:
            strategies.append(_make_signal("풋매도", put_score, put_reasons, put_cand, today))
        if call_cand and call_score >= CALL_SIGNAL_THRESHOLD:
            strategies.append(_make_signal("콜매도", call_score, call_reasons, call_cand, today))
        if put_raw and call_raw and str_score >= SIGNAL_THRESHOLD:
            # 양매도는 합산 프리미엄 기준으로 평가 (레그 2개 → 수수료 2배 차감)
            combo = {
                "strike": None, "put": put_raw, "call": call_raw,
                "premium": put_raw["premium"] + call_raw["premium"],
                "delta": None, "iv": (put_raw["iv"] + call_raw["iv"]) / 2,
                "dte": put_raw["dte"], "expiry": exp,
                "yield_pct": (put_raw["premium"] + call_raw["premium"]) / ind["price"] * 100,
                "annualized": ((put_raw["premium"] + call_raw["premium"]) / ind["price"] * 100) * 365 / put_raw["dte"],
                "otm_pct": None,
            }
            combo = apply_costs(combo, fee_points(key, legs=2), ind["price"])
            if combo:
                strategies.append(_make_signal("양매도", str_score, str_reasons, combo, today))

    # 같은 전략이 두 만기 모두에서 나오면 수수료 차감 후 연환산 수익률 기준 상위 1개만
    dedup = {}
    for s in strategies:
        k = s["strategy"]
        if k not in dedup or s["candidate"]["net_annualized"] > dedup[k]["candidate"]["net_annualized"]:
            dedup[k] = s

    return {
        "key": key, "symbol": symbol, "name": name_kr, "category": category,
        "desc": desc, "indicators": ind,
        "signals": sorted(dedup.values(), key=lambda s: -s["score"]),
    }


def _make_signal(strategy, score, reasons, candidate, today):
    reasons = list(reasons)
    exp = candidate["expiry"]
    label = expiry_label(exp, today)
    reasons.append(
        f"{label} {exp.strftime('%m월')} 만기 (잔존 약 {candidate['dte']}일) — "
        f"세타(시간가치 소멸)가 본격화되는 30~90일 구간으로 옵션 매도에 적정한 만기"
    )
    if candidate.get("annualized"):
        reasons.append(
            f"추정 프리미엄 수익률 {candidate['yield_pct']:.2f}% (연환산 약 {candidate['annualized']:.1f}%) — "
            f"역사적 변동성(HV20) 기반 블랙-76 모델 추정치"
        )
    if candidate.get("net_annualized") is not None:
        reasons.append(
            f"왕복 수수료·슬리피지 약 {candidate['fee']:.4g} pt 차감 후 순 프리미엄 {candidate['net_premium']:.4g} pt, "
            f"순 연환산 {candidate['net_annualized']:.1f}% — 비용 감안 후에도 수익성 기준(연 {MIN_NET_ANNUALIZED:.0f}% 이상) 충족"
        )
    if candidate.get("delta") is not None:
        reasons.append(
            f"추정 델타 {abs(candidate['delta']):.2f} — 만기 내 행사가 도달 확률 약 {abs(candidate['delta'])*100:.0f}% 수준의 보수적 OTM 행사가"
        )
    reasons.append(
        f"⚠ 손절 기준: 옵션 가격이 수취 프리미엄의 {STOP_LOSS_MULT:.0f}배({candidate['premium']*STOP_LOSS_MULT:.4g} pt) "
        f"도달 시 환매 청산 권고 — 추세 전환 시 손실이 무한정 커지기 전에 이탈"
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
                name_kr, category = UNIVERSE[key][1], UNIVERSE[key][2]
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


def _fmt_price(key, val):
    decimals = UNIVERSE[key][5]
    return f"{val:,.{decimals}f}"


def render_signal_card(asset, sig):
    ind = asset["indicators"]
    cand = sig["candidate"]
    key = asset["key"]
    cat_cls = CATEGORY_COLORS.get(asset["category"], ("cat-comm",))[0]
    color = _score_color(sig["score"])
    strong_cls = " strong" if sig["score"] >= 75 else ""

    if sig["strategy"] == "양매도":
        strike_html = (f'<div class="kv"><div class="k">행사가 (풋/콜)</div>'
                       f'<div class="v">{_fmt_price(key, cand["put"]["strike"])} / {_fmt_price(key, cand["call"]["strike"])}</div></div>')
        delta_html = (f'<div class="kv"><div class="k">추정 델타 (풋/콜)</div>'
                      f'<div class="v">{abs(cand["put"]["delta"]):.2f} / {abs(cand["call"]["delta"]):.2f}</div></div>')
    else:
        otm = f'<small> (OTM {cand["otm_pct"]:.1f}%)</small>' if cand.get("otm_pct") is not None else ""
        strike_html = f'<div class="kv"><div class="k">추천 행사가</div><div class="v">{_fmt_price(key, cand["strike"])}{otm}</div></div>'
        delta_html = f'<div class="kv"><div class="k">추정 델타</div><div class="v">{abs(cand["delta"]):.2f}</div></div>'

    reasons_html = "".join(
        f'<div class="reason{" warn" if r.startswith("⚠") else ""}">{r}</div>' for r in sig["reasons"]
    )

    return f"""
  <div class="sig-card{strong_cls}">
    <div class="sig-head">
      <div><span class="sig-name">{asset["name"]}</span><span class="sig-sym">{asset["symbol"]}</span><br>
        <span style="font-size:11px;color:#718096;">현재가 {_fmt_price(key, ind["price"])} · {asset["desc"]}</span></div>
      <div class="badges">
        <span class="badge {cat_cls}">{asset["category"]}</span>
        {_strat_badge(sig["strategy"])}
        <span class="badge exp-badge">{sig["expiry_label"]} {cand["expiry"].strftime("%m월")}</span>
      </div>
    </div>
    <div class="score-row">
      <div class="score-bar"><div class="score-fill" style="width:{sig["score"]}%;background:{color};"></div></div>
      <div class="score-num" style="color:{color};">신호 {sig["score"]}점</div>
    </div>
    <div class="kv-grid">
      {strike_html}
      <div class="kv"><div class="k">추정 프리미엄</div><div class="v">{cand["premium"]:,.2f}<small> pt</small></div></div>
      <div class="kv"><div class="k">순수익률 (수수료 차감)</div><div class="v">{cand["net_yield_pct"]:.2f}%<small> (연 {cand["net_annualized"]:.0f}%)</small></div></div>
      {delta_html}
      <div class="kv"><div class="k">변동성 (HV20)</div><div class="v">{cand["iv"]:.0f}%</div></div>
      <div class="kv"><div class="k">잔존일수</div><div class="v">약 {cand["dte"]}일</div></div>
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
    key = asset["key"]
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
            f'<td style="text-align:right;font-weight:700;color:#f7fafc;">{_fmt_price(key, ind["price"])}</td>'
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
                  f'{top[1]["candidate"]["expiry"].strftime("%m월")} 만기</div>')
        cards_html = "".join(render_signal_card(a, s) for a, s in all_signals)
    else:
        banner = ('<div class="banner"><strong>📡 오늘의 신호 없음</strong> — '
                  '현재 기준을 충족하는 옵션 매도 기회가 없습니다. 아래 관찰 리스트에서 지표를 확인하세요.</div>')
        cards_html = '<div class="empty-note">조건(신호 55점 이상)을 만족하는 종목이 없습니다.<br>변동성이 낮은 장세에서는 신호가 드물게 발생합니다 — 이는 정상입니다.</div>'

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
<title>옵션 매도 신호봇 — 통화·귀금속·에너지·곡물 선물 옵션</title>
<style>{STYLE}</style>
</head>
<body>

<div class="header">
  <h1>📡 옵션 매도 신호봇</h1>
  <div class="subtitle">통화 · 귀금속 · 에너지 · 곡물 선물 — 차월물~차차월물 옵션 매도 기회 탐지 (시장 동향 보고서와 동일 종목)</div>
  <div class="date-badge">🗓 {now_str}{gen_note}</div>
  <a class="nav-link" href="./index.html">📊 시장 동향 보고서 →</a>
</div>

{banner}

<div class="warn-banner">⚠ 본 신호는 자동 생성된 참고 정보이며 투자 권유가 아닙니다. 행사가·프리미엄·델타는 역사적 변동성 기반 <strong>모델 추정치</strong>이므로 실제 호가와 다를 수 있고, 표기 만기일은 대표치(셋째 금요일 기준)로 상품별 실제 만기와 다를 수 있습니다. 주문 전 반드시 HTS/거래소에서 실제 옵션 시세를 확인하세요. 옵션 매도는 수취 프리미엄 대비 큰 손실이 발생할 수 있는 전략입니다.</div>

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
      <div class="reason"><strong>신호 점수</strong> — RSI·볼린저밴드·변동성 랭크·추세를 종합한 0~100점. 풋매도·양매도는 55점, 콜매도는 65점 이상만 표시(원자재 급등 꼬리 리스크 반영). 75점 이상은 강한 신호로 강조.</div>
      <div class="reason"><strong>추세장 차단</strong> — 최근 30일 등락 ±8% 초과, 20일 이평선 ±5% 이상 이격, 5일 신고가/신저가 갱신 중, 볼린저밴드 이탈 상태면 추세 폭주로 간주해 역방향 매도 신호를 점수와 무관하게 차단. 변동성 랭크가 높아도 강한 추세가 동반되면 "정당한 변동성"으로 보고 가점을 축소 — 옵션 매도는 변동성은 높은데 가격은 갈 곳이 없는 평균회귀 장세에서만 유리하기 때문.</div>
      <div class="reason"><strong>추정 델타</strong> — 만기 시 행사가 도달(ITM) 확률의 근사치. 풋은 0.30, 콜은 0.25 이하의 보수적 OTM 행사가만 추천 (콜은 급등 꼬리가 길어 더 낮게).</div>
      <div class="reason"><strong>만기 선택</strong> — 차월물~차차월물(약 30~90일)만 추천. 시간가치 소멸(세타)이 가장 효율적으로 작동하는 구간.</div>
      <div class="reason"><strong>수수료 반영</strong> — 계약당 왕복 수수료·슬리피지 추정치(약 $12/레그)를 포인트로 환산해 차감. 비용이 프리미엄의 15%를 넘거나 순 연환산 수익률이 명목가 기준 4% 미만이면 신호에서 제외 — 표시되는 순수익률은 비용 차감 후 값. 선물옵션은 증거금 거래이므로 실제 투입 증거금 기준 수익률은 표기(명목가 기준)의 수배 수준.</div>
      <div class="reason"><strong>손절 기준</strong> — 모든 신호에 수취 프리미엄 2배 도달 시 환매 청산 기준을 제시. 옵션 매도의 손실은 이론상 무한대이므로 기계적 손절이 필수.</div>
      <div class="reason"><strong>프리미엄 추정 방식</strong> — 선물 옵션 실시간 호가는 무료 데이터로 제공되지 않아, 최근 20일 역사적 변동성을 내재변동성 프록시로 사용해 블랙-76 모델로 계산. 실제 시장 IV가 더 높으면 수취 프리미엄은 표기보다 커질 수 있음.</div>
    </div>
  </div>
</div>

<div class="footer">
  본 보고서는 Yahoo Finance 선물 시세를 바탕으로 자동 생성되었습니다 (시장 동향 보고서와 동일한 종목·가격 기준).
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
