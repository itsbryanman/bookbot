# BookBot Tier 1 & Tier 2 Feature Implementation — Coding Agent Prompt

## Context

You are implementing new features for **BookBot**, a Python 3.10+ CLI/TUI audiobook library manager. The codebase uses Click for CLI, Textual for TUI, Pydantic v2 for models, aiohttp/requests for HTTP, mutagen for audio tags, rapidfuzz for fuzzy matching, and ffmpeg-python for audio processing. It follows MVVM-ish patterns with a clean provider abstraction.

**Repository structure** (key files only):

```
bookbot/
├── __init__.py
├── cli.py                          # Click CLI — @cli.group() with subcommands
├── gui.py                          # GUI launcher stub
├── config/
│   ├── models.py                   # Pydantic: Config, ProviderConfig, ConversionConfig, etc.
│   └── manager.py                  # ConfigManager: load/save TOML config
├── core/
│   ├── models.py                   # Pydantic: AudiobookSet, Track, ProviderIdentity, MatchCandidate, etc.
│   ├── discovery.py                # AudioFileScanner: walk dirs, detect tracks, build AudiobookSet
│   ├── matching.py                 # Score/merge candidates across providers
│   ├── operations.py               # TransactionManager: atomic rename/retag with undo
│   ├── templates.py                # TemplateEngine: render folder/file name templates
│   └── logging.py                  # get_logger()
├── providers/
│   ├── base.py                     # MetadataProvider ABC: search(), get_by_id(), find_matches()
│   ├── manager.py                  # ProviderManager: init + priority-order providers
│   ├── openlibrary.py              # OpenLibraryProvider (always enabled)
│   ├── googlebooks.py              # GoogleBooksProvider (needs API key)
│   ├── librivox.py                 # LibriVoxProvider
│   ├── audible.py                  # AudibleProvider (Audible scraping)
│   ├── local.py                    # LocalProvider (reads sidecar NFO/JSON files)
│   └── health.py                   # Provider health checks
├── convert/
│   ├── ffmpeg.py                   # FFmpegWrapper: probe, concat, normalize, embed cover
│   └── pipeline.py                 # ConversionPipeline: multi-step M4B conversion
├── drm/                            # DRM detection and removal (AAX/AAXC)
├── io/
│   └── cache.py                    # CacheManager
├── tui/
│   ├── app.py                      # BookBotApp(App) — Textual application
│   └── screens.py                  # TUI screens
└── tests/
pyproject.toml                      # bookbot-cli v0.3.0, deps listed
```

**Key existing patterns to follow:**

1. **Provider ABC** (`bookbot/providers/base.py`): All metadata providers inherit `MetadataProvider` and implement `search()`, `get_by_id()`, `calculate_match_score()`. They return `ProviderIdentity` objects and `MatchCandidate` lists.
2. **Provider registration** (`bookbot/providers/manager.py`): `ProviderManager.__init__()` reads config and conditionally initializes providers into `self.providers: dict[str, MetadataProvider]`.
3. **Config models** (`bookbot/config/models.py`): Pydantic models. `Config` has `.providers: ProviderConfig`, which has nested configs per provider. Add new provider configs following `GoogleBooksConfig` pattern.
4. **CLI commands** (`bookbot/cli.py`): Click groups. Top-level `@cli.group()`, subcommands with `@cli.command()`. Uses `ctx.obj["config_manager"]`.
5. **Async HTTP**: Providers use `aiohttp.ClientSession` for API calls. Cache via `CacheManager`.
6. **Models** (`bookbot/core/models.py`): `ProviderIdentity` has fields: provider, external_id, title, authors, series_name, series_index, year, language, narrator, edition, publisher, isbn_10, isbn_13, asin, description, cover_urls, raw_data.

---

## TIER 1 — Core Differentiators

### Feature 1A: Audnexus Provider

**What:** Add `bookbot/providers/audnexus.py` — a new `MetadataProvider` subclass that queries the public Audnexus API for audiobook metadata and chapter data.

