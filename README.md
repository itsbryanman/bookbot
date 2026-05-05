<p align="center">
  <img src="logo.png" alt="BookBot logo" width="220" />
</p>

<h1 align="center">BookBot</h1>

<p align="center"><em>Declarative audiobook command center for collectors who care about clean metadata.</em></p>

<p align="center">
  <a href="https://github.com/itsbryanman/BookBot/actions/workflows/ci.yml">
    <img alt="CI" src="https://github.com/itsbryanman/BookBot/actions/workflows/ci.yml/badge.svg?style=for-the-badge" />
  </a>
  <a href="https://ghcr.io/itsbryanman/bookbot">
    <img alt="Docker" src="https://img.shields.io/badge/docker-ghcr.io%2Fitsbryanman%2Fbookbot-2496ED?style=for-the-badge&logo=docker&logoColor=white" />
  </a>
  <a href="https://pypi.org/project/bookbot-cli/">
    <img alt="PyPI" src="https://img.shields.io/pypi/v/bookbot-cli?style=for-the-badge&logo=pypi&logoColor=white" />
  </a>
  <a href="https://pypi.org/project/bookbot-cli/">
    <img alt="Python versions" src="https://img.shields.io/pypi/pyversions/bookbot-cli?style=for-the-badge&logo=python&logoColor=white" />
  </a>
  <a href="LICENSE">
    <img alt="License" src="https://img.shields.io/github/license/itsbryanman/BookBot?style=for-the-badge&color=brightgreen" />
  </a>
  <a href="https://github.com/psf/black">
    <img alt="Code style" src="https://img.shields.io/badge/code%20style-black-000000?style=for-the-badge&logo=python&logoColor=white" />
  </a>
  <a href="https://github.com/astral-sh/ruff">
    <img alt="Ruff" src="https://img.shields.io/badge/linter-ruff-FCC21B?style=for-the-badge&logo=ruff&logoColor=white" />
  </a>
  <a href="http://mypy-lang.org/">
    <img alt="Typing" src="https://img.shields.io/badge/typed-mypy-blue?style=for-the-badge&logo=python&logoColor=white" />
  </a>
</p>

BookBot is a Textual-powered terminal app and command-line toolkit for taming large audiobook libraries. Its core workflow is a safe, reviewable rename plan: scan a library, inspect the exact moves, apply them atomically, and undo them if needed.

## Highlights

- Safe-plan workflow with dry-runs, atomic file operations, transaction history, diffable JSON plans, and undo support.
- Built-in profiles for `safe`, `audiobookshelf`, `plex`, `prologue`, and `apple-books`.
- `bookbot doctor` checks your runtime, FFmpeg health, writable config/cache paths, and library integrity before changes land.
- Fast metadata discovery that combines local heuristics with Open Library plus optional Google Books, LibriVox, and Audible lookups.
- Mission-control TUI with scan, metadata review, preview, warnings, diff, and undo shortcuts in one interface.
- Configurable templates and collector profiles so folders, file names, covers, and sidecars match the way your players expect them.
- Optional M4B conversion pipeline with FFmpeg stream copy, loudness normalization, and chapter generation.
- Audible authentication, DRM detection, and removal helpers for supported formats (AAX, AAXC, M4B, and more).

## Installation

Docker is the fastest way to run BookBot with every dependency pre-baked. It keeps FFmpeg, optional DRM tooling, and Python libraries in one container so your host stays clean.

### Docker (recommended)

```bash
docker run -it --rm \
  -v "/path/to/audiobooks:/data" \
  -v "$PWD/config:/config" \
  -e BOOKBOT_CONFIG_DIR=/config \
  ghcr.io/itsbryanman/bookbot:latest tui /data
```

Mount your library into `/data` and persist configuration under `/config`. Swap `tui /data` for commands like `scan /data --plan /config/bookbot-plan.json`, `doctor /data`, or `convert /book --profile conversion`.

Or use Docker Compose with the included [`docker-compose.yml`](docker-compose.yml):

```bash
docker compose up
```

You can also add a convenience alias:

```bash
alias bookbot-docker='docker run -it --rm -v "$PWD/config:/config" -e BOOKBOT_CONFIG_DIR=/config -v "$PWD:/data" ghcr.io/itsbryanman/bookbot:latest'
```

Then run `bookbot-docker tui /data` from any library directory.

### pipx (alternative)

```bash
pipx install bookbot-cli
```

The package on PyPI is `bookbot-cli`, but the executable is `bookbot`. pipx keeps BookBot isolated and places `bookbot` on your PATH. If the executable is not found after install, add `$HOME/.local/bin` to your shell profile.

