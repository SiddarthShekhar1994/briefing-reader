"""Tests for briefing_reader.ask.routes: the typed shortcuts that are not an Ask (this build: memory).

Every name and note is invented.
"""

from __future__ import annotations

import unittest

from briefing_reader.ask.routes import (
    MEMORY_ROUTES,
    ROUTE_MEMORY_FORGET,
    ROUTE_MEMORY_SAVE,
    ROUTE_MEMORY_SHOW,
    ROUTE_NONE,
    Route,
    RouteFlags,
    route,
)

ON = RouteFlags(memory=True)


class MemoryRouteTests(unittest.TestCase):
    def test_saves(self) -> None:
        cases = {
            "remember Ana is my project lead": "Ana is my project lead",
            "remember that Ana is my project lead": "Ana is my project lead",
            "jarvis, remember: I prefer mornings": "I prefer mornings",
            "Jarvis remember, gym on Tuesdays": "gym on Tuesdays",
            "please remember that Ben is the lab manager.": "Ben is the lab manager.",
            "REMEMBER THAT the lab opens at 9!": "the lab opens at 9!",
            "remember   that   Ana  leads": "Ana leads",
            "remember that that report is due Friday": "that report is due Friday",
            "remember the milk is in the fridge": "the milk is in the fridge",
        }
        for text, note in cases.items():
            with self.subTest(text=text):
                self.assertEqual(route(text, ON), Route(ROUTE_MEMORY_SAVE, note))

    def test_the_that_form_wins(self) -> None:
        found = route("remember that Ana is my project lead", ON)
        self.assertFalse(found.payload.casefold().startswith("that "))

    def test_never_a_note(self) -> None:
        for text in ("remember to call Ana", "remember when we met?", "remember what Ana said",
                     "remember that meeting we had?", "remember who organizes the sync",
                     "remember how to file the report", "remember whether Ana replied",
                     "remember if Ben said yes", "remember why we moved it", "remember where the lab is",
                     "remember which room it was", "remember", "remember that", "remember:", "remember:   ",
                     "web: remember that x", "do you remember the address?", "I remember Ana",
                     "remember Ana's number?"):
            with self.subTest(text=text):
                self.assertEqual(route(text, ON).kind, ROUTE_NONE)

    def test_forget(self) -> None:
        for text, words in (("forget that Ana is my lead", "Ana is my lead"), ("forget about Ana", "Ana"),
                            ("forget: gym", "gym"), ("Jarvis, please forget about the lab.", "the lab.")):
            with self.subTest(text=text):
                self.assertEqual(route(text, ON), Route(ROUTE_MEMORY_FORGET, words))
        for text in ("forget it", "forget", "forget that", "don't forget the report", "forget about"):
            with self.subTest(text=text):
                self.assertEqual(route(text, ON).kind, ROUTE_NONE)

    def test_show(self) -> None:
        for text in ("what do you remember?", "What do you remember", "show my memory", "show me my memory",
                     "show memory", "open memory", "jarvis, open memory.", "please show me memory!"):
            with self.subTest(text=text):
                self.assertEqual(route(text, ON), Route(ROUTE_MEMORY_SHOW))
        for text in ("what do you remember about Ana?", "show my memory of Friday", "open memory.json"):
            with self.subTest(text=text):
                self.assertEqual(route(text, ON).kind, ROUTE_NONE)

    def test_the_spec_must_not_match_list(self) -> None:
        for text in ("remember to call Ana", "remember when we met?", "remember what Ana said",
                     "remember that meeting we had?", "web: remember that x", "planet news",
                     "plan my day and email Ana", "prepare the report", "brief me on the news", "forget it"):
            with self.subTest(text=text):
                self.assertNotIn(route(text, ON).kind, MEMORY_ROUTES)

    def test_flags_off_is_a_normal_ask(self) -> None:
        for text in ("remember that Ana is my project lead", "forget about Ana", "what do you remember?"):
            with self.subTest(text=text):
                self.assertEqual(route(text, RouteFlags()).kind, ROUTE_NONE)
                self.assertEqual(route(text, RouteFlags(plan=True, prep=True, followups=True)).kind, ROUTE_NONE)
        self.assertEqual(route("", ON).kind, ROUTE_NONE)

    def test_repr_has_no_words(self) -> None:
        found = route("remember that Zq7 is my lead", ON)
        self.assertEqual(repr(found), "Route(memory_save)")
        self.assertNotIn("Zq7", repr(found))


if __name__ == "__main__":
    unittest.main()
