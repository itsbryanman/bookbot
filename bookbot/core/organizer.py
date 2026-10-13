"""Smart file organizer for messy audiobook libraries."""

from pathlib import Path

from pydantic import BaseModel, Field

from ..config.models import Config
from .logging import get_logger
from .models import AudiobookSet, ProviderIdentity
from .planning import (
    PlanBuilder,
    collision_skip_order,
    duplicate_reason,
    self_collision_reason,
    set_is_planned,
    unplanned_reason,
)
from .templates import TemplateEngine

logger = get_logger("organizer")


class MoveOperation(BaseModel):
    """A single file move operation."""

    source: Path
    destination: Path
    audiobook_title: str = ""


class ReorganizationPlan(BaseModel):
    """Complete plan for reorganizing a library."""

    operations: list[MoveOperation] = Field(default_factory=list)
    source_root: Path | None = None
    conflicts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Books left in place, as {"source_path", "reason"}: duplicates whose
    # destination another book already claims, and books awaiting review.
    skipped: list[dict[str, str]] = Field(default_factory=list)

    @property
    def total_moves(self) -> int:
        return len(self.operations)

    @property
    def is_valid(self) -> bool:
        return len(self.conflicts) == 0


# Template presets
TEMPLATE_PRESETS: dict[str, dict[str, str]] = {
    "default": {
        "folder_template": "{AuthorLastFirst}/{Title} ({Year})",
        "file_template": "{DiscPad}{TrackPad} - {Title}",
    },
    "abs": {
        "folder_template": "{Author}/{Title}",
        "file_template": "{Author} - {Title}",
    },
    "plex": {
        "folder_template": "{AuthorLastFirst}/{SeriesName}/{SeriesIndex} - {Title}",
        "file_template": "{DiscPad}{TrackPad} - {Title}",
    },
}


