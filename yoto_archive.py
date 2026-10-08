"""Back up the downloadable assets embedded in a Yoto card's share page."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
from http.client import HTTPException
import json
import math
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urldefrag, urlsplit
from urllib.request import Request, urlopen


class ArchiveError(Exception):
    """An invalid card page, unavailable asset, or incomplete download."""


class _PageDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capturing = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.capturing = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.capturing = False

    def handle_data(self, data):
        if self.capturing:
            self.parts.append(data)


def card_from_html(html: str) -> dict:
    parser = _PageDataParser()
    parser.feed(html)
    if not parser.parts:
        raise ArchiveError("No Yoto card data found. Use the full NFC/share URL, including its query string.")
    try:
        data = json.loads("".join(parser.parts))
        card = data["props"]["pageProps"]["card"]
    except (ValueError, KeyError, TypeError):
        raise ArchiveError("The page does not contain valid Yoto card data.") from None
    if not isinstance(card, dict) or not isinstance(card.get("title"), str) or not card["title"].strip():
        raise ArchiveError("The card data is missing its title.")
    content = card.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("chapters"), list) or not content["chapters"]:
        raise ArchiveError("The card has no downloadable chapters.")
    track_count = 0
    for chapter in content["chapters"]:
        if not isinstance(chapter, dict) or not isinstance(chapter.get("tracks"), list) or not chapter["tracks"]:
            raise ArchiveError("A card chapter has no downloadable tracks.")
        for track in chapter["tracks"]:
            track_count += 1
            if not isinstance(track, dict) or not track.get("trackUrl"):
                raise ArchiveError(f"Track {track_count} has no download URL. Streaming-only content is not supported.")
            _web_url(track["trackUrl"])
    return card


def _web_url(value: str) -> str:
    if not isinstance(value, str):
        raise ArchiveError("Expected an HTTP or HTTPS URL.")
    try:
        parts = urlsplit(value)
        valid = parts.scheme in ("http", "https") and parts.hostname and not parts.username and not parts.password
        parts.port  # Also reject malformed ports.
    except ValueError:
        valid = False
    if not valid:
        raise ArchiveError("Expected an HTTP or HTTPS URL without embedded credentials.")
    return urldefrag(value)[0]


@contextmanager
def _response(url: str, timeout: float, label: str):
    request = Request(_web_url(url), headers={"User-Agent": "yoto-archive/0.1"})
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as exc:
        hint = " The link may have expired; rerun with the full NFC URL." if exc.code in (401, 403) else ""
        raise ArchiveError(f"Could not download {label}: HTTP {exc.code}.{hint}") from None
    except (URLError, OSError, HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        # Never include URLs or credentials in network errors.
        raise ArchiveError(f"Could not download {label}: {type(reason).__name__}. Check the connection and retry.") from None
    with response:
        yield response


def _read(response, size: int, label: str) -> bytes:
    try:
        return response.read(size)
    except (OSError, HTTPException) as exc:
        raise ArchiveError(f"Could not download {label}: {type(exc).__name__}. Check the connection and retry.") from None


def fetch_card(url: str, timeout: float = 30) -> dict:
    with _response(url, timeout, "card page") as response:
        html = _read(response, -1, "card page").decode(response.headers.get_content_charset() or "utf-8")
    return card_from_html(html)


def safe_name(value: str, fallback: str = "Untitled") -> str:
    """Keep readable titles while producing a portable single path component."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value.encode("utf-8")[:160].decode("utf-8", errors="ignore").rstrip(" .")
    if not value:
        return fallback
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value):
        value = "_" + value
    return value


_EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg",
    "audio/mp4": ".m4a", "video/mp4": ".m4a", "audio/x-m4a": ".m4a",
    "audio/aac": ".aac", "audio/aacp": ".aac",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "audio/ogg": ".ogg", "application/ogg": ".ogg",
    "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/flac": ".flac", "audio/x-flac": ".flac",
}


def _extension(content_type: str, url: str, audio_format: str | None) -> str:
    if content_type in _EXTENSIONS:
        return _EXTENSIONS[content_type]
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in set(_EXTENSIONS.values()):
        return suffix
    if isinstance(audio_format, str) and audio_format.lower() in ("aac", "m4a", "mp3", "ogg", "wav", "flac"):
        return "." + audio_format.lower()
    return ".bin"


