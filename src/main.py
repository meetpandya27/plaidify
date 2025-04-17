from fastapi import FastAPI, HTTPException
from src.models import ConnectRequest, ConnectResponse
from src.core.engine import connect_to_site
from src.database import SessionLocal, init_db, Link, AccessToken, encrypt_password, decrypt_password

from fastapi.staticfiles import StaticFiles

app = FastAPI()
app.mount("/ui", StaticFiles(directory="frontend", html=True), name="frontend")

@app.on_event("startup")
def on_startup():
    init_db()

@app.post("/connect", response_model=ConnectResponse)
async def connect(request: ConnectRequest):
    try:
        response_data = await connect_to_site(request.site, request.username, request.password)
        return response_data
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return response_data

@app.get("/")
async def root():
    return {"message": "Welcome to the API!"}

@app.get("/status")
async def status():
    return {"status": "API is running"}

@app.post("/disconnect")
async def disconnect():
    return {"status": "disconnected"}

# Setting up UUID import
import uuid

# Removed in-memory dicts, replaced with DB

@app.post("/create_link")
async def create_link(site: str):
    """
    Generates a link_token for the specified site.
    Example usage:
      POST /create_link?site=mock_site
      -> Returns {"link_token": "some-link-token"}
    """
    link_token = str(uuid.uuid4())

    db = SessionLocal()
    new_link = Link(link_token=link_token, site=site)
    db.add(new_link)
    db.commit()
    db.close()

    return {"link_token": link_token}


@app.post("/submit_credentials")
async def submit_credentials(link_token: str, username: str, password: str):
    """
    Accepts user credentials for a given link_token, then returns an access_token
    which can be used to fetch data for that site.
    Example usage:
      POST /submit_credentials?link_token=abc&username=mock_user&password=mock_password
      -> Returns {"access_token": "some-access-token"}
    """
    db = SessionLocal()
    existing_link = db.query(Link).filter_by(link_token=link_token).first()
    if not existing_link:
        db.close()
        raise HTTPException(status_code=404, detail="Invalid link token.")

    encrypted_username = encrypt_password(username)
    encrypted_password = encrypt_password(password)

    access_token = str(uuid.uuid4())
    new_token = AccessToken(
        token=access_token,
        link_token=link_token,
        username_encrypted=encrypted_username,
        password_encrypted=encrypted_password
    )
    db.add(new_token)
    db.commit()
    db.close()

    return {"access_token": access_token}


@app.post("/submit_instructions")
async def submit_instructions(access_token: str, instructions: str):
    """
    Accept instructions for a given access_token, storing them in the DB.
    Example usage:
      POST /submit_instructions?access_token=abc&instructions=ScrapeLastMonth
      -> Returns {"status":"Instructions stored successfully"} or an error if invalid token
    """
    db = SessionLocal()
    token_record = db.query(AccessToken).filter_by(token=access_token).first()
    if not token_record:
        db.close()
        raise HTTPException(status_code=401, detail="Invalid access token.")

    token_record.instructions = instructions
    db.commit()
    db.close()
    return {"status": "Instructions stored successfully"}

@app.get("/fetch_data")
async def fetch_data(access_token: str):
    """
    Fetches data for the site using the specified access_token.
    Delegates to connect_to_site for the actual site blueprint logic.
    Example usage:
      GET /fetch_data?access_token=some-access-token
      -> Returns the extracted data or a default payload if credentials are invalid
    """
    db = SessionLocal()
    token_record = db.query(AccessToken).filter_by(token=access_token).first()
    if not token_record:
        db.close()
        raise HTTPException(status_code=401, detail="Invalid access token.")

    site = db.query(Link).filter_by(link_token=token_record.link_token).first()
    if not site:
        db.close()
        raise HTTPException(status_code=401, detail="Linked data not found.")

    username = decrypt_password(token_record.username_encrypted)
    password = decrypt_password(token_record.password_encrypted)

    # Retrieve any instructions stored for this access token
    user_instructions = token_record.instructions

    response_data = await connect_to_site(site.site, username, password)

    # Optionally attach instructions to the response, or apply special logic
    if user_instructions:
        response_data["instructions_applied"] = user_instructions

    db.close()
    return response_data