**API Details (verified working):**
- Base URL: `https://api.audnex.us`
- **Search by ASIN**: `GET /books/{ASIN}` — returns full book metadata (title, authors, narrators, series, genres, cover image URL, release date, publisher, description, runtime)
- **Search by name**: `GET /books?name={query}` — returns search results
- **Get chapters**: `GET /books/{ASIN}/chapters` — returns chapter list with `title`, `startOffsetMs`, `startOffsetSec`, `lengthMs` fields. NOTE: This endpoint requires the book to have been previously imported; if not found, it triggers an import from Audible.
- **Author lookup**: `GET /authors?name={name}` — returns author ASIN and metadata
- **Author by ASIN**: `GET /authors/{ASIN}` — returns full author details including books list, image, description
- **Region support**: `?region={us|uk|ca|au|fr|de|it|es|jp|in}` query param on all endpoints
- No API key required. Rate-limited (respect 429 responses with exponential backoff).
- All responses are JSON. ASIN values must be **uppercase** (the API is case-sensitive).

**Implementation:**
1. Create `bookbot/providers/audnexus.py` with class `AudnexusProvider(MetadataProvider)`.
2. Implement `search()` — use name search endpoint, map results to `ProviderIdentity` objects. Map ASIN into the `asin` field.
3. Implement `get_by_id()` — treat `external_id` as ASIN, call `/books/{ASIN}`.
4. Add `get_chapters(asin: str) -> list[dict]` method (not part of ABC, called separately) that calls `/books/{ASIN}/chapters` and returns parsed chapter list with `title`, `start_ms`, `length_ms` keys.
5. Implement `calculate_match_score()` — use `rapidfuzz.fuzz.token_sort_ratio` for title matching, `rapidfuzz.fuzz.ratio` for author matching. Weight: title 0.5, author 0.3, narrator 0.1, year 0.1.
6. Add `AudnexusConfig` to `bookbot/config/models.py`: `enabled: bool = True`, `marketplace: str = "us"`.
7. Register in `ProviderManager._initialize_providers()` and `list_providers()`.
8. Add `"audnexus"` to default `priority_order` list in `ProviderConfig`.
9. Handle ASIN uppercasing internally — always `.upper()` before API calls.
10. Use `aiohttp.ClientSession` with 30s timeout. Cache results via `CacheManager` with 24h TTL.

### Feature 1B: Hardcover Provider

**What:** Add `bookbot/providers/hardcover.py` — queries Hardcover's public GraphQL API for book metadata.

**API Details (verified working):**
- GraphQL endpoint: `POST https://api.hardcover.app/v1/graphql`
- Auth: Bearer token from user's Hardcover account settings page. Header: `Authorization: Bearer {token}`
- **Search books**: Use the Typesense search endpoint: `GET https://api.hardcover.app/v1/search/books?query={title}&per_page=10`
  - Returns: `id`, `title`, `author_names`, `alternative_titles`, `audio_seconds`, `compilation`, `isbns`, `series_names`
- **Get book by ID** (GraphQL):
```graphql
query GetBook($id: Int!) {
  books(where: {id: {_eq: $id}}) {
    id
    title
    description
    release_year
    pages
    cached_image
    cached_contributors
    book_series { series { name } position }
    editions { isbn_13 isbn_10 audio_seconds }
  }
}
```
- **Search authors**: `GET https://api.hardcover.app/v1/search/authors?query={name}`
- `audio_seconds` field gives audiobook runtime — use this for duration-based match scoring.
- API is in beta; token required. Treat as optional provider.

