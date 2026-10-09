#!/usr/bin/env python3
"""Convert Apple Calendar archives (.icbu) to iCalendar (.ics) files.

Modern .icbu archives hold an EventKit SQLite database (Calendar.sqlitedb) rather
than .ics files, and the database keeps no copy of the original iCalendar text, so
each event is rebuilt from the database columns. Standard library only.

The progress and summary output contains counts and calendar names only, never
event text, so a conversion can be run and checked without reading the calendar.
"""

import argparse
import calendar as calmod
import os
import re
import sqlite3
import sys
import tempfile
import urllib.parse
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

__version__ = "0.1.0"

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
ENTITY_EVENT = 2
FREQ = {1: "DAILY", 2: "WEEKLY", 3: "MONTHLY", 4: "YEARLY"}
STATUS = {1: "CONFIRMED", 2: "TENTATIVE", 3: "CANCELLED"}
ROLE = {1: "REQ-PARTICIPANT", 2: "OPT-PARTICIPANT", 3: "CHAIR"}
PARTICIPANT_ORGANIZER = 8
PARTICIPANT_ATTENDEE = 7
WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# --- opening the archive -----------------------------------------------------

def find_database(path, tmpdir):
    """Return the path of Calendar.sqlitedb for an .icbu folder, .icbu zip, or the db itself."""
    if os.path.isdir(path):
        db = os.path.join(path, "Calendar.sqlitedb")
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist()
                     if n.endswith("Calendar.sqlitedb") and not n.startswith("__MACOSX/")]
            if len(names) != 1:
                raise SystemExit(f"error: expected one Calendar.sqlitedb in the zip, found {len(names)}")
            db = os.path.join(tmpdir, "Calendar.sqlitedb")
            with zf.open(names[0]) as src, open(db, "wb") as dst:
                dst.write(src.read())
    else:
        db = path
    if not os.path.isfile(db):
        raise SystemExit(f"error: no Calendar.sqlitedb at {db}")
    return db


def connect(db):
    """Open the database read-only without touching it (no journal or lock files)."""
    uri = "file:" + urllib.parse.quote(os.path.abspath(db)) + "?immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def select(conn, table, wanted, where=""):
    """SELECT the wanted columns, substituting NULL for any this schema version lacks."""
    have = columns(conn, table)
    cols = ", ".join(c if c in have else f"NULL AS {c}" for c in wanted)
    return conn.execute(f"SELECT {cols} FROM {table} {where}").fetchall()


class Archive:
    def __init__(self, conn):
        self.calendars = select(conn, "Calendar", ["ROWID", "title", "store_id"])
        self.stores = {r["ROWID"]: r["name"] for r in select(conn, "Store", ["ROWID", "name"])}
        self.items = {r["ROWID"]: r for r in select(conn, "CalendarItem", [
            "ROWID", "summary", "description", "location_id", "start_date", "start_tz",
            "end_date", "all_day", "calendar_id", "orig_item_id", "orig_date", "status",
            "availability", "url", "last_modified", "creation_date", "sequence_num",
            "unique_identifier", "hidden", "phantom_master", "conference_url", "entity_type",
        ])}
        self.locations = {r["ROWID"]: r for r in select(
            conn, "Location", ["ROWID", "title", "address", "latitude", "longitude"])}
        self.recurrences = defaultdict(list)
        for r in select(conn, "Recurrence", ["owner_id", "frequency", "interval", "count",
                                             "end_date", "specifier", "by_month_months"]):
            self.recurrences[r["owner_id"]].append(r)
        self.exdates = defaultdict(list)
        for r in select(conn, "ExceptionDate", ["owner_id", "date"]):
            self.exdates[r["owner_id"]].append(r["date"])
        self.alarms = defaultdict(list)
        for r in select(conn, "Alarm", ["calendaritem_owner_id", "trigger_interval",
                                        "trigger_date", "disabled", "default_alarm"]):
            self.alarms[r["calendaritem_owner_id"]].append(r)
        identities = {r["ROWID"]: r for r in select(
            conn, "Identity", ["ROWID", "display_name", "address", "first_name", "last_name"])}
        self.participants = defaultdict(list)
        for r in select(conn, "Participant", ["ROWID", "owner_id", "entity_type", "type",
                                              "role", "email", "identity_id"],
                        "ORDER BY ROWID"):
            self.participants[r["owner_id"]].append((r, identities.get(r["identity_id"])))
        self.attachments = Counter(r["owner_id"] for r in select(conn, "Attachment", ["owner_id"]))

    def event_counts(self):
        return Counter(i["calendar_id"] for i in self.items.values() if is_event(i))