def _download(url: str, root: Path, stem: str, timeout: float, audio_format: str | None = None,
              expected_size: int | None = None) -> dict:
    with _response(url, timeout, stem) as response:
        content_type = response.headers.get_content_type() if response.headers.get("Content-Type") else "application/octet-stream"
        if content_type in ("text/html", "application/json", "text/plain", "application/vnd.apple.mpegurl",
                            "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl", "application/dash+xml"):
            raise ArchiveError(f"Expected a media file for {stem}, but the server returned {content_type}.")
        relative = stem + _extension(content_type, url, audio_format)
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with path.open("wb") as target:
            while chunk := _read(response, 1024 * 1024, stem):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if size == 0:
            raise ArchiveError(f"Downloaded an empty file for {stem}.")
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and size != int(length):
            raise ArchiveError(f"Incomplete download for {stem}: expected {length} bytes, received {size}.")
        if isinstance(expected_size, int) and expected_size > 0 and size != expected_size:
            raise ArchiveError(f"Size mismatch for {stem}: expected {expected_size} bytes, received {size}.")
        expected_hash = parse_qs(urlsplit(url).fragment).get("sha256", [None])[0]
        actual_hash = base64.urlsafe_b64encode(digest.digest()).decode().rstrip("=")
        if expected_hash and expected_hash.rstrip("=") not in (actual_hash, digest.hexdigest()):
            raise ArchiveError(f"SHA-256 checksum mismatch for {stem}.")
    return {"path": relative, "size": size, "sha256": digest.hexdigest()}


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _icon(item: dict):
    return _object(item.get("display")).get("icon16x16")


def backup_card(url: str, output: Path | str = "archives", timeout: float = 30, progress=print) -> Path:
    card = fetch_card(url, timeout)
    output = Path(output)
    destination = output / safe_name(card["title"])
    if destination.exists() or destination.is_symlink():
        raise ArchiveError(f"Archive already exists: {destination}. Choose a different --output directory.")
    output.mkdir(parents=True, exist_ok=True)
    content = card["content"]
    metadata = _object(card.get("metadata"))
    # Only retain descriptive metadata, not user identifiers or access URLs.
    metadata_fields = ("author", "authors", "description", "category", "languages", "copyright",
                       "copyrights", "genres", "accent", "accents", "minAge", "maxAge", "abridged")
    manifest = {
        "archive_version": 1,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "card": {"id": card.get("cardId"), "title": card["title"],
                 "metadata": {key: metadata[key] for key in metadata_fields if key in metadata}},
        "content": {"version": content.get("version"), "playback_type": content.get("playbackType"),
                    "cover": None, "chapters": []},
    }
    if progress:
        progress(f"Backing up {card['title']}")
    with tempfile.TemporaryDirectory(prefix=".yoto-archive-", dir=output) as temporary:
        root = Path(temporary) / "card"
        root.mkdir()
        cover = _object(metadata.get("cover")).get("imageL") or _object(content.get("cover")).get("imageL")
        if cover:
            if progress:
                progress("Downloading cover")
            manifest["content"]["cover"] = _download(cover, root, "cover", timeout)
        number = 0
        for chapter in content["chapters"]:
            saved_chapter = {"key": chapter.get("key"), "title": chapter.get("title"), "tracks": []}
            manifest["content"]["chapters"].append(saved_chapter)
            for track in chapter["tracks"]:
                number += 1
                title = track.get("title") or chapter.get("title") or f"Track {number}"
                if not isinstance(title, str):
                    raise ArchiveError(f"Track {number} has an invalid title.")
                stem = f"{number:03d} - {safe_name(title)}"
                if progress:
                    progress(f"Downloading track {number}: {title.strip()}")
                audio = _download(track["trackUrl"], root, f"tracks/{stem}", timeout,
                                  audio_format=track.get("format"), expected_size=track.get("fileSize"))
                icon_url = _icon(track) or _icon(chapter)
                icon = _download(icon_url, root, f"icons/{stem}", timeout) if icon_url else None
                saved_track = {key: track[key] for key in ("key", "format", "type", "duration", "channels") if key in track}
                saved_track.update({"title": title, "audio": audio, "icon": icon})
                saved_chapter["tracks"].append(saved_track)
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if destination.exists() or destination.is_symlink():
            raise ArchiveError(f"Archive already exists: {destination}. Choose a different --output directory.")
        root.rename(destination)
    return destination


def _timeout(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive number")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up a Yoto card's cover, track icons, and audio.")
    parser.add_argument("url", help="full NFC/share URL, including its query string")
    parser.add_argument("--output", "-o", type=Path, default=Path("archives"), help="parent directory for archives (default: archives)")
    parser.add_argument("--timeout", type=_timeout, default=30, help="network timeout in seconds (default: 30)")
    args = parser.parse_args(argv)
    try:
        destination = backup_card(args.url, args.output, args.timeout)
    except (ArchiveError, OSError, UnicodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Backup interrupted.", file=sys.stderr)
        return 130
    print(f"Saved archive to {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
