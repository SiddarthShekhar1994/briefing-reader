"""Tests for the LIVE view's stream in the app (AppController.live), without the LIVE widgets.

An approved card's task from the click (and a Send's hidden hold: SENDING, never a number) to Google's
answer (the exact payload, the call and its result), Undo during the hold, a sending account that changed,
Jarvis closing, the briefing fetch's compact task, the rolling "Background reads" task, and
[live] enabled / background = false. Qt runs offscreen; fakes only: nothing reaches Google,
Notion or claude.exe, and every name, address and id is invented.
"""

from __future__ import annotations

import dataclasses
import time
import types
import unittest
from datetime import date, datetime
from unittest import mock

from PySide6.QtWidgets import QApplication

from briefing_reader import live, pictures, ui
from briefing_reader.actions import SOURCE_ASK, STATUS_FAILED, parse_action_line
from briefing_reader.gcal import CalendarEvent
from briefing_reader.notion_client import NotionError, PollResult
from tests import ui_fakes
from tests.ui_fakes import (COMMAND, EMAIL_LINE, GUEST, MOVE_LINE, NOW, PDT, AppHarness, fonts, plan, settle,
                            wait_for)

_app: QApplication | None = None
TRUSTED = "ana@example.edu"                 # a trusted-domain recipient: no Edit needed before Send
MAIL_LINE = EMAIL_LINE.replace(GUEST, TRUSTED)


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_live_app", "-platform", "offscreen"])
    fonts()


def ask_card(line: str):
    return dataclasses.replace(parse_action_line(line), source=SOURCE_ASK)


def fields(step: live.StepView) -> dict[str, str]:
    return {field.label: field.value for field in step.fields}


def kinds(task: live.TaskView) -> list[str]:
    return [step.kind for step in task.steps]


def step_of(task: live.TaskView, kind: str) -> live.StepView:
    return next(step for step in task.steps if step.kind == kind)


class LiveAppCase(unittest.TestCase):
    config_extra = ""

    def setUp(self) -> None:
        self.app = AppHarness(self, config_extra=self.config_extra)
        self.app.reading(autoplay=False)
        self.c = self.app.c

    def tasks(self, kind: str) -> list[live.TaskView]:
        return [task for task in self.c.live.snapshot() if task.kind == kind]

    def task(self, kind: str = live.TASK_ACTION) -> live.TaskView:
        (task,) = self.tasks(kind)
        return task

    def background(self) -> live.TaskView:
        return self.task(live.TASK_BACKGROUND)

    def approve(self, action) -> None:
        card = self.c.window.reading.action_card(action.id)
        card.approve_button.click()
        settle()
        if action.held:   # a Send / Cancel: Sent at once, the call after a hidden hold
            self.assertIsNotNone(self.c._send_hold, self.c._hints.get(action.id))
        else:             # a calendar change: on its way at once (no countdown)
            self.assertTrue(action.id in self.c._jobs or self.c._store.is_decided(action.id),
                            self.c._hints.get(action.id))

    def run_out(self) -> None:
        """A Send's / Cancel's hidden hold ends (a calendar change has none)."""
        if self.c._send_hold is not None:
            self.c._send_hold.deadline = time.monotonic() - 0.001
            self.c._on_send_hold_tick()

    def wait_result(self, action) -> None:
        self.assertTrue(wait_for(lambda: action.id not in self.c._jobs and action.id not in self.c._running, 5))
        settle()

    def add_mail(self):
        mail = parse_action_line(MAIL_LINE)
        self.c._set_source("briefing", list(self.c._source_lists["briefing"]) + [mail])
        settle()
        return mail


