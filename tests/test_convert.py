import io
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import make_fixture  # noqa: E402
import unpack_icbu  # noqa: E402


def unfold(raw):
    assert raw.endswith("\r\n"), "file must end with CRLF"
    lines = raw.split("\r\n")[:-1]
    out = []
    for line in lines:
        assert len(line.encode("utf-8")) <= 75, f"line over 75 octets: {line!r}"
        if line.startswith(" "):
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def parse(path):
    """Return (calendar-level lines, [event dicts]) where an event maps NAME -> [(params, value)]."""
    with open(path, encoding="utf-8", newline="") as f:
        lines = unfold(f.read())
    events, cur, depth, top = [], None, [], []
    for line in lines:
        if line.startswith("BEGIN:"):
            depth.append(line[6:])
            if line == "BEGIN:VEVENT":
                cur = {}
            continue
        if line.startswith("END:"):
            assert depth.pop() == line[4:], "unbalanced BEGIN/END"
            if line == "END:VEVENT":
                events.append(cur)
                cur = None
            continue
        if depth[:2] == ["VCALENDAR", "VEVENT"]:
            head, _, value = line.partition(":")
            name, _, params = head.partition(";")
            if len(depth) > 2:
                name = depth[-1] + "." + name
            cur.setdefault(name, []).append((params, value))
        else:
            top.append(line)
    assert not depth
    return top, events


def one(event, name):
    vals = event.get(name, [])
    assert len(vals) == 1, f"{name}: {vals}"
    return vals[0]


class ConverterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.icbu = make_fixture.build_icbu(os.path.join(cls.tmp.name, "Sample.icbu"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_convert(self, **kw):
        outdir = tempfile.mkdtemp(dir=self.tmp.name)
        buf = io.StringIO()
        results = unpack_icbu.convert(self.icbu, outdir, out=buf, **kw)
        files = {os.path.basename(p): parse(p) for _, p, _ in results}
        return results, files, buf.getvalue()

    def by_summary(self, events, prefix):
        hits = [e for e in events if e.get("SUMMARY", [("", "")])[0][1].startswith(prefix)]
        self.assertEqual(len(hits), 1, prefix)
        return hits[0]

    def test_files_and_selection(self):
        results, files, _ = self.run_convert()
        self.assertEqual(sorted(files), ["Office-4.ics", "Office.ics", "Personal.ics"])
        _, files, _ = self.run_convert(names=["Personal"])
        self.assertEqual(list(files), ["Personal.ics"])
        _, files, _ = self.run_convert(ids=[4])
        self.assertEqual(list(files), ["Office.ics"])
        with self.assertRaises(SystemExit):
            self.run_convert(names=["Nope"])

    def test_counts_and_skips(self):
        results, files, _ = self.run_convert()
        stats = {os.path.basename(p): s for _, p, s in results}
        p = stats["Personal.ics"]
        # 9 rows in Personal; the phantom master is skipped.
        self.assertEqual(p["events_written"], 8)
        self.assertEqual(p["skipped_phantom_master"], 1)
        self.assertEqual(p["detached_occurrences"], 1)
        self.assertEqual(p["detached_standalone"], 2)
        self.assertEqual(len(files["Personal.ics"][1]), 8)
        # The reminder (entity_type 3) isn't an event.
        self.assertEqual(stats["Office.ics"]["events_written"], 6)

    def test_timed_event_fields(self):
        _, files, _ = self.run_convert()
        e = self.by_summary(files["Personal.ics"][1], "Dentist")
        self.assertEqual(one(e, "SUMMARY")[1], "Dentist\\, check-up\\; bring card")
        self.assertEqual(one(e, "DTSTART"), ("", "20240305T210000Z"))
        self.assertEqual(one(e, "DTEND"), ("", "20240305T220000Z"))
        self.assertEqual(one(e, "UID")[1], "uid-1")
        self.assertEqual(one(e, "SEQUENCE")[1], "2")
        self.assertEqual(one(e, "STATUS")[1], "CONFIRMED")
        self.assertEqual(one(e, "TRANSP")[1], "OPAQUE")
        self.assertEqual(one(e, "URL")[1], "https://example.com/booking")
        self.assertEqual(one(e, "LOCATION")[1], "Harbour Clinic\\, 1 Example Street\\, Sydney")
        self.assertEqual(one(e, "GEO")[1], "-33.870000;151.210000")
        self.assertEqual(one(e, "CREATED")[1], "20240101T000000Z")
        self.assertEqual(one(e, "LAST-MODIFIED")[1], "20240101T000100Z")
        desc = one(e, "DESCRIPTION")[1]
        self.assertTrue(desc.startswith("Line one\\nLine two\\n\\n"))
        self.assertIn("Organiser: Alex Example <alex@example.com>", desc)
        self.assertIn("- Sam Sample <sam@example.org>", desc)
        self.assertIn("- No Email Person", desc)
        self.assertIn("This event had 2 attachments in Apple Calendar", desc)
        self.assertNotIn("ATTENDEE", e)
        self.assertNotIn("ORGANIZER", e)

    def test_alarms(self):
        results, files, _ = self.run_convert()
        e = self.by_summary(files["Personal.ics"][1], "Dentist")
        self.assertEqual(e["VALARM.TRIGGER"], [("", "-PT15M")])  # placeholder and disabled skipped
        stats = results[0][2]
        self.assertEqual(stats["alarms_default_placeholder_skipped"], 1)
        self.assertEqual(stats["alarms_disabled_skipped"], 1)
        e = self.by_summary(files["Personal.ics"][1], "Holiday")
        self.assertEqual(e["VALARM.TRIGGER"], [("", "-PT9H")])

    def test_all_day_and_floating(self):
        _, files, _ = self.run_convert()
        e = self.by_summary(files["Personal.ics"][1], "Holiday")
        self.assertEqual(one(e, "DTSTART"), ("VALUE=DATE", "20240710"))
        self.assertEqual(one(e, "DTEND"), ("VALUE=DATE", "20240712"))
        e = self.by_summary(files["Personal.ics"][1], "Floating")
        self.assertEqual(one(e, "DTSTART"), ("", "20240801T123000"))

    def test_recurrence_with_vtimezone(self):
        _, files, _ = self.run_convert()
        top, events = files["Personal.ics"]
        e = self.by_summary(events, "Weekly sync")
        self.assertEqual(one(e, "DTSTART"), ("TZID=Australia/Sydney", "20240205T080000"))
        self.assertEqual(one(e, "RRULE")[1], "FREQ=WEEKLY;UNTIL=20240630T115900Z;BYDAY=MO")
        self.assertEqual(one(e, "EXDATE"), ("TZID=Australia/Sydney", "20240219T080000"))
        moved = self.by_summary(events, "Moved sync")
        self.assertEqual(one(moved, "UID")[1], "uid-4")
        self.assertEqual(one(moved, "RECURRENCE-ID"), ("TZID=Australia/Sydney", "20240226T080000"))
        # Sydney 2024: AEDT ends first Sunday in April, starts first Sunday in October.
        joined = "\n".join(top)
        self.assertIn("TZID:Australia/Sydney", joined)
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=4;BYDAY=1SU", joined)
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=1SU", joined)
        self.assertIn("DTSTART:20240407T030000", joined)
        self.assertIn("DTSTART:20241006T020000", joined)
        self.assertIn("TZOFFSETFROM:+1100", joined)
        self.assertIn("TZOFFSETTO:+1000", joined)

    def test_london_vtimezone_and_count(self):
        _, files, _ = self.run_convert()
        top, events = files["Office.ics"]
        e = self.by_summary(events, "London standup")
        self.assertEqual(one(e, "DTSTART"), ("TZID=Europe/London", "20230301T090000"))
        self.assertEqual(one(e, "RRULE")[1], "FREQ=WEEKLY;INTERVAL=2;COUNT=5")
        joined = "\n".join(top)
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", joined)
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", joined)
        self.assertIn("DTSTART:20230326T010000", joined)
        self.assertIn("TZOFFSETTO:+0100", joined)
        self.assertNotIn("Australia/Sydney", joined)  # only zones used by recurring events

    def test_detached_without_recurring_master_is_standalone(self):
        _, files, _ = self.run_convert()
        events = files["Personal.ics"][1]
        e = self.by_summary(events, "One-off moved")
        self.assertEqual(one(e, "UID")[1], "uid-6/RID=1")
        self.assertNotIn("RECURRENCE-ID", e)
        self.by_summary(events, "Phantom occurrence")
        self.assertFalse([x for x in events if one(x, "UID")[1] == "uid-8"])

    def test_other_fields(self):
        _, files, _ = self.run_convert()
        events = files["Office.ics"][1]
        e = self.by_summary(events, "Fixed offset call")
        self.assertEqual(one(e, "DTSTART"), ("", "20240110T020000Z"))
        e = self.by_summary(events, "Mail link event")
        self.assertNotIn("URL", e)
        e = self.by_summary(events, "Cancelled drinks")
        self.assertEqual(one(e, "STATUS")[1], "CANCELLED")
        self.assertEqual(one(e, "TRANSP")[1], "TRANSPARENT")
        self.assertEqual(one(e, "X-MICROSOFT-CDO-BUSYSTATUS")[1], "FREE")
        e = self.by_summary(events, "Video catch-up")
        self.assertIn("Join: https://meet.example.com/abc", one(e, "DESCRIPTION")[1])
        e = self.by_summary(events, "Café")
        self.assertEqual(one(e, "SUMMARY")[1], "Café ☕ meeting " * 8)

    def test_attendee_modes(self):
        _, files, _ = self.run_convert(attendees="keep")
        e = self.by_summary(files["Personal.ics"][1], "Dentist")
        self.assertEqual(e["ORGANIZER"], [('CN="Alex Example"', "mailto:alex@example.com")])
        self.assertEqual(e["ATTENDEE"], [
            ('CN="Sam Sample";ROLE=REQ-PARTICIPANT;CUTYPE=INDIVIDUAL;RSVP=FALSE',
             "mailto:sam@example.org")])
        self.assertNotIn("Organiser:", one(e, "DESCRIPTION")[1])
        _, files, _ = self.run_convert(attendees="drop")
        e = self.by_summary(files["Personal.ics"][1], "Dentist")
        self.assertNotIn("ATTENDEE", e)
        self.assertNotIn("example.org", one(e, "DESCRIPTION")[1])

    def test_utc_only(self):
        _, files, _ = self.run_convert(utc_only=True)
        top, events = files["Personal.ics"]
        self.assertFalse([line for line in top if line.startswith("TZID")])
        e = self.by_summary(events, "Weekly sync")
        self.assertEqual(one(e, "DTSTART"), ("", "20240204T210000Z"))
        self.assertEqual(one(e, "EXDATE"), ("", "20240218T210000Z"))

    def test_zip_and_db_inputs(self):
        zpath = os.path.join(self.tmp.name, "Sample.icbu.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.write(os.path.join(self.icbu, "Calendar.sqlitedb"), "Sample.icbu/Calendar.sqlitedb")
            zf.writestr("__MACOSX/Sample.icbu/._Calendar.sqlitedb", b"junk")
        for source in (zpath, os.path.join(self.icbu, "Calendar.sqlitedb")):
            outdir = tempfile.mkdtemp(dir=self.tmp.name)
            results = unpack_icbu.convert(source, outdir, out=io.StringIO())
            self.assertEqual(len(results), 3)

    def test_console_output_has_no_event_text(self):
        _, _, printed = self.run_convert()
        buf = io.StringIO()
        with redirect_stdout(buf):
            unpack_icbu.main([self.icbu, "--list"])
        printed += buf.getvalue()
        self.assertIn("Personal", printed)
        for secret in make_fixture.PRIVATE_TEXT:
            self.assertNotIn(secret, printed)

    def test_duration_and_fold_helpers(self):
        d = unpack_icbu.duration
        self.assertEqual([d(0), d(-900), d(-32400), d(-205200), d(3661)],
                         ["PT0S", "-PT15M", "-PT9H", "-P2DT9H", "PT1H1M1S"])
        folded = unpack_icbu.fold("SUMMARY:" + "é" * 80)
        for part in folded.split("\r\n"):
            self.assertLessEqual(len(part.encode("utf-8")), 75)
        self.assertEqual(folded.replace("\r\n ", ""), "SUMMARY:" + "é" * 80)


if __name__ == "__main__":
    unittest.main()
