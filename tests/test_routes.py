"""Tests for briefing_reader.ask.routes: the typed shortcuts that are not an Ask (memory, the day plan
and the meeting brief).

Every name and note is invented.
"""

from __future__ import annotations

import unittest

from briefing_reader.ask.routes import (
    MEMORY_ROUTES,
    PART_AFTERNOON,
    PART_DAY,
    PART_EVENING,
    PART_MORNING,
    PART_TOMORROW,
    ROUTE_MEMORY_FORGET,
    ROUTE_MEMORY_SAVE,
    ROUTE_MEMORY_SHOW,
    ROUTE_NONE,
    ROUTE_PLAN,
    ROUTE_PREP,
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


PLAN_ON = RouteFlags(plan=True)
BOTH = RouteFlags(memory=True, plan=True)


class PlanRouteTests(unittest.TestCase):
    def test_plan_variants(self) -> None:
        cases = {
            "plan my day": (PART_DAY, ""), "plan today": (PART_DAY, ""), "Plan the rest of my day": (PART_DAY, ""),
            "plan the rest of today.": (PART_DAY, ""), "plan tomorrow": (PART_TOMORROW, ""),
            "plan my day tomorrow": (PART_TOMORROW, ""), "plan my morning": (PART_MORNING, ""),
            "plan my afternoon please": (PART_AFTERNOON, ""), "please plan my evening for me": (PART_EVENING, ""),
            "jarvis, plan my day!": (PART_DAY, ""),
            "plan my day, keep the afternoon free": (PART_DAY, "keep the afternoon free"),
            "plan my day: gym and reading": (PART_DAY, "gym and reading"),
            "plan tomorrow; lab report first": (PART_TOMORROW, "lab report first"),
            "PLAN MY AFTERNOON for me , gym": (PART_AFTERNOON, "gym"),
        }
        for text, (part, extra) in cases.items():
            with self.subTest(text=text):
                self.assertEqual(route(text, PLAN_ON), Route(ROUTE_PLAN, extra, part))
                self.assertEqual(route(text, BOTH).kind, ROUTE_PLAN)

    def test_never_a_plan(self) -> None:
        for text in ("planet news", "plan my day and email Ana", "plan a trip to Lisbon", "plan my day,",
                     "can you plan my day", "plan my week", "web: plan my day", "plan", "planning my day"):
            with self.subTest(text=text):
                self.assertEqual(route(text, PLAN_ON).kind, ROUTE_NONE)

    def test_the_flag_turns_it_off(self) -> None:
        self.assertEqual(route("plan my day", ON).kind, ROUTE_NONE)   # memory only
        self.assertEqual(route("plan my day", RouteFlags()).kind, ROUTE_NONE)

    def test_memory_still_wins_its_own_words(self) -> None:
        self.assertEqual(route("remember that I plan my day at 8", BOTH).kind, ROUTE_MEMORY_SAVE)
        self.assertEqual(repr(route("plan my day, Zq7", BOTH)), "Route(plan)")

    def test_prep_words_never_route_with_plan_alone(self) -> None:
        self.assertEqual(route("prep me for my 2 PM", PLAN_ON).kind, ROUTE_NONE)

    def test_part_words_match_dayplan(self) -> None:
        from briefing_reader.ask import dayplan

        self.assertEqual((PART_DAY, PART_MORNING, PART_AFTERNOON, PART_EVENING, PART_TOMORROW),
                         (dayplan.PART_DAY, dayplan.PART_MORNING, dayplan.PART_AFTERNOON, dayplan.PART_EVENING,
                          dayplan.PART_TOMORROW))


PREP_ON = RouteFlags(prep=True)
ALL_ON = RouteFlags(memory=True, plan=True, prep=True)


class PrepRouteTests(unittest.TestCase):
    def test_prep_variants(self) -> None:
        cases = {
            "prep me": "", "prepare me": "", "meeting prep": "", "jarvis, prep me!": "", "please prep me.": "",
            "prep me for my 2 PM": "my 2 PM", "prep me for my 2 PM.": "my 2 PM",
            "prepare me for Project sync": "Project sync", "prep me on the budget review": "the budget review",
            "prep me for my call with Ben": "my call with Ben", "brief me on my next meeting": "my next meeting",
            "brief me for the call": "the call", "brief me on my 2 pm": "my 2 pm",
            "brief me on my 14:00 meeting": "my 14:00 meeting", "Brief me on the next call": "the next call",
            "PREP ME FOR TOMORROW'S SYNC": "TOMORROW'S SYNC",
        }
        for text, target in cases.items():
            with self.subTest(text=text):
                self.assertEqual(route(text, PREP_ON), Route(ROUTE_PREP, target))
                self.assertEqual(route(text, ALL_ON).kind, ROUTE_PREP)

    def test_never_a_prep(self) -> None:
        for text in ("prepare the report", "brief me on the news", "prep the slides", "prepare me a coffee",
                     "brief me", "brief me about Ana", "can you prep me", "web: prep me", "prep me?",
                     "meeting prep notes", "prep", "preparation for the meeting"):
            with self.subTest(text=text):
                self.assertEqual(route(text, PREP_ON).kind, ROUTE_NONE)

    def test_the_spec_must_not_match_list(self) -> None:
        for text in ("remember to call Ana", "remember when we met?", "remember what Ana said",
                     "remember that meeting we had?", "web: remember that x", "planet news",
                     "plan my day and email Ana", "prepare the report", "brief me on the news", "forget it"):
            with self.subTest(text=text):
                self.assertEqual(route(text, ALL_ON).kind, ROUTE_NONE)

    def test_the_flag_turns_it_off(self) -> None:
        self.assertEqual(route("prep me", BOTH).kind, ROUTE_NONE)              # memory and plan only
        self.assertEqual(route("prep me", RouteFlags()).kind, ROUTE_NONE)

    def test_memory_and_plan_keep_their_words(self) -> None:
        self.assertEqual(route("remember that I prep me notes on Fridays", ALL_ON).kind, ROUTE_MEMORY_SAVE)
        self.assertEqual(route("plan my day, prep me for nothing", ALL_ON), Route(ROUTE_PLAN, "prep me for nothing",
                                                                                  PART_DAY))

    def test_repr_has_no_words(self) -> None:
        found = route("prep me for Zq7 Sync", PREP_ON)
        self.assertEqual(repr(found), "Route(prep)")


if __name__ == "__main__":
    unittest.main()
