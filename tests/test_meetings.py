"""Meeting recording, part 1 (no Qt): the [meetings] switch, consent, storage, retention, the WAV files and the
recorder's stop rules and devices - with fake audio devices only (nothing real is recorded)."""

from __future__ import annotations

import json
import logging
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from briefing_reader import config as config_mod
from briefing_reader import meeting_audio as ma
from briefing_reader import meeting_speech as sp
from briefing_reader import meetings as mt
from briefing_reader.config import MeetingsConfig, load_config

from .meeting_fakes import (MIC_A, MIC_B, OUT_A, OUT_B, Clock, FakeBackend, FakePortAudio, FakeSession,
                            FakeTranscriber, tone)

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

    def test_small_files_that_redirect_and_come_gzipped_still_download_and_are_checked(self) -> None:
        session = FakeSession(self.FILES, hf_small=True)
        self.assertEqual(sp.ModelDownloader(self.target(), session=session).run(), "")
        self.assertTrue(sp.model_ready(self.target()))
        for name, body in self.FILES.items():
            self.assertEqual((self.target() / name).read_bytes(), body)
        again = FakeSession(self.FILES, hf_small=True)                 # already there: checked, not fetched
        self.assertEqual(sp.ModelDownloader(self.target(), session=again).run(), "")
        self.assertFalse([r for r in again.requests if r[0] == "GET"])

    def test_a_small_file_with_the_wrong_git_hash_is_not_kept(self) -> None:
        session = FakeSession(self.FILES, hf_small=True, bad_small=True)
        self.assertEqual(sp.ModelDownloader(self.target(), session=session).run(), sp.ERR_CHECK)
        self.assertFalse((self.target() / "config.json").exists())
        self.assertFalse(sp.model_ready(self.target()))

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

    def test_the_model_gets_the_recording_as_samples_not_through_the_add_ons_decoder(self) -> None:
        """faster-whisper 1.2's own decoder calls av.open(..., metadata_errors=...), which PyAV 15+ refuses: every
        meeting failed. The recording is read here and handed over as 16 kHz float32 samples."""
        np = self.numpy()
        folder = self.meeting()
        seen: list[Any] = []

        class Model:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def transcribe(self, audio: Any, **_kwargs: Any) -> tuple[list, None]:
                seen.append(audio)
                return [], None

        with mock.patch.object(sp.importlib, "import_module", return_value=mock.Mock(WhisperModel=Model)):
            transcriber = sp.FasterWhisperTranscriber(self.tmp)
            sp.transcribe_meeting(folder, transcriber)
        self.assertEqual(len(seen), 2)
        for audio in seen:
            self.assertIsInstance(audio, np.ndarray)
            self.assertEqual((audio.dtype, audio.ndim, len(audio)), (np.float32, 1, 10 * mt.SAMPLE_RATE))
            self.assertLessEqual(float(np.abs(audio).max()), 1.0)
        broken = folder / "broken.wav"
        broken.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")             # unreadable here: the add-on's decoder tries
        with mock.patch.object(sp.importlib, "import_module", return_value=mock.Mock(WhisperModel=Model)):
            list(sp.FasterWhisperTranscriber(self.tmp).transcribe(broken))
        self.assertEqual(seen[-1], str(broken))

    def test_read_wav_mixes_channels_and_resamples_to_16_khz(self) -> None:
        np = self.numpy()
        path = self.tmp / "stereo48k.wav"
        frames = 48_000
        left = (np.sin(np.arange(frames) * 2 * np.pi * 440 / 48_000) * 16_000).astype("<i2")
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(48_000)
            wav.writeframes(np.column_stack([left, np.zeros(frames, "<i2")]).astype("<i2").tobytes())
        samples = sp.read_wav(path)
        self.assertEqual((samples.dtype, len(samples)), (np.float32, mt.SAMPLE_RATE))
        self.assertAlmostEqual(float(np.abs(samples).max()), 16_000 / 32_768 / 2, places=2)   # averaged with silence
        eight_bit = self.tmp / "u8.wav"
        with wave.open(str(eight_bit), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(1)
            wav.setframerate(16_000)
            wav.writeframes(bytes(100))
        with self.assertRaises(ValueError):
            sp.read_wav(eight_bit)

    def test_a_failed_worker_names_its_kind_in_the_log_and_nothing_else(self) -> None:
        class Lines(list):
            def close(self) -> None:
                pass

        proc = mock.Mock()
        proc.wait.return_value = 1
        result: list[str] = []
        process = sp.TranscriptionProcess(self.tmp, self.tmp, on_progress=lambda _p: None, on_done=result.append)
        process._proc = proc
        for line, logged in ((b'{"error": "worker_failed", "kind": "TypeError"}\n',
                              "(worker_failed, TypeError, exit code 1)"),
                             (b'{"error": "worker_failed", "kind": "a message about C:\\\\Users"}\n',
                              "(worker_failed, exit code 1)")):     # only a type name is ever logged
            proc.stdout = Lines([b'{"progress": 3}\n', line])
            with self.assertLogs(sp.logger, "INFO") as logs:
                process._read()
            self.assertEqual(logs.output, [f"INFO:{sp.logger.name}:Meetings: the writing down failed {logged}"])
        self.assertEqual(result, [sp.ERR_WORKER, sp.ERR_WORKER])

    def numpy(self) -> Any:
        try:
            import numpy
        except ImportError:   # the add-on (and numpy with it) is not installed here
            self.skipTest("numpy is not installed")
        return numpy

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


class SummaryAskedTests(Base):
    def test_the_summary_tick_is_kept_with_its_time_and_off_by_default(self) -> None:
        plain = self.store.create(request())
        self.assertFalse(plain.summary_asked)
        self.assertEqual(json.loads((self.store.folder(plain.id) / mt.RECORD_FILE).read_text(encoding="utf-8"))
                         ["summary_request"], {"asked": False, "at": None})
        asked_at = NOW + timedelta(seconds=40)
        info = mt.MeetingInfo(TITLE, NOW, NOW + timedelta(minutes=30))
        asked = self.store.create(mt.StartRequest(info, True, NOW, summarise=True, summarise_at=asked_at))
        loaded = self.store.load(asked.id)
        self.assertEqual((loaded.summary_asked, loaded.summary_asked_at), (True, asked_at))
        self.assertEqual((loaded.consent_at, loaded.consent_confirmed), (NOW, True))
        self.assertFalse(mt.StartRequest(info, True, NOW, summarise="yes").summarise)   # only True counts


class DeleteLeftoverTests(Base):
    def make(self) -> str:
        record = self.store.create(request())
        for channel in mt.CHANNELS:
            self.store.audio_path(record.id, channel).write_bytes(b"RIFF")
        self.store.transcript_path(record.id).write_text("{}", encoding="utf-8")
        self.store.update(record.id, status=mt.STATUS_TRANSCRIBING, progress=50)
        return record.id

    def test_a_file_still_held_keeps_the_meeting_listed_to_delete_again(self) -> None:
        meeting_id = self.make()
        real_unlink = Path.unlink

        def held(path: Path, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202 - Path.unlink's own
            if path.name == "loopback.wav":
                raise PermissionError("in use by the worker")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", held):
            self.assertFalse(self.store.delete(meeting_id))
        record = self.store.load(meeting_id)                         # still listed: never a vanished row
        self.assertEqual(record.status, mt.STATUS_DELETING)
        self.assertEqual(record.status_text(), mt.DELETE_LEFT_TEXT)
        self.assertEqual([r.id for r in self.store.list()], [meeting_id])
        self.assertFalse(self.store.audio_path(meeting_id, mt.MIC).exists())
        self.assertFalse(self.store.transcript_path(meeting_id).exists())
        self.assertTrue(self.store.audio_path(meeting_id, mt.LOOPBACK).exists())
        self.assertIsNone(self.store.update(meeting_id, status=mt.STATUS_TRANSCRIBED))   # nothing revives it
        self.assertEqual(self.store.load(meeting_id).status, mt.STATUS_DELETING)
        self.store.recover()                                           # the next start tries again
        self.assertFalse(self.store.folder(meeting_id).exists())

    def test_delete_after_a_stopped_worker_removes_everything(self) -> None:
        """A real child process holds loopback.wav open the way the worker does; cancel() kills it and waits, so
        the Delete right after removes every file (none left on disk behind a vanished row)."""
        meeting_id = self.make()
        held = self.store.audio_path(meeting_id, mt.LOOPBACK)
        code = ("import sys, time\nfh = open(sys.argv[1], 'rb')\nprint('ready', flush=True)\ntime.sleep(60)\n")
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(held)], stdout=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual(child.stdout.readline().strip(), b"ready")
        process = sp.TranscriptionProcess(self.store.folder(meeting_id), self.tmp / "models",
                                          on_progress=lambda _p: None, on_done=lambda _e: None)
        process._proc = child
        self.assertTrue(process.cancel())
        self.assertIsNotNone(child.poll())                              # it has ended: its handle is gone
        child.stdout.close()
        self.assertTrue(self.store.delete(meeting_id))
        self.assertFalse(self.store.folder(meeting_id).exists())

    def test_cancel_kills_and_waits_and_says_when_it_could_not(self) -> None:
        proc = mock.Mock()
        proc.poll.return_value = None
        process = sp.TranscriptionProcess(self.tmp, self.tmp, on_progress=lambda _p: None, on_done=lambda _e: None)
        process._proc = proc
        self.assertTrue(process.cancel())
        proc.kill.assert_called_once_with()
        proc.wait.assert_called_once_with(timeout=sp.CANCEL_WAIT_S)
        proc.wait.side_effect = subprocess.TimeoutExpired("worker", sp.CANCEL_WAIT_S)
        self.assertFalse(process.cancel())


class RetentionSweepTests(Base):
    def make(self, days_ago: float, status: str = mt.STATUS_TRANSCRIBED) -> str:
        self.clock.start = NOW - timedelta(days=days_ago)
        record = self.store.create(request())
        for channel in mt.CHANNELS:
            self.store.audio_path(record.id, channel).write_bytes(b"RIFF" + bytes(64))
        self.store.update(record.id, status=status)
        self.clock.start = NOW
        return record.id

    def test_cleanup_names_each_meeting_it_removed(self) -> None:
        week, month = self.make(8), self.make(31)
        removed: list[str] = []
        self.assertEqual(self.store.cleanup(NOW, on_removed=removed.append), (2, 1))
        self.assertEqual(removed, [month])
        self.assertTrue(self.store.folder(week).exists())

    def test_the_sweep_with_meetings_off_keeps_the_owners_retention(self) -> None:
        nothing = self.tmp / "local" / "briefing-reader" / "elsewhere"
        self.assertEqual(mt.sweep(nothing, NOW), (0, 0))
        self.assertFalse(nothing.exists())                             # nothing is made
        week, month = self.make(8), self.make(31)
        crashed = self.make(1, mt.STATUS_RECORDING)
        self.assertEqual(mt.sweep(self.root, NOW), (2, 1))
        self.assertFalse(self.store.audio_path(week, mt.MIC).exists())
        self.assertFalse(self.store.folder(month).exists())
        self.assertEqual(self.store.load(crashed).status, mt.STATUS_RECORDED)


class StartFailureTests(Base):
    def test_a_start_that_fails_after_opening_closes_every_stream_and_file(self) -> None:
        backend = FakeBackend()
        record = self.store.create(request())
        rec = ma.Recorder(backend, self.store, record, clock=self.clock.mono, wall=self.clock.wall)
        with mock.patch.object(self.store, "save", side_effect=PermissionError("meeting.json locked")):
            with self.assertRaises(PermissionError):
                rec.start()
        self.assertFalse(rec.is_recording())
        self.assertEqual(len(backend.streams), 2)
        self.assertTrue(all(stream.closed for stream in backend.streams))   # no device left open
        self.assertTrue(all(channel.writer._fh is None for channel in rec._channels.values()))
        self.assertTrue(self.store.delete(record.id))                         # no file held: it all goes
        self.assertFalse(self.store.folder(record.id).exists())


class CallAudioTests(Base):
    def test_call_audio_that_cannot_be_opened_is_said_and_noted(self) -> None:
        backend = FakeBackend(loop_fails=True)
        record = self.store.create(request())
        rec = ma.Recorder(backend, self.store, record, clock=self.clock.mono, wall=self.clock.wall)
        rec.start()
        self.addCleanup(rec.stop)
        self.assertTrue(rec.is_recording())
        self.assertTrue(rec.call_audio_missing())
        backend.loop_fails = False                                      # the device can be opened now
        for _ in range(3):
            self.clock.advance(1.0)
            backend.live(mt.MIC).feed(tone(1.0, 3000))
            rec.tick()
        self.assertFalse(rec.call_audio_missing())
        self.assertEqual([(c["channel"], c["change"]) for c in rec.record.device_changes],
                         [("call audio", "unavailable"), ("call audio", "opened")])


class PortAudioTests(Base):
    """PyAudioWPatch's adapter over a fake PortAudio (its device list is a snapshot taken when it starts)."""

    def portaudio(self) -> FakePortAudio:
        module = FakePortAudio()
        module.add(0, "Laptop Mic", mic=True)
        module.add(1, "Laptop Speakers")
        module.add(2, "Laptop Speakers [Loopback]", loopback=True)
        module.default_in, module.default_out = 0, 1
        return module

    def plug_headset(self, module: FakePortAudio) -> None:
        module.add(3, "Headset Mic", mic=True)
        module.add(4, "Headset Earphones")
        module.add(5, "Headset Earphones [Loopback]", loopback=True)
        module.default_in, module.default_out = 3, 4

    def test_a_fresh_start_sees_a_device_plugged_in_since(self) -> None:
        module = self.portaudio()
        backend = ma.PyAudioWPatchBackend(module)
        self.plug_headset(module)
        self.assertEqual(backend.default_output().name, "Laptop Speakers")   # PortAudio's snapshot
        backend.reset()
        self.assertTrue(module.instances[0].terminated)
        self.assertEqual((backend.default_input().name, backend.default_output().name),
                         ("Headset Mic", "Headset Earphones"))
        self.assertIn("Headset Earphones", [device.name for device in backend.outputs()])

    def recorder(self, module: FakePortAudio) -> ma.Recorder:
        backend = ma.PyAudioWPatchBackend(module, clock=self.clock.mono)
        record = self.store.create(request())
        rec = ma.Recorder(backend, self.store, record, clock=self.clock.mono, wall=self.clock.wall)
        rec.start()
        self.addCleanup(rec.stop)
        return rec

    def run_for(self, rec: ma.Recorder, module: FakePortAudio, seconds: int, *, feed: bool = True) -> None:
        for _ in range(seconds):
            self.clock.advance(1.0)
            if feed:
                module.deliver(tone(1.0, 3000))
            rec.tick()

    def test_an_unplugged_headset_switches_both_channels_to_the_new_defaults(self) -> None:
        module = self.portaudio()
        self.plug_headset(module)
        rec = self.recorder(module)
        self.assertEqual(rec.device_names(), ("Headset Mic", "Headset Earphones"))
        self.run_for(rec, module, 5)
        module.unplug(3, 4, 5)                                          # Windows moves to the laptop
        module.default_in, module.default_out = 0, 1
        self.run_for(rec, module, 5)
        self.assertTrue(rec.is_recording())
        self.assertEqual(rec.device_names(), ("Laptop Mic", "Laptop Speakers"))
        self.assertEqual(sorted((c["channel"], c["change"]) for c in rec.record.device_changes),
                         [("call audio", "switched"), ("microphone", "switched")])
        self.assertTrue(module.instances[0].terminated)                 # PortAudio started again for a fresh look
        self.assertFalse(rec.call_audio_missing())
        loud_before = rec._last_sound
        self.run_for(rec, module, 3)
        self.assertGreater(rec._last_sound, loud_before)                # the laptop microphone is heard now

    def test_a_microphone_that_goes_quiet_is_opened_again(self) -> None:
        module = self.portaudio()
        rec = self.recorder(module)
        self.run_for(rec, module, 2)
        self.run_for(rec, module, int(ma.MIC_STALE_S) + 2, feed=False)  # no callback at all: the stream stopped
        self.assertTrue(rec.is_recording())
        self.assertIn(("microphone", "reopened"), [(c["channel"], c["change"]) for c in rec.record.device_changes])
        self.assertEqual(len(module.instances), 2)
        self.assertEqual(rec.device_names(), ("Laptop Mic", "Laptop Speakers"))


class DownloadEdgeTests(Base):
    FILES = DownloadTests.FILES

    def target(self) -> Path:
        return self.tmp / "local" / "briefing-reader" / "models" / sp.MODEL_FOLDER

    def test_stopping_during_the_check_keeps_the_whole_file(self) -> None:
        cancel = threading.Event()
        real = sp._sha256

        def checking(path: Path, flag) -> str:  # noqa: ANN001 - the cancel Event
            cancel.set()                                                # Stop the download (or Jarvis closes)
            return real(path, flag)

        with mock.patch.object(sp, "_sha256", checking):
            code = sp.ModelDownloader(self.target(), session=FakeSession(self.FILES), cancel=cancel).run()
        self.assertEqual(code, sp.ERR_CANCELLED)
        part = self.target() / "model.bin.part"
        self.assertEqual(part.stat().st_size, len(self.FILES["model.bin"]))   # kept: "continues where it stopped"
        again = FakeSession(self.FILES)
        self.assertEqual(sp.ModelDownloader(self.target(), session=again).run(), "")
        self.assertEqual([r for r in again.requests if r[:2] == ("GET", "model.bin")], [])   # not fetched again
        self.assertTrue(sp.model_ready(self.target()))

    def test_a_file_error_is_a_code_never_an_exception(self) -> None:
        real_replace = Path.replace

        def locked(path: Path, target):  # noqa: ANN001, ANN202 - Path.replace's own
            if path.name == "model.bin.part":
                raise PermissionError("a scanner holds the new file")
            return real_replace(path, target)

        with mock.patch.object(Path, "replace", locked):
            code = sp.ModelDownloader(self.target(), session=FakeSession(self.FILES)).run()
        self.assertEqual(code, sp.ERR_DISK)


if __name__ == "__main__":
    unittest.main()