class ActionTaskTests(LiveAppCase):
    def test_the_stream_follows_live(self) -> None:
        self.assertTrue(self.c.live.enabled)
        self.assertTrue(self.c.live.keep_text)
        self.assertIs(self.c.ask.live, self.c.live)

    def test_an_email_shows_exactly_what_is_sent(self) -> None:
        mail = self.add_mail()
        self.approve(mail)
        task = self.task()
        self.assertEqual(task.title, "Email work: Jarvis test sync moved")
        self.assertEqual(kinds(task), [live.ACTION_APPROVED, live.ACTION_PAYLOAD, live.ACTION_HOLD])
        approved = step_of(task, live.ACTION_APPROVED)
        self.assertEqual((fields(approved)["Card"], fields(approved)["Account"]), ("BRIEFING EMAIL", "work"))
        payload = step_of(task, live.ACTION_PAYLOAD)
        self.assertEqual((payload.status, payload.status_text, payload.title),
                         (live.STATUS_RUNNING, "SENDING", "Will send exactly this"))
        shown = fields(payload)
        self.assertEqual((shown["From"], shown["To"], shown["Cc"], shown["Subject"]),
                         ("me@example.edu", TRUSTED, "none", "Jarvis test sync moved"))
        body = payload.blocks[0]
        self.assertEqual((body.label, body.text, body.start_open),
                         ("Message", "Hi Sam,\nMoved to Friday at 2 PM.\nThanks\n", True))
        self.assertTrue(payload.open_hint)
        hold = step_of(task, live.ACTION_HOLD)
        self.assertEqual((hold.deadline, hold.status_text), (self.c._send_hold.deadline, "SENDING"))
        self.assertEqual(live.status_word(hold, time.monotonic()), "SENDING")   # the truth, never a number
        self.assertEqual(self.app.senders["work"].sent, [])
        self.run_out()
        self.wait_result(mail)
        task = self.task()
        self.assertEqual((task.status, task.summary), (live.STATUS_OK, "Sent"))
        self.assertEqual(kinds(task), [live.ACTION_APPROVED, live.ACTION_PAYLOAD, live.ACTION_HOLD,
                                       live.ACTION_CHECKS, live.ACTION_CALL])
        call = step_of(task, live.ACTION_CALL)
        self.assertEqual((call.status, call.status_text, fields(call)["Gmail message id"]),
                         (live.STATUS_OK, "SENT", "m1"))
        payload = step_of(task, live.ACTION_PAYLOAD)
        self.assertEqual((payload.status, payload.status_text), (live.STATUS_OK, "SENT EXACTLY THIS"))
        self.assertEqual(step_of(task, live.ACTION_HOLD).summary, "sending now")
        self.assertEqual(len(self.app.senders["work"].sent), 1)
        self.assertEqual(self.c.live.running(), 0)

    def test_undo_sends_nothing(self) -> None:
        mail = self.add_mail()
        self.approve(mail)
        self.c._send_hold.started -= 5
        self.c.undo_action(mail.id)
        settle()
        task = self.task()
        self.assertEqual((task.status, task.status_text, task.summary),
                         (live.STATUS_CANCELLED, "UNDONE", "Undone - nothing was sent"))
        hold = step_of(task, live.ACTION_HOLD)
        self.assertEqual((hold.status, hold.status_text, hold.summary),
                         (live.STATUS_CANCELLED, "UNDONE", "nothing was sent"))
        self.assertEqual(step_of(task, live.ACTION_PAYLOAD).status, live.STATUS_CANCELLED)
        self.assertEqual(self.app.senders["work"].sent, [])

    def test_a_sending_account_that_changed_is_blocked(self) -> None:
        mail = self.add_mail()
        self.approve(mail)
        self.app.senders["work"].address = "other@example.edu"
        self.run_out()
        settle()
        task = self.task()
        self.assertEqual(task.status, live.STATUS_BLOCKED)
        self.assertEqual(step_of(task, live.ACTION_HOLD).summary,
                         "the sending account changed before it went - nothing was sent")
        self.assertEqual(self.app.senders["work"].sent, [])

    def test_a_calendar_event_and_a_retry(self) -> None:
        calendar = next(action for action in self.c._actions if action.kind == "calendar")
        self.c._store.set(calendar.id, STATUS_FAILED, message="Earlier failure", kind="calendar", account="personal")
        settle()
        self.approve(calendar)
        task = self.task()
        self.assertEqual(task.title, "Add event: Club general meeting (retry)")
        approved = step_of(task, live.ACTION_APPROVED)
        self.assertEqual(fields(approved)["Retry"], "the last try ended FAILED")
        payload = step_of(task, live.ACTION_PAYLOAD)
        self.assertEqual(payload.title, "Will add exactly this")
        self.assertEqual(fields(payload)["Title"], "Club general meeting")
        self.assertNotIn(live.ACTION_HOLD, kinds(task))   # a calendar change goes at once: no hold
        self.run_out()
        self.wait_result(calendar)
        task = self.task()
        self.assertEqual(task.status, live.STATUS_OK)
        call = step_of(task, live.ACTION_CALL)
        self.assertEqual((call.status_text, fields(call)["Event id"]), ("ADDED", "evt-new"))
        link = next(field.link for field in call.fields if field.label == "Event")
        self.assertEqual(link, "https://www.google.com/calendar/event?eid=new")
        self.assertEqual(step_of(task, live.ACTION_PAYLOAD).status_text, "ADDED")

    def test_a_move_shows_googles_event_and_the_background_check(self) -> None:
        move = ask_card(MOVE_LINE)
        self.c._take_ask_cards([move])
        self.assertTrue(wait_for(lambda: move.id in self.c._checks, 5))
        settle()
        peek = step_of(self.background(), live.EVENT_CHECK)
        self.assertEqual(peek.title, "Checked 1 card's events with Google (read only)")
        self.assertTrue(peek.items[0].text.startswith("Google: Jarvis test sync"))
        self.approve(move)
        task = self.task()
        self.assertEqual(task.title, "Move work: Jarvis test sync")
        approved = fields(step_of(task, live.ACTION_APPROVED))
        self.assertEqual(approved["Card"], "ASK MOVE")
        self.assertTrue(approved["Google's event now"].startswith("Google: Jarvis test sync"))
        payload = fields(step_of(task, live.ACTION_PAYLOAD))
        self.assertEqual((payload["Event id"], payload["New time"]),
                         ("jts0001aa", "Fri Oct 9 2:00-3:00 PM, in your calendar's time zone"))
        self.run_out()
        self.wait_result(move)
        call = step_of(self.task(), live.ACTION_CALL)
        self.assertEqual((call.status, call.status_text), (live.STATUS_OK, "MOVED"))

    def test_shutdown_during_a_hold_sends_it(self) -> None:
        mail = self.add_mail()
        self.approve(mail)
        closed: list[tuple] = []
        real_close = self.c.live.close

        def close() -> None:
            closed.append(self.c.live.snapshot())
            real_close()

        self.c.live.close = close   # type: ignore[method-assign]
        with mock.patch.object(ui, "_remove_audio_dir_after"):
            self.c.shutdown()
        (snapshot,) = closed
        (task,) = [view for view in snapshot if view.kind == live.TASK_ACTION]
        hold = step_of(task, live.ACTION_HOLD)
        self.assertEqual((hold.status, hold.summary), (live.STATUS_OK, "Jarvis is closing - sending it now"))
        self.assertEqual(len(self.app.senders["work"].sent), 1)   # the owner already saw Sent
        self.assertEqual(self.c.live.snapshot(), ())     # closed: nothing is kept
        self.assertFalse(self.c.live.enabled)


