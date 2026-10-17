"""The follow-up list in the app (spec 3.4, 9.3-9.5; AppHarness, fake readers, FakeRunner).

FOLLOW-UPS fills from Jarvis's own header scan 20 s after the reading screen came up (an own-reads
LIVE step also with [live] background = false, R6); a row's menu drafts a reply (a NUDGE task, a
"Nudge - reply" card with a NEW recipient); a sent reply turns its row "replied - checking" and the
thread's headers are read again at once (R47); a denied draft can be drafted again (R15); a reply
card already waiting is shown instead of a new run, without moving the focus (R31); "Tell Jarvis
what to say" replaces only an unedited draft (R31) and answers a draft's question (R46); a guard stop
re-fetches the row (R47); a brief's "owes you an answer" drafts a nudge (R32); typed "follow-ups" for
every state (R45); hide, show hidden, turn off and on, rescan; nothing of the list in a log, an
ACTIVITY row or anything spoken. Qt offscreen; every name, subject and address is invented."""

from __future__ import annotations

import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from PySide6.QtWidgets import QApplication

from briefing_reader import hud, live, persona, ui
from briefing_reader.ask.modes import MODE_NUDGE, ThreadTarget
from briefing_reader.gmail import MailReadError
from tests.ask_fakes import FakeScanReader, gmail_message, gmail_thread, plan_stream
from tests.ui_fakes import NOW, AppHarness, fonts, settle, wait_for

_app: QApplication | None = None
OWN = "me@example.edu"
ANA = "ana.fu@example.org"
BEN = "ben.fu@example.org"
SUBJECT = "Zq7subj Lab budget"
REPLY = (f"Reply: acct=work | thread=thrwait001 | msgid=<CAW1@mail.example.org> | gmid= | to={ANA} | cc= | "
         f"subject=Re: {SUBJECT} | replied=no | due= | link= | body=Hi Ana,\\nConfirmed.\\nThanks")
SAY = "I drafted a short answer."
SECRETS = ("Zq7", "zq7", "Ana Fu", ANA, "Ben Fu", BEN, "thrwait001", "throwed001", "Lab budget")


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_followups_app", "-platform", "offscreen"])
    fonts()


def at_ms(days: float) -> int:
    return int((NOW - timedelta(days=days)).timestamp() * 1000)


def waiting_thread(*more: dict) -> dict:
    return gmail_thread("thrwait001", [gmail_message(
        "msgwait001", sender=f"Ana Fu <{ANA}>", to=OWN, subject=SUBJECT, body="Can you confirm the lab budget?",
        msgid="<CAW1@mail.example.org>", at_ms=at_ms(3)), *more])


def owed_thread() -> dict:
    return gmail_thread("throwed001", [gmail_message(
        "msgowed001", sender=OWN, to=f"Ben Fu <{BEN}>", subject="Zq7subj Room booking", body="Is room 2 free?",
        msgid="<CAO1@mail.example.edu>", at_ms=at_ms(5), labels=("SENT",))])


def draft(question: str = "", line: str = REPLY) -> list[str]:
    return plan_stream({"say": SAY, "question": question, "lines": [line]})


