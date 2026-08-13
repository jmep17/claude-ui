"""Cost math: day bucketing, window boundaries, de-duplication, pricing.

Stdlib unittest, no dependencies — `python3 -m unittest discover tests` from the
repo root, or just `python3 tests/test_costs.py`.

The window tests drive cost_stats() with a synthetic day table instead of real
transcripts, so they exercise the boundary arithmetic without touching the disk.
"""

import datetime
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))

from claude_ui import insight  # noqa: E402


def row(tin=0, out=0, cw5m=0, cw1h=0, cr=0, web=0, msgs=1):
    r = insight.ROW_ZERO[:]
    r[insight.R_IN], r[insight.R_OUT] = tin, out
    r[insight.R_CW5M], r[insight.R_CW1H] = cw5m, cw1h
    r[insight.R_CR], r[insight.R_WEB], r[insight.R_MSGS] = cr, web, msgs
    return r


class FixedZone(unittest.TestCase):
    """Day buckets are local dates, so pin the zone or the assertions drift."""

    ZONE = "UTC"

    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = self.ZONE
        time.tzset()

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()


class DayBucketing(FixedZone):
    """_local_day converts an API timestamp to the *local* calendar day."""

    def test_formats(self):
        for ts in ("2026-07-30T11:25:27.932Z", "2026-07-30T11:25:27Z",
                   "2026-07-30T11:25:27", "2026-07-30 11:25:27",
                   "2026-07-30T11:25:27.932456789Z"):
            self.assertEqual(insight._local_day(ts), "2026-07-30", ts)

    def test_unparseable_is_empty(self):
        for ts in ("", None, "not a date", "2026-13-99T99:99:99Z"):
            self.assertEqual(insight._local_day(ts), "")

    def test_offset_sign(self):
        # 2026-07-30T01:00-05:00 is 06:00 UTC the same day: a negative offset
        # means local is *behind* UTC, so the epoch moves forward.
        self.assertEqual(insight._local_day("2026-07-30T01:00:00-05:00"),
                         "2026-07-30")
        # 23:00 at -05:00 is 04:00 UTC the *next* day.
        self.assertEqual(insight._local_day("2026-07-30T23:00:00-05:00"),
                         "2026-07-31")
        # 01:00 at +05:00 is 20:00 UTC the *previous* day.
        self.assertEqual(insight._local_day("2026-07-30T01:00:00+05:00"),
                         "2026-07-29")
        self.assertEqual(insight._local_day("2026-07-30T01:00:00+0500"),
                         "2026-07-29")

    def test_local_zone_is_honoured(self):
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        # 03:00 UTC is 23:00 the previous day in New York.
        self.assertEqual(insight._local_day("2026-07-30T03:00:00Z"),
                         "2026-07-29")

    def test_is_day(self):
        self.assertTrue(insight._is_day("2026-07-30"))
        for bad in ("unknown", "", "2026-7-30", "2026-07-30T00:00:00", None):
            self.assertFalse(insight._is_day(bad), bad)


