"""
글로벌 선물·옵션 시장 동향 보고서 — 웹 앱 (Vercel 배포용)
로컬 실행: python futures_report_web.py  →  http://localhost:5500
배포: vercel --prod
"""

from flask import Flask, Response
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import pytz

try:
    yf.set_tz_cache_location("/tmp")
except Exception:
    pass

TICKERS = {
    "AUD_USD": "AUDUSD=X",
    "JPY_USD": "JPYUSD=X",
    "EUR_USD": "EURUSD=X",
    "GOLD":    "GC=F",
    "SILVER":  "SI=F",
    "CRUDE":   "CL=F",
    "NATGAS":  "NG=F",
    "CORN":    "ZC=F",
    "SOY":     "ZS=F",
    "WHEAT":   "ZW=F",
}

# ── 병렬 시세 조회 ─────────────────────────────────────────
def _fetch_price_one(args):
    name, symbol = args
    try:
        hist = yf.Ticker(symbol).history(period="30d")
        if hist.empty:
            raise ValueError("empty")
        latest    = float(hist["Close"].iloc[-1])
        prev      = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else latest
        price_5d  = float(hist["Close"].iloc[-6]) if len(hist) >= 6 else float(hist["Close"].iloc[0])
        price_30d = float(hist["Close"].iloc[0])
        chg       = (latest - prev)      / prev      * 100 if prev      else 0
        chg_5d    = (latest - price_5d)  / price_5d  * 100 if price_5d  else 0
        chg_30d   = (latest - price_30d) / price_30d * 100 if price_30d else 0
        closes = [float(c) for c in hist["Close"].tolist()[-14:]]
        return name, {
            "price": round(latest, 4), "change_pct": round(chg, 2),
            "prev":  round(prev,   4), "chg_5d":     round(chg_5d, 2),
            "chg_30d": round(chg_30d, 2), "closes": closes, "symbol": symbol,
        }
    except Exception as e:
        return name, {
            "price": None, "change_pct": None, "prev": None,
            "chg_5d": None, "chg_30d": None, "closes": [], "symbol": symbol, "error": str(e),
        }

def fetch_prices():
    results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        fs = {ex.submit(_fetch_price_one, item): item[0] for item in TICKERS.items()}
        for f in as_completed(fs, timeout=25):
            try:
                name, data = f.result(timeout=8)
            except Exception:
                name = fs[f]
                data = {"price": None, "change_pct": None, "prev": None,
                        "chg_5d": None, "chg_30d": None, "closes": [], "symbol": TICKERS.get(name, "")}
            results[name] = data
    for name, symbol in TICKERS.items():
        results.setdefault(name, {"price": None, "change_pct": None, "prev": None,
                                   "chg_5d": None, "chg_30d": None, "closes": [], "symbol": symbol})
    return results


# ── 병렬 뉴스 조회 ──────────────────────────────────────────
def _fetch_news_one(args):
    name, symbol = args
    try:
        news_raw = yf.Ticker(symbol).news or []
        result = []
        for item in news_raw[:3]:
            content = item.get("content", {})
            if isinstance(content, dict) and content.get("title"):
                title     = content.get("title", "")
                provider  = content.get("provider", {})
                publisher = provider.get("displayName", "") if isinstance(provider, dict) else ""
                ct        = content.get("clickThroughUrl", {})
                link      = ct.get("url", "") if isinstance(ct, dict) else ""
                pub_str   = content.get("pubDate", "")
                pub_date  = pub_str[5:10] if len(pub_str) >= 10 else ""
            else:
                title     = item.get("title", "")
                publisher = item.get("publisher", "")
                link      = item.get("link", "")
                pub_ts    = item.get("providerPublishTime", 0)
                try:
                    pub_date = datetime.fromtimestamp(pub_ts).strftime("%m/%d") if pub_ts else ""
                except Exception:
                    pub_date = ""
            if title:
                result.append({"title": title, "publisher": publisher, "link": link, "date": pub_date})
        return name, result
    except Exception:
        return name, []

def fetch_all_news():
    results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        fs = {ex.submit(_fetch_news_one, item): item[0] for item in TICKERS.items()}
        for f in as_completed(fs, timeout=20):
            try:
                name, news_list = f.result(timeout=8)
            except Exception:
                name = fs[f]
                news_list = []
            results[name] = news_list
    for name in TICKERS:
        results.setdefault(name, [])
    return results


# ── 헬퍼 ──────────────────────────────────────────────────
def fmt(val, decimals=2, prefix=""):
    if val is None:
        return "N/A"
    return f"{prefix}{val:,.{decimals}f}"

def change_badge(chg):
    if chg is None:
        return '<span class="card-change neutral">— N/A</span>'
    if chg > 0:
        return f'<span class="card-change up">▲ +{chg:.2f}% (24h)</span>'
    elif chg < 0:
        return f'<span class="card-change down">▼ {chg:.2f}% (24h)</span>'
    return f'<span class="card-change neutral">↔ {chg:.2f}% (24h)</span>'

