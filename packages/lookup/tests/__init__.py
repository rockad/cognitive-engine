"""Test package setup that must run before `lookup` is imported.

⚠️ `config.py` reads `.env` at import time, and every path in it is resolved
once at module load. A developer's own `.env` would therefore reach the tests
and make them pass or fail depending on that file's contents. `LOOKUP_NO_DOTENV`
turns the loader off, and it has to be set here — before any test module
imports the package — rather than in each test's setUp.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("LOOKUP_NO_DOTENV", "1")

# The suite runs without an install step; `src/` goes on the path here.
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
