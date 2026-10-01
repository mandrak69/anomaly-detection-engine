from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection, Row
from typing import Literal

EntityType = Literal["team", "competition"]


@dataclass(frozen=True)
class IdentityCleanupCandidate:
    """A provisional canonical entity that is currently orphaned.

    Orphaned only means "no event references it". ``safe_to_delete`` is
    deliberately stricter: durable identity knowledge must survive even when
    the canonical entity is temporarily unused by events.
    """

    entity_type: EntityType
    entity_id: str
    canonical_name: str
    sport: str
    event_count: int
    verified_mappings: int
    unverified_mappings: int
    suspect_mappings: int
    rejected_mappings: int
    other_mappings: int
    relation_count: int
    reference_provider: str | None
    reference_provider_id: str | None

    @property
    def safe_to_delete(self) -> bool:
        return not self.protection_reasons

    @property
    def protection_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.event_count:
            reasons.append("referenced-by-event")
        if self.reference_provider is not None or self.reference_provider_id is not None:
            reasons.append("reference-identity")
        if self.verified_mappings:
            reasons.append("verified-mapping")
        if self.suspect_mappings:
            reasons.append("suspect-mapping")
        if self.rejected_mappings:
            reasons.append("rejected-mapping")
        if self.other_mappings:
            reasons.append("unknown-trust-state")
        return tuple(reasons)

    def to_evidence(self) -> dict[str, object]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "canonical_name": self.canonical_name,
            "sport": self.sport,
            "event_count": self.event_count,
            "verified_mappings": self.verified_mappings,
            "unverified_mappings": self.unverified_mappings,
            "suspect_mappings": self.suspect_mappings,
            "rejected_mappings": self.rejected_mappings,
            "other_mappings": self.other_mappings,
            "relation_count": self.relation_count,
            "safe_to_delete": self.safe_to_delete,
            "protection_reasons": list(self.protection_reasons),
        }


@dataclass(frozen=True)
class IdentityCleanupSummary:
    teams_deleted: int
    competitions_deleted: int
    mappings_deleted: int
    relations_deleted: int

    @property
    def total_rows_deleted(self) -> int:
        return (
            self.teams_deleted
            + self.competitions_deleted
            + self.mappings_deleted
            + self.relations_deleted
        )


def find_identity_cleanup_candidates(
    connection: Connection,
    *,
    entity_type: EntityType | None = None,
    entity_id: str | None = None,
) -> list[IdentityCleanupCandidate]:
    """Returns orphaned provisional teams/competitions and classifies safety.

    Only UNVERIFIED mappings may disappear with a provisional canonical
    entity. VERIFIED, SUSPECT and REJECTED mappings are durable identity
    knowledge. An unknown future trust state also protects the entity rather
    than being guessed disposable.
    """
    candidates: list[IdentityCleanupCandidate] = []
    if entity_type in (None, "team"):
        candidates.extend(_team_candidates(connection, entity_id=entity_id))
    if entity_type in (None, "competition"):
        candidates.extend(_competition_candidates(connection, entity_id=entity_id))
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.entity_type,
            candidate.sport,
            candidate.canonical_name.casefold(),
            candidate.entity_id,
        ),
    )