class Windows(unittest.TestCase):
    """Window membership: today / last7 / last30 / month-to-date."""

    def setUp(self):
        self._real = insight.transcript_stats
        self.today = datetime.date.today()

    def tearDown(self):
        insight.transcript_stats = self._real

    def totals(self, days):
        """days: {day_string: cost_in_dollars} -> cost_stats totals."""
        # 1M input tokens on opus-5 ($5/Mtok) is $5, so scale from there.
        table = {}
        for day, dollars in days.items():
            key = insight._rate_key("claude-opus-5", 1.0)
            table.setdefault(day, {})[key] = row(tin=int(dollars / 5 * 1_000_000))
        insight.transcript_stats = lambda rescan=False: {
            "days": table, "projects": {}, "sessions": 1,
            "dir": "~/.claude/projects", "available": True}
        return insight.cost_stats()

    def day(self, delta):
        return (self.today + datetime.timedelta(days=delta)).isoformat()

    def test_last7_counts_seven_days(self):
        t = self.totals({self.day(-d): 5 for d in range(0, 10)})
        self.assertAlmostEqual(t["totals"]["last7"], 35.0, places=2)
        self.assertAlmostEqual(t["totals"]["today"], 5.0, places=2)

    def test_last7_boundary(self):
        self.assertAlmostEqual(self.totals({self.day(-6): 5})["totals"]["last7"],
                               5.0, places=2)
        self.assertAlmostEqual(self.totals({self.day(-7): 5})["totals"]["last7"],
                               0.0, places=2)

    def test_last30_boundary(self):
        self.assertAlmostEqual(self.totals({self.day(-29): 5})["totals"]["last30"],
                               5.0, places=2)
        self.assertAlmostEqual(self.totals({self.day(-30): 5})["totals"]["last30"],
                               0.0, places=2)

    def test_month_to_date(self):
        first = self.today.replace(day=1)
        prev_last = first - datetime.timedelta(days=1)
        t = self.totals({first.isoformat(): 5, prev_last.isoformat(): 5,
                         self.today.isoformat(): 5})
        expected = 5.0 if first == self.today else 10.0
        self.assertAlmostEqual(t["totals"]["month"], expected, places=2)

    def test_unknown_day_excluded_from_every_window(self):
        """Regression: "unknown" sorts after every ISO date, so a plain >=
        comparison counted undated messages in last7/last30/month."""
        t = self.totals({"unknown": 5})
        for w in ("today", "last7", "last30", "month"):
            self.assertAlmostEqual(t["totals"][w], 0.0, places=2, msg=w)
        # ...but the tokens were really spent, so all-time still counts them.
        self.assertAlmostEqual(t["totals"]["all"], 5.0, places=2)
        # ...and it must not show up as a bar on the daily chart.
        self.assertEqual([d["day"] for d in t["days"]], [])

    def test_future_day_excluded_from_every_window(self):
        """Regression: a clock-skewed future day passed every `day >= x` test."""
        t = self.totals({self.day(3): 5})
        for w in ("today", "last7", "last30", "month"):
            self.assertAlmostEqual(t["totals"][w], 0.0, places=2, msg=w)
        self.assertAlmostEqual(t["totals"]["all"], 5.0, places=2)

    def test_windows_nest(self):
        t = self.totals({self.day(-d): 5 for d in range(0, 40)})
        tt = t["totals"]
        self.assertLessEqual(tt["today"], tt["last7"])
        self.assertLessEqual(tt["last7"], tt["last30"])
        self.assertLessEqual(tt["last30"], tt["all"])
        self.assertLessEqual(tt["month"], tt["all"])


