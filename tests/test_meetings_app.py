"""Meeting recording, part 1, in the app (offscreen; fake audio devices, a fake transcriber, a fake download - nothing
real is recorded, downloaded or transcribed): consent gating, REC on every screen, the always-on-top pill, the
tray tooltip, every stop path, devices, the model download on click, the writing down, the Meetings screen with
its statuses and Delete, the feature switch, and logs without titles, names or text."""

from __future__ import annotations

import json
import logging
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from briefing_reader import dock, find_view, meeting_audio as ma, meeting_speech as sp, meetings as mt
from briefing_reader import meetings_view, ui
from tests.meeting_fakes import MIC_A, MIC_B, OUT_A, OUT_B, Clock, FakeBackend, FakeSession, FakeTranscriber, tone
from tests.ui_fakes import NOW, AppHarness, fonts, settle

_app: QApplication | None = None
MEETINGS_ON = "\n[meetings]\nenabled = true\n"
TITLE = "Zq7 Budget Review"
GUESTS = ("Ana Lopez", "Ben Okafor")
SPOKEN = "Zq7 the secret budget number is forty two"
MODEL_FILES = {"config.json": b"{}", "tokenizer.json": b"t" * 20, "vocabulary.txt": b"v" * 20,
               "model.bin": bytes(range(256)) * 8}


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_meetings_app", "-platform", "offscreen"])
    fonts()


class FakeJob:
    """A writing-down job the test drives: ``progress(p)``, ``finish(error)``, or ``run(transcriber)`` (the real
    transcribe_meeting with a fake transcriber, then done)."""

    jobs: list[FakeJob] = []

    def __init__(self, folder: Path, model: Path, on_progress, on_done) -> None:
        self.folder, self.model = folder, model
        self.on_progress, self.on_done = on_progress, on_done
        self.started = self.cancelled = False
        FakeJob.jobs.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def progress(self, percent: int) -> None:
        self.on_progress(percent)

    def finish(self, error: str = "") -> None:
        self.on_done(error)

    def run(self, transcriber: FakeTranscriber) -> None:
        try:
            sp.transcribe_meeting(self.folder, transcriber, self.on_progress)
        except Exception:  # noqa: BLE001 - like the worker: only a code goes back
            self.on_done(sp.ERR_WORKER)
            return
        self.on_done("")


class MeetingsApp(unittest.TestCase):
    def make(self, *, enabled: bool = True, model: bool = False, audio_missing: str = "",
             speech_missing: str = "") -> AppHarness:
        FakeJob.jobs = []
        app = AppHarness(self, config_extra=MEETINGS_ON if enabled else "")
        app.reading(autoplay=False)
        self.app, self.c, self.reading = app, app.c, app.c.window.reading
        self.clock = Clock(NOW)
        self.backend = FakeBackend()
        m = self.m = app.c.meetings
        if m is not None:
            m.threaded = False
            m._mono, m._now = self.clock.mono, self.clock.wall
            m.store._now = self.clock.wall
            m.stage.card.set_clock(self.clock.wall)
            m.backend_factory = lambda: (self.backend, "")
            m.audio_missing = lambda: audio_missing
            m.speech_missing = lambda: speech_missing
            m.process_factory = FakeJob
            m.summary_runner = None     # part 1's tests: no summary run (part 2's tests in test_meeting_summary_app)
            self.session = FakeSession(MODEL_FILES)
            m.downloader_factory = lambda target, progress, cancel: sp.ModelDownloader(
                target, session=self.session, progress=progress, cancel=cancel)
            if model:
                self.assertEqual(sp.ModelDownloader(sp.model_dir(m.models), session=FakeSession(MODEL_FILES)).run(),
                                 "")
            m.start()
        return app

    def open_card(self, info: mt.MeetingInfo | None = None) -> meetings_view.StartCard:
        if info is None:
            self.m.stage.record_button.click()
        else:
            self.m.open_start_card(info)
        settle()
        return self.m.stage.card

    def record(self, info: mt.MeetingInfo | None = None) -> mt.MeetingRecord:
        card = self.open_card(info)
        card.tick.click()
        card.start_button.click()
        settle()
        self.assertTrue(self.m.is_recording())
        return self.m.recorder.record

    def tick(self, seconds: float, *, mic: int = 3000, step: float = 1.0) -> None:
        elapsed = 0.0
        while elapsed < seconds and self.m.is_recording():
            self.clock.advance(step)
            elapsed += step
            self.backend.live(mt.MIC).feed(tone(step, mic))
            self.m.recorder.tick()
        self.m._sync_indicators()
        settle()


