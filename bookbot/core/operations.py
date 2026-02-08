"""Atomic file operations for safe renaming and undo functionality."""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from mutagen import MutagenError

from ..config.manager import ConfigManager
from .models import AudioTags, OperationRecord, RenameOperation, RenamePlan, Track


class TransactionManager:
    """Manages atomic file operations with undo capability."""

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.log_dir = config_manager.get_log_dir()

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

        transaction_id = str(uuid.uuid4())
        transaction_log = []

        try:
            # Phase 1: Create temporary names to avoid conflicts
            temp_operations = []
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
                operation.track.src_path = operation.new_path
                transaction_log.append(record)

            # Save transaction log for undo
            self._save_transaction_log(transaction_id, transaction_log)

            # Update plan status
            plan.dry_run = False

            return True

        except (OSError, ValueError) as e:
            # Rollback on failure
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
        for record in reversed(completed_records):
            try:
                if record.new_path and record.new_path.exists():
                    if record.old_path:
                        record.new_path.rename(record.old_path)
                    else:
                        record.new_path.unlink()  # Remove if no original path
            except OSError:
                # Best effort rollback
                pass

    def _save_transaction_log(
        self, transaction_id: str, records: list[OperationRecord]
    ) -> None:
        """Save transaction log for undo functionality."""
        log_file = self.log_dir / f"transaction_{transaction_id}.json"

        log_data = {
            "transaction_id": transaction_id,
            "timestamp": datetime.now().isoformat(),
            "operations": [record.model_dump() for record in records],
        }

        try:
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(log_data, f, indent=2, default=str)
        except OSError as e:
            print(f"Warning: Failed to save transaction log: {e}")

    def undo_transaction(self, transaction_id: str) -> bool:
        """Undo a previous transaction."""
        log_file = self.log_dir / f"transaction_{transaction_id}.json"

        if not log_file.exists():
            return False

        try:
            with open(log_file, encoding="utf-8") as f:
                log_data = json.load(f)

            records = [OperationRecord(**op) for op in log_data["operations"]]

            # Reverse the operations
            for record in reversed(records):
                if record.operation_type == "rename":
                    self._undo_rename(record)
                elif record.operation_type == "retag":
                    self._undo_retag(record)

            # Mark transaction as undone
            undo_file = log_file.with_suffix(".undone")
            log_file.rename(undo_file)

            return True

        except (OSError, json.JSONDecodeError) as e:
            print(f"Failed to undo transaction {transaction_id}: {e}")
            return False

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
                        "operation_count": len(log_data["operations"]),
                        "can_undo": False,
                        "status": "undone",
                    }
                )
            except (OSError, json.JSONDecodeError):
                continue

        return sorted(transactions, key=lambda x: x["timestamp"], reverse=True)

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
