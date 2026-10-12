from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .manifest_verifier import ManifestVerificationError, verify_archive_manifest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "portfolio.db"
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"


class BackupArchiveError(ValueError):
    """Base error for archive backup and restore operations."""


class ArchiveServiceError(BackupArchiveError):
    """Backward-compatible alias for archive service errors."""


def _normalize_path(value: str | Path | None, *, default: Path, name: str) -> Path:
    path = Path(value) if value is not None else default
    path = path.expanduser()
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    return path


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _collect_files_for_manifest(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for file_path in sorted(root.rglob("*")):
        if file_path.is_file() and file_path.name != "manifest.json":
            relative_path = file_path.relative_to(root).as_posix()
            data = file_path.read_bytes()
            entries.append(
                {
                    "path": relative_path,
                    "sha256": _sha256_bytes(data),
                    "size_bytes": len(data),
                }
            )
    return entries


def _write_manifest(root: Path, *, files: list[dict[str, Any]], created_at: str | None = None) -> Path:
    payload = {
        "created_at": created_at or _utc_timestamp(),
        "files": files,
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return manifest_path


def _copy_sqlite_backup(source_db: Path, backup_db_path: Path) -> None:
    if not source_db.exists():
        raise FileNotFoundError(f"Database file not found: {source_db}")

    backup_db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(source_db) as source_conn, sqlite3.connect(
            str(backup_db_path)
        ) as backup_conn:
            source_conn.backup(backup_conn)
    except sqlite3.DatabaseError as exc:
        raise BackupArchiveError(f"Failed to create SQLite backup from {source_db}: {exc}") from exc


def create_backup_archive(
    db_path: str | Path | None = None,
    archive_path: str | Path | None = None,
    *,
    config_dir: str | Path | None = None,
    include_config: bool = True,
    temp_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Create a ZIP archive containing a SQLite backup and the active config directory."""
    source_db = _normalize_path(db_path, default=DEFAULT_DB_PATH, name="db_path")
    archive_target = _normalize_path(
        archive_path,
        default=PROJECT_ROOT / "backups" / f"portfolio-backup-{_utc_timestamp().replace(':', '-')}.zip",
        name="archive_path",
    )
    archive_target.parent.mkdir(parents=True, exist_ok=True)

    if not source_db.exists():
        raise FileNotFoundError(f"Database file not found: {source_db}")

    work_dir = Path(temp_dir) if temp_dir is not None else Path(tempfile.mkdtemp(prefix="portfolio-backup-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    payload_root = work_dir / "archive_payload"
    payload_root.mkdir(parents=True, exist_ok=True)

    backup_db_path = payload_root / "portfolio.db"
    _copy_sqlite_backup(source_db, backup_db_path)

    if include_config:
        resolved_config_dir = _normalize_path(config_dir, default=DEFAULT_CONFIG_DIR, name="config_dir")
        if resolved_config_dir.exists() and resolved_config_dir.is_dir():
            shutil.copytree(resolved_config_dir, payload_root / "config", dirs_exist_ok=True)

    created_at = _utc_timestamp()
    manifest_files = _collect_files_for_manifest(payload_root)
    _write_manifest(payload_root, files=manifest_files, created_at=created_at)

    with zipfile.ZipFile(archive_target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(payload_root.rglob("*")):
            if file_path.is_file():
                archive.write(file_path, arcname=file_path.relative_to(payload_root).as_posix())

    if temp_dir is None:
        shutil.rmtree(work_dir, ignore_errors=True)

    return {
        "archive_path": str(archive_target),
        "created_at": created_at,
        "database_path": str(source_db),
        "config_dir": str(_normalize_path(config_dir, default=DEFAULT_CONFIG_DIR, name="config_dir")) if include_config else None,
        "manifest": {
            "created_at": created_at,
            "files": manifest_files,
        },
    }


def backup_database(
    db_path: str | Path | None = None,
    archive_path: str | Path | None = None,
    *,
    config_dir: str | Path | None = None,
    include_config: bool = True,
    temp_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Alias for create_backup_archive."""
    return create_backup_archive(
        db_path=db_path,
        archive_path=archive_path,
        config_dir=config_dir,
        include_config=include_config,
        temp_dir=temp_dir,
    )


def create_archive(
    db_path: str | Path | None = None,
    archive_path: str | Path | None = None,
    *,
    config_dir: str | Path | None = None,
    include_config: bool = True,
    temp_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Alias for create_backup_archive."""
    return create_backup_archive(
        db_path=db_path,
        archive_path=archive_path,
        config_dir=config_dir,
        include_config=include_config,
        temp_dir=temp_dir,
    )


def restore_backup_archive(
    archive_path: str | Path,
    *,
    db_target_path: str | Path | None = None,
    config_target_dir: str | Path | None = None,
    overwrite: bool = True,
    verify_manifest_hashes: bool = True,
) -> dict[str, Any]:
    """Restore a ZIP backup archive only after verifying manifest checksum integrity."""
    source_archive = Path(archive_path)
    if not source_archive.exists():
        raise FileNotFoundError(f"Archive not found: {source_archive}")
    if not zipfile.is_zipfile(source_archive):
        raise BackupArchiveError(f"Archive is not a valid ZIP file: {source_archive}")

    if verify_manifest_hashes:
        verification = verify_archive_manifest(source_archive)
        if not verification["valid"]:
            raise ManifestVerificationError("Archive checksum verification failed: " + "; ".join(verification["errors"]))

    restore_root = Path(tempfile.mkdtemp(prefix="portfolio-restore-"))
    try:
        with zipfile.ZipFile(source_archive, "r") as archive:
            archive.extractall(restore_root)

        manifest_path = restore_root / "manifest.json"
        if not manifest_path.exists():
            raise BackupArchiveError("Manifest file is missing from archive content.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        target_db = _normalize_path(db_target_path, default=DEFAULT_DB_PATH, name="db_target_path")
        target_config_dir = _normalize_path(config_target_dir, default=DEFAULT_CONFIG_DIR, name="config_target_dir")

        target_db.parent.mkdir(parents=True, exist_ok=True)
        if target_db.exists() and not overwrite:
            raise FileExistsError(f"Target database already exists and overwrite=False: {target_db}")

        extracted_db = restore_root / "portfolio.db"
        if extracted_db.exists():
            if target_db.exists() and overwrite:
                target_db.unlink()
            shutil.copy2(extracted_db, target_db)

        extracted_config = restore_root / "config"
        if extracted_config.exists():
            if target_config_dir.exists() and overwrite:
                shutil.rmtree(target_config_dir)
            if target_config_dir.exists() and not overwrite:
                raise FileExistsError(f"Target config dir already exists and overwrite=False: {target_config_dir}")
            shutil.copytree(extracted_config, target_config_dir)

        return {
            "archive_path": str(source_archive),
            "restored_database": str(target_db),
            "restored_config_dir": str(target_config_dir),
            "manifest": manifest,
        }
    finally:
        shutil.rmtree(restore_root, ignore_errors=True)


def restore_archive(
    archive_path: str | Path,
    *,
    db_target_path: str | Path | None = None,
    config_target_dir: str | Path | None = None,
    overwrite: bool = True,
    verify_manifest_hashes: bool = True,
) -> dict[str, Any]:
    """Alias for restore_backup_archive."""
    return restore_backup_archive(
        archive_path,
        db_target_path=db_target_path,
        config_target_dir=config_target_dir,
        overwrite=overwrite,
        verify_manifest_hashes=verify_manifest_hashes,
    )


def restore_database(
    archive_path: str | Path,
    *,
    db_target_path: str | Path | None = None,
    config_target_dir: str | Path | None = None,
    overwrite: bool = True,
    verify_manifest_hashes: bool = True,
) -> dict[str, Any]:
    """Alias for restore_backup_archive."""
    return restore_backup_archive(
        archive_path,
        db_target_path=db_target_path,
        config_target_dir=config_target_dir,
        overwrite=overwrite,
        verify_manifest_hashes=verify_manifest_hashes,
    )
