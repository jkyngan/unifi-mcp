"""Make the example clip client importable in the normal root pytest suite."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples/python"))
