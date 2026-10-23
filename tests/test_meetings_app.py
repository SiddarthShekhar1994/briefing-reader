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

from briefing_reader import context_view, dock, find_view, meeting_audio as ma, meeting_speech as sp, meetings as mt
from briefing_reader import meetings_ui, meetings_view, ui
from briefing_reader.gcal import CalendarEvent
from tests.meeting_fakes import MIC_A, MIC_B, OUT_A, OUT_B, Clock, FakeBackend, FakeSession, FakeTranscriber, tone
from tests.ui_fakes import NOW, AppHarness, fonts, settle, wait_for

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
        self.c._sweep_meetings()                                        # the start-up's retention: nothing is made
        self.assertFalse((self.c.config.data_dir / "meetings").exists())
        self.assertNotIn("Recording", self.c._tray_tooltip())
        self.c._agenda_status, self.c._agenda_events = ui._AGENDA_OK, [
            CalendarEvent(TITLE, NOW + timedelta(minutes=30), NOW + timedelta(minutes=60))]
        self.assertFalse(self.c._context_facts().record)               # no Record on Home's tile ...
        self.c._agenda_range = (NOW - timedelta(days=1), NOW + timedelta(days=2))
        self.c._render_agenda()
        self.assertEqual([row.info().record for row in reading.agenda_panel.event_rows()], [""])   # ... or a row

    def test_meetings_recorded_before_it_was_turned_off_still_go_by_retention(self) -> None:
        self.make(enabled=False)
        root = self.c.config.data_dir / "meetings"
        made = {}
        for days in (8, 31):
            store = mt.MeetingStore(root, now=lambda d=days: NOW - timedelta(days=d))
            record = store.create(mt.StartRequest(mt.MeetingInfo(), True, NOW - timedelta(days=days)))
            for channel in mt.CHANNELS:
                store.audio_path(record.id, channel).write_bytes(b"RIFF")
            store.update(record.id, status=mt.STATUS_TRANSCRIBED)
            made[days] = record.id
        self.c._sweep_meetings()
        store = mt.MeetingStore(root)
        self.assertFalse(store.audio_path(made[8], mt.MIC).exists())   # audio after 7 days
        self.assertTrue(store.folder(made[8]).exists())
        self.assertFalse(store.folder(made[31]).exists())               # everything after 30 days
        self.assertIsNotNone(self.c._meetings_sweep_timer)             # and daily while Jarvis runs
        self.assertTrue(self.c._meetings_sweep_timer.isActive())
        self.assertIsNone(self.c.meetings)                              # still no Meetings surface


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


class ConsentFreshnessTests(MeetingsApp):
    def ticked(self) -> meetings_view.StartCard:
        card = self.open_card()
        card.tick.click()
        self.assertTrue(card.start_button.isEnabled())
        return card

    def test_another_screen_closes_the_card_and_its_tick(self) -> None:
        self.make()
        card = self.ticked()
        self.reading.set_tab(ui.SCREEN_HOME)                            # the owner is pulled away
        settle()
        self.assertFalse(card.isVisible())
        self.assertFalse(card.tick.isChecked())
        self.clock.advance(6 * 3600)
        self.reading.dock.activate(dock.MEETINGS)                       # hours later, Ctrl+7 / the Dock
        settle()
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_MEETINGS)
        self.assertFalse(card.isVisible())                              # no old card waiting with its tick
        card.start_button.click()
        settle()
        self.assertFalse(self.m.is_recording())
        card = self.open_card()
        self.assertFalse(card.tick.isChecked())

    def test_hiding_or_minimising_the_window_closes_the_card(self) -> None:
        self.make()
        card = self.ticked()
        self.c.window.hide()
        settle()
        self.assertFalse(card.tick.isChecked())
        self.c.window.show()
        settle()
        self.assertFalse(card.isVisible())
        card = self.ticked()
        self.c.window.showMinimized()
        settle()
        self.assertFalse(card.tick.isChecked())
        self.c.window.showNormal()
        settle()
        self.assertFalse(card.isVisible())
        self.assertFalse(self.m.is_recording())

    def test_a_tick_counts_five_minutes_and_the_record_keeps_when_it_was_given(self) -> None:
        self.make()
        card = self.ticked()
        self.clock.advance(meetings_view.CONSENT_FRESH_S + 1)
        card.start_button.click()
        settle()
        self.assertFalse(self.m.is_recording())                         # too old: tick again
        self.assertFalse(card.tick.isChecked())
        self.assertTrue(card.stale_label.isVisible())
        card.tick.click()
        ticked_at = self.clock.wall()
        self.clock.advance(40)                                          # Start a little later
        card.start_button.click()
        settle()
        self.assertTrue(self.m.is_recording())
        record = self.m.store.load(self.m.recorder.record.id)
        self.assertEqual(record.consent_at, ticked_at.replace(microsecond=0))   # when the box was ticked
        self.m.stop()
        settle()