### pip / virtualenv (alternative)

```bash
python -m pip install bookbot-cli
python -m pip install "bookbot-cli[conversion]"
```

You still need a system FFmpeg binary for audio conversion and DRM extraction.

### From source

```bash
git clone https://github.com/itsbryanman/BookBot.git
cd BookBot
python -m pip install -e .[dev]
```

The editable install gives you live reload while iterating on the app.

## Quick start

```bash
# 1. Create a safe, reviewable rename plan
bookbot scan /path/to/audiobooks --plan ./bookbot-plan.json

# 2. Review or diff the plan before touching files
bookbot review ./bookbot-plan.json
bookbot plan diff ./bookbot-plan.json

# 3. Apply it atomically, then undo if needed
bookbot apply ./bookbot-plan.json
bookbot undo <transaction-id>

# 4. Check the host or a mounted library before a bigger run
bookbot doctor
bookbot doctor /path/to/audiobooks --profile audiobookshelf

# 5. Launch the Textual TUI for interactive review
bookbot tui /path/to/audiobooks

#    Stay offline by reusing existing sidecar metadata
bookbot tui /path/to/audiobooks --metadata-from-files

# 6. Convert a finished book to a single tagged M4B
bookbot convert /path/to/book -o /path/to/output --normalize --chapters auto

# 7. Authenticate with Audible once, then import books by ASIN
bookbot audible auth
bookbot audible import B01234567X --remove-drm
```

Prefer a desktop entry point? `bookbot gui` launches the same Textual application and falls back to the CLI if GUI dependencies are missing.

## Core workflows

**Organize safely**
- `bookbot scan DIR --plan bookbot-plan.json`
- `bookbot review bookbot-plan.json`
- `bookbot apply bookbot-plan.json`
- `bookbot undo <transaction-id>`
- The `bookbot plan` subgroup exposes the same flow explicitly: `create`, `show`, `diff`, `validate`, and `apply`.

**Tailor metadata**
- Activate opinionated profiles with `bookbot profile list`, `bookbot profile show audiobookshelf`, and `bookbot profile use plex`.
- Built-in collector profiles define folder layout, preferred packaged filename, cover behavior, sidecar output, M4B preference, and chapter style.
- Customize templates or output behavior with `bookbot config show`, `bookbot config set output.folder_template ...`, and runtime `--profile` flags.

**Diagnose first**
- `bookbot doctor` checks Python, FFmpeg, config/cache writability, and provider configuration.
- `bookbot doctor ./library --profile audiobookshelf` validates plan compatibility, cover art, narrators, duplicate titles, broken chapters, suspiciously short tracks, unsupported formats, and Windows/Samba-hostile filenames.

**Bring your own providers**
- Open Library is always on. Add Google Books, LibriVox, or Audible enrichment with:
  ```bash
  bookbot provider set-key googlebooks YOUR_API_KEY
  bookbot provider enable librivox
  bookbot provider enable audible
  bookbot provider list
  ```
- Audible marketplace defaults to US; switch with `bookbot provider set-marketplace UK`.

**Convert and normalize**
- Install FFmpeg, then enable the conversion profile or pass `--profile conversion`.
- Stream copy AAC sources when possible, or set bitrate/VBR/normalization flags per run.
- Chapters can come from tags, track order, or custom names depending on your config.

**DRM tooling**
- Detect protection on folders of files with `bookbot drm detect ./library --recursive`.
- Store activation bytes once using `bookbot drm set-activation-bytes DEADBEAF`.
- Remove DRM in place or to a clean output location using `bookbot drm remove book.aax -o ./clean`.

## CLI cheat sheet

| Command | Purpose |
| --- | --- |
| `bookbot scan DIR --plan PLAN.json` | Inspect directories, infer series/disc structure, and write a reviewable rename plan without touching files. |
| `bookbot review PLAN.json` | Show the saved plan with warnings, conflicts, and exact file moves. |
| `bookbot apply PLAN.json` | Apply a saved plan atomically and emit a transaction ID for undo. |
| `bookbot plan create/show/diff/validate/apply` | Explicit safe-plan workflow for scripting and auditing. |
| `bookbot doctor [DIR]` | Verify runtime dependencies and optionally inspect a library for compatibility problems. |
| `bookbot profile use NAME` | Apply a collector profile such as `safe`, `audiobookshelf`, `plex`, `prologue`, or `apple-books`. |
| `bookbot tui DIR...` | Launch the interactive Textual mission-control interface. Add `--metadata-from-files` to reuse local NFO/JSON sidecars instead of online providers. |
| `bookbot convert DIR -o OUT` | Build single-file, chaptered M4B releases with optional normalization and artwork. |
| `bookbot history --days 7` | Review completed transactions and identify undo candidates. |
| `bookbot undo ID` | Roll back an operation safely using its transaction identifier. |
| `bookbot provider ...` | Enable, disable, and configure metadata providers and API keys. |
| `bookbot config ...` | Manage global config, reset defaults, and inspect profile settings stored under `~/.config/bookbot`. |
| `bookbot audible ...` | Authenticate, list your library, and download titles directly from Audible. |
| `bookbot drm ...` | Detect DRM, save activation bytes, and convert protected files. |
| `bookbot completions SHELL` | Generate shell completions (bash, zsh, fish, or all). |

