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
    require_auth, csrf_protect, validate_email,
    validate_otp, sanitize_log
)

PHASE_COLORS = {
    "INIT": "#555", "LOADING": "#d4a017", "SETUP": "#d4a017", "AUTHENTICATING": "#d4a017",
    "REQUESTING": "#00d4ff", "SOLVING": "#7b2fff", "VERIFYING": "#00d4ff",
    "SUBMITTING": "#d4a017", "POSTING_RECEIPT": "#00e676", "COOLDOWN": "#4a5568",
    "PAUSED": "#ff4757", "SUCCESS": "#00e676", "FAILED": "#ff4757",
}

VALID_MODELS = {mid for mid, _ in AVAILABLE_MODELS}

# Rate limiting (in-memory, per IP)
_rate_limits: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def _parse_bankr_balances(balances: dict, chain: str = "base") -> tuple[float, list]:
    """Parse Bankr /agent/balances response. Returns (native_balance, token_list).

    Bankr format:
    {
      "balances": {
        "base": {
          "nativeBalance": "0.01...",
          "nativeUsd": "20.23",
          "tokenBalances": [{"symbol": "BOTCOIN", "balance": "123", "address": "0x..."}]
        }
      }
    }
    """
    native = 0.0
    tokens = []
    if isinstance(balances, dict):
        chain_data = None
        bal_root = balances.get("balances", {})
        if isinstance(bal_root, dict):
            chain_data = bal_root.get(chain, {})
        if isinstance(chain_data, dict):
            native = float(chain_data.get("nativeBalance", 0) or 0)
            tokens = chain_data.get("tokenBalances", [])
            if not isinstance(tokens, list):
                tokens = []
        else:
            # Fallback: try flat token list formats
            for key in ("tokens", "balances", "data", "result"):
                val = balances.get(key)
                if isinstance(val, list):
                    tokens = val
                    break
    elif isinstance(balances, list):
        tokens = balances
    return native, tokens


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
# Legal pages
# ---------------------------------------------------------------------------
LEGAL_CSS = """
.legal{max-width:720px;margin:0 auto;padding:60px 24px 80px}
.legal h1{font-size:28px;font-weight:700;margin-bottom:8px}
.legal .updated{font-size:12px;color:var(--dim);margin-bottom:32px}
.legal h2{font-size:17px;font-weight:700;margin:28px 0 10px;color:var(--accent)}
.legal p,.legal li{font-size:14px;color:var(--dim);line-height:1.8;margin-bottom:10px}
.legal ul,.legal ol{padding-left:24px;margin-bottom:16px}
.legal a{color:var(--accent)}
.legal .back{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--dim);margin-bottom:24px}
.legal .back:hover{color:var(--accent)}
@media(max-width:480px){.legal{padding:30px 16px 50px}.legal h1{font-size:22px}.legal h2{font-size:15px}}
"""

TERMS_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terms of Service — BOTCOIN Miner</title>
<link rel="icon" type="image/x-icon" href="/static/favicon.ico"><link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png"><link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<style>
""" + SHARED_CSS + LEGAL_CSS + """
</style></head><body>
<div class="legal">
<a href="/" class="back">&larr; Back</a>
<h1>Terms of Service</h1>
<p class="updated">Last updated: March 10, 2026</p>

<h2>1. Parties and Acceptance</h2>
<p>These Terms of Service ("Terms") constitute a legally binding agreement between you ("User," "you") and <strong>Question Labs LLC</strong> ("Company," "we," "us"), the operator of the BOTCOIN Miner software and related services (the "Service"). The Service integrates with <strong>Bankr Bot</strong> ("Service Provider"), a third-party platform for wallet management, transaction execution, and LLM gateway access. By accessing or using the Service, you acknowledge that you have read, understood, and agree to be bound by these Terms in their entirety. If you do not agree, you must not use the Service.</p>

<h2>2. Nature of the Service</h2>
<p>The Service is experimental software that automates participation in BOTCOIN mining challenges using large language models (LLMs). The Service interacts with blockchain smart contracts on the Base network (chain ID 8453) and third-party APIs. The Service is provided on an <strong>"AS IS" and "AS AVAILABLE"</strong> basis without warranties of any kind, whether express, implied, statutory, or otherwise, including but not limited to warranties of merchantability, fitness for a particular purpose, non-infringement, accuracy, completeness, reliability, or uninterrupted availability.</p>

<h2>3. Eligibility</h2>
<p>You must be at least 18 years of age and have the legal capacity to enter into binding agreements in your jurisdiction. You are solely responsible for ensuring that your use of the Service complies with all applicable laws, regulations, and ordinances in your jurisdiction, including but not limited to securities laws, tax laws, money transmission laws, and cryptocurrency regulations. The Service is not available in jurisdictions where cryptocurrency trading, staking, or mining is prohibited.</p>

<h2>4. Account and API Keys</h2>
<p>You are solely responsible for maintaining the confidentiality and security of your Bankr API key, email credentials, and any other authentication credentials. You agree not to share your credentials with any third party. You are fully responsible for all activities that occur under your account, whether or not authorized by you. We are not liable for any loss or damage arising from unauthorized access to your account.</p>

<h2>5. Assumption of Risk</h2>
<p>You expressly acknowledge and assume all risks associated with using the Service, including but not limited to:</p>
<ul>
<li><strong>Financial loss:</strong> Loss of cryptocurrency (including but not limited to BOTCOIN, ETH, and any other tokens) due to smart contract bugs, blockchain network failures, oracle manipulation, front-running, MEV extraction, bridge exploits, liquidity issues, token depegging, market volatility, or any other cause whatsoever.</li>
<li><strong>Software defects:</strong> Bugs, errors, vulnerabilities, logic flaws, race conditions, or other defects in the Service, smart contracts, third-party integrations, or underlying infrastructure that may result in loss of funds, incorrect transactions, failed transactions, double spending, or other unintended outcomes.</li>
<li><strong>LLM failures:</strong> Incorrect, incomplete, or failed LLM responses that result in failed mining challenges, wasted LLM credits, wasted gas fees, or other losses.</li>
<li><strong>Third-party failures:</strong> Downtime, errors, security breaches, insolvency, or changes to Bankr Bot, LLM providers (including but not limited to Anthropic, OpenAI, Google), blockchain networks, DEXs, bridges, or any other third-party service the Service depends on.</li>
<li><strong>Smart contract risk:</strong> The mining contract, staking contract, and token contract are third-party code not authored or audited by Question Labs LLC. Smart contracts are immutable once deployed and may contain undiscovered vulnerabilities.</li>
<li><strong>Regulatory risk:</strong> Changes in laws, regulations, or enforcement actions that may affect the legality, availability, or value of BOTCOIN, the Service, or cryptocurrency in general.</li>
<li><strong>Network risk:</strong> Blockchain congestion, reorganizations, forks, gas price spikes, or network outages.</li>
<li><strong>Key management risk:</strong> Loss of access to your Bankr wallet or API key, whether due to Bankr service disruption, account compromise, or any other cause.</li>
</ul>

<h2>6. Limitation of Liability</h2>
<p><strong>TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, IN NO EVENT SHALL QUESTION LABS LLC, ITS OFFICERS, DIRECTORS, EMPLOYEES, AGENTS, AFFILIATES, SUCCESSORS, OR ASSIGNS BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, PUNITIVE, OR EXEMPLARY DAMAGES, OR DAMAGES FOR LOSS OF PROFITS, REVENUE, GOODWILL, USE, DATA, TOKENS, CRYPTOCURRENCY, OR OTHER INTANGIBLE LOSSES (EVEN IF QUESTION LABS LLC HAS BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGES), ARISING OUT OF OR IN CONNECTION WITH YOUR USE OF OR INABILITY TO USE THE SERVICE.</strong></p>
<p><strong>QUESTION LABS LLC'S TOTAL AGGREGATE LIABILITY TO YOU FOR ALL CLAIMS ARISING OUT OF OR RELATING TO THE SERVICE SHALL NOT EXCEED THE GREATER OF (A) THE AMOUNT YOU PAID TO QUESTION LABS LLC IN THE TWELVE (12) MONTHS PRECEDING THE CLAIM, OR (B) ONE HUNDRED US DOLLARS ($100.00).</strong></p>
<p><strong>YOU ACKNOWLEDGE THAT THE SERVICE IS PROVIDED FREE OF CHARGE AND THAT THE LIMITATIONS OF LIABILITY SET FORTH HEREIN ARE A FUNDAMENTAL ELEMENT OF THE AGREEMENT BETWEEN YOU AND QUESTION LABS LLC.</strong></p>

<h2>7. No Financial or Investment Advice</h2>
<p>Nothing in the Service constitutes financial, investment, tax, legal, or other professional advice. BOTCOIN tokens may have no value. Past mining performance is not indicative of future results. You should consult qualified professionals before making any financial decisions. We make no representations regarding the value, utility, or future prospects of BOTCOIN or any other cryptocurrency.</p>