**Implementation:**
1. Create `bookbot/providers/hardcover.py` with class `HardcoverProvider(MetadataProvider)`.
2. Use the search endpoint for `search()` (REST, not GraphQL — simpler and sufficient).
3. Use GraphQL for `get_by_id()` to get full book details.
4. Map `cached_contributors` → `authors`, `book_series` → `series_name`/`series_index`, `cached_image` → `cover_urls`, `release_year` → `year`.
5. `calculate_match_score()` — same weighting as Audnexus but add 0.05 bonus if `audio_seconds` is within 10% of library's `total_duration`.
6. Add `HardcoverConfig` to config models: `enabled: bool = False`, `api_key: str | None = None`.
7. Register in `ProviderManager` — only init if `enabled` and `api_key` is set.
8. Add `bookbot provider set-key hardcover YOUR_TOKEN` CLI command support.

### Feature 1C: Audiobookshelf API Integration

**What:** Add `bookbot/abs/` module providing a CLI-driven Audiobookshelf client.

**API Details (verified working March 2026):**
- Base URL: user-configured (e.g., `https://abs.example.com`)
- **Auth**: `POST /login` with `{"username": "...", "password": "..."}` → returns `user.token` (JWT). Use as `Authorization: Bearer {token}`.
- As of v2.26.0, API Keys are also supported (permanent tokens created in admin UI). Preferred for CLI tools.
- **Libraries**: `GET /api/libraries` → list of libraries with IDs and mediaType
- **Library items**: `GET /api/libraries/{id}/items?limit=20&page=0&sort=media.metadata.title&filter=...`
- **Search**: `GET /api/libraries/{id}/search?q={query}&limit=10`
- **Get item**: `GET /api/items/{id}?expanded=1` → full item with media, chapters, audioFiles, progress
- **Update metadata**: `PATCH /api/items/{id}/media` with JSON body of metadata fields
- **Match item**: `POST /api/items/{id}/match` with `{"provider": "audnexus", "title": "...", "author": "..."}`
- **Batch match**: `POST /api/libraries/{id}/match-all`
- **Progress tracking**: 
  - Read: `GET /api/me/progress/{libraryItemId}` → `{progress: 0.0-1.0, currentTime: secs, isFinished: bool}`
  - Update: `PATCH /api/me/progress/{libraryItemId}` with `{"progress": 0.5, "currentTime": 3600, "isFinished": false}`
  - Sync local sessions: `POST /api/session/local` with array of session objects
- **Collections**: CRUD at `/api/collections`, add/remove books
- **Series**: `GET /api/libraries/{id}/series`, `GET /api/series/{id}`
- **Cover art**: `GET /api/items/{id}/cover`
- **M4B encode**: `POST /api/tools/encode-m4b/{id}` — server-side M4B encoding
- **Stats**: `GET /api/me/listening-stats`
- **RSS feeds**: `POST /api/feeds/item/{id}/open` — open RSS feed for a library item

**Implementation:**
1. Create `bookbot/abs/__init__.py` and `bookbot/abs/client.py` with class `AudiobookshelfClient`:
   - `__init__(self, server_url: str, api_token: str)` 
   - `async login(username, password) -> str` (returns token)
   - `async get_libraries() -> list[dict]`
   - `async get_library_items(library_id, limit=20, page=0, sort=None, filter=None) -> dict`
   - `async search_library(library_id, query) -> dict`
   - `async get_item(item_id, expanded=True) -> dict`
   - `async update_item_metadata(item_id, metadata: dict) -> dict`
   - `async match_item(item_id, provider, title=None, author=None) -> dict`
   - `async batch_match(library_id) -> dict`
   - `async get_progress(item_id) -> dict`
   - `async update_progress(item_id, progress, current_time, is_finished=False) -> dict`
   - `async get_collections(library_id) -> list[dict]`
   - `async create_collection(library_id, name, book_ids) -> dict`
   - `async get_stats() -> dict`
   - All methods use `aiohttp.ClientSession` with Bearer token auth header.