class Pricing(unittest.TestCase):
    def test_table_matches_published_rates(self):
        # Checked against the published pricing page.
        for model, day, want in [
            ("claude-fable-5", "2026-07-31", (10, 50)),
            ("claude-mythos-5", "2026-07-31", (10, 50)),
            ("claude-opus-5", "2026-07-31", (5, 25)),
            ("claude-opus-4-8", "2026-07-31", (5, 25)),
            ("claude-opus-4-5", "2026-07-31", (5, 25)),
            ("claude-opus-4-1-20250805", "2026-07-31", (15, 75)),
            ("claude-opus-4-0", "2026-07-31", (15, 75)),
            ("claude-3-opus-20240229", "2026-07-31", (15, 75)),
            ("claude-sonnet-4-6", "2026-07-31", (3, 15)),
            ("claude-haiku-4-5", "2026-07-31", (1, 5)),
            ("claude-3-5-haiku-20241022", "2026-07-31", (0.8, 4)),
            ("claude-3-haiku-20240307", "2026-07-31", (0.25, 1.25)),
        ]:
            pin, pout, known = insight.model_price(model, day, {})
            self.assertTrue(known, model)
            self.assertEqual((pin, pout), want, model)

    def test_sonnet5_introductory_rate_expires(self):
        # $2/$10 through 2026-08-31, $3/$15 from 2026-09-01.
        self.assertEqual(insight.model_price("claude-sonnet-5", "2026-08-31", {})[:2],
                         (2, 10))
        self.assertEqual(insight.model_price("claude-sonnet-5", "2026-09-01", {})[:2],
                         (3, 15))

    def test_narrower_substrings_win(self):
        # First match wins, so the specific entries must precede the generic ones.
        self.assertEqual(
            insight.model_price("claude-3-5-haiku-20241022", "2026-07-31", {})[:2],
            (0.8, 4))
        self.assertEqual(
            insight.model_price("claude-opus-4-1-20250805", "2026-07-31", {})[:2],
            (15, 75))

    def test_unknown_model_is_flagged(self):
        pin, pout, known = insight.model_price("claude-brand-new-9", "2026-07-31", {})
        self.assertFalse(known)
        self.assertEqual((pin, pout), (5, 25))

    def test_cache_multipliers(self):
        # 1M each of the five categories on a $5/$25 model.
        M = 1_000_000
        self.assertAlmostEqual(insight._row_cost(row(tin=M), 5, 25), 5.0)
        self.assertAlmostEqual(insight._row_cost(row(out=M), 5, 25), 25.0)
        self.assertAlmostEqual(insight._row_cost(row(cw5m=M), 5, 25), 6.25)
        self.assertAlmostEqual(insight._row_cost(row(cw1h=M), 5, 25), 10.0)
        self.assertAlmostEqual(insight._row_cost(row(cr=M), 5, 25), 0.5)

    def test_worked_example_from_pricing_page(self):
        # 10k uncached input + 40k cache reads + 15k output on opus-5 = $0.445.
        self.assertAlmostEqual(
            insight._row_cost(row(tin=10_000, cr=40_000, out=15_000), 5, 25),
            0.445, places=6)

    def test_web_search_is_billed_per_search(self):
        # $10 per 1,000 searches, on top of tokens. Web fetch is free and is
        # deliberately not counted.
        self.assertAlmostEqual(insight._row_cost(row(web=1000), 5, 25), 10.0)
        self.assertAlmostEqual(insight._row_cost(row(web=1), 5, 25), 0.01)

    def test_rate_multiplier_scales_tokens_but_not_web_search(self):
        M = 1_000_000
        self.assertAlmostEqual(insight._row_cost(row(tin=M, web=100), 5, 25, 2.0),
                               10.0 + 1.0)


class RateMultipliers(unittest.TestCase):
    def test_fast_mode_doubles_supported_models(self):
        for model in ("claude-opus-5", "claude-opus-4-8"):
            self.assertEqual(
                insight._rate_multiplier(model, {"speed": "fast"}), 2.0, model)

    def test_fast_mode_ignored_where_it_is_not_billed(self):
        # Opus 4.6 accepts speed=fast, runs at standard speed and bills standard;
        # 4.7 rejects it outright. Neither should get the 2x premium.
        for model in ("claude-opus-4-6", "claude-opus-4-7", "claude-sonnet-5"):
            self.assertEqual(
                insight._rate_multiplier(model, {"speed": "fast"}), 1.0, model)

    def test_standard_and_absent_speed_are_plain(self):
        for usage in ({"speed": "standard"}, {}):
            self.assertEqual(
                insight._rate_multiplier("claude-opus-5", usage), 1.0)

    def test_us_inference_geo(self):
        self.assertAlmostEqual(
            insight._rate_multiplier("claude-opus-5", {"inference_geo": "us"}), 1.1)
        for geo in ("global", "not_available", None):
            self.assertEqual(
                insight._rate_multiplier("claude-opus-5", {"inference_geo": geo}), 1.0)

    def test_premiums_stack(self):
        self.assertAlmostEqual(
            insight._rate_multiplier(
                "claude-opus-5", {"speed": "fast", "inference_geo": "us"}), 2.2)

    def test_rate_key_round_trip(self):
        for mult in (1.0, 1.1, 2.0, 2.2):
            model, back = insight._split_rate_key(
                insight._rate_key("claude-opus-5", mult))
            self.assertEqual(model, "claude-opus-5")
            self.assertAlmostEqual(back, mult, places=4)


