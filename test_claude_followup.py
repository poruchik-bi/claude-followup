#!/usr/bin/env python3
"""Tests for the pure parsing logic -- run with: python3 -m unittest -v

Everything with side effects (zellij, tmux, systemd) is deliberately not
mocked; the parsers are where the bugs actually live, especially the
usage-limit reset scraping, which is coupled to Claude Code's output format.
"""

import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_followup import (
    ParseError,
    __version__,
    build_id,
    classify_when,
    describe,
    fire_at_of,
    fmt_delta,
    parse_duration,
    parse_clock,
    parse_iso,
    parse_reset,
    prog_name,
    sanitize,
    unit_name,
    version_string,
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

    def test_stale_notice_stays_in_the_past(self):
        # 3am read at noon is a notice from before 3am; that limit has reset.
        # Rolling it to tomorrow would queue a 15h wait for nothing.
        when, _ = parse_reset(
            "You've hit your session limit · resets 3am (UTC)", NOW
        )
        self.assertEqual(when, NOW.replace(hour=3))

    def test_reset_that_just_happened_is_not_pushed_a_day_out(self):
        # Reported case: pane said 'resets 7pm (UTC)' and it was 19:04.
        # The limit had reset 4 minutes earlier, not in 23h56m.
        anchor = NOW.replace(hour=19, minute=4)
        when, _ = parse_reset(
            "You've hit your session limit · resets 7pm (UTC)", anchor
        )
        self.assertEqual(when, NOW.replace(hour=19, minute=0))
        self.assertLess(when, anchor)

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
        # NOW is 18:00 in Dhaka (UTC+6); the nearest 08:00 Dhaka is that
        # morning, 02:00 UTC the same day.
        when, stamp = parse_reset("resets 8am (Asia/Dhaka)", NOW)
        self.assertEqual(when, datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(stamp, "8am (Asia/Dhaka)")

    def test_hour_only_needs_meridiem(self):
        self.assertEqual(parse_reset("resets 8pm", NOW)[0], NOW.replace(hour=20))
        self.assertIsNone(parse_reset("resets 8", NOW))

    def test_nearest_occurrence_not_next(self):
        # 9am is 3h behind noon; 9am tomorrow is 21h ahead. Nearest wins.
        self.assertEqual(parse_reset("resets 9am", NOW)[0], NOW.replace(hour=9))
        # 8pm is 8h ahead, yesterday's 8pm is 16h behind. Nearest is ahead.
        self.assertEqual(parse_reset("resets 8pm", NOW)[0], NOW.replace(hour=20))

    def test_ignores_reset_stamps_on_non_limit_lines(self):
        # Real pane scrollback: chat text about this tool sat above the actual
        # notice, and every line of it matched the bare "resets <time>" shape.
        pane = "\n".join([
            "An undated stamp like `resets 7pm` is read as the nearest",
            "a past resets 7pm sends now; a future resets 11pm still schedules",
            "  You've hit your session limit · resets 12am (UTC)",
            "  /upgrade to increase your usage limit.",
        ])
        when, stamp = parse_reset(pane, NOW)
        self.assertEqual(stamp, "12am (UTC)")
        self.assertEqual(when, datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc))

    def test_exact_twelve_hour_tie_prefers_the_future(self):
        # 'resets 12am' read at noon is 12h either way. Waiting is recoverable;
        # firing into a session that is still limited wastes the prompt.
        when, _ = parse_reset("session limit · resets 12am (UTC)", NOW)
        self.assertEqual(when, datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc))

    def test_falls_back_when_no_line_mentions_a_limit(self):
        # Keeps working if the wording ever drops the word "limit".
        self.assertEqual(parse_reset("resets 8pm", NOW)[0], NOW.replace(hour=20))

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


class TestTranscriptAnchoring(unittest.TestCase):
    """A bare 'resets 5pm' only means something relative to when it was written."""

    def test_parses_transcript_timestamps(self):
        self.assertEqual(
            parse_iso("2026-07-08T13:54:17.138Z"),
            datetime(2026, 7, 8, 13, 54, 17, 138000, tzinfo=timezone.utc),
        )
        self.assertEqual(parse_iso("2026-07-08T13:54:17+00:00").year, 2026)

    def test_bad_timestamps_are_none(self):
        for stamp in [None, "", "not-a-date", "2026-13-45T99:99:99Z"]:
            self.assertIsNone(parse_iso(stamp), stamp)

    def test_stale_entry_resolves_into_the_past(self):
        # Written 2026-07-08 13:54Z, saying 'resets 4:50pm'. That reset happened
        # the same afternoon; anchoring to NOW instead would wrongly place it
        # tomorrow and schedule a follow-up for a month-dead rate limit.
        written = parse_iso("2026-07-08T13:54:17.138Z")
        when, _ = parse_reset(
            "You've hit your session limit · resets 4:50pm (UTC)", written
        )
        self.assertEqual(when, datetime(2026, 7, 8, 16, 50, tzinfo=timezone.utc))
        self.assertLess(when, NOW)  # so detect_reset discards it

    def test_live_entry_still_resolves_into_the_future(self):
        written = NOW - timedelta(minutes=5)
        when, _ = parse_reset(
            "You've hit your session limit · resets 5pm (UTC)", written
        )
        self.assertEqual(when, NOW.replace(hour=17))
        self.assertGreater(when, NOW)

    def test_entry_written_before_midnight_rolls_forward(self):
        written = datetime(2026, 8, 5, 23, 30, tzinfo=timezone.utc)
        when, _ = parse_reset("resets 1am (UTC)", written)
        self.assertEqual(when, datetime(2026, 8, 6, 1, 0, tzinfo=timezone.utc))


class TestProgName(unittest.TestCase):
    def test_reports_the_alias_it_was_invoked_as(self):
        self.assertEqual(prog_name("/home/u/.local/bin/cf"), "cf")
        self.assertEqual(prog_name("/usr/bin/claude-followup"), "claude-followup")

    def test_falls_back_for_direct_and_module_invocation(self):
        for argv0 in ["", "./claude_followup.py", "/usr/bin/python3",
                      "/usr/lib/python3.12/unittest/__main__.py"]:
            self.assertEqual(prog_name(argv0), "claude-followup", argv0)


class TestVersion(unittest.TestCase):
    def test_build_id_is_a_short_hex_digest(self):
        build = build_id()
        self.assertEqual(len(build), 7)
        self.assertTrue(all(c in "0123456789abcdef" for c in build), build)

    def test_build_id_tracks_file_contents(self):
        # Two installs reporting the same version can still be different code;
        # the build id is what makes that visible when comparing machines.
        import claude_followup

        source = Path(claude_followup.__file__).read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest()[:7], build_id())
        altered = hashlib.sha256(source + b"\n# changed\n").hexdigest()[:7]
        self.assertNotEqual(altered, build_id())

    def test_version_string_carries_both(self):
        text = version_string()
        self.assertIn("claude-followup", text)
        self.assertIn(__version__, text)
        self.assertIn(build_id(), text)


class TestFormatting(unittest.TestCase):
    def test_delta(self):
        self.assertEqual(fmt_delta(45), "45s")
        self.assertEqual(fmt_delta(90), "1m30s")
        self.assertEqual(fmt_delta(5400), "1h30m")
        self.assertEqual(fmt_delta(-1), "overdue")


if __name__ == "__main__":
    unittest.main()
