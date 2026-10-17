import json
from pathlib import Path
import tempfile
import time
import unittest

from PyQt5 import QtCore, QtNetwork

import test_music as shared
from music.lyrics import LyricsCache, LyricsClient, MAX_LYRICS_BYTES, parse_lrc, select_lyrics


TRACK = {"id": "one", "title": "First Song", "artist": "Test Artist", "duration": 180}
PLAIN = "Our first original line\nOur second original line"
SYNCED = "[00:01.00]Our first original line\n[00:03.00]Our second original line"


def record(**fields):
    return {"id": 1, "trackName": "First Song", "artistName": "Test Artist", "duration": 180,
            "plainLyrics": PLAIN, "syncedLyrics": SYNCED, "instrumental": False, **fields}


class FakeReply(QtCore.QObject):
    readyRead = QtCore.pyqtSignal()
    finished = QtCore.pyqtSignal()

    def __init__(self, request):
        super().__init__()
        self.request = request
        self.data = b""
        self.status = 200
        self.failure = QtNetwork.QNetworkReply.NoError
        self.aborted = False

    def readAll(self):
        data, self.data = self.data, b""
        return QtCore.QByteArray(data)

    def attribute(self, _key):
        return self.status

    def error(self):
        return self.failure

    def abort(self):
        self.aborted = True
        self.failure = QtNetwork.QNetworkReply.OperationCanceledError
        self.finished.emit()

    def complete(self, response, status=200):
        self.data = json.dumps(response).encode("utf-8")
        self.status = status
        self.readyRead.emit()
        self.finished.emit()


class FakeManager:
    def __init__(self):
        self.replies = []

    def get(self, request):
        reply = FakeReply(request)
        self.replies.append(reply)
        return reply


class LyricsClientTests(unittest.TestCase):
    def setUp(self):
        shared.application()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = FakeManager()
        self.client = LyricsClient(self.root, manager=self.manager)
        self.ready, self.errors = [], []
        self.client.ready.connect(lambda identity, doc: self.ready.append((identity, doc)))
        self.client.failed.connect(lambda identity, message: self.errors.append((identity, message)))

    def tearDown(self):
        self.client.cancel()
        self.client.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.temporary.cleanup()

    def test_success_is_cached_and_reopening_uses_no_network(self):
        self.client.request(TRACK)
        self.assertEqual(len(self.manager.replies), 1)
        self.assertTrue(self.client.timer.isActive())
        self.manager.replies[0].complete(record())
        self.assertEqual(self.ready[0][1]["status"], "found")
        self.assertEqual(self.ready[0][1]["synced"], SYNCED)
        self.assertFalse(self.client.timer.isActive())
        self.client.request(TRACK)
        self.assertEqual(len(self.manager.replies), 1)
        self.assertTrue(self.ready[-1][1]["cached"])
        self.assertEqual(LyricsCache(self.root).load(TRACK)["plain"], PLAIN)

    def test_exact_lookup_falls_back_to_search_and_rejects_wrong_song(self):
        self.client.request(TRACK)
        self.manager.replies[0].complete({}, 404)
        self.assertTrue(self.manager.replies[1].request.url().path().endswith("/search"))
        self.manager.replies[1].complete([record(artistName="Another Artist"), record(duration=330), record()])
        self.assertEqual(self.ready[-1][1]["source_id"], 1)
        self.assertEqual(self.ready[-1][1]["matched_artist"], "Test Artist")

    def test_missing_artist_uses_search_and_misses_are_cached_but_can_be_retried(self):
        track = TRACK | {"artist": ""}
        self.client.request(track)
        self.assertTrue(self.manager.replies[0].request.url().path().endswith("/search"))
        self.manager.replies[0].complete([])
        self.assertEqual(self.ready[-1][1]["status"], "not_found")
        self.client.request(track)
        self.assertEqual(len(self.manager.replies), 1)
        self.client.request(track, refresh=True)
        self.assertEqual(len(self.manager.replies), 2)

    def test_new_track_cancels_old_request_and_ignores_late_response(self):
        self.client.request(TRACK)
        previous = self.manager.replies[0]
        other = TRACK | {"id": "two", "title": "Second Song"}
        self.client.request(other)
        self.assertTrue(previous.aborted)
        previous.complete(record())
        self.assertFalse(self.ready)
        self.manager.replies[1].complete(record(trackName="Second Song"))
        self.assertEqual([identity for identity, _document in self.ready], ["two"])
        self.assertFalse(self.client.cache.path(TRACK).exists())

    def test_timeout_and_server_failure_do_not_poison_cache(self):
        self.client.request(TRACK)
        self.client._timeout()
        self.assertTrue(self.manager.replies[0].aborted)
        self.assertEqual(len(self.errors), 1)
        self.assertFalse(self.client.cache.path(TRACK).exists())
        self.client.request(TRACK)
        self.manager.replies[1].complete({}, 503)
        self.assertEqual(len(self.errors), 2)
        self.assertFalse(self.client.cache.path(TRACK).exists())

    def test_reply_size_is_bounded_and_instrumental_is_a_valid_result(self):
        self.client.request(TRACK)
        self.manager.replies[0].complete({"plainLyrics": "x" * (MAX_LYRICS_BYTES + 1)})
        self.assertTrue(self.errors)
        self.assertFalse(self.ready)
        self.client.request(TRACK)
        self.manager.replies[1].complete(record(instrumental=True, plainLyrics=None, syncedLyrics=None))
        self.assertEqual(self.ready[-1][1]["status"], "instrumental")

    def test_corrupt_and_expired_cache_is_ignored_and_display_rename_keeps_identity(self):
        path = self.client.cache.path(TRACK)
        path.parent.mkdir(parents=True)
        path.write_text("{invalid", encoding="utf-8")
        self.assertIsNone(self.client.cache.load(TRACK))
        self.client.cache.save(TRACK, {"status": "not_found", "plain": "", "synced": ""})
        document = json.loads(path.read_text(encoding="utf-8"))
        document["saved_at"] = time.time() - 22000
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertIsNone(self.client.cache.load(TRACK))
        self.assertEqual(self.client.cache.path(TRACK), self.client.cache.path(TRACK | {
            "title": "My display title", "original_title": "First Song"}))


