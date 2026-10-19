"""Meeting recording, part 1 (no Qt): the [meetings] switch, consent, storage, retention, the WAV files and the
recorder's stop rules and devices - with fake audio devices only (nothing real is recorded)."""

from __future__ import annotations

import json
import logging
import struct
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from briefing_reader import config as config_mod
from briefing_reader import meeting_audio as ma
from briefing_reader import meeting_speech as sp
from briefing_reader import meetings as mt
from briefing_reader.config import MeetingsConfig, load_config

from .meeting_fakes import MIC_A, MIC_B, OUT_A, OUT_B, Clock, FakeBackend, FakeSession, FakeTranscriber, tone

NOW = datetime(2026, 10, 19, 14, 0)
TITLE = "Secret Project Sync"
GUESTS = ("Ana Lopez", "Ben Okafor")


def request(*, end: datetime | None = None, consent: bool = True) -> mt.StartRequest:
    info = mt.MeetingInfo(TITLE, NOW, end, "evt123", GUESTS) if end else mt.MeetingInfo()
    return mt.StartRequest(info, consent, NOW, "Owner Laptop Microphone", "Owner Laptop Speakers")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "local" / "briefing-reader" / "meetings"
        self.clock = Clock(NOW)
        self.store = mt.MeetingStore(self.root, now=self.clock.wall)


class ConfigTests(Base):
    def write(self, text: str) -> Path:
        (self.tmp / "config.toml").write_text(text, encoding="utf-8")
        return self.tmp

    def test_off_by_default_and_switched_on_per_pc(self) -> None:
        root = self.write("")
        env = {"LOCALAPPDATA": str(self.tmp / "local")}
        self.assertEqual(load_config(root, environ=env).meetings, MeetingsConfig(enabled=False))
        self.assertTrue(load_config(root, environ={**env, "JARVIS_MEETINGS_ENABLED": "true"}).meetings.enabled)
        root = self.write("[meetings]\nenabled = true\n")
        self.assertTrue(load_config(root, environ=env).meetings.enabled)
        self.assertFalse(load_config(root, environ={**env, "JARVIS_MEETINGS_ENABLED": "off"}).meetings.enabled)
        with self.assertLogs(config_mod.logger, "WARNING"):
            load_config(self.write("[meetings]\nenabled = 3\n"), environ=env)

    def test_shipped_config_is_off(self) -> None:
        cfg = load_config(config_mod.PROJECT_ROOT, environ={"LOCALAPPDATA": str(self.tmp / "local")})
        self.assertFalse(cfg.meetings.enabled)
        text = (config_mod.PROJECT_ROOT / "config.toml").read_text(encoding="ascii").split("[meetings]", 1)[1]
        self.assertIn("JARVIS_MEETINGS_ENABLED=true", text)
        self.assertIn("not legal advice", text)

    def test_storage_lives_under_local_app_data(self) -> None:
        cfg = load_config(self.write(""), environ={"LOCALAPPDATA": str(self.tmp / "local")})
        self.assertEqual(mt.meetings_root(cfg), self.tmp / "local" / "briefing-reader" / "meetings")
        self.assertEqual(mt.models_root(cfg), self.tmp / "local" / "briefing-reader" / "models")
        self.assertEqual(mt.unsafe_root(mt.meetings_root(cfg)), "")
        for synced in (Path("C:/Users/x/OneDrive/briefing-reader/meetings"),
                       Path("C:/Users/x/OneDrive - Contoso/m"), Path("C:/Users/x/Downloads/meetings"),
                       Path("C:/Users/x/Dropbox/meetings")):
            self.assertTrue(mt.unsafe_root(synced), str(synced))
        store = mt.MeetingStore(Path("C:/Users/x/OneDrive/meetings"))
        with self.assertRaises(OSError):
            store.create(request())


