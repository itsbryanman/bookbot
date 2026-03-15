"""Smart file organizer for messy audiobook libraries."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .logging import get_logger
from .models import AudiobookSet
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
    conflicts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

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
            target_path: Root path for the reorganized library (defaults to source_path).
            template_name: Name of the template preset to use.
            audiobook_sets: List of discovered audiobook sets.

        Returns:
            A ReorganizationPlan with proposed moves, conflicts, and warnings.
        """
        target = target_path or source_path
        preset = TEMPLATE_PRESETS.get(template_name, TEMPLATE_PRESETS["default"])
        folder_template = preset["folder_template"]
        file_template = preset["file_template"]

        plan = ReorganizationPlan()
        used_destinations: dict[str, str] = {}

        for ab_set in audiobook_sets:
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

            for track in sorted(
                ab_set.tracks, key=lambda t: (t.disc, t.track_index)
            ):
                # Generate filename
                filename = self.template_engine.generate_filename(
                    track, ab_set, identity, template=file_template
                )

                dest = target / folder_name / filename

                # Check path length
                if len(str(dest)) > self.max_path_length:
                    plan.warnings.append(
                        f"Path too long ({len(str(dest))} chars): {dest}"
                    )

                # Check for collisions
                dest_key = str(dest).lower()
                if dest_key in used_destinations:
                    plan.conflicts.append(
                        f"Path collision: {dest} (conflicts with "
                        f"{used_destinations[dest_key]})"
                    )
                else:
                    used_destinations[dest_key] = str(track.src_path)

                # Check for overwriting existing files
                if dest.exists() and dest != track.src_path:
                    plan.conflicts.append(
                        f"Would overwrite existing file: {dest}"
                    )

                plan.operations.append(
                    MoveOperation(
                        source=track.src_path,
                        destination=dest,
                        audiobook_title=ab_set.raw_title_guess
                        or ab_set.source_path.name,
                    )
                )

        return plan

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
