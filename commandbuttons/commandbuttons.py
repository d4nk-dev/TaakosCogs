"""CommandButtons - Sticky command button panels with modals."""
import asyncio
import logging
import re
from typing import Dict, List

import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

UNIQUE_ID = 0xCB5E9010
log = logging.getLogger("red.commandbuttons")


class PanelView(discord.ui.View):
    def __init__(self, cog, panel_id: str, buttons: List[dict]):
        super().__init__(timeout=None)
        self.cog = cog
        self.panel_id = panel_id
        for i, btn in enumerate(buttons):
            style = discord.ButtonStyle.secondary
            s = str(btn.get("style", "secondary")).lower()
            if s in ("primary", "blurple"):
                style = discord.ButtonStyle.primary
            elif s in ("success", "green"):
                style = discord.ButtonStyle.success
            elif s in ("danger", "red"):
                style = discord.ButtonStyle.danger
            b = discord.ui.Button(label=btn.get("label") or "Click", style=style, custom_id=f"cb:{panel_id}:{i}")
            b.callback = self._cb(i)
            self.add_item(b)

    def _cb(self, idx):
        async def callback(interaction: discord.Interaction):
            await self.cog.on_button(interaction, self.panel_id, idx)
        return callback


class InputModal(discord.ui.Modal, title="Command Input"):
    def __init__(self, cog, panel_id: str, btn_idx: int, command: str, fields: List[dict]):
        super().__init__(timeout=300)
        self.cog = cog
        self.panel_id = panel_id
        self.btn_idx = btn_idx
        self.command = command
        for field in fields[:5]:
            self.add_item(discord.ui.TextInput(label=str(field.get("label", "Value"))[:45], required=field.get("required", True), style=discord.TextStyle.short if not field.get("multiline") else discord.TextStyle.paragraph))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        vals = []
        for c in self.children:
            if isinstance(c, discord.ui.TextInput):
                vals.append(str(c.value))
        cmd = self.command
        try:
            for i, v in enumerate(vals):
                cmd = cmd.replace("{" + str(i) + "}", v)
            if "{" not in cmd:
                for v in vals:
                    if " " in v:
                        cmd += f' "{v}"'
                    else:
                        cmd += f' {v}'
        except Exception:
            pass
        await self.cog.run_cmd(interaction, cmd)


