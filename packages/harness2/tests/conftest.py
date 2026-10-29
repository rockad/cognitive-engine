"""Makes the shared test helper `h2_profiles` importable under pytest's importlib mode."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
