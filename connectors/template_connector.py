from src.core.connector_base import BaseConnector
from typing import Any, Dict

class TemplateConnector(BaseConnector):
    """
    Template connector for new sites. Copy this file and implement the connect method.
    """
    def connect(self, username: str, password: str) -> Dict[str, Any]:
        # Implement site-specific login and data extraction here
        # Example return structure:
        return {
            "status": "connected",
            "data": {
                "example_field": "example_value"
            }
        }
