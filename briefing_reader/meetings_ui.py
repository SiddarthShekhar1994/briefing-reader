"""Meeting recording, part 1: the app's side ([meetings] enabled; the AppController makes one ``MeetingsController``).

It ties the parts together and keeps the owner's rules:

* A recording starts ONLY in ``start_recording(request)`` - the start card's Start, which exists only while the
  consent tick is ticked; the request must carry ``consent=True`` again here. Nothing else calls it: not the
  voice, not Ask, not an offer, not the Find list (its "Record a meeting" only opens the card).
* While it records: REC with the time and Stop in the header (every screen) and on Home, the orb's recording look,
  the Dock's coral dot on Meetings, the always-on-top pill (also while Jarvis is minimised or hidden) and the tray
  tooltip ("Jarvis: Recording 12:04"). Stop works from the pill, the header, Home and the Meetings screen; the
  recorder stops by itself at the end time + 15 min, after 10 min of silence on both channels or at 3 h, and
  ``shutdown()`` (Jarvis closing) stops and saves.
* After a stop the meeting is written down in a background process (low priority) when the faster-whisper add-on
  and the speech model are there; the model is downloaded only by ``download_model()`` - the owner's click on
  Download (Settings > Meetings or the Meetings screen). Retention runs at start-up and daily (a meeting it
  removes is announced like a Delete: ``meetingDeleted``, so its waiting drafts and notes go too).
* The start card closes - both ticks unticked - when the Meetings screen gives way to another, when the window is
  hidden or minimised, and its consent tick counts for CONSENT_FRESH_S only: a tick is never carried over to
  another call. The meeting record keeps when the box was ticked.
* The device library is made fresh for each start card, Settings > Meetings and each start (PortAudio lists the
  devices only when it starts); while recording, the call audio that can't be recorded shows on REC, the pill
  and the meeting's row ("No call audio").
* Logs: ids, counts, durations and statuses only.

Part 2, the summary: the transcript leaves this PC only when the owner asked for its summary - the start card's
own tick "summarise it" (unticked every time; the meeting keeps that he asked and when: the summary then starts
once it is written down) or his click on Summarise on the meeting. It runs through ``summary_runner`` - the
app's: the owner's own signed-in Claude Code as a planner mode run (ask.modes.MODE_MEETING), never an API key;
the transcript and the guests' names go to Anthropic that way, never the audio or an address. A run that can't
start now (Jarvis is busy with another request, a Google sign-in is open) waits in a queue and starts when the
Ask is free (``pump_summaries``, also every 30 s). ``on_summary(outcome)`` keeps a checked summary in the
meeting's summary.json (status Summary ready) or the failure's code (Retry). Opening a summary (``open_summary``)
emits ``summaryOpened(id)``: the app lends the draft cards and the Add block cards and calls ``render_summary``.
Nothing here sends, adds or keeps anything.

Tests replace ``backend_factory``, ``process_factory``, ``downloader_factory``, ``audio_missing`` and
``speech_missing`` and set ``threaded = False`` (the recorder is then ticked by the test, the download runs inline).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import shiboken6
from PySide6.QtCore import QObject, QTimer, Signal

from . import meeting_audio as ma
from . import meeting_speech as sp
from . import meeting_summary_view as sv
from . import meetings as mt
from . import meetings_view
from .ask import meeting_summary as ms

logger = logging.getLogger(__name__)

DAILY_MS = 24 * 3600 * 1000
TICK_MS = 1000
DELETE_RETRY_S = 0.3               # a Delete that left a file tries once more after this pause
SUMMARY_RETRY_MS = 30_000          # a summary waiting for the Ask is tried again this often
SUMMARY_STARTED, SUMMARY_BUSY, SUMMARY_OFF = "", "busy", "off"   # summary_runner's answers
SUMMARY_CANCELLED = "cancelled"                                   # MeetingRecord.summary_error after Esc
SETTINGS_KEY = "meetings"
SETTINGS_TITLE = "Meetings"
SETTINGS_NOTE = "Jarvis records a meeting only when you click Start, after the consent tick."
SETTINGS_HOW = ("Recording is turned on or off with [meetings] in config.toml (or JARVIS_MEETINGS_ENABLED in .env), "
                "then a restart. The devices and the speech model are chosen here.")
SUMMARY_ROW_ON = ("Only when you ask: the tick on the start card, or Summarise on the meeting. Your own Claude Code "
                  "(Ask Jarvis) writes it: the transcript and the guests' names go to Claude (Anthropic); never "
                  "the audio or an address")
_ERROR_WORDS = {sp.ERR_NETWORK: "the connection dropped", sp.ERR_CHECK: "a file did not pass its check",
                sp.ERR_DISK: "the file could not be saved", sp.ERR_CANCELLED: "you stopped it"}


class MeetingsController(QObject):
    """See the module docstring."""

    recordingChanged = Signal(bool)
    summaryOpened = Signal(str)          # part 2: a meeting's summary opened (the app lends its cards)
    summaryChanged = Signal(str)         # part 2: a summary was kept or failed (the app may re-render it)
    meetingDeleted = Signal(str)         # part 2: the owner's Delete or retention (the app drops undecided drafts)
    _stopped = Signal(str)
    _devices_changed = Signal()
    _progress = Signal(str, int)
    _finished = Signal(str, str)
    _dl_progress = Signal(int, int)
    _dl_done = Signal(str)

    def __init__(self, config: Any, reading: Any, *, now: Callable[[], datetime] = datetime.now,
                 monotonic: Callable[[], float] = time.monotonic, on_tray: Callable[[], Any] | None = None,
                 show_screen: Callable[[], Any] | None = None, on_screen: Callable[[], bool] | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.reading = reading
        self.stage: meetings_view.MeetingsStage = reading.meetings_stage
        self._now = now
        self._mono = monotonic
        self._on_tray = on_tray or (lambda: None)
        self._show_screen = show_screen or (lambda: None)
        self._on_screen = on_screen or (lambda: True)
        root = mt.meetings_root(config)
        self.store = mt.MeetingStore(root, now=now)
        self.prefs = mt.DevicePrefs(root)
        self.models = mt.models_root(config)
        # The meetings a start card may be for, best first (on now or starting within 15 minutes; the app's).
        self.meetings_now: Callable[[], list[mt.MeetingInfo]] = lambda: []
        # Replaceable (tests): the device library, the worker process, the download, the add-on checks.
        self.backend_factory: Callable[[], tuple[Any, str]] = ma.load_backend
        self.audio_missing: Callable[[], str] = ma.backend_missing
        self.speech_missing: Callable[[], str] = sp.speech_missing
        self.process_factory: Callable[..., Any] = (
            lambda folder, model, on_progress, on_done: sp.TranscriptionProcess(folder, model, on_progress=on_progress,
                                                                                on_done=on_done))
        self.downloader_factory: Callable[..., Any] = (
            lambda target, progress, cancel: sp.ModelDownloader(target, progress=progress, cancel=cancel))
        self.threaded = True
        # Part 2: the app's summary runner (job, origin) -> SUMMARY_STARTED | SUMMARY_BUSY | SUMMARY_OFF; None while
        # Ask Jarvis is off (no summaries; the list says so).
        self.summary_runner: Callable[[ms.MeetingJob, str], str] | None = None
        self._summary_queue: list[tuple[str, str]] = []      # (meeting id, origin) waiting for the Ask
        self._summary_running = ""
        self._summary_origin = ""                            # what started the running summary (tick or click)
        self._summary_tasks: dict[str, int] = {}             # meeting id -> its LIVE task (this session only)
        self._summary_retry = QTimer(self)
        self._summary_retry.setInterval(SUMMARY_RETRY_MS)
        self._summary_retry.timeout.connect(self.pump_summaries)
        self._backend: Any = None
        self.recorder: ma.Recorder | None = None
        self.pill: meetings_view.RecPill | None = None
        self._queue: list[str] = []
        self._job: tuple[str, Any] | None = None
        self._saved_progress: dict[str, int] = {}
        self._download: threading.Thread | None = None
        self._dl_cancel = threading.Event()
        self._dl_text = ""
        self._shut = False
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._sync_indicators)
        self._daily = QTimer(self)
        self._daily.setInterval(DAILY_MS)
        self._daily.timeout.connect(self.cleanup)
        self._stopped.connect(self._on_stopped)
        self._devices_changed.connect(self.refresh)
        self._progress.connect(self._on_progress)
        self._finished.connect(self._on_finished)
        self._dl_progress.connect(self._on_dl_progress)
        self._dl_done.connect(self._on_dl_done)
        stage = self.stage
        stage.card.set_clock(now)
        stage.recordClicked.connect(lambda: self.open_start_card())
        stage.stopClicked.connect(self.stop)
        stage.deleteClicked.connect(self.delete)
        stage.retryClicked.connect(self.retry)
        stage.downloadClicked.connect(self.download_model)
        stage.cancelDownloadClicked.connect(self.cancel_download)
        stage.startRequested.connect(self.start_recording)
        stage.summariseClicked.connect(lambda meeting_id: self.summarise(meeting_id, ms_origin_click()))
        stage.openSummaryClicked.connect(self.open_summary)
        stage.pageChanged.connect(self._on_page_changed)
        reading.recordMeeting.connect(lambda: self.open_start_card())
        reading.stopRecording.connect(self.stop)
        reading.tabChanged.connect(self._on_tab_changed)
        self.settings_panel = meetings_view.MeetingsSettingsPanel()
        self.settings_panel.microphonePicked.connect(lambda name: self.pick_devices(microphone=name))
        self.settings_panel.callAudioPicked.connect(lambda name: self.pick_devices(call_audio=name))
        self.settings_panel.downloadClicked.connect(self.download_model)
        settings = reading.settings_page
        settings.add_group(SETTINGS_KEY, SETTINGS_TITLE, note=SETTINGS_NOTE, how=SETTINGS_HOW, rows=self.settings_rows,
                           extra=self.settings_panel)
        settings.groupChanged.connect(lambda key: self.refresh_settings() if key == SETTINGS_KEY else None)
        reading.set_meetings_available(True)

    # ---- start-up, retention, the list ------------------------------------------------------

    def start(self) -> None:
        """Jarvis started: meetings a crash left "recording" are saved, retention runs, and the meetings waiting to
        be written down are queued."""
        problem = self.store.check_root()
        if problem:
            logger.warning("Meetings: the meetings folder is in %s; recording is refused", problem)
        try:
            self.store.recover(active=self._active())
        except OSError as exc:
            logger.warning("Meetings: recovery failed (%s)", type(exc).__name__)
        self.cleanup()
        self._daily.start()
        for record in reversed(self.store.list()):
            if record.status == mt.STATUS_RECORDED and record.audio_deleted_at is None:
                self._enqueue(record.id)
        self.refresh()
        self._next_job()

    def _active(self) -> list[str]:
        return [self.recorder.record.id] if self.recorder is not None and self.recorder.is_recording() else []

    def cleanup(self) -> tuple[int, int]:
        """Retention: audio after 7 days, the transcript and summary after 30 days (never a meeting recording or
        being written down). A meeting removed whole goes like a Delete (``meetingDeleted``): its waiting drafts,
        block cards and notes leave with it."""
        busy = self._active() + ([self._job[0]] if self._job is not None else [])
        removed: list[str] = []
        try:
            counts = self.store.cleanup(self._now(), active=busy, on_removed=removed.append)
        except OSError as exc:
            logger.warning("Meetings retention failed (%s)", type(exc).__name__)
            return 0, 0
        for meeting_id in removed:
            self._forget(meeting_id)
        if any(counts) and shiboken6.isValid(self.stage):
            self.refresh()
        for meeting_id in removed:
            self.meetingDeleted.emit(meeting_id)
        return counts

    def _forget(self, meeting_id: str) -> None:
        """A meeting gone from disk: out of every queue, its summary no longer shown."""
        if meeting_id in self._queue:
            self._queue.remove(meeting_id)
        self._summary_queue = [item for item in self._summary_queue if item[0] != meeting_id]
        self._summary_tasks.pop(meeting_id, None)
        if shiboken6.isValid(self.stage) and self.stage.showing_summary() == meeting_id:
            self.stage.show_list()

    def refresh(self) -> None:
        if self._shut and not shiboken6.isValid(self.stage):
            return
        elapsed = self.elapsed_text()
        recording_id = self._active()
        summaries = self.summaries_on()
        waiting = {meeting_id for meeting_id, _origin in self._summary_queue}
        call_missing = self.call_audio_missing()
        infos = [meetings_view.row_info(record, elapsed=elapsed if record.id in recording_id else "",
                                        summaries=summaries, waiting=record.id in waiting,
                                        call_missing=call_missing and record.id in recording_id)
                 for record in self.store.list()]
        self.stage.set_meetings(infos)
        self._sync_missing()

    def _sync_missing(self) -> None:
        pieces = sp.missing_pieces(self.models, audio_missing=self.audio_missing, speech=self.speech_missing)
        downloading = self.downloading()
        model_missing = any(piece.key == "model" for piece in pieces)
        self.stage.set_missing([f"Missing: {piece.text}" for piece in pieces], model_missing=model_missing,
                               downloading=downloading, download_text=self._dl_text)
        if shiboken6.isValid(self.settings_panel):
            if downloading:
                model_text = self._dl_text
            elif model_missing:
                model_text = f"Speech model: not downloaded. {sp.DOWNLOAD_TEXT}."
            else:
                model_text = f"Speech model: {sp.MODEL_NAME}, ready on this PC."
            self.settings_panel.set_model(model_text, can_download=model_missing and not downloading)

    # ---- the start card --------------------------------------------------------------------------

    def backend(self, *, fresh: bool = False) -> tuple[Any, str]:
        """The device library, or (None, what is missing). ``fresh`` (the start card, Settings > Meetings, a
        start - never while a meeting records): made again, so a device plugged in since and today's Windows
        defaults are seen (PortAudio lists them only when it starts)."""
        if fresh and self._backend is not None and not self.is_recording():
            old, self._backend = self._backend, None
            close = getattr(old, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - a library that already went
                    pass
        if self._backend is None:
            try:
                backend, missing = self.backend_factory()
            except Exception as exc:  # noqa: BLE001 - a broken add-on is reported, never raised
                logger.warning("Meetings: the audio add-on failed to start (%s)", type(exc).__name__)
                return None, ma.ADDON_AUDIO
            if backend is None:
                return None, missing
            self._backend = backend
        return self._backend, ""

    def device_choice(self, *, fresh: bool = False) -> tuple[tuple[str, bool], tuple[str, bool]]:
        """((microphone name, follows the default), (call-audio name, follows the default)) as a start would use."""
        backend, _missing = self.backend(fresh=fresh)
        if backend is None:
            return ("", True), ("", True)
        try:
            mic, mic_follows = ma.pick_device(backend.inputs() if self.prefs.microphone else [],
                                              backend.default_input(), self.prefs.microphone)
            call, call_follows = ma.pick_device(backend.outputs() if self.prefs.call_audio else [],
                                                backend.default_output(), self.prefs.call_audio)
        except Exception as exc:  # noqa: BLE001 - the card says no device was found
            logger.info("Meetings: the devices could not be listed (%s)", type(exc).__name__)
            return ("", True), ("", True)
        return ((mic.name if mic else "", mic_follows), (call.name if call else "", call_follows))

    def card_problem(self, mic: str) -> str:
        if self.store.check_root():
            return "The meetings folder is in a folder that syncs to the cloud, so Jarvis will not record there."
        missing = self.audio_missing()
        if missing:
            return (f"Recording needs the Python add-on {missing}. It is installed with your OK; Jarvis never "
                    "installs it.")
        backend, missing = self.backend()
        if backend is None:
            return f"Recording needs the Python add-on {missing or ma.ADDON_AUDIO}."
        if not mic:
            return "No microphone was found. Connect one, then open this card again."
        return ""

    def open_start_card(self, info: mt.MeetingInfo | None = None) -> None:
        """The Meetings screen with the start card for ``info`` (a meeting the owner pointed at: the record offer,
        Home's next-meeting tile, a Day view row), else the meeting he most likely starts now (the next one starting
        within 15 minutes before one already running), else an unscheduled call. The card names the meeting and
        lets him switch to another meeting on now or to an unscheduled call. It never records: only the card's
        Start does, after the consent tick."""
        if self._shut:
            return
        self._show_screen()
        if self.is_recording():
            return                                   # one at a time: the screen shows the one recording
        try:
            candidates = [item for item in self.meetings_now() or () if isinstance(item, mt.MeetingInfo)]
        except Exception:  # noqa: BLE001 - no calendar: an unscheduled call
            candidates = []
        if info is not None:
            candidates = [info] + [item for item in candidates if not same_meeting(item, info)]
        else:
            info = candidates[0] if candidates else None
        mic, call = self.device_choice(fresh=True)
        self.stage.show_list()                       # the card lives on the list page (a summary gives way)
        self.stage.card.open_card(info, mic, call, problem=self.card_problem(mic[0]), choices=candidates,
                                  summaries=self.summaries_on())
        logger.info("Meetings: start card opened (scheduled: %s, %d other meeting(s) to choose)",
                    bool(info and info.scheduled), max(0, len(candidates) - 1))

    def _on_tab_changed(self, _index: int) -> None:
        """Another screen came up: the start card closes (its ticks with it)."""
        if not self._on_screen():
            self.stage.card.close_card()

    def window_hidden(self) -> None:
        """The window was hidden or minimised: the start card closes (its ticks with it)."""
        self.stage.card.close_card()

    # ---- recording -----------------------------------------------------------------------------------

    def start_recording(self, request: Any) -> bool:
        """The start card's Start - the only way a recording begins. Refused without the consent tick."""
        if self._shut or not isinstance(request, mt.StartRequest) or request.consent is not True:
            logger.warning("Meetings: a start without the consent tick was refused")
            return False
        if self.is_recording():
            return False
        backend, missing = self.backend(fresh=True)
        if backend is None:
            self.stage.card.open_card(request.info, ("", True), ("", True),
                                      problem=f"Recording needs the Python add-on {missing}.")
            return False
        try:
            record = self.store.create(request)
        except (OSError, mt.ConsentMissing) as exc:
            logger.warning("Meetings: the meeting folder could not be made (%s)", type(exc).__name__)
            return False
        recorder = ma.Recorder(backend, self.store, record, mic_pick=self.prefs.microphone,
                               call_pick=self.prefs.call_audio, clock=self._mono, wall=self._now,
                               on_stop=self._stopped.emit, on_change=self._devices_changed.emit)
        try:
            recorder.start()                         # on any failure it closed every stream and file it opened
        except (ma.RecorderError, OSError) as exc:
            code = getattr(exc, "code", type(exc).__name__)
            logger.warning("Meeting %s: could not start (%s)", record.id, code)
            self.store.delete(record.id)
            mic, call = self.device_choice()
            problem = ("The microphone could not be opened. Check it, then try again."
                       if isinstance(exc, ma.RecorderError) else
                       "The meeting's files could not be written on this PC. Check the disk, then try again.")
            self.stage.card.open_card(request.info, mic, call, problem=problem, summaries=self.summaries_on())
            return False
        self.recorder = recorder
        if self.threaded:
            recorder.run_thread()
        self._timer.start()
        self._sync_indicators()
        self.refresh()
        self.recordingChanged.emit(True)
        return True

    def is_recording(self) -> bool:
        return self.recorder is not None and self.recorder.is_recording()

    def call_audio_missing(self) -> bool:
        """A meeting records but its call audio ("Others") does not: the device could not be opened (yet)."""
        recorder = self.recorder
        try:
            return recorder is not None and bool(recorder.call_audio_missing())
        except Exception:  # noqa: BLE001 - unknown: not said
            return False

    def elapsed_text(self) -> str:
        return mt.length_text(self.recorder.elapsed()) if self.is_recording() else ""

    def tray_text(self) -> str:
        """The tray tooltip while a meeting records ("" otherwise); it says when the call audio is not recorded."""
        if not self.is_recording():
            return ""
        return f"Jarvis: Recording {self.elapsed_text()}" + (" (no call audio)" if self.call_audio_missing() else "")

    def stop(self, reason: str = mt.STOP_BUTTON) -> bool:
        """Stop (the owner's Stop on the pill, the header, Home or the Meetings screen) and save."""
        recorder = self.recorder
        if recorder is None or not recorder.is_recording():
            return False
        return recorder.stop(reason)

    def _on_stopped(self, reason: str) -> None:
        """On the GUI thread after any stop: the indicators go, the list updates, the writing down is queued."""
        self._timer.stop()
        recorder = self.recorder
        self._sync_indicators()
        self.recordingChanged.emit(False)
        if self._shut or recorder is None:
            return
        logger.info("Meeting %s: saved (%s)", recorder.record.id, reason)
        self._enqueue(recorder.record.id)
        self.refresh()
        self._next_job()

    def _sync_indicators(self) -> None:
        """Every REC on screen, the pill and the tray tooltip, from the recorder's state (each second)."""
        elapsed = self.elapsed_text()
        warning = meetings_view.CALL_MISSING_TEXT if elapsed and self.call_audio_missing() else ""
        if shiboken6.isValid(self.reading):
            self.reading.set_recording(elapsed, warning=warning)
            row = self.stage.row(self.recorder.record.id) if elapsed and self.recorder is not None else None
            if row is not None:
                row.length_label.setText(elapsed)
                row.set_call_missing(bool(warning))
        if elapsed and not self._shut:
            if self.pill is None:
                self.pill = meetings_view.RecPill()
                self.pill.stopClicked.connect(self.stop)
            self.pill.show_recording(elapsed, warning=warning)
        elif self.pill is not None:
            self.pill.dismiss()
        self._on_tray()

    def pick_devices(self, *, microphone: str | None = None, call_audio: str | None = None) -> None:
        """Settings > Meetings: a device picked ("" follows the Windows default). Used from the next start."""
        self.prefs.set(microphone=microphone, call_audio=call_audio)
        self.refresh_settings()

    # ---- writing it down ------------------------------------------------------------------------------

    def ready_to_write(self) -> bool:
        return not self.speech_missing() and sp.model_ready(sp.model_dir(self.models))

    def _enqueue(self, meeting_id: str) -> None:
        if meeting_id not in self._queue and (self._job is None or self._job[0] != meeting_id):
            self._queue.append(meeting_id)

    def _next_job(self) -> None:
        while self._job is None and self._queue and not self._shut:
            if not self.ready_to_write():
                self._sync_missing()
                return
            meeting_id = self._queue.pop(0)
            record = self.store.load(meeting_id)
            if record is None or record.status != mt.STATUS_RECORDED or record.audio_deleted_at is not None:
                continue
            self.store.update(meeting_id, status=mt.STATUS_TRANSCRIBING, progress=0, error="")
            self._saved_progress[meeting_id] = 0
            try:
                job = self.process_factory(self.store.folder(meeting_id), sp.model_dir(self.models),
                                           lambda percent, m=meeting_id: self._progress.emit(m, percent),
                                           lambda error, m=meeting_id: self._finished.emit(m, error))
                self._job = (meeting_id, job)
                job.start()
            except Exception as exc:  # noqa: BLE001 - the meeting shows Failed with Retry
                logger.warning("Meeting %s: the writing down could not start (%s)", meeting_id, type(exc).__name__)
                self._job = None
                self.store.update(meeting_id, status=mt.STATUS_FAILED, error=sp.ERR_WORKER)
                continue
            logger.info("Meeting %s: writing it down", meeting_id)
            self.refresh()

    def _on_progress(self, meeting_id: str, percent: int) -> None:
        if self._job is None or self._job[0] != meeting_id:
            return
        if percent - self._saved_progress.get(meeting_id, 0) >= 5 or percent >= 100:
            self._saved_progress[meeting_id] = percent
            self.store.update(meeting_id, progress=percent)
        row = self.stage.row(meeting_id)
        if row is not None:
            row.status_label.setText(f"Writing it down {percent}%")

    def _on_finished(self, meeting_id: str, error: str) -> None:
        if self._job is not None and self._job[0] == meeting_id:
            self._job = None
        record = self.store.load(meeting_id)
        if record is not None and record.status == mt.STATUS_DELETING:
            record = None                                # the owner's Delete of it is under way
        if record is not None:
            if not error:
                self.store.update(meeting_id, status=mt.STATUS_TRANSCRIBED, progress=100, error="", summary_error="")
            elif error == sp.ERR_CANCELLED:
                self.store.update(meeting_id, status=mt.STATUS_RECORDED, progress=0)
            else:
                self.store.update(meeting_id, status=mt.STATUS_FAILED, error=error[:40])
            logger.info("Meeting %s: writing it down %s", meeting_id, "done" if not error else f"stopped ({error})")
        if not self._shut:
            self.refresh()
            self._next_job()
            if record is not None and not error and record.summary_asked and self.summaries_on():
                # Only because the owner ticked "summarise it" on this meeting's start card; otherwise the
                # transcript stays on this PC until his click on Summarise.
                self.summarise(meeting_id, ms_origin_auto())

    def retry(self, meeting_id: str) -> bool:
        """A failed meeting's Retry: written down again (when the add-on and the model are there)."""
        record = self.store.load(meeting_id)
        if record is None or record.status != mt.STATUS_FAILED:
            return False
        self.store.update(meeting_id, status=mt.STATUS_RECORDED, error="", progress=0)
        self._enqueue(meeting_id)
        self.refresh()
        self._next_job()
        return True

    # ---- part 2: the summary ----------------------------------------------------------------------------

    def summaries_on(self) -> bool:
        """A transcript can be summarised: Ask Jarvis is on (the app set its runner)."""
        return self.summary_runner is not None and not self._shut

    def summarise(self, meeting_id: str, origin: str) -> bool:
        """Summarise a written-down meeting - only as the owner asked: his "summarise it" tick on its start card
        (once its transcript is ready) or his click on Summarise / Retry. The app's runner starts one planner run;
        when it can't start now the meeting waits for the Ask. False when there is nothing to summarise (no such
        meeting, no transcript, summaries off)."""
        runner = self.summary_runner
        record = self.store.load(meeting_id)
        if runner is None or self._shut or record is None or record.status != mt.STATUS_TRANSCRIBED:
            return False
        if not self.store.transcript_path(meeting_id).is_file():
            return False
        if self._summary_running:
            self._wait(meeting_id, origin)
            return True
        try:
            answer = runner(job_of(self.store, record), origin)
        except Exception as exc:  # noqa: BLE001 - the meeting keeps its transcript; Summarise tries again
            logger.warning("Meeting %s: the summary could not start (%s)", meeting_id, type(exc).__name__)
            answer = SUMMARY_OFF
        if answer == SUMMARY_STARTED:
            self._summary_queue = [item for item in self._summary_queue if item[0] != meeting_id]
            self._summary_running, self._summary_origin = meeting_id, origin
            self.store.update(meeting_id, status=mt.STATUS_SUMMARISING, summary_error="")
            logger.info("Meeting %s: summary started (%s)", meeting_id, origin)
        elif answer == SUMMARY_BUSY:
            self._wait(meeting_id, origin)
        else:
            logger.info("Meeting %s: no summary (%s)", meeting_id, answer or "off")
        self.refresh()
        return answer in (SUMMARY_STARTED, SUMMARY_BUSY)

    def _wait(self, meeting_id: str, origin: str) -> None:
        if all(item[0] != meeting_id for item in self._summary_queue):
            self._summary_queue.append((meeting_id, origin))
            logger.info("Meeting %s: summary waits for the Ask (%s)", meeting_id, origin)
        if not self._summary_retry.isActive():
            self._summary_retry.start()

    def pump_summaries(self) -> None:
        """The Ask may be free: the oldest waiting summary starts (the app calls this when a request ends)."""
        if self._summary_running or self._shut:
            return
        while self._summary_queue:
            meeting_id, origin = self._summary_queue.pop(0)
            record = self.store.load(meeting_id)
            if record is None or record.status != mt.STATUS_TRANSCRIBED:
                continue
            self.summarise(meeting_id, origin)
            break
        if not self._summary_queue:
            self._summary_retry.stop()

    def summary_running(self) -> str:
        return self._summary_running

    def on_summary(self, outcome: Any, task_id: int = 0, *, meeting_id: str = "") -> ms.MeetingSummary | None:
        """A summary run ended (the app hands its outcome): a checked summary goes to the meeting's summary.json
        (Summary ready); a failure keeps the transcript with its code (Retry). A meeting deleted meanwhile stays
        deleted (nothing is written). An outcome that names no meeting (a run that stopped before it read one)
        is the running summary's. Returns the kept summary."""
        meeting_id = str(getattr(outcome, "target", "") or meeting_id or self._summary_running or "")
        if self._summary_running == meeting_id:
            self._summary_running = ""
        if not meeting_id:
            return None
        if task_id:
            self._summary_tasks[meeting_id] = int(task_id)
        record = self.store.load(meeting_id)
        if record is not None and record.status == mt.STATUS_DELETING:
            record = None
        summary = getattr(outcome, "brief", None)
        kept: ms.MeetingSummary | None = None
        if record is None:
            logger.info("Meeting %s: summary ended after the meeting was deleted (nothing kept)", meeting_id)
        elif getattr(outcome, "ok", False) and isinstance(summary, ms.MeetingSummary):
            try:
                self.store.save_summary(meeting_id, ms.summary_json(summary))
                self.store.update(meeting_id, status=mt.STATUS_SUMMARY_READY, summary_error="")
                kept = summary
                logger.info("Meeting %s: summary ready (%s)", meeting_id,
                            ", ".join(f"{name} {count}" for name, count in summary.counts().items()))
            except OSError as exc:
                logger.warning("Meeting %s: the summary could not be saved (%s)", meeting_id, type(exc).__name__)
                self.store.update(meeting_id, status=mt.STATUS_TRANSCRIBED, summary_error="disk")
        elif getattr(outcome, "kind", "") == "busy":   # another request took the planner first: it waits again
            self.store.update(meeting_id, status=mt.STATUS_TRANSCRIBED, summary_error="")
            self._wait(meeting_id, self._summary_origin or ms_origin_click())
        else:
            code = str(getattr(outcome, "kind", "") or "error")[:40]
            self.store.update(meeting_id, status=mt.STATUS_TRANSCRIBED, summary_error=code)
            logger.info("Meeting %s: summary failed (%s)", meeting_id, code)
        if not self._shut:
            self.refresh()
            self.summaryChanged.emit(meeting_id)
            self.pump_summaries()
        return kept

    def on_summary_cancelled(self) -> None:
        """The running summary was cancelled (Esc on the bar): the transcript stays; Summarise starts it again."""
        meeting_id, self._summary_running = self._summary_running, ""
        if meeting_id and self.store.load(meeting_id) is not None:
            self.store.update(meeting_id, status=mt.STATUS_TRANSCRIBED, summary_error=SUMMARY_CANCELLED)
            logger.info("Meeting %s: summary cancelled", meeting_id)
        if not self._shut:
            self.refresh()
            self.summaryChanged.emit(meeting_id)

    def summary_of(self, meeting_id: str) -> ms.MeetingSummary | None:
        """The meeting's kept summary (summary.json, every text checked again), or None."""
        try:
            raw = self.store.load_summary(meeting_id)
        except ValueError:
            return None
        return ms.summary_of(raw, meeting_id)

    def steps_link(self, meeting_id: str) -> str:
        """"live:<task>" for the summary run of this session (See every step); "" otherwise."""
        task_id = self._summary_tasks.get(meeting_id, 0)
        return f"live:{task_id}" if task_id else ""

    def open_summary(self, meeting_id: str) -> bool:
        """A meeting's Open summary: the summary takes the list's place; the app lends its cards and renders it
        (summaryOpened). False when it has no summary."""
        record = self.store.load(meeting_id)
        summary = self.summary_of(meeting_id) if record is not None else None
        if record is None or summary is None:
            return False
        self.render_summary(meeting_id)
        logger.info("Meeting %s: summary opened", meeting_id)
        self.summaryOpened.emit(meeting_id)
        return True

    def render_summary(self, meeting_id: str, **states: Any) -> bool:
        """Show (or show again) the summary with the card, block and note states the app knows."""
        record = self.store.load(meeting_id)
        summary = self.summary_of(meeting_id) if record is not None else None
        if record is None or summary is None:
            return False
        self.stage.show_summary(summary_info(record, summary, steps_link=self.steps_link(meeting_id), **states))
        return True

    def _on_page_changed(self) -> None:
        if not self.stage.showing_summary():
            self.refresh()

    # ---- delete ---------------------------------------------------------------------------------------

    def delete(self, meeting_id: str) -> bool:
        """The owner's Delete (confirmed on the row): audio, transcript and summary at once. Never the meeting that
        is recording (stop it first); a writing down of it is stopped first and waited for (its files are free
        then). A file that still could not go keeps the meeting listed with "Delete again" - never a row that
        vanishes while its audio stays. False then."""
        if self.recorder is not None and self.recorder.is_recording() and self.recorder.record.id == meeting_id:
            return False
        if self._job is not None and self._job[0] == meeting_id:
            job = self._job[1]
            self._job = None
            try:
                job.cancel()                         # kills the worker and waits until it has ended
            except Exception:  # noqa: BLE001 - the process may be gone already
                pass
        self._forget(meeting_id)
        done = self.store.delete(meeting_id)
        if not done and self._folder_exists(meeting_id):
            time.sleep(DELETE_RETRY_S)               # a file handle the ended worker held is let go of by now
            done = self.store.delete(meeting_id)
        if not done:
            logger.info("Meeting %s: some files stayed; listed to delete again", meeting_id)
        self.refresh()
        self._next_job()
        self.meetingDeleted.emit(meeting_id)
        return done

    def _folder_exists(self, meeting_id: str) -> bool:
        try:
            return self.store.folder(meeting_id).exists()
        except ValueError:
            return False

    # ---- the speech model (only on the owner's click) ---------------------------------------------------

    def downloading(self) -> bool:
        return self._download is not None

    def download_model(self) -> bool:
        """Download (Settings > Meetings or the Meetings screen): the speech model from Hugging Face, resumable."""
        if self._shut or self.downloading() or sp.model_ready(sp.model_dir(self.models)):
            return False
        self._dl_cancel = threading.Event()
        downloader = self.downloader_factory(sp.model_dir(self.models), self._dl_progress.emit, self._dl_cancel)
        self._dl_text = "Starting the download..."
        logger.info("Speech model: download started by the owner")
        def run() -> None:
            code = sp.ERR_DISK
            try:
                code = downloader.run()
            except Exception as exc:  # noqa: BLE001 - the download always ends with a word on screen
                logger.info("Speech model: the download stopped (%s)", type(exc).__name__)
            finally:
                self._dl_done.emit(code)

        if not self.threaded:
            self._download = threading.current_thread()
            self._sync_missing()
            run()
            return True
        thread = threading.Thread(target=run, name="meeting-model", daemon=True)
        self._download = thread
        self._sync_missing()
        thread.start()
        return True

    def cancel_download(self) -> None:
        self._dl_cancel.set()

    def _on_dl_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        mb = 1 << 20
        self._dl_text = (f"Downloading the speech model: {done // mb:,} of {total // mb:,} MB "
                         f"({min(100, 100 * done // total)}%)")
        self._sync_missing()

    def _on_dl_done(self, code: str) -> None:
        self._download = None
        if code:
            words = _ERROR_WORDS.get(code, "it stopped")
            self._dl_text = f"The download stopped ({words}). Download again continues where it stopped."
        else:
            self._dl_text = ""
        logger.info("Speech model: download %s", "finished" if not code else f"stopped ({code})")
        if self._shut:
            return
        self._sync_missing()
        if not code:
            for record in reversed(self.store.list()):
                if record.status == mt.STATUS_RECORDED and record.audio_deleted_at is None:
                    self._enqueue(record.id)
            self._next_job()

    # ---- Settings > Meetings ----------------------------------------------------------------------------

    def settings_rows(self) -> list[tuple[str, str]]:
        return [("Recording", "On - starts only when you click Start, after the consent tick"),
                ("Consent", "Ticked on the card for every meeting; never remembered"),
                ("While it records", "REC in the header and a pill on top, also when Jarvis is minimised"),
                ("Stops by itself", "15 minutes after the meeting's end, after 10 minutes of silence, or at 3 hours"),
                ("Audio", "Kept 7 days on this PC, then deleted; never uploaded"),
                ("Transcript and summary", "Kept 30 days on this PC, then deleted"),
                ("Speech to text", f"On this PC ({sp.MODEL_NAME}); the audio never leaves it"),
                ("Summary", SUMMARY_ROW_ON if self.summaries_on() else "Needs Ask Jarvis (your Claude Code) on"),
                ("Follow-ups", "Drafts to the invite's guests only; nothing is sent until you click Send"),
                ("Record offer", "Shortly before a meeting with guests (with the productivity bundle); it only opens "
                                 "the start card"),
                ("Kept in", "The meetings folder inside the data folder")]

    def refresh_settings(self) -> None:
        """The device rows (what is connected now) and the model's line."""
        backend, _missing = self.backend(fresh=True)
        mic_choices: list[str] = []
        call_choices: list[str] = []
        mic_default = call_default = ""
        if backend is not None:
            try:
                mic_choices = [d.name for d in backend.inputs()]
                call_choices = [d.name for d in backend.outputs()]
                mic_default = getattr(backend.default_input(), "name", "")
                call_default = getattr(backend.default_output(), "name", "")
            except Exception as exc:  # noqa: BLE001 - shown as none found
                logger.info("Meetings: the devices could not be listed (%s)", type(exc).__name__)
        self.settings_panel.set_devices((self.prefs.microphone, mic_default, mic_choices),
                                        (self.prefs.call_audio, call_default, call_choices))
        self._sync_missing()

    # ---- closing ------------------------------------------------------------------------------------------

    def shutdown(self) -> None:
        """Jarvis is closing: a recording stops and is saved, a writing down stops (it starts again next time),
        a download stops (it continues next time), the pill goes."""
        if self._shut:
            return
        self._shut = True
        self._timer.stop()
        self._daily.stop()
        self._summary_retry.stop()
        if self.is_recording():
            assert self.recorder is not None
            self.recorder.stop(mt.STOP_CLOSING)
        backend, self._backend = self._backend, None    # the device library ends too (PortAudio let go)
        close = getattr(backend, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - closing goes on
                pass
        if self._job is not None:
            meeting_id, job = self._job
            self._job = None
            try:
                job.cancel()
            except Exception:  # noqa: BLE001 - closing goes on
                pass
            if self.store.load(meeting_id) is not None:
                self.store.update(meeting_id, status=mt.STATUS_RECORDED, progress=0)
        self._dl_cancel.set()
        if self.pill is not None:
            self.pill.dismiss()
            self.pill.deleteLater()
            self.pill = None


def ms_origin_auto() -> str:
    from .ask.modes import ORIGIN_AUTO

    return ORIGIN_AUTO


def ms_origin_click() -> str:
    from .ask.modes import ORIGIN_SUMMARISE

    return ORIGIN_SUMMARISE


def job_of(store: mt.MeetingStore, record: mt.MeetingRecord) -> ms.MeetingJob:
    """What a summary run is handed (memory only): the transcript file and the facts kept with the recording."""
    return ms.MeetingJob(record.id, store.transcript_path(record.id), record.title, record.scheduled_start
                         or record.started_at, record.duration_s, record.alias,
                         tuple(ms.MeetingGuest(name, address) for name, address in record.invite))


def due_text(due: str) -> str:
    """"due Wed 22 Oct" from "2026-10-22" ("" for none)."""
    from datetime import date

    try:
        day = date.fromisoformat(due)
    except (TypeError, ValueError):
        return ""
    return f"due {day:%a} {day.day} {day:%b}"


def summary_info(record: mt.MeetingRecord, summary: ms.MeetingSummary, *,
                 blocks: dict[int, tuple[str, str]] | None = None,
                 follow_ups: list[tuple[str, str]] | None = None,
                 notes: list[str] | None = None, steps_link: str = "") -> sv.SummaryInfo:
    """What the summary view shows: the summary's own parts (yours first), each of your items' block state
    ((state, card id) by index), each follow-up's (state words, card id), each note's state, See every step."""
    blocks = blocks or {}
    started = record.scheduled_start or record.started_at
    meta = " \u00b7 ".join(part for part in (f"{started:%a %d %b} {meetings_view._clock(started)}" if started else "",
                                             mt.length_text(record.duration_s)) if part)
    actions = []
    for index, item in enumerate(summary.actions):
        state, card_id = blocks.get(index, (sv.BLOCK_NONE, ""))
        actions.append(sv.SummaryAction("You" if item.mine else item.who, item.what, due_text(item.due), item.mine,
                                        state if item.mine else sv.BLOCK_NONE, card_id if item.mine else ""))
    follows = []
    for index, item in enumerate(summary.follow_ups):
        state, card_id = (follow_ups or [])[index] if index < len(follow_ups or []) else ("", "")
        follows.append(sv.SummaryFollowUp(item.to_name, item.subject, state, card_id))
    shown = [sv.SummaryNote(text, (notes or [])[index] if index < len(notes or []) else sv.NOTE_OFF)
             for index, text in enumerate(summary.memory)]
    names = [name for name, _address in record.invite] or list(record.guests)
    return sv.SummaryInfo(record.id, summary.title or record.shown_title(), meta.upper(), sv.guests_text(names),
                          summary.decisions, tuple(actions), summary.questions, tuple(follows), tuple(shown),
                          sv.note_text(summary.dropped), steps_link)


RECORD_OFFER_LINK = "Open the start card"
RECORD_OFFER_TAIL = ("Record this meeting? The link only opens the start card: you tick the consent box and click "
                     "Start yourself.")


def _clock_words(moment: datetime, hour24: bool) -> str:
    if hour24:
        return f"{moment.hour:02d}:{moment.minute:02d}"
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def record_offer_text(title: str, start: datetime, alias: str, minutes: int, guests: int, *,
                      hour24: bool = False) -> str:
    """"Project sync at 2:00 PM (work) starts in 4 minutes, with 3 guests. Record this meeting? ..." (on screen
    only; never spoken or logged)."""
    shown = " ".join(str(title or "").split()) or "(No title)"
    where = f" ({alias})" if alias else ""
    left = int(minutes)
    starts = (f"starts in {left} minute{'' if left == 1 else 's'}" if left > 0 else
              "has started" if left < 0 else "starts now")
    count = max(0, int(guests))
    return (f"{shown} at {_clock_words(start, hour24)}{where} {starts}, with {count} guest{'' if count == 1 else 's'}. "
            + RECORD_OFFER_TAIL)


def record_offer_used_text(at: datetime, hour24: bool = False) -> str:
    """"Start card opened (1:58 PM)." (the offer's entry after its click)."""
    return f"Start card opened ({_clock_words(at, hour24)})."


def record_offer_gone_text(title: str, start: datetime, hour24: bool = False) -> str:
    """The offer went (the meeting is well under way or over): the Meetings screen still has Record a meeting."""
    shown = " ".join(str(title or "").split()) or "(No title)"
    return (f"The offer to record {shown} at {_clock_words(start, hour24)} has gone; Record a meeting on the Meetings "
            "screen still opens the start card.")


def meeting_info_of(alias: str, brief: Any, own: Any = ()) -> mt.MeetingInfo:
    """A calendar event (gcal.EventBrief) as the start card's MeetingInfo: its title and times, the account and
    calendar it is on, and the invite's guests - the organiser (when it is not the owner) and every guest with an
    address, the owner's own mailboxes left out (names for the card, (name, address) for a follow-up's checks)."""
    from .actions import email_address
    from .recipients import same_mailbox

    mine = [str(item).casefold() for item in own or ()]

    def is_own(address: str) -> bool:
        return any(address.casefold() == item or same_mailbox(address, item) for item in mine)

    invite: list[tuple[str, str]] = []
    if not getattr(brief, "organizer_self", False) and getattr(brief, "organizer_email", ""):
        invite.append((str(getattr(brief, "organizer_name", "") or ""), str(brief.organizer_email)))
    for guest in getattr(brief, "guests", ()) or ():
        invite.append((str(getattr(guest, "name", "") or ""), str(getattr(guest, "email", "") or "")))
    invite = [(name, email_address(address)) for name, address in invite
              if email_address(address) and not is_own(email_address(address))]
    info = mt.MeetingInfo(str(getattr(brief, "title", "") or ""), getattr(brief, "start", None),
                          getattr(brief, "end", None), str(getattr(brief, "event_id", "") or ""),
                          tuple(name or address for name, address in invite), alias=alias,
                          calendar_id=str(getattr(brief, "calendar_id", "") or ""), invite=tuple(invite))
    return info


START_LEAD = timedelta(minutes=15)          # a meeting starting within this long counts as "on now"


def same_meeting(first: mt.MeetingInfo, second: mt.MeetingInfo) -> bool:
    """The same calendar event (its id, else its title and start)."""
    if first.event_id and second.event_id:
        return first.event_id == second.event_id and first.alias == second.alias
    return (first.title.casefold(), first.start) == (second.title.casefold(), second.start)


def meeting_rank(start: datetime, now: datetime, *, block: bool = False, guests: int = 0) -> tuple:
    """How likely the owner is starting this meeting now (lower first): a real meeting before a block of time
    (focus time, out of office, "show as free"), then the one whose start is closest to now - so at 2:52 the 3:00
    call wins over the 2:00-3:00 meeting that is ending - then one with other guests, then the earlier."""
    wall = now if (now.tzinfo is None) == (start.tzinfo is None) else now.replace(tzinfo=start.tzinfo)
    return (1 if block else 0, abs((start - wall).total_seconds()), 0 if guests else 1, start)


def meetings_now(events: Any, now: datetime, *, lead: timedelta = START_LEAD) -> list[mt.MeetingInfo]:
    """The timed calendar events on now (or starting within ``lead``) as MeetingInfos, the likeliest first
    (``meeting_rank``)."""
    found = []
    for event in events or ():
        start, end = getattr(event, "start", None), getattr(event, "end", None)
        if start is None or end is None:
            continue
        wall = now if (now.tzinfo is None) == (start.tzinfo is None) else now.replace(tzinfo=start.tzinfo)
        if start - lead <= wall < end:
            found.append((meeting_rank(start, now), mt.MeetingInfo(getattr(event, "title", ""), start, end)))
    return [info for _rank, info in sorted(found, key=lambda item: item[0])]


def meeting_now(events: Any, now: datetime, *, lead: timedelta = START_LEAD) -> mt.MeetingInfo | None:
    """The likeliest of ``meetings_now`` (None: none on now)."""
    found = meetings_now(events, now, lead=lead)
    return found[0] if found else None
