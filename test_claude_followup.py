#!/usr/bin/env python3
"""Tests for the pure parsing logic -- run with: python3 -m unittest -v

Everything with side effects (zellij, tmux, systemd) is deliberately not
mocked; the parsers are where the bugs actually live, especially the
usage-limit reset scraping, which is coupled to Claude Code's output format.
"""

import unittest
from datetime import datetime, timedelta, timezone

from claude_followup import (
    ParseError,
    classify_when,
    describe,
    fire_at_of,
    fmt_delta,
    parse_duration,
    parse_clock,
    parse_reset,
    sanitize,
    unit_name,
    _DESCRIPTION,
)

# Fixed reference point: Wednesday 2026-08-05 12:00:00 +00:00
NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)


class TestParseDuration(unittest.TestCase):
    def test_compact_units(self):
        for text, expected in [
            ("45s", 45),
            ("90m", 5400),
            ("1h", 3600),
            ("2h30m", 9000),
            ("1h5m10s", 3910),
            ("1d", 86400),
        ]:
            self.assertEqual(parse_duration(text), expected, text)

    def test_spelled_out(self):
        for text, expected in [
            ("90 minutes", 5400),
            ("1 hour", 3600),
            ("in 2 hours", 7200),
            ("2 hours 30 minutes", 9000),
            ("1 hour and 30 mins", 5400),
            ("  30   SECONDS ", 30),
        ]:
            self.assertEqual(parse_duration(text), expected, text)

    def test_bare_number_is_ambiguous(self):
        with self.assertRaisesRegex(ParseError, "ambiguous"):
            parse_duration("90")

    def test_rejects_junk(self):
        for text in ["", "soon", "90x", "3pm", "15:00", "90m tomorrow", "0m", "-5m"]:
            with self.assertRaises(ParseError, msg=text):
                parse_duration(text)


class TestParseClock(unittest.TestCase):
    def test_today_when_still_ahead(self):
        self.assertEqual(parse_clock("15:00", NOW), NOW.replace(hour=15, minute=0))
        self.assertEqual(parse_clock("3:15pm", NOW), NOW.replace(hour=15, minute=15))
        self.assertEqual(parse_clock("11:59pm", NOW), NOW.replace(hour=23, minute=59))

    def test_rolls_to_tomorrow_when_past(self):
        self.assertEqual(parse_clock("09:00", NOW), NOW.replace(hour=9) + timedelta(days=1))
        self.assertEqual(parse_clock("8am", NOW), NOW.replace(hour=8) + timedelta(days=1))

    def test_midnight_and_noon(self):
        self.assertEqual(parse_clock("12am", NOW).hour, 0)
        self.assertEqual(parse_clock("12pm", NOW).hour, 12)

    def test_day_prefixes(self):
        self.assertEqual(
            parse_clock("tomorrow 09:00", NOW), NOW.replace(hour=9) + timedelta(days=1)
        )
        # NOW is a Wednesday; Friday is two days out.
        self.assertEqual(
            parse_clock("friday 8am", NOW), NOW.replace(hour=8) + timedelta(days=2)
        )
        # Same weekday as today means next week, not zero days.
        self.assertEqual(
            parse_clock("wednesday 8am", NOW), NOW.replace(hour=8) + timedelta(days=7)
        )

    def test_iso_date(self):
        self.assertEqual(
            parse_clock("2026-08-06 15:00", NOW),
            NOW.replace(hour=15) + timedelta(days=1),
        )

    def test_explicit_past_date_is_an_error(self):
        with self.assertRaisesRegex(ParseError, "already passed"):
            parse_clock("2020-01-01 09:00", NOW)
        with self.assertRaisesRegex(ParseError, "already passed"):
            parse_clock("today 09:00", NOW)

    def test_bare_hour_is_ambiguous(self):
        with self.assertRaisesRegex(ParseError, "ambiguous"):
            parse_clock("15", NOW)

    def test_rejects_junk(self):
        for text in ["", "later", "25:00", "13pm", "3:99"]:
            with self.assertRaises(ParseError, msg=text):
                parse_clock(text, NOW)


class TestClassifyWhen(unittest.TestCase):
    def test_durations(self):
        for text in ["90m", "2h30m", "45s", "90 minutes", "1 hour"]:
            self.assertEqual(classify_when(text), "in", text)

    def test_clocks(self):
        for text in ["15:00", "3:15pm", "8am", "tomorrow 09:00", "2026-08-06 15:00"]:
            self.assertEqual(classify_when(text), "at", text)

    def test_unclassifiable(self):
        with self.assertRaises(ParseError):
            classify_when("continue")


