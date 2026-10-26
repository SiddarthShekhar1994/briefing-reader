"""Tests for following the window Jarvis opened (briefing_reader.desktop.winwatch; SPEC 8.4, P1.9): finding it by its
process - never by a title - among the visible, uncloaked, unowned top-level windows of other processes (the one that
was not there before the open, else the foreground one), its frame from DWM, following it with WinEvent hooks filtered
to its process and thread (a drag hides the bracket and its end draws it again; a close unhooks), the DPI mapping onto
Qt's screens per monitor (a window over two monitors in two parts; one on no monitor: nothing), and the seam: every OS
call of the real one refused while the tests run, NoWinApi by default under the guard.

No real window is ever listed, measured or hooked: tests.desktop_fakes.FakeWinApi stands for Windows, and the real
seam's entry points are booby-trapped where a test calls it.
"""

from __future__ import annotations

import ctypes
import threading
import unittest
from typing import Any
from unittest import mock

from briefing_reader.desktop import backend as dbk
from briefing_reader.desktop import winwatch as ww
from briefing_reader.desktop.winwatch import Monitor, Rect, ScreenArea
from tests.desktop_fakes import DISPLAY1, DISPLAY2, EDGE_EXE, EXCEL_EXE, WORD_EXE, FakeWinApi

WORD, EXCEL, EDGE, OTHER = 0x10A, 0x20B, 0x30C, 0x40D


def watch_with(**api: Any) -> tuple[ww.WindowWatch, FakeWinApi]:
    fake = FakeWinApi(**api)
    return ww.WindowWatch(fake), fake


class SameExeTests(unittest.TestCase):
    def test_a_full_path_whole_and_a_bare_name_by_name(self) -> None:
        self.assertTrue(ww.same_exe(WORD_EXE, WORD_EXE.upper()))
        self.assertFalse(ww.same_exe(WORD_EXE, "D:\\Copies\\WINWORD.EXE"))          # another install: not it
        self.assertTrue(ww.same_exe("winword.exe", "D:\\Copies\\WINWORD.EXE"))       # a Start-menu file name
        self.assertFalse(ww.same_exe("word.exe", WORD_EXE))
        for wanted, actual in (("", WORD_EXE), (WORD_EXE, ""), ("", "")):
            self.assertFalse(ww.same_exe(wanted, actual))


