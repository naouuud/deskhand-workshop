"""Make the lab's server.py importable when pytest runs from anywhere."""
import sys
from pathlib import Path

# server.py is a flat module, not a package: `import server` works only with
# this folder on sys.path. It lives here, above tests/, because pytest loads
# this conftest before tests/conftest.py, which imports server.
sys.path.insert(0, str(Path(__file__).resolve().parent))