def trend_spans(c5, c30):
    def s(chg, label):
        if chg is None:
            return ""
        color = "#68d391" if chg > 0 else "#fc8181" if chg < 0 else "#a0aec0"
        sign = "+" if chg > 0 else ""
        return f'<span style="font-size:11px; color:{color}; margin-left:6px;">{sign}{chg:.1f}%({label})</span>'
    return s(c5, "5d") + s(c30, "30d")

def table_dir(chg):
    if chg is None:
        return '<td style="padding:10px 14px; color:#a0aec0;">— N/A</td>'
    if chg > 0:
        return f'<td style="padding:10px 14px; color:#68d391;">▲ +{chg:.2f}%</td>'
    elif chg < 0:
        return f'<td style="padding:10px 14px; color:#fc8181;">▼ {chg:.2f}%</td>'
    return f'<td style="padding:10px 14px; color:#a0aec0;">↔ {chg:.2f}%</td>'

def trend_cell(chg):
    if chg is None:
        return '<td style="padding:10px 14px; color:#a0aec0;">—</td>'
    if chg > 1:
        return f'<td style="padding:10px 14px; color:#68d391;">▲ 강세 (+{chg:.1f}%)</td>'
    elif chg < -1:
        return f'<td style="padding:10px 14px; color:#fc8181;">▼ 약세 ({chg:.1f}%)</td>'
    return f'<td style="padding:10px 14px; color:#a0aec0;">↔ 보합 ({chg:+.1f}%)</td>'

def sparkline_svg(closes, width=110, height=30):
    if not closes or len(closes) < 2:
        return ""
    mn, mx = min(closes), max(closes)
    rng = mx - mn if mx != mn else 1
    n = len(closes)
    color = "#68d391" if closes[-1] >= closes[0] else "#fc8181"
    pts = " ".join(
        f"{i/(n-1)*width:.1f},{height - ((c-mn)/rng*(height-4)) - 2:.1f}"
        for i, c in enumerate(closes)
    )
    return (f'<svg width="{width}" height="{height}" '
            f'style="vertical-align:middle; margin-left:8px; overflow:visible;">'
            f'<polyline points="{pts}" fill="none" stroke="{color}" '
            f'stroke-width="1.5" stroke-linejoin="round"/></svg>')

def tag_from_chg(chg):
    if chg is None or abs(chg) < 0.3:
        return "tag-neutral", "중립"
    return ("tag-bullish", "강세") if chg > 0 else ("tag-bearish", "약세")

def render_news_block(news_list, chg_24h):
    if not news_list:
        tag_cls, tag_lbl = tag_from_chg(chg_24h)
        note = f"전일 대비 {chg_24h:+.2f}%" if chg_24h is not None else "데이터 없음"
        return f'<div class="news-item"><span class="tag {tag_cls}">{tag_lbl}</span> {note}</div>'
    html = ""
    for i, n in enumerate(news_list):
        tag_cls, tag_lbl = tag_from_chg(chg_24h) if i == 0 else ("tag-neutral", "뉴스")
        title    = n["title"]
        link     = n["link"]
        headline = (f'<a href="{link}" style="color:#bee3f8; text-decoration:none;"><strong>{title}</strong></a>'
                    if link else f'<strong>{title}</strong>')
        parts = [p for p in [n["publisher"], n["date"]] if p]
        meta  = f' <span style="font-size:10px; color:#4a5568;">— {" · ".join(parts)}</span>' if parts else ""
        html += f'\n      <div class="news-item"><span class="tag {tag_cls}">{tag_lbl}</span> {headline}{meta}</div>'
    return html


