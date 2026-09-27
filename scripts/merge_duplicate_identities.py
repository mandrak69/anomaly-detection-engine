#!/usr/bin/env python3
"""One-off data repair: merge canonical teams/competitions/events that were
split apart by the normalize()-checked-exact-before-alias bug (fixed in
team_normalizer.py -- see test_alias_wins_over_an_exact_match_against_a_
stale_duplicate_row). That bug only stopped *new* mistakes; rows created
before the fix stay split until repaired here, the same
"fix the rule, then separately repair the data it already produced" split
this project has hit before with team-name collisions.

Merging a team or competition can make two previously-distinct events
collide on (sport, competition_id, home_team_id, away_team_id, start_time)
-- merge_duplicate_events() closes that using the exact grouping
audit_identity.py's own "duplicate-canonical-events" check uses, so running
that audit afterward is the correctness check for this script.

Usage:
    python scripts/merge_duplicate_identities.py --db-path data/copy.db \
        --team stale-team-id:target-team-id \
        --competition stale-competition-id:target-competition-id
    python scripts/merge_duplicate_identities.py --db-path data/copy.db \
        --rename-competition id:New Name:Country --move-events id1,id2:target-id
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from anomaly_detection_engine.config import load_config, load_dotenv


def _repoint_team_competitions(connection: sqlite3.Connection, stale: str, target: str) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO team_competitions
            (team_id, competition_id, first_seen_at, last_seen_at)
        SELECT ?, competition_id, first_seen_at, last_seen_at
        FROM team_competitions WHERE team_id = ?
        """,
        (target, stale),
    )
    connection.execute("DELETE FROM team_competitions WHERE team_id = ?", (stale,))