class Overrides(unittest.TestCase):
    """_excluded and model_price must agree on what a usable override is."""

    def test_claude_models_always_priced(self):
        self.assertFalse(insight._excluded("claude-opus-5", None))

    def test_non_claude_dropped_by_default(self):
        for model in ("<synthetic>", "llama3", "gpt-4o", ""):
            self.assertTrue(insight._excluded(model, None), model)

    def test_valid_override_opts_a_model_back_in(self):
        ov = {"llama": [1.0, 2.0]}
        self.assertFalse(insight._excluded("llama3", ov))
        self.assertEqual(insight.model_price("llama3", "2026-07-31", ov),
                         (1.0, 2.0, True))

    def test_malformed_override_does_not_opt_a_model_in(self):
        """Regression: _excluded used to admit any substring match while
        model_price refused to price it, so the model was counted at the
        opus-tier guess."""
        for bad in ({"llama": 3}, {"llama": []}, {"llama": [1]},
                    {"llama": [1, 2, 3]}, {"llama": ["a", "b"]}, {"llama": None}):
            self.assertTrue(insight._excluded("llama3", bad), bad)

    def test_override_wins_over_the_table(self):
        ov = {"opus-5": [1.0, 2.0]}
        self.assertEqual(insight.model_price("claude-opus-5", "2026-07-31", ov)[:2],
                         (1.0, 2.0))


class Dedup(FixedZone):
    """One API response must be counted once, however many lines carry it."""

    def scan(self, lines, path=None):
        keep = path is not None
        if path is None:
            fd, path = tempfile.mkstemp(suffix=".jsonl")
            os.close(fd)
        with open(path, "w") as fh:
            for obj in lines:
                fh.write(json.dumps(obj) + "\n")
        try:
            return insight._scan_transcript(path)["msgs"]
        finally:
            if not keep:
                os.unlink(path)

    @staticmethod
    def entry(uuid=None, mid=None, request=None, tokens=10):
        e = {"timestamp": "2026-07-30T11:25:27.932Z",
             "message": {"model": "claude-opus-5",
                         "usage": {"input_tokens": tokens, "output_tokens": 1}}}
        if uuid is not None:
            e["uuid"] = uuid
        if mid is not None:
            e["message"]["id"] = mid
        if request is not None:
            e["requestId"] = request
        return e

    def test_content_blocks_of_one_message_count_once(self):
        msgs = self.scan([self.entry(uuid="u%d" % i, mid="msg_1", request="req_1")
                          for i in range(5)])
        self.assertEqual(len(msgs), 1)

    def test_distinct_messages_stay_distinct(self):
        msgs = self.scan([self.entry(uuid="u1", mid="msg_1", request="req_1"),
                          self.entry(uuid="u2", mid="msg_2", request="req_2")])
        self.assertEqual(len(msgs), 2)

    def test_uuid_fallback_when_no_message_id(self):
        msgs = self.scan([self.entry(uuid="u1"), self.entry(uuid="u2")])
        self.assertEqual(len(msgs), 2)

    def test_idless_uuidless_entries_stay_distinct(self):
        """Regression: the fallback key used id(entry), a memory address that
        CPython reuses within a scan, so entries collided and were dropped."""
        msgs = self.scan([self.entry(tokens=n) for n in range(1, 9)])
        self.assertEqual(len(msgs), 8)
        self.assertEqual(sorted(e[3] for e in msgs.values()), list(range(1, 9)))

    def test_dedup_key_is_stable_across_runs(self):
        """Keys are persisted to the on-disk cache, so re-scanning the same file
        must reproduce them exactly."""
        lines = [self.entry(tokens=n) for n in range(1, 9)]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            self.assertEqual(sorted(self.scan(lines, path)),
                             sorted(self.scan(lines, path)))
        finally:
            os.unlink(path)

    def test_usage_fields_are_extracted(self):
        e = self.entry(uuid="u1", mid="msg_1")
        e["message"]["usage"].update({
            "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 100,
                               "ephemeral_1h_input_tokens": 200},
            "cache_read_input_tokens": 400,
            "server_tool_use": {"web_search_requests": 2,
                                "web_fetch_requests": 7},
            "speed": "fast"})
        (row_,) = self.scan([e]).values()
        day, model, mult = row_[0], row_[1], row_[2]
        self.assertEqual(model, "claude-opus-5")
        self.assertEqual(mult, 2.0)
        # [day, model, mult, in, out, cw5m, cw1h, cr, web]
        self.assertEqual(row_[3:], [10, 1, 100, 200, 400, 2])

    def test_partial_usage_on_an_early_line_does_not_win(self):
        """Regression: a message's lines are written as it streams, so the first
        can carry a partial usage and a later one the authoritative total. Keeping
        the first (setdefault) lost ~3% of output tokens against ccusage."""
        partial = self.entry(uuid="u1", mid="msg_1", request="req_1", tokens=5)
        partial["message"]["usage"]["output_tokens"] = 5
        final = self.entry(uuid="u2", mid="msg_1", request="req_1", tokens=5)
        final["message"]["usage"]["output_tokens"] = 194
        for order in ([partial, final], [final, partial]):
            (row_,) = self.scan(order).values()
            self.assertEqual(row_[4], 194)   # largest wins, whatever the order

    def test_first_line_still_fixes_the_day_bucket(self):
        """Taking the max of the counters must not move the day, whose timestamps
        drift by milliseconds between a message's lines."""
        a = self.entry(uuid="u1", mid="msg_1", request="req_1")
        a["timestamp"] = "2026-07-30T23:59:59.100Z"
        b = self.entry(uuid="u2", mid="msg_1", request="req_1")
        b["timestamp"] = "2026-07-31T00:00:00.100Z"
        b["message"]["usage"]["output_tokens"] = 999
        (row_,) = self.scan([a, b]).values()
        self.assertEqual(row_[0], "2026-07-30")
        self.assertEqual(row_[4], 999)

    def test_impossible_date_does_not_abort_the_scan(self):
        """Regression: TS_RE only checks shape, so "2026-13-99T00:00:00Z" reached
        calendar.timegm and raised ValueError straight through _scan_transcript
        (which catches only OSError), 500-ing the whole /api/costs request."""
        bad = self.entry(uuid="u1", mid="msg_1", tokens=7)
        bad["timestamp"] = "2026-13-99T00:00:00Z"
        good = self.entry(uuid="u2", mid="msg_2", tokens=9)
        msgs = self.scan([bad, good])
        self.assertEqual(len(msgs), 2)
        days = sorted(e[0] for e in msgs.values())
        self.assertEqual(days, ["2026-07-30", "unknown"])

    def test_cache_creation_split_defaults_to_5m(self):
        """Transcripts predating the TTL split report only a write total."""
        e = self.entry(uuid="u1", mid="msg_1")
        e["message"]["usage"]["cache_creation_input_tokens"] = 500
        (row_,) = self.scan([e]).values()
        self.assertEqual((row_[5], row_[6]), (500, 0))


