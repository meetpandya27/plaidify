"""Acme Bank — a demo target site with a deliberately *different* structure.

Where GreenGrid (src/demo/portal.py) uses username + OTP and a stat-grid
dashboard, Acme Bank uses **email** login, a **security-question** MFA challenge,
and an account-card + transactions-table layout. This diversity is intentional:
it demonstrates that Plaidify connectors handle very different page structures,
login flows, and MFA types.

Run standalone::

    DEMO_BANK_PORT=8798 python -m src.demo.bank

Demo credentials:
    demo@acme.test / demo_pass   (security answer: plaidify)
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

VALID_USERS = {
    "demo@acme.test": {"password": "demo_pass"},
}
SECURITY_QUESTION = "What was the name of your first pet?"
SECURITY_ANSWER = "plaidify"
BRAND = "Acme Bank"

STYLE = """
<style>
  :root { --navy:#0a2540; --acc:#635bff; --muted:#697386; --line:#e3e8ee; }
  * { box-sizing: border-box; }
  body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#f6f9fc; color:var(--navy); margin:0; }
  .nav { background:var(--navy); color:#fff; padding:16px 28px; display:flex; align-items:center; gap:10px; }
  .nav .pill { background:var(--acc); border-radius:6px; font-size:11px; padding:2px 8px; letter-spacing:.04em; text-transform:uppercase; }
  .container { max-width:880px; margin:0 auto; padding:28px; }
  .panel { background:#fff; border:1px solid var(--line); border-radius:12px; padding:26px; margin-bottom:18px; box-shadow:0 1px 3px rgba(10,37,64,.06); }
  .auth { max-width:400px; margin:64px auto; }
  label { display:block; font-size:13px; font-weight:600; margin:14px 0 6px; }
  input { width:100%; padding:11px 13px; border:1px solid var(--line); border-radius:8px; font-size:15px; }
  button { width:100%; margin-top:20px; padding:12px; background:var(--acc); color:#fff; border:none; border-radius:8px; font-weight:600; font-size:15px; cursor:pointer; }
  .err { color:#cd3d64; font-size:13px; min-height:18px; margin-top:8px; }
  .accounts { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  .acct { border:1px solid var(--line); border-radius:10px; padding:18px; }
  .acct .type { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
  .acct .bal { font-size:26px; font-weight:700; margin-top:6px; }
  .acct .no { font-size:12px; color:var(--muted); margin-top:4px; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th { text-align:left; color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; padding:8px 10px; border-bottom:2px solid var(--line); }
  td { padding:10px; border-bottom:1px solid var(--line); }
  .amt-neg { color:#cd3d64; } .amt-pos { color:#1a7f53; }
  .hint { color:var(--muted); font-size:13px; margin-top:14px; }
  h1 { font-size:22px; }
</style>
"""


def _login_html(error: str = "") -> str:
    return f"""<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'><title>{BRAND} — Online Banking</title>{STYLE}</head>
<body><div class='nav'><strong>{BRAND}</strong><span class='pill'>Sandbox</span></div>
<div class='container'><div class='panel auth'>
  <h1>Online Banking</h1>
  <p class='err'>{error}</p>
  <form action='/auth' method='post'>
    <label for='email'>Email address</label>
    <input type='email' id='email' name='email' autocomplete='off'>
    <label for='passcode'>Password</label>
    <input type='password' id='passcode' name='passcode' autocomplete='off'>
    <button type='submit' id='signin'>Sign in securely</button>
  </form>
  <p class='hint'>Demo: <code>demo@acme.test</code> / <code>demo_pass</code> · security answer <code>plaidify</code></p>
</div></div></body></html>"""


def _verify_html(error: str = "") -> str:
    return f"""<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'><title>{BRAND} — Verify</title>{STYLE}</head>
<body><div class='nav'><strong>{BRAND}</strong><span class='pill'>Sandbox</span></div>
<div class='container'><div class='panel auth'>
  <h1>Security verification</h1>
  <p id='mfa-question' style='color:var(--muted)'>{SECURITY_QUESTION}</p>
  <p class='err'>{error}</p>
  <form action='/verify' method='post'>
    <label for='security-answer'>Your answer</label>
    <input type='text' id='security-answer' name='answer' autocomplete='off'>
    <button type='submit' id='verify-btn'>Verify identity</button>
  </form>
  <p class='hint'>Demo answer: <code>plaidify</code></p>
</div></div></body></html>"""


def _accounts_html() -> str:
    return f"""<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'><title>{BRAND} — Accounts</title>{STYLE}</head>
<body><div class='nav'><strong>{BRAND}</strong><span class='pill'>Sandbox</span>
  <a href='/signout' id='signout' style='color:#fff;margin-left:auto'>Sign out</a></div>
<div class='container'>
  <h1 id='accounts-page'>Your accounts</h1>
  <div class='panel'>
    <div class='accounts'>
      <div class='acct' id='acct-checking'>
        <div class='type'>Checking</div>
        <div class='bal' id='checking-balance'>$4,820.55</div>
        <div class='no' id='checking-number'>•••• 4471</div>
      </div>
      <div class='acct' id='acct-savings'>
        <div class='type'>Savings</div>
        <div class='bal' id='savings-balance'>$18,340.12</div>
        <div class='no' id='savings-number'>•••• 9920</div>
      </div>
    </div>
    <div style='margin-top:16px'>
      <span id='account-holder'>Jordan Rivera</span> ·
      <span id='holder-email'>demo@acme.test</span> ·
      <span id='member-since'>Member since 2019</span>
    </div>
  </div>
  <div class='panel'>
    <h2 style='font-size:16px'>Recent transactions</h2>
    <table id='transactions'>
      <thead><tr><th>Date</th><th>Merchant</th><th>Category</th><th>Amount</th></tr></thead>
      <tbody>
        <tr class='txn-row'><td class='txn-date'>2026-06-18</td><td class='txn-merchant'>Whole Foods Market</td><td class='txn-category'>Groceries</td><td class='txn-amount amt-neg'>-$84.21</td></tr>
        <tr class='txn-row'><td class='txn-date'>2026-06-17</td><td class='txn-merchant'>Shell Gas</td><td class='txn-category'>Auto</td><td class='txn-amount amt-neg'>-$52.40</td></tr>
        <tr class='txn-row'><td class='txn-date'>2026-06-15</td><td class='txn-merchant'>Payroll Deposit</td><td class='txn-category'>Income</td><td class='txn-amount amt-pos'>+$3,200.00</td></tr>
        <tr class='txn-row'><td class='txn-date'>2026-06-14</td><td class='txn-merchant'>Netflix</td><td class='txn-category'>Entertainment</td><td class='txn-amount amt-neg'>-$15.49</td></tr>
        <tr class='txn-row'><td class='txn-date'>2026-06-12</td><td class='txn-merchant'>Transfer to Savings</td><td class='txn-category'>Transfer</td><td class='txn-amount amt-neg'>-$500.00</td></tr>
      </tbody>
    </table>
  </div>
</div></body></html>"""


def create_app() -> FastAPI:
    app = FastAPI(title=f"Plaidify Demo Portal — {BRAND}")
    app.add_middleware(SessionMiddleware, secret_key="plaidify-demo-bank", session_cookie="acme_session")

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok"})

    @app.get("/")
    async def root():
        return RedirectResponse("/login", status_code=302)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return HTMLResponse(_login_html())

    @app.post("/auth")
    async def auth(request: Request, email: str = Form(...), passcode: str = Form(...)):
        entry = VALID_USERS.get(email)
        if not entry or entry["password"] != passcode:
            return HTMLResponse(_login_html("We couldn't verify those details."), status_code=401)
        request.session.update({"email": email, "verified": False, "pending": True})
        return RedirectResponse("/verify", status_code=303)

    @app.get("/verify", response_class=HTMLResponse)
    async def verify_page(request: Request):
        if not request.session.get("pending"):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(_verify_html())

    @app.post("/verify")
    async def verify(request: Request, answer: str = Form(...)):
        if not request.session.get("pending"):
            return RedirectResponse("/login", status_code=303)
        if answer.strip().lower() != SECURITY_ANSWER:
            return HTMLResponse(_verify_html("That answer doesn't match our records."), status_code=401)
        request.session.update({"verified": True, "pending": False})
        return RedirectResponse("/accounts", status_code=303)

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts(request: Request):
        if not request.session.get("verified"):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(_accounts_html())

    @app.get("/signout")
    async def signout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("DEMO_BANK_HOST", "127.0.0.1")
    port = int(os.environ.get("DEMO_BANK_PORT", "8798"))
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
