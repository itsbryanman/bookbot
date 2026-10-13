"""Regression tests for matching real-world audiobook libraries.

Covers query construction from noisy folder names, tags and Audiobookshelf
metadata.json sidecars, the Audible catalog and Audnexus providers, and
graceful degradation when a provider cannot be reached. No test touches the
network.
"""

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from click.testing import CliRunner

from bookbot.core.discovery import AudioFileScanner
from bookbot.core.exceptions import MetadataError, ProviderUnavailableError
from bookbot.core.matching import clean_query_title
from bookbot.core.models import AudiobookSet, AudioTags, ProviderIdentity
from bookbot.providers.audible import AudibleProvider
from bookbot.providers.audnexus import AudnexusProvider
from bookbot.providers.base import MetadataProvider
from bookbot.providers.manager import ProviderManager
from bookbot.providers.openlibrary import OpenLibraryProvider

# ── helpers ──


def _patch_audio_metadata(
    scanner: AudioFileScanner,
    monkeypatch: pytest.MonkeyPatch,
    tag_map: dict[Path, AudioTags],
    duration: float = 600.0,
) -> None:
    def fake_extract_audio_tags(file_path: Path) -> AudioTags:
        return tag_map.get(file_path, AudioTags())

    def fake_extract_audio_properties(
        file_path: Path,
    ) -> tuple[float | None, int | None, int | None, int | None]:
        return duration, None, None, None

    monkeypatch.setattr(scanner, "_extract_audio_tags", fake_extract_audio_tags)
    monkeypatch.setattr(
        scanner, "_extract_audio_properties", fake_extract_audio_properties
    )


def _abs_metadata(**overrides: Any) -> dict[str, Any]:
    """An Audiobookshelf-style metadata.json payload."""
    data: dict[str, Any] = {
        "tags": [],
        "chapters": [],
        "title": "Rock Paper Scissors",
        "subtitle": "A Novel",
        "authors": ["Alice Feeney"],
        "narrators": ["Richard Armitage", "Stephanie Racine"],
        "series": [],
        "genres": ["Literature & Fiction"],
        "publishedYear": "2021",
        "publishedDate": None,
        "publisher": "Macmillan Audio",
        "description": "A thriller.",
        "isbn": None,
        "asin": None,
        "language": None,
        "explicit": False,
        "abridged": False,
    }
    data.update(overrides)
    return data


def _manager_with(providers: list[MetadataProvider]) -> ProviderManager:
    manager = object.__new__(ProviderManager)
    manager.providers = {p.name: p for p in providers}
    manager.config_manager = None
    manager.cache_manager = None
    return manager


class _RecordingProvider(MetadataProvider):
    """Returns a fixed identity only for searches that match ``answer_for``."""

    def __init__(
        self,
        name: str = "recording",
        answer_for: dict[str, Any] | None = None,
        identity: ProviderIdentity | None = None,
    ) -> None:
        super().__init__(name)
        self.calls: list[dict[str, Any]] = []
        self.answer_for = answer_for or {}
        self.identity = identity

    async def search(self, **kwargs: Any) -> list[ProviderIdentity]:  # type: ignore[override]
        self.calls.append(kwargs)
        if self.identity is None:
            return []
        if all(kwargs.get(key) == value for key, value in self.answer_for.items()):
            return [self.identity]
        return []

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        return None


class _UnreachableProvider(MetadataProvider):
    """Simulates a provider whose host refuses connections."""

    def __init__(self, name: str = "unreachable") -> None:
        super().__init__(name)
        self.search_calls = 0

    async def search(self, **kwargs: Any) -> list[ProviderIdentity]:  # type: ignore[override]
        self.search_calls += 1
        self._record_network_failure(aiohttp.ClientConnectionError("refused"))
        return []

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        return None


