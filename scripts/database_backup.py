#!/usr/bin/env python3
"""Create and verify consistent SQLite backups.

`backup` uses SQLite's online-backup API, so committed WAL content is included
without copying a live database file byte-for-byte. The operation refuses to
overwrite an existing target and writes a checksum/integrity manifest beside it.

`verify` runs integrity and foreign-key checks and, when the default manifest is
present, verifies the SHA-256 checksum too.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anomaly_detection_engine.config import load_config, load_dotenv


def manifest_path(backup_path: Path) -> Path:
    return backup_path.with_name(backup_path.name + ".manifest.json")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_database(path: Path, *, expected_sha256: str | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"database does not exist: {path}")
    checksum = sha256_file(path)
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        integrity_rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_key_rows = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
    except sqlite3.DatabaseError as error:
        raise ValueError(f"invalid SQLite database {path}: {error}") from error
    finally:
        connection.close()

    checksum_matches = expected_sha256 is None or checksum == expected_sha256
    ok = integrity_rows == ["ok"] and not foreign_key_rows and checksum_matches
    return {
        "path": str(path.resolve()),
        "ok": ok,
        "sha256": checksum,
        "checksum_matches": checksum_matches,
        "integrity_check": integrity_rows,
        "foreign_key_violations": foreign_key_rows,
        "user_version": user_version,
        "page_count": page_count,
        "size_bytes": path.stat().st_size,
    }


def create_backup(source: Path, target: Path) -> dict[str, Any]:
    if not source.is_file():
        raise ValueError(f"source database does not exist: {source}")
    if source.resolve() == target.resolve():
        raise ValueError("backup target must differ from the source database")
    target_manifest = manifest_path(target)
    if target.exists() or target_manifest.exists():
        raise ValueError(f"backup target or manifest already exists: {target}")

    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
    except BaseException:
        target_connection.close()
        source_connection.close()
        target.unlink(missing_ok=True)
        raise
    else:
        target_connection.close()
        source_connection.close()

    verification = verify_database(target)
    if not verification["ok"]:
        target.unlink(missing_ok=True)
        raise ValueError("new backup failed integrity verification")

    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "source_path": str(source.resolve()),
        "backup": verification,
    }
    target_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _default_backup_name() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path("backups") / f"anomaly-detection-{stamp}.db"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="Create a verified online backup")
    backup_parser.add_argument("--source-db", type=Path, default=None)
    backup_parser.add_argument("--output", type=Path, default=None)

    verify_parser = subparsers.add_parser("verify", help="Verify database integrity/checksum")
    verify_parser.add_argument("database", type=Path)
    verify_parser.add_argument(
        "--manifest", type=Path, default=None, help="Manifest to verify (default: beside database)"
    )

    args = parser.parse_args(argv)
    load_dotenv()
    try:
        if args.command == "backup":
            source = args.source_db or Path(load_config().db_path)
            target = args.output or _default_backup_name()
            result = create_backup(source, target)
        else:
            selected_manifest = args.manifest or manifest_path(args.database)
            expected_checksum = None
            if selected_manifest.exists():
                data = json.loads(selected_manifest.read_text(encoding="utf-8"))
                expected_checksum = data["backup"]["sha256"]
            result = verify_database(args.database, expected_sha256=expected_checksum)
    except (ValueError, KeyError, json.JSONDecodeError) as error:
        parser.error(str(error))

    print(json.dumps(result, indent=2, sort_keys=True))
    if args.command == "verify" and not result["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
