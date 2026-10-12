from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from src.backup.archive_service import (
    BackupArchiveError,
    create_backup_archive,
    restore_backup_archive,
)
from src.backup.manifest_verifier import (
    ManifestVerificationError,
    compute_sha256,
    verify_archive_manifest,
)


def _create_database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS sample (value TEXT NOT NULL)")
        connection.execute("DELETE FROM sample")
        connection.execute("INSERT INTO sample (value) VALUES (?)", (value,))


def _database_value(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT value FROM sample").fetchone()[0]


def _rewrite_zip(
    source: Path,
    target: Path,
    *,
    replacements: dict[str, bytes] | None = None,
    omit: set[str] | None = None,
) -> None:
    replacements = replacements or {}
    omitted = omit or set()
    with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(
        target, "w", compression=zipfile.ZIP_DEFLATED
    ) as modified:
        for name in original.namelist():
            if name in omitted:
                continue
            modified.writestr(name, replacements.get(name, original.read(name)))


def test_backup_uses_sqlite_backup_and_includes_database_and_config_files(
    tmp_path: Path,
) -> None:
    source_db = tmp_path / "source" / "portfolio.db"
    _create_database(source_db, "consistent-data")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "rules.yaml").write_bytes(b"version: 1\n")
    nested = config_dir / "brokers"
    nested.mkdir()
    (nested / "stockbit.yaml").write_bytes(b"broker_id: STOCKBIT\n")
    archive_path = tmp_path / "backup.zip"

    result = create_backup_archive(
        source_db,
        archive_path,
        config_dir=config_dir,
    )

    assert Path(result["archive_path"]) == archive_path
    assert zipfile.is_zipfile(archive_path)
    with zipfile.ZipFile(archive_path, "r") as archive:
        members = set(archive.namelist())
        assert members == {
            "portfolio.db",
            "config/rules.yaml",
            "config/brokers/stockbit.yaml",
            "manifest.json",
        }
        backup_db = tmp_path / "from-archive.db"
        backup_db.write_bytes(archive.read("portfolio.db"))
        assert _database_value(backup_db) == "consistent-data"
        assert archive.read("config/rules.yaml") == b"version: 1\n"
        assert archive.read("config/brokers/stockbit.yaml") == b"broker_id: STOCKBIT\n"