class FrameHeaderRecTests(MeetingsApp):
    def test_rec_shows_over_the_prompt_view_too(self) -> None:
        self.make()
        self.record()
        self.tick(5)
        window = self.c.window
        window.show_prompt_view()                                       # "Hear it now?" while it records
        settle()
        self.assertFalse(window.is_reading_view())
        self.assertTrue(window.header.isVisible())
        self.assertTrue(window.frame_rec.isVisible())
        self.assertEqual(window.frame_rec.text(), "REC 0:05")
        self.assertGreaterEqual(window.header.width(), window.header.minimumSizeHint().width() - 1)
        window.frame_rec.stop_button.click()                            # its Stop is the header's Stop
        settle()
        self.assertFalse(self.m.is_recording())
        self.assertFalse(window.frame_rec.isVisible())
        window.show_reading_view()
        settle()


class MeetingChoiceTests(MeetingsApp):
    def test_record_a_meeting_links_none_and_offers_the_next_meeting_first(self) -> None:
        """Owner decision 2026-10-22 (it replaces the card that picked the likeliest meeting by itself): Record a
        meeting opens the card linked to no calendar meeting; "Link a calendar meeting" offers the meetings on now -
        the next one before the one that is ending - and links the one clicked (its title into the empty box, its
        own end for the auto-stop); Unlink removes the link and the title it filled in."""
        self.make()
        ending = CalendarEvent("Zq7 Internal sync", NOW - timedelta(minutes=52), NOW + timedelta(minutes=8))
        starting = CalendarEvent("Zq7 Client call", NOW + timedelta(minutes=8), NOW + timedelta(minutes=38))
        found = meetings_ui.meetings_now([ending, starting], NOW)
        self.assertEqual([info.title for info in found], ["Zq7 Client call", "Zq7 Internal sync"])
        self.c._agenda_events = [ending, starting]
        card = self.open_card()                                          # Record a meeting on the Meetings screen
        self.assertIsNone(card.linked())                                 # not linked by default
        self.assertEqual((card.info(), card.title_edit.text()), (mt.MeetingInfo(), ""))
        self.assertFalse(card.title_label.isVisibleTo(card))
        self.assertIn(meetings_view.UNLINKED_NOTE, card.meta_label.text())
        choices = card.link_choices()
        self.assertEqual([info.title for _text, info in choices], ["Zq7 Client call", "Zq7 Internal sync"])
        self.assertTrue(card.link_button.isVisibleTo(card))
        self.assertFalse(card.unlink_button.isVisibleTo(card))
        card.tick.click()
        self.assertTrue(card.link_meeting(0))                            # Link a calendar meeting: the next one
        self.assertEqual((card.title_label.text(), card.title_edit.text()), ("Zq7 Client call", "Zq7 Client call"))
        self.assertEqual(card.info().end, starting.end)                 # its own end drives the auto-stop
        self.assertFalse(card.tick.isChecked())                         # a tick is for one meeting only
        self.assertEqual([info.title for _text, info in card.link_choices()], ["Zq7 Internal sync"])
        self.assertTrue(card.unlink_button.isVisibleTo(card))
        card.unlink()                                                    # ... or none again
        self.assertIsNone(card.linked())
        self.assertEqual(card.title_edit.text(), "")                    # the title the link filled in goes with it
        self.assertIsNone(card.info().end)

    def test_a_block_of_time_gives_way_to_a_real_meeting(self) -> None:
        now = NOW
        focus = meetings_ui.meeting_rank(now - timedelta(minutes=2), now, block=True)
        call = meetings_ui.meeting_rank(now + timedelta(minutes=2), now, guests=2)
        self.assertLess(call, focus)
        solo = meetings_ui.meeting_rank(now + timedelta(minutes=2), now)
        self.assertLess(call, solo)                                     # same start: the one with guests first


