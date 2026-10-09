"""Build a small synthetic Calendar.sqlitedb shaped like an Apple Calendar archive.

Everything here is made up (example.com addresses, invented names). Only the tables
and columns unpack_icbu reads are created. Run directly to write a fixture archive:

    python3 tests/make_fixture.py /tmp/Sample.icbu
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

SCHEMA = """
CREATE TABLE Store (ROWID INTEGER PRIMARY KEY, name TEXT, type INTEGER);
CREATE TABLE Calendar (ROWID INTEGER PRIMARY KEY, store_id INTEGER, title TEXT);
CREATE TABLE CalendarItem (ROWID INTEGER PRIMARY KEY, summary TEXT, location_id INTEGER,
  description TEXT, start_date REAL, start_tz TEXT, end_date REAL, end_tz TEXT, all_day INTEGER,
  calendar_id INTEGER, orig_item_id INTEGER DEFAULT 0, orig_date REAL, status INTEGER DEFAULT 0,
  availability INTEGER DEFAULT 0, url TEXT, last_modified REAL, sequence_num INTEGER DEFAULT 0,
  unique_identifier TEXT, hidden INTEGER DEFAULT 0, entity_type INTEGER DEFAULT 2,
  creation_date REAL, phantom_master INTEGER, conference_url TEXT);
CREATE TABLE Location (ROWID INTEGER PRIMARY KEY, title TEXT, address TEXT, latitude REAL,
  longitude REAL, item_owner_id INTEGER);
CREATE TABLE Identity (ROWID INTEGER PRIMARY KEY, display_name TEXT, address TEXT,
  first_name TEXT, last_name TEXT);
CREATE TABLE Participant (ROWID INTEGER PRIMARY KEY, entity_type INTEGER, type INTEGER,
  status INTEGER, role INTEGER, identity_id INTEGER, owner_id INTEGER, email TEXT, is_self INTEGER);
CREATE TABLE Recurrence (ROWID INTEGER PRIMARY KEY, frequency INTEGER, interval INTEGER,
  week_start INTEGER, count INTEGER, end_date REAL, specifier TEXT, by_month_months INTEGER,
  owner_id INTEGER);
CREATE TABLE ExceptionDate (ROWID INTEGER PRIMARY KEY, owner_id INTEGER, date REAL);
CREATE TABLE Alarm (ROWID INTEGER PRIMARY KEY, trigger_date REAL, trigger_interval INTEGER,
  type INTEGER, calendaritem_owner_id INTEGER, disabled INTEGER DEFAULT 0, default_alarm INTEGER DEFAULT 0);
