# Changelog

## 1.8.0

- **Guided RSVP.** Pressing **RSVP** now walks the member through a private flow: (1) tick the event's requirements and press Continue, (2) pick a role, (3) tick that role's requirements and confirm. Events without roles skip steps 2 and 3. Nobody is signed up until the last step. Confirmations are remembered, so withdrawing and re-RSVPing, or switching roles, does not repeat step 1.
- **Cleaner posts.** Event posts no longer carry requirement buttons; the requirements are listed on the post and confirmed during RSVP. Buttons on posts made by earlier versions keep working.
- **Check-in:** if staff add a requirement after someone RSVPed, check-in tells them to press RSVP once to confirm it.
- **Choose the channel from the panel.** **Create Event** now opens a private step with a channel dropdown (defaults to the `channel` setting, or the channel you clicked in) and, when templates exist, a template dropdown. Press Continue to open the form; the event is posted in the chosen channel. The bot and the creator both need permission to post there, and "Fix and try again" keeps the choice.
- The old template-only picker was replaced by this step.


## 1.7.0

- **Menu-driven cleanup.** Removed commands that duplicate the form and Manage menu: `create`, `location`, `duration`, `timezone`, `discordevent`, `attendees`, `finalize`, `cancel`, `series`, `seriescancel`, `template save` and `template use`. Remaining commands are for setup and reports (`panel`, `new`, `channel`, `setup`, `list`, `stats`, `export`, `calendar`, `checkin`, `rewardrole`, `post`, `repeat`, `template`, `template remove`).
- New Manage-menu buttons replace them: **Attendees** (private list by status, with roles), **Finalize** (asks for confirmation and shows exactly what will happen first), and **Cancel event** (optional reason form, then notifies registered members).
- `setup` no longer resets the timezone to UTC when run without one, and its error message gives timezone examples.
- Finalizing and cancelling now refuse events that are already finalized or cancelled, and slow reward-role updates no longer hold up RSVPs.
- Unchanged for other cogs: `create_draft_service`, the stored event data shape, `setup` and `post`.

## 1.6.0

- **Roles:** staff can give an event up to 10 roles (Manage -> Roles), for example Tank, Healer and DPS, or Driver and Navigator. Each role has a name, a number of slots (0 = unlimited) and up to 5 requirements of its own.
- **Signing up:** on an event with roles, **RSVP** opens a private role picker showing how full each role is. Picking a role with requirements shows a private checklist; the member ticks every item and confirms to take the role. Roles without requirements are taken instantly. Event-wide requirement buttons are still checked first.
- **Post:** each role is shown as `Role (filled/slots)` with the members in it, how many are waiting, and a summary of each role's requirements. Members whose role was removed show under *No role yet* until they pick again.
- **Waitlists per role:** a full role puts the member on that role's waitlist. When a seat opens (someone withdraws, switches role, or staff raise the slots) the longest-waiting member *of that role* is promoted and pinged in the event channel. The event's overall capacity still applies.
- **Switching roles:** pressing RSVP again lets a member switch to another role that has room; their old seat is freed for the next person waiting. Staff cannot lower a role's slots below how many members it already has.
- Templates, recurring copies, the attendance CSV (new `role` column) and the `attendees` command all understand roles. Promotions from the waitlist after a withdrawal are now announced in the event channel too, so promoted members find out.

## 1.5.0

- **Event images:** staff can add an image to any event post with a link to an externally hosted image (Manage -> Image) and remove it again. Nothing is uploaded or stored by the bot.
- **Requirements:** staff can give an event up to 10 requirements (Manage -> Requirements, one per line). Each becomes a button on the post. Members must press every requirement button before they can RSVP, and again before they can check in. Pressing is private (only the member sees the reply), confirmations are remembered per member, and withdrawing and re-RSVPing does not make anyone re-confirm. Changing a requirement's wording resets only that requirement's confirmations.
- **Template dropdown:** once any template is saved, **Create Event** first shows a dropdown of saved templates (or *Start blank*). Picking one opens the form pre-filled with its title, length, capacity and location; its description, requirements and image come along automatically.
- **Save as template** button in the Manage menu. Templates now also keep requirements and the image link. `template save` / `template use` and recurring copies carry them too.
- Every event post gets a **Manage** button (staff only) so event settings stay reachable after the creation message is gone. Posts made before this update gain the button the next time they are refreshed (for example on the next RSVP).
- The attendance CSV has a `requirements_confirmed` column (for example `2/3`); privacy deletion also removes a member's confirmations.
- RSVP / withdraw / check-in / requirement presses are now processed one at a time per server, so simultaneous presses cannot overwrite each other.

## 1.4.0

- Added a **Create Event** button and form. `eventcheckin panel` posts the button; staff click it, fill in title, start time, length, capacity and location, and the event is created and posted with RSVP controls in one step. No Unix timestamps or multi-command setup needed.
- Start times are written naturally in the guild timezone (`2026-11-05 19:30`, `5 Nov 7:30pm`, `tomorrow 8pm`, `fri 19:00`); lengths accept `90`, `1h30m`, `2 hours`.
- Mistakes (unreadable date, past time, bad number) show a private message with a button that reopens the form with everything already filled in.
- After creating, the creator gets private buttons to add a description, edit details, or add the event to the server's Discord Events tab.
- Added `eventcheckin new` (opens the form from the slash command) and `eventcheckin channel` (choose where events are announced).
- The `discordevent` command and the new button share one implementation. All existing commands are unchanged.

## 1.3.0

- Added reusable event templates, recurrence-series inspection and cancellation, aggregate attendance analytics, attendee cancellation notices, schema migrations, concurrency-safe event IDs, and OperationsCenter auditing.

## 1.2.0

- Added a bounded draft creation service and source links for optional DecisionLedger review events.

## 1.1.0

- Added bounded recurring draft creation, event duration, IANA timezone configuration, RFC 5545 calendar export, and native Discord scheduled-event creation.

## 1.0.0

- Initial release with persistent controls, capacity-aware RSVP, FIFO waitlists and promotion, reminder scheduling, time-bounded check-in, staff check-in, finalization, reward roles, no-shows, CSV export, privacy deletion, and dashboard configuration.
