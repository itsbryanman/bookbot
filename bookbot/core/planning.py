"""Rename plan creation, serialization, and review helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from ..config.models import Config
from .models import AudiobookSet, RenameOperation, RenamePlan
from .templates import TemplateEngine


class PlanBuilder:
    """Build deterministic rename plans from discovered audiobook sets."""

    TRACK_SAFE_FILE_TEMPLATE = "{DiscPad}{TrackPad} - {TrackTitle}"

    # Non-audio companions that should travel with a book when its audio
    # moves. Leaving these behind orphans covers/metadata on every apply.
    SIDECAR_EXTENSIONS = {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".opf",
        ".nfo",
        ".cue",
        ".json",
        ".m3u",
        ".m3u8",
    }
    _AUDIO_EXTENSIONS = {
        ".mp3",
        ".m4a",
        ".m4b",
        ".flac",
        ".ogg",
        ".opus",
        ".aac",
        ".wav",
    }

    def _companion_operations(
        self, audiobook_set: AudiobookSet, destination_folder: Path
    ) -> list[RenameOperation]:
        """Plan moves for sidecars (covers, .opf, .nfo, ...) with their book.

        A folder's companions move only when every audio file in that folder
        belongs to this set, so sidecars of unrelated books sharing a folder
        are never claimed. First basename claim wins on collision (e.g. one
        cover.jpg per disc folder).
        """
        track_paths = {
            track.src_path.resolve(strict=False) for track in audiobook_set.tracks
        }
        folders: list[Path] = []
        seen: set[Path] = set()
        for track in audiobook_set.tracks:
            parent = track.src_path.parent
            if parent not in seen:
                seen.add(parent)
                folders.append(parent)
        source_root = audiobook_set.source_path
        if source_root.is_dir() and source_root not in seen:
            folders.append(source_root)

        operations: list[RenameOperation] = []
        claimed_names: set[str] = set()
        for folder in folders:
            if not folder.is_dir():
                continue
            try:
                entries = sorted(folder.iterdir())
            except OSError:
                continue

            folder_audio = [
                entry
                for entry in entries
                if entry.is_file()
                and entry.suffix.lower() in self._AUDIO_EXTENSIONS
            ]
            if any(
                entry.resolve(strict=False) not in track_paths
                for entry in folder_audio
            ):
                # Folder is shared with audio outside this set; don't claim
                # its sidecars.
                continue

            for entry in entries:
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in self.SIDECAR_EXTENSIONS:
                    continue
                name_key = entry.name.lower()
                if name_key in claimed_names:
                    continue
                new_path = destination_folder / entry.name
                if new_path.resolve(strict=False) == entry.resolve(strict=False):
                    claimed_names.add(name_key)
                    continue
                claimed_names.add(name_key)
                operations.append(
                    RenameOperation(old_path=entry, new_path=new_path)
                )

        return operations

    def __init__(self, config: Config):
        self.config = config
        self.template_engine = TemplateEngine(
            case_policy=config.case_policy,
            unicode_normalize=config.unicode_normalization,
            max_path_length=config.max_path_length,
        )

    def create_plan(
        self,
        library_root: Path,
        audiobook_sets: list[AudiobookSet],
        profile_name: str | None = None,
        folder_template: str | None = None,
        file_template: str | None = None,
        source_roots: list[Path] | None = None,
    ) -> RenamePlan:
        """Create a rename plan rooted inside one library directory."""
        folder_template = folder_template or self.config.output.folder_template
        file_template = file_template or self.config.output.file_template
        source_roots = source_roots or [library_root]

        plan = self._build_plan(
            library_root=library_root,
            audiobook_sets=audiobook_sets,
            profile_name=profile_name,
            folder_template=folder_template,
            file_template=file_template,
            source_roots=source_roots,
        )

        if self.config.output.prefer_m4b and any(
            conflict.startswith("Duplicate target path:") for conflict in plan.conflicts
        ):
            fallback_plan = self._build_plan(
                library_root=library_root,
                audiobook_sets=audiobook_sets,
                profile_name=profile_name,
                folder_template=folder_template,
                file_template=self.TRACK_SAFE_FILE_TEMPLATE,
                source_roots=source_roots,
            )
            if not fallback_plan.conflicts:
                fallback_plan.warnings.append(
                    "Profile prefers single-file outputs; the rename plan kept unique "
                    "per-track names until conversion happens."
                )
                plan = fallback_plan

        return plan

    def _build_plan(
        self,
        library_root: Path,
        audiobook_sets: list[AudiobookSet],
        profile_name: str | None,
        folder_template: str,
        file_template: str,
        source_roots: list[Path],
    ) -> RenamePlan:
        operations: list[RenameOperation] = []
        root = library_root.resolve(strict=False)
        resolved_source_roots = [path.resolve(strict=False) for path in source_roots]

        for audiobook_set in audiobook_sets:
            destination_root = self._destination_root(
                audiobook_set.source_path.resolve(strict=False),
                root,
                resolved_source_roots,
            )
            destination_folder = (
                destination_root
                / self.template_engine.generate_folder_name(
                    audiobook_set,
                    audiobook_set.chosen_identity,
                    template=folder_template,
                )
            )

            set_moved = False
            for track in audiobook_set.tracks:
                filename = self.template_engine.generate_filename(
                    track,
                    audiobook_set,
                    audiobook_set.chosen_identity,
                    template=file_template,
                    zero_padding_width=self.config.zero_padding_width,
                )
                new_path = destination_folder / filename
                if new_path.resolve(strict=False) == track.src_path.resolve(
                    strict=False
                ):
                    continue
                set_moved = True
                operations.append(
                    RenameOperation(
                        old_path=track.src_path,
                        new_path=new_path,
                        track=track,
                    )
                )

            if set_moved:
                operations.extend(
                    self._companion_operations(audiobook_set, destination_folder)
                )

        plan = RenamePlan(
            plan_id=self._plan_id(root, operations, profile_name),
            created_at=datetime.now(),
            source_path=root,
            profile_name=profile_name,
            operations=operations,
            audiobook_sets=audiobook_sets,
            dry_run=True,
        )
        plan.validate_plan()
        return plan

    def _destination_root(
        self, source_path: Path, default_root: Path, source_roots: list[Path]
    ) -> Path:
        """Pick the closest selected source root that contains the audiobook."""
        matching_roots = [
            root for root in source_roots if source_path.is_relative_to(root)
        ]
        if not matching_roots:
            return default_root
        return max(matching_roots, key=lambda path: len(path.parts))

    def _plan_id(
        self,
        library_root: Path,
        operations: list[RenameOperation],
        profile_name: str | None,
    ) -> str:
        """Build a stable plan identifier from the plan contents."""
        hasher = hashlib.sha256()
        hasher.update(str(library_root).encode("utf-8"))
        hasher.update((profile_name or "").encode("utf-8"))
        for operation in operations:
            hasher.update(str(operation.old_path).encode("utf-8"))
            hasher.update(str(operation.new_path).encode("utf-8"))
        return hasher.hexdigest()[:16]


def save_plan(plan: RenamePlan, path: Path) -> None:
    """Write a rename plan to disk as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(plan.model_dump(mode="json"), handle, indent=2)