CREATE TABLE Attachment (ROWID INTEGER PRIMARY KEY, owner_id INTEGER, file_id INTEGER);
"""


def ts(*args, tz=timezone.utc):
    """Apple timestamp for a datetime given as (y, m, d, H, M) in tz (UTC by default)."""
    return (datetime(*args, tzinfo=tz) - APPLE_EPOCH).total_seconds()


def insert(conn, table, **row):
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})",
                 list(row.values()))


# Text that must never appear in the tool's console output.
PRIVATE_TEXT = [
    "Dentist", "bring card", "Line two", "Harbour Clinic", "1 Example Street", "Alex Example",
    "alex@example.com", "Sam Sample", "sam@example.org", "Weekly sync", "Moved sync",
    "One-off moved", "Holiday at the beach house", "Floating lunch", "Fixed offset call", "Café",
    "Mail link event", "Cancelled drinks", "London standup", "Video catch-up",
]


def build(path):
    """Write the fixture database to path (a .sqlitedb file)."""
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    insert(conn, "Store", ROWID=1, name="iCloud", type=2)
    insert(conn, "Calendar", ROWID=1, store_id=1, title="Personal")
    insert(conn, "Calendar", ROWID=2, store_id=1, title="Office")
    insert(conn, "Calendar", ROWID=3, store_id=1, title="Empty")
    insert(conn, "Calendar", ROWID=4, store_id=1, title="Office")  # duplicate name

    created = ts(2024, 1, 1, 0, 0)

    # 1. Timed one-off in Sydney, with everything attached.
    insert(conn, "Location", ROWID=1, title="Harbour Clinic", address="1 Example Street\nSydney",
           latitude=-33.87, longitude=151.21)
    insert(conn, "CalendarItem", ROWID=1, calendar_id=1, summary="Dentist, check-up; bring card",
           description="Line one\nLine two", location_id=1, start_date=ts(2024, 3, 5, 21, 0),
           end_date=ts(2024, 3, 5, 22, 0), start_tz="Australia/Sydney", end_tz="Australia/Sydney",
           all_day=0, url="https://example.com/booking", last_modified=created + 60,
           creation_date=created, sequence_num=2, unique_identifier="uid-1", status=1)
    insert(conn, "Identity", ROWID=1, display_name="Alex Example", address="mailto:alex@example.com")
    insert(conn, "Identity", ROWID=2, display_name="Sam Sample", address=None)
    insert(conn, "Identity", ROWID=3, display_name="No Email Person", address=None)
    insert(conn, "Participant", ROWID=1, owner_id=1, entity_type=8, type=0, role=0, identity_id=1)
    insert(conn, "Participant", ROWID=2, owner_id=1, entity_type=7, type=1, role=1, identity_id=2,
           email="sam@example.org")
    insert(conn, "Participant", ROWID=3, owner_id=1, entity_type=7, type=1, role=2, identity_id=3)
    insert(conn, "Alarm", ROWID=1, calendaritem_owner_id=1, trigger_interval=-900)
    insert(conn, "Alarm", ROWID=2, calendaritem_owner_id=1, default_alarm=1)  # placeholder
    insert(conn, "Alarm", ROWID=3, calendaritem_owner_id=1, trigger_interval=-60, disabled=1)
    insert(conn, "Attachment", ROWID=1, owner_id=1, file_id=1)
    insert(conn, "Attachment", ROWID=2, owner_id=1, file_id=2)

    # 2. Two-day all-day floating event (stored as midnight, end = start + 2 days - 1 s).
    insert(conn, "CalendarItem", ROWID=2, calendar_id=1, summary="Holiday at the beach house",
           start_date=ts(2024, 7, 10, 0, 0), end_date=ts(2024, 7, 10, 0, 0) + 172799,
           start_tz="_float", end_tz="_float", all_day=1, unique_identifier="uid-2")
    insert(conn, "Alarm", ROWID=4, calendaritem_owner_id=2, trigger_interval=-32400)

    # 3. Floating timed event.
    insert(conn, "CalendarItem", ROWID=3, calendar_id=1, summary="Floating lunch",
           start_date=ts(2024, 8, 1, 12, 30), end_date=ts(2024, 8, 1, 13, 30),
           start_tz="_float", end_tz="_float", all_day=0, unique_identifier="uid-3")

    # 4. Weekly recurring on Mondays in Sydney, until a date, with one exception
    #    and one moved occurrence (detached, UID differs from the master's as in real archives).
    insert(conn, "CalendarItem", ROWID=4, calendar_id=1, summary="Weekly sync",
           start_date=ts(2024, 2, 4, 21, 0), end_date=ts(2024, 2, 4, 21, 30),
           start_tz="Australia/Sydney", all_day=0, unique_identifier="uid-4")
    insert(conn, "Recurrence", ROWID=1, owner_id=4, frequency=2, interval=1, specifier="D=0MO",
           end_date=ts(2024, 6, 30, 11, 59))
    insert(conn, "ExceptionDate", ROWID=1, owner_id=4, date=ts(2024, 2, 18, 21, 0))
    insert(conn, "CalendarItem", ROWID=5, calendar_id=1, summary="Moved sync",
           start_date=ts(2024, 2, 26, 1, 0), end_date=ts(2024, 2, 26, 1, 30),
           start_tz="Australia/Sydney", all_day=0, unique_identifier="uid-4/RID=730000000",
           orig_item_id=4, orig_date=ts(2024, 2, 25, 21, 0))

    # 5. Detached occurrence whose master doesn't recur: stays standalone.
    insert(conn, "CalendarItem", ROWID=6, calendar_id=1, summary="Plain master",
           start_date=ts(2024, 4, 1, 0, 0), end_date=ts(2024, 4, 1, 1, 0),
           start_tz="Australia/Sydney", all_day=0, unique_identifier="uid-6")
    insert(conn, "CalendarItem", ROWID=7, calendar_id=1, summary="One-off moved",
           start_date=ts(2024, 4, 2, 0, 0), end_date=ts(2024, 4, 2, 1, 0),
           start_tz="Australia/Sydney", all_day=0, unique_identifier="uid-6/RID=1",
           orig_item_id=6, orig_date=ts(2024, 4, 1, 0, 0))

    # 6. Hidden phantom master (skipped) with its real occurrence.
    insert(conn, "CalendarItem", ROWID=8, calendar_id=1, summary="Phantom",
           start_date=ts(2024, 5, 1, 0, 0), end_date=ts(2024, 5, 1, 1, 0),
           start_tz="Australia/Sydney", all_day=0, unique_identifier="uid-8",
           hidden=1, phantom_master=1)
    insert(conn, "CalendarItem", ROWID=9, calendar_id=1, summary="Phantom occurrence",
           start_date=ts(2024, 5, 1, 0, 0), end_date=ts(2024, 5, 1, 1, 0),
           start_tz="Australia/Sydney", all_day=0, unique_identifier="uid-8/RID=2",
           orig_item_id=8, orig_date=ts(2024, 5, 1, 0, 0))

    # 7. Fixed-offset zone name that isn't IANA: written in UTC.
    insert(conn, "CalendarItem", ROWID=10, calendar_id=2, summary="Fixed offset call",
           start_date=ts(2024, 1, 10, 2, 0), end_date=ts(2024, 1, 10, 3, 0),
           start_tz="GMT+1100", all_day=0, unique_identifier="uid-10")

    # 8. Long multibyte summary (folding).
    insert(conn, "CalendarItem", ROWID=11, calendar_id=2, summary="Café ☕ meeting " * 8,
           start_date=ts(2024, 1, 11, 2, 0), end_date=ts(2024, 1, 11, 3, 0),
           start_tz="Australia/Sydney", all_day=0, unique_identifier="uid-11")

    # 9. Non-web URL (dropped); free time; cancelled.
    insert(conn, "CalendarItem", ROWID=12, calendar_id=2, summary="Mail link event",
           start_date=ts(2024, 1, 12, 2, 0), end_date=ts(2024, 1, 12, 3, 0),
           start_tz="Australia/Sydney", all_day=0, unique_identifier="uid-12",
           url="message:%3Cabc@example.com%3E")
    insert(conn, "CalendarItem", ROWID=13, calendar_id=2, summary="Cancelled drinks",
           start_date=ts(2024, 1, 13, 2, 0), end_date=ts(2024, 1, 13, 3, 0),
           start_tz="Australia/Sydney", all_day=0, unique_identifier="uid-13",
           status=3, availability=1)

    # 10. Fortnightly London event, 5 occurrences (northern hemisphere VTIMEZONE).
    insert(conn, "CalendarItem", ROWID=14, calendar_id=2, summary="London standup",
           start_date=ts(2023, 3, 1, 9, 0), end_date=ts(2023, 3, 1, 9, 15),
           start_tz="Europe/London", all_day=0, unique_identifier="uid-14")
    insert(conn, "Recurrence", ROWID=2, owner_id=14, frequency=2, interval=2, count=5)

    # 11. Conference link; 12. a reminder (not an event) that must be ignored.
    insert(conn, "CalendarItem", ROWID=15, calendar_id=2, summary="Video catch-up",
           start_date=ts(2024, 1, 15, 2, 0), end_date=ts(2024, 1, 15, 3, 0),
           start_tz="GMT", all_day=0, unique_identifier="uid-15",
           conference_url="https://meet.example.com/abc")
    insert(conn, "CalendarItem", ROWID=16, calendar_id=2, summary="A reminder",
           start_date=ts(2024, 1, 16, 2, 0), start_tz="GMT", all_day=0,
           unique_identifier="uid-16", entity_type=3)

    # 13. Second calendar named "Office" (file name clash).
    insert(conn, "CalendarItem", ROWID=17, calendar_id=4, summary="Other office",
           start_date=ts(2024, 1, 17, 2, 0), end_date=ts(2024, 1, 17, 3, 0),
           start_tz="GMT", all_day=0, unique_identifier="uid-17")
    conn.commit()
    conn.close()


def build_icbu(folder):
    """Write a fixture .icbu folder; returns its path."""
    os.makedirs(folder, exist_ok=True)
    build(os.path.join(folder, "Calendar.sqlitedb"))
    with open(os.path.join(folder, "Info.plist"), "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
                "<key>version</key><string>3.0</string></dict></plist>\n")
    return folder


if __name__ == "__main__":
    print(build_icbu(sys.argv[1] if len(sys.argv) > 1 else "Sample.icbu"))