class FollowupsAppCase(unittest.TestCase):
    def make(self, *runs, reader: FakeScanReader | None = None, extra: str = "", lines=None) -> AppHarness:
        threads = {"thrwait001": waiting_thread(), "throwed001": owed_thread()}
        self.reader = reader or FakeScanReader("work", threads, meta=dict(threads), own=(OWN,), confirmed=OWN)
        kwargs = {"lines": lines} if lines is not None else {}
        app = AppHarness(self, runs=runs, readers={"work": self.reader}, productivity=True, prod_extra=extra,
                         **kwargs)
        app.reading(autoplay=False)
        app.c.ask.start()
        self.assertTrue(wait_for(lambda: app.c.ask.ready is not None), "readiness checked")
        self.app, self.c, self.reading = app, app.c, app.c.window.reading
        self.panel = self.reading.followups_stage          # the Follow-ups screen (round 2, part 2b)
        return app

    def scan(self) -> None:
        self.app.clock.advance(seconds=25)
        self.c.productivity.tick()
        settle()

    def rows(self) -> list[hud.FollowupRowInfo]:
        return [row.info() for row in self.panel.followup_rows()]

    def row(self, kind: str = "waiting") -> hud.FollowupRowInfo:
        return next(info for info in self.rows() if info.kind == kind)

    def menu(self, row_id: str) -> dict:
        self.c._on_followup_activated(row_id)
        menu = self.c._followup_menu
        self.assertIsNotNone(menu)
        found = {action.text(): action for action in menu.actions() if action.text()}
        menu.close()
        return found

    def entries(self) -> list:
        return self.reading.conversation.entry_widgets()

    def wait_run(self) -> None:
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10), "the draft ran")
        settle()

    def nudge_cards(self) -> list:
        return [action for action in self.c._actions if action.mode == MODE_NUDGE]

    def send(self, card_id: str) -> None:
        self.reading.action_card(card_id).approve_button.click()
        settle()
        self.assertIsNotNone(self.c._send_hold)
        self.c._send_hold.deadline = time.monotonic() - 0.001
        self.c._on_send_hold_tick()


