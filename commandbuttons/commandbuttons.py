"""CommandButtons cog - Sticky command button panels with modal support."""
import asyncio
import logging
from typing import Any, Dict, List

import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

UNIQUE_ID = 0xCB5E9006
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
                label=btn.get("label") or "Click",
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
            try:
                self.title = button_label[:45]
            except Exception:
                pass
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
            )
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        values = []
        for child in self.children:
            if isinstance(child, discord.ui.TextInput):
                values.append(str(child.value))
        cmd = self.command
        # Try to substitute {0}, {1} or append
        try:
            if "{" in cmd:
                for i, v in enumerate(values):
                    cmd = cmd.replace("{" + str(i) + "}", v)
            else:
                # append quoted if spaces
                for v in values:
                    if " " in v and not (v.startswith('"') and v.endswith('"')):
                        cmd += f' "{v}"'
                    else:
                        cmd += f' {v}'
        except Exception:
            pass
        await self.cog.invoke_command(interaction, cmd)


class CommandButtons(commands.Cog):
    """Manage sticky command button panels."""

    DEFAULT_COOLDOWN = 3

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=UNIQUE_ID, force_registration=True)
        self.config.register_guild(panels={}, sticky={})
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
            description="Create, manage, post and make panels sticky. Buttons can open modals for extra context.",
            color=await ctx.embed_color(),
        )
        await ctx.send(embed=embed, view=view)