class ConsentTests(MeetingsApp):
    def test_start_stays_disabled_until_the_tick_and_the_tick_is_never_remembered(self) -> None:
        self.make()
        card = self.open_card()
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_MEETINGS)
        self.assertTrue(card.isVisible())
        self.assertFalse(card.tick.isChecked())
        self.assertEqual(card.tick.text(), "Everyone in this meeting has agreed to be recorded")
        self.assertFalse(card.start_button.isEnabled())
        card.start_button.click()                                      # disabled: nothing happens
        card._on_start()                                               # even called directly: no tick, no start
        settle()
        self.assertFalse(self.m.is_recording())
        self.assertEqual(self.m.store.list(), [])
        card.tick.click()
        self.assertTrue(card.start_button.isEnabled())
        card.start_button.click()
        settle()
        self.assertTrue(self.m.is_recording())
        record = self.m.store.load(self.m.recorder.record.id)
        self.assertTrue(record.consent_confirmed)
        self.assertEqual(record.consent_at, self.clock.wall().replace(microsecond=0))
        self.m.stop()
        settle()
        card = self.open_card()                                        # the next meeting: unticked again
        self.assertFalse(card.tick.isChecked())
        self.assertFalse(card.start_button.isEnabled())

    def test_no_start_by_voice_offer_find_or_a_bare_call(self) -> None:
        self.make()
        for bad in (None, object(), "start", {"consent": True}):
            self.assertFalse(self.m.start_recording(bad))
        with self.assertRaises(mt.ConsentMissing):
            mt.StartRequest(mt.MeetingInfo(), False, NOW)
        self.reading.homeLink.emit(ui.FIND_RECORD_KEY)                  # an offer's link: never records
        self.reading.homeLink.emit("meetings:start")
        settle()
        self.assertFalse(self.m.is_recording())
        items = [item for item in self.reading.find_items("record") if item.key == ui.FIND_RECORD_KEY]
        self.assertEqual([item.label for item in items], ["Record a meeting"])
        self.reading.set_tab(ui.SCREEN_HOME)
        self.reading._on_find_pick(items[0])                           # the Find list opens the card ...
        settle()
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_MEETINGS)
        self.assertTrue(self.m.stage.card.isVisible())
        self.assertFalse(self.m.is_recording())                        # ... and records nothing
        self.assertEqual(self.m.store.list(), [])
        self.c.voice.say("reply", "Recording now, sir.")               # what Jarvis says starts nothing either
        self.assertFalse(self.m.is_recording())

    def test_the_card_names_the_meeting_and_both_devices(self) -> None:
        self.make()
        info = mt.MeetingInfo(TITLE, NOW, NOW + timedelta(minutes=30), "e1", GUESTS)
        card = self.open_card(info)
        self.assertEqual(card.title_label.text(), TITLE)
        self.assertIn("Ana Lopez and Ben Okafor", card.meta_label.text())
        self.assertEqual(card.device_texts(), (f"You - microphone: {MIC_A.name} (Windows default)",
                                               f"Others - call audio: {OUT_A.name} (Windows default)"))
        self.m.pick_devices(microphone=MIC_B.name)
        card = self.open_card(info)
        self.assertEqual(card.device_texts()[0], f"You - microphone: {MIC_B.name} (picked in Settings)")

    def test_a_missing_add_on_keeps_start_disabled_and_says_so(self) -> None:
        self.make(audio_missing="PyAudioWPatch (or soundcard)")
        card = self.open_card()
        card.tick.click()
        self.assertFalse(card.start_button.isEnabled())
        self.assertIn("PyAudioWPatch (or soundcard)", card.problem_label.text())
        self.assertIn("never installs", card.problem_label.text())


