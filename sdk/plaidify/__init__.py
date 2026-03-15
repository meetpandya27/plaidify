"""
Plaidify — The open-source API for authenticated web data.

Quick start:
    from plaidify import Plaidify

    pfy = Plaidify(server_url="http://localhost:8000")
    result = await pfy.connect("greengrid_energy", username="demo_user", password="demo_pass")
    print(result.data["current_bill"])
"""

__version__ = "0.3.0a1"
__all__ = [
    "Plaidify",
    "PlaidifySync",
    "ConnectResult",
    "BlueprintInfo",
    "LinkResult",
    "MFAChallenge",
    "PlaidifyError",
    "ConnectionError",
    "AuthenticationError",
    "MFARequiredError",
    "BlueprintNotFoundError",
    "ServerError",
]

from plaidify.client import Plaidify, PlaidifySync
from plaidify.models import ConnectResult, BlueprintInfo, LinkResult, MFAChallenge
from plaidify.exceptions import (
    PlaidifyError,
    ConnectionError,
    AuthenticationError,
    MFARequiredError,
    BlueprintNotFoundError,
    ServerError,
)
