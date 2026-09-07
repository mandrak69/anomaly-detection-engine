import logging
import re
from sqlite3 import Connection, Row
from uuid import uuid4

from anomaly_detection_engine.models.odds import Bookmaker

logger = logging.getLogger(__name__)

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def normalize_bookmaker_name(name: str) -> str:
    """Case/whitespace/punctuation-only normalization -- "Bet365",
    "BET365", "Bet 365", " bet365 " all normalize to "bet365". No fuzzy
    matching (see BookmakerCatalog's own docstring for why): bookmaker
    brand names are a small, stable universe compared to team names, and
    a wrong auto-merge here would permanently contaminate consensus/
    outlier math across every provider, unlike a harmless duplicate
    canonical bookmaker.
    """
    return _NON_ALPHANUMERIC.sub("", name.strip().casefold())


class BookmakerCatalog:
    """Persistent, deliberately conservative canonical-bookmaker
    registry -- the same "resolve a raw sighting to a stable canonical
    identity, caching every (provider, raw identifier) resolution" shape
    as FixtureCatalog's team/competition resolution, but much simpler:

    - No fuzzy matching, ever. A raw sighting can only link to an
      existing canonical bookmaker through an exact (provider_id,
      mapping_key) mapping already on file, or an exact match (after
      normalize_bookmaker_name) against an existing canonical bookmaker's
      name. bookmakers.normalized_name is UNIQUE at the schema level, so
      "matches an existing canonical bookmaker" is always "zero or one
      matches" -- ambiguity between two existing canonical rows cannot
      arise from this catalog's own writes.
    - No auto-created alias table. source_bookmaker_mappings.source_name
      already retains enough raw-name history for manual review; a
      dedicated alias table is deferred until an actual need for one
      shows up (see this class's own history/round notes).
    - The canonical bookmaker id is always an internally-generated
      "bookmaker-<uuid>", never a provider's own bookmaker id --
      the-odds-api's "bet365" and api-football's "8" are both only ever
      recorded as mappings (source_bookmaker_mappings rows), never
      promoted to be the canonical identity itself. Otherwise this
      system's own domain identity would depend on one specific
      provider's identifiers surviving unchanged forever.

    Why so much more conservative than FixtureCatalog's fuzzy team/
    competition matching: team and league names genuinely vary a lot
    across sources ("Man Utd" / "Man United" / "Manchester Utd"), so
    fuzzy matching earns its keep there. Bookmaker brand names are a
    small, stable set, and this project's entire cross-provider
    consensus/outlier math (min_bookmakers, best-odds comparisons)
    depends on treating one real bookmaker as exactly one identity --
    wrongly merging two real bookmakers that only happen to have similar
    names (e.g. "Pinnacle" vs "Pinnacle Sports" might be genuinely
    different products/feeds) would silently corrupt that math forever,
    whereas failing to merge two spellings of the same real bookmaker
    only costs a temporary, harmless duplicate identity -- see resolve()
    for the exact precedence this trade-off implies.
    """

    def __init__(self, connection: Connection, *, provider_id: str) -> None:
        self._connection = connection
        self._provider_id = provider_id

    def resolve(self, *, source_bookmaker_id: str | None, source_name: str) -> Bookmaker:
        """Resolution order:

        1. An existing (provider_id, mapping_key) mapping -- reuse its
           canonical bookmaker unconditionally. mapping_key is
           source_bookmaker_id when the provider supplies a stable one,
           or normalize_bookmaker_name(source_name) otherwise (a source
           with no stable per-bookmaker id, e.g. the JSON demo/Mozzart,
           gets the same "same name, same provider -> same identity"
           stability the old raw.source.lower()-derived id had -- this
           just centralizes and generalizes it here).
        2. Exactly one existing canonical bookmaker whose normalized_name
           matches this sighting's normalized name -- link this mapping
           to it. Always "0 or 1 matches" by construction (normalized_name
           is UNIQUE), never a guess between two candidates.
        3. No match at all -- create a brand-new canonical bookmaker
           under this sighting's own source_name, and a mapping to it.

        Never links on a fuzzy/partial name match, and never derives the
        canonical id from any provider's own bookmaker id -- see class
        docstring.

        Wrapped in the same BEGIN IMMEDIATE pattern FixtureCatalog.match()
        uses: without it, two concurrent processes could both read "no
        mapping and no matching canonical bookmaker yet" for the first-
        ever sighting of the same real bookmaker and both try to create
        one, racing on bookmakers.normalized_name's UNIQUE constraint.
        """
        mapping_key = source_bookmaker_id or normalize_bookmaker_name(source_name)

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            bookmaker = self._resolve_locked(mapping_key=mapping_key, source_name=source_name)
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

        return bookmaker

    def _resolve_locked(self, *, mapping_key: str, source_name: str) -> Bookmaker:
        mapped = self._find_mapping(mapping_key)
        if mapped is not None:
            return mapped

        normalized_name = normalize_bookmaker_name(source_name)
        bookmaker = self._find_by_normalized_name(normalized_name) or self._create_bookmaker(
            canonical_name=source_name, normalized_name=normalized_name
        )

        self._save_mapping(
            mapping_key=mapping_key, source_name=source_name, bookmaker_id=bookmaker.id
        )
        return bookmaker

    def _find_mapping(self, mapping_key: str) -> Bookmaker | None:
        row = self._connection.execute(
            """
            SELECT b.* FROM source_bookmaker_mappings m
            JOIN bookmakers b ON b.id = m.bookmaker_id
            WHERE m.provider_id = ? AND m.source_bookmaker_id = ?
            """,
            (self._provider_id, mapping_key),
        ).fetchone()
        return self._map_row(row) if row else None

    def _find_by_normalized_name(self, normalized_name: str) -> Bookmaker | None:
        row = self._connection.execute(
            "SELECT * FROM bookmakers WHERE normalized_name = ?",
            (normalized_name,),
        ).fetchone()
        return self._map_row(row) if row else None

    def _create_bookmaker(self, *, canonical_name: str, normalized_name: str) -> Bookmaker:
        bookmaker_id = f"bookmaker-{uuid4().hex[:10]}"
        self._connection.execute(
            "INSERT INTO bookmakers (id, canonical_name, normalized_name) VALUES (?, ?, ?)",
            (bookmaker_id, canonical_name, normalized_name),
        )
        logger.info(
            "bookmaker_catalog.bookmaker.created",
            extra={"bookmaker_id": bookmaker_id, "canonical_name": canonical_name},
        )
        return Bookmaker(bookmaker_id, canonical_name)

    def _save_mapping(self, *, mapping_key: str, source_name: str, bookmaker_id: str) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO source_bookmaker_mappings
                (provider_id, source_bookmaker_id, source_name, bookmaker_id)
            VALUES (?, ?, ?, ?)
            """,
            (self._provider_id, mapping_key, source_name, bookmaker_id),
        )

    def _map_row(self, row: Row) -> Bookmaker:
        return Bookmaker(row["id"], row["canonical_name"])
