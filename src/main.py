"""Compatibility entrypoint for the Plaidify ASGI application."""

from src.app import app, settings

__all__ = ["app", "settings"]
