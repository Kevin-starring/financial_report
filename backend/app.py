import os
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

GITHUB_PAT   = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "")      # e.g. "kkwon75/futures-report"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
PAGES_URL    = os.environ.get("PAGES_URL", "")         # e.g. "https://kkwon75.github.io/futures-report"
WORKFLOW_FILE = "generate_report.yml"

_GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

def _auth():
    return {"Authorization": f"Bearer {GITHUB_PAT}", **_GH_HEADERS}


@app.post("/api/trigger")
def trigger_workflow():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    try:
        resp = requests.post(url, headers=_auth(), json={"ref": GITHUB_BRANCH}, timeout=10)
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if resp.status_code == 204:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": resp.text}), resp.status_code


@app.get("/api/status")
def get_status():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/runs"
    try:
        resp = requests.get(url, headers=_auth(), params={"per_page": 1}, timeout=10)
    except requests.RequestException as e:
        return jsonify({"status": "error", "error": str(e)}), 502
    runs = resp.json().get("workflow_runs", [])
    if not runs:
        return jsonify({"status": "none", "conclusion": None})
    run = runs[0]
    return jsonify({
        "status":     run["status"],           # queued | in_progress | completed
        "conclusion": run.get("conclusion"),   # success | failure | cancelled | None
        "created_at": run["created_at"],
        "run_url":    run["html_url"],
        "pages_url":  PAGES_URL,
    })


UI_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>선물·옵션 보고서 생성기</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI','Apple SD Gothic Neo',sans-serif;background:#0f1117;color:#e2e8f0;
     min-height:100vh;display:flex;align-items:center;justify-content:center}
.wrap{max-width:520px;width:90%;text-align:center}
h1{font-size:26px;font-weight:700;color:#f7fafc;margin-bottom:8px}
.sub{font-size:13px;color:#718096;margin-bottom:36px;line-height:1.6}
.card{background:#1a1f2e;border:1px solid #2d3748;border-radius:16px;padding:36px;margin-bottom:24px}
.icon{font-size:52px;margin-bottom:16px;line-height:1}
.title{font-size:20px;font-weight:700;margin-bottom:8px}
.desc{font-size:13px;color:#718096}
.spinner{width:48px;height:48px;border:4px solid #2d3748;border-top-color:#63b3ed;
         border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 16px}
@keyframes spin{to{transform:rotate(360deg)}}
.btn{display:inline-flex;align-items:center;gap:8px;border-radius:10px;padding:14px 28px;
     font-size:16px;font-weight:600;cursor:pointer;transition:all .2s;text-decoration:none;
     border:1px solid #4a5568;background:linear-gradient(135deg,#2b6cb0,#1a365d);color:#bee3f8}
.btn:hover{background:linear-gradient(135deg,#3182ce,#2b6cb0);border-color:#63b3ed}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn-go{background:linear-gradient(135deg,#276749,#1c4532);border-color:#68d391;color:#9ae6b4}
.btn-go:hover{background:linear-gradient(135deg,#2f855a,#276749)}
.note{font-size:12px;color:#4a5568;margin-top:20px;line-height:1.7}
.badge{display:inline-block;background:#2d3748;border-radius:20px;padding:3px 12px;
       font-size:11px;color:#a0aec0;margin-top:6px}
a{color:#63b3ed}
</style>
</head>
<body>
<div class="wrap">
  <h1>📊 선물·옵션 시장 보고서</h1>
  <p class="sub">버튼을 클릭하면 GitHub Actions가 최신 시장 데이터로<br>보고서를 자동 생성하고 GitHub Pages에 배포합니다</p>

  <div class="card" id="card">
    <div id="icon" class="icon">📋</div>
    <div id="title" class="title">준비됨</div>
    <div id="desc"  class="desc">보고서를 생성하려면 아래 버튼을 클릭하세요</div>
  </div>

  <button class="btn" id="gen-btn" onclick="trigger()">🚀 보고서 생성</button>
  <a id="report-link" class="btn btn-go" style="display:none" href="#" target="_blank">📄 보고서 열기</a>

  <p class="note">
    Yahoo Finance 실시간 데이터 · 통화·귀금속·에너지·곡물 선물 포함<br>
    GitHub Actions 워크플로우가 보고서를 생성 후 GitHub Pages에 배포합니다
    <br><span class="badge">⏱ 생성 소요 시간: 약 1 ~ 2분</span>
  </p>
</div>

<script>
let pollId = null;

async function trigger() {
  const btn = document.getElementById('gen-btn');
  btn.disabled = true;
  setState('spin', '워크플로우 시작 중...', 'GitHub Actions에 요청을 보내고 있습니다');
  try {
    const r = await fetch('/api/trigger', {method:'POST'});
    const d = await r.json();
    if (d.ok) {
      setState('spin', '보고서 생성 중...', '시장 데이터를 수집하고 HTML을 렌더링합니다');
      startPolling();
    } else {
      setState('❌', '오류 발생', d.error || '다시 시도해 주세요');
      btn.disabled = false;
    }
  } catch(e) {
    setState('❌', '연결 오류', '서버에 연결할 수 없습니다');
    btn.disabled = false;
  }
}

function startPolling() {
  if (pollId) clearInterval(pollId);
  pollId = setInterval(checkStatus, 6000);
  checkStatus();
}

async function checkStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    if (d.status === 'completed') {
      clearInterval(pollId);
      pollId = null;
      document.getElementById('gen-btn').disabled = false;
      if (d.conclusion === 'success') {
        setState('✅', '보고서 완성!', '최신 시장 데이터로 보고서가 생성되었습니다');
        const lnk = document.getElementById('report-link');
        lnk.href = d.pages_url;
        lnk.style.display = 'inline-flex';
      } else {
        setState('❌', '생성 실패', `결과: ${d.conclusion} &nbsp;·&nbsp; <a href="${d.run_url}" target="_blank">로그 보기</a>`);
      }
    } else if (['in_progress','queued'].includes(d.status)) {
      setState('spin', '보고서 생성 중...', '잠시만 기다려 주세요 (약 1~2분)');
    }
  } catch(e) {}
}

function setState(icon, title, desc) {
  const iconEl  = document.getElementById('icon');
  const titleEl = document.getElementById('title');
  const descEl  = document.getElementById('desc');
  if (icon === 'spin') {
    iconEl.innerHTML = '<div class="spinner"></div>';
  } else {
    iconEl.innerHTML = icon;
    iconEl.className = 'icon';
  }
  titleEl.textContent = title;
  descEl.innerHTML    = desc;
}

// 페이지 로드 시 현재 상태 확인
window.addEventListener('load', async () => {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    if (['in_progress','queued'].includes(d.status)) {
      setState('spin', '보고서 생성 중...', '이전에 시작된 작업이 진행 중입니다');
      document.getElementById('gen-btn').disabled = true;
      startPolling();
    } else if (d.status === 'completed' && d.conclusion === 'success') {
      setState('✅', '최근 보고서 준비됨', '이전에 생성된 보고서를 확인할 수 있습니다');
      const lnk = document.getElementById('report-link');
      lnk.href = d.pages_url;
      lnk.style.display = 'inline-flex';
    }
  } catch(e) {}
});
</script>
</body>
</html>"""


@app.get("/")
def index():
    return UI_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