class Sess(FixedZone):
    """The per-session summary the Context tab reads: first message, peak,
    timestamps. Cached per file, so its shape change is behind CACHE_V >= 6."""

    entry = staticmethod(Dedup.entry)

    def full(self, lines):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        with open(path, "w") as fh:
            for obj in lines:
                fh.write(json.dumps(obj) + "\n")
        try:
            return insight._scan_transcript(path)
        finally:
            os.unlink(path)

    def stamped(self, ts, mid, tin=10, cw=0, cr=0):
        e = self.entry(uuid=mid, mid=mid, request=mid, tokens=tin)
        e["timestamp"] = ts
        e["message"]["usage"].update({"cache_creation_input_tokens": cw,
                                      "cache_read_input_tokens": cr})
        return e

    def test_first_message_fixes_the_baseline(self):
        sess = self.full([
            self.stamped("2026-07-30T10:00:00Z", "m1", tin=5, cw=30000, cr=7),
            self.stamped("2026-07-30T10:05:00Z", "m2", tin=999, cw=90000,
                         cr=60000)])["sess"]
        self.assertEqual(sess["first"], [5, 30000, 7])
        self.assertEqual(sess["max_cr"], 60000)
        self.assertEqual(sess["first_ts"], "2026-07-30T10:00:00Z")
        self.assertEqual(sess["last_ts"], "2026-07-30T10:05:00Z")
        self.assertEqual(sess["model"], "claude-opus-5")

    def test_streamed_partial_first_line_is_healed(self):
        """The first message's early lines can carry partial usage; `first`
        must read the max-merged value, not the first line's."""
        partial = self.stamped("2026-07-30T10:00:00Z", "m1", tin=1)
        final = self.stamped("2026-07-30T10:00:00Z", "m1", tin=1, cw=33000)
        sess = self.full([partial, final])["sess"]
        self.assertEqual(sess["first"], [1, 33000, 0])

    def test_no_usage_means_no_session(self):
        self.assertIsNone(self.full([{"cwd": "/x", "type": "user"}])["sess"])

    def test_cache_version_covers_the_new_shape(self):
        """`sess` joined the cached per-file data in v6; an older cache must
        be discarded wholesale, not read with the key missing."""
        self.assertGreaterEqual(insight.CACHE_V, 6)


