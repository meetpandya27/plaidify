"""Internal browser test fixture portal for Playwright integration tests."""

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI(title="Plaidify Internal Portal Fixture")
app.add_middleware(SessionMiddleware, secret_key="plaidify-internal-fixture")

VALID_USERS = {
    "test_user": {"password": "test_pass", "requires_mfa": False},
    "fixture_mfa": {"password": "test_pass", "requires_mfa": True},
}
MFA_CODE = "123456"

PAGE_STYLE = """
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f4f7fb; color: #142033; margin: 0; }
  .wrap { max-width: 980px; margin: 0 auto; padding: 24px; }
  .card { background: white; border-radius: 16px; box-shadow: 0 6px 24px rgba(18, 32, 51, 0.08); padding: 24px; margin-bottom: 20px; }
  .grid { display: grid; gap: 18px; grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .stat { text-align: center; }
  .value { color: #0b8f73; font-size: 28px; font-weight: 700; }
  .label { color: #5a6576; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; }
  table { width: 100%; border-collapse: collapse; }
  th, td { border-bottom: 1px solid #e6ebf2; padding: 10px 12px; text-align: left; }
  input { width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #d5ddea; margin-bottom: 12px; }
  button { width: 100%; padding: 12px; border-radius: 999px; background: #0b8f73; color: white; border: none; font-weight: 700; }
  .topbar { background: #142033; color: white; padding: 14px 24px; }
  .error { color: #c2410c; min-height: 20px; }
</style>
"""