<h2>8. Indemnification</h2>
<p>You agree to indemnify, defend, and hold harmless Question Labs LLC, its officers, directors, employees, agents, and affiliates from and against any and all claims, damages, losses, liabilities, costs, and expenses (including reasonable attorneys' fees) arising out of or in connection with: (a) your use of the Service; (b) your violation of these Terms; (c) your violation of any applicable law or regulation; or (d) your violation of any third party's rights.</p>

<h2>9. Service Availability</h2>
<p>We do not guarantee that the Service will be available at all times or without interruption. The Service may experience downtime, maintenance periods, bugs, errors, or complete unavailability without notice. We reserve the right to modify, suspend, or discontinue the Service (or any part thereof) at any time, temporarily or permanently, with or without notice, and without liability to you.</p>

<h2>10. Third-Party Services</h2>
<p>The Service integrates with Bankr Bot and other third-party services. Your use of these third-party services is governed by their respective terms of service and privacy policies. Question Labs LLC is not responsible for the availability, accuracy, security, or content of any third-party service. We do not endorse and are not responsible or liable for any third-party services.</p>

<h2>11. Intellectual Property</h2>
<p>The Service and its original content, features, and functionality are owned by Question Labs LLC and are protected by copyright, trademark, and other intellectual property laws. You are granted a limited, non-exclusive, non-transferable, revocable license to use the Service for personal, non-commercial purposes in accordance with these Terms.</p>

<h2>12. Termination</h2>
<p>We may terminate or suspend your access to the Service immediately, without prior notice or liability, for any reason, including if you breach these Terms. Upon termination, your right to use the Service ceases immediately. All provisions of these Terms that by their nature should survive termination shall survive.</p>

<h2>13. Governing Law and Dispute Resolution</h2>
<p>These Terms shall be governed by and construed in accordance with the laws of the State of Delaware, United States, without regard to its conflict of law provisions. Any dispute arising out of or relating to these Terms or the Service shall be resolved exclusively in the state or federal courts located in Delaware, and you consent to personal jurisdiction in such courts.</p>

<h2>14. Severability</h2>
<p>If any provision of these Terms is held to be invalid, illegal, or unenforceable, the remaining provisions shall continue in full force and effect.</p>

<h2>15. Entire Agreement</h2>
<p>These Terms, together with the Privacy Policy, constitute the entire agreement between you and Question Labs LLC regarding the Service and supersede all prior agreements and understandings.</p>

<h2>16. Changes to Terms</h2>
<p>We reserve the right to modify these Terms at any time. Changes become effective upon posting. Your continued use of the Service after changes are posted constitutes acceptance of the modified Terms. It is your responsibility to review these Terms periodically.</p>

<h2>17. Contact</h2>
<p>For questions about these Terms, contact Question Labs LLC at the information provided on <a href="https://agentmoney.net/" target="_blank">agentmoney.net</a>.</p>
</div>
</body></html>"""

PRIVACY_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy Policy — BOTCOIN Miner</title>
<link rel="icon" type="image/x-icon" href="/static/favicon.ico"><link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png"><link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<style>
""" + SHARED_CSS + LEGAL_CSS + """
</style></head><body>
<div class="legal">
<a href="/" class="back">&larr; Back</a>
<h1>Privacy Policy</h1>
<p class="updated">Last updated: March 10, 2026</p>

<h2>1. Introduction</h2>
<p>This Privacy Policy describes how <strong>Question Labs LLC</strong> ("Company," "we," "us") collects, uses, and protects information in connection with the BOTCOIN Miner software and related services (the "Service"). The Service uses <strong>Bankr Bot</strong> as a third-party service provider for wallet management, authentication, and LLM gateway access.</p>

<h2>2. Information We Collect</h2>
<p><strong>Information you provide:</strong></p>
<ul>
<li><strong>Email address:</strong> Used solely for authentication via Bankr Bot's OTP (one-time password) system. We pass your email to Bankr Bot to initiate the login flow.</li>
<li><strong>API key:</strong> Your Bankr API key is encrypted in memory using AES-128-CBC with HMAC-SHA256 (Fernet encryption) and stored only in server-side sessions. The encryption key is ephemeral (regenerated on each server restart). API keys are never written to disk, logged, or exposed in API responses.</li>
</ul>
<p><strong>Information collected automatically:</strong></p>
<ul>
<li><strong>IP address:</strong> Used solely for rate limiting on authentication endpoints. Not stored persistently.</li>
<li><strong>Blockchain addresses:</strong> Your public wallet address on Base, as resolved from your Bankr account. This is a public blockchain address and is used to interact with mining contracts.</li>
<li><strong>Mining activity:</strong> Challenge results (pass/fail counts, credits earned) are stored in server memory during your session for dashboard display. This data is not persisted to disk and is lost on server restart.</li>
</ul>
<p><strong>Information we do NOT collect:</strong></p>
<ul>
<li>We do not use cookies for tracking or advertising.</li>
<li>We do not use analytics services, tracking pixels, or advertising networks.</li>
<li>We do not collect browser fingerprints or device identifiers.</li>
<li>We do not sell, rent, or share your personal information with third parties for marketing purposes.</li>
</ul>

<h2>3. How We Use Information</h2>
<p>We use the information we collect solely for the following purposes:</p>
<ul>
<li>Authenticating your identity via Bankr Bot</li>
<li>Managing your encrypted session</li>
<li>Executing mining operations on your behalf (requesting challenges, submitting solutions, posting receipts)</li>
<li>Displaying mining statistics and wallet balances on your dashboard</li>
<li>Rate limiting to prevent abuse</li>
</ul>

<h2>4. Data Storage and Security</h2>
<ul>
<li><strong>Sessions:</strong> All session data (including encrypted API keys) is stored in server memory only. Sessions expire after 24 hours and are limited to 50 concurrent sessions. All sessions are invalidated on server restart.</li>
<li><strong>Encryption:</strong> API keys are encrypted at rest using Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256) with an ephemeral key generated at server startup.</li>
<li><strong>Cookies:</strong> We use a single HttpOnly, SameSite=Strict session cookie for authentication. No tracking cookies are used.</li>
<li><strong>CSRF protection:</strong> All state-changing operations are protected with CSRF tokens.</li>
<li><strong>No persistent storage:</strong> We do not operate a database. No user data is written to disk or persisted beyond the server process lifetime.</li>
</ul>

<h2>5. Third-Party Services</h2>
<p>The Service integrates with the following third-party services, each governed by their own privacy policies:</p>
<ul>
<li><strong>Bankr Bot</strong> (<a href="https://bankr.bot/" target="_blank">bankr.bot</a>) — Wallet management, authentication, transaction execution, LLM gateway. Your email and API key are processed by Bankr Bot.</li>
<li><strong>LLM providers</strong> (Anthropic, OpenAI, Google, etc. via Bankr LLM Gateway) — Mining challenge text is sent to LLM providers for solving. These providers have their own data handling policies.</li>
<li><strong>Base blockchain</strong> — Transaction data, wallet addresses, and smart contract interactions are recorded on the public Base blockchain permanently.</li>
<li><strong>Google Fonts</strong> — Font files are loaded from Google's CDN. Google may collect standard web request data (IP address, user agent).</li>
</ul>

<h2>6. Blockchain Data</h2>
<p>Blockchain transactions are public and immutable. Once a transaction is posted to the Base network (including mining receipts, staking, and claims), it becomes permanently and publicly visible. Question Labs LLC has no ability to delete, modify, or restrict access to blockchain data.</p>

<h2>7. Data Retention</h2>
<p>Session data is retained in memory for a maximum of 24 hours or until server restart, whichever comes first. We do not maintain any persistent database of user information. Rate limiting counters are stored in memory and reset on server restart.</p>

<h2>8. Your Rights</h2>
<p>Since we do not persistently store personal data, most data subject rights (access, correction, deletion) are satisfied by the ephemeral nature of our data handling. You can terminate your session at any time by logging out, which destroys your encrypted session data immediately. If you have questions about your data, contact us at the information provided on <a href="https://agentmoney.net/" target="_blank">agentmoney.net</a>.</p>

<h2>9. Children's Privacy</h2>
<p>The Service is not directed to individuals under the age of 18. We do not knowingly collect personal information from children. If we learn that we have collected personal information from a child under 18, we will take steps to delete that information promptly.</p>

<h2>10. International Users</h2>
<p>The Service is operated from the United States. If you access the Service from outside the United States, you understand and consent to the transfer and processing of your information in the United States, which may have different data protection laws than your jurisdiction.</p>

<h2>11. Changes to This Policy</h2>
<p>We may update this Privacy Policy from time to time. Changes become effective upon posting. Your continued use of the Service after changes are posted constitutes acceptance of the updated Privacy Policy.</p>

<h2>12. Contact</h2>
<p>For questions about this Privacy Policy, contact Question Labs LLC at the information provided on <a href="https://agentmoney.net/" target="_blank">agentmoney.net</a>.</p>
</div>
</body></html>"""

# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------
LANDING_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOTCOIN Miner</title>
<link rel="icon" type="image/x-icon" href="/static/favicon.ico"><link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png"><link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
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
@media(max-width:480px){.hero{padding:60px 16px 40px}.hero h1{font-size:36px;letter-spacing:-1px}.hero p{font-size:15px}.hero .cta{flex-direction:column;align-items:center}.features{padding:10px 16px 40px}}
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
  <div style="padding:20px 24px 40px;font-size:12px;color:var(--muted);text-align:center;width:100%">
    &copy; 2026 Question Labs LLC &mdash;
    <a href="/terms" style="color:var(--dim)">Terms of Service</a> &middot;
    <a href="/privacy" style="color:var(--dim)">Privacy Policy</a>
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
<link rel="icon" type="image/x-icon" href="/static/favicon.ico"><link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png"><link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
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
.login-card{background:var(--bg-elevated);border:1px solid var(--border-bright);border-radius:var(--radius);padding:20px;position:relative;overflow:hidden}
.login-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--gradient);opacity:.6}
.login-field{margin-bottom:4px}
.input-group{display:flex;align-items:center;gap:8px;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);padding:4px 4px 4px 12px;transition:border-color .25s,box-shadow .25s}
.input-group:focus-within{border-color:rgba(0,212,255,.35);box-shadow:0 0 0 3px rgba(0,212,255,.06)}
.input-group .input-icon{color:var(--muted);font-size:15px;flex-shrink:0;width:18px;text-align:center;line-height:1}
.input-group input{border:none!important;background:none!important;padding:8px 4px!important;flex:1;min-width:0}
.input-group input:focus{box-shadow:none!important}
.input-group .btn{flex-shrink:0;margin:0}
.otp-reveal{max-height:0;overflow:hidden;opacity:0;transition:max-height .4s cubic-bezier(.4,0,.2,1),opacity .35s ease,margin .3s ease;margin-top:0}
.otp-reveal.show{max-height:120px;opacity:1;margin-top:14px}
.login-legal{font-size:11px;color:var(--dim);text-align:center;margin-top:16px;padding-top:14px;border-top:1px solid var(--border)}.login-legal a{color:var(--accent);opacity:.8;text-decoration:underline;text-decoration-style:dotted;text-underline-offset:2px}.login-legal a:hover{opacity:1}
.tier-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}
.tier-card{padding:12px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg);text-align:center;cursor:pointer;transition:all .2s}
.tier-card:hover{border-color:var(--accent);background:rgba(0,212,255,0.04)}
.tier-card .amt{font-size:18px;font-weight:700}.tier-card .cr{font-size:11px;color:var(--dim);margin-top:2px}
.t1 .amt{color:var(--accent)}.t2 .amt{color:var(--green)}.t3 .amt{color:var(--accent2)}
@media(max-width:480px){.container{padding:24px 14px 40px}.tier-cards{grid-template-columns:1fr}.row{flex-direction:column;gap:6px}.row input,.row button{width:100%}}
</style></head><body>
<div class="container">
<div class="logo"><span class="grad-text">BOTCOIN</span> MINER</div>
<p class="subtitle">Botcoin Setup Wizard</p>
<div class="progress" id="progress"></div>