def test_manifest_lists_sha256_for_each_payload_file_and_utc_timestamp(
    tmp_path: Path,
) -> None:
    source_db = tmp_path / "portfolio.db"
    _create_database(source_db, "manifest-data")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "rules.yaml"
    config_file.write_text("version: 1\n", encoding="utf-8")
    archive_path = tmp_path / "backup.zip"

    create_backup_archive(source_db, archive_path, config_dir=config_dir)

    with zipfile.ZipFile(archive_path, "r") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert set(manifest) == {"created_at", "files"}
        datetime.strptime(manifest["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        assert {entry["path"] for entry in manifest["files"]} == {
            "portfolio.db",
            "config/rules.yaml",
        }
        for entry in manifest["files"]:
            assert entry["sha256"] == hashlib.sha256(
                archive.read(entry["path"])
            ).hexdigest()
            assert entry["sha256"] == compute_sha256(
                _write_member_to_file(archive, entry["path"], tmp_path)
            )

    verification = verify_archive_manifest(archive_path)
    assert verification["valid"] is True
    assert verification["errors"] == []
    assert len(verification["verified_files"]) == 2


def _write_member_to_file(
    archive: zipfile.ZipFile, member: str, directory: Path
) -> Path:
    output = directory / f"hash-check-{Path(member).name}"
    output.write_bytes(archive.read(member))
    return output


def test_restore_verifies_manifest_before_replacing_database_and_config(
    tmp_path: Path,
) -> None:
    source_db = tmp_path / "source.db"
    _create_database(source_db, "backup-value")
    source_config = tmp_path / "source-config"
    source_config.mkdir()
    (source_config / "rules.yaml").write_text("version: backup\n", encoding="utf-8")
    archive_path = tmp_path / "backup.zip"
    create_backup_archive(source_db, archive_path, config_dir=source_config)

    target_db = tmp_path / "restore" / "portfolio.db"
    _create_database(target_db, "existing-value")
    target_config = tmp_path / "restore" / "config"
    target_config.mkdir(parents=True)
    (target_config / "rules.yaml").write_text("version: existing\n", encoding="utf-8")
    with zipfile.ZipFile(archive_path, "r") as archive:
        tampered_db = archive.read("portfolio.db") + b"tampered"
    bad_archive = tmp_path / "tampered.zip"
    _rewrite_zip(
        archive_path,
        bad_archive,
        replacements={"portfolio.db": tampered_db},
    )

    with pytest.raises(ManifestVerificationError, match="checksum verification failed"):
        restore_backup_archive(
            bad_archive,
            db_target_path=target_db,
            config_target_dir=target_config,
            overwrite=True,
        )

    assert _database_value(target_db) == "existing-value"
    assert (target_config / "rules.yaml").read_text(encoding="utf-8") == "version: existing\n"


def test_restore_successfully_restores_database_and_config_after_verification(
    tmp_path: Path,
) -> None:
    source_db = tmp_path / "source.db"
    _create_database(source_db, "restored-value")
    source_config = tmp_path / "config"
    source_config.mkdir()
    (source_config / "rules.yaml").write_text("version: restored\n", encoding="utf-8")
    archive_path = tmp_path / "backup.zip"
    create_backup_archive(source_db, archive_path, config_dir=source_config)

    target_db = tmp_path / "target" / "portfolio.db"
    target_config = tmp_path / "target" / "config"
    result = restore_backup_archive(
        archive_path,
        db_target_path=target_db,
        config_target_dir=target_config,
    )

    assert Path(result["restored_database"]) == target_db
    assert Path(result["restored_config_dir"]) == target_config
    assert _database_value(target_db) == "restored-value"
    assert (target_config / "rules.yaml").read_text(encoding="utf-8") == "version: restored\n"
    assert result["manifest"]["created_at"]


def test_tampered_manifest_checksum_prevents_restore(
    tmp_path: Path,
) -> None:
    source_db = tmp_path / "source.db"
    _create_database(source_db, "safe")
    archive_path = tmp_path / "backup.zip"
    create_backup_archive(source_db, archive_path, include_config=False)

    with zipfile.ZipFile(archive_path, "r") as archive:
        manifest = json.loads(archive.read("manifest.json"))
    manifest["files"][0]["sha256"] = "0" * 64
    tampered_manifest_archive = tmp_path / "tampered-manifest.zip"
    _rewrite_zip(
        archive_path,
        tampered_manifest_archive,
        replacements={
            "manifest.json": json.dumps(manifest).encode("utf-8"),
        },
    )
    target_db = tmp_path / "target.db"
    _create_database(target_db, "must-remain")

    result = verify_archive_manifest(tampered_manifest_archive)
    assert result["valid"] is False
    assert any("SHA256 mismatch" in error for error in result["errors"])

    with pytest.raises(ManifestVerificationError):
        restore_backup_archive(
            tampered_manifest_archive,
            db_target_path=target_db,
            config_target_dir=tmp_path / "target-config",
        )
    assert _database_value(target_db) == "must-remain"


def test_restore_rejects_missing_manifest_before_writing_targets(
    tmp_path: Path,
) -> None:
    source_db = tmp_path / "source.db"
    _create_database(source_db, "payload")
    source_archive = tmp_path / "backup.zip"
    create_backup_archive(source_db, source_archive, include_config=False)
    manifestless_archive = tmp_path / "no-manifest.zip"
    _rewrite_zip(
        source_archive,
        manifestless_archive,
        omit={"manifest.json"},
    )
    target_db = tmp_path / "target.db"
    _create_database(target_db, "preserve")

    with pytest.raises(ManifestVerificationError, match="manifest.json is missing"):
        restore_backup_archive(
            manifestless_archive,
            db_target_path=target_db,
            config_target_dir=tmp_path / "config-target",
        )

    assert _database_value(target_db) == "preserve"


def test_restore_respects_overwrite_false_for_existing_database(
    tmp_path: Path,
) -> None:
    source_db = tmp_path / "source.db"
    _create_database(source_db, "backup")
    archive_path = tmp_path / "backup.zip"
    create_backup_archive(source_db, archive_path, include_config=False)
    target_db = tmp_path / "target.db"
    _create_database(target_db, "existing")

    with pytest.raises(FileExistsError, match="overwrite=False"):
        restore_backup_archive(
            archive_path,
            db_target_path=target_db,
            config_target_dir=tmp_path / "config-target",
            overwrite=False,
        )

    assert _database_value(target_db) == "existing"


def test_rejects_non_zip_archive_without_modifying_target(
    tmp_path: Path,
) -> None:
    invalid_archive = tmp_path / "not-a-zip.zip"
    invalid_archive.write_text("not an archive", encoding="utf-8")
    target_db = tmp_path / "target.db"
    _create_database(target_db, "existing")

    with pytest.raises(BackupArchiveError, match="not a valid ZIP"):
        restore_backup_archive(
            invalid_archive,
            db_target_path=target_db,
            config_target_dir=tmp_path / "config-target",
        )

    assert _database_value(target_db) == "existing"