CANCEL_LINE = ("Cancel: acct=work | event=jts0001aa | cal=primary | notify=all | title=Jarvis test sync | "
               "at=2026-10-05 14:00-15:00 | link= | body=")
PERSONAL_MOVE_LINE = ("Move: acct=personal | event=stg0001aa | cal=primary | when=2026-10-06 16:00-17:00 | "
                      "notify=all | title=Study group | at=2026-10-05 10:00-11:00 | link= | body=")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=PDT)


def roles(picture) -> list[tuple[str, int, int, int, str]]:
    """(role, column, start minute, end minute, title) of a day picture's blocks."""
    return [(block.role, block.column, block.start_min, block.end_min, block.title) for block in picture.day.blocks]


def stamp_of(step: live.StepView) -> tuple[str, str]:
    return pictures.stamp(step.picture, step.status, step.status_text)


class PictureTests(LiveAppCase):
    """The pictures of an approved card's payload: the email exactly as it goes out, a calendar
    change on its day (other events only from what Jarvis already read), their stamps."""

    def test_an_email_is_drawn_as_it_goes_out_and_stamped_sent(self) -> None:
        mail = self.add_mail()
        self.approve(mail)
        payload = step_of(self.task(), live.ACTION_PAYLOAD)
        picture = payload.picture
        self.assertEqual((picture.kind, picture.state, picture.mail.account), (pictures.KIND_OUTGOING,
                                                                              live.PICTURE_READY, "work"))
        head = picture.mail.head
        self.assertEqual((head.sender, head.to, head.cc, head.subject, head.when),
                         ("me@example.edu", TRUSTED, "", "Jarvis test sync moved", ""))
        self.assertEqual(picture.mail.body, "Hi Sam,\nMoved to Friday at 2 PM.\nThanks\n")
        self.assertEqual(picture.mail.reply_line, "New email (a new thread)")
        self.assertEqual(stamp_of(payload), ("SENDING NOW", pictures.TONE_DOING))   # its hidden hold
        self.assertTrue(payload.open_hint)
        self.run_out()
        self.wait_result(mail)
        payload = step_of(self.task(), live.ACTION_PAYLOAD)
        self.assertEqual(payload.picture.mail.head.to, TRUSTED)
        self.assertEqual(stamp_of(payload), ("SENT", pictures.TONE_DONE))
        self.assertGreater(self.c.live.picture_bytes(), 0)

    def test_undo_stamps_the_email_undone(self) -> None:
        mail = self.add_mail()
        self.approve(mail)
        self.c._send_hold.started -= 5
        self.c.undo_action(mail.id)
        settle()
        payload = step_of(self.task(), live.ACTION_PAYLOAD)
        self.assertEqual(stamp_of(payload), ("UNDONE - NOT SENT", pictures.TONE_UNDONE))
        self.assertEqual(self.app.senders["work"].sent, [])

    def check(self, line: str):
        card = ask_card(line)
        self.c._take_ask_cards([card])
        self.assertTrue(wait_for(lambda: card.id in self.c._checks, 5))
        settle()
        return card

    def test_a_work_move_nobody_read_shows_only_its_own_blocks(self) -> None:
        move = self.check(MOVE_LINE)
        self.approve(move)
        payload = step_of(self.task(), live.ACTION_PAYLOAD)
        picture = payload.picture
        self.assertEqual((picture.kind, picture.caption, picture.day.change),
                         (pictures.KIND_DAY, pictures.CAPTION_DAY, pictures.CHANGE_MOVE))
        self.assertEqual(picture.day.days, ("Mon Oct 5", "Fri Oct 9"))   # Google's time, then the new one
        self.assertEqual(roles(picture), [(pictures.ROLE_BEFORE, 0, 14 * 60, 15 * 60, "Jarvis test sync"),
                                          (pictures.ROLE_AFTER, 1, 14 * 60, 15 * 60, "Jarvis test sync")])
        self.assertFalse(picture.day.others_shown)
        self.assertEqual(picture.day.others_note,
                         "Only this event is drawn: Jarvis has not read the rest of Mon Oct 5 or Fri Oct 9")
        self.assertEqual(picture.day.account, "work")
        self.run_out()
        self.wait_result(move)
        payload = step_of(self.task(), live.ACTION_PAYLOAD)
        self.assertIs(payload.picture.kind, pictures.KIND_DAY)   # the drawn change stays, its stamp follows
        self.assertEqual(stamp_of(payload), ("MOVED", pictures.TONE_DONE))

    def test_a_move_draws_the_agendas_events_of_both_its_days(self) -> None:
        personal = self.app.calendars["personal"]
        personal.events["stg0001aa"] = ui_fakes.event("stg0001aa", "Study group", at(5, 10))
        personal.list_events = lambda *_args, **_kwargs: [
            CalendarEvent("Study group", start=at(5, 10), end=at(5, 11)),   # the event itself: only its ghost
            CalendarEvent("Office hours", start=at(5, 13), end=at(5, 14)),
            CalendarEvent("Lab session", start=at(6, 15), end=at(6, 16))]
        self.assertTrue(wait_for(lambda: self.c._agenda_job is None, 5))
        self.c._request_agenda()
        self.assertTrue(wait_for(lambda: (self.c._known_days.lookup("personal", date(2026, 10, 6)) or _NONE).events, 5))
        calls = list(personal.calls)
        move = self.check(PERSONAL_MOVE_LINE)
        self.approve(move)
        payload = step_of(self.task(), live.ACTION_PAYLOAD)
        picture = payload.picture
        self.assertEqual((picture.day.change, picture.day.account, picture.day.days),
                         (pictures.CHANGE_MOVE, "personal", ("Mon Oct 5", "Tue Oct 6")))
        self.assertEqual(roles(picture), [(pictures.ROLE_OTHER, 0, 13 * 60, 14 * 60, "Office hours"),
                                          (pictures.ROLE_OTHER, 1, 15 * 60, 16 * 60, "Lab session"),
                                          (pictures.ROLE_BEFORE, 0, 10 * 60, 11 * 60, "Study group"),
                                          (pictures.ROLE_AFTER, 1, 16 * 60, 17 * 60, "Study group")])
        self.assertTrue(picture.day.others_shown)
        self.assertEqual(picture.day.others_note, "Other events as Jarvis read them at 1:52 PM (today's agenda)")
        self.wait_result(move)
        self.assertEqual(stamp_of(step_of(self.task(), live.ACTION_PAYLOAD)), ("MOVED", pictures.TONE_DONE))
        # Drawing it listed nothing more from Google (only the card's own event check read it).
        self.assertEqual([call for call in personal.calls[len(calls):] if call[0] in ("list", "briefs")], [])

    def test_a_cancel_is_struck_through_and_undo_says_not_changed(self) -> None:
        cancel = self.check(CANCEL_LINE)
        self.approve(cancel)
        payload = step_of(self.task(), live.ACTION_PAYLOAD)
        self.assertEqual(roles(payload.picture), [(pictures.ROLE_CANCEL, 0, 14 * 60, 15 * 60, "Jarvis test sync")])
        self.assertEqual(stamp_of(payload), ("CHANGING NOW", pictures.TONE_DOING))   # its hidden hold
        self.c._send_hold.started -= 5
        self.c.undo_action(cancel.id)
        settle()
        self.assertEqual(stamp_of(step_of(self.task(), live.ACTION_PAYLOAD)),
                         ("UNDONE - NOT CHANGED", pictures.TONE_UNDONE))

    def test_a_calendar_add_draws_the_agendas_events_of_its_day(self) -> None:
        self.app.calendars["personal"].list_events = lambda *_args, **_kwargs: [
            CalendarEvent("Lab session", start=at(6, 15), end=at(6, 16)),
            CalendarEvent("Club general meeting", start=at(6, 17), end=at(6, 18)),   # already there: not drawn twice
            CalendarEvent("Dinner", start=at(7, 19), end=at(7, 20))]
        self.assertTrue(wait_for(lambda: self.c._agenda_job is None, 5))
        self.c._request_agenda()
        day = date(2026, 10, 6)
        self.assertTrue(wait_for(lambda: (self.c._known_days.lookup("personal", day) or _NONE).events, 5))
        calendar = next(action for action in self.c._actions if action.kind == "calendar")
        self.approve(calendar)
        payload = step_of(self.task(), live.ACTION_PAYLOAD)
        picture = payload.picture
        self.assertEqual((picture.day.change, picture.day.days), (pictures.CHANGE_ADD, ("Tue Oct 6",)))
        self.assertEqual(roles(picture), [(pictures.ROLE_OTHER, 0, 15 * 60, 16 * 60, "Lab session"),
                                          (pictures.ROLE_NEW, 0, 17 * 60, 18 * 60, "Club general meeting")])
        self.assertTrue(picture.day.others_shown)
        self.assertEqual(picture.day.others_note, "Other events as Jarvis read them at 1:52 PM (today's agenda)")
        self.run_out()
        self.wait_result(calendar)
        self.assertEqual(stamp_of(step_of(self.task(), live.ACTION_PAYLOAD)), ("ADDED", pictures.TONE_DONE))
        self.assertNotIn("Lab session", repr(self.c._known_days) + repr(self.c._known_days.lookup("personal", day)))