# ── 1. query noise stripping ──


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Rock Paper Scissors: A Novel", "Rock Paper Scissors"),
        ("Rock Paper Scissors - A Novel", "Rock Paper Scissors"),
        ("The Only One Left A Novel", "The Only One Left"),
        ("American Psycho (Unabridged)", "American Psycho"),
        ("Intensity: A Novel (Unabridged)", "Intensity"),
        ("Sometimes I Lie [audio]", "Sometimes I Lie"),
        ("Dolores Claiborne 64k 07.58.53 {220mb}", "Dolores Claiborne"),
        ("It 96k 44.54.21 {1.81gb}", "It"),
        ("1992 - Dolores Claiborne", "Dolores Claiborne"),
        ("Educated: A Memoir", "Educated"),
        ("Gone Girl_M4B", "Gone Girl"),
        ("Stephen King - The Shining", "Stephen King - The Shining"),
        ("1984", "1984"),
    ],
)
def test_clean_query_title_strips_noise(raw: str, expected: str) -> None:
    assert clean_query_title(raw) == expected


def test_find_matches_searches_cleaned_title_with_author() -> None:
    identity = ProviderIdentity(
        provider="recording",
        external_id="x",
        title="Rock Paper Scissors",
        authors=["Alice Feeney"],
    )
    provider = _RecordingProvider(
        answer_for={"title": "Rock Paper Scissors", "author": "Alice Feeney"},
        identity=identity,
    )
    audiobook = AudiobookSet(
        source_path=Path("/library/Alice Feeney - Rock Paper Scissors"),
        raw_title_guess="Rock Paper Scissors: A Novel",
        author_guess="Alice Feeney",
    )

    candidates = asyncio.run(provider.find_matches(audiobook))

    assert provider.calls[0]["title"] == "Rock Paper Scissors"
    assert provider.calls[0]["author"] == "Alice Feeney"
    assert candidates and candidates[0].identity.title == "Rock Paper Scissors"
    # Scoring runs against the cleaned title, so the noise costs nothing.
    assert "Excellent title match" in candidates[0].match_reasons


def test_find_matches_falls_back_to_raw_title() -> None:
    identity = ProviderIdentity(
        provider="recording",
        external_id="x",
        title="Night Shift (20 short stories)",
        authors=["Stephen King"],
    )
    provider = _RecordingProvider(
        answer_for={"title": "Night Shift (20 short stories)"}, identity=identity
    )
    audiobook = AudiobookSet(
        source_path=Path("/library/x"),
        raw_title_guess="Night Shift (20 short stories)",
        author_guess="Stephen King",
    )

    candidates = asyncio.run(provider.find_matches(audiobook))

    assert candidates
    assert {"Night Shift", "Night Shift (20 short stories)"} <= {
        call.get("title") for call in provider.calls
    }


# ── 1. discovery evidence: folder names, tags and metadata.json ──


def test_discovery_uses_audiobookshelf_metadata_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "Alice Feeney - Rock Paper Scissors"
    book.mkdir()
    audio = book / "Rock Paper Scissors - A Novel.m4b"
    audio.touch()
    (book / "metadata.json").write_text(
        json.dumps(
            _abs_metadata(
                asin="B08KGKWQ5Y",
                series=["Standalone Thrillers #2"],
            )
        ),
        encoding="utf-8",
    )
    scanner = AudioFileScanner()
    _patch_audio_metadata(
        scanner,
        monkeypatch,
        {audio: AudioTags(album="Rock Paper Scissors: A Novel", artist="Alice Feeney")},
    )

    [audiobook] = scanner.scan_directory(book)

    assert audiobook.raw_title_guess == "Rock Paper Scissors"
    assert audiobook.author_guess == "Alice Feeney"
    assert audiobook.narrator_guess == "Richard Armitage"
    assert audiobook.asin_guess == "B08KGKWQ5Y"
    assert audiobook.series_guess == "Standalone Thrillers"
    assert audiobook.volume_guess == "2"
    assert audiobook.year_guess == 2021


def test_sidecar_title_replaces_noisy_folder_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "1992 - Dolores Claiborne (Ward) 64k 07.58.53 {220mb}"
    book.mkdir()
    tracks = [book / "01.mp3", book / "02.mp3"]
    for track in tracks:
        track.touch()
    (book / "metadata.json").write_text(
        json.dumps(
            _abs_metadata(
                title="Dolores Claiborne (Unabridged)",
                subtitle=None,
                authors=["Stephen King"],
                narrators=["Frances Sternhagen"],
            )
        ),
        encoding="utf-8",
    )
    scanner = AudioFileScanner()
    _patch_audio_metadata(scanner, monkeypatch, {})

    [audiobook] = scanner.scan_directory(book)

    assert audiobook.raw_title_guess == "Dolores Claiborne"
    assert audiobook.author_guess == "Stephen King"