class ConsentAndRecordTests(Base):
    def test_no_request_without_the_tick(self) -> None:
        with self.assertRaises(mt.ConsentMissing):
            request(consent=False)
        with self.assertRaises(mt.ConsentMissing):
            mt.StartRequest(mt.MeetingInfo(), "yes", NOW)          # only True counts
        with self.assertRaises(mt.ConsentMissing):
            self.store.create(object())                            # type: ignore[arg-type]
        self.assertEqual(self.store.list(), [])

    def test_record_keeps_the_consent_and_its_time(self) -> None:
        record = self.store.create(request(end=NOW + timedelta(minutes=30)))
        folder = self.store.folder(record.id)
        self.assertEqual(folder.parent, self.root)
        data = json.loads((folder / mt.RECORD_FILE).read_text(encoding="utf-8"))
        self.assertEqual(data["consent"], {"confirmed": True, "at": NOW.isoformat(timespec="seconds")})
        self.assertEqual(data["status"], mt.STATUS_RECORDING)
        self.assertEqual(data["audio"], {"mic": "mic.wav", "loopback": "loopback.wav"})
        loaded = self.store.load(record.id)
        self.assertEqual((loaded.title, loaded.guests, loaded.consent_confirmed), (TITLE, GUESTS, True))
        self.assertNotIn(TITLE, repr(loaded))
        self.assertNotIn("Ana", repr(mt.MeetingInfo(TITLE, guests=GUESTS)))
        self.assertEqual(self.store.list()[0].id, record.id)
        with self.assertRaises(ValueError):
            self.store.folder("../escape")

    def test_status_words(self) -> None:
        record = mt.MeetingRecord(id="20261019-140000-abcd")
        words = []
        for status, progress in ((mt.STATUS_RECORDING, 0), (mt.STATUS_RECORDED, 0), (mt.STATUS_TRANSCRIBING, 40),
                                 (mt.STATUS_TRANSCRIBED, 100), (mt.STATUS_SUMMARY_READY, 100),
                                 (mt.STATUS_FAILED, 0)):
            record.status, record.progress = status, progress
            words.append(record.status_text())
        self.assertEqual(words, ["Recording", "Waiting to be written down", "Writing it down 40%",
                                 "Transcript ready", "Summary ready", "Failed"])
        self.assertEqual((mt.length_text(724), mt.length_text(3723)), ("12:04", "1:02:03"))


class RetentionTests(Base):
    def make(self, days_ago: float) -> str:
        self.clock.start = NOW - timedelta(days=days_ago)
        record = self.store.create(request())
        for channel in mt.CHANNELS:
            self.store.audio_path(record.id, channel).write_bytes(b"RIFF")
        self.store.transcript_path(record.id).write_text("{}", encoding="utf-8")
        record.status = mt.STATUS_TRANSCRIBED
        self.store.save(record)
        return record.id

    def test_audio_after_7_days_text_after_30(self) -> None:
        fresh, week, month = self.make(6.9), self.make(7.1), self.make(30.5)
        busy = self.make(40)
        self.assertEqual(self.store.cleanup(NOW, active=[busy]), (2, 1))
        self.assertTrue(self.store.audio_path(fresh, mt.MIC).exists())
        self.assertFalse(self.store.audio_path(week, mt.MIC).exists())
        self.assertFalse(self.store.audio_path(week, mt.LOOPBACK).exists())
        self.assertTrue(self.store.transcript_path(week).exists())          # the text stays 30 days
        self.assertIsNotNone(self.store.load(week).audio_deleted_at)
        self.assertFalse(self.store.folder(month).exists())
        self.assertTrue(self.store.folder(busy).exists())                   # recording now: never touched
        self.assertEqual(self.store.cleanup(NOW, active=[busy]), (0, 0))

    def test_delete_removes_audio_text_and_summary_at_once(self) -> None:
        meeting_id = self.make(1)
        (self.store.folder(meeting_id) / "summary.json").write_text("{}", encoding="utf-8")
        self.assertTrue(self.store.delete(meeting_id))
        self.assertFalse(self.store.folder(meeting_id).exists())
        self.assertFalse(self.store.delete(meeting_id))
        self.assertFalse(self.store.delete("not-an-id"))


class WavTests(Base):
    def test_header_kept_current_and_repaired(self) -> None:
        self.root.mkdir(parents=True)
        path = self.root / "a.wav"
        writer = ma.WavWriter(path)
        writer.write(tone(0.5))
        writer.sync()
        self.assertAlmostEqual(ma.wav_seconds(path), 0.5, places=3)
        writer.write(tone(0.25))
        writer._fh.flush()                                   # a crash: the header still says 0.5 s
        head = path.read_bytes()[:44]
        self.assertEqual(struct.unpack("<I", head[40:44])[0], 16000)
        self.assertTrue(ma.repair_wav(path))
        self.assertEqual(struct.unpack("<I", path.read_bytes()[40:44])[0], 24000)
        writer.close()
        self.assertFalse(ma.repair_wav(path))
        import wave
        with wave.open(str(path)) as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()),
                             (16000, 1, 2, 12000))

    def test_recover_after_a_crash(self) -> None:
        record = self.store.create(request())
        for channel in mt.CHANNELS:
            writer = ma.WavWriter(self.store.audio_path(record.id, channel))
            writer.write(tone(2.0))
            writer._fh.close()                               # a crash: the header never synced
        self.assertEqual(self.store.recover(active=[record.id]), [])
        self.assertEqual(self.store.recover(), [record.id])
        loaded = self.store.load(record.id)
        self.assertEqual((loaded.status, loaded.stop_reason), (mt.STATUS_RECORDED, mt.STOP_INTERRUPTED))
        self.assertAlmostEqual(loaded.duration_s, 2.0, places=2)

    def test_conversion_to_16k_mono(self) -> None:
        stereo48 = struct.pack("<" + "h" * 12, *([1000, 3000] * 6))
        self.assertEqual(ma.to_mono16k(stereo48, 48000, 2), struct.pack("<hh", 2000, 2000))
        self.assertEqual(ma.peak(tone(0.01, 1234)), 1234)
        self.assertEqual(ma.peak(b""), 0)


