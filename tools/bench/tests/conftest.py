import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
BENCH = TESTS.parent
for p in (BENCH, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
