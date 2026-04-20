# Plaidify Python SDK

Python client for the Plaidify service.

## Install

```bash
pip install plaidify
```

## Connect Flow

```python
from plaidify import Plaidify

async with Plaidify(server_url="http://localhost:8000") as pfy:
    result = await pfy.connect(
        "hydro_one",
        username="your_username",
        password="your_password",
    )
    print(result.status)
```

## Hosted Link Flow

Preferred production pattern:

1. Your backend creates a signed hosted link bootstrap token.
2. The client redeems that token for a live hosted link session.
3. The client opens the hosted link URL.

## CLI

```bash
plaidify serve --port 8000
plaidify connect hydro_one -u your_username -p your_password
plaidify blueprint list
plaidify blueprint info hydro_one
plaidify blueprint validate ./connectors/your_site.json
plaidify health
```

## Notes

- The CLI no longer provides a bundled launcher for showcase assets.
- Production deployments should provide explicit environment configuration rather than relying on generated local defaults.
- For hosted link production usage, prefer signed bootstrap minting over anonymous public session creation.

## License

MIT.
