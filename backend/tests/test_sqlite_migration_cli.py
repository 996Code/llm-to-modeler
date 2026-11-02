"""Explicit source selection for the offline SQLite migration CLI."""
import os
from pathlib import Path
import subprocess
import sys


def test_sqlite_source_is_required_even_with_legacy_env():
    script = Path(__file__).resolve().parents[1] / "scripts/migrate_sqlite_to_pg.py"
    result = subprocess.run(
        [sys.executable, str(script), "--dry-run"],
        env={**os.environ, "DATABASE_PATH": "/missing/legacy.db", "DATABASE_URL": ""},
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "--sqlite" in result.stderr
    assert "required" in result.stderr
