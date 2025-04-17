from fastapi import FastAPI, HTTPException
from src.models import ConnectRequest, ConnectResponse
from src.core.engine import connect_to_site

app = FastAPI()

@app.post("/connect", response_model=ConnectResponse)
async def connect(request: ConnectRequest):
    try:
        response_data = await connect_to_site(request.site, request.username, request.password)
        return response_data
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return response_data

@app.get("/status")
async def status():
    return {"status": "API is running"}

@app.post("/disconnect")
async def disconnect():
    return {"status": "disconnected"}

# Below is a basic in-memory store and sample endpoints to demonstrate a Plaid-like flow in "plaidify":

import uuid

LINK_TOKENS = {}
ACCESS_TOKENS = {}

@app.post("/create_link")
async def create_link(site: str):
    """
    Generates a link_token for the specified site.
    Example usage:
      POST /create_link?site=mock_site
      -> Returns {"link_token": "some-link-token"}
    """
    link_token = str(uuid.uuid4())
    LINK_TOKENS[link_token] = {
        "site": site,
        "credentials": None
    }
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
    if link_token not in LINK_TOKENS:
        raise HTTPException(status_code=404, detail="Invalid link token.")

    LINK_TOKENS[link_token]["credentials"] = {
        "username": username,
        "password": password
    }

    # For demonstration, generate a new access_token:
    access_token = str(uuid.uuid4())
    ACCESS_TOKENS[access_token] = {
        "site": LINK_TOKENS[link_token]["site"],
        "username": username,
        "password": password
    }

    return {"access_token": access_token}


@app.get("/fetch_data")
async def fetch_data(access_token: str):
    """
    Fetches data for the site using the specified access_token.
    Delegates to connect_to_site for the actual site blueprint logic.
    Example usage:
      GET /fetch_data?access_token=some-access-token
      -> Returns the extracted data or a default payload if credentials are invalid
    """
    if access_token not in ACCESS_TOKENS:
        raise HTTPException(status_code=401, detail="Invalid access token.")
    
    token_info = ACCESS_TOKENS[access_token]
    site = token_info["site"]
    username = token_info["username"]
    password = token_info["password"]

    # Attempt connection and extraction:
    response_data = await connect_to_site(site, username, password)
    return response_data