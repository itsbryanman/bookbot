"""Source folders left holding only OS junk are pruned via quarantine.

After an apply, a book folder that contained audio plus Thumbs.db/desktop.ini
used to survive as a stale folder. The junk is now moved (never deleted) into
``.bookbot-quarantine/<plan_id>/`` under the journal so undo restores it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from bookbot.core.discovery import AudioFileScanner
from bookbot.core.operations import TransactionManager
from bookbot.core.planning import PlanBuilder

FIXTURE_MP3 = (
    Path(__file__).parent
    / "fixtures"
    / "messy_library"
    / "Brandon Sanderson - The Way of Kings"
    / "CD1"
    / "01 - Prelude.mp3"
)


def _make_track(path: Path, title: str, album: str, track: int) -> None:
    """Copy the fixture MP3 and retag it so filenames don't collide."""
    from mutagen.id3 import ID3, TALB, TIT2, TPE1, TRCK

    shutil.copy(FIXTURE_MP3, path)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text="Dave Grossman"))
    tags.add(TALB(encoding=3, text=album))
    tags.add(TRCK(encoding=3, text=str(track)))
    tags.save(path)


def _tree(root: Path) -> dict[str, bytes | None]:
    """Snapshot every file (with contents) and directory under ``root``."""
    snapshot: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        snapshot[rel] = path.read_bytes() if path.is_file() else None
    return snapshot


def _apply(library: Path, config_manager) -> tuple[TransactionManager, str]:
    sets = AudioFileScanner().scan_directory(library)
    config = config_manager.load_config()
    plan = PlanBuilder(config).create_plan(library, sets, source_roots=[library])
    manager = TransactionManager(config_manager)
    assert manager.execute_plan(plan)
    assert plan.applied_transaction_id
    return manager, plan.applied_transaction_id


def _add_junk(folder: Path) -> None:
    (folder / "Thumbs.db").write_bytes(b"THUMBS-" + folder.name.encode())
    (folder / "desktop.ini").write_text("[.ShellClassInfo]\n" + folder.name)


def test_junk_only_source_folder_is_quarantined_and_undo_restores(
    tmp_path: Path, config_manager
) -> None:
    library = tmp_path / "lib"
    book = library / "Gillian Flynn - Gone Girl"
    book.mkdir(parents=True)
    _make_track(book / "01.mp3", "Track 1", "Gone Girl", 1)
    _add_junk(book)
    before = _tree(library)

    manager, transaction_id = _apply(library, config_manager)

    assert not book.exists()
    quarantined = sorted(
        path.name for path in (library / ".bookbot-quarantine").rglob("*")
    )
    assert "Thumbs.db" in quarantined
    assert "desktop.ini" in quarantined

    assert manager.undo_transaction(transaction_id)
    assert _tree(library) == before


def test_nested_parent_and_child_books_leave_no_junk_folders(
    tmp_path: Path, config_manager
) -> None:
    library = tmp_path / "lib"
    parent = library / "Dave Grossman - On Combat"
    child = parent / "Dave Grossman - On Killing"
    child.mkdir(parents=True)
    for track in range(1, 3):
        _make_track(parent / f"{track:02d}.mp3", f"T{track}", "On Combat", track)
        _make_track(child / f"{track:02d}.mp3", f"T{track}", "On Killing", track)
    _add_junk(parent)
    _add_junk(child)
    (child / "._01.mp3").write_bytes(b"APPLEDOUBLE")
    before = _tree(library)

    manager, transaction_id = _apply(library, config_manager)

    assert not parent.exists()
    leftovers = [
        path
        for path in library.rglob("*")
        if path.is_file() and ".bookbot-quarantine" not in path.parts
    ]
    assert all(path.suffix != ".db" and path.suffix != ".ini" for path in leftovers)

    assert manager.undo_transaction(transaction_id)
    assert _tree(library) == before


def test_folder_with_real_content_keeps_its_junk(
    tmp_path: Path, config_manager
) -> None:
    library = tmp_path / "lib"
    book = library / "Author Name - Some Book"
    book.mkdir(parents=True)
    _make_track(book / "01.mp3", "Track 1", "Some Book", 1)
    (book / "notes.txt").write_text("keep me")
    _add_junk(book)

    _apply(library, config_manager)

    assert (book / "notes.txt").exists()
    assert (book / "Thumbs.db").exists()
    assert (book / "desktop.ini").exists()
    assert not (library / ".bookbot-quarantine").exists()
