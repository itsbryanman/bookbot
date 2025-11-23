# BookBot User Acceptance Testing (UAT) Report

**Test Date:** 2025-11-23
**Tester Role:** New End User
**Test Scope:** README.md verification using real audiobook data
**Environment:** Docker container, Linux 6.12.48

---

## Executive Summary

BookBot's **scan** functionality works excellently and delivers on its README promises. However, the **convert** command is currently **blocked by a critical bug** and suffers from poor discoverability around configuration requirements. The tool shows strong potential but needs polish before it's production-ready for new users.

**Overall Rating:** 6/10
- Scan: 9/10 (Excellent)
- Convert: 2/10 (Broken)
- Documentation: 7/10 (Good but incomplete)
- Onboarding: 4/10 (Frustrating)

---

## 1. Bugs & Technical Issues

### 🔴 **Critical: Convert Command Completely Broken**

**Bug:** `bookbot convert` fails with an unhandled AttributeError

```
Error during conversion: "ConversionConfig" object has no field "write_cover_art"
```

**Root Cause:**
`bookbot/convert/pipeline.py:206` attempts to access `config.write_cover_art`, but the `config` parameter is a `ConversionConfig` object. The `write_cover_art` field belongs to `TaggingConfig` instead (defined in `bookbot/config/models.py:59`).

**Impact:** Convert command is unusable in all scenarios (including --dry-run).

**Reproduction:**
```bash
bookbot convert "./test_library/11.22.63 - Stephen King" -o ./test_output --dry-run
```

**Expected Behavior:**
Should generate a dry-run conversion plan without errors.

**Suggested Fix:**
Either:
1. Add `write_cover_art: bool` to the `ConversionConfig` model, OR
2. Pass both `ConversionConfig` and `TaggingConfig` to the pipeline and use the appropriate one

---

### 🟡 **Medium: Misleading Documentation - Missing Profiles**

**Bug:** README.md references a `--profile conversion` flag, but the profile doesn't exist.

```bash
$ bookbot convert --profile conversion ...
Error: Profile 'conversion' not found

$ bookbot config list
No profiles found
```

**Expected Behavior:**
According to README.md line 137:
> "Install FFmpeg, then enable the conversion profile or pass `--profile conversion`."

And line 122:
> "Activate opinionated profiles (`safe`, `full`, `plex`, `conversion`)"

**Impact:** New users following README instructions hit a dead end.

**Note:** A `profiles/` directory exists in `~/.config/bookbot/` but it's empty. The bundled profiles mentioned in README are not auto-generated.

---

### 🟡 **Medium: Poor Error Message for Disabled Conversion**

**Bug:** When conversion is disabled in config, error message doesn't guide user to solution.

```bash
$ bookbot convert ... --dry-run
Error: M4B conversion is not enabled in configuration

Enable it with a conversion profile or modify your config
```

**Issue:** The suggestion "enable it with a conversion profile" doesn't work (profiles don't exist). Users must:
1. Find `~/.config/bookbot/config.toml`
2. Manually edit `[conversion] enabled = false` to `true`
3. Retry the command

**Suggested Improvement:**
```
Error: M4B conversion is not enabled.

To enable conversion, run:
  bookbot config set conversion.enabled true

Or manually edit: ~/.config/bookbot/config.toml
  [conversion]
  enabled = true
```

---

### 🟢 **Minor: Silent File Copy Failure**

**Bug:** Copying "01 Howl's Moving Castle" failed silently in a batch operation.

```bash
$ cp -r "/Audiobookshelf/Audiobooks/01 Howl's Moving Castle" ./test_library/
cp: cannot stat "/Audiobookshelf/Audiobooks/01 Howl's Moving Castle": No such file or directory
```

**Note:** This might be a trailing slash issue or special characters in the directory name. Not a BookBot bug, but worth noting for data validation.

---

## 2. UX Frictions (Inconveniences)

### 😣 **Poor Onboarding Experience**

**Issue:** No first-run setup wizard or automatic configuration.

