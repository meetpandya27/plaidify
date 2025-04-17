# test_endpoints.py
# This script demonstrates how to call the FastAPI endpoints and display responses in the terminal.
# Before running this script, ensure that the FastAPI server is running:
#   uvicorn src.main:app --port 8000
# Then run:
#   python test_endpoints.py

import requests
import json

def test_connect():
    url = "http://127.0.0.1:8000/connect"
    payload = {
        "site": "mock_site",
        "username": "myUser",
        "password": "myPass"
    }
    response = requests.post(url, json=payload)
    print("POST /connect response:")
    print(response.text, "\n")

def test_create_link():
    url = "http://127.0.0.1:8000/create_link"
    # Using query parameters for "site"
    params = {"site": "mock_site"}
    response = requests.post(url, params=params)
    print("POST /create_link response:")
    print(response.text, "\n")
    # Return the link_token to chain into submit_credentials
    data = response.json()
    return data.get("link_token", None)

def test_submit_credentials(link_token, username, password):
    url = "http://127.0.0.1:8000/submit_credentials"
    params = {
        "link_token": link_token,
        "username": username,
        "password": password
    }
    response = requests.post(url, params=params)
    print("POST /submit_credentials response:")
    print(response.text, "\n")
    data = response.json()
    return data.get("access_token", None)

def test_fetch_data(access_token):
    url = "http://127.0.0.1:8000/fetch_data"
    params = {"access_token": access_token}
    response = requests.get(url, params=params)
    print("GET /fetch_data response:")
    print(response.text, "\n")

def main():
    # 1) /connect
    test_connect()

    # 2) /create_link
    link_token = test_create_link()
    if not link_token:
        print("No link_token returned. Exiting.")
        return

    # 3) /submit_credentials
    access_token = test_submit_credentials(link_token, "myUser", "myPass")
    if not access_token:
        print("No access_token returned. Exiting.")
        return

    # 4) /fetch_data
    test_fetch_data(access_token)

if __name__ == "__main__":
    main()