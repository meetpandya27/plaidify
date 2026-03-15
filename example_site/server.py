"""
Example utility company portal for Plaidify demo.

Simulates "GreenGrid Energy" — a fictional utility provider with:
- Login form (username/password)
- Optional MFA (OTP)
- Dashboard with account overview, current bill, usage history, payments
- Logout

Credentials:
  Normal:  demo_user / demo_pass
  MFA:     mfa_user / mfa_pass   (OTP code: 123456)
  Legacy:  test_user / test_pass  (backward-compat)

Run:  python example_site/server.py
"""

import uvicorn
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI(title="GreenGrid Energy — Customer Portal")
app.add_middleware(SessionMiddleware, secret_key="greengrid-demo-secret")


# ── Valid Credentials ─────────────────────────────────────────────────────────

VALID_USERS = {
    "demo_user": "demo_pass",
    "mfa_user": "mfa_pass",
    "test_user": "test_pass",
    "mock_user": "mock_password",
}
MFA_CODE = "123456"


# ── Shared Styles ─────────────────────────────────────────────────────────────

STYLES = """
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f0f4f0; color: #1a2e1a; min-height: 100vh;
  }
  .topbar {
    background: linear-gradient(135deg, #1a6b3c, #2d9a5b);
    color: white; padding: 14px 24px; display: flex;
    align-items: center; justify-content: space-between;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15);
  }
  .topbar .logo { font-size: 18px; font-weight: 700; letter-spacing: -0.3px; }
  .topbar .logo span { color: #a8e6c0; }
  .topbar a { color: #c8f0d8; text-decoration: none; font-size: 14px; }
  .container { max-width: 960px; margin: 0 auto; padding: 32px 20px; }
  .card {
    background: white; border-radius: 12px; padding: 28px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08); margin-bottom: 24px;
  }
  .card h2 { font-size: 16px; color: #555; margin-bottom: 16px;
    text-transform: uppercase; letter-spacing: 0.5px; }
  .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 20px; }
  .stat { text-align: center; }
  .stat .value { font-size: 28px; font-weight: 700; color: #1a6b3c; }
  .stat .label { font-size: 12px; color: #888; margin-top: 4px; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-size: 12px; color: #888; padding: 8px 12px;
    border-bottom: 2px solid #eee; text-transform: uppercase; }
  td { padding: 10px 12px; border-bottom: 1px solid #f0f0f0; font-size: 14px; }
  .amount-negative { color: #c0392b; font-weight: 600; }
  .badge {
    display: inline-block; padding: 2px 10px; border-radius: 20px;
    font-size: 12px; font-weight: 600;
  }
  .badge-active { background: #d4edda; color: #155724; }
  .badge-paid { background: #d4edda; color: #155724; }
  input, button { font-family: inherit; }
  .login-wrap {
    min-height: calc(100vh - 56px); display: flex; align-items: center;
    justify-content: center; padding: 20px;
  }
  .login-card {
    background: white; border-radius: 16px; padding: 40px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.1); width: 100%; max-width: 400px;
  }
  .login-card h1 { font-size: 22px; margin-bottom: 4px; color: #1a2e1a; }
  .login-card .subtitle { color: #888; font-size: 14px; margin-bottom: 24px; }
  .login-card label { display: block; font-size: 13px; font-weight: 600;
    color: #555; margin-bottom: 6px; }
  .login-card input[type="text"],
  .login-card input[type="password"] {
    width: 100%; padding: 10px 14px; border: 1.5px solid #ddd;
    border-radius: 8px; font-size: 15px; margin-bottom: 16px;
    transition: border-color 0.2s;
  }
  .login-card input:focus { outline: none; border-color: #2d9a5b; }
  .login-card button[type="submit"] {
    width: 100%; padding: 12px; background: linear-gradient(135deg, #1a6b3c, #2d9a5b);
    color: white; border: none; border-radius: 8px; font-size: 15px;
    font-weight: 600; cursor: pointer; transition: opacity 0.2s;
  }
  .login-card button:hover { opacity: 0.9; }
  .error-msg { color: #c0392b; font-size: 13px; margin-bottom: 12px; min-height: 18px; }
</style>
"""


# ── HTML Templates ────────────────────────────────────────────────────────────

HTML_LOGIN = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8" />'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0" />'
    "<title>GreenGrid Energy — Sign In</title>"
    + STYLES
    + """
</head>
<body>
  <div class="topbar">
    <div class="logo" id="page-title">⚡ Green<span>Grid</span> Energy</div>
    <span style="font-size:13px; opacity:0.7;">Customer Portal</span>
  </div>
  <div class="login-wrap">
    <div class="login-card">
      <h1>Sign in to your account</h1>
      <p class="subtitle">Manage your energy usage and billing</p>
      <div class="error-msg">__ERROR__</div>
      <form action="/login" method="post">
        <label for="username">Email or Account ID</label>
        <input type="text" name="username" id="username" placeholder="demo_user" autocomplete="off" />
        <label for="password">Password</label>
        <input type="password" name="password" id="password" placeholder="demo_pass" autocomplete="off" />
        <button type="submit" id="login-btn">Sign In</button>
      </form>
    </div>
  </div>
</body>
</html>
"""
)