class FindTests(unittest.TestCase):
    def test_the_new_window_of_the_exe_is_found_first_in_z_order(self) -> None:
        watch, fake = watch_with()
        fake.add(OTHER, pid=11, exe=EXCEL_EXE)
        fake.add(WORD, pid=22, exe=WORD_EXE, rect=(220, 112, 540, 470))
        before = watch.snapshot()
        self.assertEqual(before, frozenset({OTHER, WORD}))
        new = fake.add(0x50E, pid=22, exe=WORD_EXE, rect=(300, 160, 900, 700))
        self.assertEqual(watch.find(exe=WORD_EXE, before=before), new.hwnd)            # Word's new window
        self.assertEqual(watch.find(exe="WINWORD.EXE", before=before), new.hwnd)       # by its file name
        self.assertEqual(watch.find(pid=22, before=before), new.hwnd)                  # by its process
        self.assertEqual(watch.find(exe=EDGE_EXE, before=before), 0)                   # no such app open

    def test_a_reused_window_is_found_when_it_is_in_front(self) -> None:
        watch, fake = watch_with()
        fake.add(WORD, pid=22, exe=WORD_EXE)
        fake.add(EXCEL, pid=11, exe=EXCEL_EXE, front=True)
        before = watch.snapshot()
        self.assertEqual(watch.find(exe=WORD_EXE, before=before), 0)       # neither new nor in front: none
        fake.front = WORD
        self.assertEqual(watch.find(exe=WORD_EXE, before=before), WORD)    # it came forward with the document

    def test_only_windows_a_bracket_may_go_round(self) -> None:
        watch, fake = watch_with()
        cases = {"hidden": {"visible": False}, "minimised": {"minimised": True}, "owned": {"owned": True},
                 "tool": {"tool": True}, "cloaked": {"cloaked": True}, "tiny": {"rect": (10, 10, 20, 20)}}
        for number, (name, state) in enumerate(cases.items()):
            with self.subTest(name):
                hwnd = 0x900 + number
                rect = state.pop("rect", (100, 100, 800, 600))
                fake.add(hwnd, pid=30 + number, exe=WORD_EXE, rect=rect, front=True, **state)
                self.assertEqual(watch.find(exe=WORD_EXE), 0)
                fake.remove(hwnd)
        fake.add(0x77, pid=fake.own, exe=WORD_EXE, front=True)            # Jarvis's own process: never
        self.assertEqual(watch.find(exe=WORD_EXE), 0)
        self.assertEqual(watch.find(pid=fake.own), 0)

    def test_an_unreadable_exe_is_no_match_and_nothing_is_ever_read_by_title(self) -> None:
        watch, fake = watch_with()
        fake.add(WORD, pid=22, exe=WORD_EXE, front=True)
        fake.exe_unreadable.add(22)
        self.assertEqual(watch.find(exe=WORD_EXE), 0)
        self.assertEqual(watch.find(pid=22), WORD)                         # its pid is still known
        self.assertFalse(any("title" in name or "text" in name for name, _arg in fake.calls))
        public = [name for name in dir(ww.WinApi) if not name.startswith("_")]
        self.assertTrue(public)
        self.assertFalse(any("title" in name.casefold() or "text" in name.casefold() for name in public))

    def test_nothing_wanted_or_a_broken_seam_finds_nothing(self) -> None:
        watch, fake = watch_with()
        fake.add(WORD, pid=22, exe=WORD_EXE, front=True)
        self.assertEqual(watch.find(), 0)
        self.assertEqual(fake.calls, [])
        fake.broken.add("windows")
        with self.assertLogs("briefing_reader.desktop.winwatch", level="DEBUG") as logs:
            self.assertEqual(watch.find(exe=WORD_EXE), 0)
            self.assertEqual(watch.snapshot(), frozenset())
        text = "\n".join(logs.output)
        self.assertIn("OSError", text)
        self.assertNotIn("WINWORD", text)
        self.assertNotIn("refused", text)                                  # the type only, never its text


class BoundsTests(unittest.TestCase):
    def test_the_frame_as_drawn_and_none_for_a_window_that_does_not_show(self) -> None:
        watch, fake = watch_with()
        fake.add(WORD, pid=22, exe=WORD_EXE, rect=(220, 112, 540, 470))
        self.assertEqual(watch.bounds(WORD), Rect(220, 112, 540, 470))
        for state in ({"visible": False}, {"minimised": True}, {"cloaked": True}):
            with self.subTest(state=state):
                fake.set(WORD, **state)
                self.assertIsNone(watch.bounds(WORD))
                fake.set(WORD, visible=True, minimised=False, cloaked=False)
        fake.remove(WORD)
        self.assertIsNone(watch.bounds(WORD))                              # closed
        self.assertIsNone(watch.bounds(0))
        fake.add(WORD, pid=22, exe=WORD_EXE)
        fake.broken.add("frame_bounds")
        self.assertIsNone(watch.bounds(WORD))


