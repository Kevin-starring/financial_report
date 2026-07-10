"""
급락 풋매도 백테스트 — "패닉에 팔고 반등에 회수" 전략의 청산 규칙 검증
─────────────────────────────────────────────────────────────────
의도한 전략:
  선물 가격이 급락(패닉)했을 때 OTM 풋을 매도하고, 가격이 반등해
  옵션 프리미엄이 축소되면 환매(매수 청산)해서 이익을 확정한다.

구현한 권장 수정 4종 (외부 백테스트 도구의 실패 원인 보완):
  ① 익절  — 옵션 가치가 수취 프리미엄의 TAKE_PROFIT_RATIO(50%) 이하로
            축소되면 환매. 반등 구간을 그냥 지나치지 않는다
  ② 패닉 해소 청산 — 가격 z-점수가 RESOLVE_Z(-0.5) 위로 정상화되면 환매.
            HV 마킹 왜곡과 무관하게 작동하는 가격 기반 보조 청산
  ③ 손절  — 옵션 가치가 수취 프리미엄의 STOP_LOSS_MULT(2배) 이상이면 환매
  ④ IV 마킹 개선 — 보유일수에 따라 마킹 IV를 1년 중앙값으로 회귀시켜
            실제 시장의 IV crush(공포 해소 시 IV 급락)를 근사

동일 진입에서 "만기 보유(청산 규칙 없음)" 성과를 함께 출력해
청산 규칙이 성과를 얼마나 바꾸는지 비교한다.

실행: python backtest_sellput.py            # 전 종목 (Yahoo 시세 필요)
      python backtest_sellput.py NG=F CL=F  # 특정 심볼만
샌드박스에서는 Yahoo가 차단되므로 scratchpad/test_backtest.py로 검증.
"""

import math
import sys
from datetime import date, timedelta

from options_signal_bot import (
    UNIVERSE, RISK_FREE_RATE, ROUND_TRIP_COST_USD,
    STOP_LOSS_MULT, TAKE_PROFIT_RATIO,
    TARGET_DELTA_PUT, MAX_DELTA_PUT,
    b76_price, b76_delta, third_friday, _hist_vol, _sma,
)

try:
    import yfinance as yf
    yf.set_tz_cache_location("/tmp")
except Exception:
    yf = None

# ── 진입/청산 파라미터 ─────────────────────────────────────────
PANIC_Z = -2.0          # 진입: 종가가 20일 평균 대비 -2.0σ 이하로 급락
PANIC_HV_RANK = 70.0    # 진입: HV 랭크(1년 백분위)가 70 이상
RESOLVE_Z = -0.5        # 청산②: z-점수가 이 값 위로 회복하면 패닉 해소로 간주
IV_REVERT_DAYS = 10.0   # 청산④: 마킹 IV가 1년 중앙값으로 회귀하는 반감기(일)
MIN_DTE, MAX_DTE = 30, 75   # 진입 시 선택할 만기 잔존일수 범위 (차월~차차월권)
EXIT_DTE = 7            # 만기 임박 시 정리 (감마 리스크 회피)
WARMUP = 252            # 지표 계산에 필요한 최소 이력


def fetch_history_dated(symbol, period="2y"):
    """(날짜 리스트, 종가 리스트) 반환."""
    hist = yf.Ticker(symbol).history(period=period)
    if hist.empty:
        return None, None
    dates = [d.date() for d in hist.index.to_pydatetime()]
    closes = [float(c) for c in hist["Close"].tolist()]
    return dates, closes


def _zscore(closes, i, period=20):
    window = closes[i - period + 1: i + 1]
    mean = sum(window) / period
    var = sum((c - mean) ** 2 for c in window) / period
    std = math.sqrt(var)
    return (closes[i] - mean) / std if std > 0 else 0.0


def _hv_series(closes, period=20):
    """i번째 원소 = closes[:i+1] 기준 HV20 (앞부분은 None)."""
    out = [None] * len(closes)
    for i in range(period, len(closes)):
        out[i] = _hist_vol(closes[: i + 1], period)
    return out


def _hv_rank_at(hvs, i, lookback=252):
    window = [h for h in hvs[max(0, i - lookback): i + 1] if h is not None]
    if len(window) < 30:
        return None
    current = window[-1]
    return sum(1 for h in window if h < current) / len(window) * 100