class StartFailureAppTests(MeetingsApp):
    def test_a_start_whose_record_cannot_be_saved_leaves_no_device_open(self) -> None:
        self.make()
        real_save = self.m.store.save
        calls = []

        def save(record: mt.MeetingRecord) -> None:
            calls.append(record.id)
            if len(calls) == 2:                                          # the recorder's own save fails
                raise PermissionError("meeting.json locked")
            real_save(record)

        card = self.open_card()
        card.tick.click()
        with mock.patch.object(self.m.store, "save", save):
            card.start_button.click()
            settle()
        self.assertFalse(self.m.is_recording())
        self.assertIsNone(self.m.pill)
        self.assertTrue(self.backend.streams and all(stream.closed for stream in self.backend.streams))
        self.assertEqual(self.m.store.list(), [])
        self.assertFalse(any(self.m.store.root.glob("*/*.wav")))         # every file closed and removed
        self.assertIn("could not be written", card.problem_label.text())


class DeleteLeftoverAppTests(MeetingsApp):
    def test_a_file_the_worker_still_holds_keeps_the_row_with_delete_again(self) -> None:
        self.make(model=True)
        record = self.record()
        self.tick(3)
        self.m.stop()
        settle()
        job = FakeJob.jobs[-1]
        real_unlink = Path.unlink

        def held(path: Path, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202 - Path.unlink's own
            if path.name == "loopback.wav":
                raise PermissionError("in use")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", held), mock.patch.object(meetings_ui, "DELETE_RETRY_S", 0.0):
            self.assertFalse(self.m.delete(record.id))
        settle()
        self.assertTrue(job.cancelled)
        row = self.m.stage.row(record.id)
        self.assertIsNotNone(row)                                       # still listed, not silently gone
        self.assertEqual(row.info.status_text, mt.DELETE_LEFT_TEXT)
        self.assertTrue(row.delete_button.isVisibleTo(row))
        self.assertTrue(self.m.delete(record.id))                       # Delete again
        settle()
        self.assertIsNone(self.m.stage.row(record.id))
        self.assertFalse(self.m.store.folder(record.id).exists())


class CallAudioAppTests(MeetingsApp):
    def test_no_call_audio_is_said_on_rec_the_pill_the_row_and_the_tray(self) -> None:
        self.make()
        self.backend.loop_fails = True
        record = self.record()
        self.tick(3)
        self.assertEqual(self.reading.rec_badge.warning(), meetings_view.CALL_MISSING_TEXT)
        self.assertEqual(self.reading.home_rec.warning(), meetings_view.CALL_MISSING_TEXT)
        self.assertEqual(self.m.pill.warning(), meetings_view.CALL_MISSING_TEXT)
        row = self.m.stage.row(record.id)
        self.assertEqual(row.note_label.text(), meetings_view.CALL_MISSING_ROW)
        self.assertEqual(self.c._tray_tooltip(), "Jarvis: Recording 0:03 (no call audio)")
        self.backend.loop_fails = False                                  # it can be opened now
        self.tick(3)
        self.assertEqual(self.reading.rec_badge.warning(), "")
        self.assertEqual(self.m.pill.warning(), "")
        self.assertNotEqual(self.m.stage.row(record.id).note_label.text(), meetings_view.CALL_MISSING_ROW)
        self.m.stop()
        settle()
        note = self.m.stage.row(record.id).info.note
        self.assertIn("the call audio was recorded only once its device could be opened", note)
        self.assertNotIn("switched", note)

    def test_the_row_note_says_what_happened(self) -> None:
        words = meetings_view.device_note
        self.assertEqual(words([{"channel": "microphone", "change": "switched"}]),
                         "the microphone switched device during the meeting")
        self.assertEqual(words([{"channel": "call audio", "change": "unavailable"}]),
                         "the call audio was not recorded (its device could not be opened)")
        self.assertEqual(words([{"channel": "call audio", "change": "reopened"}]),
                         "the call audio stopped for a moment")
        self.assertEqual(words([]), "")


class QuietWhileRecordingTests(MeetingsApp):
    def test_jarvis_keeps_quiet_while_a_meeting_records(self) -> None:
        self.make()
        voice = self.app.voice
        said_before = list(voice.texts())
        self.record()
        self.assertFalse(self.c._say("notice", "One moment, sir."))      # shown, never spoken
        self.assertTrue(self.c._announce())                              # the briefing's announcement ...
        self.assertEqual(voice.texts(), said_before)                     # ... waits: nothing heard by the others
        self.assertIsNotNone(self.c._held_key)
        self.assertFalse(self.c._prod_facts().speak_ok)                  # offers stay text only
        self.m.stop()
        settle()
        self.assertTrue(wait_for(lambda: len(voice.texts()) > len(said_before), 5))
        self.assertIn("ready to view", voice.texts()[-1])                # said once it stopped
        self.assertTrue(self.c._say("notice", "One moment, sir."))


class DownloadNeverStuckTests(MeetingsApp):
    def test_a_download_that_breaks_always_ends_with_its_words(self) -> None:
        self.make()

        class Broken:
            def run(self) -> str:
                raise OSError("a scanner holds the file")

        self.m.downloader_factory = lambda target, progress, cancel: Broken()
        self.m.threaded = True
        self.assertTrue(self.m.download_model())
        self.assertTrue(wait_for(lambda: not self.m.downloading(), 5))
        settle()
        stage = self.m.stage
        self.assertIn("The download stopped (the file could not be saved)", stage.download_label.text())
        self.assertTrue(stage.download_button.isVisibleTo(stage))       # Download is there again
        self.m.threaded = False


class FreshDevicesTests(MeetingsApp):
    def test_each_card_and_settings_look_at_the_devices_again_never_while_recording(self) -> None:
        self.make()
        made: list[FakeBackend] = []

        def factory() -> tuple[FakeBackend, str]:
            backend = FakeBackend()
            if made:
                backend.default_out = OUT_B                              # a headset plugged in since
            made.append(backend)
            self.backend = backend
            return backend, ""

        self.m.backend_factory = factory
        self.m._backend = None
        card = self.open_card()
        self.assertIn(OUT_A.name, card.device_texts()[1])
        card = self.open_card()
        self.assertIn(OUT_B.name, card.device_texts()[1])                # the second card sees the headset
        card.tick.click()
        card.start_button.click()
        settle()
        self.assertTrue(self.m.is_recording())
        count = len(made)
        self.m.refresh_settings()
        self.assertEqual(len(made), count)                               # never remade under a recording
        self.m.stop()
        settle()


class StartPointTests(MeetingsApp):
    def agenda(self) -> tuple[CalendarEvent, CalendarEvent]:
        past = CalendarEvent("Zq7 Morning standup", NOW - timedelta(hours=3), NOW - timedelta(hours=2))
        later = CalendarEvent(TITLE, NOW + timedelta(minutes=40), NOW + timedelta(minutes=70))
        self.c._agenda_status = ui._AGENDA_OK
        self.c._agenda_events = [past, later]
        self.c._agenda_range = (NOW - timedelta(days=1), NOW + timedelta(days=2))
        return past, later

    def test_record_on_homes_next_meeting_tile_only_opens_its_card(self) -> None:
        self.make()
        _past, later = self.agenda()
        facts = self.c._context_facts()
        self.assertTrue(facts.record)
        tiles = context_view.context_tiles(facts)
        self.assertTrue(tiles[0].record)
        self.reading.set_tab(ui.SCREEN_HOME)
        settle()
        self.assertGreater(self.reading.show_context(tiles), 0)
        tile = next(t for t in self.reading.stage.context.tiles() if t.info.kind == context_view.KIND_MEETING)
        self.assertIsNotNone(tile.record_button)
        tile.record_button.click()
        settle()
        card = self.m.stage.card
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_MEETINGS)
        self.assertTrue(card.isVisible())
        self.assertEqual((card.title_label.text(), card.info().start), (TITLE, later.start))
        self.assertFalse(card.tick.isChecked())
        self.assertFalse(card.start_button.isEnabled())
        self.assertFalse(self.m.is_recording())                          # it only opened the card

    def test_record_on_a_day_view_row_only_opens_its_card_and_goes_while_recording(self) -> None:
        self.make()
        _past, later = self.agenda()
        self.c._render_agenda()
        rows = self.reading.agenda_panel.event_rows()
        self.assertEqual([row.record_button() is not None for row in rows], [False, True])   # not for a past one
        rows[1].record_button().click()
        settle()
        card = self.m.stage.card
        self.assertTrue(card.isVisible())
        self.assertEqual((card.title_label.text(), card.info().end), (TITLE, later.end))
        self.assertFalse(card.tick.isChecked())
        self.assertFalse(self.m.is_recording())
        card.tick.click()
        card.start_button.click()
        settle()
        self.assertTrue(self.m.is_recording())
        rows = self.reading.agenda_panel.event_rows()
        self.assertEqual([row.record_button() for row in rows], [None, None])   # one recording at a time
        self.assertFalse(self.c._context_facts().record)
        self.m.stop()
        settle()
        self.assertIsNotNone(self.reading.agenda_panel.event_rows()[1].record_button())


