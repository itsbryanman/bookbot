"""Audiobook deduplication engine.

Provides two levels of deduplication:

1. **Edition-level**: Clusters AudiobookSets by normalized (author, title),
   with a fuzzy second pass, then scores each member to pick the keeper.
   Non-keepers are quarantined.

2. **File-level byte dedup**: Staged hashing (size -> partial hash -> full hash)
   to find identical files. Quarantines duplicates, preferring the copy inside
   the keeper edition.

Edition scoring priority (descending):
  1. Format rank: m4b > m4a > flac > opus > ogg > mp3 > aac > wav
  2. Has a matched identity with ISBN or ASIN
  3. Clean track order (validate_track_order returns no issues)
  4. Has cover art in folder
  5. Mean bitrate (higher wins)
  6. Total duration (longer wins — proxy for unabridged)
"""

import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rapidfuzz import fuzz

from ..config.manager import ConfigManager
from .health import COVER_NAMES, IMAGE_EXTENSIONS
from .matching import AdvancedMatcher
from .models import AudiobookSet, AudioFormat, OperationRecord
from .operations import TransactionManager

# Format rank: higher is better
FORMAT_RANK: dict[AudioFormat, int] = {
    AudioFormat.M4B: 8,
    AudioFormat.M4A: 7,
    AudioFormat.FLAC: 6,
    AudioFormat.OPUS: 5,
    AudioFormat.OGG: 4,
    AudioFormat.MP3: 3,
    AudioFormat.AAC: 2,
    AudioFormat.WAV: 1,
}


@dataclass
class DedupeCandidate:
    """An audiobook set annotated with keeper/quarantine status."""

    audiobook_set: AudiobookSet
    is_keeper: bool = False
    quarantine_reason: str = ""
    score_tuple: tuple[int, ...] = ()


@dataclass
class EditionGroup:
    """A group of audiobook sets considered duplicates of one edition."""

    members: list[DedupeCandidate] = field(default_factory=list)
    keeper: DedupeCandidate | None = None


@dataclass
class FileGroup:
    """A group of byte-identical files."""

    size: int
    paths: list[Path] = field(default_factory=list)
    keeper: Path | None = None


@dataclass
class QuarantineOp:
    """A single quarantine operation (move)."""

    source: Path
    destination: Path
    reason: str


@dataclass
class DedupePlan:
    """Complete plan for deduplication moves."""

    plan_id: str
    created_at: str
    library_root: str
    quarantine_root: str
    operations: list[QuarantineOp] = field(default_factory=list)
    edition_groups: list[dict] = field(default_factory=list)
    file_groups: list[dict] = field(default_factory=list)

    @property
    def total_reclaimable_bytes(self) -> int:
        total = 0
        for op in self.operations:
            try:
                total += op.source.stat().st_size
            except OSError:
                pass
        return total

    def has_conflicts(self) -> bool:
        """Check if any destination already exists."""
        for op in self.operations:
            if op.destination.exists():
                return True
        return False

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "library_root": self.library_root,
            "quarantine_root": self.quarantine_root,
            "operations": [
                {
                    "source": str(op.source),
                    "destination": str(op.destination),
                    "reason": op.reason,
                }
                for op in self.operations
            ],
            "edition_groups": self.edition_groups,
            "file_groups": self.file_groups,
        }

    def to_transaction_records(self) -> list[OperationRecord]:
        """Convert to OperationRecords for compatibility with undo machinery."""
        records = []
        for op in self.operations:
            records.append(
                OperationRecord(
                    operation_id=self.plan_id,
                    timestamp=datetime.fromisoformat(self.created_at),
                    operation_type="rename",
                    old_path=op.source,
                    new_path=op.destination,
                    metadata={
                        "transaction_type": "dedupe",
                        "quarantine_reason": op.reason,
                    },
                )
            )
        return records