<!-- Step 1: Connect -->
<div class="step active" id="step1">
  <div class="step-header"><span class="step-num">1</span><span class="step-title">Connect to Bankr</span></div>
  <p class="step-desc">Paste your API key to connect. Get one at <a href="https://bankr.bot/api" target="_blank">bankr.bot/api</a><br><a href="https://bankr.bot/" target="_blank">What is Bankr?</a></p>
  <div class="step-body">
    <div class="login-card">
      <div>
        <label>API Key</label>
        <div class="input-group">
          <div class="input-icon" style="font-size:14px">&#128273;</div>
          <input type="password" id="apiKeyInput" placeholder="bk_..." style="font-family:var(--mono);font-size:13px">
          <button class="btn btn-green btn-sm" onclick="connectApiKey()">Connect</button>
        </div>
        <div style="font-size:11px;color:var(--dim);margin-top:6px">Enable <strong>Agent API</strong> &amp; <strong>LLM Gateway</strong>, turn off <strong>Read-Only</strong></div>
      </div>
      <div style="text-align:center;margin-top:14px"><a href="#" onclick="toggleEmailLogin();return false" style="color:var(--dim);font-size:12px;text-decoration:underline dotted;text-underline-offset:3px" id="emailToggle">New user? Sign up with email</a></div>
      <div id="emailSection" class="otp-reveal">
        <label>Email Address</label>
        <div class="input-group">
          <div class="input-icon">&#9993;</div>
          <input type="email" id="emailInput" placeholder="you@example.com">
          <button class="btn btn-accent btn-sm" id="btnSendOtp" onclick="sendOtp()">Send Code</button>
        </div>
        <div id="otpSection" class="otp-reveal">
          <label style="margin-top:10px">Verification Code</label>
          <div class="input-group">
            <div class="input-icon" style="font-size:16px">&#128274;</div>
            <input type="text" id="otpInput" placeholder="123456" maxlength="8" style="font-family:var(--mono);letter-spacing:6px;font-size:18px">
            <button class="btn btn-green btn-sm" onclick="verifyOtp()">Verify</button>
          </div>
        </div>
      </div>
      <div class="login-legal">By continuing you accept our <a href="/terms" target="_blank">Terms</a> &amp; <a href="/privacy" target="_blank">Privacy Policy</a></div>
    </div>
    <div id="step1Status"></div>
  </div>
</div>

<!-- Step 2: Configure Bankr Account -->
<div class="step" id="step2">
  <div class="step-header"><span class="step-num">2</span><span class="step-title">Configure Bankr Account</span></div>
  <p class="step-desc">First-time users: enable these settings at <a href="https://bankr.bot/api" target="_blank">bankr.bot/api</a> so mining works correctly.</p>
  <div class="step-body">
    <div style="display:flex;flex-direction:column;gap:12px">
      <div class="checklist-item" style="display:flex;align-items:flex-start;gap:10px;padding:12px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg)">
        <input type="checkbox" id="chkAgentApi" style="width:18px;height:18px;accent-color:var(--green);margin-top:2px;flex-shrink:0">
        <div><div style="font-size:14px;font-weight:600">Enable Agent API</div><div style="font-size:12px;color:var(--dim);margin-top:2px">Required for wallet operations, signing, and submitting transactions.</div></div>
      </div>
      <div class="checklist-item" style="display:flex;align-items:flex-start;gap:10px;padding:12px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg)">
        <input type="checkbox" id="chkReadWrite" style="width:18px;height:18px;accent-color:var(--green);margin-top:2px;flex-shrink:0">
        <div><div style="font-size:14px;font-weight:600">Turn OFF Read-Only Mode</div><div style="font-size:12px;color:var(--dim);margin-top:2px">Mining requires submitting transactions. Read-only keys will be rejected.</div></div>
      </div>
      <div class="checklist-item" style="display:flex;align-items:flex-start;gap:10px;padding:12px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg)">
        <input type="checkbox" id="chkLlmGateway" style="width:18px;height:18px;accent-color:var(--green);margin-top:2px;flex-shrink:0">
        <div><div style="font-size:14px;font-weight:600">Enable LLM Gateway</div><div style="font-size:12px;color:var(--dim);margin-top:2px">Powers AI inference for solving mining challenges.</div></div>
      </div>
    </div>
    <div style="margin-top:14px;padding:10px 14px;border-radius:var(--radius-sm);background:rgba(0,212,255,0.06);border:1px solid rgba(0,212,255,0.1);font-size:12px;color:var(--accent)">
      Open <a href="https://bankr.bot/api" target="_blank" style="color:var(--accent);font-weight:600">bankr.bot/api</a>, select your API key, and toggle these settings on. Check the boxes above once done.
    </div>
    <div id="step2Status"></div>
    <div style="display:flex;gap:8px;margin-top:14px">
      <button class="btn btn-ghost btn-sm" onclick="goStep(1)">Back</button>
      <button class="btn btn-accent btn-sm" id="btnStep2Next" onclick="confirmBankrConfig()">Continue</button>
    </div>
  </div>
</div>

<!-- Step 3: Wallet -->
<div class="step" id="step3">
  <div class="step-header"><span class="step-num">3</span><span class="step-title">Wallet & Balances</span></div>
  <p class="step-desc">Your non-custodial wallet on Base. You need ETH for gas and BOTCOIN for staking.</p>
  <div class="step-body">
    <div class="info-box"><div class="lbl">Wallet Address</div><div class="val" id="walletAddr" style="font-size:13px;font-family:var(--mono)">—</div></div>
    <div style="display:flex;gap:10px">
      <div class="info-box" style="flex:1"><div class="lbl">ETH</div><div class="val" id="ethBal">—</div></div>
      <div class="info-box" style="flex:1"><div class="lbl">BOTCOIN (wallet)</div><div class="val" id="botBal">—</div></div>
      <div class="info-box" style="flex:1"><div class="lbl">BOTCOIN (staked)</div><div class="val" id="botStaked" style="color:var(--green)">—</div></div>
    </div>
    <div id="step3Status"></div>
    <div id="fundingActions" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px"></div>
    <div style="display:flex;gap:8px;margin-top:14px">
      <button class="btn btn-ghost btn-sm" onclick="goStep(2)">Back</button>
      <button class="btn btn-accent btn-sm" id="btnStep3Next" onclick="goStep(4)">Continue</button>
    </div>
  </div>
</div>

<!-- Step 4: Stake -->
<div class="step" id="step4">
  <div class="step-header"><span class="step-num">4</span><span class="step-title">Stake BOTCOIN</span></div>
  <p class="step-desc">Stake to earn credits. Higher stake = more credits per solve.</p>
  <div class="step-body">
    <div style="display:flex;gap:10px;margin-bottom:14px">
      <div class="info-box" style="flex:1"><div class="lbl">BOTCOIN (wallet)</div><div class="val" id="step4BotBal">—</div></div>
      <div class="info-box" style="flex:1"><div class="lbl">BOTCOIN (staked)</div><div class="val" id="step4BotStaked" style="color:var(--green)">—</div></div>
    </div>
    <div class="tier-cards">
      <div class="tier-card t1" onclick="doStake('25000000000000000000000000')"><div class="amt">25M</div><div class="cr">1 credit/solve</div></div>
      <div class="tier-card t2" onclick="doStake('50000000000000000000000000')"><div class="amt">50M</div><div class="cr">2 credits/solve</div></div>
      <div class="tier-card t3" onclick="doStake('100000000000000000000000000')"><div class="amt">100M</div><div class="cr">3 credits/solve</div></div>
    </div>
    <div id="step4Status"></div>
    <div style="display:flex;gap:8px;margin-top:14px">
      <button class="btn btn-ghost btn-sm" onclick="goStep(3)">Back</button>
      <button class="btn btn-accent btn-sm" onclick="goStep(5)">Continue / Skip</button>
    </div>
  </div>
