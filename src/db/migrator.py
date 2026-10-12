from __future__ import annotations

from pathlib import Path

from .connection import DEFAULT_DB_PATH, get_connection

MIGRATION_DIR = Path(__file__).resolve().parent / "migrations"


def run_migrations(
    db_path: str | Path = DEFAULT_DB_PATH, migration_dir: Path = MIGRATION_DIR
) -> list[str]:
    """Execute all SQL migration scripts in a single atomic transaction."""
    connection = get_connection(db_path)
    migration_files = sorted(Path(migration_dir).glob("*.sql"))
    applied_files: list[str] = []

    try:
        migration_scripts: list[str] = []
        for migration_file in migration_files:
            migration_scripts.append(migration_file.read_text(encoding="utf-8"))
            applied_files.append(migration_file.name)

        if migration_scripts:
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + "\n".join(migration_scripts)
            )
        connection.commit()
        return applied_files
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