class MixedRatesInOneDay(unittest.TestCase):
    """A day can mix fast and standard requests; each part prices separately."""

    def setUp(self):
        self._real = insight.transcript_stats

    def tearDown(self):
        insight.transcript_stats = self._real

    def test_split_and_recombined(self):
        today = datetime.date.today().isoformat()
        M = 1_000_000
        table = {today: {insight._rate_key("claude-opus-5", 1.0): row(tin=M),
                         insight._rate_key("claude-opus-5", 2.0): row(tin=M)}}
        insight.transcript_stats = lambda rescan=False: {
            "days": table, "projects": {}, "sessions": 1, "dir": "d",
            "available": True}
        r = insight.cost_stats()
        # $5 standard + $10 fast
        self.assertAlmostEqual(r["totals"]["today"], 15.0, places=2)
        # by_model merges the rates back into one visible row...
        self.assertEqual(len(r["by_model"]), 1)
        self.assertEqual(r["by_model"][0]["model"], "claude-opus-5")
        self.assertEqual(r["by_model"][0]["in"], 2 * M)
        self.assertEqual(r["by_model"][0]["msgs"], 2)
        # ...as does the chart's per-day breakdown.
        self.assertEqual(r["days"][0]["by"], {"claude-opus-5": 15.0})


class DroppedModelsAreReported(unittest.TestCase):
    """A model pricing refuses must be named, or the tab reads $0 with no reason."""

    def setUp(self):
        self._real = insight.transcript_stats
        self._cfg = insight.read_cfg
        insight.read_cfg = lambda: {}   # no local `pricing` overrides
        self.today = datetime.date.today().isoformat()

    def tearDown(self):
        insight.transcript_stats = self._real
        insight.read_cfg = self._cfg

    def stats(self, rows):
        """rows: {model: row} on today -> cost_stats()."""
        table = {self.today: {insight._rate_key(m, 1.0): r
                              for m, r in rows.items()}}
        insight.transcript_stats = lambda rescan=False: {
            "days": table, "projects": {}, "sessions": 1, "dir": "d",
            "available": True}
        return insight.cost_stats()

    def test_alias_only_transcript_reports_why_it_is_zero(self):
        r = self.stats({"internal-gateway-alias": row(tin=1_000_000, msgs=4)})
        self.assertEqual(r["totals"]["all"], 0)
        self.assertEqual(r["excluded_models"], ["internal-gateway-alias"])
        self.assertEqual(r["dropped_msgs"], 4)
        self.assertEqual(r["by_model"], [])

    def test_mixed_prices_the_claude_one_and_still_names_the_alias(self):
        r = self.stats({"claude-opus-5": row(tin=1_000_000, msgs=2),
                        "sonnet-4-5": row(tin=1_000_000, msgs=3)})
        self.assertAlmostEqual(r["totals"]["all"], 5.0, places=2)
        self.assertEqual(r["excluded_models"], ["sonnet-4-5"])
        self.assertEqual(r["dropped_msgs"], 3)
        self.assertEqual([m["model"] for m in r["by_model"]], ["claude-opus-5"])

    def test_synthetic_placeholder_is_not_reported_as_a_gap(self):
        """<synthetic> messages were never billed — warning about them would
        cry wolf on every healthy machine."""
        r = self.stats({"claude-opus-5": row(tin=1_000_000, msgs=2),
                        "<synthetic>": row(tin=1_000, msgs=9)})
        self.assertEqual(r["excluded_models"], [])
        self.assertEqual(r["dropped_msgs"], 0)

    def test_anthropic_prefixed_id_is_priced_and_flagged_unknown(self):
        r = self.stats({"anthropic.brand-new-9": row(tin=1_000_000, msgs=1)})
        self.assertEqual(r["excluded_models"], [])
        self.assertEqual(r["unknown_models"], ["anthropic.brand-new-9"])
        # priced at the opus-tier guess rather than silently dropped
        self.assertAlmostEqual(r["totals"]["all"], 5.0, places=2)


