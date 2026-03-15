"""Tests for the smart organizer."""

import tempfile
from pathlib import Path

import pytest

from bookbot.core.models import AudiobookSet, AudioFormat, ProviderIdentity, Track
from bookbot.core.organizer import ReorganizationPlan, SmartOrganizer, TEMPLATE_PRESETS


@pytest.fixture
def organizer():
    return SmartOrganizer()


def _make_track(path: Path, index: int = 1) -> Track:
    return Track(
        src_path=path,
        track_index=index,
        audio_format=AudioFormat.MP3,
    )


def _make_set(
    path: Path,
    title: str = "Test Book",
    author: str = "Test Author",
    identity: ProviderIdentity | None = None,
) -> AudiobookSet:
    tracks = [_make_track(path / f"track{i}.mp3", i) for i in range(1, 4)]
    ab = AudiobookSet(
        source_path=path,
        raw_title_guess=title,
        author_guess=author,
        tracks=tracks,
        total_tracks=3,
    )
    if identity:
        ab.chosen_identity = identity
    return ab


class TestSmartOrganizer:
    def test_propose_default_template(self, organizer):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "source"
            src.mkdir()
            for i in range(1, 4):
                (src / f"track{i}.mp3").touch()

            ab = _make_set(src)
            plan = organizer.propose_reorganization(
                src, None, "default", [ab]
            )

            assert isinstance(plan, ReorganizationPlan)
            assert plan.total_moves == 3
            assert plan.is_valid

    def test_propose_abs_template(self, organizer):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "source"
            src.mkdir()
            for i in range(1, 4):
                (src / f"track{i}.mp3").touch()

            identity = ProviderIdentity(
                provider="test",
                external_id="1",
                title="My Book",
                authors=["Some Author"],
            )
            ab = _make_set(src, identity=identity)
            plan = organizer.propose_reorganization(
                src, None, "abs", [ab]
            )

            assert plan.total_moves == 3
            # ABS template: {Author}/{Title}
            for op in plan.operations:
                assert "Some Author" in str(op.destination) or "My Book" in str(
                    op.destination
                )

    def test_detects_collision(self, organizer):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "source"
            src.mkdir()
            for i in range(1, 4):
                (src / f"track{i}.mp3").touch()

            # Two sets that would map to same destination
            ab1 = _make_set(src, "Same Title", "Same Author")
            ab2 = _make_set(src, "Same Title", "Same Author")

            plan = organizer.propose_reorganization(
                src, None, "default", [ab1, ab2]
            )

            assert len(plan.conflicts) > 0
            assert not plan.is_valid

    def test_empty_sets(self, organizer):
        with tempfile.TemporaryDirectory() as tmp:
            plan = organizer.propose_reorganization(
                Path(tmp), None, "default", []
            )
            assert plan.total_moves == 0
            assert plan.is_valid


class TestTemplatePresets:
    def test_all_presets_exist(self):
        assert "default" in TEMPLATE_PRESETS
        assert "abs" in TEMPLATE_PRESETS
        assert "plex" in TEMPLATE_PRESETS

    def test_abs_preset_structure(self):
        preset = TEMPLATE_PRESETS["abs"]
        assert "{Author}" in preset["folder_template"]
        assert "{Title}" in preset["folder_template"]


class TestReorganizationPlan:
    def test_is_valid_no_conflicts(self):
        plan = ReorganizationPlan()
        assert plan.is_valid

    def test_is_invalid_with_conflicts(self):
        plan = ReorganizationPlan(conflicts=["collision"])
        assert not plan.is_valid