_NONE = types.SimpleNamespace(events=())


class AskReadPictureTests(unittest.TestCase):
    """A Move an Ask proposed: the day picture draws the other events that Ask's calendar read
    found (never a new Google call for it)."""

    def test_the_asks_calendar_read_gives_the_other_events(self) -> None:
        app = AppHarness(self, runs=(plan(lines=(MOVE_LINE,)),))
        app.calendars["work"].events.update({
            "std0001aa": ui_fakes.event("std0001aa", "Standup", at(5, 9)),
            "dsg0001aa": ui_fakes.event("dsg0001aa", "Design review", at(9, 10)),
            "far0001aa": ui_fakes.event("far0001aa", "Offsite", at(12, 10))})
        app.reading(autoplay=False)
        c = app.c
        c.ask.start()
        self.assertTrue(wait_for(lambda: c.ask.ready is not None, 5))
        c.window.reading.command_bar.set_text(COMMAND)
        c.window.reading.command_bar.input.returnPressed.emit()
        self.assertTrue(wait_for(lambda: not c.ask.busy, 10))
        settle()
        move = next(action for action in c._actions if action.source == SOURCE_ASK and action.kind == "move")
        self.assertTrue(wait_for(lambda: move.id in c._checks, 5))
        settle()
        lists = [call for call in app.calendars["work"].calls if call[0] in ("list", "briefs")]
        c.window.reading.action_card(move.id).approve_button.click()
        settle()
        (task,) = [view for view in c.live.snapshot() if view.kind == live.TASK_ACTION]
        picture = step_of(task, live.ACTION_PAYLOAD).picture
        self.assertEqual(roles(picture), [(pictures.ROLE_OTHER, 0, 9 * 60, 10 * 60, "Standup"),
                                          (pictures.ROLE_OTHER, 1, 10 * 60, 11 * 60, "Design review"),
                                          (pictures.ROLE_BEFORE, 0, 14 * 60, 15 * 60, "Jarvis test sync"),
                                          (pictures.ROLE_AFTER, 1, 14 * 60, 15 * 60, "Jarvis test sync")])
        self.assertEqual(picture.day.others_note, "Other events as Jarvis read them at 1:52 PM (Ask's calendar read)")
        # Drawing it read nothing more from Google.
        self.assertEqual([call for call in app.calendars["work"].calls if call[0] in ("list", "briefs")], lists)