class ZeroPricingOverrides(unittest.TestCase):
    """A [0, 0] override is how a local model is counted as free — but the keys
    match as substrings, so a short one silently zeroes real Claude usage."""

    def setUp(self):
        self._real = insight.transcript_stats
        self._cfg = insight.read_cfg
        self.today = datetime.date.today().isoformat()

    def tearDown(self):
        insight.transcript_stats = self._real
        insight.read_cfg = self._cfg

    def stats(self, pricing, models=("claude-opus-5",)):
        insight.read_cfg = lambda: {"pricing": pricing}
        table = {self.today: {insight._rate_key(m, 1.0): row(tin=1_000_000, msgs=1)
                              for m in models}}
        insight.transcript_stats = lambda rescan=False: {
            "days": table, "projects": {}, "sessions": 1, "dir": "d",
            "available": True}
        return insight.cost_stats()

    def test_broad_key_zeroes_real_usage_and_is_named(self):
        r = self.stats({"opus": [0, 0]})
        self.assertEqual(r["totals"]["all"], 0)
        self.assertEqual(r["zeroed_models"],
                         [{"model": "claude-opus-5", "override": "opus"}])

    def test_nothing_reported_when_prices_are_real(self):
        self.assertEqual(self.stats({})["zeroed_models"], [])

    def test_local_model_alone_does_not_flag_the_claude_ones(self):
        r = self.stats({"my-local-llama": [0, 0]},
                       models=("claude-opus-5", "my-local-llama"))
        self.assertAlmostEqual(r["totals"]["all"], 5.0, places=2)
        self.assertEqual([z["model"] for z in r["zeroed_models"]], ["my-local-llama"])

    def test_exact_key_beats_a_broader_one(self):
        """Regression: which override won used to depend on JSON key order."""
        for pricing in ({"opus": [0, 0], "claude-opus-5": [5, 25]},
                        {"claude-opus-5": [5, 25], "opus": [0, 0]}):
            r = self.stats(pricing)
            self.assertAlmostEqual(r["totals"]["all"], 5.0, places=2, msg=str(pricing))
            self.assertEqual(r["zeroed_models"], [], str(pricing))

    def test_longest_key_wins_when_neither_is_exact(self):
        self.assertEqual(
            insight._override_match({"opus": [1, 2], "claude-opus": [3, 4]},
                                    "claude-opus-5-20260101"),
            ("claude-opus", 3.0, 4.0))

    def test_diagnostics_names_the_override_that_zeroed_it(self):
        insight.read_cfg = lambda: {"pricing": {"opus": [0, 0]}}
        insight.transcript_stats = lambda rescan=False: {
            "days": {self.today: {insight._rate_key("claude-opus-5", 1.0): row(msgs=1)}},
            "projects": {}, "sessions": 1, "dir": "d", "available": True}
        m = insight.cost_diagnostics()["models"][0]
        self.assertEqual(m["verdict"], "zero-priced")
        self.assertIn('"opus"', m["note"])