**Pain Points:**
1. Conversion is disabled by default (`enabled = false`)
2. No CLI command to enable it (`bookbot config set` doesn't exist)
3. Must manually hunt for config file location
4. Must understand TOML format to edit config
5. Profiles mentioned in README don't exist

**User Journey:**
```
New user reads README → Tries convert command → Gets error →
Reads error message → Tries --profile conversion → Gets error →
Has to grep for config files → Manually edits TOML → Tries again →
Hits critical bug → Gives up
```

**Expected Experience:**
```
New user reads README → Tries convert command →
Tool either works OR provides clear guidance →
Success
```

---

### 🤔 **Unclear Prerequisites**

**Issue:** README mentions FFmpeg is required (line 82, 137) but doesn't validate its presence.

**Suggestion:** Run a pre-flight check:
```bash
$ bookbot convert ...
Warning: FFmpeg not found in PATH. Conversion requires FFmpeg.
Install with: apt-get install ffmpeg
```

---

### 📝 **Scan Command Output Lacks Next Steps**

**Issue:** Scan works beautifully but doesn't guide users on what to do next.

**Current Output:**
```
Scanning test_library...
Found 3 audiobook set(s):

1. 11.22.63 - Stephen King
   Tracks: 4
   ...

Dry run completed. Use 'bookbot tui' for interactive processing.
```

**Suggestion:** Add actionable next steps:
```
Dry run completed. Next steps:

  Interactive mode:  bookbot tui ./test_library
  Convert to M4B:    bookbot convert "./test_library/11.22.63 - Stephen King" -o ./output
  View config:       bookbot config show

For help, run: bookbot --help
```

---

### ⏱️ **No Progress Feedback**

**Issue:** File copy operations (3 audiobooks with 55+ tracks) had no progress indicator.

**Observation:** While BookBot itself isn't responsible for `cp` commands, the TUI should show progress for:
- Scanning large libraries
- Converting multi-file audiobooks
- Metadata lookups from providers

**Unknown:** Didn't test TUI in this session (only CLI), so can't comment on TUI progress indicators.

---

### 🗂️ **Config File Discovery**

**Issue:** Config location is documented (README line 167) but not discoverable via CLI.

**Suggestion:**
```bash
$ bookbot config where
/root/.config/bookbot/config.toml

$ bookbot config edit
# Opens config.toml in $EDITOR
```

---

## 3. Feature Requests & Improvements

### 🚀 **High Priority: Make Convert "Just Work"**

**Request:** Auto-enable conversion when user runs convert command.

**Rationale:** If a user explicitly runs `bookbot convert`, their intent is clear. Don't make them edit config files.

**Proposed Behavior:**
```bash
$ bookbot convert ... --dry-run
Note: Conversion was disabled. Automatically enabled for this session.
To persist this setting, run: bookbot config set conversion.enabled true

[proceeds with dry-run]
```

Or prompt:
```bash
$ bookbot convert ...
Conversion is disabled. Enable it now? [Y/n]:
```

---

### 🔧 **High Priority: Add Config CLI Commands**

**Request:** Provide CLI commands to modify config without manual TOML editing.

**Suggested Commands:**
```bash
bookbot config set conversion.enabled true
bookbot config set conversion.bitrate 256k
bookbot config get conversion.enabled
bookbot config reset conversion
bookbot config edit  # Open in $EDITOR
```

**Benefit:** Lowers barrier to entry for non-technical users.

---

### 📦 **High Priority: Auto-Generate Bundled Profiles**

**Request:** Create the 4 bundled profiles (`safe`, `full`, `plex`, `conversion`) on first run.

**Current State:** `~/.config/bookbot/profiles/` exists but is empty.

**Expected State:**
```
~/.config/bookbot/profiles/
├── safe.toml
├── full.toml
├── plex.toml
└── conversion.toml
```

**Rationale:** README promises these profiles exist. Users shouldn't need to create them manually.

---

### 🛠️ **Medium Priority: First-Run Setup Wizard**

**Request:** Interactive setup on first run (similar to `aws configure`, `git config --global`).

**Example:**
```bash
$ bookbot scan ./audiobooks

Welcome to BookBot!
It looks like this is your first time running BookBot.

Would you like to run the setup wizard? [Y/n]: y

1. Enable M4B conversion? [y/N]: y
2. Enable metadata tagging? [Y/n]: y
3. Active template [default/plex/audible/series]: default
4. Configure metadata providers? [y/N]: n

Setup complete! Configuration saved to:
/root/.config/bookbot/config.toml

Run 'bookbot config show' to review your settings.
```

---

### 📊 **Medium Priority: Better Error Messages**

**Request:** All errors should include:
1. What went wrong
2. Why it went wrong
3. How to fix it

**Examples:**

**Bad:**
```
Error: Profile 'conversion' not found
```

**Good:**
```
Error: Profile 'conversion' not found

Available profiles: (none)

To create a custom profile:
  bookbot config create-profile conversion

Or edit manually:
  ~/.config/bookbot/profiles/conversion.toml
```

---

**Bad:**
```
Error during conversion: "ConversionConfig" object has no field "write_cover_art"
```

**Good:**
```
Error: Internal configuration mismatch

This appears to be a bug in BookBot. Please report it at:
https://github.com/itsbryanman/BookBot/issues

Error details: ConversionConfig missing field 'write_cover_art'
Location: bookbot/convert/pipeline.py:206
```

---

### 🎯 **Low Priority: Validation & Preflight Checks**

**Request:** Validate inputs before starting operations.

**Examples:**
1. Check if FFmpeg is installed before convert
2. Verify output directory is writable
3. Warn if input directory is empty
4. Detect if files are already in M4B format

---

### 📖 **Low Priority: Improved README Examples**

**Request:** Add a "Quick Start (5 minutes)" section with copy-pasteable commands.

**Suggested Section:**
```markdown
## Quick Start (5 Minutes)

# 1. Enable conversion
echo "Edit ~/.config/bookbot/config.toml and set [conversion] enabled = true"

# 2. Scan your library
bookbot scan /path/to/audiobooks

# 3. Launch the TUI to review
bookbot tui /path/to/audiobooks

# 4. Convert a single book
bookbot convert "/path/to/audiobooks/MyBook" -o ./output --dry-run
```

---

### 🎨 **Low Priority: Config Defaults**

**Request:** Consider different defaults for `config.toml`:

| Setting | Current | Suggested | Reason |
|---------|---------|-----------|--------|
| `conversion.enabled` | `false` | `true` | Tool advertises conversion as core feature |
| `safe_mode` | `true` | `true` | ✅ Good default |
| `dry_run_default` | `true` | `true` | ✅ Good default |

**Rationale:** If conversion is a headline feature (README line 41: "Optional M4B conversion pipeline"), it shouldn't be disabled by default.

**Alternative:** Keep it disabled but prompt on first convert attempt.

---

## 4. What Worked Well ✅

### Scan Command
- **Perfect execution** - Detected all 3 audiobooks correctly
- **Good metadata inference** - Correctly identified author for "A Good Girl's Guide to Murder" as "Holly Jackson"
- **Useful warnings** - Flagged track numbering gaps in "Cujo"
- **Clear output** - Easy to read, well-structured

### Documentation
- README is comprehensive and well-organized
- Good use of tables, code blocks, and examples
- Configuration directory layout is documented
- Cheat sheet is helpful

### Configuration File
- TOML format is human-readable
- Well-commented and organized
- Sensible structure (tagging, conversion, providers separated)

---

## 5. Testing Notes

### Test Environment
- **Working Directory:** `/coding_projects/bookbot`
- **Data Source:** `/Audiobookshelf/Audiobooks/` (mounted volume)
- **Test Library:** `./test_library/` (3 audiobooks, ~60 tracks total)
- **Commands Tested:** `scan`, `convert --dry-run`, `config show`, `config list`
- **Commands NOT Tested:** `tui`, `audible`, `drm`, `history`, `undo`, `provider`

### Test Data
1. **11.22.63 - Stephen King** (4 tracks, 1 disc)
2. **A Good Girl's Guide to Murder** (55 tracks, 1 disc, by Holly Jackson)
3. **Cujo - Stephen King** (1 track, 1 disc, track numbering warning)

### Time Spent
- **Setup:** ~5 minutes (reading README, copying files)
- **Testing:** ~10 minutes (running commands, investigating errors)
- **Debugging:** ~15 minutes (tracing bug, checking code)
- **Total:** ~30 minutes

---

## 6. Recommendations

### Immediate Actions (Blockers)
1. **Fix the `write_cover_art` bug** - Convert is completely broken
2. **Create bundled profiles** - README promises they exist
3. **Update error messages** - Guide users to solutions

### Short-Term Improvements (UX Polish)
4. **Add `bookbot config set` commands** - No more manual TOML editing
5. **Auto-enable conversion** - Or at least prompt the user
6. **Add preflight checks** - Validate FFmpeg, paths, etc.

### Long-Term Enhancements (Nice-to-Have)
7. **First-run setup wizard** - Smooth onboarding for new users
8. **Progress indicators** - For long operations
9. **Better error context** - What, why, how to fix
10. **Expand test coverage** - Ensure convert pipeline is tested

---

## 7. Conclusion

BookBot has a **solid foundation** with excellent scan functionality and thoughtful configuration design. However, the convert feature—one of the tool's headline capabilities—is currently unusable due to a critical bug.

**For New Users:** The onboarding experience is frustrating. Following the README leads to errors, and recovering requires diving into config files and source code.

**For Production Use:** Not ready. The convert bug is a showstopper, and the missing profiles make the tool feel unfinished.

**Potential:** High. With bug fixes and UX polish, this could be a best-in-class audiobook management tool.

---

## Appendix: Commands Run During Testing

```bash
# Setup
ls -F /Audiobookshelf
ls -F /Audiobookshelf/Audiobooks/
mkdir -p ./test_library
cp -r "/Audiobookshelf/Audiobooks/11.22.63 - Stephen King" ./test_library/
cp -r "/Audiobookshelf/Audiobooks/A Good Girl's Guide to Murder" ./test_library/
cp -r "/Audiobookshelf/Audiobooks/Cujo - Stephen King" ./test_library/
ls -la ./test_library/

# Testing
bookbot scan ./test_library                                      # ✅ SUCCESS
bookbot convert "./test_library/11.22.63 - Stephen King" \
  -o ./test_output --dry-run                                     # ❌ FAIL (conversion disabled)
bookbot convert "./test_library/11.22.63 - Stephen King" \
  -o ./test_output --profile conversion --dry-run                # ❌ FAIL (profile not found)
bookbot config list                                              # ✅ No profiles found
bookbot config show                                              # ✅ Showed conversion disabled

# Manual fix attempt
vi /root/.config/bookbot/config.toml                            # Changed enabled = true
bookbot convert "./test_library/11.22.63 - Stephen King" \
  -o ./test_output --dry-run                                     # ❌ FAIL (write_cover_art bug)

# Investigation
grep -r "write_cover_art" bookbot/                              # Found bug location
cat bookbot/config/models.py                                    # Confirmed ConversionConfig
cat bookbot/convert/pipeline.py                                 # Confirmed bug
```

---

**Report End**
