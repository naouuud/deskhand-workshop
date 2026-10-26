"""Make the checkpoint's flat modules importable when pytest runs from anywhere."""
import sys
from pathlib import Path

# The checkpoint is flat modules, not a package: `import nodes` works only
# with this folder on sys.path. It lives here, above tests/, because
# pytest loads this conftest before tests/conftest.py, which imports nodes.
sys.path.insert(0, str(Path(__file__).resolve().parent))
