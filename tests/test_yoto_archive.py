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
from urllib.request import HTTPRedirectHandler

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


class AuthenticatedBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "archives"
        self.card = sample_card()
        self.token = "account-token-secret"
        self.api_url = "https://api.yotoplay.com/card/test?playable=true&signingType=s3"
        self.api_body = None
        self.api_error = None
        self.requests = []
        self.patcher = patch.object(archive, "urlopen", self.urlopen)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def urlopen(self, request, timeout):
        self.requests.append(request)
        if request.full_url == self.api_url:
            if self.api_error:
                raise self.api_error
            body = self.api_body if self.api_body is not None else json.dumps({"card": self.card}).encode()
            return FakeResponse(body, "application/json")
        if request.full_url == urldefrag(AUDIO_URL)[0]:
            return FakeResponse(AUDIO, "audio/mp4")
        if request.full_url in ("https://media.example/cover", "https://media.example/icon"):
            return FakeResponse(IMAGE, "image/png")
        self.fail(f"Unexpected request to {request.full_url}")

    def test_linked_card_backup_uses_account_api_and_keeps_token_private(self):
        path = archive.backup_card(PAGE_URL, self.output, access_token=self.token, progress=None)
        self.assertEqual(self.requests[0].full_url, self.api_url)
        self.assertEqual(self.requests[0].get_header("Authorization"), f"Bearer {self.token}")
        self.assertTrue(all(request.get_header("Authorization") is None for request in self.requests[1:]))
        manifest = (path / "manifest.json").read_text()
        for secret in (self.token, "card-secret", "download-secret", "private-user-id"):
            self.assertNotIn(secret, manifest)
        self.assertEqual((path / "tracks/001 - First _ track.m4a").read_bytes(), AUDIO)
        self.assertTrue((path / "cover.png").is_file())
        self.assertTrue((path / "icons/001 - First _ track.png").is_file())

    def test_token_is_not_forwarded_on_redirects(self):
        archive.fetch_card("test", access_token=self.token)
        redirected = HTTPRedirectHandler().redirect_request(
            self.requests[0], None, 302, "Found", Message(), "https://other.example/card")
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_card_ids_and_share_links_resolve_without_fetching_share_page(self):
        for value in ("test", PAGE_URL, "https://share.yoto.co/p/test?secret=value"):
            with self.subTest(value=value):
                self.assertEqual(archive.fetch_card(value, access_token=self.token)["title"], self.card["title"])
        self.assertEqual([request.full_url for request in self.requests], [self.api_url] * 3)

    def test_unrecognized_urls_fail_without_sending_credentials(self):
        for value in ("https://other.example/test", "https://yoto.io.evil.example/test",
                      "https://yoto.io:8080/test", "https://yoto.io/../test",
                      "https://yoto.io/test/extra", "https://share.yoto.co/test",
                      "https://user:password@yoto.io/test", "file:///test"):
            with self.subTest(value=value), self.assertRaises(archive.ArchiveError):
                archive.fetch_card(value, access_token=self.token)
        self.assertEqual(self.requests, [])

    def test_account_errors_are_actionable_and_do_not_expose_secrets(self):
        for code, hint in ((401, "Refresh the account token"), (403, "permissions"), (404, "authenticated account")):
            self.api_error = HTTPError(self.api_url, code, self.token, None, None)
            with self.subTest(code=code), self.assertRaisesRegex(archive.ArchiveError, hint) as caught:
                archive.backup_card(PAGE_URL, self.output, access_token=self.token, progress=None)
            self.assertNotIn(self.token, str(caught.exception))
            self.assertNotIn(self.api_url, str(caught.exception))
            self.assertFalse(self.output.exists())

    def test_invalid_api_data_fails_before_downloading_assets(self):
        for body in (b"not-json", b"null", b"{}", b'{"card":null}'):
            self.api_body = body
            with self.subTest(body=body), self.assertRaises(archive.ArchiveError):
                archive.backup_card("test", self.output, access_token=self.token, progress=None)
            self.assertFalse(self.output.exists())
        self.api_body = None
        self.card["content"]["chapters"][0]["tracks"][0]["trackUrl"] = "yoto:#unresolved"
        with self.assertRaises(archive.ArchiveError):
            archive.backup_card("test", self.output, access_token=self.token, progress=None)
        self.assertFalse(self.output.exists())
        self.assertTrue(all(request.full_url == self.api_url for request in self.requests))

    def test_cli_accepts_raw_token_and_oauth_json_files(self):
        for number, contents in enumerate((self.token + "\n", json.dumps({"access_token": self.token, "id_token": "unused"}))):
            token_file = self.root / "token.json"
            token_file.write_text(contents)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
                result = archive.main([PAGE_URL, "--output", str(self.output / str(number)), "--token-file", str(token_file)])
            self.assertEqual(result, 0, stderr.getvalue())
            self.assertTrue((self.output / str(number) / archive.safe_name(self.card["title"]) / "manifest.json").is_file())

    def test_invalid_token_files_fail_before_network_access(self):
        token_file = self.root / "token.json"
        for contents in (b"", b"{broken", b"{}", b"[]", b'{"access_token":null}',
                         b'{"access_token":42}', b"one\ntwo", b"token\x00secret", b"\xff"):
            token_file.write_bytes(contents)
            with self.subTest(contents=contents), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
                result = archive.main([PAGE_URL, "--token-file", str(token_file)])
            self.assertEqual(result, 1)
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertNotIn("secret", stderr.getvalue())
        token_file.unlink()
        with self.assertRaisesRegex(archive.ArchiveError, "Could not read the token file"):
            archive.read_access_token(token_file)
        self.assertEqual(self.requests, [])


class ParsingTests(unittest.TestCase):
    def test_private_share_page_explains_authenticated_backup(self):
        with self.assertRaisesRegex(archive.ArchiveError, "--token-file"):
            archive.card_from_html(page(None))

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
