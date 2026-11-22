"""The example is an installable plugin package, not a workspace Python distribution."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