class ScanAndDraftTests(FollowupsAppCase):
    def test_the_list_a_draft_and_a_sent_reply(self) -> None:
        app = self.make(draft(), extra="", )
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            self.assertEqual(self.rows(), [])                      # nothing before 20 s
            self.assertEqual(self.reader.lists, [])
            self.scan()
            rows = self.rows()
            self.assertEqual([(info.kind, info.who, info.age) for info in rows],
                             [("waiting", "Ana Fu", "3d"), ("owed", "Ben Fu", "5d")])
            self.assertTrue(self.panel.followups_meta.text().startswith(
                f"1 waiting {hud.MIDDLE_DOT} 1 owed {hud.MIDDLE_DOT} updated "), self.panel.followups_meta.text())
            self.assertEqual(self.reading.activity.entries()[0][2], "Follow-ups: 1 waiting, 1 owed")
            items = self.menu(self.row().row_id)
            self.assertEqual(list(items), [ui.FOLLOWUP_MENU_DRAFT["waiting"], ui.FOLLOWUP_MENU_WORDS,
                                           ui.FOLLOWUP_MENU_OPEN, ui.FOLLOWUP_MENU_HIDE_DAYS,
                                           ui.FOLLOWUP_MENU_HIDE_NEW])
            items[ui.FOLLOWUP_MENU_DRAFT["waiting"]].trigger()
            self.wait_run()
            card, = self.nudge_cards()
            self.assertEqual(card.unverified, frozenset({ANA}))   # NEW recipient: never typed
            widget = self.reading.action_card(card.id)
            self.assertEqual(widget.kind_label.full_text(), f"NUDGE {hud.MIDDLE_DOT} REPLY {hud.MIDDLE_DOT} WORK")
            entry = self.entries()[-1]
            self.assertEqual(entry.entry[2], persona.nudge_drafted_text(SUBJECT, "work"))
            self.assertEqual(app.voice.texts()[-1], "I've drafted a reply; it's waiting for your OK, sir.")
            self.assertEqual(self.reader.reads, ["thrwait001"])  # the one full read
            self.assertEqual(self.reading.command_bar.text(), "")
            self.assertEqual(self.row().state, hud.FOLLOWUP_DRAFT_WAITING)   # its draft waits now (R31)
            # Send (the NEW recipient ticked: as the Edit dialog's tick does, Jarvis knows the address now).
            self.c._history.add([ANA])
            self.c._refresh_actions_ui()
            settle()
            self.reader.meta["thrwait001"] = waiting_thread(gmail_message(
                "msgwait002", sender=OWN, to=ANA, subject=f"Re: {SUBJECT}", body="Confirmed.",
                msgid="<CAW2@mail.example.edu>", at_ms=at_ms(0), labels=("SENT",)))
            states = []
            original = self.reader.thread_metadata

            def watched(thread_id: str) -> dict:
                row = self.panel.followup_row(self.rows()[0].row_id)
                states.append(row.info().state if row is not None else "")
                return original(thread_id)

            self.reader.thread_metadata = watched
            self.send(card.id)
            self.assertTrue(wait_for(lambda: card.id not in self.c._jobs and app.senders["work"].sent, 10))
            settle()
        self.assertEqual(states, [hud.FOLLOWUP_CHECKING])          # "replied - checking" while it is read again
        self.assertEqual([info.kind for info in self.rows()], ["owed"])   # the answered row is gone (R47)
        self.assertEqual(app.voice.texts()[-1], "Sent, sir - your reply is on its way.")   # no subject (R27)
        for said in app.voice.texts():
            for secret in SECRETS:
                self.assertNotIn(secret, said)
        text = "\n".join(logs.output)
        rows = "\n".join(" ".join(row) for row in self.reading.activity.entries())
        for secret in SECRETS:
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, rows)
        self.assertIn("Follow-ups: a thread checked again (sent)", text)
        task = next(task for task in self.c.live.snapshot() if task.kind == live.TASK_NUDGE)
        self.assertEqual([step.kind for step in task.steps][:2], [live.MODE_REQUEST, live.ASK_CHECKS])

    def test_the_scan_is_its_own_read_with_background_off(self) -> None:
        self.make(extra="\n[live]\nbackground = false\n")
        self.scan()
        reads = [task for task in self.c.live.snapshot() if task.kind == live.TASK_OWN_READS]
        self.assertTrue(reads and any(step.kind == live.MAIL_SCAN for step in reads[0].steps))   # R6
        self.assertFalse([task for task in self.c.live.snapshot() if task.kind == live.TASK_BACKGROUND])

    def test_deny_then_draft_again_reopens_the_denied_draft(self) -> None:
        self.make(draft(), draft(line=REPLY.replace("Confirmed.", "Confirmed again.")))
        self.scan()
        self.menu(self.row().row_id)[ui.FOLLOWUP_MENU_DRAFT["waiting"]].trigger()
        self.wait_run()
        card, = self.nudge_cards()
        self.c.edit_action(card.id)                                # the owner edits the draft, then denies it
        dialog = self.c._dialog
        self.assertIsNotNone(dialog)
        dialog.body_edit.setPlainText("Zq7 my own words")
        dialog._on_save()
        settle()
        self.assertTrue(self.c._is_edited(self.c._action(card.id)))
        self.c.deny_action(card.id)
        settle()
        self.assertTrue(self.c._store.is_decided(card.id))
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            self.menu(self.row().row_id)[ui.FOLLOWUP_MENU_DRAFT["waiting"]].trigger()
            self.wait_run()
        self.assertIn("Nudge: a denied draft was replaced", "\n".join(logs.output))
        self.assertFalse(self.c._store.is_decided(card.id))       # a live card again (R15)
        self.assertEqual(self.reading.action_card(card.id).approve_text(), "Send")
        page = self.c._action(card.id)
        self.assertFalse(self.c._is_edited(page))                  # the denied card's Edit is gone with it
        self.assertIn("Confirmed again.", self.c._effective(page).body)
        self.assertNotIn("Zq7", self.c._effective(page).body)
        self.assertEqual(self.reading.action_card(card.id).kind_label.full_text(),
                         f"NUDGE {hud.MIDDLE_DOT} REPLY {hud.MIDDLE_DOT} WORK")   # not marked EDITED

    def test_a_sign_in_started_during_a_scan_cancels_it(self) -> None:
        """R19: AppController._start_sign_in stops a running header scan between two reads."""
        self.make()
        original = self.reader.thread_metadata
        jobs: list = []

        def read(thread_id: str) -> dict:
            found = original(thread_id)
            if not jobs:
                jobs.append(self.c.productivity._scan_job)
                self.c._start_sign_in("work")                      # e.g. Sign in on a card meanwhile
            return found

        self.reader.thread_metadata = read
        self.scan()
        self.assertTrue(jobs and jobs[0] is not None and jobs[0].cancel.is_set())
        report, = self.c.productivity.followups().reports
        self.assertEqual((report.partial, report.problem, len(self.reader.gets)), (True, "cancelled", 1))
        self.assertTrue(wait_for(lambda: self.c._connect_alias is None, 5), "the sign-in ended")

    def test_a_waiting_briefing_reply_is_shown_instead_of_a_new_run(self) -> None:
        line = (f"Reply: acct=work | thread=thrwait001 | msgid=<CAW1@mail.example.org> | gmid= | to={ANA} | cc= | "
                f"subject=Re: {SUBJECT} | replied=no | due= | link= | body=Thanks")
        app = self.make(lines=(line,))
        self.scan()
        info = self.row()
        self.assertEqual((info.state, info.state_text), (hud.FOLLOWUP_DRAFT_WAITING, "draft waiting"))
        items = self.menu(info.row_id)
        self.assertIn(ui.FOLLOWUP_MENU_SHOW, items)
        self.assertNotIn(ui.FOLLOWUP_MENU_DRAFT["waiting"], items)
        self.reading.command_bar.input.setFocus()
        items[ui.FOLLOWUP_MENU_SHOW].trigger()
        settle()
        card = self.reading.approvals.cards()[0]
        self.assertTrue(card.flashing())
        self.assertFalse(card.approve_button.hasFocus())           # the focus never moves to Send (R1)
        self.c._draft_row(info.row_id)                             # Draft anyway (a stale menu): refused
        settle()
        self.assertEqual(self.entries()[-1].entry[2], persona.nudge_waiting_text("sir"))
        self.assertEqual(app.factory.runner.started, [])

    def test_tell_jarvis_replaces_an_unedited_draft_and_answers_a_question(self) -> None:
        second = REPLY.replace("Confirmed.", "Confirmed, numbers attached Friday.")
        self.make(draft(question="Which date should I give?"), draft(line=second), draft(line=second))
        self.scan()
        row_id = self.row().row_id
        self.menu(row_id)[ui.FOLLOWUP_MENU_DRAFT["waiting"]].trigger()
        self.wait_run()
        question = self.entries()[-1]
        self.assertEqual(question.entry[2], "Question: Which date should I give?")
        self.assertEqual(question.link()[0], "Tell Jarvis what to say")
        self.c._on_conversation_link(question.link()[1])
        dialog = self.c._nudge_dialog
        self.assertIsInstance(dialog, hud.NudgeWordsDialog)           # R46
        dialog.input.setText("say Friday")
        dialog.ok_button.click()
        self.wait_run()
        cards = self.nudge_cards()
        self.assertEqual(len(cards), 1)
        self.assertIn("Friday", cards[0].body)                         # the unedited draft was replaced (R31)
        self.assertIn("The owner's words: say Friday", self.app.factory.runner.started[-1].stdin)
        self.assertEqual(self.reading.command_bar.text(), "")          # nothing bound to the bar
        with mock.patch.object(self.c, "_is_edited", return_value=True):
            self.menu(row_id)[ui.FOLLOWUP_MENU_WORDS].trigger()
            self.c._nudge_dialog.input.setText("say Monday")
            self.c._nudge_dialog.ok_button.click()
            settle()
        self.assertEqual(len(self.app.factory.runner.started), 2)      # an edited draft is never replaced
        self.assertEqual(self.entries()[-1].entry[2], persona.nudge_waiting_text("sir"))

    def test_a_guard_stop_reads_the_row_again(self) -> None:
        app = self.make()
        self.scan()
        info = self.row()
        newer = gmail_message("msgwait003", sender=OWN, to=ANA, subject=f"Re: {SUBJECT}", body="Done.",
                              msgid="<CAW3@mail.example.edu>", at_ms=at_ms(0.5), labels=("SENT",))
        self.reader.threads["thrwait001"] = waiting_thread(newer)
        self.reader.meta["thrwait001"] = waiting_thread(newer)
        gets = len(self.reader.gets)
        self.menu(info.row_id)[ui.FOLLOWUP_MENU_DRAFT["waiting"]].trigger()
        self.wait_run()
        self.assertEqual(self.entries()[-1].entry[2], "You've replied since, sir - nothing to draft. I'm updating "
                                                      "the list.")
        self.assertEqual(app.factory.runner.started, [])
        self.assertEqual(len(self.reader.gets), gets + 1)            # re-fetched at once, no Rescan (R47)
        self.assertEqual([row.kind for row in self.rows()], ["owed"])

    def test_a_brief_s_owes_you_an_answer_drafts_a_nudge(self) -> None:
        line = (f"Reply: acct=work | thread=throwed001 | msgid=<CAO1@mail.example.edu> | gmid= | to={BEN} | cc= | "
                f"subject=Re: Zq7subj Room booking | replied=no | due= | link= | body=Hi Ben,\\nAny news?\\nThanks")
        app = self.make(draft(line=line))
        self.scan()
        target = ThreadTarget("work", "throwed001", "owed", "msgowed001")
        self.c._brief_nudge_entries(SimpleNamespace(nudges=(("Ben Fu", "Zq7subj Room booking", "Sep 29", target),)))
        entry = self.entries()[-1]
        self.assertEqual(entry.entry[2], 'Ben Fu owes you an answer in "Zq7subj Room booking" (Sep 29).')
        self.assertEqual(entry.link()[0], "Draft a nudge")
        self.c._on_conversation_link(entry.link()[1])
        self.wait_run()
        self.assertEqual(len(app.factory.runner.started), 1)
        self.assertIn("kind=owed to you", app.factory.runner.started[0].stdin)
        task = next(task for task in self.c.live.snapshot() if task.kind == live.TASK_NUDGE)
        self.assertEqual(task.tag, "NUDGE")


