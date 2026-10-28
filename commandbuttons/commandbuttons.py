"""CommandButtons cog - Sticky command button panels with modal support."""
import asyncio
import contextlib
import logging
from typing import Any, Dict, List

import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

UNIQUE_ID = 0xCB5E9003
log = logging.getLogger("red.commandbuttons")


class PanelButtonView(discord.ui.View):
    def __init__(self, cog, panel_id: str, buttons: List[dict]):
        super().__init__(timeout=None)
        self.cog = cog
        self.panel_id = panel_id
        for idx, btn in enumerate(buttons):
            style = {
                "primary": discord.ButtonStyle.primary,
                "secondary": discord.ButtonStyle.secondary,
                "success": discord.ButtonStyle.success,
                "danger": discord.ButtonStyle.danger,
                "blurple": discord.ButtonStyle.primary,
                "grey": discord.ButtonStyle.secondary,
                "gray": discord.ButtonStyle.secondary,
                "green": discord.ButtonStyle.success,
                "red": discord.ButtonStyle.danger,
            }.get(str(btn.get("style", "secondary")).lower(), discord.ButtonStyle.secondary)

            button = discord.ui.Button(
                label=btn.get("label") or btn.get("name") or "Click",
                emoji=btn.get("emoji"),
                style=style,
                custom_id=f"cmdpanel:{panel_id}:{idx}",
                row=btn.get("row"),
            )
            button.callback = self._make_callback(idx)
            self.add_item(button)

    def _make_callback(self, idx):
        async def callback(interaction: discord.Interaction):
            await self.cog.button_clicked(interaction, self.panel_id, idx)
        return callback


class ContextModal(discord.ui.Modal, title="Command Input"):
    def __init__(self, cog, panel_id: str, btn_idx: int, command: str, fields: List[dict], button_label: str = ""):
        super().__init__(timeout=300)
        self.cog = cog
        self.panel_id = panel_id
        self.btn_idx = btn_idx
        self.command = command
        self.fields = fields
        if button_label:
            self.title = button_label[:45]

        for field in fields[:5]:
            label = str(field.get("label", "Value"))[:45]
            placeholder = str(field.get("placeholder", ""))[:100]
            required = field.get("required", True)
            multiline = field.get("multiline", False)
            min_len = field.get("min_length")
            max_len = field.get("max_length", 1024)
            text_input = discord.ui.TextInput(
                label=label,
                placeholder=placeholder,
                required=required,
                style=discord.TextStyle.paragraph if multiline else discord.TextStyle.short,
                min_length=min_len,
                max_length=max_len,
                custom_id=f"field_{len(self.children)}",
            )
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        values = []
        for child in self.children:
            if isinstance(child, discord.ui.TextInput):
                values.append(child.value)
        # Build command with values
        cmd = self.command
        # If command has placeholders like {0}, {1} or %s, replace them
        try:
            if "{" in cmd and "}" in cmd:
                for i, v in enumerate(values):
                    cmd = cmd.replace(f"{{{i}}}", str(v)).replace(f"%{i+1}", str(v))
            else:
                # Append values as arguments
                parts = []
                for v in values:
                    vstr = str(v)
                    if " " in vstr and not (vstr.startswith('"') and vstr.endswith('"')):
                        parts.append(f'"{vstr}"')
                    else:
                        parts.append(vstr)
                if parts:
                    cmd = f"{cmd} {' '.join(parts)}"
        except Exception:
            pass
        await self.cog.invoke_command(interaction, cmd)