2. Create `bookbot/abs/config.py` — `ABSConfig(BaseModel)`: `server_url: str | None = None`, `api_token: str | None = None`, `username: str | None = None`.
3. Add `abs` field to main `Config` model: `abs: ABSConfig = Field(default_factory=ABSConfig)`.
4. Add CLI command group `bookbot abs`:
   - `bookbot abs login --server URL --username USER` — prompt for password, call `/login`, store token in config
   - `bookbot abs libraries` — list all libraries
   - `bookbot abs search LIBRARY_ID QUERY` — search a library
   - `bookbot abs list LIBRARY_ID [--limit N] [--page N]` — list items
   - `bookbot abs show ITEM_ID` — show full item details with chapters, progress
   - `bookbot abs match ITEM_ID [--provider audnexus]` — trigger metadata match
   - `bookbot abs match-all LIBRARY_ID` — batch match entire library
   - `bookbot abs progress ITEM_ID [--set FLOAT]` — get or set progress
   - `bookbot abs stats` — show listening statistics
   - `bookbot abs collections LIBRARY_ID` — list collections
5. All `abs` commands should gracefully error if `server_url` or `api_token` not configured, with a message directing user to run `bookbot abs login` first.

### Feature 1D: Batch Library Health Checks

**What:** Add `bookbot health` CLI command that audits an audiobook library for common issues.

**Implementation:**
1. Create `bookbot/core/health.py` with class `LibraryHealthChecker`:
   - `check_missing_covers(audiobook_sets: list[AudiobookSet]) -> list[dict]` — books with no embedded cover art and no cover.jpg/folder.jpg sidecar
   - `check_inconsistent_tags(audiobook_sets: list[AudiobookSet]) -> list[dict]` — books where tracks have mismatched album/artist/albumartist tags
   - `check_orphaned_files(library_path: Path) -> list[Path]` — non-audio files that aren't covers, NFO, cue, or metadata sidecars
   - `check_duplicate_editions(audiobook_sets: list[AudiobookSet]) -> list[list[AudiobookSet]]` — groups of sets with the same title+author (fuzzy)
   - `check_series_gaps(audiobook_sets: list[AudiobookSet]) -> list[dict]` — detect missing volumes in a series (e.g., has book 1, 3 but not 2)
   - `check_format_consistency(audiobook_sets: list[AudiobookSet]) -> list[dict]` — books with mixed formats (some MP3, some M4A)
   - `check_bitrate_anomalies(audiobook_sets: list[AudiobookSet]) -> list[dict]` — tracks with significantly different bitrates within the same book
   - `run_all_checks(library_path: Path, audiobook_sets: list[AudiobookSet]) -> HealthReport`
2. Create `HealthReport` Pydantic model with fields for each check category, total issues count, and a `to_rich_table()` method that returns a Rich table for CLI display.
3. Add CLI command:
   - `bookbot health DIR [--json] [--verbose]` — scan directory, run all checks, display report
   - `--json` outputs machine-readable JSON
   - `--verbose` shows file paths for each issue
4. Use existing `AudioFileScanner` to build `AudiobookSet` list, then run health checks.
5. Use `rapidfuzz.fuzz.token_sort_ratio` with threshold 85 for duplicate detection.

### Feature 1E: Smart File Organizer (Fuzzy Matching for Messy Libraries)

**What:** Enhance `bookbot/core/discovery.py` to handle messy, inconsistently-named directories that would fail Audiobookshelf's strict structure requirements.

**Implementation:**
1. Add `SmartOrganizer` class to new file `bookbot/core/organizer.py`:
   - `propose_reorganization(source_path: Path, target_template: str, audiobook_sets: list[AudiobookSet]) -> ReorganizationPlan`
   - For each `AudiobookSet`, use the chosen (or best-guess) `ProviderIdentity` to compute the target path via `TemplateEngine`.
   - Generate a `ReorganizationPlan` (new Pydantic model) that contains a list of `MoveOperation(source: Path, destination: Path, audiobook_set: AudiobookSet)`.
   - Before proposing moves, validate: no path collisions, no overwriting existing files, target paths within max_path_length.
