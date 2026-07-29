"""Manager Approval Dashboard (port 8000).

A thin UI over the agent server. It renders pending expense approvals as
cards (with the AI compliance audit and security warnings) and lets a manager
Approve or Reject, which resumes the paused workflow on the agent.

Run the agent server first (port 8080), then:
    uv run python -m uvicorn dashboard.main:app --port 8000
"""

import os

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

AGENT_URL = os.getenv("LOCAL_AGENT_URL", "http://localhost:8080")

app = FastAPI(title="Manager Approval Dashboard")


class Action(BaseModel):
    session_id: str
    interrupt_id: str
    action: str  # approve | reject


@app.get("/api/pending")
async def pending():
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{AGENT_URL}/pending")
        r.raise_for_status()
        return r.json()


@app.post("/api/action")
async def action(a: Action):
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{AGENT_URL}/action/{a.session_id}",
            json={"action": a.action, "interrupt_id": a.interrupt_id},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


HTML_PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Manager Approval Dashboard</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --muted:#94a3b8; --text:#e2e8f0;
          --green:#22c55e; --red:#ef4444; --amber:#f59e0b; --border:#334155; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--text); }
  header { padding:20px 28px; border-bottom:1px solid var(--border);
           display:flex; align-items:center; justify-content:space-between; }
  h1 { font-size:20px; margin:0; }
  .sub { color:var(--muted); font-size:13px; margin-top:4px; }
  button { cursor:pointer; border:none; border-radius:8px; padding:9px 16px;
           font-weight:600; font-size:14px; }
  .refresh { background:var(--border); color:var(--text); }
  main { padding:24px 28px; display:grid; gap:18px;
         grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); }
  .card { background:var(--card); border:1px solid var(--border);
          border-radius:14px; padding:18px; }
  .card.security { border-color:var(--red); box-shadow:0 0 0 1px var(--red) inset; }
  .row { display:flex; justify-content:space-between; align-items:baseline; }
  .amount { font-size:26px; font-weight:700; }
  .who { color:var(--muted); font-size:13px; }
  .tag { display:inline-block; font-size:11px; padding:3px 8px; border-radius:999px;
         background:var(--border); color:var(--text); margin-top:6px; }
  .warn { margin:12px 0; padding:10px 12px; border-radius:8px;
          background:rgba(239,68,68,.15); color:#fecaca; font-size:13px; font-weight:600; }
  .desc { margin:10px 0; font-size:14px; }
  details { margin:10px 0; }
  summary { cursor:pointer; color:#93c5fd; font-size:13px; font-weight:600; }
  pre { white-space:pre-wrap; font-size:12px; color:var(--text);
        background:var(--bg); padding:10px; border-radius:8px; border:1px solid var(--border); }
  .actions { display:flex; gap:10px; margin-top:14px; }
  .approve { background:var(--green); color:#052e16; flex:1; }
  .reject { background:var(--red); color:#450a0a; flex:1; }
  .empty { color:var(--muted); padding:40px; text-align:center; grid-column:1/-1; }
  .toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%);
           background:var(--card); border:1px solid var(--border); padding:12px 18px;
           border-radius:10px; font-size:14px; opacity:0; transition:opacity .2s; }
  .toast.show { opacity:1; }
</style>
</head>
<body>
<header>
  <div>
    <h1>Manager Approval Dashboard</h1>
    <div class="sub">Smart Expense Compliance — pending human review</div>
  </div>
  <button class="refresh" onclick="load()">Refresh</button>
</header>
<main id="cards"><div class="empty">Loading…</div></main>
<div class="toast" id="toast"></div>

<script>
async function load() {
  const res = await fetch('/api/pending');
  const data = await res.json();
  const cards = document.getElementById('cards');
  const items = data.pending || [];
  if (!items.length) { cards.innerHTML = '<div class="empty">No pending approvals.</div>'; return; }
  cards.innerHTML = items.map(renderCard).join('');
}

function esc(s){ return (s??'').toString().replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

function renderCard(p) {
  const sec = p.security_event;
  const amount = (p.amount ?? 0).toLocaleString('en-US',{style:'currency',currency:'USD'});
  const warn = sec ? '<div class="warn">⚠️ Security Event! Prompt injection attempt detected.</div>' : '';
  const report = p.compliance_report
      ? `<details><summary>View Compliance Audit</summary><pre>${esc(p.compliance_report)}</pre></details>`
      : (sec ? '<div class="sub">LLM review bypassed for safety.</div>' : '');
  return `<div class="card ${sec?'security':''}">
    <div class="row"><div class="amount">${amount}</div><div class="who">${esc(p.date)}</div></div>
    <div class="who">${esc(p.submitter)}</div>
    <span class="tag">${esc(p.category)}</span>
    ${warn}
    <div class="desc">${esc(p.description)}</div>
    ${report}
    <div class="actions">
      <button class="approve" onclick="act('${p.session_id}','${p.interrupt_id}','approve')">Approve</button>
      <button class="reject" onclick="act('${p.session_id}','${p.interrupt_id}','reject')">Reject</button>
    </div>
  </div>`;
}

async function act(session_id, interrupt_id, action) {
  toast(`${action === 'approve' ? 'Approving' : 'Rejecting'}…`);
  const res = await fetch('/api/action', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({session_id, interrupt_id, action})
  });
  const data = await res.json();
  toast(`${action === 'approve' ? '✅ Approved' : '🚫 Rejected'}`);
  load();
}

let tid;
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(tid); tid = setTimeout(()=>t.classList.remove('show'), 2200);
}

load();
setInterval(load, 5000);
</script>
</body>
</html>
"""
