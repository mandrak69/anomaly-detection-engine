#!/usr/bin/env python3
"""Inspect and safely remove disposable orphan identity entities.

An orphan is only a canonical team/competition with no event references. That
alone is not enough to delete it: VERIFIED, SUSPECT and REJECTED mappings are
identity knowledge and must survive. Only PROVISIONAL orphans with no durable
mapping/reference evidence are GC-safe; their UNVERIFIED mappings and
team_competitions rows are deleted with them in one transaction.

Dry-run is the default. Use --apply to perform the deletion.

Examples:
    python scripts/cleanup_identity.py
    python scripts/cleanup_identity.py --entity team
    python scripts/cleanup_identity.py --entity team --id team-123
    python scripts/cleanup_identity.py --apply
    python scripts/cleanup_identity.py --json
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from anomaly_detection_engine.config import load_config, load_dotenv
from anomaly_detection_engine.identity_cleanup import (
    EntityType,
    IdentityCleanupCandidate,
    apply_identity_cleanup,
    find_identity_cleanup_candidates,
)
from anomaly_detection_engine.storage.database import create_connection, initialize_database


def _render_candidate(candidate: IdentityCleanupCandidate) -> str:
    disposition = "GC_SAFE" if candidate.safe_to_delete else "KEEP"
    lines = [
        f"{candidate.entity_type.upper()} {candidate.entity_id} {candidate.canonical_name!r}",
        f"  sport:          {candidate.sport}",
        f"  events:         {candidate.event_count}",
        f"  VERIFIED:       {candidate.verified_mappings}",
        f"  UNVERIFIED:     {candidate.unverified_mappings}",
        f"  SUSPECT:        {candidate.suspect_mappings}",
        f"  REJECTED:       {candidate.rejected_mappings}",
        f"  other mappings: {candidate.other_mappings}",
        f"  relations:      {candidate.relation_count}",
        f"  action:         {disposition}",
    ]
    if candidate.protection_reasons:
        lines.append("  reason:         " + ", ".join(candidate.protection_reasons))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db-path", default=None, help="Override the database file to open")
    parser.add_argument(
        "--entity",
        choices=("team", "competition"),
        default=None,
        help="Limit inspection/cleanup to one canonical entity type",
    )
    parser.add_argument(
        "--id",
        dest="entity_id",
        default=None,
        help="Limit to one canonical entity id (requires --entity)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete GC-safe entities; default is dry-run",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    if args.entity_id is not None and args.entity is None:
        parser.error("--id requires --entity")

    load_dotenv()
    config = load_config()
    db_path = args.db_path or config.db_path
    connection = create_connection(db_path)
    initialize_database(connection)

    entity_type: EntityType | None = args.entity
    try:
        candidates = find_identity_cleanup_candidates(
            connection,
            entity_type=entity_type,
            entity_id=args.entity_id,
        )
        safe = [candidate for candidate in candidates if candidate.safe_to_delete]

        if args.json:
            result: dict[str, object] = {
                "mode": "apply" if args.apply else "dry-run",
                "candidates": [candidate.to_evidence() for candidate in candidates],
                "gc_safe_count": len(safe),
            }
        else:
            print("Identity cleanup: " + ("APPLY" if args.apply else "DRY RUN"))
            if not candidates:
                print("No orphan provisional identities found.")
            else:
                for candidate in candidates:
                    print()
                    print(_render_candidate(candidate))
                print()
                print(f"GC-safe: {len(safe)} / {len(candidates)} orphan candidate(s)")

        if args.apply:
            summary = apply_identity_cleanup(
                connection,
                entity_type=entity_type,
                entity_id=args.entity_id,
            )
            if args.json:
                result["deleted"] = {
                    "teams": summary.teams_deleted,
                    "competitions": summary.competitions_deleted,
                    "mappings": summary.mappings_deleted,
                    "relations": summary.relations_deleted,
                    "total_rows": summary.total_rows_deleted,
                }
            else:
                print(
                    "Deleted: "
                    f"teams={summary.teams_deleted}, "
                    f"competitions={summary.competitions_deleted}, "
                    f"mappings={summary.mappings_deleted}, "
                    f"relations={summary.relations_deleted}, "
                    f"total_rows={summary.total_rows_deleted}"
                )

        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