def is_event(item):
    return item["entity_type"] in (None, ENTITY_EVENT)


# --- value formatting --------------------------------------------------------

def apple_dt(ts):
    return APPLE_EPOCH + timedelta(seconds=ts)


_zone_cache = {}


def iana_zone(name):
    """ZoneInfo for a real IANA zone name, else None (covers _float, GMT, 'GMT+1100')."""
    if not name or name in ("_float", "GMT", "UTC"):
        return None
    if name not in _zone_cache:
        try:
            _zone_cache[name] = ZoneInfo(name)
        except Exception:
            _zone_cache[name] = None
    return _zone_cache[name]


class TimeMode:
    """How an event's times are written: DATE, floating, TZID or UTC."""

    def __init__(self, kind, tz=None):
        self.kind, self.tz = kind, tz

    def date_of(self, ts, item_tz):
        dt = apple_dt(ts)
        zone = iana_zone(item_tz)
        return (dt.astimezone(zone) if zone else dt).date()

    def fmt(self, ts, item_tz=None):
        """Return (params, value) for a DTSTART-like property."""
        if self.kind == "date":
            return "VALUE=DATE", self.date_of(ts, item_tz).strftime("%Y%m%d")
        dt = apple_dt(ts)
        if self.kind == "floating":
            return "", dt.strftime("%Y%m%dT%H%M%S")
        if self.kind == "tzid":
            return "TZID=" + self.tz, dt.astimezone(ZoneInfo(self.tz)).strftime("%Y%m%dT%H%M%S")
        return "", dt.strftime("%Y%m%dT%H%M%SZ")

    def until(self, ts, item_tz):
        # RFC 5545: UNTIL is a DATE for DATE starts, local for floating starts, else UTC.
        if self.kind in ("date", "floating"):
            return self.fmt(ts, item_tz)[1]
        return apple_dt(ts).strftime("%Y%m%dT%H%M%SZ")


def time_mode(item, recurring, utc_only):
    if item["all_day"]:
        return TimeMode("date")
    if item["start_tz"] in (None, "", "_float"):
        return TimeMode("floating")
    if recurring and not utc_only and iana_zone(item["start_tz"]):
        return TimeMode("tzid", item["start_tz"])
    return TimeMode("utc")


def utc_stamp(ts):
    return apple_dt(ts).strftime("%Y%m%dT%H%M%SZ")


def duration(secs):
    secs = int(round(secs))
    if secs == 0:
        return "PT0S"
    sign, s = ("-" if secs < 0 else ""), abs(secs)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    t = (f"{h}H" if h else "") + (f"{m}M" if m else "") + (f"{s}S" if s else "")
    return sign + "P" + (f"{d}D" if d else "") + ("T" + t if t else "")


