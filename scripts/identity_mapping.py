#!/usr/bin/env python3
"""Manual escape hatch for FixtureCatalog's identity-mapping trust model.

Since migration 19 (see storage/migrations.py), a team/competition/event
mapping resolved by anything less certain than an exact/alias match or
verified fixture context stays UNVERIFIED, and a provider id that stops
matching its own previously-seen name gets marked SUSPECT rather than
silently repointed (see FixtureCatalog._resolve_team/_resolve_competition's
own "name_drift" handling). Nothing in this project currently clears a
SUSPECT mapping or resolves an UNVERIFIED one by hand -- FixtureCatalog.
verify_team_mapping/verify_competition_mapping exist and are tested, but
had no operational caller. This script is that caller: a deliberately
minimal, three-command CLI, not the fuller re-audit/report tool a
production identity system eventually wants (that's future work, not
this).

Usage:
    # List every SUSPECT/CONFLICT mapping across the database, so you
    # know what actually needs a decision.
    python scripts/identity_mapping.py suspects
    python scripts/identity_mapping.py suspects --sport football

    # Record a human-verified team mapping -- clears a SUSPECT id
    # mapping for this (provider, id) too, if one exists.
    python scripts/identity_mapping.py team \
        --provider mozzart --sport football \
        --source-name "Westham Untd" --team-id team-abc123 \
        --competition-id competition-xyz \
        --source-team-id 12345

    # Record a human-verified competition mapping.
    python scripts/identity_mapping.py competition \
        --provider mozzart --sport football \
        --source-name "Engleska Premier Liga" \
        --competition-id competition-123 --country England \
        --source-competition-id 67890

By default this opens whatever DB_PATH (or its default) load_config()
resolves to -- the same database app.py/poller.py write to. Pass
--db-path to point at a specific file instead. --competition-id/
--source-team-id/--source-competition-id/--country are optional the same
way FixtureCatalog.verify_team_mapping/verify_competition_mapping's own
matching parameters are.
"""

import argparse
import sqlite3

from anomaly_detection_engine.config import load_config, load_dotenv
from anomaly_detection_engine.storage.database import create_connection, initialize_database
from anomaly_detection_engine.storage.fixture_catalog import FixtureCatalog


def _open_catalog(
    provider_id: str, db_path: str | None
) -> tuple[FixtureCatalog, sqlite3.Connection]:
    load_dotenv()
    config = load_config()
    connection = create_connection(db_path or config.db_path)
    initialize_database(connection)
    return FixtureCatalog(connection, provider_id=provider_id), connection


def _cmd_team(args: argparse.Namespace) -> None:
    catalog, _ = _open_catalog(args.provider, args.db_path)
    catalog.verify_team_mapping(
        sport=args.sport,
        source_name=args.source_name,
        canonical_team_id=args.team_id,
        source_team_id=args.source_team_id,
        competition_id=args.competition_id,
    )
    print(
        f"Verified: {args.provider!r}/{args.sport!r} {args.source_name!r} -> "
        f"{args.team_id}"
        + (f" (source_team_id={args.source_team_id})" if args.source_team_id else "")
    )


def _cmd_competition(args: argparse.Namespace) -> None:
    catalog, _ = _open_catalog(args.provider, args.db_path)
    catalog.verify_competition_mapping(
        sport=args.sport,
        source_name=args.source_name,
        canonical_competition_id=args.competition_id,
        source_competition_id=args.source_competition_id,
        country=args.country,
    )
    print(
        f"Verified: {args.provider!r}/{args.sport!r} {args.source_name!r} -> "
        f"{args.competition_id}"
        + (f" (source_competition_id={args.source_competition_id})"
           if args.source_competition_id else "")
    )


def _print_rows(title: str, rows: list[sqlite3.Row]) -> None:
    if not rows:
        return
    print(f"\n{title} ({len(rows)}):")
    for row in rows:
        print("  " + "  ".join(f"{k}={row[k]!r}" for k in row.keys()))