class PicturesOffTests(LiveAppCase):
    config_extra = "\n[live]\npictures = false\n"

    def test_no_drawn_picture_and_no_read_kept(self) -> None:
        self.assertFalse(self.c.live.pictures)
        mail = self.add_mail()
        self.approve(mail)
        self.assertIsNone(step_of(self.task(), live.ACTION_PAYLOAD).picture)
        self.assertTrue(wait_for(lambda: self.c._agenda_job is None, 5))
        self.assertIsNone(self.c._known_days.lookup("personal", NOW.date()))


class BriefingTaskTests(LiveAppCase):
    def fetch(self) -> live.TaskView:
        ui.AppController._start_fetch(self.c, None)
        self.assertTrue(wait_for(lambda: self.c._fetch_stop is None, 5))
        settle()
        return self.tasks(live.TASK_BRIEFING)[-1]

    def test_a_fetch_is_one_compact_task(self) -> None:
        task = self.fetch()
        self.assertEqual((task.title, task.compact, task.status), ("Briefing from Notion", True, live.STATUS_OK))
        (step,) = task.steps
        self.assertEqual((step.kind, step.status), (live.BRIEFING_FETCH, live.STATUS_OK))
        shown = fields(step)
        self.assertGreater(int(shown["Lines"]), 0)
        self.assertEqual(shown["Proposals found"], "2")
        self.assertTrue(step.notes[0].text.startswith("Attempt 1: updated "))
        text = repr([(f.label, f.value) for f in step.fields]) + " ".join(note.text for note in step.notes)
        self.assertNotIn(self.c.config.page_id, text)                  # the page id is never shown

    def test_a_notion_error_never_shows_a_page_id(self) -> None:
        page = "0123abcd-4567-89ab-cdef-0123456789ab"
        error = NotionError(f"The briefing page was not found. Notion said: Could not find block with ID: {page}. "
                            f"Also {page.replace('-', '')}.", status=404)
        self.c._fetch_task = ui._live_fetch_task(self.c.live, None)
        self.c._live_fetch_attempt(None, error, 1)
        self.c._live_fetch_done(PollResult(None, None, error, 1, False, False))
        task = self.tasks(live.TASK_BRIEFING)[-1]
        (step,) = task.steps
        shown = " ".join([step.summary, task.summary, *(note.text for note in step.notes)])
        self.assertIn("The briefing page was not found", shown)
        self.assertNotIn(page, shown)
        self.assertNotIn(page.replace("-", ""), shown)
        self.assertIn("[id not shown]", step.notes[0].text)
        self.assertEqual((step.status, task.status), (live.STATUS_FAILED, live.STATUS_FAILED))
        self.assertEqual(ui._without_ids("event abc123def456ghi789 at 14:00"), "event abc123def456ghi789 at 14:00")

    def test_stopped_replaced_and_the_newest_three(self) -> None:
        ui.AppController._start_fetch(self.c, None)
        self.c._stop_fetch()
        self.assertEqual((self.tasks(live.TASK_BRIEFING)[0].status, self.tasks(live.TASK_BRIEFING)[0].summary),
                         (live.STATUS_CANCELLED, "stopped"))
        ui.AppController._start_fetch(self.c, "AM")
        ui.AppController._start_fetch(self.c, None)
        replaced = self.tasks(live.TASK_BRIEFING)[1]
        self.assertEqual((replaced.title, replaced.status, replaced.summary),
                         ("AM briefing from Notion", live.STATUS_CANCELLED, "replaced"))
        self.assertTrue(wait_for(lambda: self.c._fetch_stop is None, 5))
        for _ in range(2):
            self.fetch()
        self.assertEqual(len(self.tasks(live.TASK_BRIEFING)), 3)