def utc_offset(td):
    secs = int(td.total_seconds())
    sign = "-" if secs < 0 else "+"
    h, m = divmod(abs(secs) // 60, 60)
    return f"{sign}{h:02d}{m:02d}"


def text(value):
    value = str(value).replace("\r\n", "\n").replace("\r", "\n")
    value = CONTROL_CHARS.sub("", value)
    return (value.replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def param_text(value):
    return '"' + CONTROL_CHARS.sub("", str(value)).replace('"', "'").replace("\n", " ") + '"'


def fold(line):
    """Fold to 75 octets per line without splitting a UTF-8 character."""
    if len(line.encode("utf-8")) <= 75:
        return line
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > 75:
            out.append(cur)
            cur, size = " ", 1
        cur += ch
        size += n
    out.append(cur)
    return "\r\n".join(out)


def prop(name, value, params=""):
    return fold(f"{name};{params}:{value}" if params else f"{name}:{value}")


# --- time zones --------------------------------------------------------------

def vtimezone(tzname, year):
    """Build a VTIMEZONE from the zone's transitions in the given year, as yearly rules."""
    zone = ZoneInfo(tzname)
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    step = timedelta(hours=6)
    transitions = []
    t, prev = start, start.astimezone(zone).utcoffset()
    while t < end:
        nxt = t + step
        off = nxt.astimezone(zone).utcoffset()
        if off != prev:
            lo, hi = t, nxt
            while hi - lo > timedelta(minutes=1):
                mid = lo + (hi - lo) / 2
                if mid.astimezone(zone).utcoffset() == prev:
                    lo = mid
                else:
                    hi = mid
            transitions.append(hi.replace(second=0, microsecond=0))
            prev = off
        t = nxt

    lines = ["BEGIN:VTIMEZONE", prop("TZID", tzname)]
    if not transitions:
        local = start.astimezone(zone)
        off = utc_offset(local.utcoffset())
        lines += ["BEGIN:STANDARD", "DTSTART:19700101T000000", "TZOFFSETFROM:" + off,
                  "TZOFFSETTO:" + off, prop("TZNAME", text(local.tzname())), "END:STANDARD"]
    for tr in transitions:
        before = (tr - timedelta(minutes=1)).astimezone(zone)
        after = tr.astimezone(zone)
        wall = (tr + before.utcoffset()).replace(tzinfo=None)
        kind = "DAYLIGHT" if after.dst() else "STANDARD"
        days_in_month = calmod.monthrange(wall.year, wall.month)[1]
        nth = -1 if wall.day + 7 > days_in_month else (wall.day - 1) // 7 + 1
        lines += [
            "BEGIN:" + kind,
            "DTSTART:" + wall.strftime("%Y%m%dT%H%M%S"),
            "TZOFFSETFROM:" + utc_offset(before.utcoffset()),
            "TZOFFSETTO:" + utc_offset(after.utcoffset()),
            prop("TZNAME", text(after.tzname())),
            f"RRULE:FREQ=YEARLY;BYMONTH={wall.month};BYDAY={nth}{WEEKDAYS[wall.weekday()]}",
            "END:" + kind,
        ]
    lines.append("END:VTIMEZONE")
    return lines


# --- events ------------------------------------------------------------------

def rrule(rec, mode, item_tz, stats):
    freq = FREQ.get(rec["frequency"])
    if not freq:
        stats["warn_unknown_frequency"] += 1
        return None
    parts = ["FREQ=" + freq]
    if rec["interval"] and rec["interval"] > 1:
        parts.append(f"INTERVAL={int(rec['interval'])}")
    if rec["count"] and rec["count"] > 0:
        parts.append(f"COUNT={int(rec['count'])}")
    elif rec["end_date"] is not None:
        parts.append("UNTIL=" + mode.until(rec["end_date"], item_tz))
    for part in (rec["specifier"] or "").split(";"):
        key, _, value = part.partition("=")
        if not key:
            continue
        if key == "D":
            days = []
            for d in value.split(","):
                m = re.fullmatch(r"([+-]?\d+)?([A-Z]{2})", d.strip())
                if not m:
                    stats["warn_unparsed_rrule_day"] += 1
                    continue
                n = int(m.group(1) or 0)
                days.append((str(n) if n else "") + m.group(2))
            if days:
                parts.append("BYDAY=" + ",".join(days))
        else:
            # Other specifier keys aren't verified against real data yet; leaving them out
            # falls back to the RRULE defaults taken from DTSTART.
            stats["warn_rrule_part_left_out"] += 1
    if rec["by_month_months"]:
        stats["warn_rrule_part_left_out"] += 1
    return ";".join(parts)


def person(part, ident):
    name = ""
    email = part["email"] or ""
    if ident is not None:
        name = ident["display_name"] or " ".join(
            x for x in (ident["first_name"], ident["last_name"]) if x)
        if not email and ident["address"]:
            email = re.sub(r"^mailto:", "", ident["address"], flags=re.I)
    return name.strip(), email.strip()


def person_label(name, email):
    if name and email:
        return f"{name} <{email}>"
    return name or email


class Converter:
    def __init__(self, archive, attendees="describe", utc_only=False):
        self.a = archive
        self.attendees = attendees
        self.utc_only = utc_only

    def recurring(self, item_id, stats=None):
        """RRULE strings for an item (empty if it doesn't recur)."""
        item = self.a.items.get(item_id)
        if item is None or self.skip_reason(item):
            return []
        mode = time_mode(item, True, self.utc_only)
        rules = [rrule(r, mode, item["start_tz"], stats if stats is not None else Counter())
                 for r in self.a.recurrences.get(item_id, [])]
        return [r for r in rules if r]

    @staticmethod
    def skip_reason(item):
        if item["hidden"] and item["phantom_master"]:
            return "phantom_master"
        if item["start_date"] is None:
            return "no_start"
        return None

    def calendar_lines(self, cal, stats):
        items = [i for i in self.a.items.values() if i["calendar_id"] == cal["ROWID"] and is_event(i)]
        items.sort(key=lambda i: (bool(i["orig_item_id"]), i["start_date"] or 0, i["ROWID"]))
        events, zones = [], {}
        for item in items:
            reason = self.skip_reason(item)
            if reason:
                stats["skipped_" + reason] += 1
                continue
            lines = self.event_lines(item, stats, zones)
            events.extend(lines)
            stats["events_written"] += 1

        out = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//unpack-icbu//{__version__}//EN",
               "CALSCALE:GREGORIAN", prop("X-WR-CALNAME", text(cal["title"] or ""))]
        for tz in sorted(zones):
            out += vtimezone(tz, zones[tz])
        return out + events + ["END:VCALENDAR"]

    def event_lines(self, item, stats, zones):
        rules = self.recurring(item["ROWID"], stats)
        mode = time_mode(item, bool(rules), self.utc_only)
        uid = item["unique_identifier"] or f"unpack-icbu-{item['ROWID']}@localhost"
        if not item["unique_identifier"]:
            stats["generated_uid"] += 1

        L = ["BEGIN:VEVENT"]
        recurrence_id = None
        master = self.a.items.get(item["orig_item_id"]) if item["orig_item_id"] else None
        if master is not None and self.recurring(master["ROWID"]) and item["orig_date"] is not None:
            master_mode = time_mode(master, True, self.utc_only)
            uid = master["unique_identifier"] or f"unpack-icbu-{master['ROWID']}@localhost"
            recurrence_id = master_mode.fmt(item["orig_date"], master["start_tz"])
            self.note_zone(master_mode, master, zones)
            stats["detached_occurrences"] += 1
        elif item["orig_item_id"]:
            stats["detached_standalone"] += 1

        L.append(prop("UID", text(uid)))
        L.append(prop("DTSTAMP", utc_stamp(item["last_modified"]) if item["last_modified"] is not None
                      else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
        if item["creation_date"] is not None:
            L.append(prop("CREATED", utc_stamp(item["creation_date"])))
        if item["last_modified"] is not None:
            L.append(prop("LAST-MODIFIED", utc_stamp(item["last_modified"])))

        params, value = mode.fmt(item["start_date"], item["start_tz"])
        L.append(prop("DTSTART", value, params))
        self.note_zone(mode, item, zones)
        end = item["end_date"]
        if mode.kind == "date":
            span = (end - item["start_date"] + 1) if end is not None else 86400
            days = max(1, int(round(span / 86400)))
            start_day = mode.date_of(item["start_date"], item["start_tz"])
            L.append(prop("DTEND", (start_day + timedelta(days=days)).strftime("%Y%m%d"), "VALUE=DATE"))
        elif end is not None and end >= item["start_date"]:
            params, value = mode.fmt(end, item["start_tz"])
            L.append(prop("DTEND", value, params))
        if recurrence_id:
            L.append(prop("RECURRENCE-ID", recurrence_id[1], recurrence_id[0]))

        if rules:
            stats["recurring_events"] += 1
            L += [prop("RRULE", r) for r in rules]
            for ex in self.a.exdates.get(item["ROWID"], []):
                params, value = mode.fmt(ex, item["start_tz"])
                L.append(prop("EXDATE", value, params))
                stats["exdates"] += 1

        if item["summary"]:
            L.append(prop("SUMMARY", text(item["summary"])))
        description = self.description(item, stats)
        if description:
            L.append(prop("DESCRIPTION", text(description)))
        self.location(item, L)

        url = (item["url"] or "").strip()
        if re.match(r"https?://", url, re.I):
            L.append(prop("URL", CONTROL_CHARS.sub("", url).replace("\n", "")))
        elif url:
            stats["urls_dropped_non_web"] += 1

        if item["status"] in STATUS:
            L.append(prop("STATUS", STATUS[item["status"]]))
        if item["availability"] == 1:
            L += ["TRANSP:TRANSPARENT", "X-MICROSOFT-CDO-BUSYSTATUS:FREE"]
        else:
            L.append("TRANSP:OPAQUE")
        if item["sequence_num"] and item["sequence_num"] > 0:
            L.append(prop("SEQUENCE", str(int(item["sequence_num"]))))

        if self.attendees == "keep":
            L += self.attendee_props(item, stats)
        L += self.alarm_lines(item, stats)
        L.append("END:VEVENT")
        return L

    @staticmethod
    def note_zone(mode, item, zones):
        if mode.kind == "tzid":
            year = apple_dt(item["start_date"]).year
            zones[mode.tz] = min(year, zones.get(mode.tz, year))

    def description(self, item, stats):
        extras = []
        if item["conference_url"]:
            extras.append("Join: " + item["conference_url"].strip())
        if self.attendees == "describe":
            organiser, attendees = None, []
            for part, ident in self.a.participants.get(item["ROWID"], []):
                label = person_label(*person(part, ident))
                if not label:
                    continue
                if part["entity_type"] == PARTICIPANT_ORGANIZER:
                    organiser = label
                else:
                    attendees.append(label)
            if organiser:
                extras.append("Organiser: " + organiser)
            if attendees:
                extras.append("Attendees:\n" + "\n".join("- " + a for a in attendees))
            if organiser or attendees:
                stats["events_attendees_in_notes"] += 1
        n = self.a.attachments.get(item["ROWID"], 0)
        if n:
            extras.append(f"This event had {n} attachment{'s' if n != 1 else ''} in Apple Calendar; "
                          "they are not included in this file.")
            stats["events_attachment_note"] += 1
        body = (item["description"] or "").strip()
        if not extras:
            return body
        return (body + "\n\n" if body else "") + "\n".join(extras)

    def location(self, item, L):
        loc = self.a.locations.get(item["location_id"]) if item["location_id"] else None
        if loc is None:
            return
        title = (loc["title"] or "").strip()
        address = re.sub(r"\s*\n\s*", ", ", (loc["address"] or "").strip())
        value = title
        if address and address not in title:
            value = f"{title}, {address}" if title else address
        if value:
            L.append(prop("LOCATION", text(value)))
        if loc["latitude"] and loc["longitude"]:
            L.append(prop("GEO", f"{loc['latitude']:.6f};{loc['longitude']:.6f}"))

    def attendee_props(self, item, stats):
        out = []
        for part, ident in self.a.participants.get(item["ROWID"], []):
            name, email = person(part, ident)
            if not email:
                stats["attendees_without_email_skipped"] += 1
                continue
            params = [f"CN={param_text(name)}"] if name else []
            if part["entity_type"] == PARTICIPANT_ORGANIZER:
                out.append(prop("ORGANIZER", "mailto:" + email, ";".join(params)))
                continue
            if part["role"] in ROLE:
                params.append("ROLE=" + ROLE[part["role"]])
            if part["type"] == 1:
                params.append("CUTYPE=INDIVIDUAL")
            params.append("RSVP=FALSE")
            out.append(prop("ATTENDEE", "mailto:" + email, ";".join(params)))
        return out

    def alarm_lines(self, item, stats):
        out = []
        for al in self.a.alarms.get(item["ROWID"], []):
            if al["disabled"]:
                stats["alarms_disabled_skipped"] += 1
                continue
            if al["trigger_interval"] is not None:
                trigger = prop("TRIGGER", duration(al["trigger_interval"]))
            elif al["trigger_date"] is not None:
                trigger = prop("TRIGGER", utc_stamp(al["trigger_date"]), "VALUE=DATE-TIME")
            else:
                stats["alarms_default_placeholder_skipped"] += 1
                continue
            out += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder", trigger, "END:VALARM"]
            stats["alarms_written"] += 1
        return out


# --- command line ------------------------------------------------------------

def safe_name(title, cal_id):
    name = re.sub(r"[^\w.-]+", "_", title or "").strip("._")
    return name or f"calendar-{cal_id}"


def select_calendars(archive, names, ids):
    counts = archive.event_counts()
    if not names and not ids:
        return [c for c in archive.calendars if counts.get(c["ROWID"])]
    chosen = [c for c in archive.calendars
              if c["ROWID"] in ids or (c["title"] or "").strip() in names]
    found = {(c["title"] or "").strip() for c in chosen} | {c["ROWID"] for c in chosen}
    missing = [n for n in list(names) + list(ids) if n not in found]
    if missing:
        raise SystemExit(f"error: no calendar matching {missing}; use --list")
    return chosen


def load_archive(input_path):
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(find_database(input_path, tmp))
        try:
            return Archive(conn)
        finally:
            conn.close()


def convert(input_path, outdir, names=(), ids=(), attendees="describe", utc_only=False, out=None):
    """Convert the selected calendars. Returns [(calendar row, output path, stats)]."""
    out = out or sys.stdout
    archive = load_archive(input_path)
    conv = Converter(archive, attendees=attendees, utc_only=utc_only)
    os.makedirs(outdir, exist_ok=True)
    results, used = [], set()
    for cal in select_calendars(archive, names, ids):
        stats = Counter()
        lines = conv.calendar_lines(cal, stats)
        base = safe_name(cal["title"], cal["ROWID"])
        if base in used:
            base = f"{base}-{cal['ROWID']}"
        used.add(base)
        path = os.path.join(outdir, base + ".ics")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\r\n".join(lines) + "\r\n")
        results.append((cal, path, stats))
        detail = " ".join(f"{k}={v}" for k, v in sorted(stats.items()) if k != "events_written")
        print(f"[{cal['ROWID']}] {cal['title']}: {stats['events_written']} events -> "
              f"{os.path.basename(path)}" + (f" ({detail})" if detail else ""), file=out)
    return results


def list_calendars(input_path, out=None):
    out = out or sys.stdout
    archive = load_archive(input_path)
    counts = archive.event_counts()
    print("id\tevents\tstore\ttitle", file=out)
    for c in archive.calendars:
        print(f"{c['ROWID']}\t{counts.get(c['ROWID'], 0)}\t{archive.stores.get(c['store_id'], '')}"
              f"\t{c['title']}", file=out)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="unpack-icbu",
        description="Convert an Apple Calendar archive (.icbu) into one .ics file per calendar.")
    p.add_argument("input", help=".icbu folder, zipped .icbu, or Calendar.sqlitedb")
    p.add_argument("-o", "--outdir", default="ics", help="output folder (default: ./ics)")
    p.add_argument("--list", action="store_true", help="list calendars and event counts, then exit")
    p.add_argument("--calendar", action="append", default=[], metavar="NAME",
                   help="convert only this calendar (repeatable)")
    p.add_argument("--calendar-id", action="append", default=[], type=int, metavar="ID",
                   help="convert only this calendar id from --list (repeatable)")
    p.add_argument("--attendees", choices=["describe", "keep", "drop"], default="describe",
                   help="describe: list in the notes (default; nobody gets emailed on import); "
                        "keep: ORGANIZER/ATTENDEE properties; drop: leave out")
    p.add_argument("--utc-only", action="store_true",
                   help="write all timed events in UTC, with no VTIMEZONE")
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)
    if args.list:
        list_calendars(args.input)
        return 0
    convert(args.input, args.outdir, args.calendar, args.calendar_id, args.attendees, args.utc_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
