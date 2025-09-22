<p align="center">
  <img src=".github/assets/bookBot.png" alt="BookBot Logo" width="200"/>
</p>
# BookBot: The Audiophile's Audiobook Organizer

Organize your audiobook library with confidence. BookBot is a sleek, powerful, and safety-first TUI (Terminal User Interface) for renaming, organizing, and converting your audiobook collection. Built for audiobook lovers who crave order, it combines smart metadata matching with a bulletproof file operation system.

## Badges & Status

<p align="center">
<a href="https://github.com/itsbryanman/BookBot/actions"><img src="https://img.shields.io/github/actions/workflow/status/itsbryanman/BookBot/ci.yml?branch=main&style=for-the-badge&logo=githubactions&logoColor=white" alt="Build Status"></a>
<a href="https://img.shields.io/github/languages/top/itsbryanman/BookBot"><img src="https://img.shields.io/github/languages/top/itsbryanman/BookBot?style=for-the-badge&logo=python&logoColor=white" alt="Language"></a>
<a href="https://github.com/itsbryanman/BookBot/blob/main/LICENSE"><img src="https://img.shields.io/github/license/itsbryanman/BookBot?style=for-the-badge&color=brightgreen" alt="License: MIT"></a>
<a href="https://github.com/itsbryanman/BookBot/releases"><img src="https://img.shields.io/github/v/release/itsbryanman/BookBot?style=for-the-badge&logo=github&logoColor=white" alt="Latest Release"></a>
<br>
<a href="https://github.com/psf/black"><img src="https://img.shields.io/badge/code%20style-black-000000.svg?style=for-the-badge&logo=python&logoColor=white" alt="Code Style: Black"></a>
<a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/linter-ruff-FCC21B.svg?style=for-the-badge&logo=ruff&logoColor=white" alt="Linter: Ruff"></a>
<a href="http://mypy-lang.org/"><img src="https://img.shields.io/badge/typed-mypy-blue.svg?style=for-the-badge&logo=python&logoColor=white" alt="Type Checking: Mypy"></a>
<a href="https://github.com/itsbryanman/BookBot/"><img src="https://img.shields.io/github/stars/itsbryanman/BookBot?style=for-the-badge&logo=github&logoColor=white" alt="GitHub Stars"></a>
</p>

## Why BookBot?

Messy audiobook folders are a thing of the past. BookBot was built to solve the most common frustrations with digital audiobook collections, focusing on four core principles:

**Safety First**: Your files are precious. BookBot uses a preview-first workflow, atomic file operations, and full undo capabilities for every transaction. No more accidental deletions or botched renames.

**Intelligent Matching**: Leveraging multiple metadata sources including Open Library, Google Books, LibriVox, and Audible, BookBot intelligently scans your files and folder names to find accurate metadata, from authors and series to publication years and cover art.

**Ultimate Flexibility**: Your library, your rules. With a powerful templating engine and configurable profiles (e.g., for Plex), you can shape your folder and file structures exactly how you want them.

**Power-Packed Features**: Go beyond renaming. BookBot offers DRM removal for supported formats, an optional robust pipeline to convert your track-based audiobooks into single, beautifully tagged M4B files, complete with chapter markers and embedded artwork.

## Features

### Safe & Reversible Operations
A "dry-run" by default workflow means you preview every change. All file operations are atomic (all-or-nothing) and logged, allowing you to undo any transaction with a simple command.

### Smart Metadata Matching
Integrates seamlessly with multiple providers:
- **Open Library** (default, free): Comprehensive book database
- **Google Books**: Extensive catalog with rich metadata (API key required)
- **LibriVox**: Public domain audiobooks
- **Audible**: Commercial audiobook metadata

Uses fuzzy string matching for impressive accuracy across all providers.

### Flexible Naming Templates
Fully customizable filename and folder structures using a simple token system (e.g., `{AuthorLastFirst}/{Title} ({Year})`).

### Multi-Disc & Complex Collection Support
Intelligently parses disc numbers from folder or file names to correctly handle even the most complex audiobook sets.

### DRM Removal
Built-in support for removing DRM from supported formats:
- **Audible AAX/AAXC files** with secure browser-based authentication
- Automatic detection and handling of various DRM types
- Safe conversion to DRM-free formats
- No manual activation bytes required for Audible content

### M4B Conversion Pipeline
An optional feature to merge audiobook tracks into a single M4B file, powered by FFmpeg. Features include:
- Smart encoding (stream-copying AAC tracks, transcoding others)
- Automatic chapter marker generation from track boundaries
- Complete metadata tagging, including cover art
- Optional EBU R128 loudness normalization for a consistent listening experience

### Cross-Platform TUI
A clean, modern, and intuitive terminal interface built with Textual that works flawlessly on Windows, macOS, and Linux.

### Configuration Profiles
Switch between pre-configured setups for different use cases like safe (rename only), full (rename & tag), and plex (optimized for Plex Media Server).

## Quick Start

### 1. Installation (Recommended)
The easiest way to install BookBot is with pipx, which installs it in an isolated environment.

