"""Atomic file operations for safe renaming and undo functionality."""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from mutagen import MutagenError

from ..config.manager import ConfigManager
from ..core.logging import get_logger
from ..io.sidecar import SidecarManager, sidecar_output_paths
from .models import AudioTags, OperationRecord, RenameOperation, RenamePlan, Track

logger = get_logger("operations")


class TransactionManager:
    """Manages atomic file operations with undo capability."""

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.log_dir = config_manager.get_log_dir()
        self.sidecar_manager = SidecarManager()

    def _destination_folder_for_set(self, audiobook_set) -> Path:  # type: ignore[no-untyped-def]
        """Infer the current destination folder for one audiobook set."""
        parents = [track.src_path.parent for track in audiobook_set.tracks]
        if not parents:
            return audiobook_set.source_path
        try:
            return Path(os.path.commonpath([str(parent) for parent in parents]))
        except ValueError:
            return parents[0]

    @staticmethod
    def _next_backup_path(path: Path) -> Path:
        """Return a non-destructive backup path for an existing sidecar."""
        candidate = path.with_name(f"{path.name}.bak")
        if not candidate.exists():
            return candidate
        for index in range(2, 100):
            candidate = path.with_name(f"{path.name}.bak{index}")
            if not candidate.exists():
                return candidate
        raise FileExistsError(f"Unable to allocate backup path for {path}")

    def _download_cover_bytes(self, url: str, timeout: float) -> bytes:
        """Fetch remote cover art bytes with the configured timeout."""
        request = Request(url, headers={"User-Agent": "BookBot/1.0"})
        with urlopen(request, timeout=timeout) as response:
            return response.read()

    def _write_sidecar_record(
        self,
        transaction_id: str,
        target_path: Path,
        *,
        backup_path: Path | None,
        sidecar_kind: str,
    ) -> OperationRecord:
        """Create an undo record for a newly written sidecar file."""
        metadata: dict[str, Any] = {"sidecar_kind": sidecar_kind}
        if backup_path is not None:
            metadata["backup_path"] = str(backup_path)
        return OperationRecord(
            operation_id=transaction_id,
            timestamp=datetime.now(),
            operation_type="sidecar",
            old_path=backup_path,
            new_path=target_path,
            new_content_hash=self._calculate_file_hash(target_path),
            metadata=metadata,
        )

    def _write_sidecar_outputs(
        self,
        plan: RenamePlan,
        transaction_id: str,
        transaction_log: list[OperationRecord],
    ) -> None:
        """Write configured post-apply sidecars for reviewed audiobook sets."""
        config = self.config_manager.load_config()
        timeout = max(1.0, float(config.providers.request_timeout or 15))

        for audiobook_set in plan.audiobook_sets:
            identity = audiobook_set.chosen_identity
            if identity is None:
                continue

            destination_folder = self._destination_folder_for_set(audiobook_set)
            destination_folder.mkdir(parents=True, exist_ok=True)
            targets = sidecar_output_paths(destination_folder, identity, config.output)

            for sidecar_kind, target_path in targets.items():
                backup_path: Path | None = None
                if target_path.exists():
                    backup_path = self._next_backup_path(target_path)
                    target_path.rename(backup_path)

                try:
                    if sidecar_kind == "metadata_json":
                        self.sidecar_manager.write_metadata_json(target_path, identity)
                    elif sidecar_kind == "metadata_opf":
                        self.sidecar_manager.write_opf(target_path, identity)
                    elif sidecar_kind == "nfo":
                        self.sidecar_manager.write_nfo(target_path, identity)
                    elif sidecar_kind == "cover":
                        cover_bytes = self._download_cover_bytes(
                            identity.cover_urls[0],
                            timeout,
                        )
                        target_path.write_bytes(cover_bytes)
                    else:
                        raise ValueError(f"Unknown sidecar kind: {sidecar_kind}")
                except Exception as exc:
                    if target_path.exists():
                        target_path.unlink()
                    if backup_path is not None and backup_path.exists():
                        backup_path.rename(target_path)
                    if sidecar_kind == "cover":
                        logger.warning(
                            "Failed to fetch cover art sidecar",
                            path=str(target_path),
                            error=str(exc),
                        )
                        continue
                    raise

                transaction_log.append(
                    self._write_sidecar_record(
                        transaction_id,
                        target_path,
                        backup_path=backup_path,
                        sidecar_kind=sidecar_kind,
                    )
                )

    def create_rename_plan(self, operations: list[RenameOperation]) -> RenamePlan:
        """Create a rename plan with validation."""
        plan_id = str(uuid.uuid4())
        plan = RenamePlan(
            plan_id=plan_id,
            created_at=datetime.now(),
            source_path=operations[0].old_path.parent if operations else Path.cwd(),
            operations=operations,
            dry_run=True,
        )

        # Validate the plan
        plan.validate_plan()

        return plan

    def execute_plan(self, plan: RenamePlan, dry_run: bool = False) -> bool:
        """Execute a rename plan atomically."""
        if dry_run:
            plan.dry_run = True
            return True

        if plan.conflicts:
            raise ValueError(f"Cannot execute plan with conflicts: {plan.conflicts}")
        if not plan.operations and not plan.planned_sidecars:
            plan.dry_run = False
            return True

        transaction_id = str(uuid.uuid4())
        transaction_log: list[OperationRecord] = []
        temp_operations: list[RenameOperation] = []

        try:
            # Phase 1: Create temporary names to avoid conflicts
            for operation in plan.operations:
                temp_path = self._get_temp_path(operation.old_path)
                operation.temp_path = temp_path

                original_hash = self._calculate_file_hash(operation.old_path)

                # Record original state
                record = OperationRecord(
                    operation_id=transaction_id,
                    timestamp=datetime.now(),
                    operation_type="rename",
                    old_path=operation.old_path,
                    new_path=temp_path,
                    old_content_hash=original_hash,
                )

                # Move to temp location
                operation.old_path.rename(temp_path)
                temp_operations.append(operation)
                transaction_log.append(record)

            # Phase 2: Move from temp to final locations
            for operation in temp_operations:
                # Ensure target directory exists
                operation.new_path.parent.mkdir(parents=True, exist_ok=True)

                if operation.new_path.exists():
                    raise FileExistsError(
                        f"Target already exists: {operation.new_path}"
                    )

                if operation.temp_path is None:
                    raise ValueError("Temporary path not set for operation")

                final_hash = self._calculate_file_hash(operation.temp_path)

                # Record final state
                record = OperationRecord(
                    operation_id=transaction_id,
                    timestamp=datetime.now(),
                    operation_type="rename",
                    old_path=operation.temp_path,
                    new_path=operation.new_path,
                    new_content_hash=final_hash,
                )

                if operation.temp_path is None:
                    raise ValueError("Temporary path not set for operation")

                # Move to final location
                operation.temp_path.rename(operation.new_path)
                if operation.track is not None:
                    operation.track.src_path = operation.new_path
                transaction_log.append(record)

            # Phase 3: Write sidecars in the destination folders.
            self._write_sidecar_outputs(plan, transaction_id, transaction_log)

            # Save transaction log for undo
            self.record_transaction(
                transaction_id,
                transaction_log,
                transaction_type=(
                    "sidecar"
                    if not plan.operations
                    and any(
                        record.operation_type == "sidecar"
                        for record in transaction_log
                    )
                    else "rename"
                ),
            )

            # Prune source folders the apply just emptied. Without this the
            # old layout coexists with the new one (visible on multi-disc
            # books, where every disc folder is left behind empty).
            if plan.operations:
                self._prune_emptied_source_dirs(plan)

            # Update plan status
            plan.dry_run = False
            plan.applied_transaction_id = transaction_id

            return True

        except Exception as e:
            # Rollback on ANY failure. Catching only OSError/ValueError let
            # unexpected exceptions (e.g. AttributeError) escape mid-apply,
            # stranding .tmp_ files and leaving the library half-renamed with
            # no recorded transaction to undo.
            self._rollback_operations(temp_operations, transaction_log)
            raise RuntimeError(f"Failed to execute rename plan: {e}") from e

    def _get_temp_path(self, original_path: Path) -> Path:
        """Generate a temporary path on the same volume."""
        parent = original_path.parent
        stem = original_path.stem
        suffix = original_path.suffix
        temp_name = f"{stem}.tmp_{uuid.uuid4().hex[:8]}{suffix}"
        return parent / temp_name

    def _rollback_operations(
        self,
        operations: list[RenameOperation],
        completed_records: list[OperationRecord],
    ) -> None:
        """Rollback completed operations in reverse order."""
        _ = operations
        for record in reversed(completed_records):
            try:
                if record.operation_type == "sidecar":
                    self._undo_sidecar(record)
                    continue
                if (
                    record.operation_type == "rename"
                    and record.new_path
                    and record.new_path.exists()
                ):
                    if record.old_path:
                        record.old_path.parent.mkdir(parents=True, exist_ok=True)
                        record.new_path.rename(record.old_path)
                    else:
                        record.new_path.unlink()
            except OSError:
                # Best effort rollback
                pass

    def record_transaction(
        self,
        transaction_id: str,
        records: list[OperationRecord],
        *,
        transaction_type: str,
        timestamp: str | None = None,
        copy_to: list[Path] | None = None,
    ) -> Path | None:
        """Persist a transaction in the standard log directory and optional copies."""
        log_data = self._build_transaction_log_data(
            transaction_id,
            records,
            transaction_type=transaction_type,
            timestamp=timestamp,
        )
        primary_log = self._transaction_log_path(transaction_id)

        if not self._write_transaction_log(primary_log, log_data):
            return None

        for copy_path in copy_to or []:
            self._write_transaction_log(copy_path, log_data)

        return primary_log

    def _build_transaction_log_data(
        self,
        transaction_id: str,
        records: list[OperationRecord],
        *,
        transaction_type: str,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Build a JSON-serializable transaction payload."""
        return {
            "transaction_id": transaction_id,
            "transaction_type": transaction_type,
            "timestamp": timestamp or datetime.now().isoformat(),
            "operations": [record.model_dump(mode="json") for record in records],
        }

    def _transaction_log_path(self, transaction_id: str) -> Path:
        """Return the canonical path for a transaction log."""
        return self.log_dir / f"transaction_{transaction_id}.json"

    def _write_transaction_log(self, log_file: Path, log_data: dict[str, Any]) -> bool:
        """Write a transaction log, warning instead of raising on failure."""
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(log_data, f, indent=2, default=str)
        except OSError as e:
            print(f"Warning: Failed to save transaction log: {e}")
            return False
        return True

    def undo_transaction(self, transaction_id: str) -> bool:
        """Undo a previous transaction."""
        transaction_log = self._load_transaction_log(transaction_id)
        if transaction_log is None:
            return False

        log_file, log_data, legacy_log = transaction_log

        try:
            records = [OperationRecord(**op) for op in log_data["operations"]]

            # Reverse the operations
            for record in reversed(records):
                if record.operation_type == "rename":
                    self._undo_rename(record)
                elif record.operation_type == "sidecar":
                    self._undo_sidecar(record)
                elif record.operation_type == "retag":
                    self._undo_retag(record)

            # Mark transaction as undone
            undo_file = log_file.with_suffix(".undone")
            log_file.rename(undo_file)
            self._cleanup_dedupe_log_copies(
                transaction_id,
                log_data,
                keep_path=undo_file,
            )
            if legacy_log is not None and legacy_log.exists():
                legacy_log.unlink()
                self._cleanup_empty_directories(legacy_log.parent)

            return True

        except (OSError, json.JSONDecodeError) as e:
            print(f"Failed to undo transaction {transaction_id}: {e}")
            return False

    def _load_transaction_log(
        self, transaction_id: str
    ) -> tuple[Path, dict[str, Any], Path | None] | None:
        """Load a transaction log, migrating legacy dedupe logs when needed."""
        log_file = self._transaction_log_path(transaction_id)
        if log_file.exists():
            try:
                with open(log_file, encoding="utf-8") as f:
                    return log_file, json.load(f), None
            except (OSError, json.JSONDecodeError):
                return None

        legacy_log = self._find_legacy_dedupe_transaction_log(transaction_id)
        if legacy_log is None:
            return None

        try:
            with open(legacy_log, encoding="utf-8") as f:
                log_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

        log_data.setdefault("transaction_id", transaction_id)
        log_data.setdefault(
            "transaction_type",
            self._infer_transaction_type(log_data, fallback_path=legacy_log),
        )
        print(
            "Notice: using legacy dedupe transaction log from "
            f"{legacy_log} because no standard log entry was found."
        )

        if self._write_transaction_log(log_file, log_data):
            return log_file, log_data, legacy_log

        return legacy_log, log_data, None

    def _undo_rename(self, record: OperationRecord) -> None:
        """Undo a rename operation."""
        if record.new_path and record.old_path:
            if record.new_path.exists():
                # Verify file integrity if hash is available
                if record.new_content_hash:
                    current_hash = self._calculate_file_hash(record.new_path)
                    if current_hash != record.new_content_hash:
                        print(f"Warning: File {record.new_path} may have been modified")

                # Restore original path
                record.old_path.parent.mkdir(parents=True, exist_ok=True)
                record.new_path.rename(record.old_path)
                self._cleanup_empty_directories(record.new_path.parent)

    def _undo_sidecar(self, record: OperationRecord) -> None:
        """Undo a sidecar write, restoring any displaced file backup."""
        if record.new_path and record.new_path.exists():
            record.new_path.unlink()

        backup_value = record.metadata.get("backup_path")
        backup_path = Path(str(backup_value)) if backup_value else None
        if backup_path is not None and backup_path.exists() and record.new_path:
            record.new_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.rename(record.new_path)
            self._cleanup_empty_directories(backup_path.parent)

        if record.new_path is not None:
            self._cleanup_empty_directories(record.new_path.parent)

    def _prune_emptied_source_dirs(self, plan: RenamePlan) -> None:
        """Remove source directories left empty by an applied plan.

        Only directories that are genuinely empty are removed, only inside
        the plan's own source root, and never the root itself. Undo recreates
        them via `old_path.parent.mkdir(parents=True)`.
        """
        root = plan.source_path.resolve(strict=False)
        source_dirs = {
            operation.old_path.parent for operation in plan.operations
        }
        # Deepest first so parents become empty before they're considered.
        for directory in sorted(
            source_dirs, key=lambda path: len(path.parts), reverse=True
        ):
            resolved = directory.resolve(strict=False)
            if resolved == root or root not in resolved.parents:
                continue
            if not directory.is_dir():
                continue
            try:
                if any(directory.iterdir()):
                    continue
            except OSError:
                continue
            self._cleanup_empty_directories(directory, stop_at=root)

    def _cleanup_empty_directories(
        self, start_dir: Path, stop_at: Path | None = None
    ) -> None:
        """Best-effort cleanup of empty directories.

        `stop_at` is an exclusive boundary: it and everything above it are
        never removed, so pruning can't escape the library root.
        """
        boundary = stop_at.resolve(strict=False) if stop_at is not None else None
        current = start_dir
        while current != current.parent:
            resolved = current.resolve(strict=False)
            if boundary is not None and (
                resolved == boundary or boundary not in resolved.parents
            ):
                break
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def _cleanup_dedupe_log_copies(
        self, transaction_id: str, log_data: dict[str, Any], keep_path: Path
    ) -> None:
        """Remove quarantine-side transaction copies after dedupe undo."""
        if self._infer_transaction_type(log_data) != "dedupe":
            return

        operations = log_data.get("operations", [])
        new_paths = [
            Path(str(operation["new_path"]))
            for operation in operations
            if isinstance(operation, dict) and operation.get("new_path")
        ]
        if not new_paths:
            return

        quarantine_root = self._dedupe_quarantine_root(new_paths)
        if quarantine_root is None:
            return

        for candidate in (
            quarantine_root / f"transaction_{transaction_id}.json",
            quarantine_root / f"transaction_{transaction_id}.undone",
        ):
            if candidate == keep_path:
                continue
            try:
                if candidate.exists():
                    candidate.unlink()
                    self._cleanup_empty_directories(candidate.parent)
            except OSError:
                continue

    def _dedupe_quarantine_root(self, new_paths: list[Path]) -> Path | None:
        """Extract the per-plan quarantine root from dedupe destination paths."""
        for path in new_paths:
            parts = path.parts
            if ".bookbot-quarantine" not in parts:
                continue
            marker_index = parts.index(".bookbot-quarantine")
            if marker_index + 1 >= len(parts):
                continue
            return Path(*parts[: marker_index + 2])
        return None

    def _undo_retag(self, record: OperationRecord) -> None:
        """Undo a retag operation."""
        # This would restore original tags - implementation depends on audio library
        # For now, just log that retag undo is not fully implemented
        print(f"Warning: Tag undo for {record.new_path} not fully implemented")

    def _calculate_file_hash(self, file_path: Path) -> str:
        """Calculate SHA-256 hash of a file."""
        import hashlib

        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def list_transactions(self, days: int = 30) -> list[dict]:
        """List recent transactions that can be undone."""
        cutoff_date = datetime.now().timestamp() - (days * 24 * 60 * 60)
        transactions = []

        for log_file in self.log_dir.glob("transaction_*.json"):
            if log_file.stat().st_mtime < cutoff_date:
                continue

            try:
                with open(log_file, encoding="utf-8") as f:
                    log_data = json.load(f)

                transactions.append(
                    {
                        "id": log_data["transaction_id"],
                        "timestamp": log_data["timestamp"],
                        "transaction_type": self._infer_transaction_type(
                            log_data, fallback_path=log_file
                        ),
                        "operation_count": len(log_data["operations"]),
                        "can_undo": True,
                    }
                )
            except (OSError, json.JSONDecodeError):
                continue

        # Check for undone transactions
        for undo_file in self.log_dir.glob("transaction_*.undone"):
            try:
                with open(undo_file, encoding="utf-8") as f:
                    log_data = json.load(f)

                transactions.append(
                    {
                        "id": log_data["transaction_id"],
                        "timestamp": log_data["timestamp"],
                        "transaction_type": self._infer_transaction_type(
                            log_data, fallback_path=undo_file
                        ),
                        "operation_count": len(log_data["operations"]),
                        "can_undo": False,
                        "status": "undone",
                    }
                )
            except (OSError, json.JSONDecodeError):
                continue

        return sorted(transactions, key=lambda x: x["timestamp"], reverse=True)

    def _infer_transaction_type(
        self, log_data: dict[str, Any], fallback_path: Path | None = None
    ) -> str:
        """Infer a human-meaningful transaction type for history output."""
        transaction_type = log_data.get("transaction_type")
        if isinstance(transaction_type, str) and transaction_type:
            return transaction_type

        operations = log_data.get("operations", [])
        new_paths = [
            str(operation.get("new_path", ""))
            for operation in operations
            if isinstance(operation, dict)
        ]
        if any(".bookbot-quarantine" in path for path in new_paths):
            return "dedupe"
        if fallback_path and ".bookbot-quarantine" in str(fallback_path):
            return "dedupe"
        return "rename"

    def _find_legacy_dedupe_transaction_log(self, transaction_id: str) -> Path | None:
        """Search likely library roots for pre-migration dedupe transaction logs."""
        filename = f"transaction_{transaction_id}.json"
        seen_roots: set[Path] = set()

        for root in [Path.cwd(), *Path.cwd().parents]:
            if root in seen_roots:
                continue
            seen_roots.add(root)

            quarantine_root = root / ".bookbot-quarantine"
            direct_match = quarantine_root / transaction_id / filename
            if direct_match.exists():
                return direct_match

            matches = sorted(quarantine_root.glob(f"*/{filename}"))
            if matches:
                return matches[0]

        return None

    def cleanup_old_transactions(self, days: int = 30) -> int:
        """Clean up transaction logs older than specified days."""
        cutoff_date = datetime.now().timestamp() - (days * 24 * 60 * 60)
        removed_count = 0

        for log_file in self.log_dir.glob("transaction_*"):
            if log_file.stat().st_mtime < cutoff_date:
                try:
                    log_file.unlink()
                    removed_count += 1
                except OSError:
                    pass

        return removed_count


class TagManager:
    """Manages audio file tagging operations."""

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.config = config_manager.load_config()

    def apply_tags(
        self, track: Track, new_tags: AudioTags, preserve_existing: bool = True
    ) -> bool:
        """Apply tags to an audio file."""
        if not self.config.tagging.enabled:
            return True

        try:
            from mutagen import File as MutagenFile

            audio_file = MutagenFile(track.src_path, easy=True)
            if audio_file is None:
                return False

            # Backup original tags for undo
            original_tags = track.existing_tags

            # Apply new tags based on policy
            if self.config.tagging.overwrite_policy.value == "overwrite":
                self._write_all_tags(audio_file, new_tags)
            elif self.config.tagging.overwrite_policy.value == "fill_missing":
                self._write_missing_tags(audio_file, new_tags, original_tags)
            else:  # preserve
                return True

            # Save the file
            audio_file.save()

            # Update track with new tags
            track.proposed_tags = new_tags

            return True

        except (ImportError, MutagenError, OSError, ValueError) as e:
            print(f"Failed to apply tags to {track.src_path}: {e}")
            return False

    def _write_all_tags(self, audio_file: Any, new_tags: AudioTags) -> None:
        """Write all tags, overwriting existing ones."""
        tag_mapping = {
            "title": new_tags.title,
            "album": new_tags.album,
            "artist": new_tags.artist,
            "albumartist": new_tags.albumartist,
            "date": new_tags.date,
            "genre": new_tags.genre or "Audiobook",
            "tracknumber": str(new_tags.track) if new_tags.track else None,
            "discnumber": str(new_tags.disc) if new_tags.disc else None,
        }

        for key, value in tag_mapping.items():
            if value is not None and self._should_write_tag(key):
                audio_file[key] = value

    def _write_missing_tags(
        self, audio_file: Any, new_tags: AudioTags, original_tags: AudioTags
    ) -> None:
        """Write tags only if they don't already exist."""
        tag_mapping = {
            "title": (new_tags.title, original_tags.title),
            "album": (new_tags.album, original_tags.album),
            "artist": (new_tags.artist, original_tags.artist),
            "albumartist": (new_tags.albumartist, original_tags.albumartist),
            "date": (new_tags.date, original_tags.date),
            "genre": (new_tags.genre or "Audiobook", original_tags.genre),
            "tracknumber": (
                str(new_tags.track) if new_tags.track else None,
                str(original_tags.track) if original_tags.track else None,
            ),
            "discnumber": (
                str(new_tags.disc) if new_tags.disc else None,
                str(original_tags.disc) if original_tags.disc else None,
            ),
        }

        for key, (new_value, original_value) in tag_mapping.items():
            if (
                new_value is not None
                and not original_value
                and self._should_write_tag(key)
            ):
                audio_file[key] = new_value

    def _should_write_tag(self, tag_name: str) -> bool:
        """Check if a specific tag should be written based on config."""
        tag_config_map = {
            "title": self.config.tagging.write_title,
            "album": self.config.tagging.write_album,
            "artist": self.config.tagging.write_artist,
            "albumartist": self.config.tagging.write_albumartist,
            "date": self.config.tagging.write_date,
            "genre": self.config.tagging.write_genre,
            "tracknumber": self.config.tagging.write_track,
            "discnumber": self.config.tagging.write_disc,
        }

        return tag_config_map.get(tag_name, True)