2. Add `bookbot organize` CLI command:
   - `bookbot organize SOURCE [--target DIR] [--template plex|abs|default] [--dry-run] [--confirm]`
   - `--template abs` uses `{Author}/{Title}/{Author} - {Title}` (Audiobookshelf's expected structure)
   - Default is `--dry-run` — shows proposed moves with color-coded output
   - `--confirm` executes the moves using `TransactionManager` for atomicity/undo
3. Add ABS-compatible template preset: `"abs": NamingTemplate(name="Audiobookshelf", description="ABS-compatible directory structure", folder_template="{Author}/{Title}", file_template="{Author} - {Title}")`

---

## TIER 2 — High-Value Gap Fillers

### Feature 2A: Chapter Detection Pipeline

**What:** Create `bookbot/chapters/` module that detects and generates chapter markers for audiobooks that lack them.

**Implementation:**
1. Create `bookbot/chapters/__init__.py` and `bookbot/chapters/detector.py` with class `ChapterDetector`:
   - `detect_from_silence(audio_files: list[Path], noise_db: float = -50.0, min_silence_sec: float = 2.0) -> list[Chapter]`
     - Uses FFmpeg `silencedetect` filter: `ffmpeg -i {file} -af silencedetect=n={noise_db}dB:d={min_silence_sec} -f null -`
     - Parse stderr output for `silence_start` and `silence_end` timestamps
     - For multi-file books, offset timestamps by cumulative duration of preceding files
     - Filter candidate chapter breaks: prefer silences > 3 seconds, space chapters at least 5 minutes apart (configurable)
     - Return list of `Chapter(title: str, start_ms: int, end_ms: int | None)`
   - `detect_from_tracks(audiobook_set: AudiobookSet) -> list[Chapter]`
     - Use track boundaries as chapter markers — each track file = one chapter
     - Chapter title from track tag if available, else "Chapter N"
   - `detect_from_audnexus(asin: str, provider: AudnexusProvider) -> list[Chapter] | None`
     - Call `AudnexusProvider.get_chapters(asin)` and convert to `Chapter` objects
   - `detect_from_cue(cue_path: Path) -> list[Chapter]`
     - Parse .cue file format for chapter timecodes
   - `auto_detect(audiobook_set: AudiobookSet, audnexus: AudnexusProvider | None = None) -> list[Chapter]`
     - Strategy chain: Audnexus (if ASIN available) → existing embedded chapters → cue file → track boundaries → silence detection
     - Return first successful result
2. Create `Chapter` Pydantic model in `bookbot/chapters/models.py`: `title: str`, `start_ms: int`, `end_ms: int | None = None`, `source: str` (which detection method).
3. Create `bookbot/chapters/writer.py` with `ChapterWriter`:
   - `write_to_m4b(m4b_path: Path, chapters: list[Chapter])` — uses ffmpeg to write chapter metadata
   - `write_to_ffmetadata(output_path: Path, chapters: list[Chapter])` — writes FFmpeg FFMETADATA1 format file
   - `write_to_cue(output_path: Path, chapters: list[Chapter])` — writes .cue sidecar
4. Add CLI command:
   - `bookbot chapters detect DIR [--method auto|silence|tracks|audnexus] [--noise-db -50] [--min-silence 2.0]`
   - `bookbot chapters apply DIR --format ffmetadata|cue [--dry-run]`
5. For silence detection, the FFmpeg command is:
```
ffmpeg -i input.m4b -af "silencedetect=n=-50dB:d=2.0" -f null - 2>&1
```
Output lines to parse:
```
[silencedetect @ 0x...] silence_start: 1234.567
[silencedetect @ 0x...] silence_end: 1237.890 | silence_duration: 3.323
```
Use regex: `r'silence_start:\s*([\d.]+)'` and `r'silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)'`

### Feature 2B: M4B Tooling Enhancements

**What:** Enhance `bookbot/convert/` to support merge, split, chapter injection, and metadata embedding workflows using the existing `FFmpegWrapper`.

**Implementation:**
1. Add methods to `FFmpegWrapper` in `bookbot/convert/ffmpeg.py`:
   - `merge_to_m4b(input_files: list[Path], output: Path, chapters: list[Chapter] | None = None, metadata: dict | None = None, cover: Path | None = None) -> Path`
     - Create concat demuxer file, detect if stream copy is safe (all AAC → copy, else transcode)
     - If chapters provided, write FFMETADATA1 temp file and pass via `-i metadata.txt -map_metadata 1`
     - If cover provided, embed as attached picture
   - `split_m4b(input: Path, output_dir: Path, chapters: list[Chapter] | None = None) -> list[Path]`
     - Split by chapter markers into individual files
   - `embed_metadata(file: Path, metadata: dict, cover: Path | None = None) -> Path`
     - Write tags using ffmpeg `-metadata` flags without re-encoding
   - `extract_chapters(file: Path) -> list[Chapter]`
     - Use `ffprobe -show_chapters -print_format json` to extract embedded chapters
2. Add CLI commands:
   - `bookbot m4b merge DIR -o OUTPUT.m4b [--chapters auto] [--cover cover.jpg] [--normalize]`
   - `bookbot m4b split INPUT.m4b -o OUTPUT_DIR [--format mp3|m4a]`
   - `bookbot m4b chapters INPUT.m4b` — display chapters
   - `bookbot m4b tag INPUT.m4b --title "..." --author "..." --cover cover.jpg`

### Feature 2C: Sidecar Metadata Files

**What:** Read and write sidecar metadata files for interoperability with Audiobookshelf, BookLore, and Calibre.

**Implementation:**
1. Create `bookbot/io/sidecar.py` with class `SidecarManager`:
   - `read_opf(path: Path) -> ProviderIdentity | None` — parse OPF/XML (Calibre/ABS format)
   - `write_opf(path: Path, identity: ProviderIdentity) -> None`
   - `read_metadata_json(path: Path) -> ProviderIdentity | None` — parse BookLore-style `.metadata.json`
   - `write_metadata_json(path: Path, identity: ProviderIdentity) -> None`
   - `read_nfo(path: Path) -> ProviderIdentity | None` — parse audiobook .nfo files
   - `auto_detect_sidecar(directory: Path) -> ProviderIdentity | None` — check for metadata.opf, .metadata.json, audiobook.nfo in order
2. OPF format (Dublin Core XML — same as Calibre's `metadata.opf`):
```xml
<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>Book Title</dc:title>
    <dc:creator opf:role="aut">Author Name</dc:creator>
    <dc:language>en</dc:language>
    <dc:description>Description</dc:description>
    <dc:publisher>Publisher</dc:publisher>
    <dc:date>2024</dc:date>
    <dc:identifier opf:scheme="ISBN">9781234567890</dc:identifier>
    <dc:identifier opf:scheme="ASIN">B01234567X</dc:identifier>
    <meta name="calibre:series" content="Series Name"/>
    <meta name="calibre:series_index" content="1"/>
    <meta name="calibre:narrator" content="Narrator Name"/>
  </metadata>
</package>
```
3. BookLore JSON format:
```json
{
  "title": "Book Title",
  "authors": ["Author Name"],
  "series": "Series Name",
  "seriesIndex": "1",
  "narrator": "Narrator Name",
  "year": 2024,
  "isbn": "9781234567890",
  "asin": "B01234567X",
  "description": "Description",
  "publisher": "Publisher",
  "language": "en",
  "coverUrl": "cover.jpg"
}
```
4. Integrate with `bookbot scan` and `bookbot tui`: if `--metadata-from-files` flag is passed, check for sidecars before querying online providers. Already partially supported via `LocalProvider` — extend it.
5. Add CLI commands:
   - `bookbot sidecar read DIR` — display detected sidecar metadata
   - `bookbot sidecar write DIR --format opf|json [--from-tags]` — generate sidecar from embedded tags or matched identity
   - `bookbot sidecar sync DIR` — read sidecar, apply to audio tags (or vice versa)

### Feature 2D: Progress Sync Daemon

**What:** Create a background sync daemon that periodically synchronizes playback progress between a local state file and an Audiobookshelf server.

**Implementation:**
1. Create `bookbot/abs/sync.py` with class `ProgressSyncDaemon`:
   - `__init__(self, client: AudiobookshelfClient, state_path: Path)`
   - `sync_from_server() -> list[dict]` — pull all progress from ABS, write to local SQLite DB
   - `sync_to_server(item_id: str, progress: float, current_time: float) -> bool` — push local progress to ABS
   - `sync_all() -> SyncReport` — bidirectional sync: compare timestamps, most-recent-wins
   - `get_local_progress(item_id: str) -> dict | None` — read from local DB
   - `update_local_progress(item_id: str, progress: float, current_time: float) -> None`
2. Local state stored in SQLite at `~/.local/share/bookbot/progress.db`:
   - Table: `progress(item_id TEXT PRIMARY KEY, progress REAL, current_time REAL, is_finished INTEGER, last_update TEXT, synced INTEGER)`
3. Add CLI commands:
   - `bookbot abs sync [--direction pull|push|both]` — one-shot sync
   - `bookbot abs sync --watch --interval 60` — continuous sync daemon (check every N seconds)
4. Sync logic:
   - On `pull`: fetch `/api/me/progress/{id}` for all items, update local DB where server `lastUpdate > local lastUpdate`
   - On `push`: find local entries where `synced = 0`, push to server, mark `synced = 1`
   - On `both`: pull first, then push unsynced local changes
5. Create `SyncReport` model: `pulled: int, pushed: int, conflicts: int, errors: list[str]`

---

## Implementation Notes

### Dependencies to Add to `pyproject.toml`

No new dependencies required — everything uses existing deps:
- `aiohttp` — for Audnexus, Hardcover, ABS API calls
- `rapidfuzz` — for fuzzy match scoring
- `ffmpeg-python` — for silence detection, chapter writing, M4B merge/split (already in `[conversion]` extra)
- `pydantic` — for new models
- `click` — for new CLI commands
- `rich` — for health report tables

### Testing Requirements

For each new module, create corresponding test files under `tests/`:
- `tests/test_audnexus_provider.py` — mock API responses, test search/match scoring
- `tests/test_hardcover_provider.py` — mock GraphQL responses
- `tests/test_abs_client.py` — mock HTTP responses for all ABS endpoints
- `tests/test_health_checker.py` — create temp directories with known issues, verify detection
- `tests/test_chapter_detector.py` — mock ffmpeg output, test silence parsing
- `tests/test_sidecar.py` — write/read round-trip tests for OPF and JSON
- `tests/test_organizer.py` — test path generation and collision detection

Use `pytest` with `pytest-asyncio` for async tests. Mock external APIs with `unittest.mock.AsyncMock`.

### Code Quality

- Run `ruff` and `black` formatting (already configured in pyproject.toml)
- Add type annotations to all new code (mypy strict mode)
- Follow existing docstring style (Google style)
- Add new CLI commands to the README's CLI cheat sheet
- Log all API calls at DEBUG level using `get_logger()`

### Order of Implementation

1. **Audnexus Provider** (1A) — standalone, no other new code depends on it
2. **Sidecar Metadata** (2C) — standalone, useful for testing other features
3. **Chapter Detection** (2A) — depends on 1A (Audnexus chapters) but can be tested independently
4. **M4B Tooling** (2B) — depends on 2A (Chapter model) and extends existing convert module
5. **Health Checks** (1D) — standalone, uses existing scanner
6. **Smart Organizer** (1E) — depends on existing TemplateEngine
7. **ABS Client** (1C) — standalone HTTP client
8. **Progress Sync** (2D) — depends on 1C (ABS client)
9. **Hardcover Provider** (1B) — standalone, lowest priority since it requires API key