</div>

<!-- Step 5: LLM -->
<div class="step" id="step5">
  <div class="step-header"><span class="step-num">5</span><span class="step-title">LLM Configuration</span></div>
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
    <div id="step5Status"></div>
    <div style="display:flex;gap:8px;margin-top:16px">
      <button class="btn btn-ghost btn-sm" onclick="goStep(4)">Back</button>
      <button class="btn btn-green" onclick="finishSetup()" style="flex:1">Start Mining</button>
    </div>
  </div>
</div>

<!-- Step 6: Done -->
<div class="step" id="step6">
  <div class="step-header"><span class="step-check">&#10003;</span><span class="step-title">Setup Complete</span></div>
  <div class="status ok" style="margin:16px 0 0 38px">Mining is starting. Redirecting to dashboard...</div>
</div>

<script>
let CSRF=document.querySelector('meta[name=csrf-token]').content;
function updateCSRF(token){if(token){CSRF=token;document.querySelector('meta[name=csrf-token]').content=token}}
function H(method,body){return{method,headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF},body:body?JSON.stringify(body):undefined}}
let currentStep=1;
function updateProgress(){const p=document.getElementById('progress');p.innerHTML='';for(let i=1;i<=5;i++){const cls=i<currentStep?'seg done':i===currentStep?'seg active':'seg';p.innerHTML+='<div class="'+cls+'"></div>'}}
updateProgress();
function showStatus(elId,cls,msg){const el=document.getElementById(elId);if(!msg){el.innerHTML='';return}el.innerHTML='<div class="status '+cls+'">'+msg+'</div>'}
function goStep(n){document.getElementById('step'+currentStep).classList.remove('active');currentStep=n;document.getElementById('step'+n).classList.add('active');updateProgress();if(n===3)loadWallet();if(n===4)checkStake();if(n===5)loadLLMCredits()}
function confirmBankrConfig(){showStatus('step2Status','');goStep(3)}

