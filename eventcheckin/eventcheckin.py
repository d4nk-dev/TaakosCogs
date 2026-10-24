"""Persistent event registration, waitlists, check-ins, and attendance."""

from __future__ import annotations

import asyncio
import copy
import csv
import io
import logging
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import tasks
from redbot.core import Config, commands

from .calendar_utils import build_calendar
from .dashboard_integration import DashboardIntegration
from .parsing import ParseError, parse_capacity, parse_duration, parse_start

if TYPE_CHECKING:
    from redbot.core.bot import Red

log = logging.getLogger("red.taakoscogs.eventcheckin")

# discord.py 2.6+ wants modal fields wrapped in a Label; older versions use TextInput(label=...).
HAS_LABEL = hasattr(discord.ui, "Label")

MAX_REQUIREMENTS = 10
REQUIREMENT_LENGTH = 80  # a requirement is also a button label, which Discord caps at 80
MAX_PING_ROLES = 10
MAX_ROLES = 10
MAX_ROLE_REQUIREMENTS = 5
ROLE_NAME_LENGTH = 40
MAX_ROLE_SLOTS = 500


def add_text_field(
    modal: discord.ui.Modal, label: str, *, hint: str | None = None, **kwargs: Any
) -> discord.ui.TextInput:
    """Add a labelled TextInput to a modal in a way that works on old and new discord.py."""
    if HAS_LABEL:
        field = discord.ui.TextInput(**kwargs)
        modal.add_item(discord.ui.Label(text=label[:45], description=hint[:100] if hint else None, component=field))
    else:
        field = discord.ui.TextInput(label=label[:45], **kwargs)
        modal.add_item(field)
    return field


