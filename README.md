# unpack-icbu

Convert Apple Calendar archives (`.icbu`) into standard iCalendar (`.ics`) files, one per
calendar, ready to import into Outlook, Google Calendar or anything else that reads `.ics`.

Calendar on macOS makes an archive with **File › Export › Calendar Archive…**. Current versions
store the calendars in an SQLite database inside the archive (`Calendar.sqlitedb`), not as
`.ics` files, and nothing on the Mac exports them back out in bulk. This tool rebuilds each
event from that database.

Python 3.9 or later, standard library only.

## Use

```bash
# See what's in the archive (an .icbu folder, a zipped .icbu, or the Calendar.sqlitedb itself)
python3 unpack_icbu.py "Calendars.icbu" --list

# Convert every calendar that has events into ./ics/
python3 unpack_icbu.py "Calendars.icbu"

# Convert chosen calendars into a chosen folder
python3 unpack_icbu.py "Calendars.icbu.zip" -o out --calendar Home --calendar Work
python3 unpack_icbu.py "Calendars.icbu" -o out --calendar-id 10 --calendar-id 11
```

Or install it with `pip install .` and run `unpack-icbu`.

The archive is opened read-only and never modified. The console output shows calendar names and
counts only, never event text, so a conversion can be run and checked without reading anyone's
calendar.

### Options

| Option | |
|---|---|
| `--attendees describe` | Default. Organiser and attendees are written into the event notes as text. The `.ics` has no `ORGANIZER`/`ATTENDEE` properties, so the importing calendar treats the events as plain appointments and never emails anyone when they are edited or deleted. |
| `--attendees keep` | Write `ORGANIZER` and `ATTENDEE` properties. Events then import as meetings; editing or deleting one may send updates to the people on it. |
| `--attendees drop` | Leave attendees out. |
| `--utc-only` | Write every timed event in UTC with no `VTIMEZONE`. Use if a calendar app shows recurring events at the wrong time. |

## Importing into Outlook

Import each `.ics` into a **new** calendar rather than an existing one, so a bad import can be
deleted whole and redone. Try the smallest file first.

## What is converted

| Apple Calendar | iCalendar |
|---|---|
| Title, notes, URL (web links only), status, free/busy, sequence, created/modified | `SUMMARY`, `DESCRIPTION`, `URL`, `STATUS`, `TRANSP` (+ `X-MICROSOFT-CDO-BUSYSTATUS:FREE`), `SEQUENCE`, `CREATED`/`LAST-MODIFIED`/`DTSTAMP` |
| All-day events | `DTSTART`/`DTEND` as `VALUE=DATE` |
| Floating times | Local time with no zone |
| Repeating events in a named zone | `TZID` plus a generated `VTIMEZONE` |
| Other timed events, and fixed-offset zones such as `GMT+1100` | UTC |
| Repeat rules: frequency, interval, count or end date, weekdays | `RRULE` |
| Deleted occurrences | `EXDATE` |
| Moved occurrences of a repeating event | Separate `VEVENT` with the master's `UID` and a `RECURRENCE-ID` |
| Location title, address, coordinates | `LOCATION`, `GEO` |
| Alerts with a time offset or a fixed time | `VALARM` (display) |
| Video meeting link | Added to the notes as "Join: …" |
| Attachments | Not included. The notes say how many the event had. |

Not converted: reminders/tasks, birthdays from Contacts, subscribed calendars' remote data
(only what was cached in the archive), "use the default alert" placeholders, and non-web links
such as `message:` (Apple Mail) links.

## Known limits

- The archive format is undocumented. The mapping was worked out from a real archive's schema
  and value codes; anything not seen there is left out rather than guessed. Repeat rules using
  more than weekdays (for example "the second Tuesday", specific month days, or set positions)
  fall back to the defaults from the start date, and a warning count is printed.
- Attendee response status (accepted, declined) isn't written, because Apple's status codes
  aren't confirmed.
- The generated `VTIMEZONE` describes the zone's rules in the year of its first event. Zones
  whose rules changed after that year will be off for the later occurrences.

## Tests

```bash
python3 -m unittest discover -s tests
```

Tests run against a synthetic archive built by `tests/make_fixture.py`. Every name, address
and event in it is made up.

## Contributing

Never add real calendar data to this repository: no archives, `.ics` output, names, email
addresses, event titles or UIDs from a real calendar, in code, tests, issues or commit messages.
If something converts wrongly, reproduce it with a made-up event in `tests/make_fixture.py`.

## Licence

MIT