def _login_html(error: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1' /><title>Internal Portal Sign In</title>{PAGE_STYLE}</head>
<body>
  <div class='topbar'>Plaidify Internal Portal Fixture</div>
  <div class='wrap'>
    <div class='card' style='max-width: 420px; margin: 80px auto;'>
      <h1>Sign in</h1>
      <p class='error'>{error}</p>
      <form action='/login' method='post'>
        <label for='username'>Username</label>
        <input type='text' name='username' id='username' autocomplete='off' />
        <label for='password'>Password</label>
        <input type='password' name='password' id='password' autocomplete='off' />
        <button type='submit' id='login-btn'>Continue</button>
      </form>
    </div>
  </div>
</body></html>"""


def _mfa_html(error: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1' /><title>Internal Portal Verification</title>{PAGE_STYLE}</head>
<body>
  <div class='topbar'>Plaidify Internal Portal Fixture</div>
  <div class='wrap'>
    <div class='card' style='max-width: 420px; margin: 80px auto;'>
      <h1>Verify your identity</h1>
      <p class='error'>{error}</p>
      <form action='/mfa' method='post'>
        <label for='otp-input'>Verification Code</label>
        <input type='text' name='code' id='otp-input' autocomplete='off' />
        <button type='submit' id='otp-submit'>Verify</button>
      </form>
    </div>
  </div>
</body></html>"""


def _dashboard_html(username: str) -> str:
    return f"""<!DOCTYPE html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1' /><title>Internal Portal Dashboard</title>{PAGE_STYLE}</head>
<body>
  <div class='topbar'>Plaidify Internal Portal Fixture <a href='/logout' id='logout-btn' style='color:white; float:right;'>Sign Out</a></div>
  <div class='wrap'>
    <h1 id='dashboard'>Account Overview</h1>
    <div class='card'>
      <div class='grid'>
        <div class='stat'><div class='value' id='current-bill'>$142.57</div><div class='label'>Current Bill</div></div>
        <div class='stat'><div class='value' id='usage-kwh'>847 kWh</div><div class='label'>Usage</div></div>
        <div class='stat'><div class='value' id='account-number'>INT-2847591</div><div class='label'>Account Number</div></div>
        <div class='stat'><div class='value' id='account-status'>Active</div><div class='label'>Account Status</div></div>
      </div>
    </div>
    <div class='card'>
      <div class='grid'>
        <div class='stat'><div class='value' style='font-size:18px' id='service-address'>742 Evergreen Terrace</div><div class='label'>Service Address</div></div>
        <div class='stat'><div class='value' style='font-size:18px' id='plan-name'>Operational Saver</div><div class='label'>Plan</div></div>
        <div class='stat'><div class='value' style='font-size:18px' id='meter-id'>MTR-00458821</div><div class='label'>Meter ID</div></div>
        <div class='stat'><div class='value' style='font-size:18px' id='next-read-date'>03/28/2026</div><div class='label'>Next Read</div></div>
      </div>
      <div style='margin-top: 18px;'>
        <div id='customer-name'>Alex Johnson</div>
        <div id='customer-email'>{username}@internal.test</div>
      </div>
    </div>
    <div class='card'>
      <h2>Usage History</h2>
      <table id='usage-table'>
        <thead><tr><th>Month</th><th>Usage (kWh)</th><th>Cost</th><th>Avg Temp</th></tr></thead>
        <tbody>
          <tr class='usage-row'><td class='usage-month'>March 2026</td><td class='usage-kwh-val'>847</td><td class='usage-cost'>$142.57</td><td class='usage-temp'>52°F</td></tr>
          <tr class='usage-row'><td class='usage-month'>February 2026</td><td class='usage-kwh-val'>1,124</td><td class='usage-cost'>$189.32</td><td class='usage-temp'>38°F</td></tr>
          <tr class='usage-row'><td class='usage-month'>January 2026</td><td class='usage-kwh-val'>1,008</td><td class='usage-cost'>$171.08</td><td class='usage-temp'>41°F</td></tr>
          <tr class='usage-row'><td class='usage-month'>December 2025</td><td class='usage-kwh-val'>1,228</td><td class='usage-cost'>$206.41</td><td class='usage-temp'>29°F</td></tr>
          <tr class='usage-row'><td class='usage-month'>November 2025</td><td class='usage-kwh-val'>935</td><td class='usage-cost'>$154.90</td><td class='usage-temp'>47°F</td></tr>
          <tr class='usage-row'><td class='usage-month'>October 2025</td><td class='usage-kwh-val'>810</td><td class='usage-cost'>$132.88</td><td class='usage-temp'>58°F</td></tr>
        </tbody>
      </table>
    </div>
    <div class='card'>
      <h2>Recent Payments</h2>
      <table id='payment-table'>
        <thead><tr><th>Date</th><th>Description</th><th>Amount</th><th>Status</th></tr></thead>
        <tbody>
          <tr class='payment-row'><td class='payment-date'>2026-03-04</td><td class='payment-desc'>February Bill Payment</td><td class='payment-amount'>$158.83</td><td class='payment-status'>Completed</td></tr>
          <tr class='payment-row'><td class='payment-date'>2026-02-03</td><td class='payment-desc'>January Bill Payment</td><td class='payment-amount'>$171.08</td><td class='payment-status'>Completed</td></tr>
          <tr class='payment-row'><td class='payment-date'>2026-01-05</td><td class='payment-desc'>December Bill Payment</td><td class='payment-amount'>$206.41</td><td class='payment-status'>Completed</td></tr>
          <tr class='payment-row'><td class='payment-date'>2025-12-04</td><td class='payment-desc'>November Bill Payment</td><td class='payment-amount'>$154.90</td><td class='payment-status'>Completed</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</body></html>"""


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


@app.get("/")
async def root():
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(_login_html())


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    entry = VALID_USERS.get(username)
    if not entry or entry["password"] != password:
        return HTMLResponse(_login_html("Invalid credentials."), status_code=401)

    request.session["username"] = username
    request.session["authenticated"] = not entry["requires_mfa"]
    if entry["requires_mfa"]:
        request.session["pending_mfa"] = True
        return RedirectResponse("/mfa", status_code=303)

    return RedirectResponse("/dashboard", status_code=303)


@app.get("/mfa", response_class=HTMLResponse)
async def mfa_page(request: Request):
    if not request.session.get("pending_mfa"):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_mfa_html())


@app.post("/mfa")
async def mfa_submit(request: Request, code: str = Form(...)):
    if not request.session.get("pending_mfa"):
        return RedirectResponse("/login", status_code=303)
    if code != MFA_CODE:
        return HTMLResponse(_mfa_html("Invalid verification code."), status_code=401)

    request.session["pending_mfa"] = False
    request.session["authenticated"] = True
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_dashboard_html(request.session.get("username", "test_user")))


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
