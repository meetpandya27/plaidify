from abc import ABC, abstractmethod
from typing import Any, ClassVar, Dict, List


class BaseConnector(ABC):
    """
    Abstract base class for all site connectors.
    Implement this interface to add support for a new site.

    ``connect`` may be a plain function (the engine runs it in a worker thread)
    or a coroutine; either way it gets the engine's time budget. Tag a
    connector "internal", "fixture", "sandbox" or "demo" and it runs only in
    demo mode or tests, like a blueprint with those tags.
    """

    tags: ClassVar[List[str]] = []

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