class BackgroundTests(LiveAppCase):
    def test_agenda_claude_and_sign_in(self) -> None:
        self.assertTrue(wait_for(lambda: any(step.kind == live.AGENDA_READ and step.status == live.STATUS_OK
                                             for step in self.background().steps), 5))
        background = self.background()
        self.assertTrue(background.pinned and background.compact)
        self.assertEqual(self.c.live.snapshot()[0].id, background.id)
        agenda = step_of(background, live.AGENDA_READ)
        self.assertEqual((agenda.title, fields(agenda)["Calendars"], fields(agenda)["Events"]),
                         ("Agenda: read the personal calendar (read only)", "primary", "0"))
        self.c._request_agenda()
        self.assertTrue(wait_for(lambda: self.c._agenda_job is None, 5))
        self.assertEqual(len([step for step in self.background().steps if step.kind == live.AGENDA_READ]), 1)
        self.c.ask.start()
        self.assertTrue(wait_for(lambda: any(step.kind == live.CLAUDE_CHECK and step.status == live.STATUS_OK
                                             for step in self.background().steps), 5))
        claude = fields(step_of(self.background(), live.CLAUDE_CHECK))
        self.assertEqual(claude["Version"], "2.1.293, standalone install")
        self.assertEqual(claude["Runs left"], "20 this hour, 60 today")
        self.app.calendars["work"].signed_in = False
        self.c._start_sign_in("work")
        self.assertTrue(wait_for(lambda: self.c._connect_alias is None, 5))
        signin = step_of(self.background(), live.ACCOUNT_SIGNIN)
        self.assertEqual((signin.key, signin.status, signin.summary), ("signin:work", live.STATUS_OK, "signed in"))
        self.assertEqual(self.c.live.running(), 0)   # background reads never count as work


