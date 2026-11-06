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
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rapidfuzz import fuzz

from ..config.manager import ConfigManager
from .discovery import (
    AudioFileScanner,
    is_within_quarantine_tree,
    iter_files_excluding_quarantine,
)
from .health import AUDIO_EXTENSIONS, COVER_NAMES, IMAGE_EXTENSIONS, SIDECAR_EXTENSIONS
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
SEGMENT_SUFFIX_PATTERN = re.compile(
    r"^(?P<base>.+?)(?:[\s\-_]+(?:disc|cd|disk|part|pt))?[\s\-_]*(?P<num>\d+)$",
    re.IGNORECASE,
)


_EDITION_BRACKETS_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_EDITION_WORDS_RE = re.compile(
    r"\b(?:unabridged|abridged|audiobook|audio\s*book|retail)\b", re.IGNORECASE
)
_ARTICLES = {"the", "a", "an"}


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
class PossibleDuplicate:
    """Sets that look alike but lack evidence; listed for review only."""

    members: list[AudiobookSet] = field(default_factory=list)
    reason: str = ""


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
    # Never quarantined: {"paths": [...], "reason": "..."}
    possible_duplicates: list[dict] = field(default_factory=list)

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

    def validate_sources_outside_quarantine(self) -> None:
        """Reject plans that try to move files already inside quarantine."""
        invalid_sources = [
            op.source for op in self.operations if is_within_quarantine_tree(op.source)
        ]
        if invalid_sources:
            sample = ", ".join(str(path) for path in invalid_sources[:3])
            raise ValueError(
                f"Plan includes source path(s) inside .bookbot-quarantine: {sample}"
            )

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
            "possible_duplicates": self.possible_duplicates,
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

    # Fuzzy thresholds. token_sort_ratio compares every token, so extra
    # words lower the score: "dune" vs "dune messiah" is 50 (token_set_ratio
    # gave 100 because one token set contains the other).
    TITLE_THRESHOLD = 92
    AUTHOR_THRESHOLD = 90

    def __init__(self, library_root: Path, duration_tolerance_pct: float = 2.0) -> None:
        self.library_root = library_root
        self.matcher = AdvancedMatcher()
        self.scanner = AudioFileScanner()
        self.analysis_warnings: list[str] = []
        # Fuzzy-only matches need a shared ASIN/ISBN or durations within
        # this many percent before anything is quarantined.
        self.duration_tolerance_pct = duration_tolerance_pct
        self.possible_duplicates: list[PossibleDuplicate] = []

    def analyze_editions(
        self, audiobook_sets: list[AudiobookSet]
    ) -> list[EditionGroup]:
        """Find edition-duplicate groups among audiobook sets."""
        self.analysis_warnings = []

        # Pass 1: exact key clustering
        clusters: dict[str, list[AudiobookSet]] = defaultdict(list)
        for ab_set in audiobook_sets:
            key = self._edition_key(ab_set)
            clusters[key].append(ab_set)

        # Pass 2: fuzzy-merge clusters. Only an exact normalized
        # author+title key with an author on both sides is trusted on its
        # own; everything else needs corroboration (same ASIN/ISBN, or
        # durations within duration_tolerance_pct) and otherwise is listed
        # as a possible duplicate for review, never quarantined.
        self.possible_duplicates = []
        cluster_keys = list(clusters.keys())
        merged_indices: set[int] = set()
        merged_clusters: list[list[AudiobookSet]] = []

        for i, key_i in enumerate(cluster_keys):
            if i in merged_indices:
                continue
            author_i, title_i = self._split_key(key_i)
            group = self._corroborated_authorless(clusters[key_i], author_i)

            for j in range(i + 1, len(cluster_keys)):
                if j in merged_indices:
                    continue
                author_j, title_j = self._split_key(cluster_keys[j])
                other = clusters[cluster_keys[j]]

                if fuzz.token_sort_ratio(title_i, title_j) < self.TITLE_THRESHOLD:
                    continue
                if not (author_i and author_j):
                    self._add_possible(
                        group + other,
                        "Titles match but the author is missing on one side; "
                        "review before removing anything",
                    )
                    continue
                if fuzz.token_sort_ratio(author_i, author_j) < self.AUTHOR_THRESHOLD:
                    continue
                if key_i != cluster_keys[j] and not self._corroborated(group, other):
                    self._add_possible(
                        group + other,
                        "Similar title/author, but no shared ASIN/ISBN and "
                        f"durations differ by more than "
                        f"{self.duration_tolerance_pct:g}%; review before "
                        "removing anything",
                    )
                    continue
                group.extend(other)
                merged_indices.add(j)

            merged_indices.add(i)
            if len(group) > 1:
                merged_clusters.append(group)

        # Score and pick keepers
        edition_groups: list[EditionGroup] = []
        for members in merged_clusters:
            guard_warning = self._segment_group_warning(members)
            if guard_warning is not None:
                self.analysis_warnings.append(guard_warning)
                continue

            edition_group = EditionGroup()
            candidates = [DedupeCandidate(audiobook_set=ab) for ab in members]
            # Score each
            for c in candidates:
                c.score_tuple = self._edition_score(c.audiobook_set)

            # Sort by score descending
            candidates.sort(key=lambda c: c.score_tuple, reverse=True)

            # Keeper is the first
            candidates[0].is_keeper = True
            edition_group.keeper = candidates[0]

            # Others are quarantined
            for c in candidates[1:]:
                c.quarantine_reason = self._explain_loss(
                    c.score_tuple, candidates[0].score_tuple
                )

            edition_group.members = candidates
            edition_groups.append(edition_group)

        return edition_groups

    def _corroborated_authorless(
        self, members: list[AudiobookSet], author: str
    ) -> list[AudiobookSet]:
        """Members of an exact-key cluster that may be grouped.

        With an author the exact key is trusted. Without one, each member
        must corroborate the first; the rest become possible duplicates.
        """
        if author or len(members) < 2:
            return list(members)
        group = [members[0]]
        for member in members[1:]:
            if self._corroborated(group, [member]):
                group.append(member)
            else:
                self._add_possible(
                    [members[0], member],
                    "Same title but no author, no shared ASIN/ISBN and "
                    f"durations differ by more than "
                    f"{self.duration_tolerance_pct:g}%; review before removing "
                    "anything",
                )
        return group

    def _add_possible(self, members: list[AudiobookSet], reason: str) -> None:
        self.possible_duplicates.append(
            PossibleDuplicate(members=list(members), reason=reason)
        )

    @staticmethod
    def _identifiers(ab_set: AudiobookSet) -> set[str]:
        values = {ab_set.asin_guess, ab_set.isbn_guess}
        identity = ab_set.chosen_identity
        if identity is not None:
            values |= {identity.asin, identity.isbn_13, identity.isbn_10}
        return {
            "".join(ch for ch in value if ch.isalnum()).upper()
            for value in values
            if value
        }

    def _durations_agree(self, left: AudiobookSet, right: AudiobookSet) -> bool:
        if not left.total_duration or not right.total_duration:
            return False
        longest = max(left.total_duration, right.total_duration)
        delta = abs(left.total_duration - right.total_duration)
        return delta / longest * 100.0 <= self.duration_tolerance_pct

    def _corroborated(
        self, left: list[AudiobookSet], right: list[AudiobookSet]
    ) -> bool:
        """Whether any pair across the two lists shares an id or a duration."""
        for a in left:
            for b in right:
                if self._identifiers(a) & self._identifiers(b):
                    return True
                if self._durations_agree(a, b):
                    return True
        return False

    def _segment_group_warning(self, members: list[AudiobookSet]) -> str | None:
        """Return a warning when a cluster looks like one segmented work."""
        if self._looks_like_numbered_sibling_segments(members):
            title = members[0].raw_title_guess or members[0].source_path.name
            return (
                f"Skipped potential duplicate edition group for '{title}' because "
                "the members look like segmented discs of one work. The library "
                "may have uncollapsed disc folders."
            )

        if self._looks_like_partitioned_sibling_durations(members):
            title = members[0].raw_title_guess or members[0].source_path.name
            return (
                f"Skipped potential duplicate edition group for '{title}' because "
                "the sibling durations look like partitioned segments of one work. "
                "The library may have uncollapsed disc folders."
            )

        return None

    def _looks_like_numbered_sibling_segments(
        self, members: list[AudiobookSet]
    ) -> bool:
        """Reject sibling folders that only differ by a trailing sequence number."""
        if not self._share_common_parent(members):
            return False

        labels = [member.source_path.name for member in members]
        if self._labels_form_numbered_sequence(labels):
            return True

        raw_titles = [
            member.raw_title_guess or member.source_path.name for member in members
        ]
        return self._labels_form_numbered_sequence(raw_titles)

    def _labels_form_numbered_sequence(self, labels: list[str]) -> bool:
        """Check whether labels only differ by a trailing numbered segment."""
        bases: set[str] = set()
        numbers: set[int] = set()

        for label in labels:
            match = SEGMENT_SUFFIX_PATTERN.match(label)
            if match is None:
                return False
            bases.add(self.matcher.normalize_title(match.group("base")))
            try:
                numbers.add(int(match.group("num")))
            except ValueError:
                return False

        return len(bases) == 1 and len(numbers) == len(labels)

    def _looks_like_partitioned_sibling_durations(
        self, members: list[AudiobookSet]
    ) -> bool:
        """Reject sibling groups whose durations look like partitioned segments."""
        if not self._share_common_parent(members) or len(members) < 3:
            return False

        durations = [member.total_duration for member in members]
        if any(duration is None for duration in durations):
            return False

        typed_durations = [
            float(duration) for duration in durations if duration is not None
        ]
        total_duration = sum(typed_durations)
        longest_member = max(typed_durations, default=0.0)
        if longest_member == 0:
            return False

        return total_duration > longest_member * 2.5

    def _share_common_parent(self, members: list[AudiobookSet]) -> bool:
        """Check whether all candidate sets are sibling folders."""
        parents = {member.source_path.parent for member in members}
        return len(parents) == 1

    def analyze_files(
        self,
        dedupe_extensions: set[str] | None = None,
    ) -> list[FileGroup]:
        """Find byte-duplicate files under library_root using staged hashing."""
        if dedupe_extensions is None:
            dedupe_extensions = AUDIO_EXTENSIONS | IMAGE_EXTENSIONS | SIDECAR_EXTENSIONS

        # Stage 1: group by exact size
        size_groups: dict[int, list[Path]] = defaultdict(list)
        for f in iter_files_excluding_quarantine(self.library_root):
            if not f.is_file():
                continue
            if f.suffix.lower() not in dedupe_extensions:
                continue
            if f.name.startswith("."):
                continue
            try:
                size = f.stat().st_size
                size_groups[size].append(f)
            except OSError:
                continue

        # Discard singletons
        candidates = {sz: paths for sz, paths in size_groups.items() if len(paths) > 1}

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
        stage2 = {key: paths for key, paths in partial_groups.items() if len(paths) > 1}

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
            scoped_groups: dict[tuple[str, ...], list[Path]] = defaultdict(list)
            for path in paths:
                scoped_groups[self._file_dedupe_scope(path)].append(path)

            for scoped_paths in scoped_groups.values():
                if len(scoped_paths) <= 1:
                    continue
                fg = FileGroup(
                    size=scoped_paths[0].stat().st_size,
                    paths=sorted(scoped_paths),
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
        scheduled_sources: set[Path] = set()

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
                                    f"Edition duplicate: {candidate.quarantine_reason}"
                                ),
                            )
                        )
                        scheduled_sources.add(src)
                    group_info["quarantined"].append(
                        {
                            "path": str(ab.source_path),
                            "reason": candidate.quarantine_reason,
                        }
                    )

                plan.edition_groups.append(group_info)

        # File-level quarantines
        if file_groups:
            for fg in file_groups:
                keeper = self._pick_file_keeper(fg.paths, keeper_edition_paths)
                fg.keeper = keeper

                fg_info: dict = {
                    "keeper": str(keeper),
                    "size": fg.size,
                    "quarantined": [],
                }

                for p in fg.paths:
                    if p == keeper or p in scheduled_sources:
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
                    scheduled_sources.add(p)
                    fg_info["quarantined"].append(str(p))

                plan.file_groups.append(fg_info)

        plan.possible_duplicates = [
            {
                "paths": [str(member.source_path) for member in possible.members],
                "reason": possible.reason,
            }
            for possible in self.possible_duplicates
        ]
        plan.validate_sources_outside_quarantine()
        return plan

    def execute_plan(self, plan: DedupePlan, config_manager: ConfigManager) -> None:
        """Execute a quarantine plan (move files)."""
        plan.validate_sources_outside_quarantine()
        if plan.has_conflicts():
            raise ValueError("Plan has conflicts — destination files already exist")

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

    def _file_dedupe_scope(self, path: Path) -> tuple[str, ...]:
        """Keep non-audio duplicate detection scoped to one logical book root."""
        if path.suffix.lower() in AUDIO_EXTENSIONS:
            return ("audio",)

        logical_root = self.scanner._group_root_for_file(path)
        return ("non-audio", str(logical_root))

    def _edition_key(self, ab_set: AudiobookSet) -> str:
        author = self.matcher.normalize_author(ab_set.author_guess or "")
        # Strip dots for consistent keying (e.g. "J.R.R." → "JRR")
        author = author.replace(".", "")
        return f"{author}||{self._edition_title(ab_set.raw_title_guess or '')}"

    def _edition_title(self, raw_title: str) -> str:
        """Normalized title without edition noise or a moved article.

        "The Stand (Unabridged)" and "Stand, The" both give "stand"; the
        fuzzy pass no longer forgives extra words, so they must go here.
        """
        title = _EDITION_BRACKETS_RE.sub(" ", raw_title)
        title = _EDITION_WORDS_RE.sub(" ", title)
        title = self.matcher.normalize_title(title)
        tokens = title.split()
        if len(tokens) > 1 and tokens[-1] in _ARTICLES:
            tokens = tokens[:-1]
        if len(tokens) > 1 and tokens[0] in _ARTICLES:
            tokens = tokens[1:]
        return " ".join(tokens)

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
            format_rank,
            has_id,
            clean_order,
            has_cover,
            mean_bitrate,
            total_duration,
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
        for i, (w, lo) in enumerate(zip(winner_score, loser_score, strict=False)):
            if w > lo:
                return criteria[i] if i < len(criteria) else "lower score"
        return "tie-broken by position"

    def _pick_file_keeper(
        self, paths: list[Path], keeper_edition_paths: set[Path]
    ) -> Path:
        """Pick the keeper file from byte-identical duplicates."""
        paths_sorted = sorted(
            paths,
            key=lambda p: (
                -self._file_keeper_score(p, keeper_edition_paths)[0],
                -self._file_keeper_score(p, keeper_edition_paths)[1],
                -self._file_keeper_score(p, keeper_edition_paths)[2],
                len(str(p)),
                str(p),
            ),
        )
        return paths_sorted[0]

    def _file_keeper_score(
        self, path: Path, keeper_edition_paths: set[Path]
    ) -> tuple[int, int, int]:
        """Score byte-identical file candidates for keeper selection."""
        return (
            1 if self._is_within_keeper_edition(path, keeper_edition_paths) else 0,
            self._relative_directory_depth(path),
            1 if self._has_structured_parent_metadata(path) else 0,
        )

    @staticmethod
    def _is_within_keeper_edition(path: Path, keeper_edition_paths: set[Path]) -> bool:
        """Check whether a duplicate file lives inside an already-kept edition."""
        for keeper_path in keeper_edition_paths:
            try:
                path.relative_to(keeper_path)
                return True
            except ValueError:
                continue
        return False

    def _relative_directory_depth(self, path: Path) -> int:
        """Count how many folders separate a file from the library root."""
        try:
            relative_path = path.relative_to(self.library_root)
        except ValueError:
            relative_path = path
        return max(len(relative_path.parts) - 1, 0)

    def _has_structured_parent_metadata(self, path: Path) -> bool:
        """Detect whether ancestor folders look like organized audiobook metadata."""
        try:
            relative_path = path.relative_to(self.library_root)
        except ValueError:
            return False

        for index in range(len(relative_path.parts) - 1):
            folder_name = relative_path.parts[index]
            cleaned_name = self.scanner._clean_metadata_name(folder_name)
            title_guess, author_guess = self.scanner._author_title_guess(cleaned_name)
            if title_guess and author_guess:
                return True

        return False

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
