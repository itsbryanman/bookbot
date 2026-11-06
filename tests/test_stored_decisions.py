"""Saved review decisions drive scan/plan/organize/convert/sidecar offline."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from bookbot.cli import cli
from bookbot.config.manager import ConfigManager
from bookbot.core.models import (
    AudiobookSet,
    AudioFormat,
    MatchCandidate,
    MatchConfidence,
    ProviderIdentity,
    Track,
    TrackStatus,
)
from bookbot.core.operations import TransactionManager
from bookbot.core.planning import PlanBuilder
from bookbot.core.review_store import (
    ReviewDecision,
    ReviewDecisionStore,
    StoredCandidateSelector,
    apply_stored_decisions,
)

ACCEPTED = ProviderIdentity(
    provider="audnexus",
    external_id="B0ASR",
    title="All Systems Red",
    authors=["Martha Wells"],
    series_name="The Murderbot Diaries",
    series_index="1",
    asin="B0ASR00001",
    isbn_13="9780765397539",
    narrator="Kevin R. Free",
    year=2017,
)
DECOY = ProviderIdentity(
    provider="audnexus",
    external_id="B0DECOY",
    title="Artificial Condition",
    authors=["Martha Wells"],
    asin="B0DECOY001",
)


class _Provider:
    """Two candidates; the decoy ranks first with a confidence that needs review."""

    async def find_matches_merged(
        self, audiobook_set: AudiobookSet, limit: int = 10
    ) -> list[MatchCandidate]:
        return [
            MatchCandidate(
                identity=DECOY,
                confidence=0.70,
                confidence_level=MatchConfidence.MEDIUM,
            ),
            MatchCandidate(
                identity=ACCEPTED,
                confidence=0.68,
                confidence_level=MatchConfidence.MEDIUM,
            ),
        ][:limit]

    async def close_all(self) -> None:
        return None


class _OfflineProvider:
    async def find_matches_merged(self, audiobook_set, limit=10):  # type: ignore[no-untyped-def]
        raise AssertionError("providers must not be called without --match")

    async def close_all(self) -> None:
        return None


def _make_set(folder: Path, name: str = "rough-book") -> AudiobookSet:
    source_path = folder / name
    source_path.mkdir(parents=True, exist_ok=True)
    tracks = []
    for index in (1, 2):
        track_path = source_path / f"{index:02d}.mp3"
        if not track_path.exists():
            track_path.write_bytes(b"\0" * index)
        tracks.append(
            Track(
                src_path=track_path,
                disc=1,
                track_index=index,
                file_size=index,
                audio_format=AudioFormat.MP3,
                status=TrackStatus.VALID,
            )
        )
    return AudiobookSet(
        source_path=source_path,
        raw_title_guess="Rough Book",
        total_tracks=len(tracks),
        tracks=tracks,
    )


def _candidate(identity: ProviderIdentity) -> MatchCandidate:
    return MatchCandidate(
        identity=identity, confidence=0.7, confidence_level=MatchConfidence.MEDIUM
    )


def _invoke(runner: CliRunner, config_dir: Path, *args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(cli, ["--config-dir", str(config_dir), *args])


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(
        "bookbot.core.discovery.AudioFileScanner.scan_directory",
        lambda self, path, progress=None: [_make_set(path)],
    )
    return root


def _use_provider(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    monkeypatch.setattr(
        "bookbot.cli._build_matching_provider",
        lambda config_manager, metadata_from_files=False: provider,
    )


# ── store ──


def test_accept_stores_full_identity_snapshot(tmp_path: Path) -> None:
    store = ReviewDecisionStore(ConfigManager(tmp_path / "cfg"))
    audiobook_set = _make_set(tmp_path)

    store.set_accept(audiobook_set, _candidate(ACCEPTED), candidate_index=2)

    raw = json.loads(store.path.read_text())
    decision = next(iter(raw.values()))
    assert decision["candidate"]["external_id"] == "B0ASR"
    assert decision["identity"]["series_name"] == "The Murderbot Diaries"
    assert decision["identity"]["narrator"] == "Kevin R. Free"


def test_apply_stored_decisions_without_candidates(tmp_path: Path) -> None:
    store = ReviewDecisionStore(ConfigManager(tmp_path / "cfg"))
    accepted = _make_set(tmp_path, "accepted")
    skipped = _make_set(tmp_path, "skipped")
    legacy = _make_set(tmp_path, "legacy")
    untouched = _make_set(tmp_path, "untouched")

    store.set_accept(accepted, _candidate(ACCEPTED), candidate_index=1)
    store.set_skip(skipped, reason="Not a book")
    decisions = store.load()
    decisions[store.review_key(legacy)] = ReviewDecision(
        action="accept",
        candidate=StoredCandidateSelector(
            provider="audnexus", external_id="old", title="Old", authors=[]
        ),
        candidate_index=1,
        decided_at=datetime.now().isoformat(),
    )
    store.save(decisions)

    states = apply_stored_decisions([accepted, skipped, legacy, untouched], store)

    assert states == ["accepted_saved", "skipped", "needs_review", None]
    assert accepted.chosen_identity == ACCEPTED
    assert accepted.needs_review is False
    assert skipped.review_skipped is True
    assert skipped.review_reason == "Not a book"
    assert legacy.chosen_identity is None
    assert legacy.needs_review is True
    assert "re-accept to refresh saved decision" in (legacy.review_reason or "")
    assert untouched.chosen_identity is None


def test_snapshot_used_when_provider_no_longer_returns_candidate(
    tmp_path: Path,
) -> None:
    config_manager = ConfigManager(tmp_path / "cfg")
    store = ReviewDecisionStore(config_manager)
    audiobook_set = _make_set(tmp_path)
    store.set_accept(audiobook_set, _candidate(ACCEPTED), candidate_index=2)

    state, chosen = store.resolve_match_state(
        audiobook_set, [_candidate(DECOY)], config_manager.load_config().matching
    )

    assert state == "accepted_saved"
    assert audiobook_set.chosen_identity == ACCEPTED
    assert chosen is not None and chosen.identity == ACCEPTED


# ── CLI: match → accept → scan --plan ──


def test_match_json_emits_stable_candidate_ids(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_provider(monkeypatch, _Provider())
    result = _invoke(CliRunner(), tmp_path / "cfg", "match", str(library), "--json")

    assert result.exit_code == 0, result.output
    candidates = json.loads(result.output)[0]["candidates"]
    assert candidates[1]["candidate_id"] == "audnexus:B0ASR"
    assert candidates[1]["asin"] == "B0ASR00001"
    assert candidates[1]["isbn_13"] == "9780765397539"


@pytest.mark.parametrize(
    "selector",
    [
        ["--candidate-id", "audnexus:B0ASR"],
        ["--asin", "b0asr00001"],
        ["--isbn", "978-0-7653-9753-9"],
        ["--candidate", "2"],
    ],
)
def test_match_accept_then_scan_plan_uses_accepted_identity(
    tmp_path: Path,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
    selector: list[str],
) -> None:
    runner = CliRunner()
    config_dir = tmp_path / "cfg"
    _use_provider(monkeypatch, _Provider())

    accepted = _invoke(
        runner, config_dir, "accept", str(library), "--set", "1", *selector
    )
    assert accepted.exit_code == 0, accepted.output
    assert "All Systems Red" in accepted.output

    _use_provider(monkeypatch, _OfflineProvider())
    plan_path = tmp_path / "plan.json"
    scanned = _invoke(
        runner, config_dir, "scan", str(library), "--plan", str(plan_path)
    )
    assert scanned.exit_code == 0, scanned.output

    diff = _invoke(runner, config_dir, "plan", "diff", str(plan_path))
    assert diff.exit_code == 0, diff.output
    assert "All Systems Red" in diff.output
    plan_payload = json.loads(plan_path.read_text())
    chosen = plan_payload["audiobook_sets"][0]["chosen_identity"]
    assert chosen["series_name"] == "The Murderbot Diaries"

    created = _invoke(
        runner,
        config_dir,
        "plan",
        "create",
        str(library),
        "-o",
        str(tmp_path / "plan2.json"),
    )
    assert created.exit_code == 0, created.output
    plan2 = json.loads((tmp_path / "plan2.json").read_text())
    assert plan2["audiobook_sets"][0]["chosen_identity"]["title"] == "All Systems Red"


def test_accept_rejects_unknown_identifier(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_provider(monkeypatch, _Provider())
    result = _invoke(
        CliRunner(),
        tmp_path / "cfg",
        "accept",
        str(library),
        "--set",
        "1",
        "--asin",
        "B000000000",
    )
    assert result.exit_code == 1
    assert "No candidate" in result.output


def test_accept_requires_exactly_one_selector(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_provider(monkeypatch, _Provider())
    result = _invoke(
        CliRunner(),
        tmp_path / "cfg",
        "accept",
        str(library),
        "--set",
        "1",
        "--candidate",
        "1",
        "--asin",
        "B0ASR00001",
    )
    assert result.exit_code == 1
    assert "exactly one" in result.output


def test_legacy_decision_blocks_plan_with_refresh_hint(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "cfg"
    store = ReviewDecisionStore(ConfigManager(config_dir))
    audiobook_set = _make_set(library)
    store.save(
        {
            store.review_key(audiobook_set): ReviewDecision(
                action="accept",
                candidate=StoredCandidateSelector(
                    provider="audnexus", external_id="old", title="Old", authors=[]
                ),
                candidate_index=1,
                decided_at=datetime.now().isoformat(),
            )
        }
    )
    _use_provider(monkeypatch, _OfflineProvider())
    plan_path = tmp_path / "plan.json"

    result = _invoke(
        CliRunner(), config_dir, "scan", str(library), "--plan", str(plan_path)
    )

    assert result.exit_code == 0, result.output
    assert "re-accept to refresh saved decision" in result.output
    assert json.loads(plan_path.read_text())["operations"] == []


def test_scan_match_runs_providers_and_saved_decisions(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    config_dir = tmp_path / "cfg"
    _use_provider(monkeypatch, _Provider())

    unreviewed = _invoke(runner, config_dir, "scan", str(library), "--match")
    assert unreviewed.exit_code == 0, unreviewed.output
    assert "Needs review" in unreviewed.output

    _invoke(
        runner, config_dir, "accept", str(library), "--set", "1", "--asin", "B0ASR00001"
    )
    reviewed = _invoke(runner, config_dir, "scan", str(library), "--match")
    assert reviewed.exit_code == 0, reviewed.output
    assert "Match: All Systems Red" in reviewed.output


def test_sidecar_write_uses_saved_decision(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "cfg"
    store = ReviewDecisionStore(ConfigManager(config_dir))
    audiobook_set = _make_set(library)
    store.set_accept(audiobook_set, _candidate(ACCEPTED), candidate_index=2)

    result = _invoke(
        CliRunner(),
        config_dir,
        "sidecar",
        "write",
        str(library),
        "--format",
        "json",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads((library / "metadata.json").read_text())
    assert payload["title"] == "All Systems Red"


# ── decisions follow books when apply moves them ──


def test_saved_decision_follows_book_after_apply_and_undo(tmp_path: Path) -> None:
    config_manager = ConfigManager(tmp_path / "cfg")
    store = ReviewDecisionStore(config_manager)
    library = tmp_path / "library"
    audiobook_set = _make_set(library)
    store.set_accept(audiobook_set, _candidate(ACCEPTED), candidate_index=2)
    apply_stored_decisions([audiobook_set], store)

    plan = PlanBuilder(config_manager.load_config()).create_plan(
        library_root=library, audiobook_sets=[audiobook_set], source_roots=[library]
    )
    assert plan.operations
    moves = {op.old_path: op.new_path for op in plan.operations}

    manager = TransactionManager(config_manager)
    assert manager.execute_plan(plan) is True

    new_tracks = sorted(path for path in moves.values() if path.suffix == ".mp3")
    moved = AudiobookSet(
        source_path=new_tracks[0].parent,
        tracks=[
            Track(
                src_path=new_path,
                disc=1,
                track_index=index,
                file_size=new_path.stat().st_size,
                audio_format=AudioFormat.MP3,
                status=TrackStatus.VALID,
            )
            for index, new_path in enumerate(new_tracks, start=1)
        ],
    )
    decision = store.get(moved)
    assert decision is not None
    assert decision.identity == ACCEPTED

    assert manager.undo_transaction(plan.applied_transaction_id or "")
    original = _make_set(library)
    assert store.get(original) is not None


# ── Codex review on #5 ──


def _legacy_decision(store: ReviewDecisionStore, audiobook_set: AudiobookSet) -> None:
    store.save(
        {
            store.review_key(audiobook_set): ReviewDecision(
                action="accept",
                candidate=StoredCandidateSelector(
                    provider=ACCEPTED.provider,
                    external_id=ACCEPTED.external_id,
                    title=ACCEPTED.title,
                    authors=ACCEPTED.authors,
                ),
                candidate_index=2,
                decided_at=datetime.now().isoformat(),
            )
        }
    )


def test_legacy_decision_is_upgraded_when_provider_resolves_it(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "cfg"
    store = ReviewDecisionStore(ConfigManager(config_dir))
    _legacy_decision(store, _make_set(library))
    runner = CliRunner()

    _use_provider(monkeypatch, _Provider())
    matched = _invoke(runner, config_dir, "match", str(library))
    assert matched.exit_code == 0, matched.output
    assert next(iter(store.load().values())).identity == ACCEPTED

    _use_provider(monkeypatch, _OfflineProvider())
    plan_path = tmp_path / "plan.json"
    scanned = _invoke(
        runner, config_dir, "scan", str(library), "--plan", str(plan_path)
    )
    assert scanned.exit_code == 0, scanned.output
    chosen = json.loads(plan_path.read_text())["audiobook_sets"][0]["chosen_identity"]
    assert chosen["title"] == "All Systems Red"


def test_saved_snapshot_survives_empty_provider_results(tmp_path: Path) -> None:
    config_manager = ConfigManager(tmp_path / "cfg")
    store = ReviewDecisionStore(config_manager)
    audiobook_set = _make_set(tmp_path)
    store.set_accept(audiobook_set, _candidate(ACCEPTED), candidate_index=2)

    state, chosen = store.resolve_match_state(
        audiobook_set, [], config_manager.load_config().matching
    )

    assert state == "accepted_saved"
    assert audiobook_set.chosen_identity == ACCEPTED


def test_saved_skip_survives_empty_provider_results(tmp_path: Path) -> None:
    config_manager = ConfigManager(tmp_path / "cfg")
    store = ReviewDecisionStore(config_manager)
    audiobook_set = _make_set(tmp_path)
    store.set_skip(audiobook_set)

    state, _ = store.resolve_match_state(
        audiobook_set, [], config_manager.load_config().matching
    )

    assert state == "skipped"
    assert audiobook_set.review_skipped is True