class FollowTests(unittest.TestCase):
    def follow(self) -> tuple[ww.WindowWatch, FakeWinApi, ww.Follow, list[str]]:
        watch, fake = watch_with()
        fake.add(WORD, pid=22, tid=2201, exe=WORD_EXE)
        fake.add(0x10C, pid=22, tid=2201, exe=WORD_EXE)                    # another window of the same thread
        seen: list[str] = []
        follow = watch.follow(WORD, seen.append)
        self.assertIsNotNone(follow)
        return watch, fake, follow, seen

    def test_hooks_are_set_per_event_range_for_its_process_and_thread_only(self) -> None:
        watch, fake, follow, _seen = self.follow()
        self.assertEqual(sorted((low, high, pid, tid) for low, high, pid, tid, _cb in fake.hooks.values()),
                         sorted((low, high, 22, 2201) for low, high in ww.HOOK_RANGES))
        self.assertEqual(ww.HOOK_FLAGS, ww.WINEVENT_OUTOFCONTEXT | ww.WINEVENT_SKIPOWNPROCESS)
        events = {event for low, high in ww.HOOK_RANGES for event in (low, high)}
        self.assertEqual(events, {ww.EVENT_SYSTEM_FOREGROUND, ww.EVENT_SYSTEM_MOVESIZESTART,
                                  ww.EVENT_SYSTEM_MOVESIZEEND, ww.EVENT_SYSTEM_MINIMIZESTART,
                                  ww.EVENT_SYSTEM_MINIMIZEEND, ww.EVENT_OBJECT_DESTROY,
                                  ww.EVENT_OBJECT_LOCATIONCHANGE})
        self.assertTrue(follow.active())
        self.assertEqual(watch.following(), [follow])

    def test_its_own_events_come_out_as_kinds_and_a_drag_hides_until_it_ends(self) -> None:
        _watch, fake, follow, seen = self.follow()
        fake.fire(ww.EVENT_SYSTEM_FOREGROUND, WORD)
        fake.fire(ww.EVENT_SYSTEM_MOVESIZESTART, WORD)
        self.assertTrue(follow.dragging)
        fake.fire(ww.EVENT_OBJECT_LOCATIONCHANGE, WORD)                    # mid-drag: not reported
        fake.fire(ww.EVENT_OBJECT_LOCATIONCHANGE, WORD)
        fake.fire(ww.EVENT_SYSTEM_MOVESIZEEND, WORD)
        self.assertFalse(follow.dragging)
        fake.fire(ww.EVENT_OBJECT_LOCATIONCHANGE, WORD)                    # a snap or a maximise
        fake.fire(ww.EVENT_SYSTEM_MINIMIZESTART, WORD)
        fake.fire(ww.EVENT_SYSTEM_MINIMIZEEND, WORD)
        self.assertEqual(seen, [ww.FRONT, ww.DRAG, ww.DROP, ww.MOVED, ww.MINIMISED, ww.RESTORED])

    def test_other_windows_carets_and_children_are_left_out(self) -> None:
        _watch, fake, _follow, seen = self.follow()
        fake.fire(ww.EVENT_OBJECT_LOCATIONCHANGE, 0x10C)                   # another window of its thread
        fake.fire(ww.EVENT_OBJECT_LOCATIONCHANGE, WORD, object_id=-8)      # OBJID_CARET: typing in it
        fake.fire(ww.EVENT_OBJECT_LOCATIONCHANGE, WORD, child_id=3)
        fake.fire(ww.EVENT_OBJECT_DESTROY, WORD, object_id=-4)             # a menu of it went, not the window
        fake.fire(ww.EVENT_SYSTEM_FOREGROUND, 0x10C)
        self.assertEqual(seen, [])

    def test_a_window_that_closes_unhooks_and_says_so_once(self) -> None:
        watch, fake, follow, seen = self.follow()
        fake.fire(ww.EVENT_OBJECT_DESTROY, WORD)
        self.assertEqual(seen, [ww.CLOSED])
        self.assertEqual(fake.live_hooks(), 0)
        self.assertEqual(len(fake.unhooked), len(ww.HOOK_RANGES))
        self.assertFalse(follow.active())
        self.assertEqual(watch.following(), [])
        fake.fire(ww.EVENT_SYSTEM_FOREGROUND, WORD)
        self.assertEqual(seen, [ww.CLOSED])                                # nothing after the close

    def test_stop_unhooks_and_nothing_comes_after(self) -> None:
        watch, fake, follow, seen = self.follow()
        watch.stop()
        self.assertEqual(fake.live_hooks(), 0)
        self.assertFalse(follow.active())
        fake.fire(ww.EVENT_SYSTEM_MOVESIZESTART, WORD)
        self.assertEqual(seen, [])
        follow.stop()                                                      # twice: nothing more
        self.assertEqual(len(fake.unhooked), len(ww.HOOK_RANGES))

    def test_unfollowable_windows_give_none_and_leave_no_hook(self) -> None:
        watch, fake = watch_with()
        self.assertIsNone(watch.follow(0x999, lambda _kind: None))         # no such window
        fake.add(0x77, pid=fake.own, exe=WORD_EXE)
        self.assertIsNone(watch.follow(0x77, lambda _kind: None))          # Jarvis's own: never
        fake.add(WORD, pid=22, exe=WORD_EXE)
        fake.hookable = False
        self.assertIsNone(watch.follow(WORD, lambda _kind: None))          # no hook could be set: a still bracket
        fake.hookable = True
        fake.broken.add("hook")
        self.assertIsNone(watch.follow(WORD, lambda _kind: None))
        self.assertEqual(fake.live_hooks(), 0)
        self.assertEqual(watch.following(), [])

    def test_a_followers_error_is_logged_by_type_and_never_reaches_windows(self) -> None:
        watch, fake = watch_with()
        fake.add(WORD, pid=22, exe=WORD_EXE)

        def broken(_kind: str) -> None:
            raise ValueError("C:\\Users\\Owner\\secret.docx")

        watch.follow(WORD, broken)
        with self.assertLogs("briefing_reader.desktop.winwatch", level="WARNING") as logs:
            fake.fire(ww.EVENT_SYSTEM_FOREGROUND, WORD)
        text = "\n".join(logs.output)
        self.assertIn("ValueError", text)
        self.assertNotIn("secret", text)


