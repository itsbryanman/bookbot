"""Sidecar metadata file reader/writer for OPF, JSON, and NFO formats."""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from ..core.models import ProviderIdentity

logger = get_logger("sidecar")

# XML namespaces for OPF
NS_OPF = "http://www.idpf.org/2007/opf"
NS_DC = "http://purl.org/dc/elements/1.1/"


def _coerce_scalar(value: Any) -> str | None:
    """Reduce a loosely-typed sidecar JSON value to a clean string or None.

    Real-world metadata.json files (Audiobookshelf, BookLore, Calibre
    exports) are not strictly typed: `series` may be `[]`, a bare string, or
    a list of objects like `[{"name": "...", "sequence": "1"}]`; `narrator`
    may be a list; `isbn` may be a number. Every one of those shapes used to
    raise past the reader's except clause and abort the whole command.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "title", "value", "series"):
            nested = _coerce_scalar(value.get(key))
            if nested is not None:
                return nested
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            nested = _coerce_scalar(item)
            if nested is not None:
                return nested
        return None
    return None


def _coerce_str_list(value: Any) -> list[str]:
    """Reduce a sidecar JSON value to a list of clean strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        results = []
        for item in value:
            coerced = _coerce_scalar(item)
            if coerced is not None:
                results.append(coerced)
        return results
    coerced = _coerce_scalar(value)
    return [coerced] if coerced is not None else []


def _coerce_int(value: Any) -> int | None:
    """Reduce a sidecar JSON value to an int, tolerating strings like '2013'."""
    coerced = _coerce_scalar(value)
    if coerced is None:
        return None
    match = re.search(r"-?\d+", coerced)
    if match is None:
        return None
    try:
        return int(match.group())
    except ValueError:
        return None