class PanelManagerView(discord.ui.View):
    def __init__(self, cog, ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    @discord.ui.button(label="Create Panel", style=discord.ButtonStyle.primary, row=0)
    async def create_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Not for you.", ephemeral=True)
        modal = CreatePanelModal(self.cog)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="List Panels", style=discord.ButtonStyle.secondary, row=0)
    async def list_panels(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Not for you.", ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        if not panels:
            return await interaction.response.send_message("No panels.", ephemeral=True)
        embed = discord.Embed(title="Panels", color=await self.ctx.embed_color())
        for pid, p in panels.items():
            embed.add_field(name=f"{p.get('name')} ({pid})", value=f"Buttons: {len(p.get('buttons',[]))}", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Manage Buttons", style=discord.ButtonStyle.primary, row=1)
    async def manage_buttons(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Not for you.", ephemeral=True)
        modal = SelectPanelForButtonsModal(self.cog, self.ctx)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Post/Sticky", style=discord.ButtonStyle.success, row=1)
    async def manage_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Not for you.", ephemeral=True)
        modal = PostPanelModal(self.cog)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Delete Panel", style=discord.ButtonStyle.danger, row=2)
    async def delete_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Not for you.", ephemeral=True)
        modal = DeletePanelModal(self.cog)
        await interaction.response.send_modal(modal)


class CreatePanelModal(discord.ui.Modal, title="Create Panel"):
    name = discord.ui.TextInput(label="Panel Name", placeholder="e.g. Admin Tools", required=True)
    description = discord.ui.TextInput(label="Description", placeholder="Description shown above panel", required=False, style=discord.TextStyle.paragraph)

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.name.value).lower().replace(" ", "-")
        panels[pid] = {
            "name": str(self.name.value),
            "description": str(self.description.value) if self.description.value else None,
            "buttons": [],
            "message_id": None,
            "channel_id": None,
            "sticky_last_id": None,
        }
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        await interaction.followup.send(f"Created panel `{pid}`. Use Manage Panel to add buttons.", ephemeral=True)


class PostPanelModal(discord.ui.Modal, title="Post/Make Sticky"):
    panel_id = discord.ui.TextInput(label="Panel ID", placeholder="panel-id", required=True)
    channel = discord.ui.TextInput(label="Channel ID or mention", placeholder="123456...", required=True)
    make_sticky = discord.ui.TextInput(label="Make Sticky? (yes/no)", placeholder="yes/no", required=False)

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            return await interaction.followup.send("Panel not found.", ephemeral=True)
        # Resolve channel
        ch_str = str(self.channel.value).strip()
        ch_id = None
        import re
        m = re.findall(r'(\d+)', ch_str)
        if m:
            ch_id = int(m[-1])
        ch = interaction.guild.get_channel(ch_id) if ch_id else interaction.channel
        if not ch:
            return await interaction.followup.send("Channel not found.", ephemeral=True)
        panel = panels[pid]
        view = PanelButtonView(self.cog, pid, panel["buttons"])
        content = panel.get("description") or f"**{panel['name']}**"
        msg = await ch.send(content, view=view)
        panel["message_id"] = msg.id
        panel["channel_id"] = ch.id
        sticky_flag = str(self.make_sticky.value or "").lower() in ("yes", "y", "true")
        if sticky_flag:
            sticky = await self.cog.config.guild(interaction.guild).sticky()
            sticky[str(ch.id)] = {"panel_id": pid, "cooldown": 3}
            await self.cog.config.guild(interaction.guild).sticky.set(sticky)
            panel["sticky_last_id"] = msg.id
        panels[pid] = panel
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        self.cog.bot.add_view(view, message_id=msg.id)
        await interaction.followup.send(f"Posted to {ch.mention}{' and made sticky.' if sticky_flag else '.'}", ephemeral=True)


class ManagePanelModal(discord.ui.Modal, title="Manage Panel Buttons"):
    panel_id = discord.ui.TextInput(label="Panel ID", placeholder="panel-id", required=True)
    action = discord.ui.TextInput(label="Action (add/edit/remove/view)", placeholder="add/edit/remove/view", required=True)
    button_index = discord.ui.TextInput(label="Button Index (for edit/remove)", placeholder="0", required=False)
    button_label = discord.ui.TextInput(label="Button Label", placeholder="My Button", required=False)
    button_command = discord.ui.TextInput(label="Command (with ! prefix or without)", placeholder="role add {0} Member", required=False)
    button_style = discord.ui.TextInput(label="Style", placeholder="primary/secondary/success/danger", required=False)
    need_modal = discord.ui.TextInput(label="Need modal inputs? (yes/no)", placeholder="yes/no", required=False)
    modal_fields = discord.ui.TextInput(label="Modal fields (comma labels)", placeholder="User,Role", required=False, style=discord.TextStyle.paragraph)

    def __init__(self, cog):
        super().__init__(timeout=600)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            return await interaction.followup.send("Panel not found.", ephemeral=True)
        action = str(self.action.value).strip().lower()
        if action == "view":
            panel = panels[pid]
            btns = panel.get("buttons", [])
            if not btns:
                return await interaction.followup.send("No buttons.", ephemeral=True)
            msg_out = "\n".join([f"{i}: {b.get('label')} -> {b.get('command')} ({'modal' if b.get('modal_fields') else 'direct'})" for i,b in enumerate(btns)])
            return await interaction.followup.send(f"```\n{msg_out}\n```", ephemeral=True)
        elif action == "add":
            label = str(self.button_label.value or "Button").strip()
            cmd = str(self.button_command.value or "").strip()
            style = str(self.button_style.value or "secondary").strip().lower()
            need = str(self.need_modal.value or "no").lower() in ("yes", "y", "true")
            fields = []
            if need and self.modal_fields.value:
                for f in str(self.modal_fields.value).split(","):
                    f = f.strip()
                    if f:
                        fields.append({"label": f, "required": True})
            panels[pid]["buttons"].append({"label": label, "command": cmd, "style": style, "modal_fields": fields})
            await self.cog.config.guild(interaction.guild).panels.set(panels)
            # Update message if posted
            panel = panels[pid]
            if panel.get("channel_id") and panel.get("message_id"):
                try:
                    ch = interaction.guild.get_channel(panel["channel_id"])
                    if ch:
                        view = PanelButtonView(self.cog, pid, panels[pid]["buttons"])
                        msg = await ch.fetch_message(panel["message_id"])
                        await msg.edit(view=view)
                        self.cog.bot.add_view(view, message_id=msg.id)
                except Exception:
                    pass
            return await interaction.followup.send("Button added.", ephemeral=True)
        elif action == "remove":
            try:
                idx = int(str(self.button_index.value))
            except Exception:
                return await interaction.followup.send("Invalid index.", ephemeral=True)
            btns = panels[pid]["buttons"]
            if idx < 0 or idx >= len(btns):
                return await interaction.followup.send("Invalid index.", ephemeral=True)
            btns.pop(idx)
            panels[pid]["buttons"] = btns
            await self.cog.config.guild(interaction.guild).panels.set(panels)
            panel = panels[pid]
            if panel.get("channel_id") and panel.get("message_id"):
                try:
                    ch = interaction.guild.get_channel(panel["channel_id"])
                    if ch:
                        if btns:
                            view = PanelButtonView(self.cog, pid, btns)
                            msg = await ch.fetch_message(panel["message_id"])
                            await msg.edit(view=view)
                            self.cog.bot.add_view(view, message_id=msg.id)
                        else:
                            msg = ch.get_partial_message(panel["message_id"])
                            await msg.edit(view=None)
                except Exception:
                    pass
            return await interaction.followup.send("Button removed.", ephemeral=True)
        elif action == "edit":
            try:
                idx = int(str(self.button_index.value))
            except Exception:
                return await interaction.followup.send("Invalid index.", ephemeral=True)
            btns = panels[pid]["buttons"]
            if idx < 0 or idx >= len(btns):
                return await interaction.followup.send("Invalid index.", ephemeral=True)
            btn = btns[idx]
            if self.button_label.value:
                btn["label"] = str(self.button_label.value).strip()
            if self.button_command.value:
                btn["command"] = str(self.button_command.value).strip()
            if self.button_style.value:
                btn["style"] = str(self.button_style.value).strip().lower()
            need = str(self.need_modal.value or "").lower() in ("yes", "y", "true") if self.need_modal.value else None
            if need is True and self.modal_fields.value:
                fields = []
                for f in str(self.modal_fields.value).split(","):
                    f = f.strip()
                    if f:
                        fields.append({"label": f, "required": True})
                btn["modal_fields"] = fields
            elif need is False:
                btn["modal_fields"] = []
            btns[idx] = btn
            panels[pid]["buttons"] = btns
            await self.cog.config.guild(interaction.guild).panels.set(panels)
            panel = panels[pid]
            if panel.get("channel_id") and panel.get("message_id"):
                try:
                    ch = interaction.guild.get_channel(panel["channel_id"])
                    if ch:
                        view = PanelButtonView(self.cog, pid, btns)
                        msg = await ch.fetch_message(panel["message_id"])
                        await msg.edit(view=view)
                        self.cog.bot.add_view(view, message_id=msg.id)
                except Exception:
                    pass
            return await interaction.followup.send("Button updated.", ephemeral=True)
        else:
            return await interaction.followup.send("Unknown action.", ephemeral=True)


class CommandButtons(commands.Cog):
    """Manage sticky command button panels."""

    DEFAULT_COOLDOWN = 3

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=UNIQUE_ID, force_registration=True)
        self.config.register_guild(
            panels={},
            sticky={},
        )
        self.locked = set()
        self._cvs: Dict[int, asyncio.Condition] = {}

    async def cog_load(self):
        self.bot.loop.create_task(self._restore_delayed())

    async def _restore_delayed(self):
        try:
            await self.bot.wait_until_red_ready()
            await self._restore()
        except Exception as e:
            log.error(f"Failed to restore views: {e}", exc_info=e)

    async def _restore(self):
        try:
            all_guilds = await self.config.all_guilds()
            for guild_id, data in all_guilds.items():
                for panel_id, panel in data.get("panels", {}).items():
                    try:
                        view = PanelButtonView(self, panel_id, panel.get("buttons", []))
                        msg_id = panel.get("message_id")
                        if msg_id:
                            self.bot.add_view(view, message_id=msg_id)
                    except Exception as e:
                        log.error(f"Failed to restore {panel_id}: {e}")
        except Exception as e:
            log.error(f"Error in restore: {e}", exc_info=e)

    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    @commands.command(name="cbuttons")
    async def cbuttons_panel(self, ctx: commands.Context):
        """Open button panel manager UI."""
        view = PanelManagerView(self, ctx)
        embed = discord.Embed(
            title="Command Buttons Manager",
            description="Create, manage, post and make panels sticky with modals for command context.",
            color=await ctx.embed_color(),
        )
        await ctx.send(embed=embed, view=view)

    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    @commands.command(name="cbuttonsmanage")
    async def cbuttons_manage(self, ctx: commands.Context):
        """Manage buttons on panels via modal."""
        modal = ManagePanelModal(self)
        try:
            if ctx.interaction:
                await ctx.interaction.response.send_modal(modal)
            else:
                # Can't send modal without interaction, send info
                await ctx.send("Use slash command for modals, or use the `!cbuttons` UI buttons.")
        except Exception:
            await ctx.send("Use `!cbuttons` for UI management.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if isinstance(message.channel, discord.abc.PrivateChannel):
            return
        try:
            sticky = await self.config.guild(message.guild).sticky()
            cfg = sticky.get(str(message.channel.id))
            if not cfg:
                return
            await self._repost(message.guild, message.channel, cfg["panel_id"])
        except Exception as e:
            log.error(f"on_message error: {e}")

    async def _repost(self, guild, channel, panel_id):
        try:
            cv = self._cvs.setdefault(channel.id, asyncio.Condition())
            async with cv:
                await cv.wait_for(lambda: channel.id not in self.locked)
            sticky = await self.config.guild(guild).sticky()
            cfg = sticky.get(str(channel.id))
            if not cfg or cfg.get("panel_id") != panel_id:
                return
            cooldown = cfg.get("cooldown", self.DEFAULT_COOLDOWN)
            await asyncio.sleep(cooldown)
            async with cv:
                await cv.wait_for(lambda: channel.id not in self.locked)
                sticky2 = await self.config.guild(guild).sticky()
                if str(channel.id) not in sticky2 or sticky2[str(channel.id)].get("panel_id") != panel_id:
                    return
                panels = await self.config.guild(guild).panels()
                if panel_id not in panels:
                    return
                panel = panels[panel_id]
                old_id = panel.get("sticky_last_id")
                if old_id:
                    try:
                        old = channel.get_partial_message(old_id)
                        await old.delete()
                    except Exception:
                        pass
                view = PanelButtonView(self, panel_id, panel["buttons"])
                content = panel.get("description") or f"**{panel['name']}**"
                msg = await channel.send(content, view=view)
                panel["sticky_last_id"] = msg.id
                panel["message_id"] = msg.id
                panel["channel_id"] = channel.id
                panels[panel_id] = panel
                await self.config.guild(guild).panels.set(panels)
                self.bot.add_view(view, message_id=msg.id)
        except Exception as e:
            log.error(f"_repost error: {e}")

    async def button_clicked(self, interaction: discord.Interaction, panel_id: str, btn_idx: int):
        try:
            panels = await self.config.guild(interaction.guild).panels()
            if panel_id not in panels:
                return await interaction.response.send_message("Panel missing.", ephemeral=True)
            btns = panels[panel_id].get("buttons", [])
            if btn_idx >= len(btns):
                return await interaction.response.send_message("Button missing.", ephemeral=True)
            btn = btns[btn_idx]
            command = btn.get("command", "")
            fields = btn.get("modal_fields", [])
            if fields:
                modal = ContextModal(self, panel_id, btn_idx, command, fields, btn.get("label", ""))
                return await interaction.response.send_modal(modal)
            await interaction.response.defer(ephemeral=True)
            await self.invoke_command(interaction, command)
        except Exception as e:
            log.error(f"button_clicked error: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Error.", ephemeral=True)
                else:
                    await interaction.followup.send("Error.", ephemeral=True)
            except Exception:
                pass

    async def invoke_command(self, interaction: discord.Interaction, command: str):
        try:
            msg = interaction.message
            if not msg:
                return await interaction.followup.send("Cannot invoke command.", ephemeral=True)
            prefix = "!"
            try:
                cp = getattr(self.bot, "command_prefix", "!")
                if callable(cp):
                    try:
                        cp = cp(self.bot, msg)
                    except Exception:
                        cp = "!"
                if isinstance(cp, (list, tuple, set)):
                    for p in cp:
                        if isinstance(p, str) and p:
                            prefix = p
                            break
                elif isinstance(cp, str):
                    prefix = cp
            except Exception:
                prefix = "!"
            cmd_clean = command.strip()
            # Strip any existing prefix
            for p in [prefix]:
                if isinstance(p, str) and len(p) > 0 and cmd_clean.startswith(p):
                    cmd_clean = cmd_clean[len(p):].strip()
                    break
            # Also check common prefixes
            if cmd_clean and cmd_clean[0] in "!.?":
                # remove first char if it's a prefix char
                pass  # be careful
            ctx = await self.bot.get_context(msg)
            ctx.message.content = f"{prefix}{cmd_clean}"
            ctx.author = interaction.user
            ctx.channel = interaction.channel
            ctx.guild = interaction.guild
            ctx2 = await self.bot.get_context(ctx.message)
            ctx2.author = interaction.user
            ctx2.channel = interaction.channel
            ctx2.guild = interaction.guild
            if ctx2.valid and ctx2.command:
                await self.bot.invoke(ctx2)
                return
            await self.bot.process_commands(ctx2.message)
            return
        except Exception as e:
            try:
                await interaction.followup.send(f"Error: {e}", ephemeral=True)
            except Exception:
                pass


class PanelButtonManagerView(discord.ui.View):
    def __init__(self, cog, ctx, panel_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.panel_id = panel_id

    @discord.ui.button(label="Add Button", style=discord.ButtonStyle.success, row=0)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Not for you.", ephemeral=True)
        modal = AddButtonModal(self.cog, self.panel_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Remove Button", style=discord.ButtonStyle.danger, row=0)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Not for you.", ephemeral=True)
        modal = RemoveButtonModal(self.cog, self.panel_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Edit Button", style=discord.ButtonStyle.primary, row=0)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Not for you.", ephemeral=True)
        modal = EditButtonModal(self.cog, self.panel_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="View Buttons", style=discord.ButtonStyle.secondary, row=1)
    async def view_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Not for you.", ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        if self.panel_id not in panels:
            return await interaction.response.send_message("Panel not found.", ephemeral=True)
        btns = panels[self.panel_id].get("buttons", [])
        if not btns:
            return await interaction.response.send_message("No buttons.", ephemeral=True)
        msg = "\n".join([f"{i}: {b.get('label')} | {b.get('command')} | modal:{len(b.get('modal_fields',[]))}" for i,b in enumerate(btns)])
        await interaction.response.send_message(f"```\n{msg}\n```", ephemeral=True)


class AddButtonModal(discord.ui.Modal, title="Add Button"):
    button_label = discord.ui.TextInput(label="Button Label", placeholder="My Button", required=True)
    button_command = discord.ui.TextInput(label="Command (e.g. !kick or kick)", placeholder="say Hello", required=True)
    button_style = discord.ui.TextInput(label="Style", placeholder="primary/secondary/success/danger", required=False)
    need_modal = discord.ui.TextInput(label="Need modal for context? (yes/no)", placeholder="no", required=False)
    modal_fields = discord.ui.TextInput(label="Modal field labels (comma separated)", placeholder="User, Reason", required=False, style=discord.TextStyle.paragraph)

    def __init__(self, cog, panel_id: str):
        super().__init__(timeout=600)
        self.cog = cog
        self.panel_id = panel_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        if self.panel_id not in panels:
            return await interaction.followup.send("Panel not found.", ephemeral=True)
        label = str(self.button_label.value).strip()
        cmd = str(self.button_command.value).strip()
        style = str(self.button_style.value or "secondary").strip().lower()
        need = str(self.need_modal.value or "no").lower() in ("yes", "y", "true")
        fields = []
        if need and self.modal_fields.value:
            for f in str(self.modal_fields.value).split(","):
                f = f.strip()
                if f:
                    fields.append({"label": f, "required": True})
        panels[self.panel_id]["buttons"].append({"label": label, "command": cmd, "style": style, "modal_fields": fields})
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        panel = panels[self.panel_id]
        if panel.get("channel_id") and panel.get("message_id"):
            try:
                ch = interaction.guild.get_channel(panel["channel_id"])
                if ch:
                    view = PanelButtonView(self.cog, self.panel_id, panels[self.panel_id]["buttons"])
                    msg = await ch.fetch_message(panel["message_id"])
                    await msg.edit(view=view)
                    self.cog.bot.add_view(view, message_id=msg.id)
            except Exception:
                pass
        await interaction.followup.send("Button added.", ephemeral=True)


class RemoveButtonModal(discord.ui.Modal, title="Remove Button"):
    button_index = discord.ui.TextInput(label="Button Index", placeholder="0", required=True)

    def __init__(self, cog, panel_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.panel_id = panel_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        if self.panel_id not in panels:
            return await interaction.followup.send("Panel not found.", ephemeral=True)
        try:
            idx = int(str(self.button_index.value).strip())
        except Exception:
            return await interaction.followup.send("Invalid index.", ephemeral=True)
        btns = panels[self.panel_id]["buttons"]
        if idx < 0 or idx >= len(btns):
            return await interaction.followup.send("Invalid index.", ephemeral=True)
        btns.pop(idx)
        panels[self.panel_id]["buttons"] = btns
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        panel = panels[self.panel_id]
        if panel.get("channel_id") and panel.get("message_id"):
            try:
                ch = interaction.guild.get_channel(panel["channel_id"])
                if ch:
                    if btns:
                        view = PanelButtonView(self.cog, self.panel_id, btns)
                        msg = await ch.fetch_message(panel["message_id"])
                        await msg.edit(view=view)
                        self.cog.bot.add_view(view, message_id=msg.id)
                    else:
                        msg = ch.get_partial_message(panel["message_id"])
                        await msg.edit(view=None)
            except Exception:
                pass
        await interaction.followup.send("Button removed.", ephemeral=True)


class EditButtonModal(discord.ui.Modal, title="Edit Button"):
    button_index = discord.ui.TextInput(label="Button Index", placeholder="0", required=True)
    button_label = discord.ui.TextInput(label="New Label (leave blank to keep)", placeholder="", required=False)
    button_command = discord.ui.TextInput(label="New Command (leave blank to keep)", placeholder="", required=False)
    button_style = discord.ui.TextInput(label="New Style (leave blank to keep)", placeholder="primary/secondary/success/danger", required=False)
    need_modal = discord.ui.TextInput(label="Need modal? (yes/no/leave blank)", placeholder="", required=False)
    modal_fields = discord.ui.TextInput(label="Modal field labels comma sep (if modal)", placeholder="", required=False, style=discord.TextStyle.paragraph)

    def __init__(self, cog, panel_id: str):
        super().__init__(timeout=600)
        self.cog = cog
        self.panel_id = panel_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        if self.panel_id not in panels:
            return await interaction.followup.send("Panel not found.", ephemeral=True)
        try:
            idx = int(str(self.button_index.value).strip())
        except Exception:
            return await interaction.followup.send("Invalid index.", ephemeral=True)
        btns = panels[self.panel_id]["buttons"]
        if idx < 0 or idx >= len(btns):
            return await interaction.followup.send("Invalid index.", ephemeral=True)
        btn = btns[idx]
        if self.button_label.value and str(self.button_label.value).strip():
            btn["label"] = str(self.button_label.value).strip()
        if self.button_command.value and str(self.button_command.value).strip():
            btn["command"] = str(self.button_command.value).strip()
        if self.button_style.value and str(self.button_style.value).strip():
            btn["style"] = str(self.button_style.value).strip().lower()
        if self.need_modal.value and str(self.need_modal.value).strip():
            need = str(self.need_modal.value).strip().lower() in ("yes", "y", "true")
            if need and self.modal_fields.value and str(self.modal_fields.value).strip():
                fields = []
                for f in str(self.modal_fields.value).split(","):
                    f = f.strip()
                    if f:
                        fields.append({"label": f, "required": True})
                btn["modal_fields"] = fields
            elif not need:
                btn["modal_fields"] = []
        btns[idx] = btn
        panels[self.panel_id]["buttons"] = btns
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        panel = panels[self.panel_id]
        if panel.get("channel_id") and panel.get("message_id"):
            try:
                ch = interaction.guild.get_channel(panel["channel_id"])
                if ch:
                    view = PanelButtonView(self.cog, self.panel_id, btns)
                    msg = await ch.fetch_message(panel["message_id"])
                    await msg.edit(view=view)
                    self.cog.bot.add_view(view, message_id=msg.id)
            except Exception:
                pass
        await interaction.followup.send("Button updated.", ephemeral=True)


class SelectPanelForButtonsModal(discord.ui.Modal, title="Select Panel"):
    panel_id = discord.ui.TextInput(label="Panel ID", placeholder="panel-id", required=True)

    def __init__(self, cog, ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            return await interaction.response.send_message("Panel not found.", ephemeral=True)
        view = PanelButtonManagerView(self.cog, self.ctx, pid)
        embed = discord.Embed(title=f"Manage Buttons - {panels[pid]['name']} ({pid})", description="Add, remove, edit, or view buttons", color=0x2F3136)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class DeletePanelModal(discord.ui.Modal, title="Delete Panel"):
    panel_id = discord.ui.TextInput(label="Panel ID", placeholder="panel-id", required=True)

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            return await interaction.followup.send("Panel not found.", ephemeral=True)
        panel = panels[pid]
        sticky = await self.cog.config.guild(interaction.guild).sticky()
        for ch_id, cfg in list(sticky.items()):
            if cfg.get("panel_id") == pid:
                sticky.pop(ch_id)
        await self.cog.config.guild(interaction.guild).sticky.set(sticky)
        if panel.get("channel_id") and panel.get("message_id"):
            try:
                ch = interaction.guild.get_channel(panel["channel_id"])
                if ch:
                    msg = ch.get_partial_message(panel["message_id"])
                    await msg.delete()
            except Exception:
                pass
        del panels[pid]
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        await interaction.followup.send(f"Deleted panel `{pid}`.", ephemeral=True)
