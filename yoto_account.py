"""Paced library discovery and incremental account archives, using only stdlib."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import random
import re
import shutil
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import uuid

import yoto_archive as core


API = "https://api.yotoplay.com"
SOURCES = ("/card/family/library", "/content/mine")
METADATA_FIELDS = ("author", "authors", "description", "category", "languages", "copyright",
                   "copyrights", "genres", "accent", "accents", "minAge", "maxAge", "abridged")


class RequestPolicy:
    """One request at a time, with pacing and bounded transient-error retries."""

    def __init__(self, interval=1, retries=3, *, clock=None, sleep=None, jitter=None):
        self.interval = interval
        self.retries = retries
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.jitter = jitter or (lambda: random.uniform(0, 1))
        self.next_request = 0.0

    def _wait(self):
        delay = self.next_request - self.clock()
        if delay > 0:
            self.sleep(delay)
        self.next_request = self.clock() + self.interval

    def _retry_after(self, value):
        try:
            delay = float(value)
            return max(0, delay) if math.isfinite(delay) else 0
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(value)
                return max(0, date.timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                return 0

    def open(self, request, timeout):
        for attempt in range(self.retries + 1):
            self._wait()
            try:
                return core.urlopen(request, timeout=timeout)
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise
                delay = max(self._retry_after(exc.headers.get("Retry-After") if exc.headers else None),
                            2 ** attempt + self.jitter())
                exc.close()
            except (URLError, TimeoutError, ConnectionError):
                if attempt == self.retries:
                    raise
                delay = 2 ** attempt + self.jitter()
            self.next_request = max(self.next_request, self.clock() + delay)


def _subject(token):
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject or len(subject) > 256:
            raise ValueError
        if "exp" in claims and float(claims["exp"]) <= time.time():
            raise core.ArchiveError("The account token has expired. Refresh --token-file through your Yoto login.")
        return subject
    except (ValueError, KeyError, TypeError, IndexError):
        raise core.ArchiveError("Account backup needs a Yoto JWT access token with an account subject.") from None


def _json_get(route, token, timeout, policy):
    with core._response(API + route, timeout, "account data", token, policy=policy) as response:
        try:
            return json.loads(core._read(response, -1, "account data"))
        except (ValueError, UnicodeError):
            raise core.ArchiveError("The account API returned invalid JSON.") from None


def _entry(entry, source):
    if not isinstance(entry, dict):
        raise core.ArchiveError("The library contains an invalid card entry.")
    nested = core._object(entry.get("card"))
    identifier = entry.get("cardId") or entry.get("contentId") or nested.get("cardId")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
        raise core.ArchiveError("The library contains a card without a valid ID.")
    return {"id": identifier, "title": nested.get("title") or entry.get("title") or identifier,
            "sources": [source]}


def discover(token, timeout, policy):
    """Merge family-library and owned MYO entries; never hide an incomplete list."""
    entries, errors = {}, []
    for source in SOURCES:
        route, visited, source_count = source, set(), 0
        try:
            while route:
                if route in visited:
                    raise core.ArchiveError("The API returned a pagination loop.")
                visited.add(route)
                data = _json_get(route, token, timeout, policy)
                if not isinstance(data, dict) or not isinstance(data.get("cards"), list):
                    raise core.ArchiveError("The API did not return a cards list.")
                for item in data["cards"]:
                    entry = _entry(item, source)
                    source_count += 1
                    if core._object(item).get("deleted"):
                        continue
                    if entry["id"] in entries:
                        if source not in entries[entry["id"]]["sources"]:
                            entries[entry["id"]]["sources"].append(source)
                    else:
                        entries[entry["id"]] = entry
                pagination = core._object(data.get("pagination"))
                next_page = data.get("next") or core._object(data.get("links")).get("next") or pagination.get("next")
                if next_page:
                    if not isinstance(next_page, str):
                        raise core.ArchiveError("Unsupported library pagination; enumeration is incomplete.")
                    parts = urlsplit(urljoin(API + route, next_page))
                    if parts.scheme != "https" or parts.netloc != "api.yotoplay.com" or parts.path != source:
                        raise core.ArchiveError("Refused a pagination URL outside the library API.")
                    route = parts.path + ("?" + parts.query if parts.query else "")
                else:
                    total = data.get("total", pagination.get("total"))
                    if (data.get("hasMore") or pagination.get("hasMore") or data.get("nextPageToken")
                            or data.get("nextToken") or data.get("nextPage")
                            or data.get("nextCursor") or pagination.get("nextToken")
                            or pagination.get("nextPageToken") or pagination.get("nextCursor")
                            or isinstance(total, int) and total > source_count):
                        raise core.ArchiveError("Unsupported library pagination; enumeration is incomplete.")
                    route = None
        except (core.ArchiveError, ValueError) as exc:
            errors.append(f"{source}: {exc}")
            if isinstance(exc, core.ArchiveError) and exc.status_code in (401, 403, 429):
                break
    return {"cards": list(entries.values()), "complete": not errors, "errors": errors}


def _safe_path(root, relative):
    if not isinstance(relative, str):
        raise core.ArchiveError("Invalid path in the archive manifest.")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or not parts.parts or ".." in parts.parts or "\\" in relative or ":" in relative:
        raise core.ArchiveError("Unsafe path in the archive manifest.")
    path = root
    for part in parts.parts:
        path = path / part
        if path.is_symlink():
            raise core.ArchiveError("Refused a symlink inside the account archive.")
    return path


def _load(path):
    try:
        if path.is_symlink():
            raise core.ArchiveError("Refused a symlinked archive manifest.")
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise core.ArchiveError(f"Invalid archive manifest: {path.name}. Preserve or repair it before updating.") from None


def _write_json(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(data, target, indent=2, ensure_ascii=False)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _lock(root):
    path = root / ".account-lock"
    try:
        path.mkdir()
    except FileExistsError:
        raise core.ArchiveError("Another account backup may be running. If a previous process was killed, remove .account-lock after checking.") from None
    try:
        yield
    finally:
        path.rmdir()


def _asset_records(manifest):
    if not isinstance(manifest, dict) or not isinstance(manifest.get("content"), dict):
        raise core.ArchiveError("Invalid card manifest in the account archive.")
    content = manifest["content"]
    if not isinstance(content.get("chapters"), list):
        raise core.ArchiveError("Invalid chapters in the account archive manifest.")
    yield content.get("cover")
    for chapter in content.get("chapters", []):
        if not isinstance(chapter, dict) or not isinstance(chapter.get("tracks"), list):
            raise core.ArchiveError("Invalid chapter in the account archive manifest.")
        yield chapter.get("icon")
        for track in chapter.get("tracks", []):
            if not isinstance(track, dict):
                raise core.ArchiveError("Invalid track in the account archive manifest.")
            yield track.get("audio")
            yield track.get("icon")


def _add_cache(cache, root, manifest):
    for asset in _asset_records(manifest):
        if isinstance(asset, dict) and isinstance(asset.get("source_id"), str):
            path = _safe_path(root, asset.get("path"))
            cache.setdefault(asset["source_id"], []).append((path, asset))


def _verified(path, asset):
    if not path.is_file() or path.is_symlink() or path.stat().st_size != asset.get("size"):
        return False
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest() == asset.get("sha256")


def _unsigned(url):
    parts = urlsplit(core._web_url(url))
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("x-amz-") and key.lower() not in ("expires", "signature", "key-pair-id", "policy")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _source_id(url, revision):
    core._web_url(url)
    expected = parse_qs(urlsplit(url).fragment).get("sha256", [None])[0]
    if expected:
        return "sha256:" + expected.rstrip("=")
    unsigned = _unsigned(url)
    # Yoto's image paths are content-addressed, even without a hash fragment.
    parts = urlsplit(unsigned)
    address = parts.path.rsplit("/", 1)[-1]
    stable = (parts.hostname == "card-content.yotoplay.com" and parts.path.startswith("/yoto/")
              and re.fullmatch(r"[A-Za-z0-9_-]{43}", address))
    return "url:" + hashlib.sha256((unsigned + ("" if stable else "|" + revision)).encode()).hexdigest()


def _asset(url, root, stem, revision, cache, timeout, policy, audio_format=None, expected_size=None):
    source_id = _source_id(url, revision)
    for previous_path, previous in cache.get(source_id, []):
        if expected_size and previous.get("size") != expected_size:
            continue
        if _verified(previous_path, previous):
            suffix = previous_path.suffix if previous_path.suffix in set(core._EXTENSIONS.values()) | {".bin"} else ".bin"
            relative = stem + suffix
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(previous_path, target)
            except OSError:
                shutil.copyfile(previous_path, target)
            return dict(previous, path=relative, source_id=source_id)
    asset = core._download(url, root, stem, timeout, audio_format, expected_size, policy=policy)
    asset["source_id"] = source_id
    cache.setdefault(source_id, []).append((root / asset["path"], asset))
    return asset


def _build_card(card, root, cache, timeout, policy):
    if not isinstance(card, dict) or not isinstance(card.get("title"), str) or not card["title"].strip():
        raise core.ArchiveError("The account card is missing a title.")
    content, metadata = core._object(card.get("content")), core._object(card.get("metadata"))
    chapters = content.get("chapters")
    if not isinstance(chapters, list):
        raise core.ArchiveError("The account card is missing its chapters list.")
    feed = metadata.get("feedUrl")
    podcast = bool(feed or core._object(content.get("editSettings")).get("podcastType"))
    url_only_card = podcast or metadata.get("category") in ("podcast", "podcasts", "radio")
    revision = json.dumps([content.get("version"), card.get("updatedAt")])
    manifest = {
        "archive_version": 2,
        "card": {"id": card.get("cardId"), "title": card["title"], "updated_at": card.get("updatedAt"),
                 "metadata": {key: metadata[key] for key in METADATA_FIELDS if key in metadata}},
        "content": {"version": content.get("version"), "playback_type": content.get("playbackType"),
                    "cover": None, "chapters": []},
    }
    if feed:
        manifest["content"]["feed_url"] = _unsigned(feed)
    cover = core._object(metadata.get("cover")).get("imageL") or core._object(content.get("cover")).get("imageL")
    if cover:
        manifest["content"]["cover"] = _asset(cover, root, "cover", revision, cache, timeout, policy)
    number, file_tracks, stream_tracks = 0, 0, 0
    for chapter_number, chapter in enumerate(chapters, 1):
        if not isinstance(chapter, dict) or not isinstance(chapter.get("tracks"), list):
            raise core.ArchiveError("The account card has an invalid chapter.")
        chapter_icon = core._icon(chapter)
        icon = _asset(chapter_icon, root, f"icons/chapter-{chapter_number:03d}", revision, cache, timeout, policy) if chapter_icon else None
        saved_chapter = {"key": chapter.get("key"), "title": chapter.get("title"), "icon": icon, "tracks": []}
        manifest["content"]["chapters"].append(saved_chapter)
        for track in chapter["tracks"]:
            number += 1
            if not isinstance(track, dict):
                raise core.ArchiveError("The account card has an invalid track.")
            title = track.get("title") or chapter.get("title") or f"Track {number}"
            if not isinstance(title, str):
                raise core.ArchiveError("The account card has an invalid track title.")
            stream = url_only_card or track.get("type") in ("stream", "podcast", "radio")
            if not stream and track.get("type") not in (None, "audio"):
                raise core.ArchiveError("The account card has an unsupported track type.")
            url = track.get("trackUrl") or (feed if stream else None)
            if not url:
                raise core.ArchiveError(f"Track {number} has no audio or stream URL.")
            saved = {key: track[key] for key in ("key", "format", "type", "duration", "channels") if key in track}
            saved.update(title=title, audio=None, icon=icon)
            if stream:
                saved["source"] = {"kind": "stream", "url": _unsigned(url)}
                stream_tracks += 1
            else:
                stem = f"tracks/{number:03d} - {core.safe_name(title)}"
                saved["audio"] = _asset(url, root, stem, revision, cache, timeout, policy,
                                        track.get("format"), track.get("fileSize"))
                file_tracks += 1
            own_icon = core._icon(track)
            if own_icon and own_icon != chapter_icon:
                saved["icon"] = _asset(own_icon, root, f"icons/track-{number:03d}", revision, cache, timeout, policy)
            saved_chapter["tracks"].append(saved)
    manifest["kind"] = "url_only" if not file_tracks and (stream_tracks or feed) else "files"
    return manifest


def _card_update(root, entry, previous, token, timeout, policy, cache):
    # Owned MYO IDs are content IDs, including playlists not linked to a card.
    # Family-only physical IDs use /card to resolve their linked content.
    resource = "content" if "/content/mine" in entry["sources"] else "card"
    data = _json_get(f"/{resource}/{entry['id']}?playable=true&signingType=s3", token, timeout, policy)
    card = data.get("card") if isinstance(data, dict) else None
    old_root, old_manifest = None, None
    if previous.get("path"):
        old_root = _safe_path(root, previous["path"])
        if (old_root / "manifest.json").is_file():
            old_manifest = _load(old_root / "manifest.json")
    with tempfile.TemporaryDirectory(prefix=".card-", dir=root) as temporary:
        staging = Path(temporary) / "card"
        staging.mkdir()
        local_cache = {key: list(values) for key, values in cache.items()}
        manifest = _build_card(card, staging, local_cache, timeout, policy)
        comparable = dict(old_manifest or {})
        comparable.pop("archived_at", None)
        if comparable == manifest and all(_verified(_safe_path(old_root, asset["path"]), asset)
                                          for asset in _asset_records(manifest) if asset):
            return dict(previous, title=card["title"], sources=entry["sources"], state="unchanged", error=None)
        manifest["archived_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(staging / "manifest.json", manifest)
        relative = f"cards/{entry['id']}/revisions/{uuid.uuid4().hex}"
        destination = _safe_path(root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(destination)
        _add_cache(cache, destination, manifest)
    return {"title": card["title"], "content_id": card.get("cardId"), "sources": entry["sources"],
            "path": relative, "kind": manifest["kind"], "state": "url_only" if manifest["kind"] == "url_only" else "archived",
            "last_success": manifest["archived_at"], "error": None}


def list_account(token, *, output=None, timeout=30, interval=1, progress=print, policy=None):
    _subject(token)
    listing = discover(token, timeout, policy or RequestPolicy(interval))
    if progress:
        for entry in listing["cards"]:
            progress(f"{entry['id']}  {entry['title']}")
        progress(f"Found {len(listing['cards'])} unique cards; enumeration {'complete' if listing['complete'] else 'incomplete'}.")
        for error in listing["errors"]:
            progress(error)
    return listing


def backup_account(token, *, output=None, timeout=30, interval=1, progress=print, policy=None):
    subject = _subject(token)
    root = Path(output) if output is not None else Path("archives") / core.safe_name(subject)
    root.mkdir(parents=True, exist_ok=True)
    index_path = root / "account.json"
    with _lock(root):
        if index_path.exists() or index_path.is_symlink():
            index = _load(index_path)
            if (not isinstance(index, dict) or index.get("archive_version") != 1
                    or not isinstance(index.get("cards"), dict)):
                raise core.ArchiveError("Unsupported account manifest. Preserve it before creating a new archive.")
            if core._object(index.get("account")).get("id") != subject:
                raise core.ArchiveError("This archive belongs to a different account. Choose another --output directory.")
        else:
            if any(path.name != ".account-lock" for path in root.iterdir()):
                raise core.ArchiveError("The target contains files without an account manifest. Choose an empty directory.")
            index = {"archive_version": 1, "account": {"id": subject}, "cards": {}}
        cache = {}
        for record in index["cards"].values():
            if not isinstance(record, dict):
                raise core.ArchiveError("Invalid card record in the account manifest.")
            if record.get("path"):
                card_root = _safe_path(root, record["path"])
                if (card_root / "manifest.json").is_file():
                    _add_cache(cache, card_root, _load(card_root / "manifest.json"))
        policy = policy or RequestPolicy(interval)
        listing = discover(token, timeout, policy)
        index["discovery"] = {"complete": listing["complete"], "errors": listing["errors"]}
        counts = {key: 0 for key in ("archived", "unchanged", "url_only", "unavailable", "failed")}
        observed = {entry["id"] for entry in listing["cards"]}
        index["complete"] = False
        _write_json(index_path, index)
        stop_reason = None
        for entry in listing["cards"]:
            previous = index["cards"].get(entry["id"], {})
            if progress:
                progress(f"Checking {entry['title']} ({entry['id']})")
            try:
                if stop_reason:
                    raise core.ArchiveError(stop_reason)
                record = _card_update(root, entry, previous, token, timeout, policy, cache)
            except (core.ArchiveError, OSError, UnicodeError) as exc:
                if isinstance(exc, core.ArchiveError) and exc.status_code in (401, 403, 429):
                    stop_reason = ("The API remained rate-limited; retry the account backup later." if exc.status_code == 429
                                   else "Account access was denied earlier; refresh --token-file and check permissions before retrying.")
                state = "unavailable" if isinstance(exc, core.ArchiveError) and exc.status_code == 404 else "failed"
                record = dict(previous, title=entry["title"], sources=entry["sources"], state=state, error=str(exc))
                if progress:
                    progress(f"{entry['id']}: {exc}")
            counts[record["state"]] += 1
            index["cards"][entry["id"]] = record
            index["updated_at"] = datetime.now(timezone.utc).isoformat()
            index["summary"] = counts
            _write_json(index_path, index)
        if listing["complete"]:
            for identifier, record in index["cards"].items():
                if identifier not in observed:
                    record["state"] = "not_in_library"
        index["complete"] = listing["complete"] and not counts["failed"] and not counts["unavailable"]
        index["updated_at"] = datetime.now(timezone.utc).isoformat()
        index["summary"] = counts
        _write_json(index_path, index)
    if progress:
        progress("; ".join(f"{key}: {value}" for key, value in counts.items()))
        for error in listing["errors"]:
            progress(error)
        progress(f"Saved {'complete' if index['complete'] else 'incomplete'} account archive to {root}")
    return index