HTML_MFA = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8" />'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0" />'
    "<title>GreenGrid Energy — Verify Identity</title>"
    + STYLES
    + """
</head>
<body>
  <div class="topbar">
    <div class="logo">⚡ Green<span>Grid</span> Energy</div>
    <span style="font-size:13px; opacity:0.7;">Security Verification</span>
  </div>
  <div class="login-wrap">
    <div class="login-card">
      <h1>Verify your identity</h1>
      <p class="subtitle">We sent a 6-digit code to your phone ending in ••42</p>
      <div class="error-msg">__ERROR__</div>
      <form action="/mfa" method="post">
        <label for="otp-code">Verification Code</label>
        <input type="text" name="code" id="otp-input" maxlength="6"
               placeholder="Enter 6-digit code" autocomplete="off" />
        <button type="submit" id="otp-submit">Verify</button>
      </form>
    </div>
  </div>
</body>
</html>
"""
)

HTML_DASHBOARD = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8" />'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0" />'
    "<title>GreenGrid Energy — Dashboard</title>"
    + STYLES
    + """
</head>
<body>
  <div class="topbar">
    <div class="logo">⚡ Green<span>Grid</span> Energy</div>
    <div>
      <span style="font-size:13px; margin-right:16px;">Welcome, __DISPLAY_NAME__</span>
      <a href="/logout" id="logout-btn">Sign Out</a>
    </div>
  </div>

  <div class="container">
    <h1 id="dashboard" style="font-size:24px; margin-bottom:24px;">
      Your Energy Dashboard
    </h1>

    <!-- Account Overview -->
    <div class="card">
      <h2>Account Overview</h2>
      <div class="stat-grid">
        <div class="stat">
          <div class="value" id="current-bill">$142.57</div>
          <div class="label">Current Bill</div>
        </div>
        <div class="stat">
          <div class="value" id="usage-kwh">847 kWh</div>
          <div class="label">This Month Usage</div>
        </div>
        <div class="stat">
          <div class="value" id="account-number">GG-2847591</div>
          <div class="label">Account Number</div>
        </div>
        <div class="stat">
          <div class="value" id="account-status">
            <span class="badge badge-active">Active</span>
          </div>
          <div class="label">Account Status</div>
        </div>
      </div>
    </div>

    <!-- Service Details -->
    <div class="card">
      <h2>Service Details</h2>
      <div class="stat-grid">
        <div class="stat">
          <div class="value" style="font-size:18px;" id="service-address">742 Evergreen Terrace</div>
          <div class="label">Service Address</div>
        </div>
        <div class="stat">
          <div class="value" style="font-size:18px;" id="plan-name">EcoSaver Plus</div>
          <div class="label">Rate Plan</div>
        </div>
        <div class="stat">
          <div class="value" style="font-size:18px;" id="meter-id">MTR-00458821</div>
          <div class="label">Meter ID</div>
        </div>
        <div class="stat">
          <div class="value" style="font-size:18px;" id="next-read-date">03/28/2026</div>
          <div class="label">Next Meter Read</div>
        </div>
      </div>
    </div>

    <!-- Monthly Usage History -->
    <div class="card">
      <h2>Monthly Usage History</h2>
      <table id="usage-table">
        <thead>
          <tr><th>Month</th><th>Usage (kWh)</th><th>Cost</th><th>Avg Temp</th></tr>
        </thead>
        <tbody>
          <tr class="usage-row">
            <td class="usage-month">March 2026</td>
            <td class="usage-kwh-val">847</td>
            <td class="usage-cost">$142.57</td>
            <td class="usage-temp">52°F</td>
          </tr>
          <tr class="usage-row">
            <td class="usage-month">February 2026</td>
            <td class="usage-kwh-val">1,124</td>
            <td class="usage-cost">$189.32</td>
            <td class="usage-temp">38°F</td>
          </tr>
          <tr class="usage-row">
            <td class="usage-month">January 2026</td>
            <td class="usage-kwh-val">1,302</td>
            <td class="usage-cost">$218.94</td>
            <td class="usage-temp">31°F</td>
          </tr>
          <tr class="usage-row">
            <td class="usage-month">December 2025</td>
            <td class="usage-kwh-val">1,198</td>
            <td class="usage-cost">$201.43</td>
            <td class="usage-temp">35°F</td>
          </tr>
          <tr class="usage-row">
            <td class="usage-month">November 2025</td>
            <td class="usage-kwh-val">876</td>
            <td class="usage-cost">$147.38</td>
            <td class="usage-temp">48°F</td>
          </tr>
          <tr class="usage-row">
            <td class="usage-month">October 2025</td>
            <td class="usage-kwh-val">654</td>
            <td class="usage-cost">$110.07</td>
            <td class="usage-temp">62°F</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Payment History -->
    <div class="card">
      <h2>Payment History</h2>
      <table id="payment-table">
        <thead>
          <tr><th>Date</th><th>Description</th><th>Amount</th><th>Status</th></tr>
        </thead>
        <tbody>
          <tr class="payment-row">
            <td class="payment-date">03/01/2026</td>
            <td class="payment-desc">Auto-Pay — February Bill</td>
            <td class="payment-amount">-$189.32</td>
            <td class="payment-status"><span class="badge badge-paid">Paid</span></td>
          </tr>
          <tr class="payment-row">
            <td class="payment-date">02/01/2026</td>
            <td class="payment-desc">Auto-Pay — January Bill</td>
            <td class="payment-amount">-$218.94</td>
            <td class="payment-status"><span class="badge badge-paid">Paid</span></td>
          </tr>
          <tr class="payment-row">
            <td class="payment-date">01/02/2026</td>
            <td class="payment-desc">Auto-Pay — December Bill</td>
            <td class="payment-amount">-$201.43</td>
            <td class="payment-status"><span class="badge badge-paid">Paid</span></td>
          </tr>
          <tr class="payment-row">
            <td class="payment-date">12/01/2025</td>
            <td class="payment-desc">Auto-Pay — November Bill</td>
            <td class="payment-amount">-$147.38</td>
            <td class="payment-status"><span class="badge badge-paid">Paid</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Customer Info -->
    <div class="card">
      <h2>Customer Information</h2>
      <div class="stat-grid">
        <div class="stat">
          <div class="value" style="font-size:18px;" id="customer-name">Alex Johnson</div>
          <div class="label">Name</div>
        </div>
        <div class="stat">
          <div class="value" style="font-size:18px;" id="customer-email">alex.johnson@email.com</div>
          <div class="label">Email</div>
        </div>
        <div class="stat">
          <div class="value" style="font-size:18px;" id="customer-phone">(555) 867-5309</div>
          <div class="label">Phone</div>
        </div>
        <div class="stat">
          <div class="value" style="font-size:18px;" id="customer-since">June 2019</div>
          <div class="label">Customer Since</div>
        </div>
      </div>
    </div>

  </div>
</body>
</html>
"""
)