def load_plan(path: Path) -> RenamePlan:
    """Load a rename plan from disk."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    plan = RenamePlan(**data)
    plan.validate_plan()
    return plan


def format_plan_summary(plan: RenamePlan, include_operations: bool = True) -> str:
    """Render a plan as human-readable text."""
    lines = [
        f"Plan ID: {plan.plan_id}",
        f"Created: {plan.created_at.isoformat()}",
        f"Source: {plan.source_path}",
        f"Profile: {plan.profile_name or 'default'}",
        (
            f"Books: {len(plan.audiobook_sets)} | "
            f"Operations: {len(plan.operations)} | "
            f"Conflicts: {len(plan.conflicts)} | "
            f"Warnings: {len(plan.warnings)}"
        ),
    ]

    if plan.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"  - {warning}" for warning in plan.warnings)

    if plan.conflicts:
        lines.append("")
        lines.append("Conflicts:")
        lines.extend(f"  - {conflict}" for conflict in plan.conflicts)

    if include_operations and plan.operations:
        lines.append("")
        lines.append("Operations:")
        lines.extend(
            f"  - {operation.old_path} -> {operation.new_path}"
            for operation in plan.operations
        )

    return "\n".join(lines)


def format_plan_diff(plan: RenamePlan) -> str:
    """Render a concise old->new diff for a plan."""
    if not plan.operations:
        return "No file changes are required."

    def relative_or_absolute(path: Path) -> str:
        try:
            return str(path.relative_to(plan.source_path))
        except ValueError:
            return str(path)

    return "\n".join(
        f"{relative_or_absolute(operation.old_path)} -> "
        f"{relative_or_absolute(operation.new_path)}"
        for operation in plan.operations
    )
