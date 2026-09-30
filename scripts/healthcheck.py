#!/usr/bin/env python3
"""Container health probe: GET a local URL and pass only on HTTP 200.

A redirect is a failure, not something to follow: a 307 from /health means
the HTTPS redirect or the proxy trust settings are wrong, and following it to
https:// on a plain-HTTP port would only hide that.

Usage (the image installs this as ``plaidify-healthcheck``):
    plaidify-healthcheck                              # the API: http://127.0.0.1:8000/health
    plaidify-healthcheck http://127.0.0.1:9101/health # the access-job executor

HEALTHCHECK_URL and HEALTHCHECK_TIMEOUT (seconds, default 4) set the defaults.
"""

import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8000/health"


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError for the 3xx


def main(argv: list) -> int:
    url = argv[1] if len(argv) > 1 else os.environ.get("HEALTHCHECK_URL", DEFAULT_URL)
    timeout = float(os.environ.get("HEALTHCHECK_TIMEOUT", "4"))
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(url, timeout=timeout) as resp:
            if resp.status == 200:
                return 0
            print(f"healthcheck: HTTP {resp.status} from {url}", file=sys.stderr)
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location") if exc.headers else None
        suffix = f" -> {location}" if location else ""
        print(f"healthcheck: HTTP {exc.code} from {url}{suffix}", file=sys.stderr)
    except Exception as exc:  # connection refused, timeout, DNS...
        print(f"healthcheck: {url}: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
