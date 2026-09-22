# Manual capture sources -- quick reference

For every provider that has no automatic fetch (Cloudflare bot-management,
or an OAuth-style token tied to a real logged-in session -- see each
collector's own docstring for why). Check this **before** saving a
response over a drop file: as the number of bookmakers grows, it gets
easy to grab the wrong request from DevTools (a stream/video endpoint, a
UI-config endpoint, a detail-page endpoint) and only find out later that
it silently didn't produce the odds you expected.

Each collector's own `parse_*_response()` already raises a named error
(`MozzartResponseError`, `MeridianbetResponseError`, ...) the moment a
wrong-shaped capture is dropped -- rather than silently reporting "zero
matches this cycle" -- so a mistake here is never invisible, just a
wasted capture. This table is what you check *before* that happens.

## Meridianbet

- **URL**: `https://online.meridianbet.com/betshop/api/v1/standard/sport/58/events?page=0&time=ONE_DAY`
  (`sport/58` = football; other sport ids will have their own number --
  the response's own `header.sport.name` confirms which one you got)
- **Correct response shape**: a JSON object with `payload.leagues[]`
  (each with its own `events[]`) *or* `payload.events[]` directly (both
  seen live, same `header`/`positions` shape underneath either way --
  see `parse_meridianbet_response`'s own docstring) -- either way, each
  event has `header.rivals` (team names) and `positions[].groups[]` with
  real `selections[]` prices in it.
- **Wrong responses actually captured here before** (both silently
  contain zero odds -- caught only because `MeridianbetResponseError`
  fires, not from eyeballing the file):
  - a *video-stream* metadata endpoint -- top-level keys `live`,
    `standard`, `mapped`; WebSocket/M3U8 URLs, no team names or prices
    anywhere
  - a *market column config* endpoint -- `payload.positions` /
    `payload.configuredPositions`; market *names* only
    (`"Pobednik"`/`"Konačan Ishod"`/...), no actual event/odds data
- **Drop as**: `meridianbet/meridianbet.json` (`MERIDIANBET_CAPTURE_DIR`)
- **Needs auth to fetch automatically**: yes -- 401 `invalid_token`
  (OAuth-style bearer token, not a plain API key), behind Cloudflare
  (`__cf_bm` cookie) with device-fingerprint checks on top. Not worth
  automating for the same reason Mozzart isn't -- see that collector's
  own docstring.
- **Pagination**: the full listing for one sport is split across many
  `?page=0`, `?page=1`, ... requests (40-50+ for football), each covering
  a different subset of leagues -- not one response like the URL above
  suggests. `scripts/import_meridianbet_har.py` merges a full HAR export
  (DevTools Network tab -> "Save all as HAR with content", capturing
  every page in one file) into the single combined drop file this
  collector expects, deduplicating leagues by `leagueId` and events by
  `header.eventId` in case pagination overlaps.
- **Known HAR-export encoding bug (Chrome), not a capture mistake**:
  league/team names with Serbian-specific characters (Š/š, Č/č, Ć/ć, ...)
  can come out of Chrome's "Save all as HAR with content" as literal
  U+FFFD replacement characters (e.g. "Liga Šampiona" -> "Liga
  �ampiona") even though the response's `Content-Type: application/json`
  never declared a non-UTF-8 charset -- confirmed present in the raw HAR
  `response.content.text` itself, before any of our own code touches it,
  so it is **not recoverable** by re-parsing or re-encoding after the
  fact; the original bytes are already gone by the time HAR export wrote
  them. Only display strings are affected -- odds, ids, and timestamps in
  the same capture are untouched. If a capture needs to preserve these
  characters exactly (e.g. for name-matching against another source),
  don't use the HAR-export path for it; a live per-response capture via
  the Chrome DevTools Protocol's own `Network.getResponseBody` (which
  decodes the raw bytes directly, not through HAR's export path) does not
  hit this bug.

## Mozzart

- **URL**: not on record with the exact full path -- captured historically
  as a `/live/matches`-shaped request on mozzartbet.com's live-betting
  page. The pre-match listing page returns the *exact same envelope
  shape* -- you no longer need to avoid it; the collector tells live and
  pre-match matches apart itself (see below).
- **Correct response shape**: a JSON object with an `"items"` key (a
  list of matches), each with `oddsGroup[]` containing a group named
  `"Konačan ishod"` with real `odds[]` prices.
- **Live vs pre-match**: each match's own `status` field decides its
  phase, not which page you captured from -- a live match has
  `status.isLive: true`; a pre-match (not-yet-started) match has
  `status.name: "Nije počeo"` and no `isLive`/`result`/`matchTime` keys.
  Don't use `betStatus` for this: it reads `"STARTED"` in *both* cases
  (it means "this market accepts bets", not "kickoff happened"). A
  single capture can safely mix matches from both phases -- each is
  tagged correctly on its own (see `mozzart_file_collector.
  _resolve_market_and_lifecycle`).
- **Wrong responses actually captured here before**: none on record yet
  for this source specifically.
- **Drop as**: `mozzart/live.json` (`MOZZART_CAPTURE_DIR`). If your
  capture tooling always saves under the same default filename
  regardless of phase, a live capture and a pre-match capture landing in
  the same directory close together can overwrite each other before the
  poller reads either one -- point a second capture at
  `MOZZART_PREMATCH_CAPTURE_DIR` (a different directory) to avoid that
  race. Optional: only set it if you're actually capturing both phases
  back-to-back.
- **Needs auth to fetch automatically**: Cloudflare bot-management
  (`cf_clearance`/`__cf_bm` cookies observed) -- not automated for the
  same reason as Meridianbet.

## Adding a new source here

When a new manual-capture provider gets added (its own `_xxx_collector()`
in `pipeline.py`, mirroring the existing ones), add a section above with
the same five fields -- URL, correct shape, wrong shapes already hit (if
any), drop-file path, and why it isn't automated. Keeping "wrong shapes
already hit" up to date is the actual point of this file: the same
mistake (grabbing a stream/config/detail endpoint instead of the real
listing) has already happened more than once across different providers.
