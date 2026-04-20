"""
Plaidify — The open-source API for authenticated web data.

Quick start:
    from plaidify import Plaidify

    pfy = Plaidify(server_url="http://localhost:8000")
    result = await pfy.connect("hydro_one", username="test_user", password="test_pass")
    print(result.data["current_bill"])
"""

__version__ = "0.3.0b1"
__all__ = [
    "Plaidify",
    "PlaidifySync",
    "AccessJobInfo",
    "AccessJobListResult",
    "ConnectResult",
    "BlueprintInfo",
    "LinkResult",
    "LinkSession",
    "LinkEvent",
    "WebhookRegistration",
    "MFAChallenge",
    "AgentInfo",
    "AgentListResult",
    "ConsentRequest",
    "ConsentGrant",
    "ApiKeyInfo",
    "AuditEntry",
    "AuditLogResult",
    "AuditVerifyResult",
    "PublicTokenExchangeResult",
    "WebhookDeliveryResult",
    "PlaidifyError",
    "ConnectionError",
    "AuthenticationError",
    "MFARequiredError",
    "BlueprintNotFoundError",
    "ServerError",
]

from plaidify.client import Plaidify, PlaidifySync
from plaidify.models import (
    AccessJobInfo,
    AccessJobListResult,
    AgentInfo,
    AgentListResult,
    ApiKeyInfo,
    AuditEntry,
    AuditLogResult,
    AuditVerifyResult,
    BlueprintInfo,
    ConnectResult,
    ConsentGrant,
    ConsentRequest,
    LinkEvent,
    LinkResult,
    LinkSession,
    MFAChallenge,
    PublicTokenExchangeResult,
    WebhookDeliveryResult,
    WebhookRegistration,
)
from plaidify.exceptions import (
    PlaidifyError,
    ConnectionError,
    AuthenticationError,
    MFARequiredError,
    BlueprintNotFoundError,
    ServerError,
)