class IndicatorTests(MeetingsApp):
    def test_rec_on_every_screen_the_orb_the_dock_and_the_tray(self) -> None:
        self.make()
        self.record()
        self.tick(64)
        for screen in ui.SCREENS:
            self.reading.set_tab(screen)
            settle()
            self.assertTrue(self.reading.rec_badge.isVisible(), screen)
            self.assertEqual(self.reading.recording_text(), "REC 1:04")
        self.reading.set_tab(ui.SCREEN_HOME)
        settle()
        self.assertTrue(self.reading.home_rec.isVisible())
        self.assertTrue(self.reading.hero.is_recording())
        self.assertIn("recording a meeting", self.reading.hero.accessibleDescription())
        self.assertEqual(self.reading.dock.signal_words(dock.MEETINGS), "recording")
        self.assertEqual(self.c._tray_tooltip(), "Jarvis: Recording 1:04")
        self.c.tray = mock.Mock()
        self.c._update_tray("Briefing: asking again at 10:40 AM")         # recording wins over any other tooltip
        self.c.tray.setToolTip.assert_called_with("Jarvis: Recording 1:04")
        self.m.stop()
        settle()
        self.assertFalse(self.reading.rec_badge.isVisible())
        self.assertFalse(self.reading.home_rec.isVisible())
        self.assertFalse(self.reading.hero.is_recording())
        self.assertEqual(self.reading.dock.signal_words(dock.MEETINGS), "")
        self.assertNotIn("Recording", self.c._tray_tooltip())

    def test_the_pill_is_on_top_never_takes_focus_and_stays_when_minimised(self) -> None:
        self.make()
        self.c.window.activateWindow()
        settle()
        self.record()
        pill = self.m.pill
        self.assertIsNotNone(pill)
        self.assertTrue(pill.isVisible())
        flags = pill.windowFlags()
        for flag in (Qt.WindowType.WindowStaysOnTopHint, Qt.WindowType.WindowDoesNotAcceptFocus,
                     Qt.WindowType.Tool, Qt.WindowType.FramelessWindowHint):
            self.assertTrue(flags & flag, flag)
        self.assertTrue(pill.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertEqual(pill.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertEqual(pill.stop_button.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertIsNot(QApplication.activeWindow(), pill)
        self.assertIsNone(pill.parent())                               # its own window: not hidden with Jarvis
        self.c.window.showMinimized()
        settle()
        self.tick(3)
        self.assertTrue(pill.isVisible())
        self.assertEqual(pill.text(), "Recording 0:03")
        self.c.window.hide()
        self.tick(1)
        self.assertTrue(pill.isVisible())
        self.c.window.showNormal()
        settle()


class StopTests(MeetingsApp):
    def saved(self, record: mt.MeetingRecord) -> mt.MeetingRecord:
        settle()
        self.assertFalse(self.m.is_recording())
        self.assertFalse(self.m.pill is not None and self.m.pill.isVisible())
        self.assertFalse(self.reading.rec_badge.isVisible())
        saved = self.m.store.load(record.id)
        self.assertIn(saved.status, (mt.STATUS_RECORDED, mt.STATUS_TRANSCRIBING))
        for channel in mt.CHANNELS:
            self.assertTrue(self.m.store.audio_path(record.id, channel).is_file())
        return saved

    def test_stop_from_the_pill_the_header_home_and_the_meetings_row(self) -> None:
        self.make()
        for press in (lambda: self.m.pill.stop_button.click(),
                      lambda: self.reading.rec_badge.stop_button.click(),
                      lambda: self.reading.home_rec.stop_button.click(),
                      lambda: self.m.stage.row(self.m.recorder.record.id).stop_button.click()):
            record = self.record()
            self.tick(5)
            press()
            self.assertEqual(self.saved(record).stop_reason, mt.STOP_BUTTON)
        self.assertEqual(len(self.m.store.list()), 4)

    def test_end_time_plus_15_minutes(self) -> None:
        self.make()
        record = self.record(mt.MeetingInfo(TITLE, NOW - timedelta(minutes=20), NOW + timedelta(minutes=10)))
        self.tick(24 * 60 + 50, step=10)
        self.assertTrue(self.m.is_recording())
        self.tick(20, step=10)
        self.assertEqual(self.saved(record).stop_reason, mt.STOP_END_TIME)

    def test_silence_and_the_cap(self) -> None:
        self.make()
        with mock.patch.object(ma, "SILENCE_S", 60):
            record = self.record()
            self.tick(70, mic=0, step=10)
        self.assertEqual(self.saved(record).stop_reason, mt.STOP_SILENCE)
        with mock.patch.object(ma, "MAX_S", 60):
            record = self.record()
            self.tick(70, step=10)
        self.assertEqual(self.saved(record).stop_reason, mt.STOP_CAP)

    def test_closing_jarvis_stops_and_saves(self) -> None:
        self.make()
        record = self.record()
        self.tick(5)
        with mock.patch.object(ui, "_remove_audio_dir_after"):
            self.c.shutdown()
        settle()
        saved = self.m.store.load(record.id)
        self.assertEqual((saved.status, saved.stop_reason), (mt.STATUS_RECORDED, mt.STOP_CLOSING))
        self.assertIsNone(self.m.pill)
        self.assertAlmostEqual(ma.wav_seconds(self.m.store.audio_path(record.id, mt.MIC)), 5.0, delta=0.1)

    def test_a_crash_is_saved_at_the_next_start(self) -> None:
        self.make()
        record = self.record()
        self.tick(4)
        recorder, self.m.recorder = self.m.recorder, None              # Jarvis "ended": nothing closed
        self.assertEqual(self.m.store.recover(), [record.id])
        saved = self.m.store.load(record.id)
        self.assertEqual((saved.status, saved.stop_reason), (mt.STATUS_RECORDED, mt.STOP_INTERRUPTED))
        recorder._recording = False
        for channel in recorder._channels.values():
            channel.writer.close()


class DeviceTests(MeetingsApp):
    def test_settings_meetings_picks_devices_and_a_switch_mid_meeting_is_noted(self) -> None:
        self.make()
        settings = self.reading.settings_page
        self.assertIn("meetings", settings.group_buttons)
        self.reading.set_tab(ui.SCREEN_SETTINGS)
        settings.select_group("meetings")
        settle()
        panel = self.m.settings_panel
        self.assertTrue(panel.isVisible())
        self.assertIn(f"Follows the Windows default ({MIC_A.name})", panel.mic_row.value_label.text())
        self.assertEqual(panel.call_row.menu_items(), [meetings_view.FOLLOW_DEFAULT, OUT_A.name, OUT_B.name])
        panel.call_row.choose(OUT_B.name)
        self.assertEqual(self.m.prefs.call_audio, OUT_B.name)
        saved = json.loads((self.m.store.root / mt.DEVICES_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved, {"microphone": "", "call_audio": OUT_B.name})
        self.assertEqual(panel.call_row.value_label.text(), OUT_B.name)
        panel.call_row.choose(meetings_view.FOLLOW_DEFAULT)
        self.assertEqual(self.m.prefs.call_audio, "")
        rows = dict(settings.rows())
        self.assertIn("7 days", rows["Audio"])
        self.assertIn("30 days", rows["Transcript and summary"])
        record = self.record()
        self.backend.default_out = OUT_B                              # the headset: the new default output
        self.tick(3)
        self.assertEqual(self.backend.live(mt.LOOPBACK).device, OUT_B)
        self.m.stop()
        settle()
        saved = self.m.store.load(record.id)
        self.assertEqual(saved.devices["call_audio"], OUT_B.name)
        self.assertEqual([c["change"] for c in saved.device_changes], ["switched"])
        row = self.m.stage.row(record.id)
        self.assertIn("switched device", row.note_label.text())


class StorageTests(MeetingsApp):
    def test_meetings_live_in_the_data_folder_and_retention_runs_at_start_up(self) -> None:
        self.make()
        self.assertEqual(self.m.store.root, self.c.config.data_dir / "meetings")
        self.assertEqual(self.m.store.root.parent.name, "briefing-reader")
        record = self.record()
        self.tick(2)
        self.m.stop()
        settle()
        self.assertTrue(self.m.store.audio_path(record.id, mt.MIC).is_file())
        self.clock.start = NOW + timedelta(days=8)                      # a week later: the audio goes
        self.assertEqual(self.m.cleanup(), (2, 0))
        self.assertFalse(self.m.store.audio_path(record.id, mt.MIC).exists())
        self.assertEqual(self.m.stage.row(record.id).info.status_text, "Audio deleted after 7 days")
        self.clock.start = NOW + timedelta(days=31)
        self.m.start()                                                  # at start-up too
        self.assertFalse(self.m.store.folder(record.id).exists())
        self.assertEqual(self.m.stage.rows(), [])


class DownloadAndWritingTests(MeetingsApp):
    def test_the_model_downloads_only_on_the_click(self) -> None:
        self.make()
        self.assertEqual(self.session.requests, [])                     # opening Jarvis fetched nothing
        stage = self.m.stage
        self.assertIn("The speech model medium.en (about 1.5 GB)", stage.missing_text())
        self.assertTrue(stage.download_button.isVisibleTo(stage))
        self.assertIn("Systran/faster-whisper-medium.en", stage.download_label.text())
        self.session.cut_after = 500                                    # the first try breaks off
        stage.download_button.click()
        settle()
        self.assertIn("The download stopped (the connection dropped)", stage.download_label.text())
        self.assertFalse(sp.model_ready(sp.model_dir(self.m.models)))
        stage.download_button.click()                                   # continues where it stopped
        settle()
        self.assertIn(("GET", "model.bin", "bytes=500-"), self.session.requests)
        self.assertTrue(sp.model_ready(sp.model_dir(self.m.models)))
        self.assertEqual(sp.model_dir(self.m.models).parent, self.c.config.data_dir / "models")
        self.assertNotIn("speech model", stage.missing_text())
        self.assertFalse(self.m.download_model())                      # ready: nothing more is fetched

    def test_missing_add_ons_are_named_exactly(self) -> None:
        self.make(audio_missing="PyAudioWPatch (or soundcard)", speech_missing="faster-whisper")
        text = self.m.stage.missing_text()
        self.assertIn("Missing: The Python add-on PyAudioWPatch (or soundcard)", text)
        self.assertIn("Missing: The Python add-on faster-whisper", text)
        self.assertIn("Missing: The speech model medium.en", text)

    def test_writing_down_progress_you_and_others_failure_and_retry(self) -> None:
        self.make(model=True)
        record = self.record()
        self.backend.live(mt.LOOPBACK).feed(tone(2, 2500))
        self.tick(4)
        self.m.stop()
        settle()
        (job,) = FakeJob.jobs
        self.assertTrue(job.started)
        self.assertEqual(job.folder, self.m.store.folder(record.id))
        row = self.m.stage.row(record.id)
        self.assertEqual(row.status_label.text(), "Writing it down 0%")
        job.progress(40)
        settle()
        self.assertEqual(self.m.stage.row(record.id).status_label.text(), "Writing it down 40%")
        self.assertEqual(self.m.store.load(record.id).progress, 40)
        job.finish(sp.ERR_WORKER)
        settle()
        row = self.m.stage.row(record.id)
        self.assertEqual(row.info.status_text, "Failed")
        self.assertTrue(row.retry_button.isVisibleTo(row))
        row.retry_button.click()
        settle()
        job = FakeJob.jobs[-1]
        job.run(FakeTranscriber({"mic.wav": [sp.Segment(0.0, 1.0, "Shall we start?")],
                                 "loopback.wav": [sp.Segment(1.5, 3.0, SPOKEN)]}))
        settle()
        self.assertEqual(self.m.stage.row(record.id).info.status_text, "Transcript ready")
        transcript = json.loads(self.m.store.transcript_path(record.id).read_text(encoding="utf-8"))
        self.assertEqual([turn["speaker"] for turn in transcript["turns"]], ["You", "Others"])

    def test_nothing_is_written_down_until_the_model_is_there(self) -> None:
        self.make()
        record = self.record()
        self.tick(2)
        self.m.stop()
        settle()
        self.assertEqual(FakeJob.jobs, [])
        self.assertEqual(self.m.stage.row(record.id).info.status_text, "Waiting to be written down")
        self.m.stage.download_button.click()
        settle()
        self.assertEqual(len(FakeJob.jobs), 1)                          # queued meetings start once it is ready


class MeetingsScreenTests(MeetingsApp):
    def test_list_statuses_and_delete_asked_twice(self) -> None:
        self.make()
        store = self.m.store
        made = []
        for minutes, status, progress in ((10, mt.STATUS_TRANSCRIBED, 100), (20, mt.STATUS_TRANSCRIBING, 40),
                                          (30, mt.STATUS_FAILED, 0)):
            self.clock.start = NOW - timedelta(minutes=minutes)
            record = store.create(mt.StartRequest(mt.MeetingInfo(f"{TITLE} {minutes}"), True, NOW))
            store.update(record.id, status=status, progress=progress, duration_s=724.0)
            made.append(record.id)
        self.clock.start = NOW
        self.m.refresh()
        live = self.record()
        self.tick(3)
        rows = self.m.stage.rows()
        self.assertEqual([row.info.id for row in rows], [live.id, *made])
        self.assertEqual([row.info.status_text for row in rows],
                         ["Recording", "Transcript ready", "Writing it down 40%", "Failed"])
        self.assertEqual(rows[1].length_label.text(), "12:04")
        self.assertEqual(rows[1].title_label.text(), f"{TITLE} 10")
        self.assertFalse(rows[0].delete_button.isVisibleTo(rows[0]))     # the recording one: Stop, no Delete
        self.assertTrue(rows[3].retry_button.isVisibleTo(rows[3]))
        target = rows[1]
        target.delete_button.click()
        settle()
        self.assertTrue(target.asking())
        self.assertTrue(store.folder(made[0]).exists())                  # asked first: nothing deleted yet
        target.keep_button.click()
        self.assertFalse(target.asking())
        target.delete_button.click()
        target.sure_button.click()
        settle()
        self.assertFalse(store.folder(made[0]).exists())
        self.assertIsNone(self.m.stage.row(made[0]))
        self.assertFalse(self.m.delete(live.id))                       # never the meeting that is recording
        self.m.stop()
        settle()


class SwitchOffTests(MeetingsApp):
    def test_off_hides_every_meetings_surface(self) -> None:
        self.make(enabled=False)
        self.assertIsNone(self.c.meetings)
        reading = self.reading
        self.assertFalse(reading.meetings_available())
        self.assertNotIn(dock.MEETINGS, reading.dock.visible_slots())
        self.assertNotIn(ui.FIND_RECORD_KEY, [item.key for item in reading.find_items("")])
        self.assertNotIn(ui.FIND_RECORD_KEY, [item.key for item in reading.find_items("record")])
        self.assertNotIn("meetings", reading.settings_page.group_buttons)
        reading.set_tab(ui.SCREEN_MEETINGS)
        self.assertNotEqual(reading.current_tab(), ui.SCREEN_MEETINGS)
        reading._on_find_pick(find_view.FindItem(find_view.KIND_ACTION, ui.FIND_RECORD_KEY, "Record a meeting"))
        self.assertNotEqual(reading.current_tab(), ui.SCREEN_MEETINGS)
        self.assertFalse(reading.rec_badge.isVisible())
        self.assertFalse((self.c.config.data_dir / "meetings").exists())
        self.assertNotIn("Recording", self.c._tray_tooltip())


class PrivacyTests(MeetingsApp):
    def test_logs_carry_no_titles_names_devices_or_text(self) -> None:
        with self.assertLogs("briefing_reader", logging.DEBUG) as logs:
            self.make(model=True)
            record = self.record(mt.MeetingInfo(TITLE, NOW, NOW + timedelta(minutes=5), "evt-1", GUESTS))
            self.backend.default_out = OUT_B
            self.tick(4)
            self.m.stop()
            settle()
            FakeJob.jobs[-1].run(FakeTranscriber({"loopback.wav": [sp.Segment(0.0, 2.0, SPOKEN)]}))
            settle()
            self.m.pick_devices(microphone=MIC_B.name)
            self.m.delete(record.id)
            logging.getLogger("briefing_reader.meetings").info("Meetings: check %s", record.id)
        text = "\n".join(logs.output)
        for secret in (TITLE, "Zq7", *GUESTS, MIC_A.name, MIC_B.name, OUT_A.name, OUT_B.name, "Owner", SPOKEN,
                       "budget"):
            self.assertNotIn(secret, text)
        self.assertIn(record.id, text)


if __name__ == "__main__":
    unittest.main()