def _hv_median_at(hvs, i, lookback=252):
    window = sorted(h for h in hvs[max(0, i - lookback): i + 1] if h is not None)
    return window[len(window) // 2] if window else None


def pick_expiry(today):
    """잔존 MIN_DTE~MAX_DTE 범위의 셋째 금요일 만기 선택."""
    y, m = today.year, today.month
    for _ in range(4):
        exp = third_friday(y, m)
        if MIN_DTE <= (exp - today).days <= MAX_DTE:
            return exp
        m, y = (m + 1, y) if m < 12 else (1, y + 1)
    return None


def pick_put_strike(key, spot, t_years, iv):
    """델타 상한 내에서 목표 델타에 가장 가까운 풋 행사가 (신호봇과 동일 규칙)."""
    step = UNIVERSE[key][4]
    base = round(spot / step) * step
    best, best_diff = None, None
    for i in range(1, 200):
        strike = base - i * step
        if strike <= 0:
            break
        delta = b76_delta(spot, strike, t_years, iv, is_call=False)
        if delta is None:
            break
        if abs(delta) > MAX_DELTA_PUT:
            continue
        diff = abs(abs(delta) - TARGET_DELTA_PUT)
        if best is None or diff < best_diff:
            best, best_diff = strike, diff
        if abs(delta) <= TARGET_DELTA_PUT:
            break
    return best


def mark_iv(hv_now, hv_median, days_held):
    """④ 보유일수에 따라 마킹 IV를 1년 중앙값으로 지수 회귀 (IV crush 근사).
    실현변동성은 급반등에서도 높게 유지되는 왜곡이 있어, 시장 IV처럼
    공포 해소와 함께 가라앉는 효과를 중앙값 회귀로 근사한다."""
    if hv_median is None:
        return hv_now
    w = math.exp(-days_held / IV_REVERT_DAYS)
    return hv_now * w + hv_median * (1 - w)


def simulate_symbol(key, dates, closes):
    """한 종목 백테스트. 각 트레이드에 청산규칙 적용 결과와
    만기 보유(비교용) 결과를 함께 기록한다."""
    hvs = _hv_series(closes)
    usd_per_pt = UNIVERSE[key][6]
    trades = []
    pos = None  # 진행 중 포지션 (만기까지 슬롯 점유 — 두 변형의 진입을 동일하게 유지)

    for i in range(WARMUP, len(closes)):
        today, spot = dates[i], closes[i]

        if pos:
            dte = (pos["expiry"] - today).days
            days_held = (today - pos["entry_date"]).days
            hv_med = _hv_median_at(hvs, i)
            iv_m = mark_iv(hvs[i], hv_med, days_held) / 100.0
            if dte <= 0:
                val = max(pos["strike"] - spot, 0.0)  # 만기 내재가치 정산
            else:
                val = b76_price(spot, pos["strike"], dte / 365.0, iv_m, is_call=False) or 0.0

            # 청산 규칙 (우선순위: 손절 → 익절 → 패닉해소 → 만기임박)
            if not pos["rule_exited"]:
                reason = None
                if dte <= 0:
                    reason = "만기 정산"
                elif val >= pos["premium"] * STOP_LOSS_MULT:
                    reason = "손절 (프리미엄 2배)"
                elif val <= pos["premium"] * TAKE_PROFIT_RATIO:
                    reason = "익절 (프리미엄 50% 회수)"
                elif _zscore(closes, i) >= RESOLVE_Z:
                    reason = "패닉 해소 (z 정상화)"
                elif dte <= EXIT_DTE:
                    reason = "만기 임박 정리"
                if reason:
                    pos["rule_exited"] = True
                    pos["rule_exit"] = {"date": today, "value": val, "reason": reason,
                                        "spot": spot, "days": days_held}

            if dte <= 0:  # 비교용 만기 보유 결과 확정 + 슬롯 해제
                pos["hold_exit"] = {"date": today, "value": val, "spot": spot,
                                    "days": days_held}
                trades.append(pos)
                pos = None
            continue

        # 진입 판정 (포지션 없음)
        hv_rank = _hv_rank_at(hvs, i)
        z = _zscore(closes, i)
        if hv_rank is None or hvs[i] is None:
            continue
        if z <= PANIC_Z and hv_rank >= PANIC_HV_RANK:
            expiry = pick_expiry(today)
            if not expiry:
                continue
            t_years = (expiry - today).days / 365.0
            iv = hvs[i] / 100.0
            strike = pick_put_strike(key, spot, t_years, iv)
            if not strike:
                continue
            premium = b76_price(spot, strike, t_years, iv, is_call=False)
            if not premium or premium <= 0:
                continue
            pos = {"entry_date": today, "spot": spot, "strike": strike,
                   "premium": premium, "expiry": expiry, "iv": hvs[i],
                   "z": z, "hv_rank": hv_rank,
                   "rule_exited": False, "rule_exit": None, "hold_exit": None}

    if pos:  # 데이터 끝까지 미결제 — 마지막 마킹가로 정리
        i = len(closes) - 1
        days_held = (dates[i] - pos["entry_date"]).days
        dte = max((pos["expiry"] - dates[i]).days, 0)
        iv_m = mark_iv(hvs[i], _hv_median_at(hvs, i), days_held) / 100.0
        val = (b76_price(closes[i], pos["strike"], max(dte, 1) / 365.0, iv_m, False) or 0.0)
        last = {"date": dates[i], "value": val, "spot": closes[i], "days": days_held}
        if not pos["rule_exited"]:
            pos["rule_exit"] = dict(last, reason="데이터 종료")
        pos["hold_exit"] = last
        trades.append(pos)

    fee_pts = ROUND_TRIP_COST_USD / usd_per_pt
    for t in trades:
        t["rule_pnl_usd"] = (t["premium"] - t["rule_exit"]["value"] - fee_pts) * usd_per_pt
        t["hold_pnl_usd"] = (t["premium"] - t["hold_exit"]["value"] - fee_pts) * usd_per_pt
    return trades


def run_backtest(symbols=None, fetch=fetch_history_dated):
    keys = [k for k, v in UNIVERSE.items() if not symbols or v[0] in symbols]
    all_trades = {}
    for key in keys:
        symbol = UNIVERSE[key][0]
        dates, closes = fetch(symbol)
        if not closes or len(closes) <= WARMUP:
            print(f"  {UNIVERSE[key][1]}: 데이터 부족 — 건너뜀")
            continue
        all_trades[key] = simulate_symbol(key, dates, closes)
    return all_trades


def print_report(all_trades):
    dec = {k: UNIVERSE[k][5] for k in UNIVERSE}
    tot_rule, tot_hold, n_trades, n_win = 0.0, 0.0, 0, 0
    print("\n" + "=" * 100)
    print(f"{'급락 풋매도 백테스트':^96}")
    print(f"{'진입: z≤' + str(PANIC_Z) + ' & HV랭크≥' + str(int(PANIC_HV_RANK)) + '  |  청산규칙: 익절 50% · 패닉해소 · 손절 2배 · 만기임박':^92}")
    print("=" * 100)
    for key, trades in all_trades.items():
        name = UNIVERSE[key][1]
        if not trades:
            print(f"\n▷ {name}: 진입 조건 충족 없음")
            continue
        print(f"\n▷ {name} — {len(trades)}건")
        for t in trades:
            d = dec[key]
            re_, he = t["rule_exit"], t["hold_exit"]
            n_trades += 1
            n_win += t["rule_pnl_usd"] > 0
            tot_rule += t["rule_pnl_usd"]
            tot_hold += t["hold_pnl_usd"]
            print(f"   {t['entry_date']} 진입 {t['spot']:,.{d}f} → 풋 K={t['strike']:,.{d}f} "
                  f"만기 {t['expiry']} 프리미엄 {t['premium']:.4g}pt (HV {t['iv']:.0f}%, z {t['z']:.1f})")
            print(f"     [청산규칙] {re_['date']} {re_['reason']} — 가치 {re_['value']:.4g}pt, "
                  f"{re_['days']}일 보유, 손익 ${t['rule_pnl_usd']:+,.0f}")
            print(f"     [만기보유] {he['date']} 가치 {he['value']:.4g}pt → 손익 ${t['hold_pnl_usd']:+,.0f}"
                  f"   (규칙 효과 ${t['rule_pnl_usd'] - t['hold_pnl_usd']:+,.0f})")
    print("\n" + "-" * 100)
    if n_trades:
        print(f"합계 {n_trades}건 — 청산규칙 적용: ${tot_rule:+,.0f} (승률 {n_win/n_trades*100:.0f}%)  "
              f"vs  만기 보유: ${tot_hold:+,.0f}  →  규칙 효과 ${tot_rule - tot_hold:+,.0f}")
    else:
        print("진입 조건을 충족한 트레이드가 없습니다.")
    print("※ 1계약 기준, 왕복 수수료·슬리피지 $12 반영. 프리미엄·평가가는 블랙-76/HV 모델 추정치.")
    print("=" * 100)


if __name__ == "__main__":
    symbols = [a for a in sys.argv[1:] if not a.startswith("-")] or None
    print("데이터 수집 중 (Yahoo Finance)...")
    print_report(run_backtest(symbols))