class UsageWithoutAModelId(FixedZone):
    """Usage that names no model is dropped at scan time, before pricing ever
    sees it — so the scan has to count it or the tab can't say where it went."""

    def scan(self, lines):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        with open(path, "w") as fh:
            for obj in lines:
                fh.write(json.dumps(obj) + "\n")
        try:
            return insight._scan_transcript(path)
        finally:
            os.unlink(path)

    def test_counted_and_not_priced(self):
        d = self.scan([
            {"timestamp": "2026-07-30T11:25:27.932Z", "uuid": "u1",
             "message": {"id": "m1", "usage": {"input_tokens": 100,
                                               "output_tokens": 10}}},
            {"timestamp": "2026-07-30T11:26:27.932Z", "uuid": "u2",
             "message": {"id": "m2", "model": "claude-opus-5",
                         "usage": {"input_tokens": 100, "output_tokens": 10}}},
        ])
        self.assertEqual(d["nomodel"], 1)
        self.assertEqual(len(d["msgs"]), 1)   # only the one with a model id

    def test_zero_when_every_message_names_its_model(self):
        d = self.scan([
            {"timestamp": "2026-07-30T11:25:27.932Z", "uuid": "u1",
             "message": {"id": "m1", "model": "claude-opus-5",
                         "usage": {"input_tokens": 100, "output_tokens": 10}}},
        ])
        self.assertEqual(d["nomodel"], 0)


class Diagnostics(unittest.TestCase):
    """The Costs tab's built-in census: what the pricer saw and what it decided."""

    def setUp(self):
        self._real = insight.transcript_stats
        self._cfg = insight.read_cfg
        insight.read_cfg = lambda: {"pricing": {"gateway": [3, 15],
                                                "broken": 7}}
        today = datetime.date.today().isoformat()
        table = {today: {
            insight._rate_key("claude-opus-5", 1.0): row(tin=1_000_000, msgs=2),
            insight._rate_key("internal-alias", 1.0): row(tin=500, msgs=3),
            insight._rate_key("gateway-sonnet", 1.0): row(tin=500, msgs=1),
            insight._rate_key("claude-brand-new-9", 1.0): row(tin=500, msgs=1),
        }}
        insight.transcript_stats = lambda rescan=False: {
            "days": table, "projects": {}, "sessions": 5, "dir": "d",
            "available": True, "usage_msgs": 7, "nomodel": 4,
            "bytes": 2048, "oversize": 1}

    def tearDown(self):
        insight.transcript_stats = self._real
        insight.read_cfg = self._cfg

    def verdicts(self):
        return {m["model"]: m["verdict"] for m in insight.cost_diagnostics()["models"]}

    def test_each_model_gets_a_verdict(self):
        self.assertEqual(self.verdicts(), {
            "claude-opus-5": "priced",
            "claude-brand-new-9": "estimated",   # in-family, no list price
            "gateway-sonnet": "priced",          # opted in by a valid override
            "internal-alias": "dropped",
        })

    def test_scan_time_losses_are_reported(self):
        d = insight.cost_diagnostics()
        self.assertEqual(d["nomodel"], 4)
        self.assertEqual(d["usage_msgs"], 7)
        self.assertEqual(d["oversize"], 1)

    def test_models_are_ordered_by_message_count(self):
        d = insight.cost_diagnostics()
        self.assertEqual([m["msgs"] for m in d["models"]],
                         sorted((m["msgs"] for m in d["models"]), reverse=True))

    def test_malformed_override_is_flagged_unusable(self):
        ov = {o["key"]: o["ok"] for o in insight.cost_diagnostics()["overrides"]}
        self.assertEqual(ov, {"gateway": True, "broken": False})

    def test_synthetic_is_named_as_a_placeholder_not_a_gap(self):
        insight.transcript_stats = lambda rescan=False: {
            "days": {"2026-07-30": {insight._rate_key("<synthetic>", 1.0): row(msgs=9)}},
            "projects": {}, "sessions": 1, "dir": "d", "available": True}
        m = insight.cost_diagnostics()["models"][0]
        self.assertEqual(m["verdict"], "dropped")
        self.assertIn("never billed", m["note"])


class AnthropicFamilyIsNotExcluded(unittest.TestCase):
    def test_family_ids_pass(self):
        for model in ("claude-opus-5", "us.anthropic.claude-sonnet-4-5-v1:0",
                      "claude-sonnet-5@20250514", "anthropic.whatever"):
            self.assertFalse(insight._excluded(model, {}), model)

    def test_others_still_dropped(self):
        for model in ("sonnet-4-5", "internal-gateway-alias", "default",
                      "<synthetic>", ""):
            self.assertTrue(insight._excluded(model, {}), model)


if __name__ == "__main__":
    unittest.main(verbosity=2)
