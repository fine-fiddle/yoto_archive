import base64
from contextlib import redirect_stderr, redirect_stdout
import copy
from email.message import Message
import hashlib
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag

import yoto_archive as archive


PAGE_URL = "https://yoto.io/test?card-key=card-secret"
IMAGE = b"synthetic image"
AUDIO = b"synthetic audio"
HASH = base64.urlsafe_b64encode(hashlib.sha256(AUDIO).digest()).decode().rstrip("=")
AUDIO_URL = f"https://media.example/audio?Signature=download-secret#sha256={HASH}"


def sample_card():
    return {
        "cardId": "test",
        "title": "Let's Race: é/..?",
        "userId": "private-user-id",
        "metadata": {
            "author": "An author", "languages": ["en"],
            "description": "A card with 'quotes' & symbols",
            "cover": {"imageL": "https://media.example/cover"},
            "audioPreviewUrl": "https://media.example/preview",
        },
        "content": {
            "version": "1", "playbackType": "linear",
            "chapters": [{
                "key": "chapter-1", "title": "Chapter one",
                "display": {"icon16x16": "https://media.example/icon"},
                "tracks": [{"key": "track-1", "title": "First / track", "format": "aac",
                            "duration": 10, "type": "audio", "fileSize": len(AUDIO),
                            "trackUrl": AUDIO_URL}],
            }],
        },
    }


def page(card):
    data = json.dumps({"props": {"pageProps": {"card": card}}}, ensure_ascii=False)
    return f'<html><script>ignored</script><script type="application/json" id="__NEXT_DATA__">{data}</script></html>'


