"""SDK test configuration."""

import sys
from pathlib import Path

# Ensure the SDK package is importable
sdk_root = Path(__file__).parent.parent
if str(sdk_root) not in sys.path:
    sys.path.insert(0, str(sdk_root))