class EventControls(discord.ui.View):
    """Persistent controls on an event post: RSVP, Withdraw, Check in, Manage.

    Requirements are confirmed inside the private RSVP walkthrough, so new posts carry no requirement
    buttons. The registration copy still knows the old fixed slots (``eventcheckin:req:<1-10>``) so
    posts made by earlier versions keep working after a restart.
    """

    def __init__(self, cog: EventCheckin, event: dict[str, Any] | None = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        if event is None:  # the registration copy: every legacy slot, so every custom_id is known
            requirements = [{"id": slot, "text": f"Requirement {slot}"} for slot in range(1, MAX_REQUIREMENTS + 1)]
        else:
            requirements = []
        for position, requirement in enumerate(requirements[:MAX_REQUIREMENTS]):
            slot = int(requirement["id"])
            button = discord.ui.Button(
                label=str(requirement["text"])[:REQUIREMENT_LENGTH],
                style=discord.ButtonStyle.secondary,
                custom_id=f"eventcheckin:req:{slot}",
                row=1 + position // 5,
            )
            button.callback = self._make_requirement_callback(slot)
            self.add_item(button)

    def _make_requirement_callback(self, slot: int):
        async def callback(interaction: discord.Interaction) -> None:
            await self.cog.handle_requirement(interaction, slot)

        return callback

    @discord.ui.button(label="RSVP", emoji="✅", style=discord.ButtonStyle.success, custom_id="eventcheckin:rsvp", row=0)
    async def rsvp(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_button(interaction, "rsvp")

    @discord.ui.button(
        label="Withdraw", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="eventcheckin:withdraw", row=0
    )
    async def withdraw(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_button(interaction, "withdraw")

    @discord.ui.button(
        label="Check in", emoji="📍", style=discord.ButtonStyle.primary, custom_id="eventcheckin:checkin", row=0
    )
    async def checkin(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_button(interaction, "checkin")

    @discord.ui.button(
        label="Manage", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="eventcheckin:manage", row=0
    )
    async def manage(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.open_manage_menu(interaction)


class EventCreatePanel(discord.ui.View):
    """Persistent "Create Event" button that opens the event creation modal."""

    def __init__(self, cog: EventCheckin) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Create Event",
        emoji="📅",
        style=discord.ButtonStyle.primary,
        custom_id="eventcheckin:create",
    )
    async def create(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.open_create_modal(interaction)

    @discord.ui.button(label="Drafts", emoji="📝", style=discord.ButtonStyle.secondary, custom_id="eventcheckin:drafts")
    async def drafts(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.open_drafts(interaction)


class CreateEventModal(discord.ui.Modal):
    """Collect everything needed to create and post an event in one step."""

    def __init__(
        self,
        cog: EventCheckin,
        timezone_name: str,
        defaults: dict[str, str] | None = None,
        template_key: str | None = None,
        channel_id: int | None = None,
    ) -> None:
        super().__init__(title="Create Event", timeout=900)
        self.cog = cog
        self.timezone_name = timezone_name
        self.template_key = template_key
        self.channel_id = channel_id
        defaults = defaults or {}
        self.name_input = add_text_field(
            self,
            "Event title",
            placeholder="Movie night",
            default=defaults.get("title") or None,
            min_length=1,
            max_length=100,
        )
        self.starts_input = add_text_field(
            self,
            f"Starts ({timezone_name})",
            placeholder="2026-11-05 19:30  |  tomorrow 8pm  |  fri 7:30pm",
            default=defaults.get("starts") or None,
            min_length=1,
            max_length=60,
        )
        self.duration_input = add_text_field(
            self,
            "How long? (default 60 minutes)",
            placeholder="90  |  1h30m  |  2 hours",
            default=defaults.get("duration") or None,
            required=False,
            max_length=30,
        )
        self.capacity_input = add_text_field(
            self,
            "Max attendees (blank = unlimited)",
            placeholder="25",
            default=defaults.get("capacity") or None,
            required=False,
            max_length=5,
        )
        self.location_input = add_text_field(
            self,
            "Location or join link (optional)",
            placeholder="Voice channel, game server, link...",
            default=defaults.get("location") or None,
            required=False,
            max_length=500,
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_create_submit(
            interaction,
            {
                "title": str(self.name_input.value or ""),
                "starts": str(self.starts_input.value or ""),
                "duration": str(self.duration_input.value or ""),
                "capacity": str(self.capacity_input.value or ""),
                "location": str(self.location_input.value or ""),
                "template": self.template_key or "",
                "channel_id": str(self.channel_id or ""),
            },
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await self.cog.handle_modal_error(interaction, error)


class EditEventModal(discord.ui.Modal):
    """Edit the descriptive fields of an existing event."""

    def __init__(self, cog: EventCheckin, event: dict[str, Any]) -> None:
        super().__init__(title=f"Edit Event #{event['event_id']}"[:45], timeout=900)
        self.cog = cog
        self.event_id = int(event["event_id"])
        self.name_input = add_text_field(
            self,
            "Event title",
            default=str(event.get("title") or "")[:100],
            min_length=1,
            max_length=100,
        )
        self.description_input = add_text_field(
            self,
            "Description",
            style=discord.TextStyle.paragraph,
            default=str(event.get("description") or "")[:2000] or None,
            placeholder="What is this event about? What should people bring?",
            required=False,
            max_length=2000,
        )
        self.location_input = add_text_field(
            self,
            "Location or join link",
            default=str(event.get("location") or "")[:500] or None,
            required=False,
            max_length=500,
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_edit_submit(
            interaction,
            self.event_id,
            str(self.name_input.value or ""),
            str(self.description_input.value or ""),
            str(self.location_input.value or ""),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await self.cog.handle_modal_error(interaction, error)


class RetryCreateView(discord.ui.View):
    """Reopen the create modal with what the member already typed."""

    def __init__(self, cog: EventCheckin, actor_id: int, timezone_name: str, values: dict[str, str]) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.actor_id = actor_id
        self.timezone_name = timezone_name
        self.values = values

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.actor_id:
            return True
        await interaction.response.send_message("This prompt is not for you.", ephemeral=True)
        return False

    @discord.ui.button(label="Fix and try again", emoji="✏️", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            CreateEventModal(
                self.cog,
                self.timezone_name,
                self.values,
                self.values.get("template") or None,
                int(self.values["channel_id"]) if str(self.values.get("channel_id") or "").isdigit() else None,
            )
        )


class EditRequirementsModal(discord.ui.Modal):
    """Set the buttons members must press before they can RSVP."""

    def __init__(self, cog: EventCheckin, event: dict[str, Any]) -> None:
        super().__init__(title=f"Requirements - Event #{event['event_id']}"[:45], timeout=900)
        self.cog = cog
        self.event_id = int(event["event_id"])
        current = "\n".join(str(item["text"]) for item in cog._requirements(event))
        self.requirements_input = add_text_field(
            self,
            "Requirements (one per line)",
            hint=f"Members tick each one when they RSVP. Up to {MAX_REQUIREMENTS}. Empty removes them all.",
            style=discord.TextStyle.paragraph,
            default=current or None,
            placeholder="I have read the rules\nI can stay for the whole session",
            required=False,
            max_length=1500,
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_requirements_submit(interaction, self.event_id, str(self.requirements_input.value or ""))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await self.cog.handle_modal_error(interaction, error)


class EventImageModal(discord.ui.Modal):
    """Set the event's banner image from a link to an externally hosted image."""

    def __init__(self, cog: EventCheckin, event: dict[str, Any]) -> None:
        super().__init__(title=f"Image - Event #{event['event_id']}"[:45], timeout=900)
        self.cog = cog
        self.event_id = int(event["event_id"])
        self.url_input = add_text_field(
            self,
            "Image link",
            hint="A direct link to an image hosted somewhere public (https://...).",
            placeholder="https://example.com/banner.png",
            default=event.get("image_url") or None,
            min_length=1,
            max_length=500,
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_image_submit(interaction, self.event_id, str(self.url_input.value or ""))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await self.cog.handle_modal_error(interaction, error)


class SaveTemplateModal(discord.ui.Modal):
    """Save an event's setup as a reusable template."""

    def __init__(self, cog: EventCheckin, event: dict[str, Any]) -> None:
        super().__init__(title="Save as Template", timeout=900)
        self.cog = cog
        self.event_id = int(event["event_id"])
        self.name_input = add_text_field(
            self,
            "Template name",
            hint="Saving under an existing name replaces that template.",
            default=str(event.get("title") or "")[:50] or None,
            min_length=1,
            max_length=50,
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_save_template_submit(interaction, self.event_id, str(self.name_input.value or ""))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await self.cog.handle_modal_error(interaction, error)


class CreateStartView(discord.ui.View):
    """Step before the create form: choose where the event is posted and (optionally) a template."""

    def __init__(
        self,
        cog: EventCheckin,
        actor_id: int,
        templates: dict[str, Any],
        default_channel: discord.TextChannel | None,
    ) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.actor_id = actor_id
        self.channel_id: int | None = default_channel.id if default_channel else None
        self.template_key: str | None = None
        kwargs: dict[str, Any] = {}
        if default_channel is not None:
            kwargs["default_values"] = [discord.SelectDefaultValue.from_channel(default_channel)]
        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Post the event in which channel?",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=1,
            max_values=1,
            row=0,
            **kwargs,
        )
        self.channel_select.callback = self._channel_picked
        self.add_item(self.channel_select)
        self.template_select: discord.ui.Select | None = None
        if templates:
            options = [discord.SelectOption(label="Blank event", value="__blank__", description="Start from scratch", emoji="✨")]
            for key, template in sorted(templates.items())[:24]:
                minutes = int(template.get("duration_minutes") or 60)
                capacity = int(template.get("capacity") or 0)
                detail = f"{template.get('title', '')} - {minutes} min - {capacity if capacity else 'no limit'}"
                options.append(
                    discord.SelectOption(
                        label=str(template.get("name") or key)[:100], value=key[:100], description=detail[:100]
                    )
                )
            self.template_select = discord.ui.Select(
                placeholder="Start from a saved template (optional)...", options=options, min_values=1, max_values=1, row=1
            )
            self.template_select.callback = self._template_picked
            self.add_item(self.template_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.actor_id:
            return True
        await interaction.response.send_message("This prompt is not for you.", ephemeral=True)
        return False

    async def _channel_picked(self, interaction: discord.Interaction) -> None:
        self.channel_id = int(self.channel_select.values[0].id)
        await interaction.response.defer()

    async def _template_picked(self, interaction: discord.Interaction) -> None:
        self.template_key = None if self.template_select.values[0] == "__blank__" else self.template_select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="Continue", emoji="➡️", style=discord.ButtonStyle.primary, row=2)
    async def proceed(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.open_template_modal(interaction, self.template_key, self.channel_id)


class RoleModal(discord.ui.Modal):
    """Add a role to an event, or edit one: name, how many members fill it, what they must confirm."""

    def __init__(self, cog: EventCheckin, event_id: int, role: dict[str, Any] | None = None) -> None:
        super().__init__(title="Edit Role" if role else "Add Role", timeout=900)
        self.cog = cog
        self.event_id = event_id
        self.role_id = int(role["id"]) if role else None
        self.name_input = add_text_field(
            self,
            "Role name",
            placeholder="Tank",
            default=str(role["name"]) if role else None,
            min_length=1,
            max_length=ROLE_NAME_LENGTH,
        )
        self.slots_input = add_text_field(
            self,
            "Slots (0 = unlimited)",
            hint="How many members can fill this role. Extra sign-ups wait in line.",
            default=str(int(role.get("slots") or 0)) if role else "1",
            min_length=1,
            max_length=3,
        )
        self.requirements_input = add_text_field(
            self,
            "Requirements (one per line)",
            hint=f"Members confirm each one before taking this role. Up to {MAX_ROLE_REQUIREMENTS}.",
            style=discord.TextStyle.paragraph,
            default="\n".join(role.get("requirements") or []) if role and role.get("requirements") else None,
            placeholder="Level 60 or higher\nHave a working microphone",
            required=False,
            max_length=800,
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_role_submit(
            interaction,
            self.event_id,
            self.role_id,
            str(self.name_input.value or ""),
            str(self.slots_input.value or ""),
            str(self.requirements_input.value or ""),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await self.cog.handle_modal_error(interaction, error)


class RolesEditorView(discord.ui.View):
    """Staff: add, edit or remove an event's roles."""

    def __init__(self, cog: EventCheckin, actor_id: int, event: dict[str, Any]) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.actor_id = actor_id
        self.event_id = int(event["event_id"])
        roles = cog._roles(event)
        self.add_role.disabled = len(roles) >= MAX_ROLES
        if roles:
            for placeholder, row, callback in (
                ("Edit a role...", 1, self._edit_picked),
                ("Remove a role...", 2, self._remove_picked),
            ):
                select = discord.ui.Select(
                    placeholder=placeholder,
                    row=row,
                    options=[
                        discord.SelectOption(
                            label=str(role["name"])[:100],
                            value=str(role["id"]),
                            description=(
                                f"{int(role.get('slots') or 0) or 'unlimited'} slot(s), "
                                f"{len(role.get('requirements') or [])} requirement(s)"
                            )[:100],
                        )
                        for role in roles
                    ],
                )
                select.callback = self._make_select_callback(select, callback)
                self.add_item(select)

    @staticmethod
    def _make_select_callback(select: discord.ui.Select, callback):
        async def run(interaction: discord.Interaction) -> None:
            await callback(interaction, int(select.values[0]))

        return run

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.actor_id:
            return True
        await interaction.response.send_message("These buttons are for the staff member who opened them.", ephemeral=True)
        return False

    @discord.ui.button(label="Add role", emoji="➕", style=discord.ButtonStyle.success, row=0)
    async def add_role(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.open_role_modal(interaction, self.event_id, None)

    async def _edit_picked(self, interaction: discord.Interaction, role_id: int) -> None:
        await self.cog.open_role_modal(interaction, self.event_id, role_id)

    async def _remove_picked(self, interaction: discord.Interaction, role_id: int) -> None:
        await self.cog.handle_role_remove(interaction, self.event_id, role_id)


class EventRequirementsView(discord.ui.View):
    """Step 1 of the RSVP walkthrough: tick every event-wide requirement, then continue."""

    def __init__(self, cog: EventCheckin, user_id: int, event: dict[str, Any]) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.user_id = user_id
        self.event_id = int(event["event_id"])
        items = cog._requirements(event)[:MAX_REQUIREMENTS]
        self.ids = [int(item["id"]) for item in items]
        self.texts = [str(item["text"]) for item in items]
        self.confirmed: set[int] = set()
        self.buttons: list[discord.ui.Button] = []
        for index in range(len(self.texts)):
            button = discord.ui.Button(label=self._label(index), style=discord.ButtonStyle.secondary, row=index // 3)
            button.callback = self._make_toggle(index)
            self.buttons.append(button)
            self.add_item(button)
        self.continue_button = discord.ui.Button(
            label="Continue", emoji="➡️", style=discord.ButtonStyle.success, row=4, disabled=bool(self.texts)
        )
        self.continue_button.callback = self._continue
        self.add_item(self.continue_button)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=4)
        cancel.callback = self._cancel
        self.add_item(cancel)

    def _label(self, index: int) -> str:
        mark = "✅" if index in self.confirmed else "☐"
        return f"{mark} {self.texts[index]}"[:80]

    def _make_toggle(self, index: int):
        async def toggle(interaction: discord.Interaction) -> None:
            self.confirmed ^= {index}
            button = self.buttons[index]
            button.label = self._label(index)
            button.style = discord.ButtonStyle.success if index in self.confirmed else discord.ButtonStyle.secondary
            self.continue_button.disabled = len(self.confirmed) < len(self.texts)
            await interaction.response.edit_message(view=self)

        return toggle

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("This prompt is not for you.", ephemeral=True)
        return False

    async def _continue(self, interaction: discord.Interaction) -> None:
        if len(self.confirmed) < len(self.texts):
            await interaction.response.send_message("Tick every requirement first.", ephemeral=True)
            return
        await self.cog.complete_event_requirements(interaction, self.event_id, self.ids)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Cancelled. You were not signed up.", embed=None, view=None)


class RolePickerView(discord.ui.View):
    """Member: choose which role to sign up for (private, shown after pressing RSVP)."""

    def __init__(self, cog: EventCheckin, user_id: int, event: dict[str, Any]) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.event_id = int(event["event_id"])
        options = []
        for role in cog._roles(event):
            slots = int(role.get("slots") or 0)
            filled = cog._role_filled(event, role["id"])
            state = "FULL - join the waitlist" if slots and filled >= slots else f"{filled}/{slots or 'open'} filled"
            needs = len(role.get("requirements") or [])
            detail = state + (f" - {needs} requirement(s)" if needs else "")
            options.append(
                discord.SelectOption(label=str(role["name"])[:100], value=str(role["id"]), description=detail[:100])
            )
        self.select = discord.ui.Select(placeholder="Choose your role...", options=options)
        self.select.callback = self._picked
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("This prompt is not for you.", ephemeral=True)
        return False

    async def _picked(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_role_pick(interaction, self.event_id, int(self.select.values[0]))


class RoleRequirementsView(discord.ui.View):
    """Member: tick off a role's requirements, then confirm the sign-up (private checklist)."""

    def __init__(self, cog: EventCheckin, user_id: int, event_id: int, role: dict[str, Any]) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.event_id = event_id
        self.role_id = int(role["id"])
        self.texts = [str(text) for text in role.get("requirements") or []][:MAX_ROLE_REQUIREMENTS]
        self.confirmed: set[int] = set()
        self.buttons: list[discord.ui.Button] = []
        for index, text in enumerate(self.texts):
            button = discord.ui.Button(label=self._label(index), style=discord.ButtonStyle.secondary, row=index // 2)
            button.callback = self._make_toggle(index)
            self.buttons.append(button)
            self.add_item(button)
        self.confirm_button = discord.ui.Button(
            label="Confirm sign-up", emoji="✅", style=discord.ButtonStyle.success, row=3, disabled=bool(self.texts)
        )
        self.confirm_button.callback = self._confirm
        self.add_item(self.confirm_button)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
        cancel.callback = self._cancel
        self.add_item(cancel)

    def _label(self, index: int) -> str:
        mark = "✅" if index in self.confirmed else "☐"
        return f"{mark} {self.texts[index]}"[:80]

    def _make_toggle(self, index: int):
        async def toggle(interaction: discord.Interaction) -> None:
            self.confirmed ^= {index}
            button = self.buttons[index]
            button.label = self._label(index)
            button.style = discord.ButtonStyle.success if index in self.confirmed else discord.ButtonStyle.secondary
            self.confirm_button.disabled = len(self.confirmed) < len(self.texts)
            await interaction.response.edit_message(view=self)

        return toggle

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("This prompt is not for you.", ephemeral=True)
        return False

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if len(self.confirmed) < len(self.texts):
            await interaction.response.send_message("Tick every requirement first.", ephemeral=True)
            return
        await self.cog.complete_role_signup(interaction, self.event_id, self.role_id)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Cancelled. You were not signed up.", embed=None, view=None)


class ConfirmActionView(discord.ui.View):
    """A private "are you sure?" step in front of an action that can't be undone."""

    def __init__(self, actor_id: int, action, *, confirm_label: str, confirm_style: discord.ButtonStyle) -> None:
        super().__init__(timeout=120)
        self.actor_id = actor_id
        self.action = action
        self.confirm.label = confirm_label
        self.confirm.style = confirm_style

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.actor_id:
            return True
        await interaction.response.send_message("This prompt is not for you.", ephemeral=True)
        return False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.action(interaction)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Nothing was changed.", view=None)


class CancelEventModal(discord.ui.Modal):
    """Ask for an optional reason, then cancel the event and tell everyone who registered."""

    def __init__(self, cog: EventCheckin, event: dict[str, Any]) -> None:
        super().__init__(title=f"Cancel Event #{event['event_id']}"[:45], timeout=900)
        self.cog = cog
        self.event_id = int(event["event_id"])
        self.reason_input = add_text_field(
            self,
            "Reason (optional)",
            hint="Submitting cancels the event and sends this to everyone who registered.",
            style=discord.TextStyle.paragraph,
            placeholder="Why is it cancelled? Will it be rescheduled?",
            required=False,
            max_length=500,
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_cancel_submit(interaction, self.event_id, str(self.reason_input.value or ""))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await self.cog.handle_modal_error(interaction, error)


class EventManageView(discord.ui.View):
    """Private staff menu for one event; opened by the Manage button or right after creating."""

    def __init__(self, cog: EventCheckin, actor_id: int, event_id: int) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.actor_id = actor_id
        self.event_id = event_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.actor_id:
            return True
        await interaction.response.send_message("These buttons are for the staff member who opened them.", ephemeral=True)
        return False

    @discord.ui.button(label="Edit details", emoji="📝", style=discord.ButtonStyle.secondary, row=0)
    async def edit(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.open_edit_modal(interaction, self.event_id)

    @discord.ui.button(label="Image", emoji="🖼️", style=discord.ButtonStyle.secondary, row=0)
    async def image(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.open_image_modal(interaction, self.event_id)

    @discord.ui.button(label="Requirements", emoji="📋", style=discord.ButtonStyle.secondary, row=0)
    async def requirements(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.open_requirements_modal(interaction, self.event_id)

    @discord.ui.button(label="Roles", emoji="🎭", style=discord.ButtonStyle.secondary, row=0)
    async def roles(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.open_roles_editor(interaction, self.event_id)

    @discord.ui.button(label="Post to channel", emoji="📣", style=discord.ButtonStyle.success, row=3)
    async def post_event(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.open_post_menu(interaction, self.event_id)

    @discord.ui.button(label="Save as template", emoji="💾", style=discord.ButtonStyle.secondary, row=1)
    async def save_template(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.open_save_template_modal(interaction, self.event_id)

    @discord.ui.button(label="Add to Discord events", emoji="🗓️", style=discord.ButtonStyle.secondary, row=1)
    async def discord_event(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_discord_event_button(interaction, self.event_id)

    @discord.ui.button(label="Remove image", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def remove_image(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.handle_remove_image(interaction, self.event_id)

    @discord.ui.button(label="Attendees", emoji="👥", style=discord.ButtonStyle.secondary, row=2)
    async def attendees(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_attendees(interaction, self.event_id)

    @discord.ui.button(label="Finalize", emoji="🏁", style=discord.ButtonStyle.primary, row=2)
    async def finalize(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.request_finalize(interaction, self.event_id)

    @discord.ui.button(label="Cancel event", emoji="🚫", style=discord.ButtonStyle.danger, row=2)
    async def cancel_event(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.open_cancel_modal(interaction, self.event_id)


class PostEventView(discord.ui.View):
    """Choose the channel and the roles to ping, then post a finished event."""

    def __init__(
        self,
        cog: EventCheckin,
        actor_id: int,
        event_id: int,
        channel: discord.TextChannel | None,
        roles: list[discord.Role],
    ) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.actor_id = actor_id
        self.event_id = event_id
        self.channel_id: int | None = channel.id if channel else None
        self.role_ids: list[int] = [role.id for role in roles]
        channel_kwargs: dict[str, Any] = {}
        if channel is not None:
            channel_kwargs["default_values"] = [discord.SelectDefaultValue.from_channel(channel)]
        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Post in which channel?",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=1,
            max_values=1,
            row=0,
            **channel_kwargs,
        )
        self.channel_select.callback = self._channel_picked
        self.add_item(self.channel_select)
        role_kwargs: dict[str, Any] = {}
        if roles:
            role_kwargs["default_values"] = [discord.SelectDefaultValue.from_role(role) for role in roles[:MAX_PING_ROLES]]
        self.role_select = discord.ui.RoleSelect(
            placeholder="Roles to ping (optional)...", min_values=0, max_values=MAX_PING_ROLES, row=1, **role_kwargs
        )
        self.role_select.callback = self._roles_picked
        self.add_item(self.role_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.actor_id:
            return True
        await interaction.response.send_message("These buttons are for the staff member who opened them.", ephemeral=True)
        return False

    async def _channel_picked(self, interaction: discord.Interaction) -> None:
        self.channel_id = int(self.channel_select.values[0].id)
        await interaction.response.defer()

    async def _roles_picked(self, interaction: discord.Interaction) -> None:
        self.role_ids = [int(role.id) for role in self.role_select.values]
        await interaction.response.defer()

    @discord.ui.button(label="Post now", emoji="📣", style=discord.ButtonStyle.success, row=2)
    async def post_now(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.post_from_menu(interaction, self.event_id, self.channel_id, self.role_ids)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.show_manage_menu(interaction, self.event_id)


class DraftPickerView(discord.ui.View):
    """Staff: pick one of the unposted drafts to keep editing."""

    def __init__(self, cog: EventCheckin, actor_id: int, drafts: list[dict[str, Any]]) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.actor_id = actor_id
        options = [
            discord.SelectOption(
                label=f"#{event['event_id']} {event['title']}"[:100],
                value=str(event["event_id"]),
                description=datetime.fromtimestamp(int(event["starts_at"]), timezone.utc).strftime("Starts %Y-%m-%d %H:%M UTC"),
            )
            for event in drafts[:25]
        ]
        self.select = discord.ui.Select(placeholder="Pick a draft to edit or post...", options=options)
        self.select.callback = self._picked
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.actor_id:
            return True
        await interaction.response.send_message("This prompt is not for you.", ephemeral=True)
        return False

    async def _picked(self, interaction: discord.Interaction) -> None:
        await self.cog.show_manage_menu(interaction, int(self.select.values[0]))


class EventCheckin(DashboardIntegration, commands.Cog):
    """Know who intended to attend, who arrived, and who missed out."""

    CONFIG_IDENTIFIER = 2026080310
    COLOR = 0x5865F2
    SCHEMA_VERSION = 2

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=self.CONFIG_IDENTIFIER, force_registration=True)
        self.config.register_guild(
            schema_version=self.SCHEMA_VERSION,
            next_event_id=1,
            reminder_minutes=60,
            checkin_early_minutes=120,
            checkin_late_minutes=720,
            log_channel_id=None,
            announce_channel_id=None,
            timezone="UTC",
            templates={},
            events={},
        )
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, guild_id: int) -> asyncio.Lock:
        if not hasattr(self, "_locks"):
            self._locks = {}
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def cog_load(self) -> None:
        for guild_id in await self.config.all_guilds():
            conf = self.config.guild_from_id(guild_id)
            events = await conf.events()
            changed = False
            for event in events.values():
                defaults = {
                    "duration_minutes": 60,
                    "scheduled_event_id": None,
                    "recurrence_group": None,
                    "source_type": "manual",
                    "source_id": None,
                    "cancel_reason": "",
                }
                for key, value in defaults.items():
                    if key not in event:
                        event[key] = value
                        changed = True
            if changed:
                await conf.events.set(events)
            await conf.schema_version.set(self.SCHEMA_VERSION)
        self.bot.add_view(EventControls(self))
        self.bot.add_view(EventCreatePanel(self))
        self.event_loop.start()

    def cog_unload(self) -> None:
        self.event_loop.cancel()

    @staticmethod
    def _now() -> int:
        return int(time.time())

    async def _event(self, guild: discord.Guild, event_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        events = await self.config.guild(guild).events()
        event = events.get(str(event_id))
        if not event:
            raise commands.BadArgument("Event not found.")
        return events, event

    async def _event_for_message(
        self, guild: discord.Guild, message_id: int
    ) -> tuple[dict[str, Any], dict[str, Any]] | tuple[None, None]:
        events = await self.config.guild(guild).events()
        event = next((item for item in events.values() if item.get("message_id") == message_id), None)
        return (events, event) if event else (None, None)

    async def _save(self, guild: discord.Guild, events: dict[str, Any], event: dict[str, Any]) -> None:
        event["updated_at"] = self._now()
        events[str(event["event_id"])] = event
        await self.config.guild(guild).events.set(events)

    async def _audit(self, guild: discord.Guild, action: str, status: str, detail: str = "") -> None:
        operations = self.bot.get_cog("OperationsCenter")
        if operations and hasattr(operations, "record_audit"):
            await operations.record_audit(guild, "EventCheckin", action, status, detail)

    @staticmethod
    def _counts(event: dict[str, Any]) -> dict[str, int]:
        counts = {"going": 0, "waitlist": 0, "checked_in": 0, "no_show": 0}
        for record in event.get("attendees", {}).values():
            status = record.get("status")
            if status in counts:
                counts[status] += 1
            if record.get("checked_in_at"):
                counts["checked_in"] += status != "checked_in"
        return counts

    def _embed(self, event: dict[str, Any]) -> discord.Embed:
        counts = self._counts(event)
        capacity = "Unlimited" if not event["capacity"] else str(event["capacity"])
        requirements = self._requirements(event)
        has_roles = bool(self._roles(event))
        if requirements:
            default_text = "Press every requirement button below, then RSVP" + (" and pick your role." if has_roles else ".")
        elif has_roles:
            default_text = "Press RSVP to pick your role, then check in on the day."
        else:
            default_text = "Use the buttons below to register and check in."
        embed = discord.Embed(
            title=event["title"],
            description=event.get("description") or default_text,
            color=self.COLOR if event["status"] == "open" else 0x95A5A6,
        )
        embed.add_field(name="Starts", value=f"<t:{event['starts_at']}:F>\n<t:{event['starts_at']}:R>")
        embed.add_field(name="Duration", value=f"{int(event.get('duration_minutes') or 60)} minutes")
        embed.add_field(name="Capacity", value=capacity)
        embed.add_field(name="RSVPs", value=f"{counts['going']} going\n{counts['waitlist']} waiting")
        embed.add_field(name="Status", value=event["status"].title())
        if event.get("location"):
            embed.add_field(name="Location", value=event["location"], inline=False)
        if requirements:
            embed.add_field(
                name="Requirements (you confirm these when you RSVP)",
                value="\n".join(f"• {item['text']}" for item in requirements)[:1024],
                inline=False,
            )
        self._add_role_fields(embed, event)
        if event.get("image_url"):
            embed.set_image(url=event["image_url"])
        if event.get("source_type") and event["source_type"] != "manual":
            source = event["source_type"].replace("_", " ").title()
            value = f"{source} #{event['source_id']}" if event.get("source_id") else source
            embed.add_field(name="Created from", value=value, inline=False)
        embed.set_footer(text=f"Event #{event['event_id']} · {counts['checked_in']} checked in")
        return embed

    async def _refresh(
        self,
        guild: discord.Guild,
        event: dict[str, Any],
        *,
        strict: bool = False,
    ) -> None:
        """Re-render an event post. ``strict`` raises instead of ignoring Discord errors."""
        channel = guild.get_channel(event.get("channel_id"))
        if not isinstance(channel, discord.TextChannel) or not event.get("message_id"):
            if strict:
                raise commands.BadArgument("This event has no posted message to update.")
            return
        try:
            message = await channel.fetch_message(event["message_id"])
            await message.edit(
                embed=self._embed(event),
                view=EventControls(self, event) if event["status"] == "open" else None,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            if strict:
                raise

    async def _log(self, guild: discord.Guild, text: str) -> None:
        channel_id = await self.config.guild(guild).log_channel_id()
        channel = guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.TextChannel):
            with suppress(discord.HTTPException):
                await channel.send(embed=discord.Embed(description=text, color=self.COLOR, timestamp=discord.utils.utcnow()))

    @staticmethod
    def _requirements(event: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in (event.get("requirements") or []) if isinstance(item, dict) and "id" in item]

    def _missing_requirements(self, event: dict[str, Any], user_id: int) -> list[dict[str, Any]]:
        confirmed = set((event.get("acks") or {}).get(str(user_id), []))
        return [item for item in self._requirements(event) if item["id"] not in confirmed]

    def _set_requirements(self, event: dict[str, Any], texts: list[str]) -> None:
        """Replace an event's requirements, keeping slot ids (and members' confirmations) for unchanged text."""
        cleaned: list[str] = []
        for text in texts:
            text = " ".join(str(text).split())[:REQUIREMENT_LENGTH]
            if text and text not in cleaned:
                cleaned.append(text)
        cleaned = cleaned[:MAX_REQUIREMENTS]
        existing = {item["text"]: int(item["id"]) for item in self._requirements(event)}
        kept = {existing[text] for text in cleaned if text in existing}
        free = [slot for slot in range(1, MAX_REQUIREMENTS + 1) if slot not in kept]
        result: list[dict[str, Any]] = []
        fresh: set[int] = set()
        for text in cleaned:
            if text in existing:
                result.append({"id": existing[text], "text": text})
            else:
                slot = free.pop(0)
                fresh.add(slot)
                result.append({"id": slot, "text": text})
        removed = {int(item["id"]) for item in self._requirements(event)} - kept
        stale = removed | fresh  # a reused slot must not inherit the old requirement's confirmations
        acks = event.get("acks") or {}
        for user_key in list(acks):
            acks[user_key] = [slot for slot in acks[user_key] if slot not in stale]
        event["acks"] = acks
        event["requirements"] = result

    @staticmethod
    def _template_from_event(event: dict[str, Any], name: str) -> dict[str, Any]:
        return {
            "name": name,
            "title": event["title"],
            "description": event.get("description", ""),
            "location": event.get("location", ""),
            "duration_minutes": int(event.get("duration_minutes") or 60),
            "capacity": int(event.get("capacity") or 0),
            "reward_role_id": event.get("reward_role_id"),
            "requirements": [item["text"] for item in EventCheckin._requirements(event)],
            "roles": [
                {
                    "name": role["name"],
                    "slots": int(role.get("slots") or 0),
                    "requirements": list(role.get("requirements") or []),
                }
                for role in EventCheckin._roles(event)
            ],
            "image_url": event.get("image_url"),
        }

    def _apply_template(self, event: dict[str, Any], template: dict[str, Any]) -> None:
        event["description"] = str(template.get("description", ""))[:2000]
        event["location"] = str(template.get("location", ""))[:500]
        event["reward_role_id"] = template.get("reward_role_id")
        self._set_requirements(event, list(template.get("requirements") or []))
        self._set_roles_from_specs(event, list(template.get("roles") or []))
        if template.get("image_url"):
            event["image_url"] = template["image_url"]

    def _promote(self, event: dict[str, Any]) -> int | None:
        """Move the longest-waiting eligible member from the waitlist to going, if a seat is free."""
        capacity = int(event.get("capacity") or 0)
        attendees = event.setdefault("attendees", {})
        going = sum(record.get("status") in {"going", "checked_in"} for record in attendees.values())
        if capacity and going >= capacity:
            return None
        waiting = sorted(
            (record for record in attendees.values() if record.get("status") == "waitlist"),
            key=lambda record: record.get("registered_at", 0),
        )
        for record in waiting:
            role = self._role(event, record.get("role_id"))
            if role is not None and not self._role_open(event, role):
                continue  # their role is still full; the next person in line may fit elsewhere
            record["status"] = "going"
            record["promoted_at"] = self._now()
            return record["user_id"]
        return None

    def _promote_all(self, event: dict[str, Any]) -> list[int]:
        promoted: list[int] = []
        while len(promoted) < 1000 and (user_id := self._promote(event)) is not None:
            promoted.append(user_id)
        return promoted

    # ---- roles -------------------------------------------------------------------------------

    @staticmethod
    def _roles(event: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in (event.get("roles") or []) if isinstance(item, dict) and "id" in item]

    def _role(self, event: dict[str, Any], role_id: Any) -> dict[str, Any] | None:
        if role_id is None:
            return None
        return next((item for item in self._roles(event) if item["id"] == role_id), None)

    @staticmethod
    def _role_filled(event: dict[str, Any], role_id: int, *, exclude: int | None = None) -> int:
        return sum(
            record.get("role_id") == role_id
            and record.get("status") in {"going", "checked_in"}
            and record.get("user_id") != exclude
            for record in event.get("attendees", {}).values()
        )

    def _role_open(self, event: dict[str, Any], role: dict[str, Any]) -> bool:
        slots = int(role.get("slots") or 0)
        return not slots or self._role_filled(event, role["id"]) < slots

    def _set_roles_from_specs(self, event: dict[str, Any], specs: list[dict[str, Any]]) -> None:
        """Replace an event's roles from template data (name, slots, requirements)."""
        roles: list[dict[str, Any]] = []
        for spec in specs[:MAX_ROLES]:
            name = " ".join(str(spec.get("name", "")).split())[:ROLE_NAME_LENGTH]
            if not name:
                continue
            requirements = [" ".join(str(text).split())[:REQUIREMENT_LENGTH] for text in spec.get("requirements") or []]
            roles.append(
                {
                    "id": len(roles) + 1,
                    "name": name,
                    "slots": max(0, min(int(spec.get("slots") or 0), MAX_ROLE_SLOTS)),
                    "requirements": [text for text in requirements if text][:MAX_ROLE_REQUIREMENTS],
                }
            )
        event["roles"] = roles

    def _add_role_fields(self, embed: discord.Embed, event: dict[str, Any]) -> None:
        roles = self._roles(event)
        if not roles:
            return
        attendees = sorted(event.get("attendees", {}).values(), key=lambda record: record.get("registered_at", 0))
        for role in roles:
            slots = int(role.get("slots") or 0)
            members = [r["user_id"] for r in attendees if r.get("role_id") == role["id"] and r.get("status") in {"going", "checked_in"}]
            waiting = sum(r.get("role_id") == role["id"] and r.get("status") == "waitlist" for r in attendees)
            lines = [f"<@{user_id}>" for user_id in members[:15]]
            if len(members) > 15:
                lines.append(f"+{len(members) - 15} more")
            if not lines:
                lines.append("*Open*")
            if waiting:
                lines.append(f"⏳ {waiting} waiting")
            embed.add_field(name=f"{role['name']} ({len(members)}/{slots or '∞'})"[:256], value="\n".join(lines)[:1024])
        known = {role["id"] for role in roles}
        unassigned = [
            r["user_id"] for r in attendees if r.get("status") in {"going", "checked_in"} and r.get("role_id") not in known
        ]
        if unassigned:
            embed.add_field(
                name=f"No role yet ({len(unassigned)})",
                value=("\n".join(f"<@{u}>" for u in unassigned[:15]) + "\nPress RSVP to pick a role.")[:1024],
            )
        needs = [f"**{role['name']}**: " + "; ".join(role["requirements"]) for role in roles if role.get("requirements")]
        if needs:
            embed.add_field(name="Role requirements (confirmed when you pick the role)", value="\n".join(needs)[:1024], inline=False)

    async def _announce_promotions(self, guild: discord.Guild, event: dict[str, Any], user_ids: list[int]) -> None:
        channel = guild.get_channel(event.get("channel_id")) if event.get("channel_id") else None
        if not user_ids or not isinstance(channel, discord.TextChannel):
            return
        mentions = " ".join(f"<@{user_id}>" for user_id in user_ids[:25])
        with suppress(discord.HTTPException):
            await channel.send(
                f"{mentions} moved up from the waitlist for **{event['title']}**. You're in!",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )

    async def handle_button(self, interaction: discord.Interaction, action: str) -> None:
        if interaction.guild:
            async with self._lock(interaction.guild.id):
                await self._handle_button(interaction, action)
        else:
            await self._handle_button(interaction, action)

    @staticmethod
    def _missing_text(missing: list[dict[str, Any]]) -> str:
        return "\n".join(f"• {item['text']}" for item in missing)

    async def _handle_button(self, interaction: discord.Interaction, action: str) -> None:
        if not interaction.guild or not interaction.message or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This event control only works in a server.", ephemeral=True)
            return
        events, event = await self._event_for_message(interaction.guild, interaction.message.id)
        if not event or events is None:
            await interaction.response.send_message("This event is no longer tracked.", ephemeral=True)
            return
        if event["status"] != "open":
            await interaction.response.send_message("Registration for this event is closed.", ephemeral=True)
            return
        attendees = event.setdefault("attendees", {})
        key = str(interaction.user.id)
        current = attendees.get(key)
        response = ""
        promoted_id = None
        missing = self._missing_requirements(event, interaction.user.id)
        if action == "rsvp":
            if current and current.get("status") == "checked_in":
                await interaction.response.send_message("You are already checked in.", ephemeral=True)
                return
            if missing:
                await interaction.response.send_message(
                    "**Step 1: event requirements.** Tick each one to confirm it, then press **Continue**."
                    + (" Next you'll pick your role." if self._roles(event) else ""),
                    view=EventRequirementsView(self, interaction.user.id, event),
                    ephemeral=True,
                )
                return
            if self._roles(event):
                held = self._role(event, current.get("role_id")) if current else None
                intro = (
                    f"You are currently **{discord.utils.escape_markdown(held['name'])}**. Pick a different role to switch."
                    if held and current.get("status") in {"going", "waitlist"}
                    else "Pick the role you want to play."
                )
                await interaction.response.send_message(
                    intro, view=RolePickerView(self, interaction.user.id, event), ephemeral=True
                )
                return
            going = sum(record.get("status") in {"going", "checked_in"} for record in attendees.values())
            capacity = int(event.get("capacity") or 0)
            status = (
                "waitlist" if capacity and going >= capacity and (not current or current.get("status") != "going") else "going"
            )
            attendees[key] = {
                "user_id": interaction.user.id,
                "status": status,
                "registered_at": current.get("registered_at", self._now()) if current else self._now(),
                "checked_in_at": current.get("checked_in_at") if current else None,
                "promoted_at": current.get("promoted_at") if current else None,
                "staff_actor_id": None,
            }
            response = "You are registered." if status == "going" else "The event is full; you joined the waitlist."
        elif action == "withdraw":
            if not current or current.get("status") not in {"going", "waitlist"}:
                response = "You do not have an active RSVP."
            else:
                was_going = current.get("status") == "going"
                current["status"] = "withdrawn"
                current["withdrawn_at"] = self._now()
                promoted_id = self._promote(event) if was_going else None
                response = "Your RSVP was withdrawn."
        else:
            early = event["starts_at"] - int(await self.config.guild(interaction.guild).checkin_early_minutes()) * 60
            late = event["starts_at"] + int(await self.config.guild(interaction.guild).checkin_late_minutes()) * 60
            if not current or current.get("status") != "going":
                response = "You must have a confirmed RSVP before checking in."
            elif missing:
                response = "The event requirements changed. Press **RSVP** to confirm these, then check in:\n" + self._missing_text(missing)
            elif not early <= self._now() <= late:
                response = f"Check-in is available from <t:{early}:F> through <t:{late}:F>."
            else:
                current["checked_in_at"] = self._now()
                current["status"] = "checked_in"
                response = "You are checked in."
        await self._save(interaction.guild, events, event)
        await interaction.response.send_message(
            response + (f" <@{promoted_id}> was promoted from the waitlist." if promoted_id else ""),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._refresh(interaction.guild, event)
        if promoted_id:
            await self._announce_promotions(interaction.guild, event, [promoted_id])

    async def _can_manage_events(self, member: discord.Member) -> bool:
        """Same audience as the existing staff commands: admins and Manage Events."""
        perms = member.guild_permissions
        if perms.administrator or perms.manage_events:
            return True
        return await self.bot.is_owner(member) or await self.bot.is_admin(member)

    async def _guild_zone(self, guild: discord.Guild) -> tuple[str, ZoneInfo]:
        name = await self.config.guild(guild).timezone()
        try:
            return name, ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return "UTC", ZoneInfo("UTC")

    async def _announce_destination(
        self, guild: discord.Guild, fallback: discord.abc.GuildChannel | None
    ) -> discord.TextChannel | None:
        channel_id = await self.config.guild(guild).announce_channel_id()
        channel = guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.TextChannel):
            return channel
        return fallback if isinstance(fallback, discord.TextChannel) else None

    async def open_create_modal(self, interaction: discord.Interaction) -> None:
        """Open the create modal for staff; used by the panel button and the slash command."""
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Events can only be created in a server.", ephemeral=True)
            return
        if not await self._can_manage_events(interaction.user):
            await interaction.response.send_message(
                "You need the **Manage Events** permission to create events.", ephemeral=True
            )
            return
        templates = await self.config.guild(interaction.guild).templates()
        default = await self._announce_destination(interaction.guild, interaction.channel)
        text = "Choose the channel this event is meant for (you can change it when you post)"
        text += ", and optionally a saved template, then press **Continue**." if templates else ", then press **Continue**."
        await interaction.response.send_message(
            text, view=CreateStartView(self, interaction.user.id, templates, default), ephemeral=True
        )

    async def open_template_modal(
        self, interaction: discord.Interaction, key: str | None, channel_id: int | None = None
    ) -> None:
        """Open the create form, prefilled from a template (or blank when ``key`` is None)."""
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not await self._can_manage_events(interaction.user):
            await interaction.response.send_message("You need the **Manage Events** permission to do that.", ephemeral=True)
            return
        defaults: dict[str, str] = {}
        if key:
            template = (await self.config.guild(interaction.guild).templates()).get(key)
            if not template:
                await interaction.response.send_message("That template no longer exists.", ephemeral=True)
                return
            capacity = int(template.get("capacity") or 0)
            defaults = {
                "title": str(template.get("title", ""))[:100],
                "duration": str(int(template.get("duration_minutes") or 60)),
                "capacity": str(capacity) if capacity else "",
                "location": str(template.get("location", ""))[:500],
            }
        timezone_name, _zone = await self._guild_zone(interaction.guild)
        await interaction.response.send_modal(CreateEventModal(self, timezone_name, defaults, key, channel_id))

    async def handle_create_submit(self, interaction: discord.Interaction, values: dict[str, str]) -> None:
        guild = interaction.guild
        member = interaction.user
        if not guild or not isinstance(member, discord.Member) or not await self._can_manage_events(member):
            await interaction.response.send_message("You need the **Manage Events** permission to do that.", ephemeral=True)
            return

        timezone_name, zone = await self._guild_zone(guild)
        retry = RetryCreateView(self, member.id, timezone_name, values)
        now = datetime.now(zone)
        try:
            starts_at = parse_start(values["starts"], zone, now)
            duration = parse_duration(values["duration"])
            capacity = parse_capacity(values["capacity"])
        except ParseError as error:
            await interaction.response.send_message(f"⚠️ {error}", view=retry, ephemeral=True)
            return
        if starts_at <= int(now.timestamp()):
            await interaction.response.send_message(
                f"⚠️ <t:{starts_at}:F> is already in the past. Pick a time in the future (times use **{timezone_name}**).",
                view=retry,
                ephemeral=True,
            )
            return

        chosen = str(values.get("channel_id") or "")
        destination = guild.get_channel(int(chosen)) if chosen.isdigit() else None
        if not isinstance(destination, discord.TextChannel):
            destination = await self._announce_destination(guild, interaction.channel)
        problem = None
        if destination is None:
            problem = "I could not find a text channel to post in."
        else:
            perms = destination.permissions_for(guild.me)
            mine = destination.permissions_for(member)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                problem = f"I need View Channel, Send Messages and Embed Links in {destination.mention}."
            elif not (mine.view_channel and mine.send_messages):
                problem = f"You can't post in {destination.mention}, so I won't post there for you."
        if problem:
            await interaction.response.send_message(f"⚠️ {problem} Nothing was created.", view=retry, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        title = self._clean_line(values["title"]) or "Untitled event"
        location = self._clean_line(values["location"])[:500]
        template_key = values.get("template") or ""
        template = (await self.config.guild(guild).templates()).get(template_key) if template_key else None
        event = await self.create_draft_service(
            guild,
            actor_id=member.id,
            starts_at=starts_at,
            capacity=capacity,
            title=title,
            duration_minutes=duration,
            source_type="template" if template else "manual",
        )
        event_id = int(event["event_id"])

        async with self._lock(guild.id):
            events, stored = await self._event(guild, event_id)
            if template:
                self._apply_template(stored, template)
            stored["location"] = location  # what the creator saw and confirmed wins over the template
            stored["post_channel_id"] = destination.id
            await self._save(guild, events, stored)
            event = stored

        await self._audit(guild, "event created", "completed", f"Event #{event_id} by {member.id} via modal")
        embed = self._manage_embed(event)
        embed.title = f"Event #{event_id} saved as a draft"
        embed.description = (
            f"**{discord.utils.escape_markdown(event['title'])}**\n"
            "Nothing is posted yet. Finish editing with the buttons below, then press **Post to channel**."
        )
        embed.set_footer(text=f"Times are in {timezone_name}. Find unposted events later with the Drafts button on the panel.")
        await interaction.followup.send(embed=embed, view=EventManageView(self, member.id, event_id), ephemeral=True)

    @staticmethod
    def _clean_line(value: str) -> str:
        return " ".join(str(value or "").split())

    async def open_edit_modal(self, interaction: discord.Interaction, event_id: int) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not await self._can_manage_events(interaction.user):
            await interaction.response.send_message("You need the **Manage Events** permission to do that.", ephemeral=True)
            return
        try:
            _events, event = await self._event(interaction.guild, event_id)
        except commands.BadArgument:
            await interaction.response.send_message("That event no longer exists.", ephemeral=True)
            return
        await interaction.response.send_modal(EditEventModal(self, event))

    async def handle_edit_submit(
        self, interaction: discord.Interaction, event_id: int, title: str, description: str, location: str
    ) -> None:
        guild = interaction.guild
        if not guild or not isinstance(interaction.user, discord.Member) or not await self._can_manage_events(interaction.user):
            await interaction.response.send_message("You need the **Manage Events** permission to do that.", ephemeral=True)
            return
        async with self._lock(guild.id):
            try:
                events, event = await self._event(guild, event_id)
            except commands.BadArgument:
                await interaction.response.send_message("That event no longer exists.", ephemeral=True)
                return
            event["title"] = self._clean_line(title)[:200] or event["title"]
            event["description"] = description.strip()[:2000]
            event["location"] = self._clean_line(location)[:500]
            await self._save(guild, events, event)
        await interaction.response.send_message(f"Event **#{event_id}** updated.", ephemeral=True)
        await self._refresh(guild, event)

    async def _staff_event(
        self, interaction: discord.Interaction, event_id: int | None = None
    ) -> tuple[discord.Guild, discord.Member, dict[str, Any], dict[str, Any]] | None:
        """Common guard: staff member, in a guild, event exists. Replies and returns None otherwise."""
        guild = interaction.guild
        member = interaction.user
        if not guild or not isinstance(member, discord.Member) or not await self._can_manage_events(member):
            await interaction.response.send_message("You need the **Manage Events** permission to do that.", ephemeral=True)
            return None
        try:
            if event_id is None:
                events, event = await self._event_for_message(guild, interaction.message.id if interaction.message else 0)
                if not event:
                    raise commands.BadArgument("This event is no longer tracked.")
            else:
                events, event = await self._event(guild, event_id)
        except commands.BadArgument as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return None
        return guild, member, events, event

    def _manage_embed(self, event: dict[str, Any]) -> discord.Embed:
        image = "set" if event.get("image_url") else "none"
        embed = discord.Embed(
            title=f"Manage event #{event['event_id']}",
            description=f"**{event['title']}**\nStarts <t:{event['starts_at']}:F>",
            color=self.COLOR,
        )
        embed.add_field(name="Requirements", value=str(len(self._requirements(event))))
        embed.add_field(name="Roles", value=str(len(self._roles(event))))
        embed.add_field(name="Image", value=image)
        posted = event.get("channel_id") and event.get("message_id")
        status = str(event["status"]).title() + (" (not posted yet)" if event["status"] == "draft" and not posted else "")
        embed.add_field(name="Status", value=status)
        if event.get("post_channel_id") or posted:
            embed.add_field(name="Channel", value=f"<#{event.get('channel_id') if posted else event['post_channel_id']}>")
        pings = " ".join(f"<@&{role_id}>" for role_id in event.get("ping_role_ids") or [])
        if pings:
            embed.add_field(name="Pings on post", value=pings)
        return embed

    async def show_manage_menu(self, interaction: discord.Interaction, event_id: int) -> None:
        """Replace this private message with the manage menu (used by Back and the draft picker)."""
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        _guild, member, _events, event = context
        await interaction.response.edit_message(
            content=None, embed=self._manage_embed(event), view=EventManageView(self, member.id, event_id)
        )

    async def open_drafts(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if not guild or not isinstance(interaction.user, discord.Member) or not await self._can_manage_events(interaction.user):
            await interaction.response.send_message("You need the **Manage Events** permission to do that.", ephemeral=True)
            return
        events = await self.config.guild(guild).events()
        drafts = sorted(
            (event for event in events.values() if event.get("status") == "draft" and not event.get("message_id")),
            key=lambda event: int(event["starts_at"]),
        )
        if not drafts:
            await interaction.response.send_message("No unposted drafts. Press **Create Event** to start one.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"{len(drafts)} unposted draft(s):", view=DraftPickerView(self, interaction.user.id, drafts), ephemeral=True
        )

    # ---- posting -------------------------------------------------------------------------------

    async def open_post_menu(self, interaction: discord.Interaction, event_id: int) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        guild, member, _events, event = context
        if event.get("message_id"):
            await interaction.response.send_message(
                f"Event #{event_id} is already posted in <#{event.get('channel_id')}>.", ephemeral=True
            )
            return
        if event["status"] != "draft":
            await interaction.response.send_message(f"This event is {event['status']}, so it can't be posted.", ephemeral=True)
            return
        chosen = guild.get_channel(event.get("post_channel_id")) if event.get("post_channel_id") else None
        channel = chosen if isinstance(chosen, discord.TextChannel) else await self._announce_destination(guild, interaction.channel)
        roles = [role for role in (guild.get_role(rid) for rid in event.get("ping_role_ids") or []) if role]
        await interaction.response.edit_message(
            content="Choose where to post the event and which roles to ping (optional), then press **Post now**.",
            embed=None,
            view=PostEventView(self, member.id, event_id, channel, roles),
        )

    async def post_from_menu(
        self, interaction: discord.Interaction, event_id: int, channel_id: int | None, role_ids: list[int]
    ) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        guild, member, _events, _event = context
        destination = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(destination, discord.TextChannel):
            await interaction.response.send_message("Pick a text channel first.", ephemeral=True)
            return
        roles = [role for role in (guild.get_role(rid) for rid in role_ids) if role and not role.is_default()]
        problem = self._post_problem(guild, member, destination, roles)
        if problem:
            await interaction.response.send_message(f"⚠️ {problem} Nothing was posted.", ephemeral=True)
            return
        try:
            message = await self.publish_event(guild, event_id, destination, [role.id for role in roles])
        except commands.BadArgument as error:
            await interaction.response.send_message(f"⚠️ {error}", ephemeral=True)
            return
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not post EventCheckin event %s in guild %s.", event_id, guild.id)
            await interaction.response.send_message(
                f"⚠️ Discord would not let me post in {destination.mention}. Check my permissions there. Nothing was posted.",
                ephemeral=True,
            )
            return
        await self._audit(guild, "event posted", "completed", f"Event #{event_id} by {member.id} in {destination.id}")
        _events, event = await self._event(guild, event_id)
        pinged = " Pinged " + " ".join(role.mention for role in roles) + "." if roles else ""
        await interaction.response.edit_message(
            content=f"✅ Posted in {destination.mention}: {message.jump_url}.{pinged}",
            embed=self._manage_embed(event),
            view=EventManageView(self, member.id, event_id),
        )

    def _post_problem(
        self,
        guild: discord.Guild,
        member: discord.Member,
        destination: discord.TextChannel,
        roles: list[discord.Role],
    ) -> str | None:
        mine = destination.permissions_for(guild.me)
        yours = destination.permissions_for(member)
        if not (mine.view_channel and mine.send_messages and mine.embed_links):
            return f"I need View Channel, Send Messages and Embed Links in {destination.mention}."
        if not (yours.view_channel and yours.send_messages):
            return f"You can't post in {destination.mention}, so I won't post there for you."
        blocked = [role for role in roles if not role.mentionable]
        if blocked and not mine.mention_everyone:
            names = ", ".join(f"**{discord.utils.escape_markdown(role.name)}**" for role in blocked)
            return f"I can't ping {names} (not mentionable, and I lack Mention Everyone in {destination.mention})."
        return None

    async def publish_event(
        self,
        guild: discord.Guild,
        event_id: int,
        destination: discord.TextChannel,
        ping_role_ids: list[int] | None = None,
    ) -> discord.Message:
        """Post (or re-post) an event's registration panel and open registration."""
        async with self._lock(guild.id):
            events, event = await self._event(guild, event_id)
            if event.get("message_id") and event["status"] != "draft":
                raise commands.BadArgument(f"Event #{event_id} is already posted.")
            if event["status"] in {"finalized", "cancelled"}:
                raise commands.BadArgument(f"Event #{event_id} is {event['status']}.")
            if ping_role_ids is not None:
                event["ping_role_ids"] = [int(rid) for rid in ping_role_ids][:MAX_PING_ROLES]
            roles = [role for role in (guild.get_role(rid) for rid in event.get("ping_role_ids") or []) if role and not role.is_default()]
            preview = {**event, "status": "open"}
            message = await destination.send(
                content=" ".join(role.mention for role in roles) or None,
                embed=self._embed(preview),
                view=EventControls(self, preview),
                allowed_mentions=discord.AllowedMentions(roles=roles, users=False, everyone=False),
            )
            event.update(status="open", channel_id=destination.id, message_id=message.id, post_channel_id=destination.id)
            await self._save(guild, events, event)
            return message

    async def open_manage_menu(self, interaction: discord.Interaction) -> None:
        context = await self._staff_event(interaction)
        if context is None:
            return
        _guild, member, _events, event = context
        await interaction.response.send_message(
            embed=self._manage_embed(event), view=EventManageView(self, member.id, int(event["event_id"])), ephemeral=True
        )

    async def handle_requirement(self, interaction: discord.Interaction, slot: int) -> None:
        """A member pressed one of an event's requirement buttons."""
        guild = interaction.guild
        if not guild or not interaction.message:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        async with self._lock(guild.id):
            events, event = await self._event_for_message(guild, interaction.message.id)
            if not event or events is None:
                await interaction.response.send_message("This event is no longer tracked.", ephemeral=True)
                return
            if event["status"] != "open":
                await interaction.response.send_message("This event is closed.", ephemeral=True)
                return
            requirements = self._requirements(event)
            requirement = next((item for item in requirements if item["id"] == slot), None)
            if requirement is None:
                await interaction.response.send_message(
                    "That requirement was removed. The event post will refresh shortly.", ephemeral=True
                )
                await self._refresh(guild, event)
                return
            confirmed = event.setdefault("acks", {}).setdefault(str(interaction.user.id), [])
            if slot not in confirmed:
                confirmed.append(slot)
                await self._save(guild, events, event)
            missing = self._missing_requirements(event, interaction.user.id)
            attendee = event.get("attendees", {}).get(str(interaction.user.id)) or {}
            registered = attendee.get("status") in {"going", "waitlist", "checked_in"}
        done = len(requirements) - len(missing)
        if not missing:
            tail = "You are registered." if registered else "Now press **RSVP** to join."
            text = f"✅ Confirmed. All {len(requirements)} requirement(s) done. {tail}"
        else:
            text = f"✅ Confirmed: {requirement['text']} ({done}/{len(requirements)}).\nStill to confirm:\n" + self._missing_text(missing)
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def open_requirements_modal(self, interaction: discord.Interaction, event_id: int) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is not None:
            await interaction.response.send_modal(EditRequirementsModal(self, context[3]))

    async def handle_requirements_submit(self, interaction: discord.Interaction, event_id: int, raw: str) -> None:
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        lines = [line for line in lines if line]
        too_long = [line for line in lines if len(line) > REQUIREMENT_LENGTH]
        if too_long:
            await interaction.response.send_message(
                f"⚠️ Each requirement becomes a button, so it can be at most {REQUIREMENT_LENGTH} characters. "
                f"Shorten: *{too_long[0][:60]}...* Nothing was changed.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if len(set(lines)) > MAX_REQUIREMENTS:
            await interaction.response.send_message(
                f"⚠️ An event can have at most {MAX_REQUIREMENTS} requirements. Nothing was changed.", ephemeral=True
            )
            return
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        guild = context[0]
        async with self._lock(guild.id):
            events, event = await self._event(guild, event_id)
            self._set_requirements(event, lines)
            count = len(self._requirements(event))
            await self._save(guild, events, event)
        await interaction.response.send_message(
            f"Requirements updated: **{count}** on event #{event_id}."
            + (" Members confirm each one when they press RSVP." if count else " RSVP no longer needs confirmations."),
            ephemeral=True,
        )
        await self._refresh(guild, event)

    async def open_image_modal(self, interaction: discord.Interaction, event_id: int) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is not None:
            await interaction.response.send_modal(EventImageModal(self, context[3]))

    @staticmethod
    def _valid_image_url(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not any(c.isspace() for c in url) and len(url) <= 500

    async def handle_image_submit(self, interaction: discord.Interaction, event_id: int, url: str) -> None:
        url = url.strip()
        if not self._valid_image_url(url):
            await interaction.response.send_message(
                "⚠️ That doesn't look like a web link. It should start with `https://` and point straight at an image.",
                ephemeral=True,
            )
            return
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        guild = context[0]
        await interaction.response.defer(ephemeral=True)
        async with self._lock(guild.id):
            events, event = await self._event(guild, event_id)
            updated = {**event, "image_url": url}
            try:
                await self._refresh(guild, updated, strict=True)
            except commands.BadArgument as error:
                await interaction.followup.send(f"⚠️ {error}", ephemeral=True)
                return
            except discord.Forbidden:
                await interaction.followup.send("⚠️ I couldn't edit the event post. Check my permissions in that channel.", ephemeral=True)
                return
            except discord.HTTPException as error:
                await interaction.followup.send(f"⚠️ Discord rejected that image link: {error.text or error}", ephemeral=True)
                return
            await self._save(guild, events, updated)
        await interaction.followup.send(
            "🖼️ Image set. If the post shows no picture, the link probably isn't a direct image link.", ephemeral=True
        )

    async def handle_remove_image(self, interaction: discord.Interaction, event_id: int) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        guild = context[0]
        if not context[3].get("image_url"):
            await interaction.response.send_message("This event has no image.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with self._lock(guild.id):
            events, event = await self._event(guild, event_id)
            updated = {**event, "image_url": None}
            try:
                await self._refresh(guild, updated, strict=True)
            except (commands.BadArgument, discord.HTTPException) as error:
                await interaction.followup.send(f"⚠️ I couldn't update the post: {error}", ephemeral=True)
                return
            await self._save(guild, events, updated)
        await interaction.followup.send("Image removed.", ephemeral=True)

    async def open_save_template_modal(self, interaction: discord.Interaction, event_id: int) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is not None:
            await interaction.response.send_modal(SaveTemplateModal(self, context[3]))

    async def handle_save_template_submit(self, interaction: discord.Interaction, event_id: int, name: str) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        guild, _member, _events, event = context
        display = self._clean_line(name)[:50]
        key = display.casefold()
        if not key:
            await interaction.response.send_message("Give the template a name.", ephemeral=True)
            return
        async with self.config.guild(guild).templates() as templates:
            if key not in templates and len(templates) >= 25:
                await interaction.response.send_message(
                    "⚠️ A server can keep at most 25 templates. Remove one with the `template remove` command first.",
                    ephemeral=True,
                )
                return
            replaced = key in templates
            templates[key] = self._template_from_event(event, display)
        extras = []
        if self._requirements(event):
            extras.append(f"{len(self._requirements(event))} requirement(s)")
        if event.get("image_url"):
            extras.append("the image link")
        await interaction.response.send_message(
            f"💾 {'Replaced' if replaced else 'Saved'} template **{discord.utils.escape_markdown(display)}**"
            + (f" with {', '.join(extras)}." if extras else "."),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---- roles: staff editor ------------------------------------------------------------------

    def _roles_embed(self, event: dict[str, Any]) -> discord.Embed:
        roles = self._roles(event)
        embed = discord.Embed(title=f"Roles - event #{event['event_id']}", color=self.COLOR)
        if not roles:
            embed.description = "No roles yet. Without roles, everyone simply RSVPs. Add one to let members pick a role."
            return embed
        lines = []
        for role in roles:
            slots = int(role.get("slots") or 0)
            line = f"**{discord.utils.escape_markdown(role['name'])}** - {self._role_filled(event, role['id'])}/{slots or '∞'}"
            if role.get("requirements"):
                line += "\n" + "\n".join(f"  • {text}" for text in role["requirements"])
            lines.append(line)
        embed.description = "\n".join(lines)[:4000]
        embed.set_footer(text=f"{len(roles)}/{MAX_ROLES} roles")
        return embed

    async def open_roles_editor(self, interaction: discord.Interaction, event_id: int) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        _guild, member, _events, event = context
        await interaction.response.send_message(
            embed=self._roles_embed(event), view=RolesEditorView(self, member.id, event), ephemeral=True
        )

    async def open_role_modal(self, interaction: discord.Interaction, event_id: int, role_id: int | None) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        event = context[3]
        role = self._role(event, role_id) if role_id is not None else None
        if role_id is not None and role is None:
            await interaction.response.send_message("That role no longer exists.", ephemeral=True)
            return
        if role is None and len(self._roles(event)) >= MAX_ROLES:
            await interaction.response.send_message(f"An event can have at most {MAX_ROLES} roles.", ephemeral=True)
            return
        await interaction.response.send_modal(RoleModal(self, event_id, role))

    async def handle_role_submit(
        self,
        interaction: discord.Interaction,
        event_id: int,
        role_id: int | None,
        name: str,
        slots_raw: str,
        requirements_raw: str,
    ) -> None:
        name = self._clean_line(name)[:ROLE_NAME_LENGTH]
        slots_raw = slots_raw.strip()
        lines: list[str] = []
        for line in requirements_raw.splitlines():
            line = " ".join(line.split())
            if line and line not in lines:
                lines.append(line)
        problem = None
        if not name:
            problem = "Give the role a name."
        elif not (slots_raw.isascii() and slots_raw.isdigit()) or int(slots_raw) > MAX_ROLE_SLOTS:
            problem = f"Slots must be a number from 0 to {MAX_ROLE_SLOTS} (0 means unlimited)."
        elif any(len(line) > REQUIREMENT_LENGTH for line in lines):
            problem = f"Each requirement can be at most {REQUIREMENT_LENGTH} characters."
        elif len(lines) > MAX_ROLE_REQUIREMENTS:
            problem = f"A role can have at most {MAX_ROLE_REQUIREMENTS} requirements."
        if problem:
            await interaction.response.send_message(f"⚠️ {problem} Nothing was changed.", ephemeral=True)
            return
        slots = int(slots_raw)
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        guild, member = context[0], context[1]
        async with self._lock(guild.id):
            events, event = await self._event(guild, event_id)
            roles = event.setdefault("roles", [])
            clash = next((item for item in roles if item["name"].casefold() == name.casefold()), None)
            if clash is not None and clash["id"] != role_id:
                await interaction.response.send_message(f"⚠️ There is already a role called **{clash['name']}**.", ephemeral=True)
                return
            if role_id is None:
                if len(roles) >= MAX_ROLES:
                    await interaction.response.send_message(f"⚠️ An event can have at most {MAX_ROLES} roles.", ephemeral=True)
                    return
                used = {item["id"] for item in roles}
                role = {"id": next(i for i in range(1, MAX_ROLES + 1) if i not in used)}
                roles.append(role)
            else:
                role = self._role(event, role_id)
                if role is None:
                    await interaction.response.send_message("⚠️ That role no longer exists.", ephemeral=True)
                    return
                filled = self._role_filled(event, role_id)
                if slots and slots < filled:
                    await interaction.response.send_message(
                        f"⚠️ **{role['name']}** already has {filled} member(s). Ask someone to switch or withdraw first, "
                        "or choose at least that many slots.",
                        ephemeral=True,
                    )
                    return
            role.update(name=name, slots=slots, requirements=lines)
            promoted = self._promote_all(event)
            await self._save(guild, events, event)
        await interaction.response.send_message(
            f"🎭 Saved role **{discord.utils.escape_markdown(name)}**.",
            embed=self._roles_embed(event),
            view=RolesEditorView(self, member.id, event),
            ephemeral=True,
        )
        await self._refresh(guild, event)
        await self._announce_promotions(guild, event, promoted)

    async def handle_role_remove(self, interaction: discord.Interaction, event_id: int, role_id: int) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        guild, member = context[0], context[1]
        async with self._lock(guild.id):
            events, event = await self._event(guild, event_id)
            role = self._role(event, role_id)
            if role is None:
                await interaction.response.send_message("That role no longer exists.", ephemeral=True)
                return
            event["roles"] = [item for item in self._roles(event) if item["id"] != role_id]
            affected = 0
            for record in event.get("attendees", {}).values():
                if record.get("role_id") == role_id:
                    record["role_id"] = None
                    affected += record.get("status") in {"going", "checked_in", "waitlist"}
            promoted = self._promote_all(event)
            await self._save(guild, events, event)
        note = f" {affected} member(s) now have no role and can press RSVP to pick another." if affected else ""
        await interaction.response.send_message(
            f"Removed role **{discord.utils.escape_markdown(role['name'])}**.{note}",
            embed=self._roles_embed(event),
            view=RolesEditorView(self, member.id, event),
            ephemeral=True,
        )
        await self._refresh(guild, event)
        await self._announce_promotions(guild, event, promoted)

    # ---- roles: member sign-up ----------------------------------------------------------------

    async def complete_event_requirements(
        self, interaction: discord.Interaction, event_id: int, shown_ids: list[int]
    ) -> None:
        """Record the member's event-requirement confirmations, then continue to roles or sign them up."""
        guild = interaction.guild
        member = interaction.user
        if not guild or not isinstance(member, discord.Member):
            return
        async with self._lock(guild.id):
            try:
                events, event = await self._event(guild, event_id)
            except commands.BadArgument:
                await interaction.response.edit_message(content="That event no longer exists.", view=None)
                return
            if event["status"] != "open":
                await interaction.response.edit_message(content="Registration for this event is closed.", view=None)
                return
            current_ids = [int(item["id"]) for item in self._requirements(event)]
            if set(current_ids) - set(shown_ids):
                await interaction.response.edit_message(
                    content="The requirements changed while you were reading. Please tick the updated list:",
                    view=EventRequirementsView(self, member.id, event),
                )
                return
            confirmed = event.setdefault("acks", {}).setdefault(str(member.id), [])
            for slot in current_ids:
                if slot not in confirmed:
                    confirmed.append(slot)
            await self._save(guild, events, event)
            current = event.get("attendees", {}).get(str(member.id)) or {}
            status = current.get("status")
            has_roles = bool(self._roles(event))
            view = RolePickerView(self, member.id, event) if has_roles else None
        if status == "checked_in":
            await interaction.response.edit_message(content="✅ Confirmed. You are already checked in.", view=None)
            return
        if has_roles:
            held = self._role(event, current.get("role_id"))
            if held and status in {"going", "waitlist"}:
                text = f"✅ Requirements confirmed. You are **{discord.utils.escape_markdown(held['name'])}**. Pick a different role to switch."
            else:
                text = "✅ Requirements confirmed.\n**Step 2: pick the role you want to play.**"
            await interaction.response.edit_message(content=text, view=view)
            return
        if status in {"going", "waitlist"}:
            await interaction.response.edit_message(content="✅ Requirements confirmed. You are already registered.", view=None)
            return
        await self.complete_plain_signup(interaction, event_id)

    async def complete_plain_signup(self, interaction: discord.Interaction, event_id: int) -> None:
        """Sign a member up for an event that has no roles (last step of the walkthrough)."""
        guild = interaction.guild
        member = interaction.user
        if not guild or not isinstance(member, discord.Member):
            return
        async with self._lock(guild.id):
            try:
                events, event = await self._event(guild, event_id)
            except commands.BadArgument:
                await interaction.response.edit_message(content="That event no longer exists.", view=None)
                return
            if event["status"] != "open":
                await interaction.response.edit_message(content="Registration for this event is closed.", view=None)
                return
            if self._missing_requirements(event, member.id):
                await interaction.response.edit_message(
                    content="The event requirements changed. Press **RSVP** again to confirm them.", view=None
                )
                return
            attendees = event.setdefault("attendees", {})
            current = attendees.get(str(member.id))
            if current and current.get("status") in {"going", "waitlist", "checked_in"}:
                await interaction.response.edit_message(content="You are already registered.", view=None)
                return
            going = sum(record.get("status") in {"going", "checked_in"} for record in attendees.values())
            capacity = int(event.get("capacity") or 0)
            status = "waitlist" if capacity and going >= capacity else "going"
            now = self._now()
            attendees[str(member.id)] = {
                "user_id": member.id,
                "status": status,
                "registered_at": current.get("registered_at", now) if current else now,
                "checked_in_at": None,
                "promoted_at": current.get("promoted_at") if current else None,
                "staff_actor_id": None,
            }
            await self._save(guild, events, event)
        text = "✅ Requirements confirmed. You are registered." if status == "going" else "✅ Requirements confirmed. The event is full; you joined the waitlist."
        await interaction.response.edit_message(content=text, view=None)
        await self._refresh(guild, event)

    async def handle_role_pick(self, interaction: discord.Interaction, event_id: int, role_id: int) -> None:
        guild = interaction.guild
        if not guild or not isinstance(interaction.user, discord.Member):
            return
        try:
            _events, event = await self._event(guild, event_id)
        except commands.BadArgument:
            await interaction.response.edit_message(content="That event no longer exists.", view=None)
            return
        role = self._role(event, role_id)
        if event["status"] != "open" or role is None:
            text = "Registration for this event is closed." if event["status"] != "open" else "That role was removed."
            await interaction.response.edit_message(content=text, view=None)
            return
        if role.get("requirements"):
            await interaction.response.edit_message(
                content=f"**{discord.utils.escape_markdown(role['name'])}** needs you to confirm each of these. Tick them all, then confirm:",
                view=RoleRequirementsView(self, interaction.user.id, event_id, role),
            )
            return
        await self.complete_role_signup(interaction, event_id, role_id)

    async def complete_role_signup(self, interaction: discord.Interaction, event_id: int, role_id: int) -> None:
        """Seat the member in a role (or on that role's waitlist). Called from the private role prompts."""
        guild = interaction.guild
        member = interaction.user
        if not guild or not isinstance(member, discord.Member):
            return

        async def reply(text: str) -> None:
            await interaction.response.edit_message(content=text, embed=None, view=None)

        promoted: list[int] = []
        async with self._lock(guild.id):
            try:
                events, event = await self._event(guild, event_id)
            except commands.BadArgument:
                await reply("That event no longer exists.")
                return
            role = self._role(event, role_id)
            if event["status"] != "open":
                await reply("Registration for this event is closed.")
                return
            if role is None:
                await reply("That role was removed. Press RSVP again to pick another.")
                return
            missing = self._missing_requirements(event, member.id)
            if missing:
                await reply("The event requirements changed. Press **RSVP** again to confirm them first.")
                return
            attendees = event.setdefault("attendees", {})
            key = str(member.id)
            current = attendees.get(key)
            role_label = discord.utils.escape_markdown(discord.utils.escape_mentions(role["name"]))
            if current and current.get("status") == "checked_in":
                await reply("You are already checked in.")
                return
            if current and current.get("role_id") == role_id and current.get("status") in {"going", "waitlist"}:
                waiting = current.get("status") == "waitlist"
                await reply(f"You are already {'waiting for' if waiting else 'signed up as'} **{role_label}**.")
                return

            was_going = bool(current and current.get("status") == "going")
            slots = int(role.get("slots") or 0)
            capacity = int(event.get("capacity") or 0)
            role_full = bool(slots) and self._role_filled(event, role_id, exclude=member.id) >= slots
            others_going = sum(
                record.get("status") in {"going", "checked_in"} and record.get("user_id") != member.id
                for record in attendees.values()
            )
            event_full = bool(capacity) and others_going >= capacity
            if was_going and role_full:
                await reply(f"**{role_label}** is full, so you stay as you are. Withdraw first if you'd rather wait for it.")
                return
            status = "waitlist" if (role_full or event_full) else "going"
            now = self._now()
            attendees[key] = {
                "user_id": member.id,
                "status": status,
                "registered_at": current.get("registered_at", now) if current else now,
                "checked_in_at": current.get("checked_in_at") if current else None,
                "promoted_at": current.get("promoted_at") if current else None,
                "staff_actor_id": None,
                "role_id": role_id,
            }
            if was_going:
                promoted = self._promote_all(event)  # the role they left has a free seat
            await self._save(guild, events, event)

        if status == "going":
            text = f"✅ You're in as **{role_label}**."
        elif role_full:
            text = f"⏳ **{role_label}** is full. You're on its waitlist and will be pinged if a spot opens."
        else:
            text = f"⏳ The event is full. You're on the waitlist as **{role_label}**."
        await reply(text)
        await self._refresh(guild, event)
        await self._announce_promotions(guild, event, promoted)

    # ---- Manage menu: attendees, finalize, cancel ------------------------------------------------

    def _attendees_embed(self, event: dict[str, Any]) -> discord.Embed:
        embed = discord.Embed(title=f"Attendees - {event['title']}"[:256], color=self.COLOR)
        attendees = sorted(event.get("attendees", {}).values(), key=lambda record: record.get("registered_at", 0))
        for status, title in (
            ("checked_in", "📍 Checked in"),
            ("going", "✅ Going"),
            ("waitlist", "⏳ Waiting"),
            ("no_show", "❌ No-show"),
        ):
            rows = [record for record in attendees if record.get("status") == status]
            if not rows:
                continue
            lines = []
            for record in rows[:25]:
                role = self._role(event, record.get("role_id"))
                lines.append(f"<@{record['user_id']}>" + (f" - {role['name']}" if role else ""))
            if len(rows) > 25:
                lines.append(f"+{len(rows) - 25} more (use the export command for the full list)")
            embed.add_field(name=f"{title} ({len(rows)})", value="\n".join(lines)[:1024], inline=False)
        if not embed.fields:
            embed.description = "Nobody has registered yet."
        return embed

    async def show_attendees(self, interaction: discord.Interaction, event_id: int) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is not None:
            await interaction.response.send_message(
                embed=self._attendees_embed(context[3]), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )

    async def request_finalize(self, interaction: discord.Interaction, event_id: int) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        guild, member, _events, event = context
        if event["status"] in {"finalized", "cancelled"}:
            await interaction.response.send_message(f"This event is already {event['status']}.", ephemeral=True)
            return
        if event["status"] == "draft":
            await interaction.response.send_message("Post the event first; a draft has nothing to finalize.", ephemeral=True)
            return
        records = list(event.get("attendees", {}).values())
        arrived = sum(bool(record.get("checked_in_at")) for record in records)
        no_shows = sum(record.get("status") == "going" and not record.get("checked_in_at") for record in records)
        role = guild.get_role(event["reward_role_id"]) if event.get("reward_role_id") else None
        reward = f"{arrived} checked-in member(s) receive {role.mention}" if role else "no reward role is set"
        await interaction.response.send_message(
            f"Finalize **{discord.utils.escape_markdown(event['title'])}**?\n"
            f"• Registration closes\n• {no_shows} confirmed RSVP(s) who never checked in become no-shows\n• {reward}\n"
            "This can't be undone.",
            view=ConfirmActionView(
                member.id,
                lambda it: self._run_finalize(it, event_id),
                confirm_label="Finalize",
                confirm_style=discord.ButtonStyle.primary,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _run_finalize(self, interaction: discord.Interaction, event_id: int) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        guild, member = context[0], context[1]
        await interaction.response.defer()
        try:
            checked, no_shows, rewards, errors = await self._finalize_event(guild, event_id, member)
        except commands.BadArgument as error:
            await interaction.edit_original_response(content=f"⚠️ {error}", view=None)
            return
        await interaction.edit_original_response(
            content=f"🏁 Finalized: **{checked}** checked in, **{no_shows}** no-show(s), "
            f"**{rewards}** reward role(s) given, **{errors}** error(s).",
            view=None,
        )

    async def _finalize_event(
        self, guild: discord.Guild, event_id: int, actor: discord.abc.User
    ) -> tuple[int, int, int, int]:
        """Close registration, mark unarrived RSVPs as no-shows, award the role. Returns the four counts."""
        checked = no_shows = 0
        reward_ids: list[int] = []
        async with self._lock(guild.id):
            events, event = await self._event(guild, event_id)
            if event["status"] in {"finalized", "cancelled"}:
                raise commands.BadArgument(f"This event is already {event['status']}.")
            for record in event.get("attendees", {}).values():
                if record.get("checked_in_at"):
                    record["status"] = "checked_in"
                    checked += 1
                    reward_ids.append(record["user_id"])
                elif record.get("status") == "going":
                    record["status"] = "no_show"
                    no_shows += 1
            event.update(status="finalized", finalized_at=self._now(), finalized_by=actor.id)
            await self._save(guild, events, event)
        rewards = errors = 0
        role = guild.get_role(event["reward_role_id"]) if event.get("reward_role_id") else None
        if role:  # role changes are slow API calls, so they happen outside the lock
            for user_id in reward_ids:
                target = guild.get_member(user_id)
                if target and role not in target.roles:
                    try:
                        await target.add_roles(role, reason=f"EventCheckin event #{event_id} attendance")
                        rewards += 1
                    except discord.HTTPException:
                        errors += 1
        await self._refresh(guild, event)
        await self._log(
            guild,
            f"Event **#{event_id} {event['title']}** finalized: {checked} checked in, "
            f"{no_shows} no-show(s), {rewards} reward(s), {errors} error(s).",
        )
        return checked, no_shows, rewards, errors

    async def open_cancel_modal(self, interaction: discord.Interaction, event_id: int) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        if context[3]["status"] in {"finalized", "cancelled"}:
            await interaction.response.send_message(f"This event is already {context[3]['status']}.", ephemeral=True)
            return
        await interaction.response.send_modal(CancelEventModal(self, context[3]))

    async def handle_cancel_submit(self, interaction: discord.Interaction, event_id: int, reason: str) -> None:
        context = await self._staff_event(interaction, event_id)
        if context is None:
            return
        guild, member = context[0], context[1]
        await interaction.response.defer(ephemeral=True)
        try:
            notified = await self._cancel_event(guild, event_id, member, reason.strip())
        except commands.BadArgument as error:
            await interaction.followup.send(f"⚠️ {error}", ephemeral=True)
            return
        await interaction.followup.send(
            f"🚫 Event **#{event_id}** cancelled. {notified} registered member(s) were notified by DM.", ephemeral=True
        )

    async def _cancel_event(self, guild: discord.Guild, event_id: int, actor: discord.abc.User, reason: str) -> int:
        """Cancel without deleting attendance, update the post, tell the channel and DM registrants."""
        async with self._lock(guild.id):
            events, event = await self._event(guild, event_id)
            if event["status"] in {"finalized", "cancelled"}:
                raise commands.BadArgument(f"This event is already {event['status']}.")
            event.update(status="cancelled", cancel_reason=reason[:500], finalized_at=self._now(), finalized_by=actor.id)
            await self._save(guild, events, event)
        await self._refresh(guild, event)
        notified = 0
        for record in list(event.get("attendees", {}).values())[:100]:
            target = guild.get_member(record.get("user_id"))
            if target:
                with suppress(discord.Forbidden, discord.HTTPException):
                    await target.send(
                        f"**{event['title']}** in **{guild.name}** was cancelled."
                        + (f"\nReason: {reason[:500]}" if reason else "")
                    )
                    notified += 1
        channel = guild.get_channel(event.get("channel_id")) if event.get("channel_id") else None
        if isinstance(channel, discord.TextChannel):
            with suppress(discord.HTTPException):
                await channel.send(
                    f"**{event['title']}** was cancelled." + (f"\nReason: {reason[:500]}" if reason else ""),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        await self._audit(guild, "event cancellation", "completed", f"Event #{event_id}; {notified} DM(s)")
        return notified

    async def handle_discord_event_button(self, interaction: discord.Interaction, event_id: int) -> None:
        guild = interaction.guild
        if not guild or not isinstance(interaction.user, discord.Member) or not await self._can_manage_events(interaction.user):
            await interaction.response.send_message("You need the **Manage Events** permission to do that.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            scheduled = await self._create_discord_event(guild, event_id, interaction.user)
        except commands.BadArgument as error:
            await interaction.followup.send(f"⚠️ {error}", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("⚠️ I need the **Manage Events** permission to do that.", ephemeral=True)
        except discord.HTTPException as error:
            await interaction.followup.send(f"⚠️ Discord rejected the scheduled event: {error.text or error}", ephemeral=True)
        else:
            await interaction.followup.send(f"Added **{scheduled.name}** to the server's Events tab.", ephemeral=True)

    async def handle_modal_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("EventCheckin modal failed.", exc_info=(type(error), error, error.__traceback__))
        message = "Something went wrong handling that form. Nothing was changed; please try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        for guild_id in await self.config.all_guilds():
            conf = self.config.guild_from_id(guild_id)
            events = await conf.events()
            changed = False
            for event in events.values():
                if event.get("created_by") == user_id:
                    event["created_by"] = None
                    changed = True
                if event.get("finalized_by") == user_id:
                    event["finalized_by"] = None
                    changed = True
                if event.get("attendees", {}).pop(str(user_id), None):
                    self._promote(event)
                    changed = True
                if (event.get("acks") or {}).pop(str(user_id), None) is not None:
                    changed = True
                for attendee in event.get("attendees", {}).values():
                    if attendee.get("staff_actor_id") == user_id:
                        attendee["staff_actor_id"] = None
                        changed = True
            if changed:
                await conf.events.set(events)

    @tasks.loop(minutes=5)
    async def event_loop(self) -> None:
        now = self._now()
        for guild in self.bot.guilds:
            conf = self.config.guild(guild)
            events = await conf.events()
            changed = False
            reminder_minutes = int(await conf.reminder_minutes())
            for event in events.values():
                if event["status"] != "open" or event.get("reminder_sent_at"):
                    continue
                if now >= event["starts_at"] - reminder_minutes * 60:
                    channel = guild.get_channel(event.get("channel_id"))
                    going_ids = [
                        record["user_id"] for record in event.get("attendees", {}).values() if record.get("status") == "going"
                    ]
                    if isinstance(channel, discord.TextChannel):
                        mentions = " ".join(f"<@{user_id}>" for user_id in going_ids[:50])
                        with suppress(discord.HTTPException):
                            await channel.send(
                                (
                                    f"{mentions}\n**{event['title']}** starts "
                                    f"<t:{event['starts_at']}:R>. Check in from the event panel."
                                ).strip()
                            )
                    event["reminder_sent_at"] = now
                    changed = True
            if changed:
                await conf.events.set(events)

    @event_loop.before_loop
    async def before_event_loop(self) -> None:
        await self.bot.wait_until_red_ready()

    async def create_draft_service(
        self,
        guild: discord.Guild,
        *,
        actor_id: int,
        starts_at: int,
        capacity: int,
        title: str,
        description: str = "",
        duration_minutes: int = 60,
        source_type: str = "manual",
        source_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a draft for commands and optional TaakosCogs integrations."""
        if starts_at <= self._now():
            raise commands.BadArgument("The event start timestamp must be in the future.")
        if not 0 <= capacity <= 10000:
            raise commands.BadArgument("Capacity must be between 0 and 10,000.")
        if not 1 <= duration_minutes <= 10080:
            raise commands.BadArgument("Duration must be between 1 and 10,080 minutes.")
        conf = self.config.guild(guild)
        async with self._lock(getattr(guild, "id", 0)):
            event_id = int(await conf.next_event_id())
            now = self._now()
            event = {
                "event_id": event_id,
                "title": title[:200],
                "description": description[:2000],
                "location": "",
                "starts_at": starts_at,
                "duration_minutes": duration_minutes,
                "capacity": capacity,
                "status": "draft",
                "created_by": actor_id,
                "created_at": now,
                "updated_at": now,
                "channel_id": None,
                "message_id": None,
                "reminder_sent_at": None,
                "finalized_at": None,
                "finalized_by": None,
                "reward_role_id": None,
                "scheduled_event_id": None,
                "recurrence_group": None,
                "source_type": source_type[:50],
                "source_id": source_id,
                "attendees": {},
            }
            events = await conf.events()
            events[str(event_id)] = event
            await conf.events.set(events)
            await conf.next_event_id.set(event_id + 1)
        return event

    @commands.hybrid_group(name="eventcheckin", aliases=["checkinevent"], invoke_without_command=True)
    @commands.guild_only()
    async def event_checkin(self, ctx: commands.Context) -> None:
        """Create event panels and manage attendance."""
        await ctx.send_help()

    @event_checkin.command(name="setup")
    @commands.admin_or_permissions(manage_events=True)
    async def setup_command(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
        timezone_name: str | None = None,
    ) -> None:
        """Set the log channel and the timezone that event times are typed in.

        Example: `[p]eventcheckin setup #event-logs Atlantic/Reykjavik`
        Leave the timezone off to keep the current one.
        """
        destination = channel or ctx.channel
        if not isinstance(destination, discord.TextChannel):
            raise commands.BadArgument("Choose a text channel for attendance logs.")
        if timezone_name is not None:
            try:
                ZoneInfo(timezone_name)
            except (ZoneInfoNotFoundError, ValueError) as error:
                raise commands.BadArgument(
                    "Unknown timezone. Use a name like `Atlantic/Reykjavik`, `America/Chicago` or `Europe/London`."
                ) from error
        permissions = destination.permissions_for(ctx.guild.me)
        if not permissions.send_messages or not permissions.embed_links:
            raise commands.BadArgument("I need Send Messages and Embed Links in that channel.")
        conf = self.config.guild(ctx.guild)
        await conf.log_channel_id.set(destination.id)
        if timezone_name is not None:
            await conf.timezone.set(timezone_name)
        current = await conf.timezone()
        await ctx.send(f"EventCheckin logs use {destination.mention}; event times are entered in `{current}`.")

    @event_checkin.command(name="post")
    @commands.admin_or_permissions(manage_events=True)
    async def post(self, ctx: commands.Context, event_id: int, channel: discord.TextChannel | None = None) -> None:
        """Post or move an event registration panel."""
        _events, event = await self._event(ctx.guild, event_id)
        if channel is None and event.get("post_channel_id"):
            channel = ctx.guild.get_channel(event["post_channel_id"])
        destination = channel or ctx.channel
        if not isinstance(destination, discord.TextChannel):
            raise commands.BadArgument("Choose a text channel.")
        roles = [role for role in (ctx.guild.get_role(rid) for rid in event.get("ping_role_ids") or []) if role]
        problem = self._post_problem(ctx.guild, ctx.author, destination, roles)
        if problem:
            raise commands.BadArgument(problem)
        message = await self.publish_event(ctx.guild, event_id, destination)
        await ctx.send(f"Event panel posted: {message.jump_url}")

    @event_checkin.command(name="repeat")
    @commands.admin_or_permissions(manage_events=True)
    async def repeat(
        self, ctx: commands.Context, event_id: int, interval_days: commands.Range[int, 1, 365], count: commands.Range[int, 1, 52]
    ) -> None:
        """Create recurring draft copies of an event."""
        _events, source = await self._event(ctx.guild, event_id)
        conf = self.config.guild(ctx.guild)
        async with self._lock(ctx.guild.id):
            next_id = int(await conf.next_event_id())
            group = source.get("recurrence_group") or f"{ctx.guild.id}:{event_id}:{self._now()}"
            created: list[int] = []
            events = await conf.events()
            source["recurrence_group"] = group
            events[str(event_id)] = source
            for occurrence in range(1, int(count) + 1):
                clone = {
                    **source,
                    "event_id": next_id,
                    "starts_at": int(source["starts_at"]) + int(interval_days) * 86400 * occurrence,
                    "status": "draft",
                    "created_by": ctx.author.id,
                    "created_at": self._now(),
                    "updated_at": self._now(),
                    "channel_id": None,
                    "message_id": None,
                    "scheduled_event_id": None,
                    "reminder_sent_at": None,
                    "finalized_at": None,
                    "finalized_by": None,
                    "attendees": {},
                    "acks": {},
                    "requirements": copy.deepcopy(source.get("requirements") or []),
                    "roles": copy.deepcopy(source.get("roles") or []),
                    "recurrence_group": group,
                }

                events[str(next_id)] = clone
                created.append(next_id)
                next_id += 1
            await conf.events.set(events)
            await conf.next_event_id.set(next_id)
        await ctx.send(f"Created {len(created)} recurring drafts: {', '.join(f'#{item}' for item in created)}.")

    @event_checkin.group(name="template", invoke_without_command=True)
    @commands.admin_or_permissions(manage_events=True)
    async def template(self, ctx: commands.Context) -> None:
        """Manage reusable event templates."""
        templates = await self.config.guild(ctx.guild).templates()
        await ctx.send(", ".join(f"`{name}`" for name in sorted(templates)) or "No event templates are configured.")

    @template.command(name="remove")
    async def template_remove(self, ctx: commands.Context, name: str) -> None:
        """Remove a named event template."""
        async with self.config.guild(ctx.guild).templates() as templates:
            removed = templates.pop(name.casefold(), None)
        await ctx.send("Template removed." if removed else "Template not found.")

    @event_checkin.command(name="calendar")
    async def calendar(self, ctx: commands.Context, status: str = "all") -> None:
        """Export retained events as an iCalendar (.ics) file."""
        allowed = {"all", "draft", "open", "finalized", "cancelled"}
        status = status.casefold()
        if status not in allowed:
            raise commands.BadArgument(f"Status must be one of: {', '.join(sorted(allowed))}.")
        events = (await self.config.guild(ctx.guild).events()).values()
        selected = list(events) if status == "all" else [event for event in events if event.get("status") == status]
        payload = build_calendar(ctx.guild.id, ctx.guild.name, selected, self._now()).encode("utf-8")
        await ctx.send(file=discord.File(io.BytesIO(payload), filename=f"{ctx.guild.id}-events.ics"))

    async def _create_discord_event(
        self, guild: discord.Guild, event_id: int, actor: discord.abc.User
    ) -> discord.ScheduledEvent:
        """Create the native scheduled event for an event; shared by the command and the modal button."""
        events, event = await self._event(guild, event_id)
        if event.get("scheduled_event_id"):
            raise commands.BadArgument("This event already has a stored Discord scheduled-event ID.")
        if not event.get("location"):
            raise commands.BadArgument(
                "Add a location first (use **Add description / edit**); Discord needs one for scheduled events."
            )
        start = datetime.fromtimestamp(int(event["starts_at"]), timezone.utc)
        if start <= discord.utils.utcnow():
            raise commands.BadArgument("That event has already started.")
        end = start + timedelta(minutes=max(1, int(event.get("duration_minutes") or 60)))
        create_options: dict[str, Any] = {}
        if event.get("description"):
            create_options["description"] = event["description"]
        scheduled = await guild.create_scheduled_event(
            name=event["title"][:100],
            start_time=start,
            end_time=end,
            entity_type=discord.EntityType.external,
            privacy_level=discord.PrivacyLevel.guild_only,
            location=event["location"][:100],
            reason=f"EventCheckin event #{event_id} created by {actor}",
            **create_options,
        )
        async with self._lock(guild.id):
            events, event = await self._event(guild, event_id)
            event["scheduled_event_id"] = scheduled.id
            await self._save(guild, events, event)
        return scheduled

    @event_checkin.command(name="new")
    @commands.admin_or_permissions(manage_events=True)
    async def new_event(self, ctx: commands.Context) -> None:
        """Open the Create Event form (slash command) or post a Create Event button."""
        if ctx.interaction is not None:
            await self.open_create_modal(ctx.interaction)
            return
        await self._send_create_panel(ctx, ctx.channel)

    @event_checkin.command(name="panel")
    @commands.admin_or_permissions(manage_events=True)
    async def create_panel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        """Post a permanent Create Event button staff can click to open the form."""
        await self._send_create_panel(ctx, channel or ctx.channel)

    async def _send_create_panel(self, ctx: commands.Context, channel: discord.abc.Messageable) -> None:
        if not isinstance(channel, discord.TextChannel):
            raise commands.BadArgument("Choose a text channel.")
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise commands.BadArgument(f"I need View Channel, Send Messages and Embed Links in {channel.mention}.")
        timezone_name, _zone = await self._guild_zone(ctx.guild)
        embed = discord.Embed(
            title="📅 Create an event",
            description=(
                "Click **Create Event** and fill in the form. The event is saved as a draft: edit it, then post it with **Post to channel** when it's ready. **Drafts** reopens unposted events.\n"
                "Only members with **Manage Events** can create events."
            ),
            color=self.COLOR,
        )
        embed.set_footer(text=f"Times you enter use the {timezone_name} timezone.")
        await channel.send(embed=embed, view=EventCreatePanel(self))
        if channel.id != ctx.channel.id:
            await ctx.send(f"Create Event panel posted in {channel.mention}.")

    @event_checkin.command(name="channel")
    @commands.admin_or_permissions(manage_events=True)
    async def announce_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        """Choose where new events are posted. Leave empty to post where the form was opened."""
        if channel is not None:
            perms = channel.permissions_for(ctx.guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                raise commands.BadArgument(f"I need View Channel, Send Messages and Embed Links in {channel.mention}.")
        await self.config.guild(ctx.guild).announce_channel_id.set(channel.id if channel else None)
        if channel:
            await ctx.send(f"New events will be posted in {channel.mention}.")
        else:
            await ctx.send("New events will be posted in the channel where the form is opened.")

    @event_checkin.command(name="rewardrole")
    @commands.admin_or_permissions(manage_roles=True)
    async def reward_role(self, ctx: commands.Context, event_id: int, role: discord.Role | None = None) -> None:
        """Set a role awarded to checked-in attendees when finalized."""
        if role and (role.managed or role >= ctx.guild.me.top_role):
            raise commands.BadArgument("Choose a role the bot can manage.")
        events, event = await self._event(ctx.guild, event_id)
        event["reward_role_id"] = role.id if role else None
        await self._save(ctx.guild, events, event)
        await ctx.send(f"Reward role set to {role.mention}." if role else "Reward role cleared.")

    @event_checkin.command(name="checkin")
    @commands.admin_or_permissions(manage_events=True)
    async def staff_checkin(self, ctx: commands.Context, event_id: int, member: discord.Member) -> None:
        """Check in an attendee manually."""
        events, event = await self._event(ctx.guild, event_id)
        record = event.setdefault("attendees", {}).setdefault(
            str(member.id),
            {"user_id": member.id, "registered_at": self._now(), "promoted_at": None},
        )
        record.update(status="checked_in", checked_in_at=self._now(), staff_actor_id=ctx.author.id)
        await self._save(ctx.guild, events, event)
        await self._refresh(ctx.guild, event)
        await ctx.send(f"Checked in {member.mention}.")

    @event_checkin.command(name="list")
    async def list_events(self, ctx: commands.Context, status: str = "open") -> None:
        """List events by status, or use all."""
        events = await self.config.guild(ctx.guild).events()
        timezone_name = await self.config.guild(ctx.guild).timezone()
        timezone = ZoneInfo(timezone_name)
        rows = [event for event in events.values() if status == "all" or event.get("status") == status]
        rows.sort(key=lambda event: event["starts_at"])
        lines = [
            f"• **#{event['event_id']} {event['title']}** · {event['status']} · <t:{event['starts_at']}:R> · "
            f"{datetime.fromtimestamp(event['starts_at'], timezone):%Y-%m-%d %H:%M %Z}"
            for event in rows[:25]
        ]
        await ctx.send(
            embed=discord.Embed(title=f"Events · {timezone_name}", description="\n".join(lines) or "No events.", color=self.COLOR)
        )

    @event_checkin.command(name="stats")
    @commands.admin_or_permissions(manage_events=True)
    async def stats(self, ctx: commands.Context, days: commands.Range[int, 1, 3650] = 90) -> None:
        """Show aggregate attendance for events started within the selected period."""
        cutoff = self._now() - int(days) * 86400
        events = [
            item
            for item in (await self.config.guild(ctx.guild).events()).values()
            if int(item.get("starts_at", 0)) >= cutoff and item.get("status") in {"finalized", "completed"}
        ]
        registrations = checked = no_shows = 0
        for item in events:
            counts = self._counts(item)
            registrations += len(item.get("attendees", {}))
            checked += counts["checked_in"]
            no_shows += counts["no_show"]
        rate = checked / registrations * 100 if registrations else 0
        embed = discord.Embed(title=f"Attendance · last {int(days)} days", color=self.COLOR)
        embed.add_field(name="Events", value=str(len(events)))
        embed.add_field(name="Registrations", value=str(registrations))
        embed.add_field(name="Checked in", value=f"{checked} ({rate:.1f}%)")
        embed.add_field(name="No-shows", value=str(no_shows))
        await ctx.send(embed=embed)

    @event_checkin.command(name="export")
    @commands.admin_or_permissions(manage_events=True)
    async def export(self, ctx: commands.Context, event_id: int) -> None:
        """Export event registration and attendance as CSV."""
        _events, event = await self._event(ctx.guild, event_id)
        output = io.StringIO()
        fields = [
            "event_id",
            "event_title",
            "user_id",
            "status",
            "registered_at",
            "promoted_at",
            "checked_in_at",
            "withdrawn_at",
            "staff_actor_id",
            "requirements_confirmed",
            "role",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        total = len(self._requirements(event))
        for record in event.get("attendees", {}).values():
            done = total - len(self._missing_requirements(event, record["user_id"]))
            writer.writerow(
                {
                    "event_id": event_id,
                    "event_title": event["title"],
                    **{key: record.get(key) for key in fields[2:-2]},
                    "requirements_confirmed": f"{done}/{total}" if total else "",
                    "role": (self._role(event, record.get("role_id")) or {}).get("name", ""),
                }
            )
        await ctx.send(file=discord.File(io.BytesIO(output.getvalue().encode()), filename=f"event-{event_id}-attendance.csv"))