class FakeResponse(io.BytesIO):
    def __init__(self, body, content_type, length=None):
        super().__init__(body)
        self.headers = Message()
        if content_type:
            self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(body) if length is None else length)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "archives"
        self.card = sample_card()
        self.requests = []
        self.responses = []
        self.assets = {
            "https://media.example/cover": (IMAGE, "image/png"),
            "https://media.example/icon": (IMAGE, "image/png"),
            urldefrag(AUDIO_URL)[0]: (AUDIO, "audio/mp4"),
        }
        self.patcher = patch.object(archive, "urlopen", self.urlopen)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def urlopen(self, request, timeout):
        self.requests.append((request.full_url, timeout))
        if request.full_url == PAGE_URL:
            response = FakeResponse(page(self.card).encode(), "text/html; charset=utf-8")
        else:
            value = self.assets[request.full_url]
            if isinstance(value, BaseException):
                raise value
            response = FakeResponse(*value)
        self.responses.append(response)
        return response

    def backup(self):
        return archive.backup_card(PAGE_URL, self.output, timeout=7, progress=None)

    def manifest(self, path):
        return json.loads((path / "manifest.json").read_text(encoding="utf-8"))

    def test_complete_archive_preserves_order_formats_and_metadata(self):
        second = copy.deepcopy(self.card["content"]["chapters"][0])
        second["key"] = "chapter-2"
        second["tracks"][0]["key"] = "track-2"
        second["tracks"][0]["title"] = "First / track"
        self.card["content"]["chapters"][0]["tracks"].append(copy.deepcopy(second["tracks"][0]))
        self.card["content"]["chapters"].append(second)
        path = self.backup()
        self.assertEqual(path.name, "Let's Race_ é_.._")
        manifest = self.manifest(path)
        self.assertEqual(manifest["archive_version"], 1)
        self.assertEqual(manifest["card"]["title"], self.card["title"])
        self.assertEqual(manifest["card"]["metadata"]["author"], "An author")
        self.assertEqual((path / "cover.png").read_bytes(), IMAGE)
        tracks = [track for chapter in manifest["content"]["chapters"] for track in chapter["tracks"]]
        self.assertEqual(len(tracks), 3)
        for number, track in enumerate(tracks, 1):
            self.assertEqual(track["audio"]["path"], f"tracks/{number:03d} - First _ track.m4a")
            self.assertEqual(track["icon"]["path"], f"icons/{number:03d} - First _ track.png")
            for kind, data in (("audio", AUDIO), ("icon", IMAGE)):
                asset = track[kind]
                self.assertEqual((path / asset["path"]).read_bytes(), data)
                self.assertEqual(asset["size"], len(data))
                self.assertEqual(asset["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(list(self.output.iterdir()), [path])
        self.assertTrue(all(timeout == 7 for _, timeout in self.requests))

    def test_access_urls_and_user_identifiers_are_not_saved(self):
        path = self.backup()
        text = (path / "manifest.json").read_text()
        for value in ("card-secret", "download-secret", "private-user-id", "trackUrl", "audioPreviewUrl", "https://"):
            self.assertNotIn(value, text)
        self.assertIn((PAGE_URL, 7), self.requests)
        self.assertIn((urldefrag(AUDIO_URL)[0], 7), self.requests)
        self.assertFalse(any("#" in url for url, _ in self.requests))

    def test_track_icon_overrides_chapter_icon(self):
        self.card["content"]["chapters"][0]["tracks"][0]["display"] = {"icon16x16": "https://media.example/track-icon"}
        self.assets["https://media.example/track-icon"] = (b"track image", "image/jpeg")
        path = self.backup()
        self.assertEqual((path / "icons/001 - First _ track.jpg").read_bytes(), b"track image")

    def test_optional_images_can_be_missing_and_content_cover_is_supported(self):
        del self.card["metadata"]["cover"]
        del self.card["content"]["chapters"][0]["display"]
        self.card["content"]["cover"] = {"imageL": "https://media.example/cover"}
        path = self.backup()
        self.assertTrue((path / "cover.png").is_file())
        self.assertIsNone(self.manifest(path)["content"]["chapters"][0]["tracks"][0]["icon"])

    def test_missing_cover_does_not_prevent_audio_backup(self):
        self.card["metadata"] = None
        path = self.backup()
        self.assertIsNone(self.manifest(path)["content"]["cover"])
        self.assertTrue((path / "tracks/001 - First _ track.m4a").is_file())

    def test_http_failure_removes_partial_archive_and_does_not_expose_url(self):
        self.assets[urldefrag(AUDIO_URL)[0]] = HTTPError(AUDIO_URL, 403, "Forbidden", None, None)
        with self.assertRaisesRegex(archive.ArchiveError, "HTTP 403.*expired") as caught:
            self.backup()
        self.assertNotIn("download-secret", str(caught.exception))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_network_failure_removes_partial_archive(self):
        self.assets["https://media.example/icon"] = URLError(TimeoutError("signed-url-secret"))
        with self.assertRaisesRegex(archive.ArchiveError, "TimeoutError") as caught:
            self.backup()
        self.assertNotIn("signed-url-secret", str(caught.exception))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_interrupted_response_body_removes_partial_archive(self):
        original_read = FakeResponse.read

        def interrupted_read(response, size=-1):
            if response.headers.get_content_type() == "audio/mp4":
                raise IncompleteRead(b"partial audio")
            return original_read(response, size)

        with patch.object(FakeResponse, "read", interrupted_read):
            with self.assertRaisesRegex(archive.ArchiveError, "IncompleteRead"):
                self.backup()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_disk_failure_is_reported_as_a_disk_error(self):
        with patch.object(Path, "open", side_effect=OSError("No space left on device")):
            with redirect_stderr(io.StringIO()) as stderr, redirect_stdout(io.StringIO()):
                result = archive.main([PAGE_URL, "--output", str(self.output)])
        self.assertEqual(result, 1)
        self.assertIn("No space left on device", stderr.getvalue())
        self.assertNotIn("Check the connection", stderr.getvalue())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_checksum_mismatch_removes_partial_archive(self):
        self.assets[urldefrag(AUDIO_URL)[0]] = (b"corrupted audio", "audio/mp4")
        self.card["content"]["chapters"][0]["tracks"][0]["fileSize"] = len(b"corrupted audio")
        with self.assertRaisesRegex(archive.ArchiveError, "checksum mismatch"):
            self.backup()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_metadata_size_mismatch_removes_partial_archive(self):
        self.card["content"]["chapters"][0]["tracks"][0]["fileSize"] = 9999
        with self.assertRaisesRegex(archive.ArchiveError, "Size mismatch"):
            self.backup()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_truncated_http_response_removes_partial_archive(self):
        self.assets["https://media.example/cover"] = (IMAGE, "image/png", 9999)
        with self.assertRaisesRegex(archive.ArchiveError, "Incomplete download"):
            self.backup()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_empty_response_and_error_page_are_rejected(self):
        for body, mime, message in ((b"", "image/png", "empty file"),
                                    (b"<html>error</html>", "text/html", "Expected a media file"),
                                    (b"#EXTM3U", "application/vnd.apple.mpegurl", "Expected a media file")):
            with self.subTest(mime=mime, body=body):
                self.assets["https://media.example/cover"] = (body, mime)
                with self.assertRaisesRegex(archive.ArchiveError, message):
                    self.backup()
                self.assertEqual(list(self.output.iterdir()), [])

    def test_existing_archive_is_never_overwritten(self):
        path = self.backup()
        original = (path / "manifest.json").read_bytes()
        self.requests.clear()
        with self.assertRaisesRegex(archive.ArchiveError, "Archive already exists"):
            self.backup()
        self.assertEqual(self.requests, [(PAGE_URL, 7)])
        self.assertEqual((path / "manifest.json").read_bytes(), original)

    def test_keyboard_interrupt_removes_partial_archive(self):
        self.assets[urldefrag(AUDIO_URL)[0]] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.backup()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_large_assets_are_read_in_bounded_chunks(self):
        large_image = IMAGE * 200000
        self.assets["https://media.example/cover"] = (large_image, "image/png")
        path = self.backup()
        self.assertEqual((path / "cover.png").stat().st_size, len(large_image))
        self.assertTrue(all(size == 1024 * 1024 for size in self.responses[1].read_sizes))
        self.assertGreater(len(self.responses[1].read_sizes), 2)

    def test_missing_audio_url_is_detected_before_creating_output(self):
        del self.card["content"]["chapters"][0]["tracks"][0]["trackUrl"]
        with self.assertRaisesRegex(archive.ArchiveError, "no download URL"):
            self.backup()
        self.assertFalse(self.output.exists())

    def test_cli_success_and_readable_failure(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = archive.main([PAGE_URL, "--output", str(self.output)])
        self.assertEqual(result, 0)
        self.assertIn("Saved archive to", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = archive.main([PAGE_URL, "--output", str(self.output)])
        self.assertEqual(result, 1)
        self.assertIn("Archive already exists", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


class ParsingTests(unittest.TestCase):
    def test_missing_or_malformed_page_data_is_readable(self):
        for html in ("<html>no data</html>", '<script id="__NEXT_DATA__">broken</script>',
                     '<script id="__NEXT_DATA__">null</script>', '<script id="__NEXT_DATA__">{}</script>'):
            with self.subTest(html=html), self.assertRaises(archive.ArchiveError):
                archive.card_from_html(html)

    def test_invalid_card_structure_is_rejected(self):
        mutations = [
            lambda card: card.update(title=None),
            lambda card: card.update(content=None),
            lambda card: card["content"].update(chapters=[]),
            lambda card: card["content"]["chapters"].append(None),
            lambda card: card["content"]["chapters"][0].update(tracks=[]),
            lambda card: card["content"]["chapters"][0]["tracks"].append(None),
            lambda card: card["content"]["chapters"][0]["tracks"][0].update(trackUrl="file:///private/file"),
        ]
        for mutate in mutations:
            card = sample_card()
            mutate(card)
            with self.subTest(mutate=mutate), self.assertRaises(archive.ArchiveError):
                archive.card_from_html(page(card))

    def test_safe_names_preserve_apostrophes_and_prevent_path_escape(self):
        self.assertEqual(archive.safe_name(" Let's é "), "Let's é")
        self.assertEqual(archive.safe_name("../../unsafe\\path\x00?*"), "_.._unsafe_path___")
        self.assertEqual(archive.safe_name(" .. "), "Untitled")
        self.assertEqual(archive.safe_name("CON.txt"), "_CON.txt")
        self.assertLessEqual(len(archive.safe_name("é" * 1000).encode()), 160)

    def test_extensions_follow_content_type_and_have_safe_fallbacks(self):
        self.assertEqual(archive._extension("audio/mp4", AUDIO_URL, "aac"), ".m4a")
        self.assertEqual(archive._extension("application/octet-stream", "https://media.example/icon.png?key=a", None), ".png")
        self.assertEqual(archive._extension("application/octet-stream", AUDIO_URL, "mp3"), ".mp3")
        self.assertEqual(archive._extension("application/octet-stream", AUDIO_URL, None), ".bin")
        self.assertEqual(archive._extension("application/octet-stream", AUDIO_URL, ["aac"]), ".bin")

    def test_invalid_urls_fail_before_any_network_request(self):
        for url in ("file:///etc/passwd", "not-a-url", "https://user:password@example.com/card", "https://example.com:bad/card"):
            with self.subTest(url=url), patch.object(archive, "urlopen") as opener:
                with self.assertRaises(archive.ArchiveError):
                    archive.fetch_card(url)
                opener.assert_not_called()

    def test_timeout_must_be_positive_and_finite(self):
        for timeout in ("0", "-1", "nan", "inf", "bad"):
            with self.subTest(timeout=timeout), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    archive.main([PAGE_URL, "--timeout", timeout])
                self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