class SidecarManager:
    """Reads and writes sidecar metadata files."""

    def read_opf(self, path: Path) -> ProviderIdentity | None:
        """Parse an OPF/XML metadata file (Calibre/ABS format)."""
        try:
            tree = ET.parse(path)
            root = tree.getroot()

            # Handle namespace
            metadata = root.find(f"{{{NS_OPF}}}metadata")
            if metadata is None:
                metadata = root.find("metadata")
            if metadata is None:
                return None

            title = self._find_dc_text(metadata, "title")
            if not title:
                return None

            authors = []
            for creator in metadata.findall(f"{{{NS_DC}}}creator"):
                text = creator.text
                if text:
                    authors.append(text.strip())
            if not authors:
                for creator in metadata.findall("creator"):
                    text = creator.text
                    if text:
                        authors.append(text.strip())

            language = self._find_dc_text(metadata, "language")
            description = self._find_dc_text(metadata, "description")
            publisher = self._find_dc_text(metadata, "publisher")
            date_str = self._find_dc_text(metadata, "date")

            year = None
            if date_str:
                match = re.search(r"\b(19|20)\d{2}\b", date_str)
                if match:
                    year = int(match.group())

            # Extract identifiers (ISBN, ASIN)
            isbn_10 = None
            isbn_13 = None
            asin = None
            for identifier in metadata.findall(f"{{{NS_DC}}}identifier"):
                scheme = identifier.get(f"{{{NS_OPF}}}scheme", "").upper()
                text = (identifier.text or "").strip()
                if scheme == "ISBN" or scheme == "ISBN13":
                    clean = re.sub(r"[^0-9X]", "", text.upper())
                    if len(clean) == 13:
                        isbn_13 = clean
                    elif len(clean) == 10:
                        isbn_10 = clean
                elif scheme == "ASIN":
                    asin = text

            # Extract meta elements (series, narrator)
            series_name = None
            series_index = None
            narrator = None
            for meta in metadata.findall(f"{{{NS_OPF}}}meta"):
                name = meta.get("name", "")
                content = meta.get("content", "")
                if name == "calibre:series":
                    series_name = content
                elif name == "calibre:series_index":
                    series_index = content
                elif name == "calibre:narrator":
                    narrator = content
            # Also check without namespace
            for meta in metadata.findall("meta"):
                name = meta.get("name", "")
                content = meta.get("content", "")
                if name == "calibre:series":
                    series_name = content
                elif name == "calibre:series_index":
                    series_index = content
                elif name == "calibre:narrator":
                    narrator = content

            return ProviderIdentity(
                provider="sidecar_opf",
                external_id=str(path),
                title=title,
                authors=authors,
                series_name=series_name,
                series_index=series_index,
                year=year,
                language=language,
                narrator=narrator,
                publisher=publisher,
                isbn_10=isbn_10,
                isbn_13=isbn_13,
                asin=asin,
                description=description,
            )

        except (ET.ParseError, OSError) as e:
            logger.warning(f"Failed to parse OPF file {path}: {e}")
            return None
        except Exception as e:
            # A malformed sidecar must degrade to "no metadata", never abort
            # the surrounding command.
            logger.warning(f"Ignoring unusable OPF file {path}: {e}")
            return None

    def write_opf(self, path: Path, identity: ProviderIdentity) -> None:
        """Write metadata as OPF/XML (Calibre/ABS format)."""
        root = ET.Element("package")
        root.set("xmlns", NS_OPF)
        root.set("version", "2.0")

        metadata = ET.SubElement(root, "metadata")
        metadata.set("xmlns:dc", NS_DC)
        metadata.set("xmlns:opf", NS_OPF)

        # Title
        dc_title = ET.SubElement(metadata, "dc:title")
        dc_title.text = identity.title

        # Authors
        for author in identity.authors:
            dc_creator = ET.SubElement(metadata, "dc:creator")
            dc_creator.set("opf:role", "aut")
            dc_creator.text = author

        # Language
        if identity.language:
            dc_lang = ET.SubElement(metadata, "dc:language")
            dc_lang.text = identity.language

        # Description
        if identity.description:
            dc_desc = ET.SubElement(metadata, "dc:description")
            dc_desc.text = identity.description

        # Publisher
        if identity.publisher:
            dc_pub = ET.SubElement(metadata, "dc:publisher")
            dc_pub.text = identity.publisher

        # Date/Year
        if identity.year:
            dc_date = ET.SubElement(metadata, "dc:date")
            dc_date.text = str(identity.year)

        # ISBN
        if identity.isbn_13:
            dc_id = ET.SubElement(metadata, "dc:identifier")
            dc_id.set("opf:scheme", "ISBN")
            dc_id.text = identity.isbn_13
        elif identity.isbn_10:
            dc_id = ET.SubElement(metadata, "dc:identifier")
            dc_id.set("opf:scheme", "ISBN")
            dc_id.text = identity.isbn_10

        # ASIN
        if identity.asin:
            dc_id = ET.SubElement(metadata, "dc:identifier")
            dc_id.set("opf:scheme", "ASIN")
            dc_id.text = identity.asin

        # Series
        if identity.series_name:
            meta_series = ET.SubElement(metadata, "meta")
            meta_series.set("name", "calibre:series")
            meta_series.set("content", identity.series_name)

        if identity.series_index:
            meta_idx = ET.SubElement(metadata, "meta")
            meta_idx.set("name", "calibre:series_index")
            meta_idx.set("content", identity.series_index)

        # Narrator
        if identity.narrator:
            meta_nar = ET.SubElement(metadata, "meta")
            meta_nar.set("name", "calibre:narrator")
            meta_nar.set("content", identity.narrator)

        tree = ET.ElementTree(root)
        ET.indent(tree, space="  ")
        tree.write(path, encoding="utf-8", xml_declaration=True)

    def read_metadata_json(self, path: Path) -> ProviderIdentity | None:
        """Parse a BookLore-style .metadata.json file."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                logger.warning(
                    f"Ignoring metadata JSON {path}: expected an object at the "
                    "top level"
                )
                return None

            title = _coerce_scalar(data.get("title"))
            if not title:
                return None

            authors = _coerce_str_list(data.get("authors"))

            isbn = _coerce_scalar(data.get("isbn"))
            isbn_13 = None
            isbn_10 = None
            if isbn:
                clean = re.sub(r"[^0-9X]", "", isbn.upper())
                if len(clean) == 13:
                    isbn_13 = clean
                elif len(clean) == 10:
                    isbn_10 = clean

            cover_url = _coerce_scalar(data.get("coverUrl"))

            return ProviderIdentity(
                provider="sidecar_json",
                external_id=str(path),
                title=title,
                authors=authors,
                series_name=_coerce_scalar(data.get("series")),
                series_index=_coerce_scalar(data.get("seriesIndex")),
                year=_coerce_int(data.get("year")),
                language=_coerce_scalar(data.get("language")),
                narrator=_coerce_scalar(data.get("narrator")),
                publisher=_coerce_scalar(data.get("publisher")),
                isbn_10=isbn_10,
                isbn_13=isbn_13,
                asin=_coerce_scalar(data.get("asin")),
                description=_coerce_scalar(data.get("description")),
                cover_urls=[cover_url] if cover_url else [],
            )

        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to parse metadata JSON {path}: {e}")
            return None
        except Exception as e:
            # A malformed sidecar must degrade to "no metadata", never abort
            # the surrounding command.
            logger.warning(f"Ignoring unusable metadata JSON {path}: {e}")
            return None

    def write_metadata_json(self, path: Path, identity: ProviderIdentity) -> None:
        """Write metadata as BookLore-style JSON."""
        data: dict[str, Any] = {
            "title": identity.title,
            "authors": identity.authors,
        }

        if identity.series_name:
            data["series"] = identity.series_name
        if identity.series_index:
            data["seriesIndex"] = identity.series_index
        if identity.narrator:
            data["narrator"] = identity.narrator
        if identity.year:
            data["year"] = identity.year
        if identity.isbn_13:
            data["isbn"] = identity.isbn_13
        elif identity.isbn_10:
            data["isbn"] = identity.isbn_10
        if identity.asin:
            data["asin"] = identity.asin
        if identity.description:
            data["description"] = identity.description
        if identity.publisher:
            data["publisher"] = identity.publisher
        if identity.language:
            data["language"] = identity.language
        if identity.cover_urls:
            data["coverUrl"] = identity.cover_urls[0]

        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def read_nfo(self, path: Path) -> ProviderIdentity | None:
        """Parse an audiobook .nfo file (simple key=value or XML)."""
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        # Try XML first
        try:
            tree = ET.fromstring(content)
            title = self._xml_text(tree, "title")
            if title:
                authors = []
                author_text = self._xml_text(tree, "author") or self._xml_text(
                    tree, "artist"
                )
                if author_text:
                    authors = [a.strip() for a in author_text.split(",")]

                year = None
                year_text = self._xml_text(tree, "year")
                if year_text:
                    try:
                        year = int(year_text)
                    except ValueError:
                        pass

                return ProviderIdentity(
                    provider="sidecar_nfo",
                    external_id=str(path),
                    title=title,
                    authors=authors,
                    year=year,
                    narrator=self._xml_text(tree, "narrator"),
                    publisher=self._xml_text(tree, "publisher"),
                    description=self._xml_text(tree, "description")
                    or self._xml_text(tree, "plot"),
                )
        except ET.ParseError:
            pass

        # Fallback: key=value parsing
        fields: dict[str, str] = {}
        for line in content.splitlines():
            line = line.strip()
            if ":" in line:
                key, _, value = line.partition(":")
                fields[key.strip().lower()] = value.strip()
            elif "=" in line:
                key, _, value = line.partition("=")
                fields[key.strip().lower()] = value.strip()

        title = fields.get("title", "")
        if not title:
            return None

        authors = []
        author_str = fields.get("author", fields.get("artist", ""))
        if author_str:
            authors = [a.strip() for a in author_str.split(",")]

        year = None
        if "year" in fields:
            try:
                year = int(fields["year"])
            except ValueError:
                pass

        return ProviderIdentity(
            provider="sidecar_nfo",
            external_id=str(path),
            title=title,
            authors=authors,
            year=year,
            narrator=fields.get("narrator"),
            publisher=fields.get("publisher"),
            description=fields.get("description", fields.get("plot")),
        )

    def auto_detect_sidecar(self, directory: Path) -> ProviderIdentity | None:
        """Check for metadata.opf, .metadata.json, audiobook.nfo in order."""
        # OPF files
        for name in ["metadata.opf", "content.opf"]:
            opf_path = directory / name
            if opf_path.exists():
                result = self.read_opf(opf_path)
                if result:
                    return result

        # JSON metadata
        for name in [".metadata.json", "metadata.json"]:
            json_path = directory / name
            if json_path.exists():
                result = self.read_metadata_json(json_path)
                if result:
                    return result

        # NFO files
        for nfo_path in directory.glob("*.nfo"):
            result = self.read_nfo(nfo_path)
            if result:
                return result

        return None

    def _find_dc_text(self, metadata: ET.Element, tag: str) -> str | None:
        """Find Dublin Core element text."""
        elem = metadata.find(f"{{{NS_DC}}}{tag}")
        if elem is not None and elem.text:
            return elem.text.strip()
        elem = metadata.find(tag)
        if elem is not None and elem.text:
            return elem.text.strip()
        return None

    def _xml_text(self, root: ET.Element, tag: str) -> str | None:
        """Get text from an XML element by tag name."""
        elem = root.find(tag)
        if elem is not None and elem.text:
            return elem.text.strip()
        return None
