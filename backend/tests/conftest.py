"""公共 pytest fixtures."""
import sys
from pathlib import Path

# 让 backend/src 可被 import
BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT / "src"))
