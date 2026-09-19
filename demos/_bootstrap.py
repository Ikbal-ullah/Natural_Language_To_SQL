"""Put the repo root and src/ on sys.path so each demo can be run directly.

Imported for its side effect. Lives here so the demos themselves stay short enough to
read in one screen, which is the only reason they are useful.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