class TypedAndMenuTests(FollowupsAppCase):
    def typed(self) -> object:
        bar = self.reading.command_bar
        bar.set_text("any follow-ups?")
        bar.input.returnPressed.emit()
        settle()
        return self.entries()[-1]

    def test_typed_followups_for_every_state(self) -> None:
        self.make()
        entry = self.typed()
        self.assertEqual(entry.entry[2], persona.followups_scanning_text("sir"))   # no scan yet: one starts
        self.assertEqual(self.app.factory.runner.started, [])
        entry = self.typed()
        self.assertTrue(entry.entry[2].startswith("1 thread waits on your answer and 1 on someone else's (checked "))
        self.assertEqual(entry.link()[0], "Show follow-ups")
        self.c._on_conversation_link(entry.link()[1])
        self.c.productivity.set_off(True)
        settle()
        entry = self.typed()
        self.assertEqual((entry.entry[2], entry.link()[0]), ("Follow-ups are off on this PC.", "Turn on"))
        self.assertEqual(self.panel.followup_notes(), ["Follow-ups are off on this PC"])
        self.c._on_conversation_link(entry.link()[1])
        settle()
        self.assertFalse(self.c.productivity.followups().off)

    def test_typed_followups_when_nothing_could_be_checked(self) -> None:
        """R45: never "nothing waits" (nor "checking now") when no account was or can be read."""
        sign_in = "Sign in again so Jarvis can read work mail."
        reader = FakeScanReader("work", {}, meta={}, confirmed="", problem_word="identity")
        self.make(reader=reader)
        self.scan()
        entry = self.typed()
        self.assertEqual((entry.entry[2], entry.link()[0]), (sign_in, "Sign in"))
        with mock.patch.object(self.c, "sign_in_for_mail") as signs_in:
            self.c._on_conversation_link(entry.link()[1])
        signs_in.assert_called_once_with("work")
        reader.confirmed = OWN
        reader.errors = {"list:SENT": MailReadError("no", status=403, problem="failed")}
        self.c.productivity.request_scan("test")
        settle()
        self.assertEqual(self.c.productivity.followups().notes, (("work", "refused"),))
        self.assertEqual(self.typed().entry[2], sign_in)
        self.c.productivity.request_scan("test")                       # stopped until the next sign-in
        settle()
        self.assertEqual(self.c.productivity.followups().notes, (("work", "stopped"),))
        self.assertEqual(self.typed().entry[2], sign_in)
        self.assertIs(self.c.productivity.config.ask, self.c.config.ask)
        object.__setattr__(self.c.config.ask, "read_mail", False)     # [ask] read_mail = false (a frozen config)
        lists = len(reader.lists)
        for _again in range(2):
            entry = self.typed()
            self.assertEqual((entry.entry[2], entry.link()[0]), ("Reading mail is off ([ask] read_mail).", ""))
        self.assertEqual(len(reader.lists), lists)                     # no scan asked for
        self.assertEqual(self.app.factory.runner.started, [])

    def test_typed_followups_while_a_sign_in_is_open(self) -> None:
        self.make()
        self.c._connect_alias = "work"                                 # a Google sign-in in the browser
        try:
            self.assertEqual(self.typed().entry[2], ui.ASK_SIGN_IN_FIRST + ".")
            self.assertEqual(self.reader.lists, [])                    # no scan meanwhile (R19)
        finally:
            self.c._connect_alias = None
        self.assertEqual(self.typed().entry[2], persona.followups_scanning_text("sir"))
        self.assertEqual(len(self.reader.lists), 2)

    def test_unconfirmed_none_and_partial(self) -> None:
        reader = FakeScanReader("work", {}, meta={}, confirmed="", problem_word="unconfirmed")
        self.make(reader=reader)
        self.scan()
        entry = self.typed()
        self.assertEqual((entry.entry[2], entry.link()[0]), ('Confirm which Google account "work" is first.',
                                                             "Confirm"))
        self.assertEqual(self.panel.followup_notes(), ['Confirm which Google account "work" is first'])
        with mock.patch.object(self.c, "_ask_account", return_value=True) as asked:
            self.c._on_conversation_link(entry.link()[1])
        asked.assert_called_once_with("work")
        reader.confirmed = OWN
        self.c.productivity.request_scan("test")
        settle()
        self.assertEqual(self.typed().entry[2][:43], "Nothing waits on you or on anyone else, sir")
        reader.errors = {"list:INBOX": MailReadError("slow", status=429)}
        self.c.productivity.request_scan("test")
        settle()
        self.assertIn("(partial - Gmail asked me to slow down).", self.typed().entry[2])
        self.assertEqual(self.panel.followups_meta.text(), "partial - Gmail asked Jarvis to slow down")

    def test_hide_show_hidden_unhide_turn_off_and_rescan(self) -> None:
        self.make()
        self.scan()
        waiting = self.row()
        self.menu(waiting.row_id)[ui.FOLLOWUP_MENU_HIDE_DAYS].trigger()
        settle()
        self.assertEqual([row.kind for row in self.rows()], ["owed"])
        self.menu(self.row("owed").row_id)[ui.FOLLOWUP_MENU_HIDE_NEW].trigger()
        settle()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.panel.followups_empty.text(), "Nothing waits on you")
        self.c._on_followups_menu()
        menu = self.c._followup_menu
        titles = [action.text() for action in menu.actions() if action.text()]
        self.assertEqual(titles, ["Rescan", "Show hidden (2)", "Turn off on this PC"])
        hidden = next(action.menu() for action in menu.actions() if action.text() == "Show hidden (2)")
        unhide = [action for action in hidden.actions() if action.text().startswith("Unhide: Ana Fu")]
        unhide[0].trigger()
        settle()
        self.assertEqual([row.kind for row in self.rows()], ["waiting"])
        lists = len(self.reader.lists)
        self.c._on_followups_menu()
        next(action for action in self.c._followup_menu.actions() if action.text() == "Rescan").trigger()
        settle()
        self.assertEqual(len(self.reader.lists), lists + 2)
        self.assertEqual([row.kind for row in self.rows()], ["waiting"])   # the owed row stays hidden
        self.c._on_followups_menu()
        next(action for action in self.c._followup_menu.actions() if action.text() == "Turn off on this PC").trigger()
        settle()
        self.assertTrue(self.c._prefs_store().followups_off)
        self.assertEqual(self.panel.followups_meta.text(), "off")
        self.c._on_followups_link(ui.FOLLOWUPS_ON)
        settle()
        self.assertFalse(self.c._prefs_store().followups_off)

    def test_off_without_the_bundle_or_a_mail_reader(self) -> None:
        app = AppHarness(self, readers={"work": FakeScanReader("work", {}, meta={})})
        app.reading(autoplay=False)
        self.assertFalse(app.c.window.reading.followups_stage.followups_visible())
        self.assertFalse(app.c.window.reading.followups_available())


if __name__ == "__main__":
    unittest.main()
