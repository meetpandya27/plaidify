"""CloudMail — a demo target site with a third distinct structure.

CloudMail is a SaaS workspace: **username** login, **no MFA** at all (some real
portals skip it), and a profile + storage-meter + activity layout. Together with
GreenGrid (OTP) and Acme Bank (security question), it shows Plaidify handling a
spread of login flows, MFA types, and DOM shapes from one engine.

Run standalone::

    DEMO_SAAS_PORT=8797 python -m src.demo.saas

Demo credentials:
    demo_saas / demo_pass   (no MFA)
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

VALID_USERS = {
    "demo_saas": {"password": "demo_pass"},
}
BRAND = "CloudMail"

STYLE = """
<style>
  * { box-sizing:border-box; }
  body { font-family:'Segoe UI', system-ui, -apple-system, sans-serif; margin:0; background:#0f172a; color:#e2e8f0; }
  .top { background:#1e293b; padding:14px 26px; display:flex; align-items:center; gap:10px; border-bottom:1px solid #334155; }
  .tag { background:#22d3ee; color:#083344; font-size:11px; font-weight:700; padding:2px 8px; border-radius:5px; text-transform:uppercase; }
  .wrap { max-width:820px; margin:0 auto; padding:28px; }
  .box { background:#1e293b; border:1px solid #334155; border-radius:14px; padding:24px; margin-bottom:18px; }
  .signin { max-width:380px; margin:60px auto; }
  label { display:block; font-size:13px; color:#94a3b8; margin:14px 0 6px; }
  input { width:100%; padding:11px 13px; background:#0f172a; border:1px solid #334155; border-radius:9px; color:#e2e8f0; font-size:15px; }
  button { width:100%; margin-top:20px; padding:12px; background:linear-gradient(90deg,#22d3ee,#6366f1); color:#fff; border:none; border-radius:9px; font-weight:700; cursor:pointer; }
  .err { color:#fb7185; font-size:13px; min-height:18px; }
  .cols { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  .kv { color:#94a3b8; font-size:13px; } .kv b { color:#e2e8f0; }
  .meter { height:10px; background:#0f172a; border-radius:6px; overflow:hidden; margin-top:8px; }
  .meter > span { display:block; height:100%; background:linear-gradient(90deg,#22d3ee,#6366f1); }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:#94a3b8; font-weight:600; padding:8px; border-bottom:1px solid #334155; }
  td { padding:9px 8px; border-bottom:1px solid #243049; }
  .hint { color:#64748b; font-size:13px; margin-top:14px; }
  h1 { font-size:22px; }
</style>
"""


def _login_html(error: str = "") -> str:
    return f"""<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'><title>{BRAND} — Sign in</title>{STYLE}</head>
<body><div class='top'><strong>{BRAND}</strong><span class='tag'>Sandbox</span></div>
<div class='wrap'><div class='box signin'>
  <h1>Welcome back</h1>
  <p class='err'>{error}</p>
  <form action='/signin' method='post'>
    <label for='user'>Username</label>
    <input type='text' id='user' name='user' autocomplete='off'>
    <label for='pass'>Password</label>
    <input type='password' id='pass' name='pass' autocomplete='off'>
    <button type='submit' id='submit-login'>Sign in</button>
  </form>
  <p class='hint'>Demo: <code>demo_saas</code> / <code>demo_pass</code> (no MFA)</p>
</div></div></body></html>"""


def _workspace_html() -> str:
    return f"""<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'><title>{BRAND} — Workspace</title>{STYLE}</head>
<body><div class='top'><strong>{BRAND}</strong><span class='tag'>Sandbox</span>
  <a href='/logout' id='logout' style='color:#e2e8f0;margin-left:auto'>Log out</a></div>
<div class='wrap'>
  <h1 id='workspace'>Workspace</h1>
  <div class='box'>
    <div class='cols'>
      <div>
        <div class='kv'>Account holder<br><b id='display-name'>Priya Sharma</b></div>
        <div class='kv' style='margin-top:10px'>Plan<br><b id='plan-tier'>Business Pro</b></div>
      </div>
      <div>
        <div class='kv'>Primary address<br><b id='primary-email'>priya@cloudmail.app</b></div>
        <div class='kv' style='margin-top:10px'>Seats<br><b id='seat-count'>12 of 20</b></div>
      </div>
    </div>
  </div>
  <div class='box'>
    <div class='kv'>Storage used — <b id='storage-used'>68.4 GB</b> of <span id='storage-total'>100 GB</span></div>
    <div class='meter'><span style='width:68%'></span></div>
  </div>
  <div class='box'>
    <h2 style='font-size:16px'>Recent sign-in activity</h2>
    <table id='activity'>
      <thead><tr><th>When</th><th>Device</th><th>Location</th><th>Status</th></tr></thead>
      <tbody>
        <tr class='activity-row'><td class='act-when'>2026-06-22 09:14</td><td class='act-device'>MacBook Pro</td><td class='act-location'>Seattle, US</td><td class='act-status'>Success</td></tr>
        <tr class='activity-row'><td class='act-when'>2026-06-21 18:02</td><td class='act-device'>iPhone 15</td><td class='act-location'>Seattle, US</td><td class='act-status'>Success</td></tr>
        <tr class='activity-row'><td class='act-when'>2026-06-20 11:47</td><td class='act-device'>Chrome / Windows</td><td class='act-location'>Portland, US</td><td class='act-status'>Success</td></tr>
      </tbody>
    </table>
  </div>
</div></body></html>"""


def create_app() -> FastAPI:
    app = FastAPI(title=f"Plaidify Demo Portal — {BRAND}")
    app.add_middleware(SessionMiddleware, secret_key="plaidify-demo-saas", session_cookie="cloudmail_session")

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok"})

    @app.get("/")
    async def root():
        return RedirectResponse("/login", status_code=302)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return HTMLResponse(_login_html())

    @app.post("/signin")
    async def signin(
        request: Request,
        user: str = Form(...),
        password: str = Form(..., alias="pass"),
    ):
        entry = VALID_USERS.get(user)
        if not entry or entry["password"] != password:
            return HTMLResponse(_login_html("Incorrect username or password."), status_code=401)
        request.session["authed"] = True
        return RedirectResponse("/workspace", status_code=303)

    @app.get("/workspace", response_class=HTMLResponse)
    async def workspace(request: Request):
        if not request.session.get("authed"):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(_workspace_html())

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("DEMO_SAAS_HOST", "127.0.0.1")
    port = int(os.environ.get("DEMO_SAAS_PORT", "8797"))
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