def apply_identity_cleanup(
    connection: Connection,
    *,
    entity_type: EntityType | None = None,
    entity_id: str | None = None,
) -> IdentityCleanupSummary:
    """Deletes only candidates that are still GC-safe inside one transaction.

    The candidate set is recomputed after BEGIN IMMEDIATE so a concurrent
    ingestion process cannot make a previously-safe entity meaningful between
    inspection and deletion. Any failure rolls the entire cleanup back.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        candidates = find_identity_cleanup_candidates(
            connection, entity_type=entity_type, entity_id=entity_id
        )
        safe = [candidate for candidate in candidates if candidate.safe_to_delete]

        mappings_deleted = 0
        relations_deleted = 0
        teams_deleted = 0
        competitions_deleted = 0

        for candidate in safe:
            if candidate.entity_type == "team":
                mappings_deleted += _delete_team_mappings(connection, candidate.entity_id)
                relations_deleted += connection.execute(
                    "DELETE FROM team_competitions WHERE team_id = ?",
                    (candidate.entity_id,),
                ).rowcount
                deleted = connection.execute(
                    """
                    DELETE FROM teams
                    WHERE id = ?
                      AND identity_status = 'PROVISIONAL'
                      AND reference_provider IS NULL
                      AND reference_provider_id IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM events
                          WHERE home_team_id = teams.id OR away_team_id = teams.id
                      )
                    """,
                    (candidate.entity_id,),
                ).rowcount
                if deleted != 1:
                    raise RuntimeError(
                        f"GC-safe team {candidate.entity_id!r} was not deleted"
                    )
                teams_deleted += deleted
            else:
                mappings_deleted += _delete_competition_mappings(
                    connection, candidate.entity_id
                )
                relations_deleted += connection.execute(
                    "DELETE FROM team_competitions WHERE competition_id = ?",
                    (candidate.entity_id,),
                ).rowcount
                deleted = connection.execute(
                    """
                    DELETE FROM competitions
                    WHERE id = ?
                      AND identity_status = 'PROVISIONAL'
                      AND reference_provider IS NULL
                      AND reference_provider_id IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM events
                          WHERE competition_id = competitions.id
                      )
                    """,
                    (candidate.entity_id,),
                ).rowcount
                if deleted != 1:
                    raise RuntimeError(
                        f"GC-safe competition {candidate.entity_id!r} was not deleted"
                    )
                competitions_deleted += deleted

        connection.commit()
    except BaseException:
        connection.rollback()
        raise

    return IdentityCleanupSummary(
        teams_deleted=teams_deleted,
        competitions_deleted=competitions_deleted,
        mappings_deleted=mappings_deleted,
        relations_deleted=relations_deleted,
    )


def _team_candidates(
    connection: Connection, *, entity_id: str | None
) -> list[IdentityCleanupCandidate]:
    params: tuple[object, ...] = ()
    id_filter = ""
    if entity_id is not None:
        id_filter = "AND t.id = ?"
        params = (entity_id,)

    rows = connection.execute(
        f"""
        SELECT t.id, t.canonical_name, t.sport,
               t.reference_provider, t.reference_provider_id,
               (
                   SELECT COUNT(*) FROM events e
                   WHERE e.home_team_id = t.id OR e.away_team_id = t.id
               ) AS event_count,
               (
                   SELECT COUNT(*) FROM team_competitions tc
                   WHERE tc.team_id = t.id
               ) AS relation_count
        FROM teams t
        WHERE t.identity_status = 'PROVISIONAL'
          AND NOT EXISTS (
              SELECT 1 FROM events e
              WHERE e.home_team_id = t.id OR e.away_team_id = t.id
          )
          {id_filter}
        """,
        params,
    ).fetchall()

    return [
        _build_candidate(
            connection,
            entity_type="team",
            row=row,
            mapping_tables=(
                ("source_team_mappings", "team_id"),
                ("source_team_id_mappings", "team_id"),
            ),
        )
        for row in rows
    ]


def _competition_candidates(
    connection: Connection, *, entity_id: str | None
) -> list[IdentityCleanupCandidate]:
    params: tuple[object, ...] = ()
    id_filter = ""
    if entity_id is not None:
        id_filter = "AND c.id = ?"
        params = (entity_id,)

    rows = connection.execute(
        f"""
        SELECT c.id, c.canonical_name, c.sport,
               c.reference_provider, c.reference_provider_id,
               (
                   SELECT COUNT(*) FROM events e
                   WHERE e.competition_id = c.id
               ) AS event_count,
               (
                   SELECT COUNT(*) FROM team_competitions tc
                   WHERE tc.competition_id = c.id
               ) AS relation_count
        FROM competitions c
        WHERE c.identity_status = 'PROVISIONAL'
          AND NOT EXISTS (
              SELECT 1 FROM events e
              WHERE e.competition_id = c.id
          )
          {id_filter}
        """,
        params,
    ).fetchall()

    return [
        _build_candidate(
            connection,
            entity_type="competition",
            row=row,
            mapping_tables=(
                ("source_competition_mappings", "competition_id"),
                ("source_competition_id_mappings", "competition_id"),
                # Contextual team mappings also point at the canonical
                # competition (migration 20). Treat them as identity knowledge,
                # not unrelated rows, or cleanup could leave a dangling context.
                ("source_team_mappings", "competition_id"),
            ),
        )
        for row in rows
    ]


def _build_candidate(
    connection: Connection,
    *,
    entity_type: EntityType,
    row: Row,
    mapping_tables: tuple[tuple[str, str], ...],
) -> IdentityCleanupCandidate:
    entity_id = str(row["id"])
    counts = _mapping_counts(connection, entity_id, mapping_tables)
    return IdentityCleanupCandidate(
        entity_type=entity_type,
        entity_id=entity_id,
        canonical_name=str(row["canonical_name"]),
        sport=str(row["sport"]),
        event_count=int(row["event_count"]),
        verified_mappings=counts["VERIFIED"],
        unverified_mappings=counts["UNVERIFIED"],
        suspect_mappings=counts["SUSPECT"],
        rejected_mappings=counts["REJECTED"],
        other_mappings=counts["OTHER"],
        relation_count=int(row["relation_count"]),
        reference_provider=row["reference_provider"],
        reference_provider_id=row["reference_provider_id"],
    )


def _mapping_counts(
    connection: Connection,
    entity_id: str,
    mapping_tables: tuple[tuple[str, str], ...],
) -> dict[str, int]:
    counts = {
        "VERIFIED": 0,
        "UNVERIFIED": 0,
        "SUSPECT": 0,
        "REJECTED": 0,
        "OTHER": 0,
    }
    for table, column in mapping_tables:
        rows = connection.execute(
            f"""
            SELECT trust_state, COUNT(*) AS mapping_count
            FROM {table}
            WHERE {column} = ?
            GROUP BY trust_state
            """,
            (entity_id,),
        ).fetchall()
        for mapping_row in rows:
            state = str(mapping_row["trust_state"])
            key = state if state in counts and state != "OTHER" else "OTHER"
            counts[key] += int(mapping_row["mapping_count"])
    return counts


def _delete_team_mappings(connection: Connection, team_id: str) -> int:
    deleted = 0
    for table in ("source_team_mappings", "source_team_id_mappings"):
        deleted += connection.execute(
            f"DELETE FROM {table} WHERE team_id = ? AND trust_state = 'UNVERIFIED'",
            (team_id,),
        ).rowcount
    return deleted


def _delete_competition_mappings(connection: Connection, competition_id: str) -> int:
    deleted = 0
    for table in ("source_competition_mappings", "source_competition_id_mappings"):
        deleted += connection.execute(
            f"DELETE FROM {table} "
            "WHERE competition_id = ? AND trust_state = 'UNVERIFIED'",
            (competition_id,),
        ).rowcount
    # source_team_mappings uses competition_id as contextual identity scope but
    # migration 20 intentionally did not add a foreign key. Delete only the
    # disposable UNVERIFIED rows here so no stale context survives the parent.
    deleted += connection.execute(
        """
        DELETE FROM source_team_mappings
        WHERE competition_id = ? AND trust_state = 'UNVERIFIED'
        """,
        (competition_id,),
    ).rowcount
    return deleted