class DedupeEngine:
    """Performs deduplication analysis and plan generation."""

    def __init__(self, library_root: Path) -> None:
        self.library_root = library_root
        self.matcher = AdvancedMatcher()

    def analyze_editions(
        self, audiobook_sets: list[AudiobookSet]
    ) -> list[EditionGroup]:
        """Find edition-duplicate groups among audiobook sets."""
        # Pass 1: exact key clustering
        clusters: dict[str, list[AudiobookSet]] = defaultdict(list)
        for ab_set in audiobook_sets:
            key = self._edition_key(ab_set)
            clusters[key].append(ab_set)

        # Pass 2: fuzzy-merge clusters
        cluster_keys = list(clusters.keys())
        merged_indices: set[int] = set()
        merged_clusters: list[list[AudiobookSet]] = []

        for i, key_i in enumerate(cluster_keys):
            if i in merged_indices:
                continue
            group = list(clusters[key_i])
            author_i, title_i = self._split_key(key_i)

            for j in range(i + 1, len(cluster_keys)):
                if j in merged_indices:
                    continue
                author_j, title_j = self._split_key(cluster_keys[j])

                title_sim = fuzz.token_set_ratio(title_i, title_j)
                author_sim = fuzz.token_sort_ratio(author_i, author_j) if (
                    author_i and author_j
                ) else 100

                if title_sim >= 92 and author_sim >= 90:
                    group.extend(clusters[cluster_keys[j]])
                    merged_indices.add(j)

            merged_indices.add(i)
            if len(group) > 1:
                merged_clusters.append(group)

        # Score and pick keepers
        edition_groups: list[EditionGroup] = []
        for members in merged_clusters:
            group = EditionGroup()
            candidates = [
                DedupeCandidate(audiobook_set=ab) for ab in members
            ]
            # Score each
            for c in candidates:
                c.score_tuple = self._edition_score(c.audiobook_set)

            # Sort by score descending
            candidates.sort(key=lambda c: c.score_tuple, reverse=True)

            # Keeper is the first
            candidates[0].is_keeper = True
            group.keeper = candidates[0]

            # Others are quarantined
            for c in candidates[1:]:
                c.quarantine_reason = self._explain_loss(
                    c.score_tuple, candidates[0].score_tuple
                )

            group.members = candidates
            edition_groups.append(group)

        return edition_groups

    def analyze_files(
        self, audio_extensions: set[str] | None = None,
    ) -> list[FileGroup]:
        """Find byte-duplicate files under library_root using staged hashing."""
        if audio_extensions is None:
            audio_extensions = {
                ".mp3", ".m4a", ".m4b", ".flac",
                ".ogg", ".opus", ".aac", ".wav",
            }

        # Stage 1: group by exact size
        size_groups: dict[int, list[Path]] = defaultdict(list)
        for f in self.library_root.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix.lower() not in audio_extensions:
                continue
            if f.name.startswith("."):
                continue
            try:
                size = f.stat().st_size
                size_groups[size].append(f)
            except OSError:
                continue

        # Discard singletons
        candidates = {
            sz: paths for sz, paths in size_groups.items() if len(paths) > 1
        }

        if not candidates:
            return []

        # Stage 2: partial hash (first 64K + last 64K)
        partial_groups: dict[str, list[Path]] = defaultdict(list)
        for sz, paths in candidates.items():
            for p in paths:
                ph = self._partial_hash(p)
                if ph:
                    partial_groups[f"{sz}:{ph}"].append(p)

        # Discard diverged
        stage2 = {
            key: paths for key, paths in partial_groups.items() if len(paths) > 1
        }

        if not stage2:
            return []

        # Stage 3: full hash only on stage-2 collisions
        full_groups: dict[str, list[Path]] = defaultdict(list)
        for _key, paths in stage2.items():
            for p in paths:
                fh = self._full_hash(p)
                if fh:
                    full_groups[fh].append(p)

        # Build FileGroups
        result: list[FileGroup] = []
        for _fhash, paths in full_groups.items():
            if len(paths) > 1:
                fg = FileGroup(
                    size=paths[0].stat().st_size,
                    paths=sorted(paths),
                )
                result.append(fg)

        return result

    def build_plan(
        self,
        edition_groups: list[EditionGroup] | None = None,
        file_groups: list[FileGroup] | None = None,
        keeper_edition_paths: set[Path] | None = None,
    ) -> DedupePlan:
        """Build a quarantine plan from analysis results."""
        plan_id = str(uuid.uuid4())
        quarantine_root = self.library_root / ".bookbot-quarantine" / plan_id

        plan = DedupePlan(
            plan_id=plan_id,
            created_at=datetime.now().isoformat(),
            library_root=str(self.library_root),
            quarantine_root=str(quarantine_root),
        )

        if keeper_edition_paths is None:
            keeper_edition_paths = set()

        # Edition quarantines
        if edition_groups:
            for group in edition_groups:
                group_info: dict = {"keeper": None, "quarantined": []}
                for candidate in group.members:
                    ab = candidate.audiobook_set
                    if candidate.is_keeper:
                        group_info["keeper"] = str(ab.source_path)
                        keeper_edition_paths.add(ab.source_path)
                        continue

                    # Move the entire folder
                    for track in ab.tracks:
                        src = track.src_path
                        track_rel = src.relative_to(self.library_root)
                        track_dest = quarantine_root / track_rel
                        plan.operations.append(
                            QuarantineOp(
                                source=src,
                                destination=track_dest,
                                reason=(
                                    "Edition duplicate: "
                                    f"{candidate.quarantine_reason}"
                                ),
                            )
                        )
                    group_info["quarantined"].append({
                        "path": str(ab.source_path),
                        "reason": candidate.quarantine_reason,
                    })

                plan.edition_groups.append(group_info)

        # File-level quarantines
        if file_groups:
            for fg in file_groups:
                keeper = self._pick_file_keeper(
                    fg.paths, keeper_edition_paths
                )
                fg.keeper = keeper

                fg_info: dict = {
                    "keeper": str(keeper),
                    "size": fg.size,
                    "quarantined": [],
                }

                for p in fg.paths:
                    if p == keeper:
                        continue
                    rel = p.relative_to(self.library_root)
                    dest = quarantine_root / rel
                    plan.operations.append(
                        QuarantineOp(
                            source=p,
                            destination=dest,
                            reason="Byte-identical duplicate",
                        )
                    )
                    fg_info["quarantined"].append(str(p))

                plan.file_groups.append(fg_info)

        return plan

    def execute_plan(self, plan: DedupePlan, config_manager: ConfigManager) -> None:
        """Execute a quarantine plan (move files)."""
        if plan.has_conflicts():
            raise ValueError(
                "Plan has conflicts — destination files already exist"
            )

        for op in plan.operations:
            op.destination.parent.mkdir(parents=True, exist_ok=True)
            op.source.rename(op.destination)

        # Save the authoritative transaction log where undo/history expect it.
        self._save_transaction_log(plan, config_manager)

    def _save_transaction_log(
        self, plan: DedupePlan, config_manager: ConfigManager
    ) -> None:
        """Save a standard transaction log plus a provenance copy in quarantine."""
        log_file = (
            self.library_root
            / ".bookbot-quarantine"
            / plan.plan_id
            / f"transaction_{plan.plan_id}.json"
        )
        records = plan.to_transaction_records()
        manager = TransactionManager(config_manager)
        manager.record_transaction(
            plan.plan_id,
            records,
            transaction_type="dedupe",
            timestamp=plan.created_at,
            copy_to=[log_file],
        )

    # ── Scoring helpers ──

    def _edition_key(self, ab_set: AudiobookSet) -> str:
        author = self.matcher.normalize_author(ab_set.author_guess or "")
        # Strip dots for consistent keying (e.g. "J.R.R." → "JRR")
        author = author.replace(".", "")
        title = self.matcher.normalize_title(ab_set.raw_title_guess or "")
        return f"{author}||{title}"

    @staticmethod
    def _split_key(key: str) -> tuple[str, str]:
        parts = key.split("||", 1)
        return (parts[0], parts[1] if len(parts) > 1 else "")

    def _edition_score(self, ab_set: AudiobookSet) -> tuple[int, ...]:
        """Return a comparable tuple for keeper selection (higher = better)."""
        # 1. Format rank (best format among tracks)
        format_rank = max(
            (FORMAT_RANK.get(t.audio_format, 0) for t in ab_set.tracks),
            default=0,
        )

        # 2. Has identity with ISBN or ASIN
        has_id = 0
        if ab_set.chosen_identity:
            if (
                ab_set.chosen_identity.isbn_10
                or ab_set.chosen_identity.isbn_13
                or ab_set.chosen_identity.asin
            ):
                has_id = 1

        # 3. Clean track order
        issues = ab_set.validate_track_order()
        clean_order = 1 if not issues else 0

        # 4. Has cover art
        has_cover = 0
        if ab_set.source_path.is_dir():
            for f in ab_set.source_path.iterdir():
                if (
                    f.suffix.lower() in IMAGE_EXTENSIONS
                    and f.stem.lower() in COVER_NAMES
                ):
                    has_cover = 1
                    break

        # 5. Mean bitrate
        bitrates = [t.bitrate for t in ab_set.tracks if t.bitrate]
        mean_bitrate = int(sum(bitrates) / len(bitrates)) if bitrates else 0

        # 6. Total duration
        total_duration = int(ab_set.total_duration or 0)

        return (
            format_rank, has_id, clean_order,
            has_cover, mean_bitrate, total_duration,
        )

    @staticmethod
    def _explain_loss(
        loser_score: tuple[int, ...], winner_score: tuple[int, ...]
    ) -> str:
        """Explain which criterion decided the loss."""
        criteria = [
            "lower format rank",
            "no ISBN/ASIN",
            "track order issues",
            "no cover art",
            "lower bitrate",
            "shorter duration",
        ]
        for i, (w, lo) in enumerate(
            zip(winner_score, loser_score, strict=False)
        ):
            if w > lo:
                return criteria[i] if i < len(criteria) else "lower score"
        return "tie-broken by position"

    def _pick_file_keeper(
        self, paths: list[Path], keeper_edition_paths: set[Path]
    ) -> Path:
        """Pick the keeper file from byte-identical duplicates."""
        # Prefer the copy inside a keeper edition
        for p in paths:
            for kep in keeper_edition_paths:
                try:
                    p.relative_to(kep)
                    return p
                except ValueError:
                    continue

        # Shortest path
        paths_sorted = sorted(paths, key=lambda p: (len(str(p)), str(p)))
        return paths_sorted[0]

    # ── Hashing helpers ──

    @staticmethod
    def _partial_hash(file_path: Path) -> str | None:
        """Hash first 64K + last 64K of a file."""
        try:
            size = file_path.stat().st_size
            hasher = hashlib.sha256()
            with open(file_path, "rb") as f:
                # First 64K
                hasher.update(f.read(65536))
                # Last 64K
                if size > 65536:
                    f.seek(max(0, size - 65536))
                    hasher.update(f.read(65536))
            return hasher.hexdigest()
        except OSError:
            return None

    @staticmethod
    def _full_hash(file_path: Path) -> str | None:
        """Full SHA-256 hash of a file."""
        try:
            hasher = hashlib.sha256()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except OSError:
            return None