class MapTests(unittest.TestCase):
    """SPEC 8.4's DPI rule: logical = the screen's top left + (physical - the monitor's top left) / its ratio."""

    def test_this_pc_one_screen_at_125_percent(self) -> None:
        monitors = [Monitor(DISPLAY1, Rect(0, 0, 1920, 1200))]
        screens = [ScreenArea(DISPLAY1, Rect(0, 0, 1536, 960), 1.25)]
        self.assertEqual(ww.map_rect(Rect(275, 140, 675, 588), monitors, screens), [(DISPLAY1, Rect(220, 112, 540,
                                                                                                    471))])
        self.assertEqual(ww.map_rect(Rect(0, 0, 1920, 1200), monitors, screens), [(DISPLAY1, Rect(0, 0, 1536, 960))])

    def test_one_and_one_and_a_half(self) -> None:
        for dpr in (1.0, 1.5):
            with self.subTest(dpr=dpr):
                monitors = [Monitor(DISPLAY1, Rect(0, 0, round(1280 * dpr), round(760 * dpr)))]
                screens = [ScreenArea(DISPLAY1, Rect(0, 0, 1280, 760), dpr)]
                phys = Rect(round(220 * dpr), round(112 * dpr), round(540 * dpr), round(470 * dpr))
                self.assertEqual(ww.map_rect(phys, monitors, screens), [(DISPLAY1, Rect(220, 112, 540, 470))])

    def test_a_window_over_two_monitors_comes_in_two_parts(self) -> None:
        monitors = [Monitor(DISPLAY1, Rect(0, 0, 1920, 1200)), Monitor(DISPLAY2, Rect(1920, 0, 1280, 1024))]
        screens = [ScreenArea(DISPLAY1, Rect(0, 0, 1536, 960), 1.25), ScreenArea(DISPLAY2, Rect(1536, 0, 1280, 1024),
                                                                                  1.0)]
        parts = ww.map_rect(Rect(1720, 100, 400, 300), monitors, screens)
        self.assertEqual(parts, [(DISPLAY1, Rect(1376, 80, 160, 240)), (DISPLAY2, Rect(1536, 100, 200, 300))])

    def test_no_monitor_or_no_screen_of_that_name_maps_to_nothing(self) -> None:
        monitors = [Monitor(DISPLAY1, Rect(0, 0, 1920, 1200))]
        screens = [ScreenArea(DISPLAY1, Rect(0, 0, 1920, 1200), 1.0)]
        self.assertEqual(ww.map_rect(Rect(2500, 100, 400, 300), monitors, screens), [])     # off every monitor
        self.assertEqual(ww.map_rect(Rect(100, 100, 400, 300), monitors, []), [])           # no screen known
        self.assertEqual(ww.map_rect(Rect(100, 100, 400, 300), [], screens), [])            # no monitor known
        self.assertEqual(ww.map_rect(Rect(100, 100, 400, 300), monitors,
                                     [ScreenArea(DISPLAY2, Rect(0, 0, 1920, 1200), 1.0)]), [])
        self.assertEqual(ww.map_rect(Rect(100, 100, 0, 300), monitors, screens), [])        # an empty rect

    def test_a_monitor_change_maps_anew(self) -> None:
        watch, fake = watch_with(monitors=[Monitor(DISPLAY1, Rect(0, 0, 1920, 1200)),
                                           Monitor(DISPLAY2, Rect(1920, 0, 1920, 1080))])
        fake.add(WORD, pid=22, exe=WORD_EXE, rect=(2200, 100, 800, 600))
        screens = [ScreenArea(DISPLAY1, Rect(0, 0, 1536, 960), 1.25), ScreenArea(DISPLAY2, Rect(1536, 0, 1920, 1080),
                                                                                  1.0)]
        rect = watch.bounds(WORD)
        self.assertEqual(ww.map_rect(rect, watch.monitors(), screens), [(DISPLAY2, Rect(1816, 100, 800, 600))])
        fake.monitors_now = [Monitor(DISPLAY1, Rect(0, 0, 1920, 1200))]     # the second monitor unplugged
        self.assertEqual(ww.map_rect(rect, watch.monitors(), screens), [])  # no bracket until it moves back
        fake.broken.add("monitors")
        self.assertEqual(watch.monitors(), [])