class LrcParserTests(unittest.TestCase):
    def test_timestamps_offsets_repeated_lines_and_duet_lines(self):
        text = "[ar:Original test]\n[offset:100]\n[00:01.20][00:03.500]Original line\n[00:03.500]Another voice"
        lines = parse_lrc(text)
        self.assertAlmostEqual(lines[0][0], 1.1)
        self.assertEqual(lines[0][1], "Original line")
        self.assertAlmostEqual(lines[1][0], 3.4)
        self.assertEqual(lines[1][1], "Original line\nAnother voice")
        self.assertEqual(parse_lrc(PLAIN), [])

    def test_matching_requires_the_right_title_artist_and_duration(self):
        self.assertIsNone(select_lyrics([record(trackName="Unrelated Song"), record(artistName="Other")], TRACK))
        self.assertIsNone(select_lyrics([record(plainLyrics=["invalid"], syncedLyrics=None)], TRACK))
        self.assertIsNone(select_lyrics([record(artistName=123), record(duration="invalid")], TRACK))

    def test_timestamp_output_is_bounded_for_large_local_files(self):
        text = "[00:01.00]" * 11000 + "Original words"
        self.assertEqual(parse_lrc(text), [(1.0, "\n".join(["Original words"] * 10000))])


class LyricsIntegrationTests(unittest.TestCase):
    setUp = shared.MusicIntegrationTests.setUp
    tearDown = shared.MusicIntegrationTests.tearDown

    def prepare(self):
        playlist = self.panel.library.create_playlist("Lyrics")
        one = self.panel.library.add_search_track(playlist["id"], "First Song", "Test Artist")
        two = self.panel.library.add_search_track(playlist["id"], "Second Song", "Test Artist")
        cache = LyricsCache(self.panel.library.root)
        for track in (one, two):
            cache.save(track, {"status": "found", "source": "Test", "plain": PLAIN, "synced": SYNCED})
        self.panel.player.preparationRequested.disconnect(self.panel.prepare_track)
        self.panel.player.set_queue([one["id"], two["id"]])
        self.panel.lyrics_button.click()
        return one, two, self.panel.lyrics_dialog

    def test_header_opens_cached_lyrics_follows_audio_position_and_changes_tracks(self):
        one, two, dialog = self.prepare()
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.track["id"], one["id"])
        self.assertEqual(dialog.lines.count(), 2)
        self.backend.setPosition(1500)
        self.assertEqual(dialog.lines.currentRow(), 0)
        self.backend.setPosition(3500)
        self.assertEqual(dialog.lines.currentRow(), 1)
        self.backend.setPosition(500)
        self.assertEqual(dialog.lines.currentRow(), -1)
        self.panel.player.next()
        self.assertEqual(dialog.track["id"], two["id"])
        self.assertIn("Second Song", dialog.title.text())
        self.panel.lyrics_button.click()
        self.assertFalse(dialog.isVisible())
        self.assertFalse(self.panel.lyrics_button.isChecked())

    def test_local_lyrics_are_cached_and_plain_text_is_never_interpreted_as_html(self):
        one, _two, dialog = self.prepare()
        path = self.root / "own.txt"
        text = "<b>These are literal original words</b>"
        path.write_text(text, encoding="utf-8")
        dialog.load_file(path)
        self.assertEqual(dialog.plain.toPlainText(), text)
        self.assertEqual(LyricsCache(self.panel.library.root).load(one)["plain"], text)
        self.assertFalse(dialog.follow.isEnabled())
        dialog.close()
        self.panel.lyrics_button.click()
        self.assertEqual(dialog.plain.toPlainText(), text)

    def test_double_clicking_a_timed_line_requests_seek(self):
        _one, _two, dialog = self.prepare()
        requested = []
        dialog.seekRequested.connect(requested.append)
        dialog.lines.itemDoubleClicked.emit(dialog.lines.item(1))
        self.assertEqual(requested, [3.0])


if __name__ == "__main__":
    unittest.main()