class TestParseResetRealWorld(unittest.TestCase):
    """Verbatim strings harvested from real ~/.claude transcripts.

    These are the contract with Claude Code's output. If Claude Code changes
    its usage-limit wording, these fail first and say exactly what broke.
    """

    def test_session_limit_hour_only(self):
        when, stamp = parse_reset(
            "You've hit your session limit · resets 5pm (UTC)", NOW
        )
        self.assertEqual(when, NOW.replace(hour=17))
        self.assertEqual(stamp, "5pm (UTC)")

    def test_session_limit_with_minutes(self):
        when, _ = parse_reset(
            "You've hit your session limit · resets 3:30pm (UTC)", NOW
        )
        self.assertEqual(when, NOW.replace(hour=15, minute=30))

    def test_session_limit_rolls_to_tomorrow(self):
        when, _ = parse_reset(
            "You've hit your session limit · resets 3am (UTC)", NOW
        )
        self.assertEqual(when, NOW.replace(hour=3) + timedelta(days=1))

    def test_weekly_limit_carries_a_date(self):
        # The dated form must NOT collapse to today, or we would fire days early.
        when, stamp = parse_reset(
            "You've hit your weekly limit · resets Aug 8, 9am (UTC)", NOW
        )
        self.assertEqual(when, datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(stamp, "Aug 8, 9am (UTC)")

    def test_dated_reset_wraps_to_next_year(self):
        when, _ = parse_reset(
            "You've hit your weekly limit · resets Jan 3, 9am (UTC)", NOW
        )
        self.assertEqual(when, datetime(2027, 1, 3, 9, 0, tzinfo=timezone.utc))

    def test_impossible_date_is_rejected(self):
        self.assertIsNone(parse_reset("resets Feb 30, 9am (UTC)", NOW))


class TestParseReset(unittest.TestCase):
    def test_12_hour_with_zone(self):
        found = parse_reset("Your limit will reset at 4:10pm (UTC).", NOW)
        self.assertIsNotNone(found)
        when, stamp = found
        self.assertEqual(when, NOW.replace(hour=16, minute=10))
        self.assertEqual(stamp, "4:10pm (UTC)")

    def test_24_hour_no_zone_uses_local(self):
        when, _ = parse_reset("5-hour limit reached - resets 15:00", NOW)
        self.assertEqual(when, NOW.replace(hour=15))

    def test_named_iana_zone(self):
        # 08:00 Asia/Dhaka (UTC+6) on the 6th == 02:00 UTC on the 6th.
        when, stamp = parse_reset("resets 8am (Asia/Dhaka)", NOW)
        self.assertEqual(when, datetime(2026, 8, 6, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(stamp, "8am (Asia/Dhaka)")

    def test_hour_only_needs_meridiem(self):
        self.assertEqual(parse_reset("resets 8pm", NOW)[0], NOW.replace(hour=20))
        self.assertIsNone(parse_reset("resets 8", NOW))

    def test_rolls_past_midnight(self):
        # 9am has gone by at 12:00, so the next reset is tomorrow.
        when, _ = parse_reset("resets 9am", NOW)
        self.assertEqual(when, NOW.replace(hour=9) + timedelta(days=1))

    def test_last_match_wins(self):
        text = "resets 1pm (UTC)\n...later...\nresets 5pm (UTC)"
        self.assertEqual(parse_reset(text, NOW)[0], NOW.replace(hour=17))

    def test_unknown_abbreviation_falls_back_to_local(self):
        when, stamp = parse_reset("resets 4:10pm (PST)", NOW)
        self.assertEqual(when, NOW.replace(hour=16, minute=10))
        self.assertIn("PST", stamp)  # raw stamp is preserved so a bad guess is visible

    def test_no_match(self):
        for text in ["", "nothing to see", "reset the branch", None]:
            self.assertIsNone(parse_reset(text, NOW), text)


class TestUnitNaming(unittest.TestCase):
    def test_sanitize_keeps_systemd_safe_chars(self):
        self.assertEqual(sanitize("claude4"), "claude4")
        self.assertEqual(sanitize("my session/1"), "my_session_1")
        self.assertEqual(sanitize(""), "session")

    def test_fire_time_round_trips_through_the_unit_name(self):
        unit = unit_name("my session/1", 1754400000)
        self.assertEqual(fire_at_of(unit), 1754400000)
        self.assertTrue(unit.startswith("claude-followup-my_session_1-"))

    def test_fire_time_of_garbage(self):
        self.assertIsNone(fire_at_of("claude-followup-broken"))

    def test_description_round_trips_session_and_message(self):
        # The description is our metadata store; sessions and messages may
        # contain spaces, colons and the ' :: ' separator's characters.
        parsed = _DESCRIPTION.match(describe("tmux", "my session", "run: tests :: now"))
        self.assertEqual(parsed.group("backend"), "tmux")
        self.assertEqual(parsed.group("target"), "my session")
        self.assertEqual(parsed.group("message"), "run: tests :: now")


class TestFormatting(unittest.TestCase):
    def test_delta(self):
        self.assertEqual(fmt_delta(45), "45s")
        self.assertEqual(fmt_delta(90), "1m30s")
        self.assertEqual(fmt_delta(5400), "1h30m")
        self.assertEqual(fmt_delta(-1), "overdue")


if __name__ == "__main__":
    unittest.main()
