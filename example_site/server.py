"""
Example test site for Plaidify's Playwright engine.

Provides a realistic login flow with:
- Login form with username/password
- Optional MFA (OTP) page
- Dashboard with structured data (account info, transactions)
- Logout

Credentials:
  Normal:  test_user / test_pass
  MFA:     mfa_user / mfa_pass  (then OTP code: 123456)
  Bad:     anything else → 401
"""

import uvicorn
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI(title="Plaidify Test Site")
app.add_middleware(SessionMiddleware, secret_key="test-site-secret-key")


# ── Valid Credentials ─────────────────────────────────────────────────────────

VALID_USERS = {
    "test_user": "test_pass",
    "mfa_user": "mfa_pass",
    "mock_user": "mock_password",  # backward-compat
}
MFA_CODE = "123456"


# ── HTML Templates ────────────────────────────────────────────────────────────

HTML_LOGIN = """
<!DOCTYPE html>
<html>
<head><title>Test Bank - Login</title></head>
<body>
  <h1 id="page-title">Test Bank Login</h1>
  <form action="/login" method="post">
    <label for="username">Username:</label>
    <input type="text" name="username" id="username" />
    <br/>
    <label for="password">Password:</label>
    <input type="password" name="password" id="password" />
    <br/>
    <button type="submit" id="login-btn">Log In</button>
  </form>
  <div id="error" style="color:red;">{error}</div>
</body>
</html>
"""

HTML_MFA = """
<!DOCTYPE html>
<html>
<head><title>Test Bank - Verify Identity</title></head>
<body>
  <h1>Two-Factor Authentication</h1>
  <p>We sent a code to your phone. Enter it below.</p>
  <form action="/mfa" method="post">
    <label for="otp-code">Enter code:</label>
    <input type="text" name="code" id="otp-input" maxlength="6" />
    <br/>
    <button type="submit" id="otp-submit">Verify</button>
  </form>
  <div id="mfa-error" style="color:red;">{error}</div>
</body>
</html>
"""

HTML_DASHBOARD = """
<!DOCTYPE html>
<html>
<head><title>Test Bank - Dashboard</title></head>
<body>
  <h1 id="dashboard">Welcome, {username}!</h1>

  <div id="account-section">
    <h2>Account Overview</h2>
    <div id="account-balance">$4,521.30</div>
    <div id="account-number">****7890</div>
    <div id="account-status">active</div>
    <div id="last-synced">03/14/2026</div>
  </div>

  <div id="transactions-section">
    <h2>Recent Transactions</h2>
    <table id="transaction-table">
      <thead>
        <tr><th>Date</th><th>Description</th><th>Amount</th></tr>
      </thead>
      <tbody>
        <tr class="txn-row">
          <td class="txn-date">03/14/2026</td>
          <td class="txn-desc">Coffee Shop</td>
          <td class="txn-amount">-$4.50</td>
        </tr>
        <tr class="txn-row">
          <td class="txn-date">03/13/2026</td>
          <td class="txn-desc">Direct Deposit - Payroll</td>
          <td class="txn-amount">$3,200.00</td>
        </tr>
        <tr class="txn-row">
          <td class="txn-date">03/12/2026</td>
          <td class="txn-desc">Electric Bill - PG&amp;E</td>
          <td class="txn-amount">-$127.45</td>
        </tr>
        <tr class="txn-row">
          <td class="txn-date">03/11/2026</td>
          <td class="txn-desc">Grocery Store</td>
          <td class="txn-amount">-$89.23</td>
        </tr>
        <tr class="txn-row">
          <td class="txn-date">03/10/2026</td>
          <td class="txn-desc">ATM Withdrawal</td>
          <td class="txn-amount">-$200.00</td>
        </tr>
      </tbody>
    </table>
  </div>

  <div id="profile-section">
    <h2>Profile</h2>
    <div id="profile-name">John Doe</div>
    <div id="profile-email">john.doe@example.com</div>
    <div id="profile-phone">(555) 123-4567</div>
  </div>

  <a href="/logout" id="logout-btn">Logout</a>
</body>
</html>
"""


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def home():
    return RedirectResponse(url="/login")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return HTML_LOGIN.format(error="")


@app.post("/login", response_class=HTMLResponse)
async def login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if VALID_USERS.get(username) != password:
        return HTMLResponse(
            HTML_LOGIN.format(error="Invalid username or password."),
            status_code=200,  # Playwright can read the page
        )

    request.session["username"] = username

    # MFA user gets redirected to OTP page
    if username == "mfa_user":
        request.session["mfa_pending"] = True
        return RedirectResponse(url="/mfa", status_code=302)

    request.session["logged_in"] = True
    return RedirectResponse(url="/dashboard", status_code=302)


@app.get("/mfa", response_class=HTMLResponse)
async def mfa_page(request: Request):
    if not request.session.get("mfa_pending"):
        return RedirectResponse(url="/login")
    return HTML_MFA.format(error="")


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
            HTML_MFA.format(error="Invalid code. Try again."),
            status_code=200,
        )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not request.session.get("logged_in"):
        return RedirectResponse(url="/login")
    username = request.session.get("username", "User")
    return HTML_DASHBOARD.format(username=username)


@app.get("/logout", response_class=HTMLResponse)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Entry Point ───────────────────────────────────────────────────────────────


def run_server(host: str = "0.0.0.0", port: int = 8080):
    """Start the test site server."""
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server()