def test_sidecar_ignores_narrators_listed_as_authors_and_junk_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "Freida McFadden - The Boyfriend mp3"
    book.mkdir()
    tracks = [book / "01.mp3", book / "02.mp3"]
    for track in tracks:
        track.touch()
    (book / "metadata.json").write_text(
        json.dumps(
            _abs_metadata(
                title="The Boyfriend",
                subtitle=None,
                authors=["Victoria Connolly", "Adam Blanford", "Freida McFadden"],
                narrators=["Victoria Connolly", "Adam Blanford"],
                series=["Freida McFadden"],
                asin="B0D3QV4S65",
            )
        ),
        encoding="utf-8",
    )
    scanner = AudioFileScanner()
    _patch_audio_metadata(scanner, monkeypatch, {})

    [audiobook] = scanner.scan_directory(book)

    assert audiobook.raw_title_guess == "The Boyfriend"
    assert audiobook.author_guess == "Freida McFadden"
    assert audiobook.series_guess is None
    assert audiobook.asin_guess == "B0D3QV4S65"


def test_folder_noise_and_leading_year_are_stripped_without_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "Stephen King - It (Weber) 96k 44.54.21 {1.81gb}"
    book.mkdir()
    tracks = [book / "01.mp3", book / "02.mp3"]
    for track in tracks:
        track.touch()
    scanner = AudioFileScanner()
    _patch_audio_metadata(scanner, monkeypatch, {})

    [audiobook] = scanner.scan_directory(book)

    assert audiobook.raw_title_guess == "It"
    assert audiobook.author_guess == "Stephen King"

    title, author, _series, _volume = scanner._extract_metadata_guesses(
        Path("2018 - Colleen Hoover - Verity"), []
    )
    assert (title, author) == ("Verity", "Colleen Hoover")


