import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_web_migration_upgrade_downgrade_preserves_rows(tmp_path):
    root = Path(__file__).parents[1]
    config = tmp_path / "config"
    config.mkdir()
    env = {**os.environ, "CONFIG_ROOT": str(config)}

    def migrate(revision, command="upgrade"):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", command, revision],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    migrate("c6d7e8f9a0b1")
    with sqlite3.connect(config / "subi-neko.db") as db:
        db.execute(
            "INSERT INTO projects (name,source_directory,anime_provider,anime_external_id) VALUES ('Kept','kept','anidb','1')"
        )
    migrate("head")
    with sqlite3.connect(config / "subi-neko.db") as db:
        assert db.execute(
            "SELECT name,web_config_version,web_publish_attempt FROM projects"
        ).fetchone() == ("Kept", 0, 0)
        assert {r[1] for r in db.execute("PRAGMA index_list(files)")} >= {
            "uq_file_web_tmdb",
            "uq_file_web_key",
            "uq_file_web_uuid",
        }
    migrate("c6d7e8f9a0b1", "downgrade")
    migrate("head")
