import uvicorn
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI()
# Add a very simple session middleware for demonstration (not recommended for production)
app.add_middleware(SessionMiddleware, secret_key="mock-secret-key")

HTML_LOGIN_FORM = """
<!DOCTYPE html>
<html>
    <head>
        <title>Mock Login</title>
    </head>
    <body>
        <h1>Mock Site - Login</h1>
        <form action="/login" method="post">
            <label for="username">Username:</label>
            <input type="text" name="username" id="username" />
            <br/>
            <label for="password">Password:</label>
            <input type="password" name="password" id="password" />
            <br/>
            <button type="submit">Log In</button>
        </form>
    </body>
</html>
"""

HTML_DASHBOARD = """
<!DOCTYPE html>
<html>
    <head>
        <title>Mock Dashboard</title>
    </head>
    <body>
        <h1>Mock Site - Dashboard</h1>
        <p>Welcome to the mock dashboard!</p>
        <div id="mock-status">active</div>
        <div id="mock-sync">2025-04-17T12:00:00Z</div>
        <p><a href="/logout">Logout</a></p>
    </body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def home():
    # Simple route to redirect to /login
    return RedirectResponse(url="/login")

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # Serve the mock login page
    return HTML_LOGIN_FORM

@app.post("/login", response_class=HTMLResponse)
async def login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    # Very basic check: "mock_user" / "mock_password"
    if username == "mock_user" and password == "mock_password":
        request.session["logged_in"] = True
        return RedirectResponse(url="/dashboard", status_code=302)
    else:
        raise HTTPException(status_code=401, detail="Invalid credentials")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    # Check if user is logged in
    if request.session.get("logged_in"):
        return HTML_DASHBOARD
    else:
        return RedirectResponse(url="/login")

@app.get("/logout", response_class=HTMLResponse)
async def logout(request: Request):
    request.session.pop("logged_in", None)
    return RedirectResponse(url="/login")

def run_server():
    uvicorn.run(app, host="0.0.0.0", port=8080)

if __name__ == "__main__":
    run_server()