"""Rename plan creation, serialization, and review helpers."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from ..config.models import Config
from ..io.sidecar import sidecar_output_paths
from .discovery import is_ignored_file
from .models import AudiobookSet, RenameOperation, RenamePlan
from .templates import TemplateEngine

# Folder names that mark a set as the copy of another one:
# "gone girl - Copy", "Dune copy 2", "Dune (1)".
_COPY_NAME_RE = re.compile(
    r"(?:[\s_-]+copy(?:[\s_-]*\(?\d+\)?)?|\s*\(\d+\))$", re.IGNORECASE
)


def set_is_planned(audiobook_set: AudiobookSet) -> bool:
    """Whether a set may be moved, or must wait for review.

    Shared by rename plans and ``organize`` so both leave the same books
    alone: sets a reviewer skipped, and sets flagged for review that have no
    chosen metadata yet.
    """
    if audiobook_set.review_skipped:
        return False
    return not (
        audiobook_set.chosen_identity is None
        and (
            audiobook_set.needs_review
            or bool(audiobook_set.provider_candidates)
            or audiobook_set.review_reason is not None
        )
    )


def unplanned_reason(audiobook_set: AudiobookSet) -> str:
    """Why :func:`set_is_planned` left a set out."""
    if audiobook_set.review_skipped:
        return audiobook_set.review_reason or "Skipped by reviewer"
    return audiobook_set.review_reason or "No reviewed metadata match selected"


def collision_skip_order(audiobook_sets: list[AudiobookSet]) -> list[int]:
    """Order in which colliding sets claim a destination.

    Sets whose folder looks like a copy ("gone girl - Copy") yield to the
    original; otherwise the scan order decides.
    """
    return sorted(
        range(len(audiobook_sets)),
        key=lambda index: (
            bool(_COPY_NAME_RE.search(audiobook_sets[index].source_path.name.strip())),
            index,
        ),
    )


def duplicate_reason(other: AudiobookSet, destination: Path) -> str:
    return (
        f"Duplicate of '{other.source_path.name}': both would be written to the "
        f"same destination ({destination}). Left in place; delete or merge the "
        "copy, then rescan."
    )


def self_collision_reason(destination: Path) -> str:
    return (
        "Tracks of this book would be written to the same destination "
        f"({destination}): track numbers are missing or repeated. Left in "
        "place; fix the track order, then rescan."
    )


# Index of the owning audiobook set for each operation and planned sidecar.
_Owners = tuple[list[int], list[int]]


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

            entries = [entry for entry in entries if not is_ignored_file(entry)]
            folder_audio = [
                entry
                for entry in entries
                if entry.is_file() and entry.suffix.lower() in self._AUDIO_EXTENSIONS
            ]
            if any(
                entry.resolve(strict=False) not in track_paths for entry in folder_audio
            ):
                # Folder is shared with audio outside this set; don't claim
                # its sidecars.
                continue

            for entry in entries:
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in self.SIDECAR_EXTENSIONS:
                    continue

                target_name = entry.name
                if target_name.lower() in claimed_names:
                    # Multi-disc books routinely carry one cover.jpg per disc
                    # folder. Skipping the collision used to strand those
                    # files, leaving the old source tree half-populated
                    # alongside the new one. Disambiguate with the source
                    # folder name so every companion moves and the source
                    # folder can be pruned.
                    disambiguated = self._disambiguated_name(
                        entry, folder, claimed_names
                    )
                    if disambiguated is None:
                        continue
                    target_name = disambiguated

                new_path = destination_folder / target_name
                claimed_names.add(target_name.lower())
                if new_path.resolve(strict=False) == entry.resolve(strict=False):
                    continue
                operations.append(RenameOperation(old_path=entry, new_path=new_path))

        return operations

    @staticmethod
    def _disambiguated_name(
        entry: Path, folder: Path, claimed_names: set[str]
    ) -> str | None:
        """Build a collision-free companion filename from its source folder."""
        candidates = [f"{folder.name} - {entry.name}"]
        stem = entry.stem
        suffix = entry.suffix
        candidates.extend(f"{stem} ({index}){suffix}" for index in range(2, 100))
        for candidate in candidates:
            if candidate.lower() not in claimed_names:
                return candidate
        return None

    def __init__(self, config: Config):
        self.config = config
        self.template_engine = TemplateEngine(
            case_policy=config.case_policy,
            unicode_normalize=config.unicode_normalization,
            max_path_length=config.max_path_length,
        )

    def _planned_sidecar_paths(
        self,
        audiobook_set: AudiobookSet,
        destination_folder: Path,
    ) -> list[Path]:
        """Return the sidecar files the apply step should create."""
        if audiobook_set.chosen_identity is None:
            return []
        return list(
            sidecar_output_paths(
                destination_folder,
                audiobook_set.chosen_identity,
                self.config.output,
            ).values()
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

        plan, owners = self._build_plan(
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
            fallback_plan, fallback_owners = self._build_plan(
                library_root=library_root,
                audiobook_sets=audiobook_sets,
                profile_name=profile_name,
                folder_template=folder_template,
                file_template=self.TRACK_SAFE_FILE_TEMPLATE,
                source_roots=source_roots,
            )
            # Accept the fallback whenever it resolves collisions; one genuinely
            # duplicated book must not leave every other book colliding.
            fewer_collisions = self._colliding_operation_count(
                fallback_plan
            ) < self._colliding_operation_count(plan)
            if fewer_collisions and len(fallback_plan.conflicts) <= len(plan.conflicts):
                fallback_plan.warnings.append(
                    "Profile prefers single-file outputs; the rename plan kept unique "
                    "per-track names until conversion happens."
                )
                plan = fallback_plan
                owners = fallback_owners

        return self._skip_colliding_sets(plan, owners)

    def _skip_colliding_sets(self, plan: RenamePlan, owners: _Owners) -> RenamePlan:
        """Leave out sets whose moves collide instead of blocking the plan.

        One duplicated book ("gone girl - Copy") used to put "Duplicate
        target path" conflicts into the plan, which makes the whole plan
        unappliable. The colliding set is now moved to ``plan.skipped``
        (and flagged for review) with the reason, the original keeps its
        moves, and every other book stays applicable. A set whose own
        tracks collide is skipped the same way. Conflicts of other kinds
        (missing sources, illegal names, paths escaping the root) are left
        untouched.
        """
        operation_owners, sidecar_owners = owners
        claims: dict[Path, list[int]] = {}
        for index, operation in enumerate(plan.operations):
            key = operation.new_path.resolve(strict=False)
            claims.setdefault(key, []).append(index)
        collisions = {key: ops for key, ops in claims.items() if len(ops) > 1}
        if not collisions:
            return plan

        sets = plan.audiobook_sets
        set_targets: dict[int, list[Path]] = {}
        for key, op_indexes in collisions.items():
            for op_index in op_indexes:
                set_targets.setdefault(operation_owners[op_index], []).append(key)

        skipped_sets: dict[int, str] = {}
        claimed_by: dict[Path, int] = {}
        for set_index in collision_skip_order(sets):
            targets = set_targets.get(set_index)
            if not targets:
                continue
            own_duplicates = [key for key in targets if targets.count(key) > 1]
            if own_duplicates:
                skipped_sets[set_index] = self_collision_reason(own_duplicates[0])
                continue
            taken = [key for key in targets if key in claimed_by]
            if taken:
                other = sets[claimed_by[taken[0]]]
                skipped_sets[set_index] = duplicate_reason(other, taken[0].parent)
                continue
            for key in targets:
                claimed_by[key] = set_index

        operations = [
            operation
            for index, operation in enumerate(plan.operations)
            if operation_owners[index] not in skipped_sets
        ]
        planned_sidecars = [
            path
            for index, path in enumerate(plan.planned_sidecars)
            if sidecar_owners[index] not in skipped_sets
        ]
        for set_index, reason in sorted(skipped_sets.items()):
            audiobook_set = sets[set_index]
            audiobook_set.needs_review = True
            audiobook_set.review_reason = reason
            plan.skipped.append(
                {"source_path": str(audiobook_set.source_path), "reason": reason}
            )

        plan.operations = operations
        plan.planned_sidecars = planned_sidecars
        plan.plan_id = self._plan_id(plan.source_path, operations, plan.profile_name)
        plan.validate_plan()
        return plan

    @staticmethod
    def _colliding_operation_count(plan: RenamePlan) -> int:
        """Count operations whose target is shared with another operation."""
        targets = [operation.new_path for operation in plan.operations]
        return sum(1 for target in targets if targets.count(target) > 1)

    def _build_plan(
        self,
        library_root: Path,
        audiobook_sets: list[AudiobookSet],
        profile_name: str | None,
        folder_template: str,
        file_template: str,
        source_roots: list[Path],
    ) -> tuple[RenamePlan, _Owners]:
        """Build a plan, and record which set each operation/sidecar is for."""
        operations: list[RenameOperation] = []
        operation_owners: list[int] = []
        planned_sidecars: list[Path] = []
        sidecar_owners: list[int] = []
        root = library_root.resolve(strict=False)
        resolved_source_roots = [path.resolve(strict=False) for path in source_roots]
        skipped: list[dict[str, str]] = []

        for set_index, audiobook_set in enumerate(audiobook_sets):
            if not set_is_planned(audiobook_set):
                skipped.append(
                    {
                        "source_path": str(audiobook_set.source_path),
                        "reason": unplanned_reason(audiobook_set),
                    }
                )
                continue

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
                operation_owners.append(set_index)

            if set_moved:
                companions = self._companion_operations(
                    audiobook_set, destination_folder
                )
                operations.extend(companions)
                operation_owners.extend([set_index] * len(companions))

            sidecars = self._planned_sidecar_paths(audiobook_set, destination_folder)
            planned_sidecars.extend(sidecars)
            sidecar_owners.extend([set_index] * len(sidecars))

        plan = RenamePlan(
            plan_id=self._plan_id(root, operations, profile_name),
            created_at=datetime.now(),
            source_path=root,
            profile_name=profile_name,
            operations=operations,
            audiobook_sets=audiobook_sets,
            dry_run=True,
            skipped=skipped,
            planned_sidecars=planned_sidecars,
        )
        plan.validate_plan()
        return plan, (operation_owners, sidecar_owners)

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
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"{path} is not a valid BookBot plan file (invalid JSON: {e}). "
            "Plans are created with 'bookbot scan <folder> --plan <file>'."
        ) from e
    try:
        plan = RenamePlan(**data)
    except Exception as e:
        raise ValueError(f"{path} is not a valid BookBot plan file: {e}") from e
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
            f"Sidecars: {len(plan.planned_sidecars)} | "
            f"Skipped: {len(plan.skipped)} | "
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

    if plan.skipped:
        lines.append("")
        lines.append("Skipped:")
        lines.extend(
            f"  - {item['source_path']}: {item['reason']}" for item in plan.skipped
        )

    if plan.planned_sidecars:
        lines.append("")
        lines.append("Sidecar Writes:")
        lines.extend(f"  - {path}" for path in plan.planned_sidecars)

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