class SeamTests(unittest.TestCase):
    def test_under_the_guard_the_default_seam_does_nothing(self) -> None:
        self.assertTrue(dbk.real_backend_blocked())
        api = ww.default_api()
        self.assertIsInstance(api, ww.NoWinApi)
        watch = ww.WindowWatch()
        self.assertIsInstance(watch.api, ww.NoWinApi)
        self.assertEqual(watch.snapshot(), frozenset())
        self.assertEqual(watch.find(exe=WORD_EXE), 0)
        self.assertIsNone(watch.bounds(WORD))
        self.assertIsNone(watch.follow(WORD, lambda _kind: None))
        self.assertEqual(watch.monitors(), [])

    def test_the_real_seam_cannot_be_made_in_a_test(self) -> None:
        with mock.patch.object(ctypes, "WinDLL", side_effect=AssertionError("reached ctypes.WinDLL")):
            with self.assertRaises(dbk.RealBackendInTests):
                ww.Win32WinApi()

    def test_every_real_method_is_guarded_and_refused(self) -> None:
        methods = {name for name, value in vars(ww.Win32WinApi).items() if callable(value) and not name.startswith("_")}
        protocol = {name for name, value in vars(ww.WinApi).items() if callable(value) and not name.startswith("_")}
        self.assertEqual(methods, protocol)
        self.assertLessEqual(methods, {name for name in dir(FakeWinApi) if not name.startswith("_")})
        self.assertLessEqual(methods, {name for name in dir(ww.NoWinApi) if not name.startswith("_")})
        arguments = {"windows": (), "window_process": (WORD,), "process_exe": (22,), "visible": (WORD,),
                     "minimised": (WORD,), "owned": (WORD,), "tool_window": (WORD,), "cloaked": (WORD,),
                     "frame_bounds": (WORD,), "is_window": (WORD,), "foreground": (), "monitors": (),
                     "hook": (1, 2, 22, 2201, lambda *_a: None), "unhook": (901,), "own_pid": ()}
        self.assertEqual(set(arguments), methods)
        seam = object.__new__(ww.Win32WinApi)                              # its constructor never ran
        caller = threading.current_thread()
        reached: list[str] = []
        original = ctypes.WinDLL

        def trapped(*args: Any, **kwargs: Any) -> Any:
            if threading.current_thread() is caller:
                reached.append("ctypes.WinDLL")
                raise AssertionError("the real seam reached ctypes")
            return original(*args, **kwargs)

        with mock.patch.object(ctypes, "WinDLL", trapped):
            for name in sorted(methods):
                with self.subTest(method=name):
                    self.assertTrue(getattr(getattr(ww.Win32WinApi, name), "reaches_os", False))
                    with self.assertRaises(dbk.RealBackendInTests):
                        getattr(seam, name)(*arguments[name])
                    with self.assertRaises(dbk.RealBackendInTests):
                        getattr(ww.Win32WinApi, name)(None, *arguments[name])      # called unbound, too
        self.assertEqual(reached, [])


if __name__ == "__main__":
    unittest.main()