def _repoint_team_competitions_for_competition(
    connection: sqlite3.Connection, stale: str, target: str
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO team_competitions
            (team_id, competition_id, first_seen_at, last_seen_at)
        SELECT team_id, ?, first_seen_at, last_seen_at
        FROM team_competitions WHERE competition_id = ?
        """,
        (target, stale),
    )
    connection.execute("DELETE FROM team_competitions WHERE competition_id = ?", (stale,))


def merge_teams(connection: sqlite3.Connection, stale: str, target: str) -> dict[str, int]:
    counts = {}
    _repoint_team_competitions(connection, stale, target)
    for column in ("home_team_id", "away_team_id"):
        cur = connection.execute(
            f"UPDATE events SET {column} = ? WHERE {column} = ?", (target, stale)
        )
        counts[f"events.{column}"] = cur.rowcount
    cur = connection.execute(
        "UPDATE source_team_mappings SET team_id = ? WHERE team_id = ?", (target, stale)
    )
    counts["source_team_mappings"] = cur.rowcount
    cur = connection.execute(
        "UPDATE source_team_id_mappings SET team_id = ? WHERE team_id = ?", (target, stale)
    )
    counts["source_team_id_mappings"] = cur.rowcount
    connection.execute("DELETE FROM teams WHERE id = ?", (stale,))
    return counts


def merge_competitions(connection: sqlite3.Connection, stale: str, target: str) -> dict[str, int]:
    counts = {}
    _repoint_team_competitions_for_competition(connection, stale, target)
    cur = connection.execute(
        "UPDATE events SET competition_id = ? WHERE competition_id = ?", (target, stale)
    )
    counts["events"] = cur.rowcount
    cur = connection.execute(
        "UPDATE source_competition_mappings SET competition_id = ? WHERE competition_id = ?",
        (target, stale),
    )
    counts["source_competition_mappings"] = cur.rowcount
    cur = connection.execute(
        "UPDATE source_competition_id_mappings SET competition_id = ? WHERE competition_id = ?",
        (target, stale),
    )
    counts["source_competition_id_mappings"] = cur.rowcount
    connection.execute("DELETE FROM competitions WHERE id = ?", (stale,))
    return counts


def rename_competition(
    connection: sqlite3.Connection, competition_id: str, new_name: str, country: str | None
) -> None:
    connection.execute(
        "UPDATE competitions SET canonical_name = ?, country = ? WHERE id = ?",
        (new_name, country, competition_id),
    )


def move_events(
    connection: sqlite3.Connection, event_ids: list[str], target_competition_id: str
) -> int:
    """Repoints specific events to a different competition -- for splitting
    a competition that turned out to already contain two genuinely
    different real leagues collided under one exact-matched raw name
    (e.g. "Serija A" meaning Brazil's Serie A to Mozzart and Italy's to
    Meridianbet), rather than merging two rows that really are the same.
    """
    placeholders = ",".join("?" * len(event_ids))
    cur = connection.execute(
        f"UPDATE events SET competition_id = ? WHERE id IN ({placeholders})",
        (target_competition_id, *event_ids),
    )
    return cur.rowcount


def merge_duplicate_events(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Finds events now sharing (sport, competition_id, home_team_id,
    away_team_id, start_time) -- the same identity audit_identity.py's
    duplicate-canonical-events check uses -- and collapses each group onto
    one survivor (the row with the most odds_snapshots, ties broken by the
    lexicographically smallest id for determinism).
    """
    groups = connection.execute(
        """
        SELECT sport, competition_id, home_team_id, away_team_id, start_time,
               GROUP_CONCAT(id) AS event_ids
        FROM events
        GROUP BY sport, competition_id, home_team_id, away_team_id, start_time
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    merges: list[dict[str, object]] = []
    for group in groups:
        event_ids = group["event_ids"].split(",")
        counted = [
            (
                connection.execute(
                    "SELECT COUNT(*) FROM odds_snapshots WHERE event_id = ?", (eid,)
                ).fetchone()[0],
                eid,
            )
            for eid in event_ids
        ]
        counted.sort(key=lambda pair: (-pair[0], pair[1]))
        survivor = counted[0][1]
        duplicates = [eid for _, eid in counted[1:]]

        for dup in duplicates:
            for table in ("odds_snapshots", "signals", "movements"):
                connection.execute(
                    f"UPDATE {table} SET event_id = ? WHERE event_id = ?", (survivor, dup)
                )
            connection.execute(
                "UPDATE source_event_mappings SET event_id = ? WHERE event_id = ?",
                (survivor, dup),
            )
            connection.execute("DELETE FROM event_status WHERE event_id = ?", (dup,))
            connection.execute("DELETE FROM events WHERE id = ?", (dup,))

        merges.append(
            {
                "survivor": survivor,
                "duplicates": duplicates,
                "sport": group["sport"],
                "competition_id": group["competition_id"],
                "start_time": group["start_time"],
            }
        )
    return merges


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument(
        "--team",
        action="append",
        default=[],
        metavar="STALE_ID:TARGET_ID",
        help="Merge a stale team id into a target team id (repeatable)",
    )
    parser.add_argument(
        "--competition",
        action="append",
        default=[],
        metavar="STALE_ID:TARGET_ID",
        help="Merge a stale competition id into a target competition id (repeatable)",
    )
    parser.add_argument(
        "--rename-competition",
        action="append",
        default=[],
        metavar="ID:NEW_NAME:COUNTRY",
        help="Rename a competition and set its country (COUNTRY may be empty for None; repeatable)",
    )
    parser.add_argument(
        "--move-events",
        action="append",
        default=[],
        metavar="EVENT_ID1,EVENT_ID2,...:TARGET_COMPETITION_ID",
        help="Repoint specific events to a different competition, for splitting a "
        "wrongly-collided competition rather than merging (repeatable)",
    )
    parser.add_argument(
        "--delete-empty-competition",
        action="append",
        default=[],
        metavar="ID",
        help="Delete a competition, refusing if it still has any events (repeatable)",
    )
    args = parser.parse_args()

    load_dotenv()
    db_path = args.db_path or Path(load_config().db_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for spec in args.rename_competition:
            competition_id, new_name, country = spec.split(":")
            rename_competition(connection, competition_id, new_name, country or None)
            print(
                f"renamed competition {competition_id} -> {new_name!r} "
                f"(country={country or None})"
            )
        for spec in args.move_events:
            ids_part, target = spec.rsplit(":", 1)
            event_ids = ids_part.split(",")
            moved = move_events(connection, event_ids, target)
            print(f"moved {moved} event(s) -> {target}: {event_ids}")
        for competition_id in args.delete_empty_competition:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM events WHERE competition_id = ?", (competition_id,)
            ).fetchone()[0]
            if remaining:
                raise ValueError(
                    f"refusing to delete {competition_id}: still has {remaining} event(s)"
                )
            connection.execute(
                "DELETE FROM team_competitions WHERE competition_id = ?", (competition_id,)
            )
            connection.execute(
                "DELETE FROM source_competition_mappings WHERE competition_id = ?",
                (competition_id,),
            )
            connection.execute(
                "DELETE FROM source_competition_id_mappings WHERE competition_id = ?",
                (competition_id,),
            )
            connection.execute("DELETE FROM competitions WHERE id = ?", (competition_id,))
            print(f"deleted empty competition {competition_id}")
        for spec in args.team:
            stale, target = spec.split(":")
            result = merge_teams(connection, stale, target)
            print(f"merged team {stale} -> {target}: {result}")
        for spec in args.competition:
            stale, target = spec.split(":")
            result = merge_competitions(connection, stale, target)
            print(f"merged competition {stale} -> {target}: {result}")
        event_merges = merge_duplicate_events(connection)
        for merge in event_merges:
            print(f"merged duplicate events: {merge}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