```bash
pipx install bookbot
```

### 2. Launch the TUI
Point BookBot to your audiobooks folder to launch the interactive TUI.

```bash
bookbot tui /path/to/your/audiobooks
```

### 3. The TUI Workflow
The TUI will guide you through a simple, 4-step process:

1. **Scan**: BookBot analyzes your folder structure and reads existing metadata
2. **Match**: It fetches metadata candidates from your enabled providers for you to review
3. **Preview**: You see a full list of all proposed file and folder changes before anything is touched
4. **Apply**: Once you confirm, BookBot executes the rename operations safely and atomically

## Complete Feature Guide

### Core Commands

#### Scanning and Organization
```bash
# Basic scan of a directory
bookbot scan /path/to/audiobooks

# Scan with custom profile
bookbot scan /path/to/audiobooks --profile plex

# Scan without tagging
bookbot scan /path/to/audiobooks --no-tag

# Scan with custom template
bookbot scan /path/to/audiobooks --template audible

# Scan with language preference
bookbot scan /path/to/audiobooks --lang es
```

#### Interactive TUI
```bash
# Launch TUI for single folder
bookbot tui /path/to/audiobooks

# Launch TUI for multiple folders
bookbot tui /folder1 /folder2 /folder3

# Launch TUI with specific profile
bookbot tui /path/to/audiobooks --profile plex
```

#### M4B Conversion
```bash
# Basic conversion
bookbot convert /input/folder -o /output/folder

# High quality conversion with VBR
bookbot convert /input/folder -o /output/folder --vbr 5

# Conversion with audio normalization
bookbot convert /input/folder -o /output/folder --normalize

# Conversion without cover art
bookbot convert /input/folder -o /output/folder --no-art

# Dry run to see conversion plan
bookbot convert /input/folder -o /output/folder --dry-run
```

### Audible Integration

#### Authentication
```bash
# Authenticate with Audible for importing books
bookbot audible auth

# The command opens your browser for secure OAuth authentication
# No need to extract or manage activation bytes manually
```

#### Import Books
```bash
# Import a single book by ASIN
bookbot audible import B01234567X

# Import with custom output directory
bookbot audible import B01234567X -o /path/to/downloads

# Import and automatically remove DRM
bookbot audible import B01234567X --remove-drm

# Import with manual activation bytes (if needed)
bookbot audible import B01234567X --remove-drm --activation-bytes XXXXXXXX
```

#### Library Management
```bash
# List your Audible library
bookbot audible list

# List with custom limit
bookbot audible list --limit 50
```

### DRM Management

#### Detection
```bash
# Detect DRM on individual files
bookbot drm detect file1.aax file2.m4a

# Detect DRM recursively in directories
bookbot drm detect /audiobook/folder --recursive

# Detect DRM on all audio files in current directory
bookbot drm detect *.{mp3,m4a,aax,aaxc}
```

#### Removal
```bash
# Store activation bytes securely for AAX files
bookbot drm set-activation-bytes XXXXXXXX

# Remove DRM from AAX/AAXC files (uses stored activation bytes)
bookbot drm remove file.aax

# Remove DRM with custom output directory
bookbot drm remove *.aax -o /output/folder

# Dry run to see what would be processed
bookbot drm remove *.aax --dry-run

# Process entire directory recursively
bookbot drm remove /folder --recursive

# Manual activation bytes (overrides stored ones)
bookbot drm remove file.aax --activation-bytes XXXXXXXX
```

### Provider Management

#### Configuration
```bash
# List all available providers and their status
bookbot provider list

# Enable Google Books (requires API key)
bookbot provider set-key googlebooks YOUR_API_KEY_HERE
bookbot provider enable googlebooks

# Enable LibriVox for public domain books
bookbot provider enable librivox

# Enable Audible metadata
bookbot provider enable audible

# Set Audible marketplace region
bookbot provider set-marketplace UK  # or US, CA, AU, FR, DE, IT, ES, JP, IN

# Disable a provider
bookbot provider disable audible
```

### Configuration Management

#### Profiles
```bash
# List available configuration profiles
bookbot config list

# Show current configuration
bookbot config show

# Show specific profile configuration
bookbot config show plex

# Reset configuration to defaults
bookbot config reset
```

#### Available Profiles
- **default**: Standard audiobook naming
- **plex**: Plex Media Server optimized naming
- **audible**: Audible-style naming with narrator
- **series**: Series-focused organization
- **safe**: Rename only, no tagging
- **full**: Complete rename and tag operations

### Advanced Features

#### Transaction Management
```bash
# View operation history
bookbot history

# View history for specific time period
bookbot history --days 7

# Undo a specific transaction
bookbot undo abc123def

# Undo most recent transaction
bookbot undo $(bookbot history --days 1 | head -1 | cut -d' ' -f1)
```