def test_series_number_prefix_folder_is_not_an_author(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "Earthsea 01 - A Wizard of Earthsea"
    book.mkdir()
    tracks = [book / "01.mp3", book / "02.mp3"]
    for track in tracks:
        track.touch()
    scanner = AudioFileScanner()
    _patch_audio_metadata(
        scanner,
        monkeypatch,
        {track: AudioTags(artist="Ursula K. Le Guin") for track in tracks},
    )

    [audiobook] = scanner.scan_directory(book)

    assert audiobook.author_guess == "Ursula K. Le Guin"
    assert audiobook.series_guess == "Earthsea"
    assert audiobook.volume_guess == "1"
    assert audiobook.raw_title_guess == "A Wizard of Earthsea"


def test_author_series_number_title_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanner = AudioFileScanner()

    title, author, series, volume = scanner._extract_metadata_guesses(
        Path("Matt Dinniman - Dungeon Crawler Carl 01 - Dungeon Crawler Carl"), []
    )

    assert (title, author, series, volume) == (
        "Dungeon Crawler Carl",
        "Matt Dinniman",
        "Dungeon Crawler Carl",
        "1",
    )


# ── 1. Audible catalog API and Audnexus ──


_CATALOG_PRODUCT = {
    "asin": "B08KGKWQ5Y",
    "title": "Rock Paper Scissors",
    "subtitle": "A Novel",
    "authors": [{"asin": "B01N7B9IFJ", "name": "Alice Feeney"}],
    "narrators": [{"name": "Richard Armitage"}, {"name": "Stephanie Racine"}],
    "series": [{"asin": "S1", "title": "Some Series", "sequence": "2"}],
    "release_date": "2021-09-21",
    "runtime_length_min": 622,
    "language": "english",
    "publisher_name": "Macmillan Audio",
    "format_type": "unabridged",
    "product_images": {"500": "https://m.media-amazon.com/images/I/cover.jpg"},
    "merchandising_summary": "A thriller.",
}


def test_audible_search_uses_catalog_api_with_title_and_author() -> None:
    provider = AudibleProvider(marketplace="US")
    with patch.object(
        provider,
        "_get_json",
        new_callable=AsyncMock,
        return_value={"products": [_CATALOG_PRODUCT]},
    ) as get_json:
        results = asyncio.run(
            provider.search(title="Rock Paper Scissors", author="Alice Feeney")
        )

    path, params = get_json.call_args.args
    assert path == "/1.0/catalog/products"
    assert params["title"] == "Rock Paper Scissors"
    assert params["author"] == "Alice Feeney"
    assert "keywords" not in params
    [identity] = results
    assert identity.asin == "B08KGKWQ5Y"
    assert identity.authors == ["Alice Feeney"]
    assert identity.narrator == "Richard Armitage, Stephanie Racine"
    assert identity.runtime_minutes == 622
    assert identity.year == 2021
    assert identity.series_name == "Some Series"
    assert identity.series_index == "2"
    assert identity.language == "english"
    assert identity.cover_urls == ["https://m.media-amazon.com/images/I/cover.jpg"]


def test_audible_marketplace_selects_regional_api_host() -> None:
    assert AudibleProvider(marketplace="UK").api_base == "https://api.audible.co.uk"
    assert AudibleProvider(marketplace="us").api_base == "https://api.audible.com"


def test_audible_asin_lookup_uses_catalog_product() -> None:
    provider = AudibleProvider()
    with patch.object(
        provider,
        "_get_json",
        new_callable=AsyncMock,
        return_value={"product": _CATALOG_PRODUCT},
    ) as get_json:
        identity = asyncio.run(provider.get_by_id("b08kgkwq5y"))

    assert get_json.call_args.args[0] == "/1.0/catalog/products/B08KGKWQ5Y"
    assert identity is not None and identity.title == "Rock Paper Scissors"


def test_audnexus_has_no_title_search_route_and_skips_the_request() -> None:
    provider = AudnexusProvider()
    with patch.object(provider, "_request", new_callable=AsyncMock) as request:
        results = asyncio.run(
            provider.search(title="Rock Paper Scissors", author="Alice Feeney")
        )

    assert results == []
    request.assert_not_called()


def test_audnexus_resolves_asin_guess() -> None:
    provider = AudnexusProvider()
    book = {
        "asin": "B08KGKWQ5Y",
        "title": "Rock Paper Scissors",
        "authors": [{"name": "Alice Feeney"}],
        "narrators": [{"name": "Richard Armitage"}],
        "runtimeLengthMin": 622,
    }
    audiobook = AudiobookSet(
        source_path=Path("/library/x"),
        raw_title_guess="Rock Paper Scissors",
        author_guess="Alice Feeney",
        asin_guess="B08KGKWQ5Y",
    )
    with patch.object(
        provider, "_request", new_callable=AsyncMock, return_value=book
    ) as request:
        candidates = asyncio.run(provider.find_matches(audiobook))

    request.assert_called_once_with("GET", "/books/B08KGKWQ5Y")
    assert candidates[0].identity.asin == "B08KGKWQ5Y"
    assert candidates[0].identity.runtime_minutes == 622
    assert candidates[0].confidence == 1.0


# ── 3. unreachable providers are cheap ──


def test_provider_sessions_use_short_connect_timeout() -> None:
    for provider in (AudibleProvider(), AudnexusProvider(), OpenLibraryProvider()):
        timeout = provider._client_timeout()
        assert timeout.sock_connect is not None and timeout.sock_connect <= 5
        assert timeout.total is not None and timeout.total <= 20


def test_unreachable_provider_raises_after_one_attempt() -> None:
    provider = OpenLibraryProvider()

    class _Session:
        closed = False

        def get(self, *args: Any, **kwargs: Any) -> Any:
            raise aiohttp.ClientConnectionError("Cannot connect to host")

    audiobook = AudiobookSet(
        source_path=Path("/library/x"),
        raw_title_guess="Rock Paper Scissors",
        author_guess="Alice Feeney",
    )
    with (
        patch.object(provider, "_get_session", new=AsyncMock(return_value=_Session())),
        patch.object(provider, "_rate_limit", new=AsyncMock()),
    ):
        spy = AsyncMock(wraps=provider.search)
        with patch.object(provider, "search", new=spy):
            with pytest.raises(ProviderUnavailableError):
                asyncio.run(provider.find_matches(audiobook))

    # The fallback ladder must not keep knocking on a dead host.
    assert spy.await_count == 1


def test_audnexus_does_not_retry_connection_failures() -> None:
    provider = AudnexusProvider()
    attempts = 0

    class _Session:
        closed = False

        def request(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal attempts
            attempts += 1
            raise aiohttp.ClientConnectionError("Cannot connect to host")

    with (
        patch.object(provider, "_get_session", new=AsyncMock(return_value=_Session())),
        patch("bookbot.providers.audnexus.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        result = asyncio.run(provider.get_by_id("B08KGKWQ5Y"))

    assert result is None
    assert attempts == 1
    sleep.assert_not_awaited()


def test_circuit_breaker_skips_provider_after_repeated_failures() -> None:
    dead = _UnreachableProvider("dead")
    alive = _RecordingProvider(
        "alive",
        identity=ProviderIdentity(
            provider="alive",
            external_id="1",
            title="The Stand",
            authors=["Stephen King"],
        ),
    )
    manager = _manager_with([dead, alive])
    audiobook = AudiobookSet(
        source_path=Path("/library/x"),
        raw_title_guess="The Stand",
        author_guess="Stephen King",
    )

    with patch.object(manager, "get_enabled_providers", return_value=[dead, alive]):
        for _ in range(6):
            candidates = asyncio.run(manager.find_matches_merged(audiobook))
            assert candidates[0].identity.provider == "alive"

    assert dead.search_calls == ProviderManager.CIRCUIT_BREAKER_THRESHOLD


def test_all_providers_unreachable_reports_unavailable_not_no_match() -> None:
    first, second = _UnreachableProvider("first"), _UnreachableProvider("second")
    manager = _manager_with([first, second])
    audiobook = AudiobookSet(
        source_path=Path("/library/x"),
        raw_title_guess="The Stand",
        author_guess="Stephen King",
    )

    with patch.object(manager, "get_enabled_providers", return_value=[first, second]):
        for _ in range(ProviderManager.CIRCUIT_BREAKER_THRESHOLD + 2):
            with pytest.raises(MetadataError, match="providers unavailable"):
                asyncio.run(manager.find_matches_merged(audiobook))

    # Once tripped, the breaker answers without touching the network.
    assert first.search_calls == ProviderManager.CIRCUIT_BREAKER_THRESHOLD
    assert second.search_calls == ProviderManager.CIRCUIT_BREAKER_THRESHOLD


def test_success_resets_failure_count() -> None:
    class _Flaky(_UnreachableProvider):
        def __init__(self) -> None:
            super().__init__("flaky")
            self.fail = True

        async def search(self, **kwargs: Any) -> list[ProviderIdentity]:  # type: ignore[override]
            self.search_calls += 1
            if self.fail:
                self._record_network_failure(aiohttp.ClientConnectionError("x"))
                return []
            return [
                ProviderIdentity(
                    provider="flaky", external_id="1", title="It", authors=["SK"]
                )
            ]

    flaky = _Flaky()
    manager = _manager_with([flaky])
    audiobook = AudiobookSet(
        source_path=Path("/library/x"), raw_title_guess="It", author_guess="SK"
    )
    threshold = ProviderManager.CIRCUIT_BREAKER_THRESHOLD
    with patch.object(manager, "get_enabled_providers", return_value=[flaky]):
        for _ in range(threshold - 1):
            with pytest.raises(MetadataError):
                asyncio.run(manager.find_matches_merged(audiobook))
        flaky.fail = False
        assert asyncio.run(manager.find_matches_merged(audiobook))
        flaky.fail = True
        for _ in range(threshold - 1):
            with pytest.raises(MetadataError):
                asyncio.run(manager.find_matches_merged(audiobook))

    # Failures were never consecutive enough to trip the breaker.
    assert flaky.search_calls == 2 * (threshold - 1) + 1


# ── 5. get-activation-bytes on Selenium 4.10+ ──


def test_activator_module_does_not_import_selenium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins
    import importlib

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "selenium" or name.startswith("selenium."):
            raise ImportError("No module named 'selenium'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "bookbot.drm.activator", raising=False)

    module = importlib.import_module("bookbot.drm.activator")

    assert callable(module.extract_activation_bytes)


def test_get_activation_bytes_passes_chromedriver_via_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bookbot.cli import cli

    created: dict[str, Any] = {}

    class _Stop(Exception):
        pass

    class _Service:
        def __init__(self, executable_path: str | None = None, **kwargs: Any):
            self.executable_path = executable_path

    class _Options:
        def __init__(self) -> None:
            self.arguments: list[str] = []

        def add_argument(self, argument: str) -> None:
            self.arguments.append(argument)

    class _Chrome:
        def __init__(self, options: Any = None, service: Any = None, **kwargs: Any):
            if "executable_path" in kwargs:
                raise TypeError(
                    "WebDriver.__init__() got an unexpected keyword argument "
                    "'executable_path'"
                )
            created["service"] = service

        def __enter__(self) -> "_Chrome":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, url: str) -> None:
            raise _Stop()

    selenium = types.ModuleType("selenium")
    webdriver = types.ModuleType("selenium.webdriver")
    webdriver.Chrome = _Chrome  # type: ignore[attr-defined]
    webdriver.ChromeOptions = _Options  # type: ignore[attr-defined]
    chrome = types.ModuleType("selenium.webdriver.chrome")
    service = types.ModuleType("selenium.webdriver.chrome.service")
    service.Service = _Service  # type: ignore[attr-defined]
    common = types.ModuleType("selenium.webdriver.common")
    by = types.ModuleType("selenium.webdriver.common.by")
    by.By = types.SimpleNamespace(ID="id")  # type: ignore[attr-defined]
    selenium.webdriver = webdriver  # type: ignore[attr-defined]
    for name, module in {
        "selenium": selenium,
        "selenium.webdriver": webdriver,
        "selenium.webdriver.chrome": chrome,
        "selenium.webdriver.chrome.service": service,
        "selenium.webdriver.common": common,
        "selenium.webdriver.common.by": by,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    result = CliRunner().invoke(
        cli,
        [
            "audible",
            "get-activation-bytes",
            "--username",
            "user@example.com",
            "--password",
            "secret",
        ],
    )

    assert "unexpected keyword argument" not in result.output
    assert isinstance(created.get("service"), _Service)
    assert created["service"].executable_path


def test_provider_that_cannot_search_does_not_mask_an_outage() -> None:
    """Audnexus only resolves ASINs; with no ASIN it never touches the network,
    so its empty answer must not turn "everything is down" into "no matches"."""
    dead_audible = _UnreachableProvider("audible")
    dead_openlibrary = _UnreachableProvider("openlibrary")
    audnexus = AudnexusProvider()
    providers: list[MetadataProvider] = [audnexus, dead_audible, dead_openlibrary]
    manager = _manager_with(providers)
    audiobook = AudiobookSet(
        source_path=Path("/library/x"),
        raw_title_guess="Rock Paper Scissors",
        author_guess="Alice Feeney",
    )

    with (
        patch.object(manager, "get_enabled_providers", return_value=providers),
        patch.object(audnexus, "_request", new=AsyncMock()) as request,
    ):
        with pytest.raises(MetadataError, match="providers unavailable"):
            asyncio.run(manager.find_matches_merged(audiobook))

    request.assert_not_awaited()


def test_cli_match_reports_outage_instead_of_no_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bookbot.cli import cli

    book = tmp_path / "Alice Feeney - Rock Paper Scissors"
    book.mkdir()
    (book / "book.m4b").touch()

    async def all_down(self: ProviderManager, audiobook_set: Any, limit: int = 10):  # type: ignore[no-untyped-def]
        raise MetadataError("All metadata providers unavailable")

    monkeypatch.setattr(ProviderManager, "find_matches_merged", all_down)

    result = CliRunner().invoke(
        cli, ["--config-dir", str(tmp_path / "cfg"), "match", str(book)]
    )

    assert "No matches found" not in result.output
    assert "unavailable" in result.output
    # The query shown is the one sent: title plus author.
    assert "Query: Rock Paper Scissors / author: Alice Feeney" in result.output
