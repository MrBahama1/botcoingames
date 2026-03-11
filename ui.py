"""Web-based dashboard UI — Landing, Setup Wizard, Mining Dashboard.

Security hardened:
- API keys encrypted in server-side sessions (never in MinerState, never in API responses)
- CSRF protection on all POST endpoints
- Session-based auth with HttpOnly cookies
- Input validation on all user inputs
- Security headers on all responses
- Log sanitization (API keys redacted)
"""

import json
import re
import time
import threading
import webbrowser
from flask import Flask, Response, request, jsonify, make_response, g
from state import MinerState
from config import AVAILABLE_MODELS, STAKE_AMOUNTS
from session_manager import SessionManager
from mining_manager import MiningManager
from auth import (
    require_auth, csrf_protect, validate_api_key, validate_email,
    validate_otp, sanitize_log
)

PHASE_COLORS = {
    "INIT": "#555", "SETUP": "#d4a017", "AUTHENTICATING": "#d4a017",
    "REQUESTING": "#00d4ff", "SOLVING": "#7b2fff", "VERIFYING": "#00d4ff",
    "SUBMITTING": "#d4a017", "POSTING_RECEIPT": "#00e676", "COOLDOWN": "#4a5568",
    "PAUSED": "#ff4757", "SUCCESS": "#00e676", "FAILED": "#ff4757",
}

VALID_MODELS = {mid for mid, _ in AVAILABLE_MODELS}

# Rate limiting (in-memory, per IP)
_rate_limits: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def _check_rate_limit(key: str, max_requests: int, window_seconds: int) -> bool:
    """Returns True if request is allowed, False if rate-limited."""
    now = time.time()
    with _rate_lock:
        if key not in _rate_limits:
            _rate_limits[key] = []
        timestamps = _rate_limits[key]
        _rate_limits[key] = [t for t in timestamps if now - t < window_seconds]
        if len(_rate_limits[key]) >= max_requests:
            return False
        _rate_limits[key].append(now)
        return True


# ---------------------------------------------------------------------------
# CSS theme shared across all pages
# ---------------------------------------------------------------------------
SHARED_CSS = """
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,500;0,9..40,700&family=JetBrains+Mono:wght@400;600&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#06080f;--bg-card:#0e1119;--bg-elevated:#151822;
  --border:rgba(255,255,255,0.06);--border-bright:rgba(255,255,255,0.12);
  --text:#e4e7ef;--dim:#6b7084;--muted:#3d4155;
  --accent:#00d4ff;--accent2:#7b2fff;--green:#00e676;--red:#ff4757;--yellow:#ffc107;--cyan:#00d4ff;
  --gradient:linear-gradient(135deg,#00d4ff,#7b2fff);
  --font:'DM Sans',system-ui,sans-serif;--mono:'JetBrains Mono',monospace;
  --radius:12px;--radius-sm:8px;
}
body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
button{font-family:var(--font)}
.grad-text{background:var(--gradient);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:10px 22px;border:none;border-radius:var(--radius);font-size:13px;font-weight:600;cursor:pointer;transition:all .2s;font-family:var(--font)}
.btn:hover{transform:translateY(-1px);filter:brightness(1.1)}.btn:active{transform:translateY(0)}
.btn:disabled{opacity:.35;cursor:not-allowed;transform:none!important}
.btn-accent{background:var(--gradient);color:#000}.btn-green{background:var(--green);color:#000}
.btn-ghost{background:rgba(255,255,255,0.05);color:var(--text);border:1px solid var(--border)}
.btn-ghost:hover{border-color:var(--border-bright);background:rgba(255,255,255,0.08)}
.btn-red{background:var(--red);color:#fff}.btn-yellow{background:var(--yellow);color:#000}
.btn-sm{padding:7px 14px;font-size:12px;border-radius:var(--radius-sm)}
.card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
.card-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--dim);margin-bottom:14px}
.glow{box-shadow:0 0 30px rgba(0,212,255,0.08)}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--muted);border-top-color:var(--accent);border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes fadeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
@keyframes slideIn{from{opacity:0;transform:translateX(40px)}to{opacity:1;transform:translateX(0)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--muted);border-radius:3px}
"""

# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------
LANDING_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOTCOIN Miner</title>
<style>
""" + SHARED_CSS + """
.landing{min-height:100vh;display:flex;flex-direction:column;align-items:center;overflow:hidden;position:relative}
.landing::before{content:'';position:absolute;top:-200px;left:50%;transform:translateX(-50%);width:800px;height:800px;background:radial-gradient(circle,rgba(0,212,255,0.06) 0%,rgba(123,47,255,0.04) 40%,transparent 70%);pointer-events:none}
.hero{text-align:center;padding:120px 24px 60px;max-width:720px;position:relative;z-index:1;animation:fadeIn .8s ease-out}
.hero h1{font-size:clamp(48px,8vw,80px);font-weight:700;line-height:1.05;margin-bottom:20px;letter-spacing:-2px}
.hero p{font-size:18px;color:var(--dim);max-width:480px;margin:0 auto 40px;line-height:1.7}
.hero .cta{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
.features{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px;max-width:820px;width:100%;padding:20px 24px 80px;position:relative;z-index:1}
.feature{padding:28px 24px;border-radius:var(--radius);background:rgba(14,17,25,0.7);border:1px solid var(--border);backdrop-filter:blur(10px);animation:fadeIn .8s ease-out;animation-fill-mode:both}
.feature:nth-child(1){animation-delay:.2s}.feature:nth-child(2){animation-delay:.35s}.feature:nth-child(3){animation-delay:.5s}
.feature .icon{width:40px;height:40px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px;margin-bottom:14px}
.feature h3{font-size:16px;font-weight:600;margin-bottom:6px}
.feature p{font-size:13px;color:var(--dim);line-height:1.6}
.f1 .icon{background:rgba(0,212,255,0.12);color:var(--accent)}
.f2 .icon{background:rgba(0,230,118,0.12);color:var(--green)}
.f3 .icon{background:rgba(123,47,255,0.12);color:var(--accent2)}
</style></head><body>
<div class="landing">
  <div class="hero">
    <h1>Easily earn <span class="grad-text">BOTCOIN</span></h1>
    <p>Plug-and-play AI mining agent. Stake tokens, solve challenges with LLMs, earn on-chain rewards on Base.</p>
    <div class="cta">
      <a href="/setup" class="btn btn-accent" style="padding:14px 36px;font-size:15px">Get Started</a>
      <a href="https://agentmoney.net/" target="_blank" class="btn btn-ghost" style="padding:14px 36px;font-size:15px">Learn More</a>
    </div>
  </div>
  <div class="features">
    <div class="feature f1"><div class="icon">&#9889;</div><h3>AI-Powered Solving</h3><p>Multi-model LLM solver tackles hybrid NLP challenges automatically. Choose Claude, GPT, or Gemini.</p></div>
    <div class="feature f2"><div class="icon">&#128274;</div><h3>Non-Custodial</h3><p>Your keys never leave <a href="https://bankr.bot/" target="_blank">Bankr</a>. All transactions require your approval. Fully transparent on-chain.</p></div>
    <div class="feature f3"><div class="icon">&#128200;</div><h3>Real-Time Dashboard</h3><p>Live stats, staking management, pending transactions, and LLM output &mdash; all in your browser.</p></div>
  </div>
</div>
</body></html>"""

# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------
SETUP_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf-token" content="CSRFTOKEN">
<title>BOTCOIN Miner &mdash; Setup</title>
<style>
""" + SHARED_CSS + r"""
.container{max-width:560px;margin:0 auto;padding:40px 20px 60px}
.logo{text-align:center;margin-bottom:8px;font-size:24px;font-weight:700;letter-spacing:-1px}
.subtitle{text-align:center;color:var(--dim);margin-bottom:36px;font-size:13px}
.progress{display:flex;gap:6px;margin-bottom:32px}
.progress .seg{flex:1;height:4px;border-radius:2px;background:var(--muted);transition:background .3s}
.progress .seg.done{background:var(--green)}.progress .seg.active{background:var(--accent)}
.step{display:none;animation:fadeIn .4s ease-out}.step.active{display:block}
.step-header{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.step-num{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;background:var(--gradient);color:#000;flex-shrink:0}
.step-check{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;background:var(--green);color:#000;flex-shrink:0}
.step-title{font-size:17px;font-weight:600}
.step-desc{color:var(--dim);font-size:13px;margin-bottom:18px;margin-left:38px}
.step-body{margin-left:38px}
label{display:block;font-size:12px;font-weight:600;margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim)}
input[type=text],input[type=email],input[type=password],select{width:100%;padding:10px 14px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg);color:var(--text);font-size:14px;font-family:var(--font);outline:none;transition:border .2s}
input:focus,select:focus{border-color:var(--accent)}
.row{display:flex;gap:8px;align-items:flex-end}.row input{flex:1}
.info-box{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);padding:12px;margin:10px 0}
.info-box .lbl{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--dim)}
.info-box .val{font-size:18px;font-weight:700;margin-top:2px;word-break:break-all}
.status{padding:10px 14px;border-radius:var(--radius-sm);font-size:13px;margin:10px 0}
.status.ok{background:rgba(0,230,118,0.08);color:var(--green);border:1px solid rgba(0,230,118,0.15)}
.status.warn{background:rgba(255,193,7,0.08);color:var(--yellow);border:1px solid rgba(255,193,7,0.15)}
.status.err{background:rgba(255,71,87,0.08);color:var(--red);border:1px solid rgba(255,71,87,0.15)}
.status.info{background:rgba(0,212,255,0.06);color:var(--accent);border:1px solid rgba(0,212,255,0.1)}
.or{text-align:center;color:var(--muted);margin:16px 0;font-size:12px;text-transform:uppercase;letter-spacing:1px}
.terms{font-size:11px;color:var(--dim);margin-top:8px}.terms a{color:var(--accent)}
.tier-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}
.tier-card{padding:12px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg);text-align:center;cursor:pointer;transition:all .2s}
.tier-card:hover{border-color:var(--accent);background:rgba(0,212,255,0.04)}
.tier-card .amt{font-size:18px;font-weight:700}.tier-card .cr{font-size:11px;color:var(--dim);margin-top:2px}
.t1 .amt{color:var(--accent)}.t2 .amt{color:var(--green)}.t3 .amt{color:var(--accent2)}
</style></head><body>
<div class="container">
<div class="logo"><span class="grad-text">BOTCOIN</span> MINER</div>
<p class="subtitle">Plug & Play Mining Agent</p>
<div class="progress" id="progress"></div>

<!-- Step 1: Connect -->
<div class="step active" id="step1">
  <div class="step-header"><span class="step-num">1</span><span class="step-title">Log in to Bankr</span></div>
  <p class="step-desc">Sign up or log in with your email. Your wallet is created automatically. <a href="https://bankr.bot/" target="_blank">What is Bankr?</a></p>
  <div class="step-body">
    <label>Email Address</label>
    <div class="row">
      <input type="email" id="emailInput" placeholder="you@example.com">
      <button class="btn btn-accent btn-sm" id="btnSendOtp" onclick="sendOtp()">Send Code</button>
    </div>
    <div id="otpSection" style="display:none;margin-top:12px">
      <label>Verification Code</label>
      <div class="row">
        <input type="text" id="otpInput" placeholder="123456" maxlength="8">
        <button class="btn btn-green btn-sm" onclick="verifyOtp()">Verify</button>
      </div>
      <p class="terms">By verifying you accept the <a href="https://bankr.bot/terms" target="_blank">Terms of Service</a></p>
    </div>
    <div id="step1Status"></div>
    <div id="advancedSection" style="margin-top:20px;text-align:center">
      <a href="#" onclick="document.getElementById('apiKeySection').style.display='block';this.style.display='none';return false" style="font-size:11px;color:var(--muted)">Advanced: connect with API key</a>
    </div>
    <div id="apiKeySection" style="display:none;margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">
      <label>Bankr API Key</label>
      <div class="row">
        <input type="password" id="apiKeyInput" placeholder="bk_..." autocomplete="off">
        <button class="btn btn-ghost btn-sm" onclick="submitApiKey()">Connect</button>
      </div>
      <p class="terms" style="margin-top:6px">Get a key at <a href="https://bankr.bot/api" target="_blank">bankr.bot/api</a></p>
    </div>
  </div>
</div>

<!-- Step 2: Wallet -->
<div class="step" id="step2">
  <div class="step-header"><span class="step-num">2</span><span class="step-title">Wallet & Balances</span></div>
  <p class="step-desc">Your non-custodial wallet on Base. You need ETH for gas and BOTCOIN for staking.</p>
  <div class="step-body">
    <div class="info-box"><div class="lbl">Wallet Address</div><div class="val" id="walletAddr" style="font-size:13px;font-family:var(--mono)">—</div></div>
    <div style="display:flex;gap:10px">
      <div class="info-box" style="flex:1"><div class="lbl">ETH</div><div class="val" id="ethBal">—</div></div>
      <div class="info-box" style="flex:1"><div class="lbl">BOTCOIN</div><div class="val" id="botBal">—</div></div>
    </div>
    <div id="step2Status"></div>
    <div id="fundingActions" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px"></div>
    <div style="display:flex;gap:8px;margin-top:14px">
      <button class="btn btn-ghost btn-sm" onclick="goStep(1)">Back</button>
      <button class="btn btn-accent btn-sm" id="btnStep2Next" onclick="goStep(3)">Continue</button>
    </div>
  </div>
</div>

<!-- Step 3: Stake -->
<div class="step" id="step3">
  <div class="step-header"><span class="step-num">3</span><span class="step-title">Stake BOTCOIN</span></div>
  <p class="step-desc">Stake to earn credits. Higher stake = more credits per solve.</p>
  <div class="step-body">
    <div class="tier-cards">
      <div class="tier-card t1" onclick="doStake('25000000000000000000000000')"><div class="amt">25M</div><div class="cr">1 credit/solve</div></div>
      <div class="tier-card t2" onclick="doStake('50000000000000000000000000')"><div class="amt">50M</div><div class="cr">2 credits/solve</div></div>
      <div class="tier-card t3" onclick="doStake('100000000000000000000000000')"><div class="amt">100M</div><div class="cr">3 credits/solve</div></div>
    </div>
    <div id="step3Status"></div>
    <div style="display:flex;gap:8px;margin-top:14px">
      <button class="btn btn-ghost btn-sm" onclick="goStep(2)">Back</button>
      <button class="btn btn-accent btn-sm" onclick="goStep(4)">Continue / Skip</button>
    </div>
  </div>
</div>

<!-- Step 4: LLM -->
<div class="step" id="step4">
  <div class="step-header"><span class="step-num">4</span><span class="step-title">LLM Configuration</span></div>
  <p class="step-desc">Pick your solver model. LLM credits power inference — you need credits to mine.</p>
  <div class="step-body">
    <label>Model</label>
    <select id="modelSetupSelect">MODELOPTIONS</select>
    <div class="info-box" style="margin-top:12px"><div class="lbl">LLM Gateway Credits</div><div class="val" id="llmCredits" style="color:var(--yellow)">Checking...</div></div>
    <div id="llmTopupLink" style="display:none;margin-top:8px">
      <a href="https://bankr.bot/llm?tab=credits" target="_blank" style="font-size:13px">Top up LLM credits &rarr;</a>
      <span style="font-size:12px;color:var(--dim);display:block;margin-top:4px">New accounts start with $0. You need credits to mine.</span>
      <button class="btn btn-ghost btn-sm" onclick="loadLLMCredits()" style="margin-top:8px">Refresh</button>
    </div>
    <div style="margin-top:16px;padding:14px;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm)">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-bottom:0;text-transform:none;font-size:13px;color:var(--text)">
        <input type="checkbox" id="autoTopupCheck" style="width:16px;height:16px;accent-color:var(--accent)">
        Enable auto top-up ($25 USDC when credits &lt; $5)
      </label>
    </div>
    <div id="step4Status"></div>
    <div style="display:flex;gap:8px;margin-top:16px">
      <button class="btn btn-ghost btn-sm" onclick="goStep(3)">Back</button>
      <button class="btn btn-green" onclick="finishSetup()" style="flex:1">Start Mining</button>
    </div>
  </div>
</div>

<!-- Step 5: Done -->
<div class="step" id="step5">
  <div class="step-header"><span class="step-check">&#10003;</span><span class="step-title">Setup Complete</span></div>
  <div class="status ok" style="margin:16px 0 0 38px">Mining is starting. Redirecting to dashboard...</div>
</div>

<script>
let CSRF=document.querySelector('meta[name=csrf-token]').content;
function updateCSRF(token){if(token){CSRF=token;document.querySelector('meta[name=csrf-token]').content=token}}
function H(method,body){return{method,headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF},body:body?JSON.stringify(body):undefined}}
let currentStep=1;
function updateProgress(){const p=document.getElementById('progress');p.innerHTML='';for(let i=1;i<=4;i++){const cls=i<currentStep?'seg done':i===currentStep?'seg active':'seg';p.innerHTML+='<div class="'+cls+'"></div>'}}
updateProgress();
function showStatus(elId,cls,msg){document.getElementById(elId).innerHTML='<div class="status '+cls+'">'+msg+'</div>'}
function goStep(n){document.getElementById('step'+currentStep).classList.remove('active');currentStep=n;document.getElementById('step'+n).classList.add('active');updateProgress();if(n===2)loadWallet();if(n===3)checkStake();if(n===4)loadLLMCredits()}

async function submitApiKey(){
  const key=document.getElementById('apiKeyInput').value.trim();
  if(!key||!key.startsWith('bk_')){showStatus('step1Status','err','Invalid API key format (must start with bk_)');return}
  showStatus('step1Status','info','<span class="spinner"></span> Connecting...');
  const r=await fetch('/api/setup/connect',H('POST',{api_key:key}));
  const d=await r.json();
  if(d.ok){updateCSRF(d.csrf_token);showStatus('step1Status','ok','Connected!');setTimeout(()=>goStep(2),500)}
  else showStatus('step1Status','err',d.error||'Invalid key')
}
async function sendOtp(){
  const email=document.getElementById('emailInput').value.trim();
  if(!email||!email.includes('@')){showStatus('step1Status','err','Enter a valid email');return}
  document.getElementById('btnSendOtp').disabled=true;
  showStatus('step1Status','info','<span class="spinner"></span> Sending code...');
  const r=await fetch('/api/setup/send-otp',H('POST',{email}));
  const d=await r.json();
  if(d.ok){showStatus('step1Status','ok','Code sent! Check email.');document.getElementById('otpSection').style.display='block'}
  else{showStatus('step1Status','err',d.error||'Failed');document.getElementById('btnSendOtp').disabled=false}
}
async function verifyOtp(){
  const email=document.getElementById('emailInput').value.trim(),code=document.getElementById('otpInput').value.trim();
  if(!code){showStatus('step1Status','err','Enter the verification code');return}
  showStatus('step1Status','info','<span class="spinner"></span> Verifying...');
  const r=await fetch('/api/setup/verify-otp',H('POST',{email,code}));
  const d=await r.json();
  if(d.ok){updateCSRF(d.csrf_token);showStatus('step1Status','ok','Account created!');setTimeout(()=>goStep(2),500)}
  else showStatus('step1Status','err',d.error||'Failed')
}
async function loadWallet(){
  showStatus('step2Status','info','<span class="spinner"></span> Loading...');
  const r=await fetch('/api/setup/wallet');const d=await r.json();
  document.getElementById('walletAddr').textContent=d.address||'—';
  document.getElementById('ethBal').textContent=d.eth!==undefined?parseFloat(d.eth).toFixed(6)+' ETH':'—';
  document.getElementById('botBal').textContent=d.botcoin!==undefined?Number(d.botcoin).toLocaleString()+' BOTCOIN':'—';
  const a=document.getElementById('fundingActions');a.innerHTML='';
  if(d.eth<0.001)a.innerHTML+='<a href="https://app.across.to/bridge-and-swap" target="_blank" class="btn btn-ghost btn-sm">Bridge ETH</a>';
  if(d.botcoin<25000000)a.innerHTML+='<a href="https://app.uniswap.org/swap?outputCurrency=0xA601877977340862Ca67f816eb079958E5bd0BA3&chain=base" target="_blank" class="btn btn-ghost btn-sm">Buy BOTCOIN</a>';
  if(d.eth<0.001||d.botcoin<25000000){a.innerHTML+='<button class="btn btn-ghost btn-sm" onclick="loadWallet()">Refresh</button>';showStatus('step2Status','warn','Need ETH for gas and 25M+ BOTCOIN to mine.')}
  else showStatus('step2Status','ok','Balances look good!')
}
async function checkStake(){
  showStatus('step3Status','info','<span class="spinner"></span> Checking stake...');
  try{const r=await fetch('/api/setup/check-stake');const d=await r.json();
    if(d.staked>0){const tier=d.staked>=100000000?'3cr/solve':d.staked>=50000000?'2cr/solve':'1cr/solve';showStatus('step3Status','ok','Staked: '+Number(d.staked).toLocaleString(undefined,{maximumFractionDigits:0})+' BOTCOIN ('+tier+')')}
    else showStatus('step3Status','info','Not staked yet. Pick a tier above or skip for now.')
  }catch(e){showStatus('step3Status','info','Pick a tier to stake, or skip.')}
}
async function doStake(amount){
  showStatus('step3Status','info','<span class="spinner"></span> Staking (2 transactions)...');
  const r=await fetch('/api/setup/stake',H('POST',{amount}));
  const d=await r.json();
  showStatus('step3Status',d.ok?'ok':'err',d.message||'Done')
}
async function loadLLMCredits(){
  const r=await fetch('/api/setup/llm-credits');const d=await r.json();
  const el=document.getElementById('llmCredits'),link=document.getElementById('llmTopupLink');
  if(d.balance>=0){el.textContent='$'+d.balance.toFixed(2);el.style.color=d.balance>1?'var(--green)':'var(--yellow)';link.style.display=d.balance<1?'block':'none'}
  else{el.textContent='Unable to check';link.style.display='block'}
}
async function finishSetup(){
  const model=document.getElementById('modelSetupSelect').value,autoTopup=document.getElementById('autoTopupCheck').checked;
  showStatus('step4Status','info','<span class="spinner"></span> Starting...');
  const r=await fetch('/api/setup/finish',H('POST',{model,auto_topup:autoTopup}));
  const d=await r.json();
  if(d.ok){goStep(5);setTimeout(()=>{window.location.href='/dashboard'},2000)}
  else showStatus('step4Status','err',d.error||'Failed')
}
</script></body></html>"""

# ---------------------------------------------------------------------------
# Mining dashboard
# ---------------------------------------------------------------------------
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf-token" content="CSRFTOKEN">
<title>BOTCOIN Miner Dashboard</title>
<style>
""" + SHARED_CSS + r"""
.header{display:flex;align-items:center;justify-content:space-between;padding:14px 24px;background:var(--bg-card);border-bottom:1px solid var(--border)}
.header-left{display:flex;align-items:center;gap:16px}
.logo{font-size:18px;font-weight:700;letter-spacing:-.5px}
.phase{display:inline-flex;align-items:center;gap:6px;padding:5px 14px;border-radius:20px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#fff}
.phase .dot{width:6px;height:6px;border-radius:50%;background:currentColor;animation:pulse 1.5s infinite}
.controls{display:flex;align-items:center;gap:8px}
.controls select{padding:6px 10px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:12px;font-family:var(--font)}
.wallet-tag{font-size:11px;color:var(--dim);font-family:var(--mono)}
.grid{display:grid;grid-template-columns:300px 1fr 1fr;grid-template-rows:1fr 260px;gap:14px;padding:14px 24px;height:calc(100vh - 58px)}
/* Staking panel */
.stake-panel{grid-row:1/3;display:flex;flex-direction:column;gap:14px;overflow-y:auto}
.staked-display{text-align:center;padding:24px 16px}
.staked-amt{font-size:36px;font-weight:700;line-height:1.2}
.staked-label{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin-top:2px}
.tier-badge{display:inline-block;padding:4px 12px;border-radius:20px;font-size:11px;font-weight:700;margin-top:8px}
.tier-bar{display:flex;gap:4px;margin:14px 0}
.tier-bar .seg{flex:1;height:6px;border-radius:3px;background:var(--muted)}
.tier-bar .seg.filled{background:var(--gradient)}
.wallet-bal{display:flex;justify-content:space-between;padding:10px 0;border-top:1px solid var(--border);font-size:13px}
.wallet-bal .lbl{color:var(--dim)}
.stake-actions{display:flex;flex-direction:column;gap:6px}
.cooldown-bar{padding:14px;border-radius:var(--radius-sm);background:rgba(255,193,7,0.06);border:1px solid rgba(255,193,7,0.12);text-align:center}
.cooldown-bar .timer{font-size:22px;font-weight:700;color:var(--yellow);font-family:var(--mono)}
.cooldown-bar .label{font-size:11px;color:var(--dim);text-transform:uppercase;margin-top:2px}
/* Stats */
.stats-area{display:flex;flex-direction:column;gap:14px}
.stats-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.stat{padding:12px;background:var(--bg);border-radius:var(--radius-sm);border:1px solid var(--border)}
.stat-label{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.stat-value{font-size:24px;font-weight:700;margin-top:2px;line-height:1.2}
.stat-value.green{color:var(--green)}.stat-value.red{color:var(--red)}.stat-value.cyan{color:var(--cyan)}
.stat-value.yellow{color:var(--yellow)}.stat-value.purple{color:var(--accent2)}
/* Pending txs */
.tx-list{flex:1;overflow-y:auto}
.tx-item{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);font-size:12px}
.tx-icon{width:20px;height:20px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10px;flex-shrink:0}
.tx-pending .tx-icon{background:rgba(0,212,255,0.12);color:var(--accent)}
.tx-confirmed .tx-icon{background:rgba(0,230,118,0.12);color:var(--green)}
.tx-failed .tx-icon{background:rgba(255,71,87,0.12);color:var(--red)}
.tx-desc{flex:1;color:var(--text)}.tx-time{color:var(--muted);font-size:11px}
/* LLM panel */
.llm-panel{display:flex;flex-direction:column}
.llm-content{flex:1;overflow-y:auto;font-family:var(--mono);font-size:11px;white-space:pre-wrap;word-break:break-word;color:var(--dim);padding:10px;background:var(--bg);border-radius:var(--radius-sm)}
/* Log */
.log-panel{grid-column:2/4}
.log-content{flex:1;overflow-y:auto;font-family:var(--mono);font-size:11px;line-height:1.8;padding:10px;background:var(--bg);border-radius:var(--radius-sm)}
.log-line{color:var(--dim)}.log-ts{color:var(--accent);margin-right:8px}
/* Toasts */
.toast-container{position:fixed;top:70px;right:20px;z-index:1000;display:flex;flex-direction:column;gap:8px}
.toast{padding:12px 18px;border-radius:var(--radius-sm);font-size:13px;font-weight:500;animation:slideIn .3s ease-out;min-width:260px;box-shadow:0 8px 30px rgba(0,0,0,.4)}
.toast-ok{background:rgba(0,230,118,0.15);color:var(--green);border:1px solid rgba(0,230,118,0.2)}
.toast-err{background:rgba(255,71,87,0.15);color:var(--red);border:1px solid rgba(255,71,87,0.2)}
@media(max-width:900px){.grid{grid-template-columns:1fr;grid-template-rows:auto auto auto 260px}.stake-panel{grid-row:auto}.log-panel{grid-column:auto}}
</style></head><body>
<div class="header">
  <div class="header-left">
    <div class="logo"><span class="grad-text">BOTCOIN</span> MINER</div>
    <span class="phase" id="phaseBadge"><span class="dot"></span> INIT</span>
    <span class="wallet-tag" id="walletInfo"></span>
  </div>
  <div class="controls">
    <select id="modelSelect" title="LLM Model">MODELOPTIONS</select>
    <button class="btn btn-green btn-sm" id="btnStart">Start</button>
    <button class="btn btn-red btn-sm" id="btnStop">Stop</button>
    <button class="btn btn-ghost btn-sm" onclick="doLogout()">Logout</button>
  </div>
</div>
<div class="grid">
  <!-- Left: Staking -->
  <div class="stake-panel">
    <div class="card">
      <div class="card-title">Staking</div>
      <div class="staked-display">
        <div class="staked-amt" id="stakedAmt">--</div>
        <div class="staked-label">BOTCOIN Staked</div>
        <div class="tier-badge" id="tierBadge" style="display:none"></div>
      </div>
      <div class="tier-bar"><div class="seg" id="tb1"></div><div class="seg" id="tb2"></div><div class="seg" id="tb3"></div></div>
      <div class="wallet-bal"><span class="lbl">Wallet (unstaked)</span><span id="walletBotcoin">--</span></div>
      <div class="wallet-bal"><span class="lbl">ETH (gas)</span><span id="walletEth">--</span></div>
      <div id="cooldownSection" style="display:none">
        <div class="cooldown-bar">
          <div class="timer" id="cooldownTimer">--:--:--</div>
          <div class="label" id="cooldownLabel">Withdrawal cooldown</div>
        </div>
      </div>
      <div class="stake-actions" style="margin-top:12px">
        <div style="display:flex;gap:6px">
          <button class="btn btn-accent btn-sm" style="flex:1" onclick="dashStake('25000000000000000000000000')">Stake 25M</button>
          <button class="btn btn-accent btn-sm" style="flex:1" onclick="dashStake('50000000000000000000000000')">Stake 50M</button>
          <button class="btn btn-accent btn-sm" style="flex:1" onclick="dashStake('100000000000000000000000000')">Stake 100M</button>
        </div>
        <div style="display:flex;gap:6px">
          <a href="https://app.uniswap.org/swap?outputCurrency=0xA601877977340862Ca67f816eb079958E5bd0BA3&chain=base" target="_blank" class="btn btn-ghost btn-sm" style="flex:1;text-decoration:none">Buy BOTCOIN</a>
          <a href="https://app.across.to/bridge-and-swap" target="_blank" class="btn btn-ghost btn-sm" style="flex:1;text-decoration:none">Bridge ETH</a>
        </div>
        <div style="display:flex;gap:6px">
          <button class="btn btn-yellow btn-sm" style="flex:1" onclick="dashUnstake()">Unstake</button>
          <button class="btn btn-ghost btn-sm" style="flex:1" id="btnWithdraw" onclick="dashWithdraw()" disabled>Withdraw</button>
        </div>
      </div>
      <div id="stakeStatus" style="font-size:12px;margin-top:8px"></div>
    </div>
  </div>

  <!-- Center: Stats + Pending Txs -->
  <div class="stats-area">
    <div class="card" style="flex-shrink:0">
      <div class="card-title">Mining Stats</div>
      <div class="stats-grid">
        <div class="stat"><div class="stat-label">Solves</div><div class="stat-value green" id="sSolves">0</div></div>
        <div class="stat"><div class="stat-label">Fails</div><div class="stat-value red" id="sFails">0</div></div>
        <div class="stat"><div class="stat-label">Credits</div><div class="stat-value cyan" id="sCredits">0</div></div>
        <div class="stat"><div class="stat-label">Epoch</div><div class="stat-value" id="sEpoch">--</div></div>
        <div class="stat"><div class="stat-label">Uptime</div><div class="stat-value" id="sUptime" style="font-size:16px">0h 0m</div></div>
        <div class="stat"><div class="stat-label">LLM Credits <a href="https://bankr.bot/llm?tab=credits" target="_blank" style="font-size:9px">&#8599;</a></div><div class="stat-value yellow" id="sLLM">--</div></div>
      </div>
    </div>
    <div class="card" style="flex:1;display:flex;flex-direction:column;overflow:hidden">
      <div class="card-title">Transactions</div>
      <div class="tx-list" id="txList"><div style="color:var(--muted);font-size:12px;padding:8px 0">No transactions yet</div></div>
    </div>
  </div>

  <!-- Right: LLM Output -->
  <div class="card llm-panel">
    <div class="card-title">LLM Output</div>
    <div class="llm-content" id="llmOutput">Waiting for first solve...</div>
  </div>

  <!-- Bottom: Log -->
  <div class="card log-panel" style="display:flex;flex-direction:column">
    <div class="card-title">Activity Log</div>
    <div class="log-content" id="logContent">Starting up...</div>
  </div>
</div>
<div class="toast-container" id="toasts"></div>

<script>
const PC=PHASECOLORSJS;
const CSRF=document.querySelector('meta[name=csrf-token]').content;
function H(method,body){return{method,headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF},body:body?JSON.stringify(body):undefined}}
let lastVersion=-1,prevTxMap={};

function esc(s){const el=document.createElement('span');el.textContent=s;return el.innerHTML}
function toast(msg,ok){const c=document.getElementById('toasts'),t=document.createElement('div');t.className='toast '+(ok?'toast-ok':'toast-err');t.textContent=msg;c.appendChild(t);setTimeout(()=>t.remove(),5000)}
function ago(ts){const s=Math.floor((Date.now()/1000)-ts);if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';return Math.floor(s/3600)+'h ago'}
function fmtCooldown(secs){if(secs<=0)return'Ready!';const h=Math.floor(secs/3600),m=Math.floor((secs%3600)/60),s=secs%60;return(h?h+'h ':'')+(m?m+'m ':'')+s+'s'}

function connectSSE(){const es=new EventSource('/events');es.onmessage=function(e){const d=JSON.parse(e.data);if(d.version===lastVersion)return;lastVersion=d.version;update(d)};es.onerror=function(){es.close();setTimeout(connectSSE,2000)}}

function update(d){
  // Phase
  const badge=document.getElementById('phaseBadge');badge.innerHTML='<span class="dot"></span> '+d.phase;badge.style.background=PC[d.phase]||'#555';
  // Wallet
  const addr=d.miner_address;const short=addr&&addr.length>12?addr.slice(0,6)+'...'+addr.slice(-4):'';
  document.getElementById('walletInfo').textContent=short;
  // Stats
  document.getElementById('sSolves').textContent=d.total_solves;
  document.getElementById('sFails').textContent=d.total_fails;
  document.getElementById('sCredits').textContent=d.total_credits;
  document.getElementById('sEpoch').textContent=d.epoch_id||'--';
  document.getElementById('sUptime').textContent=d.uptime;
  document.getElementById('sLLM').textContent=d.llm_credits>=0?'$'+d.llm_credits.toFixed(2):'--';
  // LLM output
  if(d.llm_output)document.getElementById('llmOutput').textContent=d.llm_output;
  // Log
  const logEl=document.getElementById('logContent');
  if(d.log_lines&&d.log_lines.length>0){logEl.innerHTML=d.log_lines.map(l=>{const p=l.match(/^(\d{2}:\d{2}:\d{2})\s(.*)$/);if(p)return'<div class="log-line"><span class="log-ts">'+esc(p[1])+'</span>'+esc(p[2])+'</div>';return'<div class="log-line">'+esc(l)+'</div>'}).join('');logEl.scrollTop=logEl.scrollHeight}
  // Model
  const sel=document.getElementById('modelSelect');if(sel.value!==d.model)sel.value=d.model;
  // Buttons
  document.getElementById('btnStart').disabled=d.mining_active;document.getElementById('btnStop').disabled=!d.mining_active;
  document.getElementById('btnStart').style.opacity=d.mining_active?'.35':'1';document.getElementById('btnStop').style.opacity=d.mining_active?'1':'.35';
  // Staking
  const sa=d.staked_amount||0;
  document.getElementById('stakedAmt').textContent=sa>0?(sa/1e6).toFixed(1)+'M':'0';
  const tier=sa>=100e6?3:sa>=50e6?2:sa>=25e6?1:0;
  const tb=document.getElementById('tierBadge');
  if(tier>0){tb.style.display='inline-block';tb.textContent='Tier '+tier+' \u2014 '+tier+'cr/solve';tb.style.background=tier===3?'rgba(123,47,255,0.15)':tier===2?'rgba(0,230,118,0.15)':'rgba(0,212,255,0.15)';tb.style.color=tier===3?'var(--accent2)':tier===2?'var(--green)':'var(--accent)'}else{tb.style.display='none'}
  ['tb1','tb2','tb3'].forEach((id,i)=>{document.getElementById(id).className='seg'+(tier>i?' filled':'')});
  document.getElementById('walletBotcoin').textContent=d.wallet_botcoin>0?Number(d.wallet_botcoin).toLocaleString(undefined,{maximumFractionDigits:0})+' BOTCOIN':'--';
  document.getElementById('walletEth').textContent=d.eth_balance>0?parseFloat(d.eth_balance).toFixed(5)+' ETH':'--';
  // Cooldown
  const csec=d.unstake_cooldown_remaining||0;
  const cdSection=document.getElementById('cooldownSection');
  const btnW=document.getElementById('btnWithdraw');
  if(d.withdrawable_at>0){cdSection.style.display='block';document.getElementById('cooldownTimer').textContent=fmtCooldown(csec);document.getElementById('cooldownLabel').textContent=csec>0?'Withdrawal cooldown':'Ready to withdraw!';btnW.disabled=csec>0}else{cdSection.style.display='none';btnW.disabled=true}
  // Pending txs
  const txEl=document.getElementById('txList');const txs=d.pending_transactions||[];
  if(txs.length===0){txEl.innerHTML='<div style="color:var(--muted);font-size:12px;padding:8px 0">No transactions</div>'}
  else{txEl.innerHTML=txs.map(tx=>{
    const cls='tx-item tx-'+tx.status;
    const icon=tx.status==='pending'?'<span class="spinner" style="width:12px;height:12px"></span>':tx.status==='confirmed'?'&#10003;':'&#10007;';
    const validHash=tx.tx_hash&&/^0x[a-fA-F0-9]{64}$/.test(tx.tx_hash);
    const hash=validHash?'<a href="https://basescan.org/tx/'+tx.tx_hash+'" target="_blank" style="font-size:10px;color:var(--dim)">view</a>':'';
    return'<div class="'+cls+'"><div class="tx-icon">'+icon+'</div><div class="tx-desc">'+esc(tx.description)+' '+hash+'</div><div class="tx-time">'+ago(tx.timestamp)+'</div></div>'
  }).join('')}
  // Toast on tx status change
  txs.forEach(tx=>{const prev=prevTxMap[tx.id];if(prev==='pending'&&tx.status==='confirmed')toast(tx.description+' confirmed!',true);if(prev==='pending'&&tx.status==='failed')toast(tx.description+' failed',false)});
  prevTxMap={};txs.forEach(tx=>prevTxMap[tx.id]=tx.status);
}

// Controls — all use CSRF header
document.getElementById('btnStart').addEventListener('click',()=>fetch('/api/control',H('POST',{action:'start'})));
document.getElementById('btnStop').addEventListener('click',()=>fetch('/api/control',H('POST',{action:'stop'})));
document.getElementById('modelSelect').addEventListener('change',e=>fetch('/api/model',H('POST',{model:e.target.value})));

async function dashStake(amt){
  document.getElementById('stakeStatus').innerHTML='<span class="spinner"></span> Staking...';
  const r=await fetch('/api/stake',H('POST',{amount:amt}));
  const d=await r.json();document.getElementById('stakeStatus').innerHTML='<span style="color:'+(d.ok?'var(--green)':'var(--red)')+'">'+esc(d.message)+'</span>';
  if(d.ok)setTimeout(()=>fetch('/api/refresh-staking'),2000);
}
async function dashUnstake(){
  if(!confirm('Unstake all BOTCOIN? Starts a 24h cooldown. You cannot mine during this period.'))return;
  document.getElementById('stakeStatus').innerHTML='<span class="spinner"></span> Unstaking...';
  const r=await fetch('/api/unstake',H('POST'));const d=await r.json();
  document.getElementById('stakeStatus').innerHTML='<span style="color:'+(d.ok?'var(--yellow)':'var(--red)')+'">'+esc(d.message)+'</span>';
}
async function dashWithdraw(){
  document.getElementById('stakeStatus').innerHTML='<span class="spinner"></span> Withdrawing...';
  const r=await fetch('/api/withdraw',H('POST'));const d=await r.json();
  document.getElementById('stakeStatus').innerHTML='<span style="color:'+(d.ok?'var(--green)':'var(--red)')+'">'+esc(d.message)+'</span>';
  if(d.ok)setTimeout(()=>fetch('/api/refresh-staking'),2000);
}
async function doLogout(){if(!confirm('Stop mining and logout?'))return;await fetch('/api/logout',H('POST'));window.location.href='/'}

connectSSE();
fetch('/api/refresh-staking');
</script></body></html>"""


# ---------------------------------------------------------------------------
# Flask UI class — session-aware, multi-tenant
# ---------------------------------------------------------------------------
class MinerUI:
    def __init__(self):
        self._sessions = SessionManager()
        self._mining = MiningManager()
        self._states: dict[str, MinerState] = {}  # session_id -> MinerState
        self._states_lock = threading.Lock()
        self._app = Flask(__name__)
        self._app.logger.disabled = True
        self._on_setup_finish = None
        self._setup_routes()
        self._server_thread = None
        # Fallback state for backward compat (used by main.py)
        self._fallback_state = MinerState()

    @property
    def state(self) -> MinerState:
        """Return the first active state, or fallback for backward compat."""
        with self._states_lock:
            if self._states:
                return next(iter(self._states.values()))
        return self._fallback_state

    def _get_state(self, session_id: str) -> MinerState | None:
        """Get or create a MinerState for a session."""
        with self._states_lock:
            return self._states.get(session_id)

    def _create_state(self, session_id: str) -> MinerState:
        """Create a new MinerState for a session."""
        state = MinerState()
        with self._states_lock:
            self._states[session_id] = state
        return state

    def _remove_state(self, session_id: str):
        with self._states_lock:
            self._states.pop(session_id, None)

    def _get_session_state(self) -> tuple[str | None, MinerState | None]:
        """Get session_id and state from cookie. Returns (None, None) if invalid."""
        session_id = request.cookies.get("session_id")
        if not session_id:
            return None, None
        sess = self._sessions.get_session(session_id)
        if not sess:
            return None, None
        state = self._get_state(session_id)
        return session_id, state

    def _model_options_html(self, current_model=""):
        opts = ""
        for mid, label in AVAILABLE_MODELS:
            sel = " selected" if mid == current_model else ""
            opts += f'<option value="{mid}"{sel}>{label}</option>\n'
        return opts

    def _make_secure_response(self, content, content_type="text/html", status=200):
        """Create a response with security headers."""
        resp = make_response(content, status)
        resp.headers["Content-Type"] = content_type
        return resp

    def _setup_routes(self):
        app = self._app
        sessions = self._sessions
        auth = require_auth(sessions)
        csrf = csrf_protect(sessions)

        # --- Security headers on ALL responses ---
        @app.after_request
        def add_security_headers(response):
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['X-Frame-Options'] = 'DENY'
            response.headers['X-XSS-Protection'] = '1; mode=block'
            response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src https://fonts.gstatic.com; "
                "script-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; "
                "img-src 'self' data:; "
                "frame-ancestors 'none'"
            )
            return response

        # --- Unauthenticated pages ---
        @app.route("/")
        def index():
            session_id, state = self._get_session_state()
            if session_id and state and state.setup_complete:
                return self._serve_dashboard(session_id, state)
            if session_id:
                return self._serve_setup(session_id)
            return LANDING_HTML

        @app.route("/dashboard")
        def dashboard():
            session_id, state = self._get_session_state()
            if not session_id:
                return Response("", status=302, headers={"Location": "/"})
            if not state:
                state = self._create_state(session_id)
            return self._serve_dashboard(session_id, state)

        @app.route("/setup")
        def setup():
            session_id = request.cookies.get("session_id")
            if not session_id:
                # No session yet — serve setup without CSRF (will get CSRF after connect)
                html = SETUP_HTML.replace("MODELOPTIONS", self._model_options_html())
                html = html.replace("CSRFTOKEN", "")
                return html
            return self._serve_setup(session_id)

        # --- SSE (authenticated) ---
        @app.route("/events")
        def events():
            session_id, state = self._get_session_state()
            if not session_id or not state:
                return Response("data: {}\n\n", mimetype="text/event-stream")

            def stream():
                last_v = -1
                while True:
                    v = state.version
                    if v != last_v:
                        last_v = v
                        yield f"data: {json.dumps(state.snapshot())}\n\n"
                    time.sleep(0.5)
            return Response(stream(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        # --- Setup API endpoints ---
        @app.route("/api/setup/connect", methods=["POST"])
        def setup_connect():
            ip = request.remote_addr or "unknown"
            if not _check_rate_limit(f"connect:{ip}", 5, 60):
                return jsonify({"ok": False, "error": "Too many attempts. Try again in a minute."}), 429

            body = request.get_json(silent=True) or {}
            api_key = body.get("api_key", "").strip()
            if not api_key or not validate_api_key(api_key):
                return jsonify({"ok": False, "error": "Invalid API key format (must start with bk_)"})
            try:
                from bankr_client import BankrClient
                bankr = BankrClient(api_key)
                me = bankr.get_me()
                wallets = me.get("wallets", [])
                if not wallets:
                    return jsonify({"ok": False, "error": "No wallets found"})

                # Create session with encrypted API key
                session_id = sessions.create_session(api_key)
                state = self._create_state(session_id)
                csrf_token = sessions.get_csrf_token(session_id)

                resp = jsonify({"ok": True, "csrf_token": csrf_token})
                resp.set_cookie("session_id", session_id,
                                httponly=True, samesite="Strict", max_age=86400,
                                secure=request.is_secure)
                return resp
            except Exception:
                return jsonify({"ok": False, "error": "Connection failed. Check your API key and try again."})

        @app.route("/api/setup/send-otp", methods=["POST"])
        def setup_send_otp():
            body = request.get_json(silent=True) or {}
            email = body.get("email", "").strip()
            if not email or not validate_email(email):
                return jsonify({"ok": False, "error": "Invalid email address"})

            ip = request.remote_addr or "unknown"
            if not _check_rate_limit(f"otp:{ip}", 3, 60):
                return jsonify({"ok": False, "error": "Too many attempts. Try again in a minute."}), 429

            try:
                import subprocess
                result = subprocess.run(["bankr", "login", "email", "--", email],
                    capture_output=True, text=True, timeout=30)
                output = result.stdout + result.stderr
                if result.returncode == 0 or "code" in output.lower() or "sent" in output.lower():
                    return jsonify({"ok": True})
                return jsonify({"ok": False, "error": "Failed to send code. Please try again."})
            except FileNotFoundError:
                try:
                    import httpx
                    resp = httpx.post("https://api.bankr.bot/auth/otp/send",
                                      json={"email": email}, timeout=15)
                    if resp.status_code < 400:
                        return jsonify({"ok": True})
                    return jsonify({"ok": False, "error": "Failed to send code. Please try again."})
                except Exception:
                    return jsonify({"ok": False, "error": "Bankr CLI not installed. Install: npm i -g @bankr/cli"})
            except Exception:
                return jsonify({"ok": False, "error": "Failed to send code. Please try again."})

        @app.route("/api/setup/verify-otp", methods=["POST"])
        def setup_verify_otp():
            body = request.get_json(silent=True) or {}
            email = body.get("email", "").strip()
            code = body.get("code", "").strip()
            if not email or not validate_email(email):
                return jsonify({"ok": False, "error": "Invalid email"})
            if not code or not validate_otp(code):
                return jsonify({"ok": False, "error": "Invalid code format"})

            ip = request.remote_addr or "unknown"
            if not _check_rate_limit(f"verify:{ip}", 5, 60):
                return jsonify({"ok": False, "error": "Too many attempts. Try again in a minute."}), 429

            import subprocess, os
            import re as _re
            try:
                result = subprocess.run(
                    ["bankr", "login", "email", "--", email, "--code", code,
                     "--accept-terms", "--key-name", "BOTCOIN Miner", "--read-write"],
                    capture_output=True, text=True, timeout=120,
                    stdin=subprocess.DEVNULL,
                    env={**os.environ, "CI": "1", "NONINTERACTIVE": "1"})
                output = result.stdout + result.stderr
                api_key = None
                key_match = _re.search(r'(bk_[A-Za-z0-9]+)', output)
                if key_match:
                    api_key = key_match.group(1)
                else:
                    config_path = os.path.expanduser("~/.bankr/config.json")
                    try:
                        with open(config_path) as f:
                            api_key = json.load(f).get("apiKey", "")
                    except Exception:
                        pass

                if api_key:
                    session_id = sessions.create_session(api_key)
                    state = self._create_state(session_id)
                    csrf_token = sessions.get_csrf_token(session_id)
                    resp = jsonify({"ok": True, "csrf_token": csrf_token})
                    resp.set_cookie("session_id", session_id,
                                    httponly=True, samesite="Strict", max_age=86400,
                                    secure=request.is_secure)
                    return resp
                if result.returncode == 0:
                    return jsonify({"ok": False, "error": "Login ok but no key found. Paste from bankr.bot/api."})
                return jsonify({"ok": False, "error": "Verification failed. Check the code and try again."})
            except subprocess.TimeoutExpired:
                config_path = os.path.expanduser("~/.bankr/config.json")
                try:
                    with open(config_path) as f:
                        api_key = json.load(f).get("apiKey", "")
                        if api_key:
                            session_id = sessions.create_session(api_key)
                            self._create_state(session_id)
                            csrf_token = sessions.get_csrf_token(session_id)
                            resp = jsonify({"ok": True, "csrf_token": csrf_token})
                            resp.set_cookie("session_id", session_id,
                                            httponly=True, samesite="Strict", max_age=86400,
                                            secure=request.is_secure)
                            return resp
                except Exception:
                    pass
                return jsonify({"ok": False, "error": "CLI timed out. Run 'bankr login' in terminal, paste key above."})
            except FileNotFoundError:
                return jsonify({"ok": False, "error": "Bankr CLI not installed. Install: npm i -g @bankr/cli"})
            except Exception:
                return jsonify({"ok": False, "error": "Verification failed. Please try again."})

        # --- Authenticated setup endpoints ---
        @app.route("/api/setup/wallet")
        @auth
        def setup_wallet():
            session_id = g.session_id
            api_key = sessions.get_api_key(session_id)
            if not api_key:
                return jsonify({"error": "Session expired"}), 401
            try:
                from bankr_client import BankrClient
                bankr = BankrClient(api_key)
                me = bankr.get_me()
                wallets = me.get("wallets", [])
                address = ""
                for w in wallets:
                    if w.get("chain", "").lower() in ("base", "evm", "ethereum"):
                        address = w.get("address", "")
                        break
                if not address and wallets:
                    address = wallets[0].get("address", "")

                state = self._get_state(session_id) or self._create_state(session_id)
                state.miner_address = address
                sessions.update_miner_address(session_id, address)

                balances = bankr.get_balances("base")
                eth, botcoin = 0.0, 0.0
                tokens = balances if isinstance(balances, list) else balances.get("tokens", balances.get("balances", []))
                if isinstance(tokens, list):
                    for t in tokens:
                        sym = t.get("symbol", "").upper()
                        if sym == "ETH":
                            eth = float(t.get("balance", 0))
                        if sym == "BOTCOIN" or t.get("address", "").lower() == "0xa601877977340862ca67f816eb079958e5bd0ba3":
                            botcoin = float(t.get("balance", 0))
                state.eth_balance = eth
                state.botcoin_balance = botcoin
                state.bump()
                return jsonify({"address": address, "eth": eth, "botcoin": botcoin})
            except Exception:
                return jsonify({"error": "Failed to load wallet. Please try again."})

        @app.route("/api/setup/check-stake")
        @auth
        def setup_check_stake():
            session_id = g.session_id
            state = self._get_state(session_id)
            miner = state.miner_address if state else ""
            if not miner:
                return jsonify({"staked": 0, "eligible": False})
            try:
                from coordinator_client import CoordinatorClient
                coord = CoordinatorClient(miner)
                staked = coord.get_staked_amount(miner)
                eligible = coord.is_eligible(miner)
                if state:
                    state.staked_amount = staked if staked >= 0 else 0
                    state.bump()
                return jsonify({"staked": staked, "eligible": eligible})
            except Exception:
                return jsonify({"staked": -1, "eligible": False})

        @app.route("/api/setup/stake", methods=["POST"])
        @auth
        @csrf
        def setup_stake():
            session_id = g.session_id
            api_key = sessions.get_api_key(session_id)
            state = self._get_state(session_id)
            miner = state.miner_address if state else ""
            if not api_key or not miner:
                return jsonify({"ok": False, "message": "Not connected"})
            try:
                from bankr_client import BankrClient
                from coordinator_client import CoordinatorClient
                from config import MIN_STAKE_WEI
                body = request.get_json(silent=True) or {}
                amount = body.get("amount", "") or MIN_STAKE_WEI
                if amount not in STAKE_AMOUNTS.values() and amount != MIN_STAKE_WEI:
                    return jsonify({"ok": False, "message": "Invalid stake amount"})
                bankr = BankrClient(api_key)
                coord = CoordinatorClient(miner)
                approve = coord.get_stake_approve_calldata(amount)
                if "transaction" in approve:
                    bankr.submit_transaction(approve["transaction"], "Approve BOTCOIN for staking")
                    time.sleep(2)
                stake = coord.get_stake_calldata(amount)
                if "transaction" in stake:
                    bankr.submit_transaction(stake["transaction"], "Stake BOTCOIN for mining")
                return jsonify({"ok": True, "message": "Staked successfully!"})
            except Exception as e:
                err_lower = str(e).lower()
                if "already" in err_lower or "nothing" in err_lower:
                    return jsonify({"ok": True, "already": True, "message": "Already staked!"})
                return jsonify({"ok": False, "message": "Staking failed. Check your BOTCOIN balance and try again."})

        @app.route("/api/setup/llm-credits")
        @auth
        def setup_llm_credits():
            try:
                import subprocess
                result = subprocess.run(["bankr", "llm", "credits"],
                    capture_output=True, text=True, timeout=15)
                m = re.search(r'\$?([\d.]+)', result.stdout + result.stderr)
                if m:
                    return jsonify({"balance": float(m.group(1))})
            except Exception:
                pass
            return jsonify({"balance": -1})

        @app.route("/api/setup/finish", methods=["POST"])
        @auth
        @csrf
        def setup_finish():
            session_id = g.session_id
            body = request.get_json(silent=True) or {}
            model = body.get("model", "claude-sonnet-4-6")
            auto_topup = body.get("auto_topup", False)

            if model not in VALID_MODELS:
                return jsonify({"ok": False, "error": "Invalid model"})

            state = self._get_state(session_id) or self._create_state(session_id)
            state.model = model
            state.auto_topup = auto_topup
            state.setup_complete = True
            state.bump()

            api_key = sessions.get_api_key(session_id)
            if api_key and self._on_setup_finish:
                self._on_setup_finish(session_id, api_key, model, state, auto_topup)
            return jsonify({"ok": True})

        # --- Dashboard API (all authenticated + CSRF) ---
        @app.route("/api/control", methods=["POST"])
        @auth
        @csrf
        def control():
            session_id = g.session_id
            state = self._get_state(session_id)
            if not state:
                return jsonify({"ok": False, "error": "No session"}), 401
            body = request.get_json(silent=True) or {}
            action = body.get("action")
            if action == "start":
                state.mining_active = True
                state.bump()
                state.log("Mining resumed")
            elif action == "stop":
                state.mining_active = False
                state.bump()
                state.log("Mining paused")
            return jsonify({"ok": True})

        @app.route("/api/model", methods=["POST"])
        @auth
        @csrf
        def change_model():
            session_id = g.session_id
            state = self._get_state(session_id)
            if not state:
                return jsonify({"ok": False}), 401
            body = request.get_json(silent=True) or {}
            new_model = body.get("model", "")
            if new_model and new_model in VALID_MODELS:
                state.model = new_model
                state.bump()
                state.log(f"Model → {new_model}")
                cb = self._mining.get_model_callback(session_id)
                if cb:
                    cb(new_model)
            return jsonify({"ok": True, "model": new_model})

        @app.route("/api/stake", methods=["POST"])
        @auth
        @csrf
        def dashboard_stake():
            session_id = g.session_id
            api_key = sessions.get_api_key(session_id)
            state = self._get_state(session_id)
            miner = state.miner_address if state else ""
            if not api_key or not miner:
                return jsonify({"ok": False, "message": "Not connected"})
            body = request.get_json(silent=True) or {}
            amount = body.get("amount", "")
            if not amount or amount not in STAKE_AMOUNTS.values():
                return jsonify({"ok": False, "message": "Invalid amount"})
            tx_id = state.add_pending_tx("Staking BOTCOIN")
            try:
                from bankr_client import BankrClient
                from coordinator_client import CoordinatorClient
                bankr = BankrClient(api_key)
                coord = CoordinatorClient(miner)
                approve = coord.get_stake_approve_calldata(amount)
                if "transaction" in approve:
                    bankr.submit_transaction(approve["transaction"], "Approve BOTCOIN")
                    time.sleep(2)
                stake_data = coord.get_stake_calldata(amount)
                if "transaction" in stake_data:
                    result = bankr.submit_transaction(stake_data["transaction"], "Stake BOTCOIN")
                    state.update_pending_tx(tx_id, "confirmed", result.get("transactionHash", ""))
                else:
                    state.update_pending_tx(tx_id, "confirmed")
                state.log("Staked BOTCOIN!")
                return jsonify({"ok": True, "message": "Staked!"})
            except Exception:
                state.update_pending_tx(tx_id, "failed")
                return jsonify({"ok": False, "message": "Staking failed. Check your BOTCOIN balance and ETH for gas."})

        @app.route("/api/unstake", methods=["POST"])
        @auth
        @csrf
        def dashboard_unstake():
            session_id = g.session_id
            api_key = sessions.get_api_key(session_id)
            state = self._get_state(session_id)
            miner = state.miner_address if state else ""
            if not api_key or not miner:
                return jsonify({"ok": False, "message": "Not connected"})
            tx_id = state.add_pending_tx("Unstaking BOTCOIN")
            try:
                from bankr_client import BankrClient
                from coordinator_client import CoordinatorClient
                bankr = BankrClient(api_key)
                coord = CoordinatorClient(miner)
                unstake = coord.get_unstake_calldata()
                if "transaction" in unstake:
                    result = bankr.submit_transaction(unstake["transaction"], "Unstake BOTCOIN")
                    state.update_pending_tx(tx_id, "confirmed", result.get("transactionHash", ""))
                state.unstake_requested_at = time.time()
                wa = coord.get_withdrawable_at(miner)
                state.withdrawable_at = wa if wa > 0 else time.time() + 86400
                state.bump()
                state.log("Unstake requested — 24h cooldown")
                return jsonify({"ok": True, "message": "Unstaking! 24h cooldown started."})
            except Exception:
                state.update_pending_tx(tx_id, "failed")
                return jsonify({"ok": False, "message": "Unstaking failed. Please try again."})

        @app.route("/api/withdraw", methods=["POST"])
        @auth
        @csrf
        def dashboard_withdraw():
            session_id = g.session_id
            api_key = sessions.get_api_key(session_id)
            state = self._get_state(session_id)
            miner = state.miner_address if state else ""
            if not api_key or not miner:
                return jsonify({"ok": False, "message": "Not connected"})
            tx_id = state.add_pending_tx("Withdrawing BOTCOIN")
            try:
                from bankr_client import BankrClient
                from coordinator_client import CoordinatorClient
                bankr = BankrClient(api_key)
                coord = CoordinatorClient(miner)
                withdraw = coord.get_withdraw_calldata()
                if "transaction" in withdraw:
                    result = bankr.submit_transaction(withdraw["transaction"], "Withdraw BOTCOIN")
                    state.update_pending_tx(tx_id, "confirmed", result.get("transactionHash", ""))
                state.unstake_requested_at = 0
                state.withdrawable_at = 0
                state.bump()
                state.log("BOTCOIN withdrawn!")
                return jsonify({"ok": True, "message": "Withdrawn to wallet!"})
            except Exception:
                state.update_pending_tx(tx_id, "failed")
                return jsonify({"ok": False, "message": "Withdrawal failed. Cooldown may not have elapsed."})

        @app.route("/api/refresh-staking")
        @auth
        def refresh_staking():
            session_id = g.session_id
            state = self._get_state(session_id)
            miner = state.miner_address if state else ""
            if not miner:
                return jsonify({"ok": False})
            try:
                from coordinator_client import CoordinatorClient
                coord = CoordinatorClient(miner)
                staked = coord.get_staked_amount(miner)
                wa = coord.get_withdrawable_at(miner)
                if staked >= 0:
                    state.staked_amount = staked
                    state.staking_tier = 3 if staked >= 100e6 else 2 if staked >= 50e6 else 1 if staked >= 25e6 else 0
                if wa > 0:
                    state.withdrawable_at = wa
                # Also refresh wallet balance
                api_key = sessions.get_api_key(session_id)
                if api_key:
                    from bankr_client import BankrClient
                    bankr = BankrClient(api_key)
                    balances = bankr.get_balances("base")
                    tokens = balances if isinstance(balances, list) else balances.get("tokens", balances.get("balances", []))
                    if isinstance(tokens, list):
                        for t in tokens:
                            sym = t.get("symbol", "").upper()
                            if sym == "ETH":
                                state.eth_balance = float(t.get("balance", 0))
                            if sym == "BOTCOIN" or t.get("address", "").lower() == "0xa601877977340862ca67f816eb079958e5bd0ba3":
                                state.wallet_botcoin = float(t.get("balance", 0))
                state.bump()
                return jsonify({"ok": True, "staked": staked})
            except Exception:
                return jsonify({"ok": False, "error": "Failed to refresh staking info."})

        @app.route("/api/logout", methods=["POST"])
        @auth
        @csrf
        def logout():
            session_id = g.session_id
            if session_id:
                # Stop mining for this session
                self._mining.remove_session(session_id)
                self._remove_state(session_id)
                sessions.destroy_session(session_id)
            resp = jsonify({"ok": True})
            resp.delete_cookie("session_id")
            return resp

        @app.route("/api/state")
        @auth
        def get_state():
            session_id = g.session_id
            state = self._get_state(session_id)
            if not state:
                return jsonify({})
            return jsonify(state.snapshot())

    def _serve_setup(self, session_id: str):
        csrf_token = self._sessions.get_csrf_token(session_id) or ""
        state = self._get_state(session_id)
        model = state.model if state else ""
        html = SETUP_HTML.replace("MODELOPTIONS", self._model_options_html(model))
        html = html.replace("CSRFTOKEN", csrf_token)
        return html

    def _serve_dashboard(self, session_id: str, state: MinerState):
        csrf_token = self._sessions.get_csrf_token(session_id) or ""
        html = DASHBOARD_HTML
        html = html.replace("MODELOPTIONS", self._model_options_html(state.model))
        html = html.replace("PHASECOLORSJS", json.dumps(PHASE_COLORS))
        html = html.replace("CSRFTOKEN", csrf_token)
        return html

    # -- public API --
    def start(self, port=5157, open_browser=True):
        import logging
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        def run():
            self._app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)
        self._server_thread = threading.Thread(target=run, daemon=True)
        self._server_thread.start()
        if open_browser:
            def _open():
                time.sleep(1.0)
                webbrowser.open(f"http://localhost:{port}")
            threading.Thread(target=_open, daemon=True).start()

    def stop(self):
        pass

    def update(self):
        self.state.bump()

    def log(self, msg: str):
        clean = re.sub(r'\[/?[^\]]*\]', '', msg)
        self.state.log(clean)

    def set_phase(self, phase: str):
        self.state.phase = phase
        self.state.bump()

    def print_banner(self):
        print("\n  BOTCOIN MINER — Plug & Play Mining Agent\n")