#### Shell Completions
```bash
# Generate completions for your shell
bookbot completions bash > /etc/bash_completion.d/bookbot
bookbot completions zsh > ~/.zsh/completions/_bookbot
bookbot completions fish > ~/.config/fish/completions/bookbot.fish

# Generate all completions to a directory
bookbot completions all -o ./completions
```

### Template System

#### Available Tokens
- `{Author}`: Primary author name
- `{AuthorLastFirst}`: Author in "Last, First" format
- `{Title}`: Book title
- `{ShortTitle}`: Abbreviated title
- `{SeriesName}`: Series name if available
- `{SeriesIndex}`: Book number in series
- `{Year}`: Publication year
- `{Language}`: Book language
- `{Narrator}`: Audiobook narrator
- `{DiscPad}`: Zero-padded disc number
- `{TrackPad}`: Zero-padded track number
- `{Disc}`: Disc number
- `{Track}`: Track number
- `{TrackTitle}`: Individual track/chapter title
- `{ISBN}`: ISBN identifier

#### Template Examples
```bash
# Folder templates
"{AuthorLastFirst}/{Title} ({Year})"
# Result: "Sanderson, Brandon/The Way of Kings (2010)"

"{SeriesName}/{SeriesIndex} - {Title}"
# Result: "The Stormlight Archive/01 - The Way of Kings"

"{Author}/{SeriesName} {SeriesIndex} - {Title} ({Narrator})"
# Result: "Brandon Sanderson/The Stormlight Archive 01 - The Way of Kings (Michael Kramer)"

# File templates
"{DiscPad}{TrackPad} - {TrackTitle}"
# Result: "0101 - Prologue"

"Chapter {TrackPad} - {Title}"
# Result: "Chapter 01 - The Way of Kings"

"{Author} - {Title} - {TrackPad}"
# Result: "Brandon Sanderson - The Way of Kings - 01"
```

### TUI Navigation

#### Keyboard Shortcuts
- **Tab/Shift+Tab**: Navigate between screens
- **Enter**: Confirm selections
- **Escape**: Cancel operations
- **Ctrl+C**: Quit application
- **Ctrl+H**: Show help
- **Ctrl+S**: Save configuration
- **Ctrl+R**: Refresh current view

#### TUI Workflow Screens
1. **Source Selection**: Choose and configure source directories
2. **Scan Results**: Review discovered audiobook sets and warnings
3. **Match Review**: Examine metadata matches and confidence scores
4. **Preview**: See proposed changes before applying
5. **Convert**: Configure and execute M4B conversion
6. **DRM Removal**: Secure browser-based Audible authentication and DRM removal

### Troubleshooting

#### Common Issues
```bash
# If Google Books isn't working
bookbot provider list  # Check if API key is set
bookbot provider set-key googlebooks YOUR_KEY

# If FFmpeg is missing for conversion
# Install FFmpeg on your system first
sudo apt install ffmpeg  # Ubuntu/Debian
brew install ffmpeg      # macOS
choco install ffmpeg     # Windows

# If DRM removal fails for Audible files
# Use the TUI DRM Removal tab for browser-based authentication
bookbot tui /path/to/audiobooks
# Navigate to DRM Removal tab and click "Begin Login"
# Complete authentication in your browser when prompted

# Clear cache if metadata seems stale
rm -rf ~/.cache/bookbot/

# Reset configuration if issues persist
bookbot config reset
```

#### Debug Mode
```bash
# Run with verbose logging
bookbot --log debug.log scan /path/to/audiobooks

# Check transaction logs
bookbot history --days 30
```

## Alternative Installation Methods

### Using pip
```bash
pip install bookbot
```

### From Source
For development or to get the latest changes:

```bash
git clone https://github.com/itsbryanman/BookBot.git
cd BookBot
pip install -e .
```

### Binary Releases
Download pre-built binaries from the [releases page](https://github.com/itsbryanman/BookBot/releases) for Windows, macOS, and Linux.

## Contributing

BookBot is an open-source project, and contributions are highly welcome!

### Development Setup

1. **Fork & Clone** the repository:
```bash
git clone https://github.com/your-username/BookBot.git
cd BookBot
```

2. **Install in editable mode** with dev dependencies:
```bash
pip install -e ".[dev]"
```

3. **Run the test suite** to ensure everything is working:
```bash
pytest
```

4. **Run pre-commit checks** before submitting a PR:
```bash
make pre-commit
```

### Available Make Commands
```bash
make help          # Show all available commands
make install-dev   # Install development dependencies
make test          # Run test suite
make lint          # Run linting
make format        # Format code
make build         # Build packages
make binary        # Build single-file executable
make completions   # Generate shell completions
```

## Credits & Acknowledgements

BookBot stands on the shoulders of giants. A huge thank you to the developers of these incredible open-source libraries:

- **Textual**: For the amazing TUI framework
- **Open Library**: For providing the book metadata
- **Mutagen**: For robust audio metadata handling
- **FFmpeg**: For the powerful audio conversion capabilities
- **Click**: For the clean command-line interface
- **Pydantic**: For rock-solid data modeling

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---

**BookBot: Organize your audiobook library with confidence.**