Use `--help` on any command or subgroup for the full option set.

## Configuration directory layout

| Location | Purpose |
| --- | --- |
| `~/.config/bookbot/config.toml` | Primary configuration file persisted by the TUI and CLI. |
| `~/.config/bookbot/profiles/*.toml` | Saved profiles, including the bundled `safe`, `audiobookshelf`, `plex`, `prologue`, `apple-books`, `full`, and `conversion` presets. |
| `~/.cache/bookbot/` | Cached metadata, cover art, and conversion plans. Delete to force fresh lookups. |
| `~/.local/share/bookbot/logs/transaction_*.json` | Transaction history used for undo and audit logs. |

Config files use TOML; edit by hand or via `bookbot config` commands.

## Metadata providers

| Provider | Notes |
| --- | --- |
| Open Library | Default, always enabled, free. Core search source for titles and authors. |
| Google Books | Requires an API key; adds richer descriptions and ISBN data. |
| LibriVox | Public domain library; great for narrator and language hints. |
| Audible | Requires authentication; unlocks commercial metadata and download tooling. |

Provider priorities and fallback logic are configurable through the `ConfigManager`. Combine multiple providers for higher confidence matches.

## Conversion pipeline

The conversion subsystem lives in `bookbot.convert` and wraps FFmpeg (through `ffmpeg-python`) to build high quality, chaptered M4B files. Highlights:

- Auto-detects when stream copy is safe to avoid re-encoding AAC tracks.
- Supports bitrate or VBR quality targets with optional EBU R128 loudness normalization.
- Generates chapter markers from track segmentation or existing tags.
- Embeds cover art and metadata pulled from the selected provider record.
- Writes conversion plans to disk so you can dry-run before touching anything (`bookbot convert --dry-run`).

## DRM and Audible helpers

BookBot includes a focused DRM toolkit backed by `ffmpeg` and the `audible` Python package.

1. Authenticate once with `bookbot audible auth`; credentials are stored securely via `keyring`.
2. List your library (`bookbot audible list --limit 50`) or import specific titles by ASIN.
3. Use `bookbot drm detect` to map protected files and `bookbot drm remove` to produce DRM-free audio. Activation bytes can be supplied per run or cached via `bookbot drm set-activation-bytes`.

### Retrieving Activation Bytes

For DRM removal of Audible files, you'll need your account's activation bytes. BookBot can retrieve these for you.

**Dependencies:**

This feature requires `selenium` and `chromedriver`. If you are not using the Docker image, you will need to install them:

```bash
pip install selenium
# Make sure chromedriver is in your PATH
```

**Usage:**

Run the following command and follow the prompts to enter your Audible username and password:

```bash
bookbot audible get-activation-bytes
```

This will securely save your activation bytes for future use. After this one-time setup, you can remove DRM from your books without needing to provide the activation bytes every time.

These features depend on optional packages (`audible`, `cryptography`, `keyring`, `selenium`) that are already declared in `pyproject.toml`. Ensure FFmpeg is compiled with the modules required for Audible AAX/AAXC processing.

## Development

```bash
make install-dev   # install dev dependencies
make lint          # ruff + mypy
make format        # black
make test          # pytest
make pre-commit    # run the full formatting and lint bundle
```

Textual ships with a live reload server: run `textual run --dev bookbot.tui.app:BookBotApp` while editing the TUI.

## Contributing

I  welcome issues and pull requests. Please:

1. Fork the repo and create a feature branch.
2. Add or update tests under `tests/` for any new functionality.
3. Run `make pre-commit` before submitting to keep style and typing checks green.
4. Document user-facing changes in this README or the changelog if applicable.

## did you like bookbot?
[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/itsbryandude)


## License

BookBot is released under the MIT License. See [LICENSE](LICENSE) for the full text.