class EventCheckTests(LiveAppCase):
    def test_two_check_jobs_on_their_way_are_one_step_until_both_are_back(self) -> None:
        self.c._live_peeks_started(1)
        self.c._live_peeks_started(2)                  # a second job while the first runs
        step = step_of(self.background(), live.EVENT_CHECK)
        self.assertEqual((step.title, step.status), ("Checked 3 cards' events with Google (read only)",
                                                     live.STATUS_RUNNING))
        self.c._live_peeks_done({"a1": (None, None)})   # the first job is back: still running
        step = step_of(self.background(), live.EVENT_CHECK)
        self.assertEqual((step.status, len(step.items)), (live.STATUS_RUNNING, 1))
        self.c._live_peeks_done({"a2": (None, None), "a3": (None, None)})
        step = step_of(self.background(), live.EVENT_CHECK)
        self.assertEqual((step.status, step.summary, len(step.items)), (live.STATUS_WARN, "3 events checked", 3))
        self.c._live_peeks_started(1)                  # the next job: a fresh step in the same place
        (again,) = [item for item in self.background().steps if item.kind == live.EVENT_CHECK]
        self.assertEqual((again.id, again.status, again.items), (step.id, live.STATUS_RUNNING, ()))


class BackgroundOffTests(LiveAppCase):
    config_extra = "\n[live]\nbackground = false\n"

    def test_no_background_task(self) -> None:
        self.assertTrue(wait_for(lambda: self.c._agenda_job is None, 5))
        self.c.ask.start()
        self.assertTrue(wait_for(lambda: self.c.ask.ready is not None, 5))
        settle()
        self.assertEqual(self.tasks(live.TASK_BACKGROUND), [])


class LiveOffTests(LiveAppCase):
    config_extra = "\n[live]\nenabled = false\n"

    def test_nothing_is_recorded_and_nothing_changes(self) -> None:
        self.assertFalse(self.c.live.enabled)
        mail = self.add_mail()
        self.approve(mail)
        self.run_out()
        self.wait_result(mail)
        self.assertEqual(len(self.app.senders["work"].sent), 1)
        self.assertEqual(self.c._store.get(mail.id)["status"], "sent")
        self.assertEqual(self.c.live.snapshot(), ())


if __name__ == "__main__":
    unittest.main()