# ── User Display Names ────────────────────────────────────────────────────────

USER_DISPLAY = {
    "demo_user": "Alex",
    "mfa_user": "Alex",
    "test_user": "Alex",
    "mock_user": "Alex",
}


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def home():
    return RedirectResponse(url="/login")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return HTML_LOGIN.replace("__ERROR__", "")


@app.post("/login", response_class=HTMLResponse)
async def login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if VALID_USERS.get(username) != password:
        return HTMLResponse(
            HTML_LOGIN.replace("__ERROR__", "Invalid email or password."),
            status_code=200,
        )

    request.session["username"] = username

    if username == "mfa_user":
        request.session["mfa_pending"] = True
        return RedirectResponse(url="/mfa", status_code=302)

    request.session["logged_in"] = True
    return RedirectResponse(url="/dashboard", status_code=302)


@app.get("/mfa", response_class=HTMLResponse)
async def mfa_page(request: Request):
    if not request.session.get("mfa_pending"):
        return RedirectResponse(url="/login")
    return HTML_MFA.replace("__ERROR__", "")


@app.post("/mfa", response_class=HTMLResponse)
async def mfa_action(request: Request, code: str = Form(...)):
    if not request.session.get("mfa_pending"):
        return RedirectResponse(url="/login")

    if code == MFA_CODE:
        request.session.pop("mfa_pending", None)
        request.session["logged_in"] = True
        return RedirectResponse(url="/dashboard", status_code=302)
    else:
        return HTMLResponse(
            HTML_MFA.replace("__ERROR__", "Invalid code. Please try again."),
            status_code=200,
        )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not request.session.get("logged_in"):
        return RedirectResponse(url="/login")
    username = request.session.get("username", "User")
    display_name = USER_DISPLAY.get(username, username)
    return HTML_DASHBOARD.replace("__DISPLAY_NAME__", display_name)


@app.get("/logout", response_class=HTMLResponse)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Entry Point ───────────────────────────────────────────────────────────────


def run_server(host: str = "0.0.0.0", port: int = 8080):
    """Start the GreenGrid Energy portal."""
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server()