function toggleEmailLogin(){const s=document.getElementById('emailSection');s.classList.toggle('show');const t=document.getElementById('emailToggle');t.textContent=s.classList.contains('show')?'Already have a key? Paste it above':'New user? Sign up with email'}
async function connectApiKey(){
  const key=document.getElementById('apiKeyInput').value.trim();
  if(!key||!key.startsWith('bk_')){showStatus('step1Status','err','Enter a valid API key (starts with bk_)');return}
  showStatus('step1Status','info','<span class="spinner"></span> Connecting...');
  const r=await fetch('/api/setup/connect',H('POST',{api_key:key}));
  const d=await r.json();
  if(d.ok){updateCSRF(d.csrf_token);showStatus('step1Status','ok','Connected! Redirecting...');setTimeout(()=>window.location.href='/dashboard',500)}
  else showStatus('step1Status','err',d.error||'Failed')
}
let _privyAppId='',_privyClientId='';
async function sendOtp(){
  const email=document.getElementById('emailInput').value.trim();
  if(!email||!email.includes('@')){showStatus('step1Status','err','Enter a valid email');return}
  document.getElementById('btnSendOtp').disabled=true;
  showStatus('step1Status','info','<span class="spinner"></span> Sending code...');
  const r=await fetch('/api/setup/send-otp',H('POST',{email}));
  const d=await r.json();
  if(d.ok){if(d.privy_app_id)_privyAppId=d.privy_app_id;if(d.privy_client_id)_privyClientId=d.privy_client_id;showStatus('step1Status','ok','Code sent! Check email.');document.getElementById('otpSection').classList.add('show')}
  else{showStatus('step1Status','err',d.error||'Failed');document.getElementById('btnSendOtp').disabled=false}
}
async function verifyOtp(){
  const email=document.getElementById('emailInput').value.trim(),code=document.getElementById('otpInput').value.trim();
  if(!code){showStatus('step1Status','err','Enter the verification code');return}
  showStatus('step1Status','info','<span class="spinner"></span> Verifying...');
  const r=await fetch('/api/setup/verify-otp',H('POST',{email,code,privy_app_id:_privyAppId,privy_client_id:_privyClientId}));
  const d=await r.json();
  if(d.ok){updateCSRF(d.csrf_token);showStatus('step1Status','ok','Connected!');setTimeout(()=>goStep(2),500)}
  else if(d.need_api_key){showStatus('step1Status','info','Account created! Now create an API key at <a href="https://bankr.bot/api" target="_blank" style="color:var(--accent);font-weight:600">bankr.bot/api</a> and paste it above.');document.getElementById('emailSection').classList.remove('show');document.getElementById('emailToggle').style.display='none'}
  else showStatus('step1Status','err',d.error||'Failed')
}
async function loadWallet(){
  console.log('[loadWallet] called');
  showStatus('step3Status','info','<span class="spinner"></span> Loading wallet...');
  try{
    const r=await fetch('/api/setup/wallet');
    console.log('[loadWallet] wallet status:',r.status);
    if(!r.ok){showStatus('step3Status','err','Wallet request failed ('+r.status+')');return}
    const d=await r.json();
    if(d.error){showStatus('step3Status','err',d.error);return}
    console.log('wallet response:',d);
    document.getElementById('walletAddr').textContent=d.address||'—';
    document.getElementById('ethBal').textContent=d.eth!==undefined?parseFloat(d.eth).toFixed(4)+' ETH':'—';
    document.getElementById('botBal').textContent=d.botcoin!==undefined?Number(d.botcoin).toLocaleString():'—';
    const sr=await fetch('/api/setup/check-stake');
    const sd=await sr.json();
    console.log('check-stake response:',sd);
    const staked=sd.staked||0;
    document.getElementById('botStaked').textContent=staked>0?Number(staked).toLocaleString():'0';
    const totalBot=(d.botcoin||0)+staked;
    const a=document.getElementById('fundingActions');a.innerHTML='';
    if(d.eth<0.001)a.innerHTML+='<a href="https://app.across.to/bridge-and-swap" target="_blank" class="btn btn-ghost btn-sm">Bridge ETH</a>';
    if(totalBot<25000000)a.innerHTML+='<a href="https://app.uniswap.org/swap?outputCurrency=0xA601877977340862Ca67f816eb079958E5bd0BA3&chain=base" target="_blank" class="btn btn-ghost btn-sm">Buy BOTCOIN</a>';
    if(d.eth<0.001||totalBot<25000000){a.innerHTML+='<button class="btn btn-ghost btn-sm" onclick="loadWallet()">Refresh</button>';showStatus('step3Status','warn','Need ETH for gas and 25M+ BOTCOIN to mine.')}
    else showStatus('step3Status','ok','Balances look good!')
  }catch(e){showStatus('step3Status','err','Error: '+e.message);console.error('loadWallet error:',e)}
}
async function checkStake(){
  showStatus('step4Status','info','<span class="spinner"></span> Checking stake...');
  try{
    const wr=await fetch('/api/setup/wallet');const wd=await wr.json();
    document.getElementById('step4BotBal').textContent=wd.botcoin!==undefined?Number(wd.botcoin).toLocaleString():'0';
    const r=await fetch('/api/setup/check-stake');const d=await r.json();
    const staked=d.staked||0;
    document.getElementById('step4BotStaked').textContent=staked>0?Number(staked).toLocaleString():'0';
    if(staked>0){const tier=staked>=100000000?'3cr/solve':staked>=50000000?'2cr/solve':'1cr/solve';showStatus('step4Status','ok','Staked: '+Number(staked).toLocaleString(undefined,{maximumFractionDigits:0})+' BOTCOIN ('+tier+')')}
    else showStatus('step4Status','info','Not staked yet. Pick a tier above or skip for now.')
  }catch(e){showStatus('step4Status','info','Pick a tier to stake, or skip.')}
}
async function doStake(amount){
  showStatus('step4Status','info','<span class="spinner"></span> Staking (2 transactions)...');
  const r=await fetch('/api/setup/stake',H('POST',{amount}));
  const d=await r.json();
  showStatus('step4Status',d.ok?'ok':'err',d.message||'Done')
}
async function loadLLMCredits(){
  const r=await fetch('/api/setup/llm-credits');const d=await r.json();
  const el=document.getElementById('llmCredits'),link=document.getElementById('llmTopupLink');
  if(d.balance>=0){el.textContent='$'+d.balance.toFixed(2);el.style.color=d.balance>1?'var(--green)':'var(--yellow)';link.style.display=d.balance<1?'block':'none'}
  else{el.textContent='Unable to check';link.style.display='block'}
}
async function finishSetup(){
  const model=document.getElementById('modelSetupSelect').value,autoTopup=document.getElementById('autoTopupCheck').checked;
  showStatus('step5Status','info','<span class="spinner"></span> Starting...');
  const r=await fetch('/api/setup/finish',H('POST',{model,auto_topup:autoTopup}));
  const d=await r.json();
  if(d.ok){goStep(6);setTimeout(()=>{window.location.href='/dashboard'},2000)}
  else showStatus('step5Status','err',d.error||'Failed')
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
<link rel="icon" type="image/x-icon" href="/static/favicon.ico"><link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png"><link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<style>
""" + SHARED_CSS + r"""
.header{display:flex;align-items:center;justify-content:space-between;padding:14px 24px;background:var(--bg-card);border-bottom:1px solid var(--border)}
.header-left{display:flex;align-items:center;gap:16px}
.logo{font-size:18px;font-weight:700;letter-spacing:-.5px}
.phase{display:inline-flex;align-items:center;gap:6px;padding:5px 14px;border-radius:20px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#fff}
.phase .dot{width:6px;height:6px;border-radius:50%;background:currentColor;animation:pulse 1.5s infinite}
.controls{display:flex;align-items:center;gap:8px}
.controls select{padding:6px 10px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:12px;font-family:var(--font)}
.wallet-tag{font-size:11px;color:var(--dim);font-family:var(--mono);cursor:pointer;position:relative}
.wallet-tag:hover{color:var(--accent)}
.copy-tip{position:absolute;top:-26px;left:50%;transform:translateX(-50%);background:var(--green);color:#000;font-size:10px;padding:2px 8px;border-radius:4px;white-space:nowrap;pointer-events:none;opacity:0;transition:opacity .2s}
.copy-tip.show{opacity:1}
.grid{display:grid;grid-template-columns:300px 1fr 1fr;grid-template-rows:auto 1fr 1fr 240px;gap:14px;padding:14px 24px;height:calc(100vh - 58px)}
/* Staking panel */
.stake-panel{grid-row:1/5;display:flex;flex-direction:column;gap:14px;overflow-y:auto}
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
/* Challenge panel */
.challenge-panel{display:flex;flex-direction:column;overflow:hidden}
.challenge-content{flex:1;overflow-y:auto;padding:10px;background:var(--bg);border-radius:var(--radius-sm);font-size:12px}
.challenge-content .q-item{padding:6px 0;border-bottom:1px solid var(--border)}
.challenge-content .q-label{font-size:10px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.5px}
.challenge-content .q-text{color:var(--text);margin-top:2px}
.constraint-item{padding:4px 0;font-size:11px;color:var(--dim);display:flex;gap:6px;align-items:flex-start}
.constraint-idx{color:var(--accent);font-weight:700;flex-shrink:0;font-size:10px;min-width:20px}
.constraint-fail{color:var(--red)}.constraint-pass{color:var(--green)}
.doc-preview{font-size:11px;color:var(--muted);font-style:italic;padding:8px 0;border-bottom:1px solid var(--border);margin-bottom:6px;line-height:1.5;max-height:400px;overflow-y:auto}
/* LLM panel */
.llm-panel{display:flex;flex-direction:column;grid-column:2/4}
.llm-content{flex:1;overflow-y:auto;font-family:var(--mono);font-size:11px;white-space:pre-wrap;word-break:break-word;color:var(--dim);padding:10px;background:var(--bg);border-radius:var(--radius-sm)}
.result-badge{display:inline-flex;align-items:center;gap:5px;padding:4px 12px;border-radius:12px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px}
.result-badge.pass{background:rgba(0,230,118,0.12);color:var(--green)}.result-badge.fail{background:rgba(255,71,87,0.12);color:var(--red)}
.artifact-box{padding:10px;margin-top:8px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg);font-family:var(--mono);font-size:11px;word-break:break-word;color:var(--text);line-height:1.6}
/* Log */
.log-panel{grid-column:2/4}
.log-content{flex:1;overflow-y:auto;font-family:var(--mono);font-size:11px;line-height:1.8;padding:10px;background:var(--bg);border-radius:var(--radius-sm)}
.log-line{color:var(--dim)}.log-ts{color:var(--accent);margin-right:8px}
/* Toasts */
.toast-container{position:fixed;top:70px;right:20px;z-index:1000;display:flex;flex-direction:column;gap:8px}
.toast{padding:12px 18px;border-radius:var(--radius-sm);font-size:13px;font-weight:500;animation:slideIn .3s ease-out;min-width:260px;box-shadow:0 8px 30px rgba(0,0,0,.4)}
.toast-ok{background:rgba(0,230,118,0.15);color:var(--green);border:1px solid rgba(0,230,118,0.2)}
.toast-err{background:rgba(255,71,87,0.15);color:var(--red);border:1px solid rgba(255,71,87,0.2)}
@media(max-width:900px){
.header{flex-direction:column;gap:10px;padding:10px 14px}
.header-left{flex-wrap:wrap;justify-content:center}
.controls{flex-wrap:wrap;justify-content:center}
.grid{grid-template-columns:1fr;grid-template-rows:auto;height:auto;padding:10px 12px;gap:10px}
.stake-panel{grid-row:auto}.log-panel{grid-column:auto}.challenge-panel{grid-column:auto}.llm-panel{grid-column:auto}
.stats-grid{grid-template-columns:repeat(2,1fr)}
.stats-area{order:-1}
.log-content{max-height:300px}.llm-content{max-height:400px}.challenge-content{max-height:350px}
}
</style></head><body>
<div id="dashLoading" style="position:fixed;inset:0;z-index:999;background:var(--bg);display:flex;align-items:center;justify-content:center;flex-direction:column;gap:16px"><span class="spinner" style="width:28px;height:28px;border-width:3px"></span><div style="color:var(--dim);font-size:13px">Loading dashboard...</div></div>
<div class="header">
  <div class="header-left">
    <div class="logo"><span class="grad-text">BOTCOIN</span> MINER</div>
    <span class="phase" id="phaseBadge" style="background:#d4a017"><span class="dot"></span> LOADING</span>
    <span class="wallet-tag" id="walletInfo" onclick="copyWallet()" title="Click to copy full address"><span class="copy-tip" id="copyTip">Copied!</span></span>
  </div>
  <div class="controls">
    <select id="modelSelect" title="LLM Model">MODELOPTIONS</select>
    <button class="btn btn-green btn-sm" id="btnStart">Start</button>
    <button class="btn btn-red btn-sm" id="btnStop">Stop</button>
    <a href="/setup" class="btn btn-ghost btn-sm" style="text-decoration:none">Setup</a>
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
          <button class="btn btn-yellow btn-sm" style="flex:1" onclick="showUnstakeUI()">Unstake</button>
        </div>
        <div id="unstakeUI" style="display:none;margin-top:8px;padding:12px;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm)">
          <label style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);margin-bottom:4px;display:block">Unstake Amount (BOTCOIN)</label>
          <input type="text" id="unstakeAmt" placeholder="0" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg-card);color:var(--text);font-size:14px;font-family:var(--mono);margin-bottom:6px">
          <div style="display:flex;gap:4px;margin-bottom:8px">
            <button class="btn btn-ghost btn-sm" style="flex:1;padding:4px" onclick="setUnstakePct(25)">25%</button>
            <button class="btn btn-ghost btn-sm" style="flex:1;padding:4px" onclick="setUnstakePct(50)">50%</button>
            <button class="btn btn-ghost btn-sm" style="flex:1;padding:4px" onclick="setUnstakePct(75)">75%</button>
            <button class="btn btn-accent btn-sm" style="flex:1;padding:4px" onclick="setUnstakePct(100)">Max</button>
          </div>
          <div style="display:flex;gap:6px">
            <button class="btn btn-yellow btn-sm" style="flex:1" onclick="dashUnstake()">Confirm Unstake</button>
            <button class="btn btn-ghost btn-sm" onclick="document.getElementById('unstakeUI').style.display='none'">Cancel</button>
          </div>
        </div>
        <div style="display:flex;gap:6px;margin-top:6px">
          <button class="btn btn-ghost btn-sm" style="flex:1" onclick="showSendReceive('receive')">Receive</button>
          <button class="btn btn-ghost btn-sm" style="flex:1" onclick="showSendReceive('send')">Send</button>
        </div>
      </div>
      <div id="stakeStatus" style="font-size:12px;margin-top:8px"></div>
      <!-- Send/Receive Panel -->
      <div id="sendReceivePanel" style="display:none;margin-top:12px;padding:14px;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm)">
        <div id="receivePanel" style="display:none">
          <div style="font-size:12px;font-weight:600;margin-bottom:8px">Receive BOTCOIN</div>
          <div style="font-size:11px;color:var(--dim);margin-bottom:8px">Send BOTCOIN to this address on the <strong style="color:var(--accent)">Base</strong> network only.</div>
          <div style="padding:10px;background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);font-family:var(--mono);font-size:11px;word-break:break-all;color:var(--text)" id="receiveAddr">—</div>
          <button class="btn btn-accent btn-sm" style="width:100%;margin-top:8px" onclick="copyReceiveAddr()">Copy Address</button>
          <div id="receiveCopyTip" style="text-align:center;font-size:11px;color:var(--green);margin-top:4px;display:none">Copied!</div>
        </div>
        <div id="sendPanel" style="display:none">
          <div style="font-size:12px;font-weight:600;margin-bottom:4px">Send BOTCOIN</div>
          <div style="font-size:11px;color:var(--dim);margin-bottom:8px">Available: <strong style="color:var(--text)" id="sendBalance">--</strong> BOTCOIN</div>
          <label style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);margin-bottom:4px;display:block">Recipient Address</label>
          <input type="text" id="sendAddr" placeholder="0x..." style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg-card);color:var(--text);font-size:12px;font-family:var(--mono);margin-bottom:8px">
          <label style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);margin-bottom:4px;display:block">Amount (BOTCOIN)</label>
          <input type="text" id="sendAmt" placeholder="0" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg-card);color:var(--text);font-size:14px;font-family:var(--mono);margin-bottom:8px">
          <button class="btn btn-accent btn-sm" style="width:100%" onclick="dashSend()">Send</button>
        </div>
        <button class="btn btn-ghost btn-sm" style="width:100%;margin-top:8px" onclick="document.getElementById('sendReceivePanel').style.display='none'">Close</button>
      </div>
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
        <div class="stat"><div class="stat-label">ETH (gas) <a href="https://app.across.to/bridge-and-swap" target="_blank" style="font-size:9px">&#8599;</a></div><div class="stat-value" id="sEth" style="font-size:16px">--</div></div>
      </div>
    </div>
    <div class="card" style="flex:1;display:flex;flex-direction:column;overflow:hidden">
      <div class="card-title">Transactions</div>
      <div class="tx-list" id="txList"><div style="color:var(--muted);font-size:12px;padding:8px 0">No transactions yet</div></div>
    </div>
  </div>

  <!-- Right: Challenge Details -->
  <div class="card challenge-panel">
    <div class="card-title">Challenge</div>
    <div class="challenge-content" id="challengeContent"><div style="color:var(--muted);font-size:12px">Waiting for challenge...</div></div>
  </div>

  <!-- LLM Output + Result -->
  <div class="card llm-panel">
    <div class="card-title">LLM Output <span id="resultBadge"></span></div>
    <div id="artifactSection" style="display:none">
      <div style="font-size:10px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Artifact</div>
      <div class="artifact-box" id="artifactText"></div>
      <div id="verifyIssues" style="margin-top:6px;font-size:11px"></div>
    </div>
    <div class="llm-content" id="llmOutput" style="margin-top:8px">Waiting for first solve...</div>
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
let lastVersion=-1,prevTxMap={},fullWalletAddr='';