class ManagerView(discord.ui.View):
    def __init__(self, cog, ctx):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx

    @discord.ui.button(label="Create Panel", style=discord.ButtonStyle.primary, row=0)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("No", ephemeral=True)
        await interaction.response.send_modal(CreateModal(self.cog))

    @discord.ui.button(label="List Panels/Buttons", style=discord.ButtonStyle.secondary, row=0)
    async def listp(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("No", ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        if not panels:
            return await interaction.response.send_message("None", ephemeral=True)
        e = discord.Embed(title="Panels", color=0x2F3136)
        for pid, p in panels.items():
            btns = p.get("buttons", [])
            txt = "\n".join([f"[{i}] {b.get('label')} -> {b.get('command')}" for i, b in enumerate(btns)]) or "Empty"
            e.add_field(name=f"{p.get('name')} ({pid})", value=txt, inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="Add Button", style=discord.ButtonStyle.success, row=1)
    async def addb(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("No", ephemeral=True)
        await interaction.response.send_modal(AddModal(self.cog))

    @discord.ui.button(label="Remove Button", style=discord.ButtonStyle.danger, row=1)
    async def remb(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("No", ephemeral=True)
        await interaction.response.send_modal(RemoveModal(self.cog))

    @discord.ui.button(label="Edit Button", style=discord.ButtonStyle.primary, row=1)
    async def editb(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("No", ephemeral=True)
        await interaction.response.send_modal(EditModal(self.cog))

    @discord.ui.button(label="Post", style=discord.ButtonStyle.success, row=2)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("No", ephemeral=True)
        await interaction.response.send_modal(PostModal(self.cog))

    @discord.ui.button(label="Sticky", style=discord.ButtonStyle.danger, row=2)
    async def sticky(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("No", ephemeral=True)
        await interaction.response.send_modal(StickyModal(self.cog))

    @discord.ui.button(label="Delete Panel", style=discord.ButtonStyle.danger, row=3)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("No", ephemeral=True)
        await interaction.response.send_modal(DeleteModal(self.cog))


class CreateModal(discord.ui.Modal, title="Create Panel"):
    name = discord.ui.TextInput(label="Panel ID/Name", placeholder="tools", required=True)
    desc = discord.ui.TextInput(label="Description", required=False, style=discord.TextStyle.paragraph)

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.name.value).strip().lower().replace(" ", "-")
        panels[pid] = {"name": str(self.name.value).strip(), "description": str(self.desc.value).strip() if self.desc.value else None, "buttons": [], "message_id": None, "channel_id": None, "sticky_last_id": None}
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        await interaction.followup.send(f"Created {pid}", ephemeral=True)


class AddModal(discord.ui.Modal, title="Add Button"):
    panel_id = discord.ui.TextInput(label="Panel ID", required=True)
    label = discord.ui.TextInput(label="Button Label", required=True)
    command = discord.ui.TextInput(label="Command (without !)", required=True)
    style = discord.ui.TextInput(label="primary/secondary/success/danger", required=False)
    need_modal = discord.ui.TextInput(label="Modal? (yes/no)", required=False)
    fields = discord.ui.TextInput(label="Field labels comma sep", required=False, style=discord.TextStyle.paragraph)

    def __init__(self, cog):
        super().__init__(timeout=600)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            return await interaction.followup.send("Not found", ephemeral=True)
        lbl = str(self.label.value).strip()
        cmd = str(self.command.value).strip()
        st = str(self.style.value or "secondary").strip().lower()
        need = str(self.need_modal.value or "no").lower() in ("yes", "y")
        flds = []
        if need and self.fields.value:
            for f in str(self.fields.value).split(","):
                f = f.strip()
                if f:
                    flds.append({"label": f, "required": True})
        panels[pid]["buttons"].append({"label": lbl, "command": cmd, "style": st, "modal_fields": flds})
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        panel = panels[pid]
        if panel.get("channel_id") and panel.get("message_id"):
            try:
                ch = interaction.guild.get_channel(panel["channel_id"])
                if ch:
                    view = PanelView(self.cog, pid, panels[pid]["buttons"])
                    try:
                        msg = await ch.fetch_message(panel["message_id"])
                        await msg.edit(view=view)
                        self.cog.bot.add_view(view, message_id=panel["message_id"])
                    except Exception:
                        pass
            except Exception:
                pass
        await interaction.followup.send("Added", ephemeral=True)


class RemoveModal(discord.ui.Modal, title="Remove Button"):
    panel_id = discord.ui.TextInput(label="Panel ID", required=True)
    index = discord.ui.TextInput(label="Button Index", required=True)

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            return await interaction.followup.send("Not found", ephemeral=True)
        try:
            idx = int(str(self.index.value))
        except Exception:
            return await interaction.followup.send("Bad index", ephemeral=True)
        btns = panels[pid]["buttons"]
        if idx < 0 or idx >= len(btns):
            return await interaction.followup.send("Bad index", ephemeral=True)
        btns.pop(idx)
        panels[pid]["buttons"] = btns
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        panel = panels[pid]
        if panel.get("channel_id") and panel.get("message_id"):
            try:
                ch = interaction.guild.get_channel(panel["channel_id"])
                if ch:
                    if btns:
                        view = PanelView(self.cog, pid, btns)
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
        await interaction.followup.send("Removed", ephemeral=True)


class EditModal(discord.ui.Modal, title="Edit Button"):
    panel_id = discord.ui.TextInput(label="Panel ID", required=True)
    index = discord.ui.TextInput(label="Button Index", required=True)
    label = discord.ui.TextInput(label="Label", required=False)
    command = discord.ui.TextInput(label="Command", required=False)
    style = discord.ui.TextInput(label="Style", required=False)
    need_modal = discord.ui.TextInput(label="Modal (yes/no)", required=False)
    fields = discord.ui.TextInput(label="Fields comma", required=False, style=discord.TextStyle.paragraph)

    def __init__(self, cog):
        super().__init__(timeout=600)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            return await interaction.followup.send("Not found", ephemeral=True)
        try:
            idx = int(str(self.index.value))
        except Exception:
            return await interaction.followup.send("Bad index", ephemeral=True)
        btns = panels[pid]["buttons"]
        if idx < 0 or idx >= len(btns):
            return await interaction.followup.send("Bad index", ephemeral=True)
        btn = btns[idx]
        if self.label.value and str(self.label.value).strip():
            btn["label"] = str(self.label.value).strip()
        if self.command.value and str(self.command.value).strip():
            btn["command"] = str(self.command.value).strip()
        if self.style.value and str(self.style.value).strip():
            btn["style"] = str(self.style.value).strip().lower()
        if self.need_modal.value and str(self.need_modal.value).strip():
            need = str(self.need_modal.value).strip().lower() in ("yes", "y")
            if need and self.fields.value and str(self.fields.value).strip():
                flds = []
                for f in str(self.fields.value).split(","):
                    f = f.strip()
                    if f:
                        flds.append({"label": f, "required": True})
                btn["modal_fields"] = flds
            elif not need:
                btn["modal_fields"] = []
        btns[idx] = btn
        panels[pid]["buttons"] = btns
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        panel = panels[pid]
        if panel.get("channel_id") and panel.get("message_id"):
            try:
                ch = interaction.guild.get_channel(panel["channel_id"])
                if ch:
                    view = PanelView(self.cog, pid, btns)
                    try:
                        msg = await ch.fetch_message(panel["message_id"])
                        await msg.edit(view=view)
                        self.cog.bot.add_view(view, message_id=panel["message_id"])
                    except Exception:
                        pass
            except Exception:
                pass
        await interaction.followup.send("Updated", ephemeral=True)


class PostModal(discord.ui.Modal, title="Post Panel"):
    panel_id = discord.ui.TextInput(label="Panel ID", required=True)
    channel = discord.ui.TextInput(label="Channel ID", required=True)

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            return await interaction.followup.send("Not found", ephemeral=True)
        m = re.findall(r'(\d+)', str(self.channel.value))
        ch = interaction.guild.get_channel(int(m[-1])) if m else interaction.channel
        if not ch:
            return await interaction.followup.send("Bad channel", ephemeral=True)
        panel = panels[pid]
        view = PanelView(self.cog, pid, panel["buttons"])
        content = panel.get("description") or f"**{panel['name']}**"
        try:
            msg = await ch.send(content, view=view)
        except Exception as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        panel["message_id"] = msg.id
        panel["channel_id"] = ch.id
        panels[pid] = panel
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        try:
            self.cog.bot.add_view(view, message_id=msg.id)
        except Exception:
            pass
        await interaction.followup.send("Posted", ephemeral=True)


class StickyModal(discord.ui.Modal, title="Make Sticky"):
    panel_id = discord.ui.TextInput(label="Panel ID", required=True)
    channel = discord.ui.TextInput(label="Channel ID", required=True)

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            return await interaction.followup.send("Not found", ephemeral=True)
        m = re.findall(r'(\d+)', str(self.channel.value))
        ch = interaction.guild.get_channel(int(m[-1])) if m else interaction.channel
        if not ch:
            return await interaction.followup.send("Bad channel", ephemeral=True)
        sticky = await self.cog.config.guild(interaction.guild).sticky()
        sticky[str(ch.id)] = {"panel_id": pid, "cooldown": 3}
        await self.cog.config.guild(interaction.guild).sticky.set(sticky)
        panel = panels[pid]
        view = PanelView(self.cog, pid, panel["buttons"])
        content = panel.get("description") or f"**{panel['name']}**"
        if panel.get("sticky_last_id"):
            try:
                old = ch.get_partial_message(panel["sticky_last_id"])
                await old.delete()
            except Exception:
                pass
        try:
            msg = await ch.send(content, view=view)
        except Exception as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        panel["message_id"] = msg.id
        panel["channel_id"] = ch.id
        panel["sticky_last_id"] = msg.id
        panels[pid] = panel
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        try:
            self.cog.bot.add_view(view, message_id=msg.id)
        except Exception:
            pass
        await interaction.followup.send("Sticky set", ephemeral=True)


class DeleteModal(discord.ui.Modal, title="Delete Panel"):
    panel_id = discord.ui.TextInput(label="Panel ID", required=True)
    confirm = discord.ui.TextInput(label="Type DELETE", required=True)

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if str(self.confirm.value).strip().upper() != "DELETE":
            return await interaction.followup.send("Cancelled", ephemeral=True)
        panels = await self.cog.config.guild(interaction.guild).panels()
        pid = str(self.panel_id.value).strip()
        if pid not in panels:
            return await interaction.followup.send("Not found", ephemeral=True)
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
        await interaction.followup.send("Deleted", ephemeral=True)


class CommandButtons(commands.Cog):
    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=UNIQUE_ID, force_registration=True)
        self.config.register_guild(panels={}, sticky={})
        self.locked = set()
        self._cvs: Dict[int, asyncio.Condition] = {}

    async def cog_load(self):
        self.bot.loop.create_task(self._restore())

    async def _restore(self):
        try:
            await self.bot.wait_until_red_ready()
            all_guilds = await self.config.all_guilds()
            for gid, data in all_guilds.items():
                for pid, panel in data.get("panels", {}).items():
                    try:
                        view = PanelView(self, pid, panel.get("buttons", []))
                        mid = panel.get("message_id")
                        if mid:
                            self.bot.add_view(view, message_id=mid)
                    except Exception:
                        pass
        except Exception:
            pass

    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    @commands.command(name="cbuttons")
    async def cbuttons(self, ctx: commands.Context):
        view = ManagerView(self, ctx)
        embed = discord.Embed(title="Command Buttons", description="Create panels, manage buttons by ID/index, post/make sticky. Use 'List Panels/Buttons' to see IDs/indices.", color=0x2F3136)
        await ctx.send(embed=embed, view=view)

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
        except Exception:
            pass

    async def _repost(self, guild, channel, panel_id):
        try:
            cv = self._cvs.setdefault(channel.id, asyncio.Condition())
            async with cv:
                await cv.wait_for(lambda: channel.id not in self.locked)
            sticky = await self.config.guild(guild).sticky()
            cfg = sticky.get(str(channel.id))
            if not cfg or cfg.get("panel_id") != panel_id:
                return
            await asyncio.sleep(cfg.get("cooldown", 3))
            async with cv:
                await cv.wait_for(lambda: channel.id not in self.locked)
                sticky2 = await self.config.guild(guild).sticky()
                if str(channel.id) not in sticky2:
                    return
                panels = await self.config.guild(guild).panels()
                if panel_id not in panels:
                    return
                panel = panels[panel_id]
                old = panel.get("sticky_last_id")
                if old:
                    try:
                        m = channel.get_partial_message(old)
                        await m.delete()
                    except Exception:
                        pass
                view = PanelView(self, panel_id, panel["buttons"])
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
        except Exception:
            pass

    async def on_button(self, interaction: discord.Interaction, panel_id: str, btn_idx: int):
        try:
            panels = await self.config.guild(interaction.guild).panels()
            if panel_id not in panels:
                return await interaction.response.send_message("Missing", ephemeral=True)
            btns = panels[panel_id].get("buttons", [])
            if btn_idx >= len(btns):
                return await interaction.response.send_message("Missing", ephemeral=True)
            btn = btns[btn_idx]
            cmd = btn.get("command", "")
            fields = btn.get("modal_fields", [])
            if fields:
                return await interaction.response.send_modal(InputModal(self, panel_id, btn_idx, cmd, fields))
            await interaction.response.defer(ephemeral=True)
            await self.run_cmd(interaction, cmd)
        except Exception:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Err", ephemeral=True)
            except Exception:
                pass

    async def run_cmd(self, interaction: discord.Interaction, command: str):
        try:
            prefix = "!"
            cmd = command.strip()
            while cmd and cmd[0] in "!.?/\\-~+><":
                cmd = cmd[1:].strip()
            full = f"{prefix}{cmd}"
            msg = interaction.message
            if not msg:
                return await interaction.followup.send(full, ephemeral=True)
            try:
                msg.content = full
            except Exception:
                pass
            try:
                ctx = await self.bot.get_context(msg)
                ctx.author = interaction.user
                ctx.channel = interaction.channel
                ctx.guild = interaction.guild
                if ctx.message:
                    ctx.message.content = full
                    ctx.message.author = interaction.user
                    ctx.message.channel = interaction.channel
                    ctx.message.guild = interaction.guild
                if ctx.command and ctx.valid:
                    await self.bot.invoke(ctx)
                    return
                ctx2 = await self.bot.get_context(ctx.message)
                ctx2.author = interaction.user
                ctx2.channel = interaction.channel
                ctx2.guild = interaction.guild
                if ctx2.command and ctx2.valid:
                    await self.bot.invoke(ctx2)
                    return
                await self.bot.process_commands(ctx2.message)
                return
            except Exception:
                try:
                    await interaction.followup.send(full, ephemeral=True)
                except Exception:
                    pass
                return
        except Exception:
            try:
                await interaction.followup.send("Err", ephemeral=True)
            except Exception:
                pass