class RecorderTests(Base):
    def recorder(self, backend: FakeBackend | None = None, *, end: datetime | None = None, mic: str = "",
                 call: str = "") -> ma.Recorder:
        self.backend = backend or FakeBackend()
        self.stops: list[str] = []
        self.record = self.store.create(request(end=end))
        rec = ma.Recorder(self.backend, self.store, self.record, mic_pick=mic, call_pick=call, clock=self.clock.mono,
                          wall=self.clock.wall, on_stop=self.stops.append)
        rec.start()
        self.addCleanup(rec.stop)
        return rec

    def run_for(self, rec: ma.Recorder, seconds: float, *, mic: int = 0, loop: int = 0, step: float = 1.0) -> None:
        elapsed = 0.0
        while elapsed < seconds and rec.is_recording():
            self.clock.advance(step)
            elapsed += step
            if mic is not None:
                self.backend.live(mt.MIC).feed(tone(step, mic))
            if loop:
                self.backend.live(mt.LOOPBACK).feed(tone(step, loop))
            rec.tick()

    def test_two_aligned_files_and_stop_button(self) -> None:
        rec = self.recorder()
        self.run_for(rec, 10, mic=ma.SILENCE_PEAK * 2)     # the call audio delivers nothing: silence fills it
        self.assertTrue(rec.stop(mt.STOP_BUTTON))
        self.assertFalse(rec.stop(mt.STOP_BUTTON))
        self.assertEqual(self.stops, [mt.STOP_BUTTON])
        mic = ma.wav_seconds(self.store.audio_path(self.record.id, mt.MIC))
        loop = ma.wav_seconds(self.store.audio_path(self.record.id, mt.LOOPBACK))
        self.assertAlmostEqual(mic, 10.0, delta=0.05)
        self.assertAlmostEqual(loop, 10.0, delta=ma.LAG_FILL_S + 0.05)
        saved = self.store.load(self.record.id)
        self.assertEqual((saved.status, saved.stop_reason), (mt.STATUS_RECORDED, mt.STOP_BUTTON))
        self.assertAlmostEqual(saved.duration_s, 10.0, delta=0.2)
        self.assertTrue(all(stream.closed for stream in self.backend.streams))

    def test_the_owners_limits(self) -> None:
        self.assertEqual((ma.END_GRACE_S, ma.SILENCE_S, ma.MAX_S), (15 * 60, 10 * 60, 3 * 3600))
        self.assertEqual((mt.AUDIO_DAYS, mt.TEXT_DAYS, ma.SAMPLE_RATE), (7, 30, 16000))

    def test_end_time_plus_15_minutes(self) -> None:
        rec = self.recorder(end=NOW)                            # started as the meeting ended (a long one)
        self.run_for(rec, 14 * 60 + 50, mic=LOUD_MIC, step=10)
        self.assertTrue(rec.is_recording())                     # end + 14:50: still on
        self.run_for(rec, 20, mic=LOUD_MIC, step=10)
        self.assertEqual(self.stops, [mt.STOP_END_TIME])

    def test_silence_on_both_channels(self) -> None:
        with mock.patch.object(ma, "SILENCE_S", 120):
            rec = self.recorder()
            self.run_for(rec, 100, mic=0, step=10)
            self.run_for(rec, 10, mic=0, loop=LOUD_MIC, step=10)   # the others speak: the silence starts again
            self.run_for(rec, 110, mic=0, step=10)
            self.assertTrue(rec.is_recording())
            self.run_for(rec, 20, mic=0, step=10)
        self.assertEqual(self.stops, [mt.STOP_SILENCE])

    def test_the_cap(self) -> None:
        with mock.patch.object(ma, "MAX_S", 300):
            rec = self.recorder()
            self.run_for(rec, 290, mic=LOUD_MIC, step=10)
            self.assertTrue(rec.is_recording())
            self.run_for(rec, 20, mic=LOUD_MIC, step=10)
        self.assertEqual(self.stops, [mt.STOP_CAP])
        self.assertEqual(self.store.load(self.record.id).stop_reason, mt.STOP_CAP)

    def test_follows_the_default_output_and_notes_the_switch(self) -> None:
        rec = self.recorder()
        self.assertEqual(rec.device_names(), (MIC_A.name, OUT_A.name))
        self.backend.default_out = OUT_B                       # a headset plugged in / the old one unplugged
        self.run_for(rec, 3, mic=LOUD_MIC)
        self.assertEqual(self.backend.live(mt.LOOPBACK).device, OUT_B)
        self.assertEqual(rec.device_names(), (MIC_A.name, OUT_B.name))
        rec.stop()
        saved = self.store.load(self.record.id)
        self.assertEqual([(c["channel"], c["change"], c["to"]) for c in saved.device_changes],
                         [("call audio", "switched", OUT_B.name)])
        self.assertEqual(saved.devices["call_audio"], OUT_B.name)
        self.assertTrue(saved.devices["call_audio_follows_default"])

    def test_a_stream_that_dies_switches_to_the_default(self) -> None:
        rec = self.recorder()
        self.backend.live(mt.LOOPBACK).fail()
        self.backend.default_out = OUT_B
        self.run_for(rec, 3, mic=LOUD_MIC)
        self.assertEqual(self.backend.live(mt.LOOPBACK).device, OUT_B)
        self.assertTrue(rec.is_recording())

    def test_picked_devices_win_and_a_missing_pick_follows_the_default(self) -> None:
        rec = self.recorder(mic=MIC_B.name, call="Gone Device")
        self.assertEqual(rec.device_names(), (MIC_B.name, OUT_A.name))
        self.backend.default_in = MIC_A
        self.run_for(rec, 3, mic=LOUD_MIC)
        self.assertEqual(self.backend.live(mt.MIC).device, MIC_B)    # a pick does not follow the default
        devices = self.store.load(self.record.id).devices
        self.assertFalse(devices["microphone_follows_default"])
        rec.stop()

    def test_no_microphone_no_recording(self) -> None:
        backend = FakeBackend(mic_fails=True)
        record = self.store.create(request())
        rec = ma.Recorder(backend, self.store, record, clock=self.clock.mono, wall=self.clock.wall)
        with self.assertRaises(ma.RecorderError) as caught:
            rec.start()
        self.assertEqual(caught.exception.code, "mic_unavailable")
        self.assertFalse(rec.is_recording())

    def test_logs_carry_no_titles_names_or_devices(self) -> None:
        with self.assertLogs("briefing_reader", logging.DEBUG) as logs:
            rec = self.recorder(end=NOW + timedelta(minutes=5))
            self.backend.default_out = OUT_B
            self.run_for(rec, 5, mic=LOUD_MIC)
            rec.stop(mt.STOP_CLOSING)
            self.store.cleanup(NOW + timedelta(days=40))
        text = "\n".join(logs.output)
        for secret in (TITLE, *GUESTS, MIC_A.name, OUT_A.name, OUT_B.name, "Owner"):
            self.assertNotIn(secret, text)
        self.assertIn(self.record.id, text)