class SmartOrganizer:
    """Proposes and executes library reorganization."""

    def __init__(self, max_path_length: int = 255) -> None:
        self.max_path_length = max_path_length
        self.template_engine = TemplateEngine(max_path_length=max_path_length)
        # Reused so covers/.opf/.nfo travel with their book like `apply` does.
        self._plan_builder = PlanBuilder(Config())

    def propose_reorganization(
        self,
        source_path: Path,
        target_path: Path | None,
        template_name: str,
        audiobook_sets: list[AudiobookSet],
    ) -> ReorganizationPlan:
        """Propose a reorganization plan for the given audiobook sets.

        Args:
            source_path: Root path of the current library.
            target_path: Root path for the reorganized library
                (defaults to source_path).
            template_name: Name of the template preset to use.
            audiobook_sets: List of discovered audiobook sets.

        Returns:
            A ReorganizationPlan with proposed moves, conflicts, and warnings.
        """
        target = target_path or source_path
        preset = TEMPLATE_PRESETS.get(template_name, TEMPLATE_PRESETS["default"])
        folder_template = preset["folder_template"]
        file_template = preset["file_template"]

        plan = ReorganizationPlan(source_root=source_path)
        used_destinations: dict[str, str] = {}
        claimed_by: dict[str, AudiobookSet] = {}
        planned: dict[int, list[MoveOperation]] = {}

        for set_index in collision_skip_order(audiobook_sets):
            ab_set = audiobook_sets[set_index]
            if not set_is_planned(ab_set):
                plan.skipped.append(
                    {
                        "source_path": str(ab_set.source_path),
                        "reason": unplanned_reason(ab_set),
                    }
                )
                continue
            identity = ab_set.chosen_identity

            # Generate folder name
            folder_name = self.template_engine.generate_folder_name(
                ab_set, identity, template=folder_template
            )

            if not folder_name or folder_name.strip() == "":
                plan.warnings.append(
                    f"Could not generate folder name for {ab_set.source_path.name}"
                )
                continue

            moves = self._track_moves(
                ab_set, target / folder_name, identity, file_template
            )
            track_keys = [str(dest).lower() for _, dest in moves]
            if len(set(track_keys)) != len(track_keys):
                # Presets such as "abs" name files "{Author} - {Title}",
                # which only works for single-file books; number the tracks
                # of a multi-file book instead of colliding.
                moves = self._track_moves(
                    ab_set,
                    target / folder_name,
                    identity,
                    PlanBuilder.TRACK_SAFE_FILE_TEMPLATE,
                )
            moves.extend(
                (companion.old_path, companion.new_path)
                for companion in self._plan_builder._companion_operations(
                    ab_set, target / folder_name
                )
            )

            # A book whose destinations collide with a book already planned
            # (an exact duplicate such as "gone girl - Copy"), or with its
            # own tracks, is left in place instead of blocking every move.
            dest_keys = [str(dest).lower() for _, dest in moves]
            own_duplicates = [key for key in dest_keys if dest_keys.count(key) > 1]
            if own_duplicates:
                reason = self_collision_reason(Path(own_duplicates[0]))
            elif taken := [key for key in dest_keys if key in claimed_by]:
                reason = duplicate_reason(claimed_by[taken[0]], Path(taken[0]).parent)
            else:
                reason = None
            if reason is not None:
                ab_set.needs_review = True
                ab_set.review_reason = reason
                plan.skipped.append(
                    {"source_path": str(ab_set.source_path), "reason": reason}
                )
                continue

            set_operations = planned.setdefault(set_index, [])
            for src_path, dest in moves:
                # Check path length
                if len(str(dest)) > self.max_path_length:
                    plan.warnings.append(
                        f"Path too long ({len(str(dest))} chars): {dest}"
                    )

                dest_key = str(dest).lower()
                used_destinations[dest_key] = str(src_path)
                claimed_by[dest_key] = ab_set

                # Check for overwriting existing files
                if dest.exists() and dest != src_path:
                    plan.conflicts.append(f"Would overwrite existing file: {dest}")

                set_operations.append(
                    MoveOperation(
                        source=src_path,
                        destination=dest,
                        audiobook_title=ab_set.raw_title_guess
                        or ab_set.source_path.name,
                    )
                )

        # Keep the scan order for the moves themselves.
        for set_index in sorted(planned):
            plan.operations.extend(planned[set_index])
        return plan

    def _track_moves(
        self,
        ab_set: AudiobookSet,
        folder: Path,
        identity: ProviderIdentity | None,
        file_template: str,
    ) -> list[tuple[Path, Path]]:
        return [
            (
                track.src_path,
                folder
                / self.template_engine.generate_filename(
                    track, ab_set, identity, template=file_template
                ),
            )
            for track in sorted(ab_set.tracks, key=lambda t: (t.disc, t.track_index))
        ]

    def execute_plan(self, plan: ReorganizationPlan, dry_run: bool = True) -> bool:
        """Execute a reorganization plan.

        Uses a two-phase approach: first move all to temp names,
        then move to final destinations.
        """
        if not plan.is_valid:
            logger.warning("Cannot execute plan with conflicts")
            return False

        if dry_run:
            return True

        completed: list[tuple[Path, Path]] = []

        try:
            for op in plan.operations:
                op.destination.parent.mkdir(parents=True, exist_ok=True)

                if op.source == op.destination:
                    continue

                op.source.rename(op.destination)
                completed.append((op.destination, op.source))

            self._prune_empty_source_dirs(plan, completed)
            return True

        except OSError as e:
            logger.error(f"Reorganization failed: {e}, rolling back")
            # Rollback
            for dest, orig in reversed(completed):
                try:
                    if dest.exists():
                        orig.parent.mkdir(parents=True, exist_ok=True)
                        dest.rename(orig)
                except OSError:
                    pass
            return False

    @staticmethod
    def _prune_empty_source_dirs(
        plan: ReorganizationPlan, completed: list[tuple[Path, Path]]
    ) -> None:
        """Remove source folders emptied by the move, never the library root."""
        root = plan.source_root.resolve() if plan.source_root else None
        for _dest, orig in completed:
            folder = orig.parent
            while root is not None and folder.resolve() != root:
                if root not in folder.resolve().parents:
                    break
                try:
                    folder.rmdir()
                except OSError:
                    break
                folder = folder.parent