# ── 거시 분석 ──────────────────────────────────────────────
def build_macro_section(p):
    def chip(label, cls=""):
        return f'<span class="factor-chip {cls}">{label}</span>'

    def chg_chip(data, label):
        chg = data.get("chg_5d")
        if chg is None:
            return chip(label)
        cls  = "green" if chg > 0.5 else "red" if chg < -0.5 else ""
        sign = "+" if chg > 0 else ""
        return chip(f"{label} {sign}{chg:.1f}%(5d)", cls)

    aud, jpy, eur    = p["AUD_USD"], p["JPY_USD"], p["EUR_USD"]
    crude, ng        = p["CRUDE"], p["NATGAS"]
    gold, silver     = p["GOLD"], p["SILVER"]
    corn, soy, wheat = p["CORN"], p["SOY"], p["WHEAT"]

    jpy_price = f"{1/jpy['price']:.2f}" if jpy.get("price") else "N/A"
    jpy_5d    = (-jpy["chg_5d"]  if jpy.get("chg_5d")  is not None else None)

    def jpy_chip():
        chg = jpy_5d
        if chg is None:
            return chip(f"USD/JPY {jpy_price}")
        cls  = "green" if chg > 0.5 else "red" if chg < -0.5 else ""
        sign = "+" if chg > 0 else ""
        return chip(f"USD/JPY {jpy_price} ({sign}{chg:.1f}% 5d)", cls)

    gold_5d    = gold.get("chg_5d") or 0
    gold_note  = ("안전자산 수요 강세" if gold_5d > 1
                  else "위험선호 개선 또는 달러 강세" if gold_5d < -1
                  else "방향성 탐색 중")
    crude_5d   = crude.get("chg_5d") or 0
    crude_note = ("에너지 강세 — 공급 우려 또는 수요 증가" if crude_5d > 2
                  else "에너지 약세 — 공급 증가 또는 수요 둔화" if crude_5d < -2
                  else "에너지 보합세 유지")

    return f"""  <div class="macro-card">
    <h3>① 통화 (FX) 시장 동향</h3>
    <p>AUD/USD <span class="key-point">{fmt(aud.get("price"), 4)}</span> (5d: {fmt(aud.get("chg_5d"), 1)}%) &nbsp;·&nbsp;
       EUR/USD <span class="key-point">{fmt(eur.get("price"), 4)}</span> (5d: {fmt(eur.get("chg_5d"), 1)}%) &nbsp;·&nbsp;
       USD/JPY <span class="key-point">{jpy_price}</span>.
       주요국 중앙은행 정책 기대 및 에너지 가격 동향이 환율 방향성을 결정.</p>
    <div class="factor-row">
      {chg_chip(aud, "AUD/USD")} {chg_chip(eur, "EUR/USD")} {jpy_chip()}
    </div>
  </div>
  <div class="macro-card">
    <h3>② 에너지 시장 동향</h3>
    <p>WTI 원유 <span class="key-point">${fmt(crude.get("price"), 2)}/배럴</span> (5d: {fmt(crude.get("chg_5d"), 1)}%) &nbsp;·&nbsp;
       천연가스 <span class="key-point">${fmt(ng.get("price"), 3)}/MMBtu</span> (5d: {fmt(ng.get("chg_5d"), 1)}%). {crude_note}.</p>
    <div class="factor-row">
      {chg_chip(crude, "WTI 원유")} {chg_chip(ng, "천연가스")}
    </div>
  </div>
  <div class="macro-card">
    <h3>③ 귀금속 시장 동향</h3>
    <p>금 <span class="key-point">${fmt(gold.get("price"), 2)}/oz</span> (5d: {fmt(gold.get("chg_5d"), 1)}%, 30d: {fmt(gold.get("chg_30d"), 1)}%) &nbsp;·&nbsp;
       은 <span class="key-point">${fmt(silver.get("price"), 2)}/oz</span> (5d: {fmt(silver.get("chg_5d"), 1)}%). {gold_note}.</p>
    <div class="factor-row">
      {chg_chip(gold, "금")} {chg_chip(silver, "은")}
    </div>
  </div>
  <div class="macro-card">
    <h3>④ 곡물 시장 동향</h3>
    <p>옥수수 <span class="key-point">${fmt(corn.get("price"), 2)}/bu</span> (5d: {fmt(corn.get("chg_5d"), 1)}%) &nbsp;·&nbsp;
       대두 <span class="key-point">${fmt(soy.get("price"), 2)}/bu</span> (5d: {fmt(soy.get("chg_5d"), 1)}%) &nbsp;·&nbsp;
       밀 <span class="key-point">${fmt(wheat.get("price"), 2)}/bu</span> (5d: {fmt(wheat.get("chg_5d"), 1)}%).
       USDA WASDE 수급 전망, 날씨·작황 변수 및 비료 가격이 방향성을 결정.</p>
    <div class="factor-row">
      {chg_chip(corn, "옥수수")} {chg_chip(soy, "대두")} {chg_chip(wheat, "밀")}
    </div>
  </div>
"""

def build_macro_banner(p):
    labels = {
        "AUD_USD": "AUD/USD", "JPY_USD": "USD/JPY", "EUR_USD": "EUR/USD",
        "GOLD": "금", "SILVER": "은", "CRUDE": "WTI 원유", "NATGAS": "천연가스",
        "CORN": "옥수수", "SOY": "대두", "WHEAT": "밀",
    }
    movers = [(name, d["chg_5d"]) for name, d in p.items()
              if d.get("chg_5d") is not None and d.get("price") is not None]
    if not movers:
        return '<div class="macro-banner"><strong>📡 자동 생성 보고서:</strong> Yahoo Finance 실시간 데이터 기반으로 자동 생성되었습니다.</div>'
    top = sorted(movers, key=lambda x: abs(x[1]), reverse=True)[:3]
    parts = []
    for name, chg in top:
        lbl   = labels.get(name, name)
        sign  = "+" if chg > 0 else ""
        arrow = "▲" if chg > 0 else "▼"
        parts.append(f"<strong>{lbl}</strong> {arrow}{sign}{chg:.1f}%(5d)")
    return f'<div class="macro-banner"><strong>📡 주요 동향 (5일):</strong> {" &nbsp;·&nbsp; ".join(parts)}. Yahoo Finance 실시간 데이터 기반 자동 생성.</div>'