def _cmd_suspects(args: argparse.Namespace) -> None:
    _, connection = _open_catalog("_unused_", args.db_path)
    sport_filter = " AND sport = :sport" if args.sport else ""
    params = {"sport": args.sport} if args.sport else {}

    _print_rows(
        "SUSPECT source_team_id_mappings",
        connection.execute(
            f"""
            SELECT source, sport, source_team_id, team_id, last_seen_raw_name,
                   last_verified_raw_name
            FROM source_team_id_mappings WHERE trust_state = 'SUSPECT'{sport_filter}
            """,
            params,
        ).fetchall(),
    )
    _print_rows(
        "SUSPECT source_competition_id_mappings",
        connection.execute(
            f"""
            SELECT source, sport, source_competition_id, competition_id,
                   last_seen_raw_name, last_verified_raw_name
            FROM source_competition_id_mappings WHERE trust_state = 'SUSPECT'{sport_filter}
            """,
            params,
        ).fetchall(),
    )
    _print_rows(
        "SUSPECT source_event_mappings",
        connection.execute(
            f"""
            SELECT source, source_event_id, event_id, last_verified_home_name,
                   last_verified_away_name, last_verified_competition_name
            FROM source_event_mappings WHERE trust_state = 'SUSPECT'{sport_filter}
            """,
            params,
        ).fetchall(),
    )
    _print_rows(
        "SUSPECT source_team_mappings",
        connection.execute(
            f"""
            SELECT source, sport, source_team_name, competition_id, team_id
            FROM source_team_mappings WHERE trust_state = 'SUSPECT'{sport_filter}
            """,
            params,
        ).fetchall(),
    )
    _print_rows(
        "SUSPECT source_competition_mappings",
        connection.execute(
            f"""
            SELECT source, sport, source_competition_name, country_key, competition_id
            FROM source_competition_mappings WHERE trust_state = 'SUSPECT'{sport_filter}
            """,
            params,
        ).fetchall(),
    )
    _print_rows(
        "CONFLICT teams",
        connection.execute(
            f"SELECT id, canonical_name, sport FROM teams "
            f"WHERE identity_status = 'CONFLICT'{sport_filter}",
            params,
        ).fetchall(),
    )
    _print_rows(
        "CONFLICT competitions",
        connection.execute(
            f"SELECT id, canonical_name, sport, country FROM competitions "
            f"WHERE identity_status = 'CONFLICT'{sport_filter}",
            params,
        ).fetchall(),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db-path", default=None, help="Override the database file to open")
    subparsers = parser.add_subparsers(dest="command", required=True)

    team_parser = subparsers.add_parser("team", help="Record a human-verified team mapping")
    team_parser.add_argument("--provider", required=True)
    team_parser.add_argument("--sport", required=True)
    team_parser.add_argument("--source-name", required=True)
    team_parser.add_argument("--team-id", required=True, help="Existing canonical team id")
    team_parser.add_argument("--competition-id", default=None)
    team_parser.add_argument("--source-team-id", default=None)
    team_parser.set_defaults(func=_cmd_team)

    competition_parser = subparsers.add_parser(
        "competition", help="Record a human-verified competition mapping"
    )
    competition_parser.add_argument("--provider", required=True)
    competition_parser.add_argument("--sport", required=True)
    competition_parser.add_argument("--source-name", required=True)
    competition_parser.add_argument(
        "--competition-id", required=True, help="Existing canonical competition id"
    )
    competition_parser.add_argument("--country", default=None)
    competition_parser.add_argument("--source-competition-id", default=None)
    competition_parser.set_defaults(func=_cmd_competition)

    suspects_parser = subparsers.add_parser(
        "suspects", help="List every SUSPECT/CONFLICT mapping in the database"
    )
    suspects_parser.add_argument("--sport", default=None)
    suspects_parser.set_defaults(func=_cmd_suspects)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ValueError as e:
        raise SystemExit(f"error: {e}") from e


if __name__ == "__main__":
    main()
