from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any


class ManifestVerificationError(ValueError):
    """Raised when archive manifest data fails validation."""


class ManifestVerifierError(ManifestVerificationError):
    """Backward-compatible alias for manifest verification failures."""


def compute_sha256(file_path: str | Path) -> str:
    """Return the SHA256 digest for a file in hexadecimal form."""
    file_obj = Path(file_path)
    digest = hashlib.sha256()
    with file_obj.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_manifest_payload(archive_path: str | Path) -> tuple[dict[str, Any], list[str]]:
    archive_file = Path(archive_path)
    if not archive_file.exists():
        raise FileNotFoundError(f"Archive not found: {archive_file}")
    if not zipfile.is_zipfile(archive_file):
        raise ManifestVerificationError(f"Archive is not a valid ZIP file: {archive_file}")

    try:
        with zipfile.ZipFile(archive_file, "r") as archive:
            manifest_names = [name for name in archive.namelist() if name.endswith("manifest.json")]
            if not manifest_names:
                raise ManifestVerificationError("Archive manifest.json is missing.")
            manifest_name = manifest_names[0]
            try:
                payload = json.loads(archive.read(manifest_name))
            except json.JSONDecodeError as exc:
                raise ManifestVerificationError("Archive manifest.json is not valid JSON.") from exc
            if not isinstance(payload, dict):
                raise ManifestVerificationError("Archive manifest.json must contain a JSON object.")
            return payload, archive.namelist()
    except zipfile.BadZipFile as exc:
        raise ManifestVerificationError(f"Archive is corrupted or unreadable: {archive_file}") from exc


def verify_archive_manifest(archive_path: str | Path) -> dict[str, Any]:
    """Verify that all files referenced by manifest.json match their recorded SHA256 hashes."""
    archive_file = Path(archive_path)
    payload, archive_members = _archive_manifest_payload(archive_file)
    files = payload.get("files", [])
    if not isinstance(files, list):
        raise ManifestVerificationError("Archive manifest must contain a 'files' list.")

    seen_paths: set[str] = set()
    errors: list[str] = []
    verified_files: list[dict[str, str]] = []
    with zipfile.ZipFile(archive_file, "r") as archive:
        for item in files:
            if not isinstance(item, dict):
                errors.append("Manifest entry is not an object.")
                continue
            relative_path = str(item.get("path", "")).strip("/")
            expected_hash = str(item.get("sha256", ""))
            if not relative_path:
                errors.append("Manifest entry has no file path.")
                continue
            if relative_path in seen_paths:
                errors.append(f"Duplicate file entry in manifest: {relative_path}")
                continue
            seen_paths.add(relative_path)
            if relative_path not in archive_members:
                errors.append(f"File listed in manifest is missing from archive: {relative_path}")
                continue
            if not expected_hash:
                errors.append(f"Missing SHA256 for file: {relative_path}")
                continue
            try:
                actual_hash = hashlib.sha256(archive.read(relative_path)).hexdigest()
            except KeyError:
                errors.append(f"Archive member could not be read: {relative_path}")
                continue
            verified_files.append({"path": relative_path, "sha256": actual_hash})
            if actual_hash != expected_hash:
                errors.append(f"SHA256 mismatch for {relative_path}")

    return {
        "valid": not errors,
        "archive_path": str(archive_file),
        "manifest": payload,
        "verified_files": verified_files,
        "errors": errors,
    }


def verify_manifest(archive_path: str | Path) -> dict[str, Any]:
    """Backward-compatible alias to archive manifest verification."""
    return verify_archive_manifest(archive_path)
