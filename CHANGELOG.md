# Changelog

## Unreleased

### Upgrade notes
- `output.metadata_json_style` is new. Built-in profiles saved on disk by an earlier version don't have it; when a built-in profile such as `audiobookshelf` is loaded, missing keys now come from the built-in definition, so it loads as `"audiobookshelf"`. A value already in the file is kept. Using the profile writes the key back to its file. [test_abs_followups.py]
- A `config.toml` copied from the audiobookshelf profile by an earlier `bookbot profile use audiobookshelf` also lacks the key and falls back to `"booklore"`. `bookbot doctor DIR` warns when the library has Audiobookshelf-style `metadata.json` files (series or narrators as lists) while the style is `"booklore"`, and prints the fix: `bookbot config set output.metadata_json_style audiobookshelf`. [test_abs_followups.py]
