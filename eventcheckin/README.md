# EventCheckin

Create events from a button and a form, let members RSVP, pick roles and confirm requirements, check in on the day, and see who showed up. Everything day-to-day is menus and buttons; there are only a few commands, for setup and reports.

## Who can do what

Creating an event needs **Manage Events** or the officer role. Once it exists, its Manage menu works only for the member who created it and members with the officer role (change it with `[p]eventcheckin officerrole @role`).

## Setup (once)

```text
[p]eventcheckin setup #event-logs Atlantic/Reykjavik
[p]eventcheckin channel #events
[p]eventcheckin panel #create-event
```

1. `setup` sets the log channel and the timezone that event times are typed in (use your own, such as `America/Chicago` or `Europe/London`). Running it again without a timezone keeps the current one.
2. `channel` sets the default channel for events. Whoever creates an event can pick a different channel from the dropdown each time.
3. `panel` posts a permanent **Create Event** button.

The bot needs View Channel, Send Messages and Embed Links in those channels.

## Creating an event

Anyone with **Manage Events** clicks **Create Event**. A private step first asks which channel to post in (and, if you have saved templates, which template to start from, or *Blank event*); press **Continue**. The form asks for the title, start time, length, max attendees and location, then the event is saved as a **draft** (nothing is posted). Use the Manage menu to finish it, then **Post to channel**: choose the channel, optionally pick roles to ping, and press **Post now**. The post has RSVP, Withdraw, Check in and Manage buttons. The panel's **Drafts** button reopens any unposted event.

- Start times: `2026-11-05 19:30`, `5 Nov 7:30pm`, `tomorrow 8pm`, `fri 19:00`.
- Lengths: `90`, `1h30m`, `2 hours`.
- Mistakes show a private message with a button that reopens the form with your answers kept.
- Slash users can run `/eventcheckin new` to open the form directly.

## The Manage button

Every event post has a **Manage** button that only staff can use. You also get the same menu privately right after creating an event.

| Button | What it does |
|---|---|
| Edit details | Title, description, location |
| Image | Set a banner from a link. A direct image link works best; a web page that has a picture also works (the bot finds it). **Remove image** clears it |
| Requirements | Up to 10 confirmations (one per line). Members must press each one before they can RSVP and again before checking in |
| Roles | Up to 10 roles (Tank / Healer / DPS, Driver / Navigator...), each with its own slots and up to 5 requirements |
| Start event | Locks sign-ups, marks the event started and opens check-in. Members already in can still check in or withdraw |
| Post to channel | Choose the channel and roles to ping, then publish the draft |
| Save as template | Saves the setup (including requirements, roles and image link) for the Create Event dropdown. 25 per server |
| Add to Discord events | Creates a native scheduled event (needs a location) |
| Attendees | Private list grouped by status, with roles |
| Finalize | After a confirmation: closes registration, marks confirmed RSVPs who never checked in as no-shows, awards the reward role |
| Cancel event | Asks for an optional reason, cancels, notifies everyone who registered |

## For members

- **RSVP** opens a private walkthrough: (1) tick the event's requirements, (2) pick a role (the dropdown shows how full each is), (3) tick that role's requirements and confirm. Events without roles skip steps 2 and 3.
- A full role has its own waitlist. When a seat opens, the longest-waiting member of that role is promoted and pinged in the event channel.
- Pressing RSVP again lets you switch roles. **Withdraw** gives your seat up.
- **Check in** opens before the start time (2 hours by default) and closes after it (12 hours by default).
- A reminder is posted shortly before the start (60 minutes by default).

## Commands

Everything above is menu-driven. These commands are for setup and reports. All need **Manage Events** or admin.

| Command | Purpose |
|---|---|
| `[p]eventcheckin panel [#channel]` | Post the Create Event button |
| `[p]eventcheckin new` | Open the form (slash) or post a button (prefix) |
| `[p]eventcheckin channel [#channel]` | Where events are announced; empty = where the form was opened |
| `[p]eventcheckin setup [#log] [timezone]` | Log channel and timezone |
| `[p]eventcheckin list [status]` | List events (open, draft, finalized, cancelled, all) |
| `[p]eventcheckin stats [days]` | Attendance totals |
| `[p]eventcheckin export <event id>` | Attendance CSV, including requirements and roles |
| `[p]eventcheckin calendar [status]` | `.ics` calendar export |
| `[p]eventcheckin checkin <event id> @member` | Check someone in by hand |
| `[p]eventcheckin rewardrole <event id> [@role]` | Role awarded to checked-in members when finalized |
| `[p]eventcheckin post <event id> [#channel]` | Publish a draft (for example from DecisionLedger) |
| `[p]eventcheckin repeat <event id> <every N days> <times>` | Make recurring draft copies; publish each with `post` |
| `[p]eventcheckin template` / `template remove <name>` | List or delete saved templates |

When DecisionLedger is loaded, `decision reviewevent` creates a linked EventCheckin draft; publish it with `post`. EventCheckin works fine without DecisionLedger.

Settings such as reminder time and check-in window are also available in Red-Web-Dashboard.
