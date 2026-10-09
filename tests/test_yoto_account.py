import base64
from contextlib import redirect_stderr, redirect_stdout
import copy
from email.message import Message
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

import yoto_account as account
import yoto_archive as core
from test_yoto_archive import AUDIO, AUDIO_URL, IMAGE, FakeResponse, sample_card


def token(subject="auth0|test-account", expiration=None):
    data = json.dumps({"sub": subject, "exp": expiration or time.time() + 3600}).encode()
    return "header." + base64.urlsafe_b64encode(data).decode().rstrip("=") + ".signature"


class Clock:
    def __init__(self):
        self.value = 0
        self.delays = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.value += seconds


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "account"
        self.clock = Clock()
        fixtures = Path(__file__).parent / "fixtures"
        self.library = json.loads((fixtures / "account_library.json").read_text())
        self.myo = json.loads((fixtures / "account_myo.json").read_text())
        self.pages = {account.SOURCES[0]: self.library, account.SOURCES[1]: self.myo}
        self.cards = {}
        for entry in self.library["cards"]:
            card = sample_card()
            card.update(cardId=entry["card"]["cardId"], title=entry["card"]["title"], updatedAt="2026-01-01T00:00:00Z")
            card["metadata"]["category"] = entry["card"]["metadata"]["category"]
            # Content-addressed image URLs can be reused across metadata edits.
            card["metadata"]["cover"]["imageL"] = "https://card-content.yotoplay.com/yoto/" + "a" * 43
            card["content"]["chapters"][0]["display"]["icon16x16"] = "https://card-content.yotoplay.com/yoto/" + "b" * 43
            if entry["cardId"] == "radio":
                card["content"]["chapters"][0]["tracks"][0].update(type="stream", format="mp3", trackUrl="https://live.example/radio.mp3")
            if entry["cardId"] == "podcast":
                card["metadata"]["feedUrl"] = "https://feeds.example/show.rss"
                card["content"]["chapters"][0]["tracks"][0].update(type="audio", format="mp3", trackUrl="https://episodes.example/episode.mp3")
            self.cards[entry["cardId"]] = card
        self.requests = []
        self.audio = AUDIO
        self.media_error = None
        patcher = patch.object(core, "urlopen", self.urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def policy(self):
        return account.RequestPolicy(interval=1, retries=0, clock=self.clock.now, sleep=self.clock.sleep, jitter=lambda: 0)

    def urlopen(self, request, timeout):
        self.requests.append((request, self.clock.now()))
        parts = urlsplit(request.full_url)
        if parts.netloc == "api.yotoplay.com":
            route = parts.path + ("?" + parts.query if parts.query else "")
            if route in self.pages:
                data = self.pages[route]
            else:
                identifier = parts.path.split("/")[-1]
                data = {"card": self.cards[identifier]}
                if isinstance(self.cards[identifier], BaseException):
                    raise self.cards[identifier]
            if isinstance(data, BaseException):
                raise data
            return FakeResponse(json.dumps(data).encode(), "application/json")
        self.assertIn(parts.netloc, ("media.example", "card-content.yotoplay.com"), "A stream/feed URL was opened")
        if parts.path == "/audio":
            if self.media_error:
                raise self.media_error
            return FakeResponse(self.audio, "audio/mp4")
        return FakeResponse(IMAGE, "image/png")

    def backup(self, access_token=None):
        return account.backup_account(access_token or token(), output=self.output, progress=None, policy=self.policy())

    def manifest(self, result, identifier):
        root = self.output / result["cards"][identifier]["path"]
        return root, json.loads((root / "manifest.json").read_text())

    def media_requests(self):
        return [request for request, _ in self.requests if urlsplit(request.full_url).netloc != "api.yotoplay.com"]

    def change_audio(self, identifier):
        self.audio = b"new audio content"
        digest = base64.urlsafe_b64encode(hashlib.sha256(self.audio).digest()).decode().rstrip("=")
        track = self.cards[identifier]["content"]["chapters"][0]["tracks"][0]
        track.update(trackUrl=f"https://media.example/audio?Signature=new-secret#sha256={digest}", fileSize=len(self.audio))

    def test_full_account_merges_sources_and_keeps_linked_identity(self):
        result = self.backup()
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["cards"]), 6)
        self.assertEqual(result["summary"], {"archived": 4, "unchanged": 0, "url_only": 2, "unavailable": 0, "failed": 0})
        self.assertEqual(result["cards"]["myo"]["sources"], list(account.SOURCES))
        self.assertEqual(result["cards"]["linked"]["content_id"], "myo")
        self.assertTrue(any(urlsplit(request.full_url).path == "/content/myo" for request, _ in self.requests))
        self.assertTrue(any(urlsplit(request.full_url).path == "/card/linked" for request, _ in self.requests))
        # Shared audio/images are fetched once, without auth on CDN requests.
        self.assertEqual(len(self.media_requests()), 3)
        for request, _ in self.requests:
            if urlsplit(request.full_url).netloc == "api.yotoplay.com":
                self.assertIsNotNone(request.get_header("Authorization"))
            else:
                self.assertIsNone(request.get_header("Authorization"))
        times = [timestamp for _, timestamp in self.requests]
        self.assertTrue(all(second - first >= 1 for first, second in zip(times, times[1:])))
        text = (self.output / "account.json").read_text()
        for record in result["cards"].values():
            text += (self.output / record["path"] / "manifest.json").read_text()
        self.assertNotIn("signature", text)
        self.assertNotIn("download-secret", text)
        self.assertNotIn("private-user-id", text)

    def test_unchanged_and_new_signed_urls_make_no_media_requests(self):
        before = self.backup()
        self.requests.clear()
        for identifier in ("purchased", "virtual", "myo", "linked"):
            self.cards[identifier]["content"]["chapters"][0]["tracks"][0]["trackUrl"] = AUDIO_URL.replace("download-secret", "refreshed-secret")
        after = self.backup()
        self.assertEqual(after["summary"]["unchanged"], 6)
        self.assertEqual(self.media_requests(), [])
        self.assertEqual(before["cards"]["purchased"]["path"], after["cards"]["purchased"]["path"])

    def test_titles_and_track_order_change_without_audio_downloads(self):
        before = self.backup()
        chapter = self.cards["purchased"]["content"]["chapters"][0]
        second = copy.deepcopy(chapter["tracks"][0])
        second.update(key="second", title="Another track")
        chapter["tracks"].insert(0, second)
        self.cards["purchased"].update(title="Renamed", updatedAt="2026-01-02T00:00:00Z")
        self.cards["virtual"]["title"] = "Renamed"
        self.requests.clear()
        after = self.backup()
        self.assertEqual(self.media_requests(), [])
        self.assertNotEqual(after["cards"]["purchased"]["path"], after["cards"]["virtual"]["path"])
        root, manifest = self.manifest(after, "purchased")
        self.assertEqual(manifest["card"]["title"], "Renamed")
        tracks = manifest["content"]["chapters"][0]["tracks"]
        self.assertEqual([track["key"] for track in tracks], ["second", "track-1"])
        self.assertEqual((root / tracks[0]["audio"]["path"]).read_bytes(), AUDIO)
        self.assertTrue((self.output / before["cards"]["purchased"]["path"]).is_dir())

    def test_missing_or_corrupt_files_are_repaired(self):
        before = self.backup()
        root, manifest = self.manifest(before, "purchased")
        (root / manifest["content"]["cover"]["path"]).unlink()
        audio = root / manifest["content"]["chapters"][0]["tracks"][0]["audio"]["path"]
        audio.write_bytes(b"broken")
        self.requests.clear()
        after = self.backup()
        root, manifest = self.manifest(after, "purchased")
        self.assertTrue(after["complete"])
        self.assertEqual((root / manifest["content"]["chapters"][0]["tracks"][0]["audio"]["path"]).read_bytes(), AUDIO)
        self.assertGreater(len(self.media_requests()), 0)

    def test_streams_podcasts_and_mixed_tracks_never_open_stream_urls(self):
        chapter = self.cards["myo"]["content"]["chapters"][0]
        stream = copy.deepcopy(chapter["tracks"][0])
        stream.update(key="live", type="stream", trackUrl="https://live.example/music.mp3")
        chapter["tracks"].append(stream)
        result = self.backup()
        _, podcast = self.manifest(result, "podcast")
        self.assertEqual(podcast["content"]["feed_url"], "https://feeds.example/show.rss")
        track = podcast["content"]["chapters"][0]["tracks"][0]
        self.assertIsNone(track["audio"])
        self.assertEqual(track["source"]["url"], "https://episodes.example/episode.mp3")
        _, mixed = self.manifest(result, "myo")
        tracks = mixed["content"]["chapters"][0]["tracks"]
        self.assertIsNotNone(tracks[0]["audio"])
        self.assertIsNone(tracks[1]["audio"])
        self.assertEqual(tracks[1]["source"]["kind"], "stream")

    def test_empty_myo_and_feed_only_podcast_are_archived(self):
        self.cards["myo"]["content"]["chapters"] = []
        self.cards["podcast"]["content"]["chapters"] = []
        result = self.backup()
        self.assertTrue(result["complete"])
        self.assertEqual(result["cards"]["podcast"]["kind"], "url_only")

    def test_failure_keeps_previous_revision_and_other_cards_succeed(self):
        before = self.backup()
        self.change_audio("purchased")
        self.media_error = HTTPError("https://media.example/audio", 503, "temporary", None, None)
        after = self.backup()
        self.assertFalse(after["complete"])
        self.assertEqual(after["summary"]["failed"], 1)
        self.assertEqual(after["summary"]["unchanged"], 5)
        self.assertEqual(after["cards"]["purchased"]["path"], before["cards"]["purchased"]["path"])
        self.assertFalse(any(path.name.startswith(".card-") for path in self.output.iterdir()))

    def test_interrupt_keeps_previous_index_and_removes_staging(self):
        before = self.backup()
        self.change_audio("purchased")
        self.media_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.backup()
        after = json.loads((self.output / "account.json").read_text())
        self.assertFalse(after["complete"])
        self.assertEqual(after["cards"]["purchased"]["path"], before["cards"]["purchased"]["path"])
        self.assertFalse((self.output / ".account-lock").exists())
        self.assertFalse(any(path.name.startswith(".card-") for path in self.output.iterdir()))

    def test_removed_cards_are_retained_and_incomplete_lists_do_not_mark_removals(self):
        before = self.backup()
        self.library["cards"] = self.library["cards"][1:]
        self.pages[account.SOURCES[1]] = HTTPError("https://api.yotoplay.com/content/mine", 503, "temporary", None, None)
        partial = self.backup()
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["cards"]["purchased"]["state"], before["cards"]["purchased"]["state"])
        self.pages[account.SOURCES[1]] = self.myo
        after = self.backup()
        self.assertEqual(after["cards"]["purchased"]["state"], "not_in_library")
        self.assertTrue((self.output / after["cards"]["purchased"]["path"]).is_dir())

    def test_unsupported_pagination_is_incomplete_not_silently_successful(self):
        self.library["nextPageToken"] = "opaque-token"
        result = self.backup()
        self.assertFalse(result["complete"])
        self.assertIn("Unsupported library pagination", result["discovery"]["errors"][0])

    def test_exhausted_rate_limit_stops_discovery_requests(self):
        self.pages[account.SOURCES[0]] = HTTPError(account.API + account.SOURCES[0], 429, "limited", None, None)
        result = account.discover(token(), 30, self.policy())
        self.assertFalse(result["complete"])
        self.assertEqual(len(self.requests), 1)

    def test_broken_card_manifest_is_not_overwritten(self):
        before = self.backup()
        root, _ = self.manifest(before, "purchased")
        (root / "manifest.json").write_text(json.dumps({"content": None}))
        original_index = (self.output / "account.json").read_bytes()
        with self.assertRaisesRegex(core.ArchiveError, "Invalid card manifest"):
            self.backup()
        self.assertEqual((self.output / "account.json").read_bytes(), original_index)

    def test_explicit_next_page_is_followed_with_pacing(self):
        second = self.library["cards"][3:]
        self.library["cards"] = self.library["cards"][:3]
        self.library["next"] = "/card/family/library?page=2"
        self.pages["/card/family/library?page=2"] = {"cards": second}
        result = self.backup()
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["cards"]), 6)

    def test_cross_host_pagination_and_loops_are_rejected(self):
        for link in ("https://evil.example/cards", "/card/family/library"):
            with self.subTest(link=link):
                self.library["next"] = link
                result = account.discover(token(), 30, self.policy())
                self.assertFalse(result["complete"])
        self.assertTrue(all(urlsplit(request.full_url).netloc == "api.yotoplay.com" for request, _ in self.requests))

    def test_bad_entries_are_reported_as_partial_discovery(self):
        self.library["cards"].append({"cardId": "../../escape"})
        result = self.backup()
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["cards"]), 6)

    def test_404_keeps_an_unavailable_record_and_archive(self):
        before = self.backup()
        self.cards["purchased"] = HTTPError("https://api.yotoplay.com/card/purchased", 404, "missing", None, None)
        after = self.backup()
        self.assertFalse(after["complete"])
        self.assertEqual(after["summary"]["unavailable"], 1)
        self.assertEqual(after["cards"]["purchased"]["path"], before["cards"]["purchased"]["path"])

    def test_authentication_failure_stops_further_card_requests(self):
        self.cards["purchased"] = HTTPError("https://api.yotoplay.com/card/purchased", 403, "expired", None, None)
        result = self.backup()
        self.assertFalse(result["complete"])
        self.assertEqual(result["summary"]["failed"], 6)
        card_requests = [request for request, _ in self.requests if urlsplit(request.full_url).path.startswith("/card/")
                         and not urlsplit(request.full_url).path.startswith("/card/family/")]
        self.assertEqual(len(card_requests), 1)

    def test_malformed_optional_media_url_fails_one_card_without_a_traceback(self):
        self.cards["purchased"]["metadata"]["cover"]["imageL"] = {"invalid": "url"}
        result = self.backup()
        self.assertFalse(result["complete"])
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertEqual(result["summary"]["archived"], 3)

    def test_other_account_and_invalid_existing_targets_are_rejected_before_requests(self):
        self.backup()
        self.requests.clear()
        with self.assertRaisesRegex(core.ArchiveError, "different account"):
            self.backup(token("auth0|another-account"))
        self.assertEqual(self.requests, [])
        (self.output / "account.json").write_text("broken")
        with self.assertRaisesRegex(core.ArchiveError, "Invalid archive manifest"):
            self.backup()
        self.assertEqual(self.requests, [])

    def test_busy_archive_and_unrelated_existing_directory_are_protected(self):
        self.output.mkdir()
        lock = self.output / ".account-lock"
        lock.mkdir()
        with self.assertRaisesRegex(core.ArchiveError, "Another account backup"):
            self.backup()
        self.assertTrue(lock.exists())
        lock.rmdir()
        (self.output / "keep.txt").write_text("existing files")
        with self.assertRaisesRegex(core.ArchiveError, "target contains files"):
            self.backup()
        self.assertEqual((self.output / "keep.txt").read_text(), "existing files")

    def test_token_validation_and_expiry_happen_before_requests(self):
        for access_token in ("opaque", token(expiration=1)):
            with self.subTest(token=access_token), self.assertRaises(core.ArchiveError):
                self.backup(access_token)
        self.assertEqual(self.requests, [])

    def test_list_and_cli_do_not_download_media(self):
        token_file = self.root / "token.json"
        token_file.write_text(json.dumps({"access_token": token()}))
        with redirect_stdout(io.StringIO()), patch.object(account, "RequestPolicy", return_value=self.policy()):
            self.assertEqual(core.main(["--account", "--list", "--token-file", str(token_file)]), 0)
        self.assertEqual(self.media_requests(), [])
        self.assertFalse(self.output.exists())
        for args in (["--account"], ["--list", "card"], ["card", "--account", "--token-file", str(token_file)]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                core.main(args)

    def test_manifest_paths_cannot_escape_or_follow_symlinks(self):
        result = self.backup()
        result["cards"]["purchased"]["path"] = "../outside"
        (self.output / "account.json").write_text(json.dumps(result))
        with self.assertRaisesRegex(core.ArchiveError, "Unsafe path"):
            self.backup()
        result["cards"]["purchased"]["path"] = "shortcut"
        (self.output / "shortcut").symlink_to(self.root, target_is_directory=True)
        (self.output / "account.json").write_text(json.dumps(result))
        with self.assertRaisesRegex(core.ArchiveError, "symlink"):
            self.backup()


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.policy = account.RequestPolicy(interval=1, retries=3, clock=self.clock.now, sleep=self.clock.sleep, jitter=lambda: 0)
        self.request = Request("https://api.yotoplay.com/card/family/library")

    def test_retry_after_is_respected_and_error_response_closed(self):
        headers = Message()
        headers["Retry-After"] = "5"
        error = HTTPError(self.request.full_url, 429, "limited", headers, io.BytesIO())
        with patch.object(core, "urlopen", side_effect=[error, FakeResponse(b"{}", "application/json")]) as opener:
            response = self.policy.open(self.request, 30)
            response.close()
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(self.clock.delays, [5])
        self.assertTrue(error.fp.closed)

    def test_retries_are_bounded_with_exponential_backoff(self):
        def unavailable(*args, **kwargs):
            raise HTTPError(self.request.full_url, 503, "temporary", None, None)

        with patch.object(core, "urlopen", side_effect=unavailable) as opener, self.assertRaises(HTTPError) as caught:
            self.policy.open(self.request, 30)
        caught.exception.close()
        self.assertEqual(opener.call_count, 4)
        self.assertEqual(self.clock.delays, [1, 2, 4])

    def test_auth_errors_are_not_retried(self):
        with patch.object(core, "urlopen", side_effect=HTTPError(self.request.full_url, 403, "forbidden", None, None)) as opener:
            with self.assertRaises(HTTPError) as caught:
                self.policy.open(self.request, 30)
        caught.exception.close()
        self.assertEqual(opener.call_count, 1)

    def test_connection_failure_retries_and_http_date_retry_after(self):
        with patch.object(core, "urlopen", side_effect=[URLError(TimeoutError()), FakeResponse(b"{}", "application/json")]):
            self.policy.open(self.request, 30).close()
        self.assertEqual(self.clock.delays, [1])
        with patch.object(account.time, "time", return_value=0):
            self.assertEqual(self.policy._retry_after("Thu, 01 Jan 1970 00:00:10 GMT"), 10)
        self.assertEqual(self.policy._retry_after("not a date"), 0)
        self.assertEqual(self.policy._retry_after("nan"), 0)


if __name__ == "__main__":
    unittest.main()
