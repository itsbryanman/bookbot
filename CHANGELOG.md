# Changelog

## Unreleased

Every entry below is covered by a test in `tests/` (file named in brackets).

### Added
- `scan --match` looks books up with the metadata providers, like `match`. [test_stored_decisions.py]
- `accept --candidate-id`, `--asin` and `--isbn` select a candidate by identifier instead of list position. `match --json` and `review-queue --json` emit `candidate_id`. [test_stored_decisions.py]
- `--metadata-from-files` on `match`, `review-queue`, `accept` and `scan --match`. [test_abs_metadata_json.py]
- `output.metadata_json_style = "audiobookshelf"`, the default in the `audiobookshelf` profile. It writes `metadata.json` with the key names and types of Audiobookshelf's parser (`mediaTypeKeys.book`). [test_abs_metadata_json.py]
- `output.cover_filename`: cover file name, template tokens allowed. [test_configurable_naming.py]
- `organize --template config`, `--folder-template` and `--file-template`. [test_configurable_naming.py]
- `apply --plan-out PATH` and `--no-update-plan` (also on `plan apply`). [test_apply_plan_preflight.py]
- `dedupe --duration-tolerance PCT` (default 2). [test_dedupe_accuracy.py]
- `make hooks` installs a commit-msg hook that strips AI co-author trailers. [test_commit_msg_hook.py]

### Changed
- Saved review decisions store the full chosen metadata. `scan`, `plan create`, `organize`, `convert` and `sidecar write` apply them without contacting providers. Decisions from older versions mark the book as needing review ("re-accept to refresh saved decision"). [test_stored_decisions.py]
- After `apply` or `organize --confirm`, saved decisions are copied to the books' new paths; `undo` still matches the old entry. [test_stored_decisions.py]
- `organize --confirm` runs through the transaction journal. It prints a transaction ID, can be undone with `bookbot undo`, and an interrupted run can be rolled back with `bookbot recover`. [test_organize_journal.py]
- Writing `metadata.json` over an existing file keeps keys BookBot does not set (e.g. `chapters`, `tags`, `explicit`, `abridged`). [test_abs_metadata_json.py]
- M4B chapter names (`auto`): title tag, then the filename ("01 - Prelude" → "Prelude"), then "Chapter N". [test_m4b_chapters.py]
- Edition dedupe: near matches and same-title books without an author are only grouped with a shared ASIN/ISBN or matching duration. Otherwise they are listed as "possible duplicates — review" and not quarantined. [test_dedupe_accuracy.py]

### Fixed
- Discovery: `Author/Series/01 - Title/` folders give the right title, author, series and number (was title "01", author "All Systems Red"). [test_discovery_series_layouts.py]
- Discovery: a folder name no longer loses to an album tag that names the series. The album becomes the series guess, and the book is flagged for review unless the track titles back the folder name. [test_discovery_series_layouts.py]
- M4B chapter boundaries are measured from MP4 temp tracks instead of bitrate estimates on ADTS files. They land within 50 ms of the real track boundaries, and the last chapter ends within the audio stream. [test_m4b_chapters.py]
- Chapter titles containing `=`, `;`, `#`, `\` or newlines are escaped in FFMETADATA and read back unchanged. [test_m4b_chapters.py]
- Cover art that cannot be embedded during `convert` produces a warning instead of failing silently. [test_m4b_chapters.py]
- `m4b tag` errors include the FFmpeg version and the end of FFmpeg's stderr. [test_m4b_chapters.py]
- `apply` checks the plan file is writable before moving anything. If saving it fails afterwards, it prints the transaction ID with `history`/`undo` hints and exits 2 (it used to exit 1 with only a permission error). [test_apply_plan_preflight.py]
- `organize` refuses a `--target` on another filesystem before moving anything. [test_organize_journal.py]
- `dedupe` no longer groups "Dune" with "Dune Messiah", and a missing author no longer counts as a match. [test_dedupe_accuracy.py]
- `dedupe --audio-hash` help says it is not implemented (it exits with an error). [test_dedupe_accuracy.py]

### Deprecated
- `organize --dry-run` is hidden; it never did anything (the dry run is the default without `--confirm`). [test_organize_journal.py]

### Not reproduced
- `m4b tag` on M4Bs that already embed a JPEG cover (embedded by ffmpeg or mutagen, with or without `--cover`) works on FFmpeg 7.1.5. These cases are regression tests now; no fix is claimed. [test_m4b_chapters.py]