function esc(s){const el=document.createElement('span');el.textContent=s;return el.innerHTML}
function toast(msg,ok){const c=document.getElementById('toasts'),t=document.createElement('div');t.className='toast '+(ok?'toast-ok':'toast-err');t.textContent=msg;c.appendChild(t);setTimeout(()=>t.remove(),5000)}
function ago(ts){const s=Math.floor((Date.now()/1000)-ts);if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';return Math.floor(s/3600)+'h ago'}
function fmtCooldown(secs){if(secs<=0)return'Ready!';const h=Math.floor(secs/3600),m=Math.floor((secs%3600)/60),s=secs%60;return(h?h+'h ':'')+(m?m+'m ':'')+s+'s'}

let _loaded=false;
function connectSSE(){const es=new EventSource('/events');es.onmessage=function(e){const d=JSON.parse(e.data);if(!_loaded){_loaded=true;document.getElementById('dashLoading').style.display='none'}if(d.version===lastVersion)return;lastVersion=d.version;update(d)};es.onerror=function(){es.close();setTimeout(connectSSE,2000)}}
async function refreshBalances(){try{const r=await fetch('/api/refresh-balances');const d=await r.json();if(d.ok){lastVersion=-1}}catch(e){}}
let _docExpanded=false;
async function toggleFullDoc(){const pre=document.getElementById('docPreviewText');const full=document.getElementById('docFullText');if(!pre||!full)return;if(_docExpanded){full.style.display='none';pre.style.display='';_docExpanded=false}else{if(!full.textContent){try{const r=await fetch('/api/challenge-doc');const d=await r.json();full.textContent=d.doc||'No document available'}catch(e){full.textContent='Failed to load'}}full.style.display='block';pre.style.display='none';_docExpanded=true}}
setTimeout(refreshBalances,500);setInterval(refreshBalances,120000);

function update(d){
  // Phase
  const badge=document.getElementById('phaseBadge');badge.innerHTML='<span class="dot"></span> '+d.phase;badge.style.background=PC[d.phase]||'#555';
  // Wallet
  const addr=d.miner_address;fullWalletAddr=addr||'';const short=addr&&addr.length>12?addr.slice(0,6)+'...'+addr.slice(-4):'';
  const wi=document.getElementById('walletInfo');wi.childNodes.forEach(n=>{if(n.nodeType===3)n.remove()});wi.insertAdjacentText('beforeend',short);
  // Stats
  document.getElementById('sSolves').textContent=d.total_solves;
  document.getElementById('sFails').textContent=d.total_fails;
  document.getElementById('sCredits').textContent=d.total_credits;
  document.getElementById('sEpoch').textContent=d.epoch_id||'--';
  document.getElementById('sUptime').textContent=d.uptime;
  document.getElementById('sLLM').textContent=d.llm_credits>=0?'$'+d.llm_credits.toFixed(2):'--';
  const ethEl=document.getElementById('sEth');if(d.eth_balance>0){ethEl.textContent=parseFloat(d.eth_balance).toFixed(5);ethEl.style.color=d.eth_balance<0.001?'var(--red)':'var(--text)'}else{ethEl.textContent='--'}
  // LLM output
  if(d.llm_output)document.getElementById('llmOutput').textContent=d.llm_output;
  // Result badge + artifact
  const rb=document.getElementById('resultBadge'),as=document.getElementById('artifactSection'),at=document.getElementById('artifactText'),vi=document.getElementById('verifyIssues');
  if(d.solve_passed==='pass'){rb.innerHTML='<span class="result-badge pass">PASS</span>';rb.style.display='inline'}
  else if(d.solve_passed==='fail'){rb.innerHTML='<span class="result-badge fail">FAIL</span>';rb.style.display='inline'}
  else{rb.innerHTML='';rb.style.display='none'}
  if(d.solve_artifact){as.style.display='block';at.textContent=d.solve_artifact;
    let viHtml='';if(d.solve_verification_issues&&d.solve_verification_issues.length>0)viHtml=d.solve_verification_issues.map(i=>'<div style="color:var(--yellow);font-size:11px">'+esc(i)+'</div>').join('');
    if(d.solve_passed==='fail'&&d.solve_failed_constraints&&d.solve_failed_constraints.length>0)viHtml+='<div style="color:var(--red);font-size:11px;margin-top:4px">Failed constraints: '+esc(d.solve_failed_constraints.join(', '))+'</div>';
    vi.innerHTML=viHtml}else{as.style.display='none'}
  // Challenge panel
  const cc=document.getElementById('challengeContent');
  if(d.challenge_questions&&d.challenge_questions.length>0){let html='';
    if(d.challenge_doc_preview){html+='<div class="doc-preview" style="cursor:pointer;position:relative" onclick="toggleFullDoc()" title="Click to expand full document"><span id="docPreviewText">'+esc(d.challenge_doc_preview.slice(0,300))+(d.challenge_doc_preview.length>300?'... <span style="color:var(--accent);font-size:10px">[show full]</span>':'')+'</span><div id="docFullText" style="display:none;white-space:pre-wrap"></div></div>';}
    html+='<div style="font-size:10px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.5px;margin:8px 0 4px">Questions</div>';
    d.challenge_questions.forEach((q,i)=>{html+='<div class="q-item"><span class="q-label">Q'+(i+1)+'</span><div class="q-text">'+esc(q)+'</div></div>'});
    if(d.challenge_constraints&&d.challenge_constraints.length>0){html+='<div style="font-size:10px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.5px;margin:12px 0 4px">Constraints</div>';
      const failSet=new Set(d.solve_failed_constraints||[]);
      d.challenge_constraints.forEach((c,i)=>{const isFail=failSet.has(i);const isPass=d.solve_passed==='pass';
        const cls=isFail?'constraint-fail':(isPass?'constraint-pass':'');
        html+='<div class="constraint-item"><span class="constraint-idx '+cls+'">C'+(i+1)+'</span><span class="'+cls+'">'+esc(c)+'</span></div>'})}
    cc.innerHTML=html}else if(d.phase!=='INIT'){cc.innerHTML='<div style="color:var(--muted);font-size:12px">Requesting challenge...</div>'}
  // Log
  const logEl=document.getElementById('logContent');
  if(d.log_lines&&d.log_lines.length>0){logEl.innerHTML=d.log_lines.map(l=>{const p=l.match(/^(\d{2}:\d{2}:\d{2})\s(.*)$/);if(p)return'<div class="log-line"><span class="log-ts">'+esc(p[1])+'</span>'+esc(p[2])+'</div>';return'<div class="log-line">'+esc(l)+'</div>'}).join('');logEl.scrollTop=logEl.scrollHeight}
  // Model
  const sel=document.getElementById('modelSelect');if(sel.value!==d.model)sel.value=d.model;
  // Buttons
  document.getElementById('btnStart').disabled=d.mining_active;document.getElementById('btnStop').disabled=!d.mining_active;
  document.getElementById('btnStart').style.opacity=d.mining_active?'.35':'1';document.getElementById('btnStop').style.opacity=d.mining_active?'1':'.35';
  // Staking
  const sa=d.staked_amount||0;currentStakedRaw=sa;
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
  if(cdSection){if(d.withdrawable_at>0){cdSection.style.display='block';document.getElementById('cooldownTimer').textContent=fmtCooldown(csec);document.getElementById('cooldownLabel').textContent=csec>0?'Withdrawal cooldown':'Ready to withdraw!'}else{cdSection.style.display='none'}}
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
let currentStakedRaw=0;
function showUnstakeUI(){document.getElementById('unstakeUI').style.display='block';document.getElementById('unstakeAmt').value=''}
function setUnstakePct(pct){const sa=currentStakedRaw;const amt=Math.floor(sa*(pct/100));document.getElementById('unstakeAmt').value=amt>0?amt.toLocaleString('en-US',{useGrouping:false}):'0'}
async function dashUnstake(){
  const amt=document.getElementById('unstakeAmt').value.replace(/,/g,'').trim();
  if(!amt||isNaN(amt)||Number(amt)<=0){document.getElementById('stakeStatus').innerHTML='<span style="color:var(--red)">Enter a valid amount</span>';return}
  if(!confirm('Unstake '+Number(amt).toLocaleString()+' BOTCOIN? This starts a 24h cooldown.'))return;
  document.getElementById('stakeStatus').innerHTML='<span class="spinner"></span> Unstaking...';
  document.getElementById('unstakeUI').style.display='none';
  const r=await fetch('/api/unstake',H('POST',{amount:amt}));const d=await r.json();
  document.getElementById('stakeStatus').innerHTML='<span style="color:'+(d.ok?'var(--yellow)':'var(--red)')+'">'+esc(d.message)+'</span>';
}
function showSendReceive(mode){
  const panel=document.getElementById('sendReceivePanel');
  const rp=document.getElementById('receivePanel'),sp=document.getElementById('sendPanel');
  panel.style.display='block';rp.style.display=mode==='receive'?'block':'none';sp.style.display=mode==='send'?'block':'none';
  if(mode==='receive')document.getElementById('receiveAddr').textContent=fullWalletAddr||'—';
  if(mode==='send'){const wb=document.getElementById('walletBotcoin').textContent;document.getElementById('sendBalance').textContent=wb&&wb!=='--'?wb:'0'}
}
function copyReceiveAddr(){if(!fullWalletAddr)return;navigator.clipboard.writeText(fullWalletAddr).then(()=>{const el=document.getElementById('receiveCopyTip');el.style.display='block';setTimeout(()=>el.style.display='none',2000)})}
async function dashSend(){
  const addr=document.getElementById('sendAddr').value.trim();
  const amt=document.getElementById('sendAmt').value.replace(/,/g,'').trim();
  if(!addr||!addr.startsWith('0x')||addr.length!==42){document.getElementById('stakeStatus').innerHTML='<span style="color:var(--red)">Invalid address</span>';return}
  if(!amt||isNaN(amt)||Number(amt)<=0){document.getElementById('stakeStatus').innerHTML='<span style="color:var(--red)">Invalid amount</span>';return}
  if(!confirm('Send '+Number(amt).toLocaleString()+' BOTCOIN to '+addr.slice(0,6)+'...'+addr.slice(-4)+'?'))return;
  document.getElementById('stakeStatus').innerHTML='<span class="spinner"></span> Sending...';
  document.getElementById('sendReceivePanel').style.display='none';
  const r=await fetch('/api/send',H('POST',{to:addr,amount:amt}));const d=await r.json();
  document.getElementById('stakeStatus').innerHTML='<span style="color:'+(d.ok?'var(--green)':'var(--red)')+'">'+esc(d.message)+'</span>';
  if(d.ok)setTimeout(()=>fetch('/api/refresh-staking'),3000);
}
async function doLogout(){if(!confirm('Stop mining and logout?'))return;await fetch('/api/logout',H('POST'));window.location.href='/'}
function copyWallet(){if(!fullWalletAddr)return;navigator.clipboard.writeText(fullWalletAddr).then(()=>{const tip=document.getElementById('copyTip');tip.classList.add('show');setTimeout(()=>tip.classList.remove('show'),1500)})}

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
        self._app = Flask(__name__, static_folder="static", static_url_path="/static")
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
            if session_id and state:
                return self._serve_dashboard(session_id, state)
            if session_id:
                # Session exists but no state yet — check if they have an API key
                api_key = self._sessions.get_api_key(session_id)
                if api_key:
                    state = self._create_state(session_id)
                    return self._serve_dashboard(session_id, state)
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

        @app.route("/terms")
        def terms():
            return TERMS_HTML

        @app.route("/privacy")
        def privacy():
            return PRIVACY_HTML

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
            body = request.get_json(silent=True) or {}
            from auth import validate_api_key
            api_key = body.get("api_key", "").strip()
            if not api_key or not validate_api_key(api_key):
                return jsonify({"ok": False, "error": "Invalid API key format"})

            ip = request.remote_addr or "unknown"
            if not _check_rate_limit(f"connect:{ip}", 5, 60):
                return jsonify({"ok": False, "error": "Too many attempts. Try again in a minute."}), 429

            # Verify the key works by calling Bankr and get wallet
            try:
                import httpx
                me_resp = httpx.get("https://api.bankr.bot/agent/me",
                                 headers={"X-API-Key": api_key}, timeout=15)
                if me_resp.status_code >= 400:
                    return jsonify({"ok": False, "error": "Invalid API key. Check it at bankr.bot/api."})
                me_data = me_resp.json()
                wallets = me_data.get("wallets", [])
                address = ""
                for w in wallets:
                    if w.get("chain", "").lower() in ("base", "evm", "ethereum"):
                        address = w.get("address", "")
                        break
                if not address and wallets:
                    address = wallets[0].get("address", "")
                email = ""
                for s in me_data.get("socialAccounts", []):
                    if s.get("platform") == "email":
                        email = s.get("username", "")
                        break
            except Exception:
                return jsonify({"ok": False, "error": "Could not verify key. Please try again."})

            session_id = sessions.create_session(api_key)
            state = self._create_state(session_id)
            state.miner_address = address
            sessions.update_miner_address(session_id, address)
            csrf_token = sessions.get_csrf_token(session_id)
            short = address[:6] + "..." + address[-4:] if len(address) > 12 else address
            resp = jsonify({"ok": True, "csrf_token": csrf_token, "wallet": short, "email": email})
            resp.set_cookie("session_id", session_id,
                            httponly=True, samesite="Strict", max_age=86400,
                            secure=request.is_secure)
            return resp

        @app.route("/api/setup/send-otp", methods=["POST"])
        def setup_send_otp():
            body = request.get_json(silent=True) or {}
            email = body.get("email", "").strip()
            if not email or not validate_email(email):
                return jsonify({"ok": False, "error": "Invalid email address"})

            ip = request.remote_addr or "unknown"
            if not _check_rate_limit(f"otp:{ip}", 3, 60):
                return jsonify({"ok": False, "error": "Too many attempts. Try again in a minute."}), 429

            # Get Privy config from Bankr
            import httpx
            try:
                config_resp = httpx.get("https://api.bankr.bot/cli/config",
                                        headers={"User-Agent": "bankr-cli/0.1"}, timeout=15)
                if config_resp.status_code >= 400:
                    raise Exception(f"Config fetch failed: {config_resp.status_code}")
                privy_config = config_resp.json()
                privy_app_id = privy_config["privyAppId"]
                privy_client_id = privy_config.get("privyClientId", "")
            except Exception:
                # Fallback to CLI
                try:
                    import subprocess
                    result = subprocess.run(["bankr", "login", "email", "--", email],
                        capture_output=True, text=True, timeout=30)
                    output = result.stdout + result.stderr
                    if result.returncode == 0 or "code" in output.lower() or "sent" in output.lower():
                        return jsonify({"ok": True})
                except Exception:
                    pass
                return jsonify({"ok": False, "error": "Failed to send code. Please try again."})

            # Send OTP via Privy
            try:
                headers = {"Content-Type": "application/json",
                           "privy-app-id": privy_app_id}
                if privy_client_id:
                    headers["privy-client-id"] = privy_client_id
                resp = httpx.post("https://auth.privy.io/api/v1/passwordless/init",
                                  json={"email": email, "type": "email"},
                                  headers=headers, timeout=15)
                if resp.status_code < 400:
                    return jsonify({"ok": True, "privy_app_id": privy_app_id,
                                    "privy_client_id": privy_client_id})
                if resp.status_code == 429:
                    return jsonify({"ok": False, "error": "Rate limited. Wait a moment and try again."})
                return jsonify({"ok": False, "error": "Failed to send code. Please try again."})
            except Exception:
                return jsonify({"ok": False, "error": "Failed to send code. Please try again."})

        @app.route("/api/setup/verify-otp", methods=["POST"])
        def setup_verify_otp():
            body = request.get_json(silent=True) or {}
            email = body.get("email", "").strip()
            code = re.sub(r'[^a-zA-Z0-9]', '', body.get("code", "").strip())
            privy_app_id = body.get("privy_app_id", "").strip()
            privy_client_id = body.get("privy_client_id", "").strip()
            if not email or not validate_email(email):
                return jsonify({"ok": False, "error": "Invalid email"})
            if not code or not validate_otp(code):
                print(f"[verify-otp] Code validation failed: '{code}' (len={len(code)})")
                return jsonify({"ok": False, "error": "Invalid code format. Enter the 6-digit code from your email."})

            ip = request.remote_addr or "unknown"
            if not _check_rate_limit(f"verify:{ip}", 5, 60):
                return jsonify({"ok": False, "error": "Too many attempts. Try again in a minute."}), 429

            import httpx
            api_key = None

            # Step 1: Get Privy config if not passed from send-otp
            if not privy_app_id:
                try:
                    config_resp = httpx.get("https://api.bankr.bot/cli/config",
                                            headers={"User-Agent": "bankr-cli/0.1"}, timeout=15)
                    privy_config = config_resp.json()
                    privy_app_id = privy_config["privyAppId"]
                    privy_client_id = privy_config.get("privyClientId", "")
                except Exception:
                    pass

            # Step 2: Verify OTP via Privy to get identity token
            identity_token = None
            if privy_app_id:
                try:
                    headers = {"Content-Type": "application/json",
                               "privy-app-id": privy_app_id}
                    if privy_client_id:
                        headers["privy-client-id"] = privy_client_id
                    resp = httpx.post("https://auth.privy.io/api/v1/passwordless/authenticate",
                                      json={"email": email, "code": code, "mode": "login-or-sign-up"},
                                      headers=headers, timeout=30)
                    if resp.status_code < 400:
                        data = resp.json()
                        identity_token = data.get("identity_token") or data.get("token") or ""
                        # Try nested structures
                        if not identity_token and isinstance(data.get("user"), dict):
                            identity_token = data.get("identity_token", "")
                        print(f"[verify-otp] Privy auth OK, keys: {list(data.keys())}, has_token: {bool(identity_token)}")
                    elif resp.status_code == 429:
                        return jsonify({"ok": False, "error": "Rate limited. Wait a moment and try again."})
                    else:
                        print(f"[verify-otp] Privy auth failed: {resp.status_code} {resp.text[:200]}")
                        try:
                            err_data = resp.json()
                            err_msg = err_data.get("message", "") or err_data.get("error", "")
                            if "expired" in err_msg.lower():
                                return jsonify({"ok": False, "error": "Code expired. Please request a new one."})
                            if "invalid" in err_msg.lower() or "incorrect" in err_msg.lower():
                                return jsonify({"ok": False, "error": "Invalid code. Please check and try again."})
                        except Exception:
                            pass
                except Exception as e:
                    print(f"[verify-otp] Privy exception: {e}")

            # Step 3: Generate wallet via Bankr
            if identity_token:
                try:
                    bankr_headers = {"Content-Type": "application/json",
                                     "User-Agent": "bankr-cli/0.1",
                                     "privy-id-token": identity_token}
                    # Generate wallet (idempotent for existing users)
                    wr = httpx.post("https://api.bankr.bot/cli/generate-wallet",
                               headers=bankr_headers, timeout=15)
                    print(f"[verify-otp] generate-wallet: {wr.status_code}")

                    # Accept terms
                    tr = httpx.post("https://api.bankr.bot/user/accept-terms",
                               headers=bankr_headers, timeout=15)
                    print(f"[verify-otp] accept-terms: {tr.status_code}")

                    # Wallet is ready — prompt user to paste their API key
                    print(f"[verify-otp] Wallet ready, prompting for API key")
                    return jsonify({"ok": False, "need_api_key": True})
                except Exception as e:
                    print(f"[verify-otp] Bankr key generation error: {e}")

            # No CLI fallback — avoid creating extra API keys

            if api_key:
                session_id = sessions.create_session(api_key)
                state = self._create_state(session_id)
                csrf_token = sessions.get_csrf_token(session_id)
                resp = jsonify({"ok": True, "csrf_token": csrf_token})
                resp.set_cookie("session_id", session_id,
                                httponly=True, samesite="Strict", max_age=86400,
                                secure=request.is_secure)
                return resp

            return jsonify({"ok": False, "error": "Verification failed. Check the code and try again."})

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
                eth, tokens = _parse_bankr_balances(balances, "base")
                botcoin = 0.0
                for t in tokens:
                    sym = (t.get("symbol") or "").upper()
                    addr = (t.get("address") or t.get("tokenAddress") or "").lower()
                    bal = float(t.get("balance") or t.get("amount") or t.get("formattedBalance") or 0)
                    if sym == "BOTCOIN" or addr == "0xa601877977340862ca67f816eb079958e5bd0ba3":
                        botcoin = bal
                state.eth_balance = eth
                state.botcoin_balance = botcoin
                state.wallet_botcoin = botcoin
                state.bump()
                return jsonify({"address": address, "eth": eth, "botcoin": botcoin})
            except Exception as e:
                import traceback; traceback.print_exc()
                return jsonify({"error": "Failed to load wallet. Please try again."})

        @app.route("/api/setup/check-stake")
        @auth
        def setup_check_stake():
            session_id = g.session_id
            state = self._get_state(session_id)
            miner = state.miner_address if state else ""
            print(f"[check-stake] miner={miner!r}")
            if not miner:
                return jsonify({"staked": 0, "eligible": False})
            try:
                from coordinator_client import CoordinatorClient
                coord = CoordinatorClient(miner)
                staked = coord.get_staked_amount(miner)
                eligible = coord.is_eligible(miner)
                print(f"[check-stake] staked={staked} eligible={eligible}")
                if state:
                    state.staked_amount = staked if staked >= 0 else 0
                    state.bump()
                return jsonify({"staked": staked, "eligible": eligible})
            except Exception as e:
                print(f"[check-stake] ERROR: {e}")
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
            session_id = g.session_id
            api_key = sessions.get_api_key(session_id)
            if api_key:
                try:
                    import httpx
                    resp = httpx.get("https://llm.bankr.bot/v1/credits",
                                     headers={"X-API-Key": api_key}, timeout=15)
                    if resp.status_code < 400:
                        data = resp.json()
                        bal = data.get("balanceUsd", -1)
                        if isinstance(bal, (int, float)):
                            return jsonify({"balance": bal})
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
        @app.route("/api/refresh-balances")
        @auth
        def refresh_balances():
            session_id = g.session_id
            api_key = sessions.get_api_key(session_id)
            state = self._get_state(session_id)
            if not api_key or not state:
                return jsonify({"ok": False})
            try:
                from bankr_client import BankrClient
                bankr = BankrClient(api_key)

                # Resolve wallet address if not yet set
                if not state.miner_address:
                    me = bankr.get_me()
                    wallets = me.get("wallets", [])
                    address = ""
                    for w in wallets:
                        if w.get("chain", "").lower() in ("base", "evm", "ethereum"):
                            address = w.get("address", "")
                            break
                    if not address and wallets:
                        address = wallets[0].get("address", "")
                    state.miner_address = address
                    sessions.update_miner_address(session_id, address)

                balances = bankr.get_balances("base")
                eth, tokens = _parse_bankr_balances(balances, "base")
                botcoin = 0.0
                for t in tokens:
                    sym = (t.get("symbol") or "").upper()
                    addr = (t.get("address") or t.get("tokenAddress") or "").lower()
                    bal = float(t.get("balance") or t.get("amount") or t.get("formattedBalance") or 0)
                    if sym == "BOTCOIN" or addr == "0xa601877977340862ca67f816eb079958e5bd0ba3":
                        botcoin = bal
                state.eth_balance = eth
                state.botcoin_balance = botcoin
                state.wallet_botcoin = botcoin

                miner = state.miner_address
                if miner:
                    from coordinator_client import CoordinatorClient
                    coord = CoordinatorClient(miner)
                    staked = coord.get_staked_amount(miner)
                    state.staked_amount = staked if staked >= 0 else 0

                state.bump()
                return jsonify({"ok": True})
            except Exception:
                return jsonify({"ok": False})

        @app.route("/api/challenge-doc")
        @auth
        def challenge_doc():
            session_id = g.session_id
            state = self._get_state(session_id)
            if not state:
                return jsonify({"doc": ""})
            return jsonify({"doc": state.challenge_doc_full})

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

        @app.route("/api/send", methods=["POST"])
        @auth
        @csrf
        def dashboard_send():
            session_id = g.session_id
            api_key = sessions.get_api_key(session_id)
            state = self._get_state(session_id)
            if not api_key:
                return jsonify({"ok": False, "message": "Not connected"})
            body = request.get_json(silent=True) or {}
            to_addr = body.get("to", "").strip()
            amount = body.get("amount", "").strip()
            # Validate address
            if not to_addr or not re.match(r'^0x[a-fA-F0-9]{40}$', to_addr):
                return jsonify({"ok": False, "message": "Invalid address"})
            # Validate amount
            try:
                amt_float = float(amount.replace(",", ""))
                if amt_float <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                return jsonify({"ok": False, "message": "Invalid amount"})
            tx_id = state.add_pending_tx(f"Send {amt_float:,.0f} BOTCOIN")
            try:
                from bankr_client import BankrClient
                bankr = BankrClient(api_key)
                prompt = f"send {amt_float:,.0f} BOTCOIN (0xA601877977340862Ca67f816eb079958E5bd0BA3) to {to_addr} on base"
                result = bankr.prompt_and_poll(prompt, timeout=120)
                if "error" in str(result).lower() and "fail" in str(result).lower():
                    state.update_pending_tx(tx_id, "failed")
                    state.log(f"Send failed: {result[:100]}")
                    return jsonify({"ok": False, "message": "Transfer failed. Check balance and try again."})
                state.update_pending_tx(tx_id, "confirmed")
                state.log(f"Sent {amt_float:,.0f} BOTCOIN to {to_addr[:10]}...")
                return jsonify({"ok": True, "message": f"Sent {amt_float:,.0f} BOTCOIN!"})
            except Exception:
                state.update_pending_tx(tx_id, "failed")
                return jsonify({"ok": False, "message": "Transfer failed. Please try again."})

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
                    eth, tokens = _parse_bankr_balances(balances, "base")
                    state.eth_balance = eth
                    for t in tokens:
                        sym = (t.get("symbol") or "").upper()
                        addr = (t.get("address") or t.get("tokenAddress") or "").lower()
                        bal = float(t.get("balance") or t.get("amount") or t.get("formattedBalance") or 0)
                        if sym == "BOTCOIN" or addr == "0xa601877977340862ca67f816eb079958e5bd0ba3":
                            state.wallet_botcoin = bal
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
            import os
            host = "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID") or os.environ.get("PORT") else "127.0.0.1"
            self._app.run(host=host, port=port, threaded=True, use_reloader=False)
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
