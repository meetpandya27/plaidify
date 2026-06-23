"""Plaidify bundled demo/sandbox.

This package ships a self-contained target site (``portal``) that behaves like a
real authenticated web portal — login, optional MFA, and a data-rich dashboard.
Paired with the ``demo_utility`` connector and the ``scripts/demo.py`` runner, it
lets anyone exercise the full Plaidify loop (connect → MFA → extract) end-to-end
without a real external site.

The demo target is intentionally neutral and is gated behind ``DEMO_MODE`` on
discovery surfaces so it never leaks into a production deployment.
"""