class PanelManagerView(discord.ui.View):
    def __init__(self, cog, ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    @discord.ui.button(label="Create Panel", style=discord.ButtonStyle.primary, row=0)
    async def create_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("Not for you.", ephemeral=True)
            except Exception:
                return
        try:
            modal = CreatePanelModal(self.cog)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass

    @discord.ui.button(label="List Panels", style=discord.ButtonStyle.secondary, row=0)
    async def list_panels(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("Not for you.", ephemeral=True)
            except Exception:
                return
        try:
            panels = await self.cog.config.guild(interaction.guild).panels()
            if not panels:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message("No panels.", ephemeral=True)
                    return
                except Exception:
                    return
            embed = discord.Embed(title="Panels", color=0x2F3136)
            for pid, p in panels.items():
                embed.add_field(name=f"{p.get('name')} ({pid})", value=f"Buttons: {len(p.get('buttons',[]))}", inline=False)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception:
                pass
        except Exception:
            pass

    @discord.ui.button(label="Manage Buttons", style=discord.ButtonStyle.primary, row=1)
    async def manage_buttons(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("Not for you.", ephemeral=True)
            except Exception:
                return
        try:
            modal = SelectPanelForButtonsModal(self.cog, self.ctx)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass

    @discord.ui.button(label="Post/Sticky", style=discord.ButtonStyle.success, row=1)
    async def manage_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("Not for you.", ephemeral=True)
            except Exception:
                return
        try:
            modal = PostPanelModal(self.cog)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass

    @discord.ui.button(label="Delete Panel", style=discord.ButtonStyle.danger, row=2)
    async def delete_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("Not for you.", ephemeral=True)
            except Exception:
                return
        try:
            modal = DeletePanelModal(self.cog)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass


class CreatePanelModal(discord.ui.Modal, title="Create Panel"):
    name = discord.ui.TextInput(label="Panel Name", placeholder="e.g. Tools", required=True)
    description = discord.ui.TextInput(label="Description", placeholder="Description", required=False, style=discord.TextStyle.paragraph)

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.name.value).lower().replace(" ", "-")
        panels[pid] = {"name": str(self.name.value), "description": str(self.description.value) if self.description.value else None, "buttons": [], "message_id": None, "channel_id": None, "sticky_last_id": None}
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        try:
            await interaction.followup.send(f"Created `{pid}`", ephemeral=True)
        except Exception:
            pass


class PostPanelModal(discord.ui.Modal, title="Post/Sticky"):
    panel_id = discord.ui.TextInput(label="Panel ID", required=True)
    channel = discord.ui.TextInput(label="Channel ID", required=True)
    make_sticky = discord.ui.TextInput(label="Sticky? (yes/no)", required=False)

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            try:
                return await interaction.followup.send("Not found", ephemeral=True)
            except Exception:
                return
        import re
        m = re.findall(r'(\d+)', str(self.channel.value))
        ch = interaction.guild.get_channel(int(m[-1])) if m else interaction.channel
        if not ch:
            try:
                return await interaction.followup.send("Bad channel", ephemeral=True)
            except Exception:
                return
        panel = panels[pid]
        view = PanelButtonView(self.cog, pid, panel["buttons"])
        content = panel.get("description") or f"**{panel['name']}**"
        try:
            msg = await ch.send(content, view=view)
        except Exception as e:
            try:
                return await interaction.followup.send(str(e), ephemeral=True)
            except Exception:
                return
        panel["message_id"] = msg.id
        panel["channel_id"] = ch.id
        if str(self.make_sticky.value or "").lower() in ("yes", "y"):
            sticky = await self.cog.config.guild(interaction.guild).sticky()
            sticky[str(ch.id)] = {"panel_id": pid, "cooldown": 3}
            await self.cog.config.guild(interaction.guild).sticky.set(sticky)
            panel["sticky_last_id"] = msg.id
        panels[pid] = panel
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        try:
            self.cog.bot.add_view(view, message_id=msg.id)
        except Exception:
            pass
        try:
            await interaction.followup.send("Posted", ephemeral=True)
        except Exception:
            pass


class SelectPanelForButtonsModal(discord.ui.Modal, title="Select Panel"):
    panel_id = discord.ui.TextInput(label="Panel ID", required=True)

    def __init__(self, cog, ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            try:
                return await interaction.response.send_message("Not found", ephemeral=True)
            except Exception:
                return
        view = PanelButtonManagerView(self.cog, self.ctx, pid)
        embed = discord.Embed(title=f"Manage {pid}", color=0x2F3136)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception:
            pass


class PanelButtonManagerView(discord.ui.View):
    def __init__(self, cog, ctx, panel_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.panel_id = panel_id

    @discord.ui.button(label="Add Button", style=discord.ButtonStyle.success)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("No", ephemeral=True)
            except Exception:
                return
        try:
            modal = AddButtonModal(self.cog, self.panel_id)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass

    @discord.ui.button(label="Remove Button", style=discord.ButtonStyle.danger)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("No", ephemeral=True)
            except Exception:
                return
        try:
            modal = RemoveButtonModal(self.cog, self.panel_id)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass

    @discord.ui.button(label="Edit Button", style=discord.ButtonStyle.primary)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("No", ephemeral=True)
            except Exception:
                return
        try:
            modal = EditButtonModal(self.cog, self.panel_id)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass

    @discord.ui.button(label="View", style=discord.ButtonStyle.secondary)
    async def view_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("No", ephemeral=True)
            except Exception:
                return
        try:
            panels = await self.cog.config.guild(interaction.guild).panels()
            btns = panels[self.panel_id].get("buttons", [])
            if not btns:
                try:
                    if not interaction.response.is_done():
                        return await interaction.response.send_message("Empty", ephemeral=True)
                except Exception:
                    return
            msg = "\n".join([f"{i}: {b.get('label')} -> {b.get('command')} (modal:{len(b.get('modal_fields',[]))})" for i,b in enumerate(btns)])
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"```\n{msg}\n```", ephemeral=True)
                else:
                    await interaction.followup.send(f"```\n{msg}\n```", ephemeral=True)
            except Exception:
                pass
        except Exception:
            pass


class AddButtonModal(discord.ui.Modal, title="Add Button"):
    button_label = discord.ui.TextInput(label="Label", required=True)
    button_command = discord.ui.TextInput(label="Command (e.g. kick or !kick)", required=True)
    button_style = discord.ui.TextInput(label="Style", required=False)
    need_modal = discord.ui.TextInput(label="Modal? (yes/no)", required=False)
    modal_fields = discord.ui.TextInput(label="Fields comma", required=False, style=discord.TextStyle.paragraph)

    def __init__(self, cog, panel_id: str):
        super().__init__(timeout=600)
        self.cog = cog
        self.panel_id = panel_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        panels = await self.cog.config.guild(interaction.guild).panels()
        if self.panel_id not in panels:
            try:
                return await interaction.followup.send("No panel", ephemeral=True)
            except Exception:
                return
        label = str(self.button_label.value).strip()
        cmd = str(self.button_command.value).strip()
        style = str(self.button_style.value or "secondary").strip().lower()
        need = str(self.need_modal.value or "no").lower() in ("yes", "y")
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
                    try:
                        msg = await ch.fetch_message(panel["message_id"])
                        await msg.edit(view=view)
                    except Exception:
                        pass
                    try:
                        self.cog.bot.add_view(view, message_id=panel["message_id"])
                    except Exception:
                        pass
            except Exception:
                pass
        try:
            await interaction.followup.send("Added", ephemeral=True)
        except Exception:
            pass


class RemoveButtonModal(discord.ui.Modal, title="Remove"):
    button_index = discord.ui.TextInput(label="Index", required=True)

    def __init__(self, cog, panel_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.panel_id = panel_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        panels = await self.cog.config.guild(interaction.guild).panels()
        try:
            idx = int(str(self.button_index.value))
        except Exception:
            try:
                return await interaction.followup.send("Bad index", ephemeral=True)
            except Exception:
                return
        btns = panels[self.panel_id]["buttons"]
        if idx < 0 or idx >= len(btns):
            try:
                return await interaction.followup.send("Bad index", ephemeral=True)
            except Exception:
                return
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
                        try:
                            msg = await ch.fetch_message(panel["message_id"])
                            await msg.edit(view=view)
                            self.cog.bot.add_view(view, message_id=panel["message_id"])
                        except Exception:
                            pass
                    else:
                        try:
                            msg = ch.get_partial_message(panel["message_id"])
                            await msg.edit(view=None)
                        except Exception:
                            pass
            except Exception:
                pass
        try:
            await interaction.followup.send("Removed", ephemeral=True)
        except Exception:
            pass


class EditButtonModal(discord.ui.Modal, title="Edit"):
    button_index = discord.ui.TextInput(label="Index", required=True)
    button_label = discord.ui.TextInput(label="Label", required=False)
    button_command = discord.ui.TextInput(label="Command", required=False)
    button_style = discord.ui.TextInput(label="Style", required=False)
    need_modal = discord.ui.TextInput(label="Modal yes/no", required=False)
    modal_fields = discord.ui.TextInput(label="Fields", required=False, style=discord.TextStyle.paragraph)

    def __init__(self, cog, panel_id: str):
        super().__init__(timeout=600)
        self.cog = cog
        self.panel_id = panel_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        panels = await self.cog.config.guild(interaction.guild).panels()
        try:
            idx = int(str(self.button_index.value))
        except Exception:
            try:
                return await interaction.followup.send("Bad", ephemeral=True)
            except Exception:
                return
        btns = panels[self.panel_id]["buttons"]
        if idx < 0 or idx >= len(btns):
            try:
                return await interaction.followup.send("Bad", ephemeral=True)
            except Exception:
                return
        btn = btns[idx]
        if self.button_label.value and str(self.button_label.value).strip():
            btn["label"] = str(self.button_label.value).strip()
        if self.button_command.value and str(self.button_command.value).strip():
            btn["command"] = str(self.button_command.value).strip()
        if self.button_style.value and str(self.button_style.value).strip():
            btn["style"] = str(self.button_style.value).strip().lower()
        if self.need_modal.value and str(self.need_modal.value).strip():
            need = str(self.need_modal.value).strip().lower() in ("yes", "y")
            if need and self.modal_fields.value and str(self.modal_fields.value).strip():
                fields = []
                for f in str(self.modal_fields.value).split(","):
                    f = f.strip()
                    if f: fields.append({"label": f, "required": True})
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
                    try:
                        msg = await ch.fetch_message(panel["message_id"])
                        await msg.edit(view=view)
                        self.cog.bot.add_view(view, message_id=panel["message_id"])
                    except Exception:
                        pass
            except Exception:
                pass
        try:
            await interaction.followup.send("Updated", ephemeral=True)
        except Exception:
            pass


class DeletePanelModal(discord.ui.Modal, title="Delete"):
    panel_id = discord.ui.TextInput(label="Panel ID", required=True)

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            try:
                return await interaction.followup.send("No", ephemeral=True)
            except Exception:
                return
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
                    try:
                        msg = ch.get_partial_message(panel["message_id"])
                        await msg.delete()
                    except Exception:
                        pass
            except Exception:
                pass
        del panels[pid]
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        try:
            await interaction.followup.send("Deleted", ephemeral=True)
        except Exception:
            pass



class PanelManagerView(discord.ui.View):
    def __init__(self, cog, ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    @discord.ui.button(label="Create Panel", style=discord.ButtonStyle.primary, row=0)
    async def create_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("Not for you.", ephemeral=True)
            except Exception:
                return
        try:
            modal = CreatePanelModal(self.cog)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass

    @discord.ui.button(label="List Panels", style=discord.ButtonStyle.secondary, row=0)
    async def list_panels(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("Not for you.", ephemeral=True)
            except Exception:
                return
        try:
            panels = await self.cog.config.guild(interaction.guild).panels()
            if not panels:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message("No panels.", ephemeral=True)
                    return
                except Exception:
                    return
            embed = discord.Embed(title="Panels", color=0x2F3136)
            for pid, p in panels.items():
                embed.add_field(name=f"{p.get('name')} ({pid})", value=f"Buttons: {len(p.get('buttons',[]))}", inline=False)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception:
                pass
        except Exception:
            pass

    @discord.ui.button(label="Manage Buttons", style=discord.ButtonStyle.primary, row=1)
    async def manage_buttons(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("Not for you.", ephemeral=True)
            except Exception:
                return
        try:
            modal = SelectPanelForButtonsModal(self.cog, self.ctx)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass

    @discord.ui.button(label="Post/Sticky", style=discord.ButtonStyle.success, row=1)
    async def manage_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("Not for you.", ephemeral=True)
            except Exception:
                return
        try:
            modal = PostPanelModal(self.cog)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass

    @discord.ui.button(label="Delete Panel", style=discord.ButtonStyle.danger, row=2)
    async def delete_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("Not for you.", ephemeral=True)
            except Exception:
                return
        try:
            modal = DeletePanelModal(self.cog)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass


class CreatePanelModal(discord.ui.Modal, title="Create Panel"):
    name = discord.ui.TextInput(label="Panel Name", placeholder="e.g. Tools", required=True)
    description = discord.ui.TextInput(label="Description", placeholder="Description", required=False, style=discord.TextStyle.paragraph)

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.name.value).lower().replace(" ", "-")
        panels[pid] = {"name": str(self.name.value), "description": str(self.description.value) if self.description.value else None, "buttons": [], "message_id": None, "channel_id": None, "sticky_last_id": None}
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        try:
            await interaction.followup.send(f"Created `{pid}`", ephemeral=True)
        except Exception:
            pass


class PostPanelModal(discord.ui.Modal, title="Post/Sticky"):
    panel_id = discord.ui.TextInput(label="Panel ID", required=True)
    channel = discord.ui.TextInput(label="Channel ID", required=True)
    make_sticky = discord.ui.TextInput(label="Sticky? (yes/no)", required=False)

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            try:
                return await interaction.followup.send("Not found", ephemeral=True)
            except Exception:
                return
        import re
        m = re.findall(r'(\d+)', str(self.channel.value))
        ch = interaction.guild.get_channel(int(m[-1])) if m else interaction.channel
        if not ch:
            try:
                return await interaction.followup.send("Bad channel", ephemeral=True)
            except Exception:
                return
        panel = panels[pid]
        view = PanelButtonView(self.cog, pid, panel["buttons"])
        content = panel.get("description") or f"**{panel['name']}**"
        try:
            msg = await ch.send(content, view=view)
        except Exception as e:
            try:
                return await interaction.followup.send(str(e), ephemeral=True)
            except Exception:
                return
        panel["message_id"] = msg.id
        panel["channel_id"] = ch.id
        if str(self.make_sticky.value or "").lower() in ("yes", "y"):
            sticky = await self.cog.config.guild(interaction.guild).sticky()
            sticky[str(ch.id)] = {"panel_id": pid, "cooldown": 3}
            await self.cog.config.guild(interaction.guild).sticky.set(sticky)
            panel["sticky_last_id"] = msg.id
        panels[pid] = panel
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        try:
            self.cog.bot.add_view(view, message_id=msg.id)
        except Exception:
            pass
        try:
            await interaction.followup.send("Posted", ephemeral=True)
        except Exception:
            pass


class SelectPanelForButtonsModal(discord.ui.Modal, title="Select Panel"):
    panel_id = discord.ui.TextInput(label="Panel ID", required=True)

    def __init__(self, cog, ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            try:
                return await interaction.response.send_message("Not found", ephemeral=True)
            except Exception:
                return
        view = PanelButtonManagerView(self.cog, self.ctx, pid)
        embed = discord.Embed(title=f"Manage {pid}", color=0x2F3136)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception:
            pass


class PanelButtonManagerView(discord.ui.View):
    def __init__(self, cog, ctx, panel_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.panel_id = panel_id

    @discord.ui.button(label="Add Button", style=discord.ButtonStyle.success)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("No", ephemeral=True)
            except Exception:
                return
        try:
            modal = AddButtonModal(self.cog, self.panel_id)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass

    @discord.ui.button(label="Remove Button", style=discord.ButtonStyle.danger)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("No", ephemeral=True)
            except Exception:
                return
        try:
            modal = RemoveButtonModal(self.cog, self.panel_id)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass

    @discord.ui.button(label="Edit Button", style=discord.ButtonStyle.primary)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("No", ephemeral=True)
            except Exception:
                return
        try:
            modal = EditButtonModal(self.cog, self.panel_id)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass

    @discord.ui.button(label="View", style=discord.ButtonStyle.secondary)
    async def view_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("No", ephemeral=True)
            except Exception:
                return
        try:
            panels = await self.cog.config.guild(interaction.guild).panels()
            btns = panels[self.panel_id].get("buttons", [])
            if not btns:
                try:
                    if not interaction.response.is_done():
                        return await interaction.response.send_message("Empty", ephemeral=True)
                except Exception:
                    return
            msg = "\n".join([f"{i}: {b.get('label')} -> {b.get('command')} (modal:{len(b.get('modal_fields',[]))})" for i,b in enumerate(btns)])
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"```\n{msg}\n```", ephemeral=True)
                else:
                    await interaction.followup.send(f"```\n{msg}\n```", ephemeral=True)
            except Exception:
                pass
        except Exception:
            pass


class AddButtonModal(discord.ui.Modal, title="Add Button"):
    button_label = discord.ui.TextInput(label="Label", required=True)
    button_command = discord.ui.TextInput(label="Command (e.g. kick or !kick)", required=True)
    button_style = discord.ui.TextInput(label="Style", required=False)
    need_modal = discord.ui.TextInput(label="Modal? (yes/no)", required=False)
    modal_fields = discord.ui.TextInput(label="Fields comma", required=False, style=discord.TextStyle.paragraph)

    def __init__(self, cog, panel_id: str):
        super().__init__(timeout=600)
        self.cog = cog
        self.panel_id = panel_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        panels = await self.cog.config.guild(interaction.guild).panels()
        if self.panel_id not in panels:
            try:
                return await interaction.followup.send("No panel", ephemeral=True)
            except Exception:
                return
        label = str(self.button_label.value).strip()
        cmd = str(self.button_command.value).strip()
        style = str(self.button_style.value or "secondary").strip().lower()
        need = str(self.need_modal.value or "no").lower() in ("yes", "y")
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
                    try:
                        msg = await ch.fetch_message(panel["message_id"])
                        await msg.edit(view=view)
                    except Exception:
                        pass
                    try:
                        self.cog.bot.add_view(view, message_id=panel["message_id"])
                    except Exception:
                        pass
            except Exception:
                pass
        try:
            await interaction.followup.send("Added", ephemeral=True)
        except Exception:
            pass


class RemoveButtonModal(discord.ui.Modal, title="Remove"):
    button_index = discord.ui.TextInput(label="Index", required=True)

    def __init__(self, cog, panel_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.panel_id = panel_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        panels = await self.cog.config.guild(interaction.guild).panels()
        try:
            idx = int(str(self.button_index.value))
        except Exception:
            try:
                return await interaction.followup.send("Bad index", ephemeral=True)
            except Exception:
                return
        btns = panels[self.panel_id]["buttons"]
        if idx < 0 or idx >= len(btns):
            try:
                return await interaction.followup.send("Bad index", ephemeral=True)
            except Exception:
                return
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
                        try:
                            msg = await ch.fetch_message(panel["message_id"])
                            await msg.edit(view=view)
                            self.cog.bot.add_view(view, message_id=panel["message_id"])
                        except Exception:
                            pass
                    else:
                        try:
                            msg = ch.get_partial_message(panel["message_id"])
                            await msg.edit(view=None)
                        except Exception:
                            pass
            except Exception:
                pass
        try:
            await interaction.followup.send("Removed", ephemeral=True)
        except Exception:
            pass


class EditButtonModal(discord.ui.Modal, title="Edit"):
    button_index = discord.ui.TextInput(label="Index", required=True)
    button_label = discord.ui.TextInput(label="Label", required=False)
    button_command = discord.ui.TextInput(label="Command", required=False)
    button_style = discord.ui.TextInput(label="Style", required=False)
    need_modal = discord.ui.TextInput(label="Modal yes/no", required=False)
    modal_fields = discord.ui.TextInput(label="Fields", required=False, style=discord.TextStyle.paragraph)

    def __init__(self, cog, panel_id: str):
        super().__init__(timeout=600)
        self.cog = cog
        self.panel_id = panel_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        panels = await self.cog.config.guild(interaction.guild).panels()
        try:
            idx = int(str(self.button_index.value))
        except Exception:
            try:
                return await interaction.followup.send("Bad", ephemeral=True)
            except Exception:
                return
        btns = panels[self.panel_id]["buttons"]
        if idx < 0 or idx >= len(btns):
            try:
                return await interaction.followup.send("Bad", ephemeral=True)
            except Exception:
                return
        btn = btns[idx]
        if self.button_label.value and str(self.button_label.value).strip():
            btn["label"] = str(self.button_label.value).strip()
        if self.button_command.value and str(self.button_command.value).strip():
            btn["command"] = str(self.button_command.value).strip()
        if self.button_style.value and str(self.button_style.value).strip():
            btn["style"] = str(self.button_style.value).strip().lower()
        if self.need_modal.value and str(self.need_modal.value).strip():
            need = str(self.need_modal.value).strip().lower() in ("yes", "y")
            if need and self.modal_fields.value and str(self.modal_fields.value).strip():
                fields = []
                for f in str(self.modal_fields.value).split(","):
                    f = f.strip()
                    if f: fields.append({"label": f, "required": True})
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
                    try:
                        msg = await ch.fetch_message(panel["message_id"])
                        await msg.edit(view=view)
                        self.cog.bot.add_view(view, message_id=panel["message_id"])
                    except Exception:
                        pass
            except Exception:
                pass
        try:
            await interaction.followup.send("Updated", ephemeral=True)
        except Exception:
            pass


class DeletePanelModal(discord.ui.Modal, title="Delete"):
    panel_id = discord.ui.TextInput(label="Panel ID", required=True)

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            try:
                return await interaction.followup.send("No", ephemeral=True)
            except Exception:
                return
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
                    try:
                        msg = ch.get_partial_message(panel["message_id"])
                        await msg.delete()
                    except Exception:
                        pass
            except Exception:
                pass
        del panels[pid]
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        try:
            await interaction.followup.send("Deleted", ephemeral=True)
        except Exception:
            pass


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
                try:
                    self.bot.add_view(view, message_id=msg.id)
                except Exception:
                    pass
        except Exception as e:
            log.error(f"_repost error: {e}")

    async def button_clicked(self, interaction: discord.Interaction, panel_id: str, btn_idx: int):
        try:
            panels = await self.config.guild(interaction.guild).panels()
            if panel_id not in panels:
                try:
                    if not interaction.response.is_done():
                        return await interaction.response.send_message("Missing", ephemeral=True)
                except Exception:
                    return
            btns = panels[panel_id].get("buttons", [])
            if btn_idx >= len(btns):
                try:
                    if not interaction.response.is_done():
                        return await interaction.response.send_message("Missing", ephemeral=True)
                except Exception:
                    return
            btn = btns[btn_idx]
            command = btn.get("command", "")
            fields = btn.get("modal_fields", [])
            if fields:
                modal = ContextModal(self, panel_id, btn_idx, command, fields, btn.get("label", ""))
                try:
                    if not interaction.response.is_done():
                        return await interaction.response.send_modal(modal)
                except Exception:
                    return
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
            except Exception:
                pass
            await self.invoke_command(interaction, command)
        except Exception as e:
            log.error(f"button_clicked error: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Error", ephemeral=True)
                else:
                    await interaction.followup.send("Error", ephemeral=True)
            except Exception:
                pass

    async def invoke_command(self, interaction: discord.Interaction, command: str):
        try:
            prefix = "!"
            cmd_clean = command.strip()
            # Strip leading prefixes
            while cmd_clean and cmd_clean and cmd_clean[0] in "!.?/\\-~+":
                cmd_clean = cmd_clean[1:].strip()
            full = f"{prefix}{cmd_clean}"
            msg = interaction.message
            if not msg:
                try:
                    return await interaction.followup.send("No msg", ephemeral=True)
                except Exception:
                    return
            try:
                # Create a context by modifying the message
                msg.content = full
            except Exception:
                pass
            try:
                ctx = await self.bot.get_context(msg)
                ctx.author = interaction.user
                ctx.channel = interaction.channel
                ctx.guild = interaction.guild
                if ctx.valid and ctx.command:
                    await self.bot.invoke(ctx)
                    return
                ctx2 = await self.bot.get_context(ctx.message)
                ctx2.author = interaction.user
                ctx2.channel = interaction.channel
                ctx2.guild = interaction.guild
                if ctx2.valid and ctx2.command:
                    await self.bot.invoke(ctx2)
                    return
                await self.bot.process_commands(ctx.message)
                return
            except Exception as e:
                try:
                    await interaction.followup.send(f"{prefix}{cmd_clean}", ephemeral=True)
                except Exception:
                    pass
                return
        except Exception as e:
            try:
                await interaction.followup.send("Err", ephemeral=True)
            except Exception:
                pass

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
                try:
                    self.bot.add_view(view, message_id=msg.id)
                except Exception:
                    pass
        except Exception as e:
            log.error(f"_repost error: {e}")

    async def button_clicked(self, interaction: discord.Interaction, panel_id: str, btn_idx: int):
        try:
            panels = await self.config.guild(interaction.guild).panels()
            if panel_id not in panels:
                try:
                    if not interaction.response.is_done():
                        return await interaction.response.send_message("Missing", ephemeral=True)
                except Exception:
                    return
            btns = panels[panel_id].get("buttons", [])
            if btn_idx >= len(btns):
                try:
                    if not interaction.response.is_done():
                        return await interaction.response.send_message("Missing", ephemeral=True)
                except Exception:
                    return
            btn = btns[btn_idx]
            command = btn.get("command", "")
            fields = btn.get("modal_fields", [])
            if fields:
                modal = ContextModal(self, panel_id, btn_idx, command, fields, btn.get("label", ""))
                try:
                    if not interaction.response.is_done():
                        return await interaction.response.send_modal(modal)
                except Exception:
                    return
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
            except Exception:
                pass
            await self.invoke_command(interaction, command)
        except Exception as e:
            log.error(f"button_clicked error: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Error", ephemeral=True)
                else:
                    await interaction.followup.send("Error", ephemeral=True)
            except Exception:
                pass

    async def invoke_command(self, interaction: discord.Interaction, command: str):
        try:
            prefix = "!"
            cmd_clean = command.strip()
            # Strip prefixes
            while cmd_clean and cmd_clean[0] in "!.?/\\-~+><":
                cmd_clean = cmd_clean[1:].strip()
            full = f"{prefix}{cmd_clean}"
            msg = interaction.message
            if not msg:
                try:
                    return await interaction.followup.send(full, ephemeral=True)
                except Exception:
                    return
            try:
                msg.content = full
            except Exception:
                pass
            try:
                ctx = await self.bot.get_context(msg)
                ctx.author = interaction.user
                ctx.channel = interaction.channel
                ctx.guild = interaction.guild
                if ctx.valid and ctx.command:
                    await self.bot.invoke(ctx)
                    return
                ctx2 = await self.bot.get_context(ctx.message)
                ctx2.author = interaction.user
                ctx2.channel = interaction.channel
                ctx2.guild = interaction.guild
                if ctx2.valid and ctx2.command:
                    await self.bot.invoke(ctx2)
                    return
                await self.bot.process_commands(ctx.message)
                return
            except Exception as e:
                try:
                    await interaction.followup.send(full, ephemeral=True)
                except Exception:
                    pass
                return
        except Exception as e:
            try:
                await interaction.followup.send("Err", ephemeral=True)
            except Exception:
                pass
