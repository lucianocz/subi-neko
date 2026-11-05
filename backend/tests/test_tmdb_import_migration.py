import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_tmdb_import_migration_keeps_existing_rows_and_round_trips(tmp_path):
    root = Path(__file__).parents[1]
    config = tmp_path / "config"
    config.mkdir()
    env = {**os.environ, "CONFIG_ROOT": str(config),
           "IMPORT_ROOT": str(tmp_path / "i"), "OUTPUT_ROOT": str(tmp_path / "o")}

    def migrate(revision, command="upgrade"):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", command, revision],
            cwd=root, env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr

    migrate("f3a4b5c6d7e8")                      # the revision before the TMDB import mapping
    db = config / "subi-neko.db"
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO projects (name,source_directory,anime_provider,anime_external_id) "
                    "VALUES ('Legacy','legacy','anidb','1')")
        con.execute("INSERT INTO project_episodes (project_id,episode_number,title) VALUES (1,1,'Old title')")

    migrate("head")
    with sqlite3.connect(db) as con:
        # Existing projects need no TMDB migration: everything new is NULL.
        assert con.execute("SELECT name, expected_episode_count, web_publish_intent, web_config_json "
                           "FROM projects").fetchone() == ("Legacy", None, None, None)
        assert con.execute("SELECT title, tmdb_episode_id, tmdb_name, tmdb_runtime "
                           "FROM project_episodes").fetchone() == ("Old title", None, None, None)
        assert "uq_project_episodes_tmdb" in {r[1] for r in con.execute("PRAGMA index_list(project_episodes)")}
        # NULL TMDB ids never collide; a real id is unique per project.
        con.execute("INSERT INTO project_episodes (project_id,episode_number,tmdb_episode_id) VALUES (1,2,NULL)")
        con.execute("INSERT INTO project_episodes (project_id,episode_number,tmdb_episode_id) VALUES (1,3,'9')")
        try:
            con.execute("INSERT INTO project_episodes (project_id,episode_number,tmdb_episode_id) VALUES (1,4,'9')")
            raise AssertionError("duplicate TMDB episode id accepted")
        except sqlite3.IntegrityError:
            pass

    migrate("f3a4b5c6d7e8", "downgrade")
    with sqlite3.connect(db) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(project_episodes)")}
        assert "tmdb_episode_id" not in cols and "title" in cols
        assert con.execute("SELECT name FROM projects").fetchone() == ("Legacy",)
    migrate("head")
