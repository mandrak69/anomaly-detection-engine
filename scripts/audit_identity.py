#!/usr/bin/env python3
"""Read-only audit for canonical identity and mapping quality.

The audit never migrates or writes the database. It reports conditions that
need review rather than guessing a repair: duplicate fixtures, orphaned
canonical entities, conflicting identities, unresolved mappings, and multiple
verified reference-provider ids attached to one canonical entity.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from anomaly_detection_engine.config import load_config, load_dotenv


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    evidence: list[dict[str, Any]]


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _rows(connection: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql).fetchall()]


def audit_identity(connection: sqlite3.Connection) -> list[Finding]:
    findings: list[Finding] = []

    if _table_exists(connection, "events"):
        duplicates = _rows(
            connection,
            """
            SELECT sport, competition_id, home_team_id, away_team_id, start_time,
                   COUNT(*) AS duplicate_count, GROUP_CONCAT(id) AS event_ids
            FROM events
            GROUP BY sport, competition_id, home_team_id, away_team_id, start_time
            HAVING COUNT(*) > 1
            ORDER BY duplicate_count DESC
            """,
        )
        if duplicates:
            findings.append(
                Finding(
                    "ERROR",
                    "duplicate-canonical-events",
                    "Multiple canonical events have identical fixture identity.",
                    duplicates,
                )
            )

    if _table_exists(connection, "teams") and _table_exists(connection, "events"):
        status_filter = (
            "AND t.identity_status = 'PROVISIONAL'"
            if "identity_status" in _columns(connection, "teams")
            else ""
        )
        orphan_teams = _rows(
            connection,
            f"""
            SELECT t.id, t.canonical_name, t.sport
            FROM teams t
            WHERE NOT EXISTS (
                SELECT 1 FROM events e
                WHERE e.home_team_id = t.id OR e.away_team_id = t.id
            ) {status_filter}
            ORDER BY t.sport, t.canonical_name
            """,
        )
        if orphan_teams:
            findings.append(
                Finding(
                    "WARNING",
                    "orphan-provisional-teams",
                    "Provisional teams are not referenced by any event.",
                    orphan_teams,
                )
            )

    if _table_exists(connection, "competitions") and _table_exists(connection, "events"):
        status_filter = (
            "AND c.identity_status = 'PROVISIONAL'"
            if "identity_status" in _columns(connection, "competitions")
            else ""
        )
        orphan_competitions = _rows(
            connection,
            f"""
            SELECT c.id, c.canonical_name, c.sport, c.country
            FROM competitions c
            WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.competition_id = c.id)
              {status_filter}
            ORDER BY c.sport, c.canonical_name
            """,
        )
        if orphan_competitions:
            findings.append(
                Finding(
                    "WARNING",
                    "orphan-provisional-competitions",
                    "Provisional competitions are not referenced by any event.",
                    orphan_competitions,
                )
            )

    for table in ("teams", "competitions", "events"):
        if not _table_exists(connection, table) or "identity_status" not in _columns(
            connection, table
        ):
            continue
        conflicts = _rows(
            connection,
            f"SELECT * FROM {table} WHERE identity_status = 'CONFLICT' ORDER BY id",
        )
        if conflicts:
            findings.append(
                Finding(
                    "ERROR",
                    f"conflicting-{table}",
                    f"Canonical {table} are quarantined as CONFLICT.",
                    conflicts,
                )
            )

    mapping_tables = (
        "source_team_mappings",
        "source_competition_mappings",
        "source_team_id_mappings",
        "source_competition_id_mappings",
        "source_event_mappings",
    )
    for table in mapping_tables:
        if not _table_exists(connection, table) or "trust_state" not in _columns(
            connection, table
        ):
            continue
        unresolved = _rows(
            connection,
            f"""
            SELECT trust_state, COUNT(*) AS mapping_count
            FROM {table}
            WHERE trust_state IN ('SUSPECT', 'UNVERIFIED')
            GROUP BY trust_state ORDER BY trust_state
            """,
        )
        if unresolved:
            findings.append(
                Finding(
                    "ERROR" if any(r["trust_state"] == "SUSPECT" for r in unresolved)
                    else "WARNING",
                    f"unresolved-{table}",
                    f"{table} contains mappings outside the verified fast path.",
                    unresolved,
                )
            )

    reference_checks = (
        ("source_team_id_mappings", "team_id", "source_team_id"),
        ("source_competition_id_mappings", "competition_id", "source_competition_id"),
        ("source_event_mappings", "event_id", "source_event_id"),
    )
    for table, canonical_column, source_id_column in reference_checks:
        if not _table_exists(connection, table):
            continue
        collisions = _rows(
            connection,
            f"""
            SELECT {canonical_column} AS canonical_id,
                   COUNT(DISTINCT {source_id_column}) AS provider_id_count,
                   GROUP_CONCAT(DISTINCT {source_id_column}) AS provider_ids
            FROM {table}
            WHERE source = 'api-football' AND trust_state = 'VERIFIED'
            GROUP BY {canonical_column}
            HAVING COUNT(DISTINCT {source_id_column}) > 1
            ORDER BY provider_id_count DESC
            """,
        )
        if collisions:
            findings.append(
                Finding(
                    "ERROR",
                    f"multiple-reference-ids-{table}",
                    "One canonical entity has multiple verified API-Football ids.",
                    collisions,
                )
            )

    if _table_exists(connection, "competitions"):
        countryless_collisions = _rows(
            connection,
            """
            SELECT bare.id AS countryless_id, bare.sport, bare.canonical_name,
                   COUNT(country_specific.id) AS contextual_candidates,
                   GROUP_CONCAT(country_specific.country) AS countries
            FROM competitions bare
            JOIN competitions country_specific
              ON country_specific.sport = bare.sport
             AND country_specific.canonical_name = bare.canonical_name
             AND country_specific.id != bare.id
             AND country_specific.country IS NOT NULL
            WHERE bare.country IS NULL
            GROUP BY bare.id, bare.sport, bare.canonical_name
            HAVING COUNT(country_specific.id) > 1
            ORDER BY contextual_candidates DESC
            """,
        )
        if countryless_collisions:
            findings.append(
                Finding(
                    "WARNING",
                    "countryless-competition-collisions",
                    "Countryless competitions share a name with multiple country-specific rows.",
                    countryless_collisions,
                )
            )

    return findings


def render_text(findings: list[Finding]) -> str:
    if not findings:
        return "Identity audit: no findings."
    lines = [f"Identity audit: {len(findings)} finding group(s)."]
    for finding in findings:
        lines.append(
            f"\n[{finding.severity}] {finding.code}: {finding.message} "
            f"({len(finding.evidence)} row(s))"
        )
        for row in finding.evidence[:20]:
            lines.append("  " + "  ".join(f"{key}={value!r}" for key, value in row.items()))
        if len(finding.evidence) > 20:
            lines.append(f"  ... {len(finding.evidence) - 20} more row(s)")
    return "\n".join(lines)


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"database does not exist: {path}")
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="Exit with status 2 when the audit finds anything",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    db_path = args.db_path or Path(load_config().db_path)
    try:
        connection = _open_read_only(db_path)
    except ValueError as error:
        parser.error(str(error))
    try:
        findings = audit_identity(connection)
    finally:
        connection.close()

    if args.json:
        print(json.dumps([asdict(finding) for finding in findings], indent=2, sort_keys=True))
    else:
        print(render_text(findings))
    if args.fail_on_findings and findings:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
