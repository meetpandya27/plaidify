from abc import ABC, abstractmethod
from typing import Any, Dict

class BaseConnector(ABC):
    """
    Abstract base class for all site connectors.
    Implement this interface to add support for a new site.
    """

    @abstractmethod
    def connect(self, username: str, password: str) -> Dict[str, Any]:
        """
        Connect to the site and return user data.
        Args:
            username (str): The user's username for the site.
            password (str): The user's password for the site.
        Returns:
            dict: A dictionary containing connection status and user data.
        """
        pass