# ── CSS ────────────────────────────────────────────────────
STYLE = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', 'Apple SD Gothic Neo', sans-serif; background: #0f1117; color: #e2e8f0; line-height: 1.7; }
.header { background: linear-gradient(135deg, #1a1f2e 0%, #0f1117 100%); border-bottom: 1px solid #2d3748; padding: 36px 48px 28px; }
.header-top { display: flex; justify-content: space-between; align-items: flex-start; }
.header h1 { font-size: 26px; font-weight: 700; color: #f7fafc; letter-spacing: -0.5px; }
.header .subtitle { margin-top: 6px; font-size: 13px; color: #718096; }
.header .date-badge { display: inline-block; margin-top: 12px; background: #2d3748; border: 1px solid #4a5568; border-radius: 20px; padding: 4px 14px; font-size: 12px; color: #a0aec0; }
.refresh-btn { background: linear-gradient(135deg, #2b6cb0, #1a365d); border: 1px solid #4a5568; color: #bee3f8; border-radius: 10px; padding: 12px 22px; font-size: 15px; cursor: pointer; display: flex; align-items: center; gap: 8px; transition: all 0.2s; white-space: nowrap; margin-top: 4px; }
.refresh-btn:hover { background: linear-gradient(135deg, #3182ce, #2b6cb0); border-color: #63b3ed; }
.refresh-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.gen-time { font-size: 11px; color: #4a5568; margin-top: 6px; text-align: right; }
#loading-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15,17,23,0.92); z-index: 9999; align-items: center; justify-content: center; flex-direction: column; gap: 16px; }
.loading-spinner { width: 48px; height: 48px; border: 4px solid #2d3748; border-top-color: #63b3ed; border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.loading-text { color: #e2e8f0; font-size: 18px; font-weight: 600; }
.loading-sub { color: #718096; font-size: 13px; }
.macro-banner { background: linear-gradient(90deg, #1a2744 0%, #1a1f2e 100%); border-left: 4px solid #63b3ed; margin: 24px 48px 0; border-radius: 8px; padding: 14px 20px; font-size: 13px; color: #bee3f8; }
.macro-banner strong { color: #63b3ed; }
.section-title { font-size: 11px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: #4a5568; padding: 32px 48px 12px; border-bottom: 1px solid #1a202c; }
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; padding: 20px 48px; }
.grid-2col { grid-template-columns: repeat(2, 1fr); }
.card { background: #1a1f2e; border: 1px solid #2d3748; border-radius: 12px; padding: 20px; transition: border-color 0.2s; }
.card:hover { border-color: #4a5568; }
.card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px; }
.card-name { font-size: 11px; font-weight: 600; color: #718096; text-transform: uppercase; letter-spacing: 1px; }
.card-category { font-size: 10px; padding: 2px 8px; border-radius: 10px; font-weight: 600; }
.cat-fx { background: #1a365d; color: #63b3ed; }
.cat-metal { background: #322659; color: #b794f4; }
.cat-energy { background: #2d3748; color: #68d391; }
.cat-grain { background: #1c4532; color: #9ae6b4; }
.card-price { font-size: 28px; font-weight: 700; color: #f7fafc; margin: 4px 0; }
.card-unit { font-size: 12px; color: #718096; margin-bottom: 8px; }
.card-change { display: inline-flex; align-items: center; gap: 4px; font-size: 13px; font-weight: 600; padding: 3px 10px; border-radius: 6px; }
.up { background: #1c4532; color: #68d391; }
.down { background: #742a2a; color: #fc8181; }
.neutral { background: #2d3748; color: #a0aec0; }
.sparkline-row { margin: 8px 0 4px; }
.divider { border: none; border-top: 1px solid #2d3748; margin: 14px 0; }
.news-block { margin-top: 10px; }
.news-item { font-size: 12.5px; color: #a0aec0; margin-bottom: 8px; padding-left: 12px; border-left: 2px solid #2d3748; }
.news-item strong { color: #e2e8f0; }
.news-item .tag { display: inline-block; font-size: 10px; font-weight: 600; padding: 1px 6px; border-radius: 4px; margin-right: 4px; background: #2d3748; color: #718096; }
.tag-bullish { background: #1c4532; color: #68d391; }
.tag-bearish { background: #742a2a; color: #fc8181; }
.tag-neutral { background: #2d3748; color: #a0aec0; }
.macro-section { padding: 0 48px 24px; }
.macro-card { background: #1a1f2e; border: 1px solid #2d3748; border-radius: 12px; padding: 20px 24px; margin-bottom: 16px; }
.macro-card h3 { font-size: 14px; font-weight: 700; color: #e2e8f0; margin-bottom: 12px; }
.macro-card p { font-size: 13px; color: #a0aec0; margin-bottom: 8px; }
.macro-card .key-point { color: #e2e8f0; }
.factor-row { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 10px; }
.factor-chip { font-size: 12px; padding: 4px 12px; border-radius: 20px; border: 1px solid #2d3748; color: #a0aec0; background: #0f1117; }
.factor-chip.red { border-color: #742a2a; color: #fc8181; background: #1a0a0a; }
.factor-chip.green { border-color: #1c4532; color: #68d391; background: #0a1a0e; }
.factor-chip.blue { border-color: #1a365d; color: #63b3ed; background: #0a0f1a; }
.factor-chip.purple { border-color: #322659; color: #b794f4; background: #0f0a1a; }
.sources { padding: 24px 48px; border-top: 1px solid #1a202c; }
.sources h4 { font-size: 11px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; color: #4a5568; margin-bottom: 12px; }
.sources a { color: #4a5568; font-size: 11px; text-decoration: none; margin-right: 16px; }
.sources a:hover { color: #718096; text-decoration: underline; }
.footer { padding: 16px 48px; font-size: 11px; color: #4a5568; border-top: 1px solid #1a202c; }
@media (max-width: 900px) { .grid { grid-template-columns: 1fr 1fr; padding: 16px 20px; } .grid-2col { grid-template-columns: 1fr; } .section-title, .header, .macro-section, .sources, .footer { padding-left: 20px; padding-right: 20px; } .macro-banner { margin-left: 20px; margin-right: 20px; } .header-top { flex-direction: column; gap: 16px; } }
@media (max-width: 600px) { .grid { grid-template-columns: 1fr; } }
"""


# ── HTML 생성 ──────────────────────────────────────────────
def build_html(p, news, today_str, gen_time):
    aud  = p["AUD_USD"]
    jpy  = p["JPY_USD"]
    eur  = p["EUR_USD"]
    gold = p["GOLD"]
    silv = p["SILVER"]
    cl   = p["CRUDE"]
    ng   = p["NATGAS"]
    corn = p["CORN"]
    soy  = p["SOY"]
    wht  = p["WHEAT"]

    def _inv(key):
        v = jpy.get(key)
        return -v if v is not None else None

    jpy_display = f"{1/jpy['price']:.2f}" if jpy.get("price") else "N/A"
    jpy_chg  = _inv("change_pct")
    jpy_5d   = _inv("chg_5d")
    jpy_30d  = _inv("chg_30d")

    def spk(data):
        return f'<div class="sparkline-row">{sparkline_svg(data.get("closes", []))}</div>'

    def jpy_spk():
        inv = [1/c for c in jpy.get("closes", []) if c]
        return f'<div class="sparkline-row">{sparkline_svg(inv)}</div>'

    banner = build_macro_banner(p)
    macro  = build_macro_section(p)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>선물·옵션 시장 동향 보고서</title>
<style>{STYLE}</style>
</head>
<body>

<!-- 로딩 오버레이 -->
<div id="loading-overlay">
  <div class="loading-spinner"></div>
  <div class="loading-text">📊 시장 데이터 조회 중...</div>
  <div class="loading-sub">Yahoo Finance에서 실시간 데이터를 가져오고 있습니다</div>
</div>

<div class="header">
  <div class="header-top">
    <div>
      <h1>📊 글로벌 선물·옵션 시장 동향 보고서</h1>
      <div class="subtitle">통화 · 귀금속 · 에너지 · 곡물 선물 가격 및 주요 뉴스 분석</div>
      <div class="date-badge">🗓 {today_str} 기준 (싱가포르 시간 오후 6시)</div>
    </div>
    <div>
      <button class="refresh-btn" onclick="refreshPage()">🔄 새로고침</button>
      <div class="gen-time">⚡ 생성 시간: {gen_time}초</div>
    </div>
  </div>
</div>

{banner}

<!-- ===== 통화 (FX) ===== -->
<div class="section-title">💱 통화 선물 (FX Futures) — CME Globex</div>
<div class="grid">

  <div class="card">
    <div class="card-header">
      <div class="card-name">Australian Dollar</div>
      <div class="card-category cat-fx">AUD/USD</div>
    </div>
    <div class="card-price">{fmt(aud["price"], 4)}</div>
    <div class="card-unit">USD per AUD · CME 6A</div>
    {change_badge(aud["change_pct"])}{trend_spans(aud.get("chg_5d"), aud.get("chg_30d"))}
    {spk(aud)}
    <hr class="divider">
    <div class="news-block">{render_news_block(news.get("AUD_USD", []), aud["change_pct"])}</div>
  </div>

  <div class="card">
    <div class="card-header">
      <div class="card-name">Japanese Yen</div>
      <div class="card-category cat-fx">JPY/USD</div>
    </div>
    <div class="card-price">{jpy_display}</div>
    <div class="card-unit">JPY per USD · CME 6J</div>
    {change_badge(jpy_chg)}{trend_spans(jpy_5d, jpy_30d)}
    {jpy_spk()}
    <hr class="divider">
    <div class="news-block">{render_news_block(news.get("JPY_USD", []), jpy_chg)}</div>
  </div>

  <div class="card">
    <div class="card-header">
      <div class="card-name">Euro FX</div>
      <div class="card-category cat-fx">EUR/USD</div>
    </div>
    <div class="card-price">{fmt(eur["price"], 4)}</div>
    <div class="card-unit">USD per EUR · CME 6E</div>
    {change_badge(eur["change_pct"])}{trend_spans(eur.get("chg_5d"), eur.get("chg_30d"))}
    {spk(eur)}
    <hr class="divider">
    <div class="news-block">{render_news_block(news.get("EUR_USD", []), eur["change_pct"])}</div>
  </div>

</div>

<!-- ===== 귀금속 ===== -->
<div class="section-title">🥇 귀금속 선물 (Metals Futures) — COMEX</div>
<div class="grid grid-2col">

  <div class="card">
    <div class="card-header">
      <div class="card-name">Gold (금)</div>
      <div class="card-category cat-metal">XAU · GC</div>
    </div>
    <div class="card-price">${fmt(gold["price"], 2)}</div>
    <div class="card-unit">USD per troy oz · COMEX GC</div>
    {change_badge(gold["change_pct"])}{trend_spans(gold.get("chg_5d"), gold.get("chg_30d"))}
    {spk(gold)}
    <hr class="divider">
    <div class="news-block">{render_news_block(news.get("GOLD", []), gold["change_pct"])}</div>
  </div>

  <div class="card">
    <div class="card-header">
      <div class="card-name">Silver (은)</div>
      <div class="card-category cat-metal">XAG · SI</div>
    </div>
    <div class="card-price">${fmt(silv["price"], 2)}</div>
    <div class="card-unit">USD per troy oz · COMEX SI</div>
    {change_badge(silv["change_pct"])}{trend_spans(silv.get("chg_5d"), silv.get("chg_30d"))}
    {spk(silv)}
    <hr class="divider">
    <div class="news-block">{render_news_block(news.get("SILVER", []), silv["change_pct"])}</div>
  </div>

</div>

<!-- ===== 에너지 ===== -->
<div class="section-title">⚡ 에너지 선물 (Energy Futures) — NYMEX</div>
<div class="grid grid-2col">

  <div class="card">
    <div class="card-header">
      <div class="card-name">Crude Oil (WTI)</div>
      <div class="card-category cat-energy">WTI · CL</div>
    </div>
    <div class="card-price">${fmt(cl["price"], 2)}</div>
    <div class="card-unit">USD per barrel · NYMEX CL</div>
    {change_badge(cl["change_pct"])}{trend_spans(cl.get("chg_5d"), cl.get("chg_30d"))}
    {spk(cl)}
    <hr class="divider">
    <div class="news-block">{render_news_block(news.get("CRUDE", []), cl["change_pct"])}</div>
  </div>

  <div class="card">
    <div class="card-header">
      <div class="card-name">Natural Gas</div>
      <div class="card-category cat-energy">NG · Henry Hub</div>
    </div>
    <div class="card-price">${fmt(ng["price"], 3)}</div>
    <div class="card-unit">USD per MMBtu · NYMEX NG</div>
    {change_badge(ng["change_pct"])}{trend_spans(ng.get("chg_5d"), ng.get("chg_30d"))}
    {spk(ng)}
    <hr class="divider">
    <div class="news-block">{render_news_block(news.get("NATGAS", []), ng["change_pct"])}</div>
  </div>

</div>

<!-- ===== 곡물 ===== -->
<div class="section-title">🌾 곡물 선물 (Grain Futures) — CBOT (CME Group)</div>
<div class="grid">

  <div class="card">
    <div class="card-header">
      <div class="card-name">Corn (옥수수)</div>
      <div class="card-category cat-grain">ZC · CBOT</div>
    </div>
    <div class="card-price">${fmt(corn["price"], 2)}</div>
    <div class="card-unit">USD per bushel · CBOT ZC</div>
    {change_badge(corn["change_pct"])}{trend_spans(corn.get("chg_5d"), corn.get("chg_30d"))}
    {spk(corn)}
    <hr class="divider">
    <div class="news-block">{render_news_block(news.get("CORN", []), corn["change_pct"])}</div>
  </div>

  <div class="card">
    <div class="card-header">
      <div class="card-name">Soybeans (대두)</div>
      <div class="card-category cat-grain">ZS · CBOT</div>
    </div>
    <div class="card-price">${fmt(soy["price"], 2)}</div>
    <div class="card-unit">USD per bushel · CBOT ZS</div>
    {change_badge(soy["change_pct"])}{trend_spans(soy.get("chg_5d"), soy.get("chg_30d"))}
    {spk(soy)}
    <hr class="divider">
    <div class="news-block">{render_news_block(news.get("SOY", []), soy["change_pct"])}</div>
  </div>

  <div class="card">
    <div class="card-header">
      <div class="card-name">Wheat (밀)</div>
      <div class="card-category cat-grain">ZW · CBOT SRW</div>
    </div>
    <div class="card-price">${fmt(wht["price"], 2)}</div>
    <div class="card-unit">USD per bushel · CBOT ZW</div>
    {change_badge(wht["change_pct"])}{trend_spans(wht.get("chg_5d"), wht.get("chg_30d"))}
    {spk(wht)}
    <hr class="divider">
    <div class="news-block">{render_news_block(news.get("WHEAT", []), wht["change_pct"])}</div>
  </div>

</div>

<!-- ===== 시장 동향 분석 ===== -->
<div class="section-title">🔑 시장 동향 분석</div>
<div class="macro-section">
{macro}
</div>

<!-- 가격 요약 테이블 -->
<div class="section-title">📋 가격 현황 요약표</div>
<div style="padding: 16px 48px 32px; overflow-x:auto;">
  <table style="width:100%; border-collapse:collapse; font-size:13px;">
    <thead>
      <tr style="background:#1a1f2e; border-bottom:2px solid #2d3748;">
        <th style="text-align:left; padding:10px 14px; color:#718096; font-weight:600;">자산</th>
        <th style="text-align:left; padding:10px 14px; color:#718096; font-weight:600;">카테고리</th>
        <th style="text-align:right; padding:10px 14px; color:#718096; font-weight:600;">현재가</th>
        <th style="text-align:left; padding:10px 14px; color:#718096; font-weight:600;">24h 등락</th>
        <th style="text-align:left; padding:10px 14px; color:#718096; font-weight:600;">5일 추세</th>
        <th style="text-align:left; padding:10px 14px; color:#718096; font-weight:600;">30일 추세</th>
      </tr>
    </thead>
    <tbody>
      <tr style="border-bottom:1px solid #1a202c;">
        <td style="padding:10px 14px; color:#e2e8f0; font-weight:600;">Australian Dollar</td>
        <td style="padding:10px 14px; color:#63b3ed;">FX</td>
        <td style="padding:10px 14px; text-align:right; color:#f7fafc; font-weight:700;">{fmt(aud["price"], 4)}</td>
        {table_dir(aud["change_pct"])}{trend_cell(aud.get("chg_5d"))}{trend_cell(aud.get("chg_30d"))}
      </tr>
      <tr style="border-bottom:1px solid #1a202c; background:#0f1117;">
        <td style="padding:10px 14px; color:#e2e8f0; font-weight:600;">Japanese Yen</td>
        <td style="padding:10px 14px; color:#63b3ed;">FX</td>
        <td style="padding:10px 14px; text-align:right; color:#f7fafc; font-weight:700;">{jpy_display} JPY/USD</td>
        {table_dir(jpy_chg)}{trend_cell(jpy_5d)}{trend_cell(jpy_30d)}
      </tr>
      <tr style="border-bottom:1px solid #1a202c;">
        <td style="padding:10px 14px; color:#e2e8f0; font-weight:600;">Euro FX</td>
        <td style="padding:10px 14px; color:#63b3ed;">FX</td>
        <td style="padding:10px 14px; text-align:right; color:#f7fafc; font-weight:700;">{fmt(eur["price"], 4)}</td>
        {table_dir(eur["change_pct"])}{trend_cell(eur.get("chg_5d"))}{trend_cell(eur.get("chg_30d"))}
      </tr>
      <tr style="border-bottom:1px solid #1a202c; background:#0f1117;">
        <td style="padding:10px 14px; color:#e2e8f0; font-weight:600;">Gold</td>
        <td style="padding:10px 14px; color:#b794f4;">귀금속</td>
        <td style="padding:10px 14px; text-align:right; color:#f7fafc; font-weight:700;">${fmt(gold["price"], 2)}</td>
        {table_dir(gold["change_pct"])}{trend_cell(gold.get("chg_5d"))}{trend_cell(gold.get("chg_30d"))}
      </tr>
      <tr style="border-bottom:1px solid #1a202c;">
        <td style="padding:10px 14px; color:#e2e8f0; font-weight:600;">Silver</td>
        <td style="padding:10px 14px; color:#b794f4;">귀금속</td>
        <td style="padding:10px 14px; text-align:right; color:#f7fafc; font-weight:700;">${fmt(silv["price"], 2)}</td>
        {table_dir(silv["change_pct"])}{trend_cell(silv.get("chg_5d"))}{trend_cell(silv.get("chg_30d"))}
      </tr>
      <tr style="border-bottom:1px solid #1a202c; background:#0f1117;">
        <td style="padding:10px 14px; color:#e2e8f0; font-weight:600;">Crude Oil (WTI)</td>
        <td style="padding:10px 14px; color:#68d391;">에너지</td>
        <td style="padding:10px 14px; text-align:right; color:#f7fafc; font-weight:700;">${fmt(cl["price"], 2)}</td>
        {table_dir(cl["change_pct"])}{trend_cell(cl.get("chg_5d"))}{trend_cell(cl.get("chg_30d"))}
      </tr>
      <tr style="border-bottom:1px solid #1a202c;">
        <td style="padding:10px 14px; color:#e2e8f0; font-weight:600;">Natural Gas</td>
        <td style="padding:10px 14px; color:#68d391;">에너지</td>
        <td style="padding:10px 14px; text-align:right; color:#f7fafc; font-weight:700;">${fmt(ng["price"], 3)}</td>
        {table_dir(ng["change_pct"])}{trend_cell(ng.get("chg_5d"))}{trend_cell(ng.get("chg_30d"))}
      </tr>
      <tr style="border-bottom:1px solid #1a202c; background:#0f1117;">
        <td style="padding:10px 14px; color:#e2e8f0; font-weight:600;">Corn</td>
        <td style="padding:10px 14px; color:#9ae6b4;">곡물</td>
        <td style="padding:10px 14px; text-align:right; color:#f7fafc; font-weight:700;">${fmt(corn["price"], 2)}/bu</td>
        {table_dir(corn["change_pct"])}{trend_cell(corn.get("chg_5d"))}{trend_cell(corn.get("chg_30d"))}
      </tr>
      <tr style="border-bottom:1px solid #1a202c;">
        <td style="padding:10px 14px; color:#e2e8f0; font-weight:600;">Soybeans</td>
        <td style="padding:10px 14px; color:#9ae6b4;">곡물</td>
        <td style="padding:10px 14px; text-align:right; color:#f7fafc; font-weight:700;">${fmt(soy["price"], 2)}/bu</td>
        {table_dir(soy["change_pct"])}{trend_cell(soy.get("chg_5d"))}{trend_cell(soy.get("chg_30d"))}
      </tr>
      <tr>
        <td style="padding:10px 14px; color:#e2e8f0; font-weight:600;">Wheat</td>
        <td style="padding:10px 14px; color:#9ae6b4;">곡물</td>
        <td style="padding:10px 14px; text-align:right; color:#f7fafc; font-weight:700;">${fmt(wht["price"], 2)}/bu</td>
        {table_dir(wht["change_pct"])}{trend_cell(wht.get("chg_5d"))}{trend_cell(wht.get("chg_30d"))}
      </tr>
    </tbody>
  </table>
</div>

<div class="sources">
  <h4>📎 데이터 출처</h4>
  <a href="https://finance.yahoo.com/quote/AUDUSD=X">Yahoo Finance – AUD/USD</a>
  <a href="https://finance.yahoo.com/quote/JPYUSD=X">Yahoo Finance – JPY/USD</a>
  <a href="https://finance.yahoo.com/quote/EURUSD=X">Yahoo Finance – EUR/USD</a>
  <a href="https://finance.yahoo.com/quote/GC=F">Yahoo Finance – Gold</a>
  <a href="https://finance.yahoo.com/quote/SI=F">Yahoo Finance – Silver</a>
  <a href="https://finance.yahoo.com/quote/CL=F">Yahoo Finance – Crude Oil</a>
  <a href="https://finance.yahoo.com/quote/NG=F">Yahoo Finance – Natural Gas</a>
  <a href="https://finance.yahoo.com/quote/ZC=F">Yahoo Finance – Corn</a>
  <a href="https://finance.yahoo.com/quote/ZS=F">Yahoo Finance – Soybeans</a>
  <a href="https://finance.yahoo.com/quote/ZW=F">Yahoo Finance – Wheat</a>
</div>

<div class="footer">
  본 보고서는 {today_str} 기준 Yahoo Finance 데이터를 바탕으로 자동 생성되었습니다 (조회 시간: {gen_time}초).
  투자 조언이 아니며, 실제 투자 결정은 전문가와 상담하시기 바랍니다.
</div>

<script>
function refreshPage() {{
  var btn = document.querySelector('.refresh-btn');
  btn.disabled = true;
  btn.innerHTML = '⏳ 조회 중...';
  document.getElementById('loading-overlay').style.display = 'flex';
  window.location.href = '/?t=' + Date.now();
}}
</script>

</body>
</html>"""


# ── 보고서 파일 생성 ────────────────────────────────────────
def generate_report_file(output_path="report.html"):
    sgt       = pytz.timezone("Asia/Singapore")
    now_sgt   = datetime.now(sgt)
    today_str = now_sgt.strftime("%Y년 %m월 %d일 (%a)")
    t0        = datetime.now()
    prices    = fetch_prices()
    news      = fetch_all_news()
    gen_time  = round((datetime.now() - t0).total_seconds(), 1)
    html      = build_html(prices, news, today_str, gen_time)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[OK] Report saved → {output_path}  ({gen_time}s)")


# ── Flask 앱 ───────────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def index():
    sgt       = pytz.timezone("Asia/Singapore")
    now_sgt   = datetime.now(sgt)
    today_str = now_sgt.strftime("%Y년 %m월 %d일 (%a)")

    t0     = datetime.now()
    prices = fetch_prices()
    news   = fetch_all_news()
    gen_time = round((datetime.now() - t0).total_seconds(), 1)

    html = build_html(prices, news, today_str, gen_time)
    return Response(html, content_type='text/html; charset=utf-8')

# Vercel serverless handler
handler = app

if __name__ == '__main__':
    import sys
    if "--generate" in sys.argv:
        idx = sys.argv.index("--generate")
        out = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "report.html"
        generate_report_file(out)
    else:
        app.run(debug=True, host='127.0.0.1', port=8080)