LOUD_MIC = ma.SILENCE_PEAK * 3


class DownloadTests(Base):
    FILES = {"config.json": b"{}" * 10, "tokenizer.json": b"t" * 50, "vocabulary.txt": b"v" * 30,
             "model.bin": bytes(range(256)) * 40}

    def target(self) -> Path:
        return self.tmp / "local" / "briefing-reader" / "models" / sp.MODEL_FOLDER

    def test_nothing_until_run_then_ready_with_checks(self) -> None:
        session = FakeSession(self.FILES)
        seen: list[tuple[int, int]] = []
        downloader = sp.ModelDownloader(self.target(), session=session, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(session.requests, [])                     # made, nothing fetched
        self.assertFalse(sp.model_ready(self.target()))
        self.assertEqual(downloader.run(), "")
        self.assertTrue(sp.model_ready(self.target()))
        total = sum(len(body) for body in self.FILES.values())
        self.assertEqual(seen[-1], (total, total))
        self.assertEqual([d for d, _t in seen], sorted(d for d, _t in seen))
        for name, body in self.FILES.items():
            self.assertEqual((self.target() / name).read_bytes(), body)
        self.assertTrue(all(url.startswith("https://huggingface.co/Systran/faster-whisper-medium.en/resolve/")
                            for url in [sp.MODEL_URL.format(repo=sp.MODEL_REPO, revision="main", name="x")]))

    def test_resumes_where_it_stopped(self) -> None:
        session = FakeSession(self.FILES, cut_after=4000)
        self.assertEqual(sp.ModelDownloader(self.target(), session=session).run(), sp.ERR_NETWORK)
        self.assertEqual((self.target() / "model.bin.part").stat().st_size, 4000)
        self.assertFalse(sp.model_ready(self.target()))
        self.assertEqual(sp.ModelDownloader(self.target(), session=session).run(), "")
        self.assertIn(("GET", "model.bin", "bytes=4000-"), session.requests)
        self.assertTrue(sp.model_ready(self.target()))

    def test_a_file_that_fails_its_check_is_not_kept(self) -> None:
        session = FakeSession(self.FILES, bad_hash=True)
        self.assertEqual(sp.ModelDownloader(self.target(), session=session).run(), sp.ERR_CHECK)
        self.assertFalse((self.target() / "model.bin").exists())
        self.assertFalse(sp.model_ready(self.target()))

    def test_cancel(self) -> None:
        cancel = threading.Event()
        cancel.set()
        code = sp.ModelDownloader(self.target(), session=FakeSession(self.FILES), cancel=cancel).run()
        self.assertEqual(code, sp.ERR_CANCELLED)

    def test_missing_pieces_say_exactly_what(self) -> None:
        models = self.tmp / "models"
        pieces = sp.missing_pieces(models, audio_missing=lambda: "PyAudioWPatch (or soundcard)",
                                   speech=lambda: "faster-whisper")
        self.assertEqual([p.key for p in pieces], ["audio_addon", "speech_addon", "model"])
        self.assertIn("PyAudioWPatch (or soundcard)", pieces[0].text)
        self.assertIn("faster-whisper", pieces[1].text)
        self.assertIn("medium.en (about 1.5 GB)", pieces[2].text)
        self.assertIn("never installs", pieces[0].text)
        self.assertEqual(sp.missing_pieces(models, audio_missing=lambda: "", speech=lambda: "")[0].key, "model")
        self.assertIn("Systran/faster-whisper-medium.en", sp.DOWNLOAD_TEXT)


class TranscriptionTests(Base):
    def meeting(self) -> Path:
        record = self.store.create(request())
        for channel, seconds in ((mt.MIC, 10.0), (mt.LOOPBACK, 10.0)):
            writer = ma.WavWriter(self.store.audio_path(record.id, channel))
            writer.write(tone(seconds, 0))
            writer.close()
        return self.store.folder(record.id)

    def test_you_and_others_merged_by_time(self) -> None:
        folder = self.meeting()
        fake = FakeTranscriber({"mic.wav": [sp.Segment(0.0, 2.0, "Hello all."), sp.Segment(2.5, 4.0, "Shall we?"),
                                            sp.Segment(8.0, 9.0, "Agreed.")],
                                "loopback.wav": [sp.Segment(4.5, 7.5, "Yes, the budget is fine.")]})
        progress: list[int] = []
        transcript = sp.transcribe_meeting(folder, fake, progress.append)
        self.assertEqual([(t["speaker"], t["text"]) for t in transcript["turns"]],
                         [("You", "Hello all. Shall we?"), ("Others", "Yes, the budget is fine."), ("You", "Agreed.")])
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(progress[-1], 100)
        self.assertIn(45, range(progress[1], 101))
        saved = json.loads((folder / mt.TRANSCRIPT_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved["turns"], transcript["turns"])
        self.assertEqual(fake.calls, ["mic.wav", "loopback.wav"])

    def test_a_failing_model_raises(self) -> None:
        folder = self.meeting()
        fake = FakeTranscriber({"mic.wav": [sp.Segment(0, 1, "a"), sp.Segment(1, 2, "b")]}, fail=True)
        with self.assertRaises(RuntimeError):
            sp.transcribe_meeting(folder, fake)
        self.assertFalse((folder / mt.TRANSCRIPT_FILE).exists())

    def test_the_worker_process_reports_a_missing_add_on(self) -> None:
        """The real background process (no model, no add-on here): it says what is missing as a code."""
        folder = self.meeting()
        done = threading.Event()
        result: list[str] = []
        process = sp.TranscriptionProcess(folder, self.tmp / "models",
                                          on_progress=lambda _p: None,
                                          on_done=lambda error: (result.append(error), done.set()))
        process.start()
        self.assertTrue(done.wait(60))
        expected = sp.ERR_ADDON if sp.speech_missing() else sp.ERR_MODEL
        self.assertEqual(result, [expected])
        self.assertFalse(process.running())


if __name__ == "__main__":
    unittest.main()