INVITE = (("Ana Lopez", "zq7ana@example.org"), ("Ben Okafor", "zq7ben@example.org"))


class UniversalCardTests(MeetingsApp):
    """Owner decisions 2026-10-22: any meeting - a title (blank: "Meeting" with its date and time), In person or On a
    call (the kind recorded last; the consent tick never remembered) and an optional calendar link."""

    def calendar_meeting(self) -> mt.MeetingInfo:
        return mt.MeetingInfo(TITLE, NOW + timedelta(minutes=5), NOW + timedelta(minutes=35), "evt-9", GUESTS,
                              alias="work", calendar_id="primary", invite=INVITE)

    def test_a_typed_title_is_kept_and_a_blank_one_is_meeting_with_its_date_and_time(self) -> None:
        self.make()
        card = self.open_card()
        self.assertTrue(card.title_edit.isVisibleTo(card))
        self.assertEqual(card.title_edit.text(), "")
        self.assertIn("left blank: Meeting, Sun 4 Oct 1:52 PM", card.title_edit.placeholderText())
        card.title_edit.setText("  Zq7   Weekly   sync  ")
        card.tick.click()
        card.start_button.click()
        settle()
        record = self.m.store.load(self.m.recorder.record.id)
        self.assertEqual((record.title, record.linked, record.kind), ("Zq7 Weekly sync", False, mt.KIND_CALL))
        self.m.stop()
        settle()
        self.clock.advance(3600)
        blank = self.record()                                          # nothing typed this time
        self.assertEqual(self.m.store.load(blank.id).title, "")
        self.m.stop()
        settle()
        self.assertEqual([row.title_label.text() for row in self.m.stage.rows()],
                         ["Meeting, Sun 4 Oct 2:52 PM", "Zq7 Weekly sync"])
        self.assertEqual(self.open_card().title_edit.text(), "")       # a new card: an empty box again

    def test_in_person_or_on_a_call_starts_on_the_kind_recorded_last(self) -> None:
        self.make()
        card = self.open_card()
        self.assertEqual(card.kind(), mt.KIND_CALL)                     # "On a call" the first time
        self.assertTrue(card.kind_choice.call_button.isChecked())
        card.kind_choice.in_person_button.click()
        self.assertEqual(card.kind(), mt.KIND_IN_PERSON)
        self.assertEqual(card.device_texts(), (f"Room - microphone: {MIC_A.name} (Windows default)",
                                               meetings_view.CALL_NOT_RECORDED))
        self.assertFalse(card.start_button.isEnabled())                 # the consent tick first, as always
        card.tick.click()
        self.assertTrue(card.start_button.isEnabled())
        card.start_button.click()
        settle()
        record = self.m.recorder.record
        self.assertEqual(self.m.store.load(record.id).kind, mt.KIND_IN_PERSON)
        self.m.stop()
        settle()
        card = self.open_card()
        self.assertEqual(card.kind(), mt.KIND_IN_PERSON)                # the kind recorded last ...
        self.assertFalse(card.tick.isChecked())                         # ... never the consent tick
        self.assertFalse(card.start_button.isEnabled())
        saved = json.loads((self.m.store.root / mt.DEVICES_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved["kind"], mt.KIND_IN_PERSON)
        card.kind_choice.call_button.click()                            # a choice alone remembers nothing
        self.assertEqual(card.device_texts()[1], f"Others - call audio: {OUT_A.name} (Windows default)")
        card.cancel_button.click()
        self.assertEqual(self.open_card().kind(), mt.KIND_IN_PERSON)
        self.assertEqual(mt.DevicePrefs(self.m.store.root).card_kind(), mt.KIND_IN_PERSON)   # after a restart too

    def test_linking_fills_the_title_only_when_none_was_typed(self) -> None:
        self.make()
        info = self.calendar_meeting()
        self.m.meetings_now = lambda: [info]
        card = self.open_card()
        self.assertIsNone(card.linked())                                 # not linked by default
        card.title_edit.setText("Zq7 My own name")
        self.assertTrue(card.link_meeting(0))
        self.assertEqual(card.title_edit.text(), "Zq7 My own name")      # typed: kept
        self.assertEqual(card.title_label.text(), TITLE)
        self.assertIn("with Ana Lopez and Ben Okafor", card.meta_label.text())
        self.assertEqual(card.summary_tick.text(), meetings_view.SUMMARY_TICK_TEXT)    # the guests' names go
        card.unlink()
        self.assertEqual(card.title_edit.text(), "Zq7 My own name")      # still his
        self.assertEqual(card.summary_tick.text(), meetings_view.SUMMARY_TICK_PLAIN)   # no guests: no names
        self.assertTrue(card.link_meeting(0))
        card.tick.click()
        card.start_button.click()
        settle()
        record = self.m.store.load(self.m.recorder.record.id)
        self.assertEqual((record.title, record.event_id, record.guests, record.linked, record.alias),
                         ("Zq7 My own name", "evt-9", GUESTS, True, "work"))
        self.assertEqual((record.scheduled_end, record.invite), (info.end, INVITE))
        self.m.stop()
        settle()

    def test_unlink_keeps_nothing_of_the_calendar(self) -> None:
        self.make()
        info = self.calendar_meeting()
        card = self.open_card(info)                                      # a calendar shortcut: opened linked
        self.assertEqual(card.linked(), info)
        self.assertEqual(card.title_edit.text(), TITLE)                  # the box was empty: filled in
        self.assertTrue(card.unlink_button.isVisibleTo(card))
        card.tick.click()
        card.unlink()
        self.assertFalse(card.tick.isChecked())                          # a tick is for one meeting only
        self.assertEqual((card.linked(), card.title_edit.text()), (None, ""))
        card.tick.click()
        card.start_button.click()
        settle()
        record = self.m.store.load(self.m.recorder.record.id)
        self.assertEqual((record.title, record.event_id, record.guests, record.invite, record.alias, record.linked,
                          record.scheduled_end), ("", "", (), (), "", False, None))
        self.m.stop()
        settle()

    def test_the_calendar_shortcuts_open_the_card_linked(self) -> None:
        self.make()
        later = CalendarEvent(TITLE, NOW + timedelta(minutes=40), NOW + timedelta(minutes=70))
        self.c._agenda_status = ui._AGENDA_OK
        self.c._agenda_events = [later]
        self.c._agenda_range = (NOW - timedelta(days=1), NOW + timedelta(days=2))
        self.c._render_agenda()
        self.reading.agenda_panel.event_rows()[0].record_button().click()      # a Day view row's Record
        settle()
        card = self.m.stage.card
        self.assertTrue(card.isVisible())
        self.assertEqual((card.linked().start, card.linked().end, card.title_edit.text()),
                         (later.start, later.end, TITLE))
        self.assertFalse(card.tick.isChecked())
        self.assertFalse(card.start_button.isEnabled())
        self.assertFalse(self.m.is_recording())                                 # it only opened the card
        card.unlink()
        self.assertIsNone(card.linked())
        card.close_card()
        self.c._next_meeting_event = later                                     # Home's NEXT MEETING tile's Record
        self.c._record_next_meeting()
        settle()
        self.assertEqual(self.m.stage.card.linked().start, later.start)
        self.assertFalse(self.m.is_recording())

    def test_record_a_meeting_offers_the_rest_of_today_to_link(self) -> None:
        self.make()
        on_now = CalendarEvent("Zq7 Standup", NOW - timedelta(minutes=10), NOW + timedelta(minutes=5))
        later = CalendarEvent("Zq7 Review", NOW + timedelta(hours=2), NOW + timedelta(hours=3))
        over = CalendarEvent("Zq7 Breakfast", NOW - timedelta(hours=4), NOW - timedelta(hours=3))
        tomorrow = CalendarEvent("Zq7 Tomorrow", NOW + timedelta(days=1), NOW + timedelta(days=1, hours=1))
        self.c._agenda_events = [later, over, on_now, tomorrow]
        card = self.open_card()
        self.assertEqual([info.title for _text, info in card.link_choices()], ["Zq7 Standup", "Zq7 Review"])
        self.assertIsNone(card.linked())


class InPersonAppTests(MeetingsApp):
    def test_in_person_records_only_the_microphone_and_never_says_no_call_audio(self) -> None:
        self.make(model=True)
        self.backend.loop_fails = True              # the call audio could not be opened: in person it is never tried
        card = self.open_card()
        card.kind_choice.in_person_button.click()
        card.tick.click()
        card.start_button.click()
        settle()
        record = self.m.recorder.record
        self.tick(3)
        self.assertEqual([kind for kind, _device in self.backend.opened], [mt.MIC])
        self.assertTrue(self.reading.rec_badge.isVisible())               # REC, the pill and the tray as always
        self.assertEqual((self.reading.rec_badge.warning(), self.m.pill.warning()), ("", ""))
        self.assertEqual(self.c._tray_tooltip(), "Jarvis: Recording 0:03")
        self.assertNotEqual(self.m.stage.row(record.id).note_label.text(), meetings_view.CALL_MISSING_ROW)
        self.m.stop()
        settle()
        self.assertTrue(self.m.store.audio_path(record.id, mt.MIC).is_file())
        self.assertFalse(self.m.store.audio_path(record.id, mt.LOOPBACK).exists())
        saved = self.m.store.load(record.id)
        self.assertEqual((saved.kind, saved.device_changes), (mt.KIND_IN_PERSON, []))
        self.assertNotIn("call audio", self.m.stage.row(record.id).info.note)
        FakeJob.jobs[-1].run(FakeTranscriber({"mic.wav": [sp.Segment(0.0, 1.0, "Shall we start?"),
                                                          sp.Segment(5.0, 6.0, SPOKEN)]}))
        settle()
        self.assertEqual(self.m.stage.row(record.id).info.status_text, "Transcript ready")
        transcript = json.loads(self.m.store.transcript_path(record.id).read_text(encoding="utf-8"))
        self.assertEqual([turn["speaker"] for turn in transcript["turns"]], ["Room", "Room"])

    def test_logs_never_carry_a_typed_title(self) -> None:
        with self.assertLogs("briefing_reader", logging.DEBUG) as logs:
            self.make()
            card = self.open_card()
            card.title_edit.setText("Zq7 Typed Secret Title")
            card.kind_choice.in_person_button.click()
            card.tick.click()
            card.start_button.click()
            settle()
            self.tick(2)
            self.m.stop()
            settle()
        text = "\n".join(logs.output)
        for secret in ("Zq7", "Typed Secret", "Owner"):
            self.assertNotIn(secret, text)
        self.assertIn("in_person", text)                                 # kinds may be logged


if __name__ == "__main__":
    unittest.main()
