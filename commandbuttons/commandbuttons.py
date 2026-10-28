"""CommandButtons cog - Sticky command button panels with modal support."""
import asyncio
import logging
import re
from typing import Any, Dict, List

import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

UNIQUE_ID = 0xCB5E9007
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
        try:
            if "{" in cmd:
                for i, v in enumerate(values):
                    cmd = cmd.replace("{" + str(i) + "}", v)
            else:
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
        view = PanelManagerView(self, ctx)
        embed = discord.Embed(
            title="Command Buttons Manager",
            description="Create panels, add buttons with commands/modals, post/make sticky.",
            color=await ctx.embed_color(),
        )
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
            while cmd_clean and cmd_clean[0] in "!.?/\\-~+><":
                cmd_clean = cmd_clean[1:].strip()
            full = f"{prefix}{cmd_clean}"
            msg = interaction.message
            if not msg:
                try:
                    await interaction.followup.send(full, ephemeral=True)
                    return
                except Exception:
                    return
            # Try to make a new message-like context
            try:
                # For Red, we can use bot.get_context on the message but override
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
                # Rebuild context
                ctx2 = await self.bot.get_context(ctx.message)
                ctx2.author = interaction.user
                ctx2.channel = interaction.channel
                ctx2.guild = interaction.guild
                if ctx2.command and ctx2.valid:
                    await self.bot.invoke(ctx2)
                    return
                await self.bot.process_commands(ctx2.message)
                return
            except Exception as e:
                try:
                    await interaction.followup.send(full, ephemeral=True)
                    return
                except Exception:
                    pass
                return
        except Exception as e:
            try:
                await interaction.followup.send("Error", ephemeral=True)
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

    @discord.ui.button(label="Manage", style=discord.ButtonStyle.secondary, row=0)
    async def manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            try:
                if not interaction.response.is_done():
                    return await interaction.response.send_message("Not for you.", ephemeral=True)
            except Exception:
                return
        try:
            modal = QuickManageModal(self.cog)
            if not interaction.response.is_done():
                await interaction.response.send_modal(modal)
        except Exception:
            pass

    @discord.ui.button(label="Post", style=discord.ButtonStyle.success, row=1)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
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


class CreatePanelModal(discord.ui.Modal, title="Create Panel"):
    name = discord.ui.TextInput(label="Panel Name", required=True)
    description = discord.ui.TextInput(label="Description", required=False, style=discord.TextStyle.paragraph)

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
        pid = str(self.name.value).strip().lower().replace(" ", "-")
        panels[pid] = {"name": str(self.name.value).strip(), "description": str(self.description.value).strip() if self.description.value else None, "buttons": [], "message_id": None, "channel_id": None, "sticky_last_id": None}
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        try:
            await interaction.followup.send(f"Created `{pid}`", ephemeral=True)
        except Exception:
            pass


class QuickManageModal(discord.ui.Modal, title="Manage Panel"):
    panel_id = discord.ui.TextInput(label="Panel ID", required=True)
    action = discord.ui.TextInput(label="add/remove/edit/view/post/sticky/list", required=True)
    index = discord.ui.TextInput(label="Button Index", required=False)
    label = discord.ui.TextInput(label="Label", required=False)
    command = discord.ui.TextInput(label="Command", required=False)
    style = discord.ui.TextInput(label="Style", required=False)
    modal_needed = discord.ui.TextInput(label="Modal? (yes/no)", required=False)
    fields = discord.ui.TextInput(label="Fields comma", required=False, style=discord.TextStyle.paragraph)
    channel = discord.ui.TextInput(label="Channel ID for post/sticky", required=False)

    def __init__(self, cog):
        super().__init__(timeout=600)
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
        action = str(self.action.value).strip().lower()
        if action == "view":
            btns = panels[pid].get("buttons", [])
            if not btns:
                try:
                    return await interaction.followup.send("Empty", ephemeral=True)
                except Exception:
                    return
            msg = "\n".join([f"{i}: {b.get('label')} -> {b.get('command')} (m:{len(b.get('modal_fields',[]))})" for i,b in enumerate(btns)])
            try:
                return await interaction.followup.send(f"```\n{msg}\n```", ephemeral=True)
            except Exception:
                return
        elif action == "list":
            msg = "\n".join([f"{k}: {v.get('name')} ({len(v.get('buttons',[]))} btns)" for k,v in panels.items()])
            try:
                return await interaction.followup.send(f"```\n{msg}\n```", ephemeral=True)
            except Exception:
                return
        elif action == "add":
            lbl = str(self.label.value or "Button").strip()
            cmd = str(self.command.value or "").strip()
            st = str(self.style.value or "secondary").strip().lower()
            need = str(self.modal_needed.value or "no").lower() in ("yes", "y")
            flds = []
            if need and self.fields.value:
                for f in str(self.fields.value).split(","):
                    f = f.strip()
                    if f: flds.append({"label": f, "required": True})
            panels[pid]["buttons"].append({"label": lbl, "command": cmd, "style": st, "modal_fields": flds})
            await self.cog.config.guild(interaction.guild).panels.set(panels)
            panel = panels[pid]
            if panel.get("channel_id") and panel.get("message_id"):
                try:
                    ch = interaction.guild.get_channel(panel["channel_id"])
                    if ch:
                        view = PanelButtonView(self.cog, pid, panels[pid]["buttons"])
                        try:
                            msg = await ch.fetch_message(panel["message_id"])
                            await msg.edit(view=view)
                            self.cog.bot.add_view(view, message_id=panel["message_id"])
                        except Exception:
                            pass
                except Exception:
                    pass
            try:
                return await interaction.followup.send("Added", ephemeral=True)
            except Exception:
                return
        elif action == "remove":
            try:
                idx = int(str(self.index.value))
            except Exception:
                try:
                    return await interaction.followup.send("Bad index", ephemeral=True)
                except Exception:
                    return
            btns = panels[pid]["buttons"]
            if idx < 0 or idx >= len(btns): 
                try:
                    return await interaction.followup.send("Bad index", ephemeral=True)
                except Exception:
                    return
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
                return await interaction.followup.send("Removed", ephemeral=True)
            except Exception:
                return
        elif action == "edit":
            try:
                idx = int(str(self.index.value))
            except Exception:
                try:
                    return await interaction.followup.send("Bad index", ephemeral=True)
                except Exception:
                    return
            btns = panels[pid]["buttons"]
            if idx < 0 or idx >= len(btns): 
                try:
                    return await interaction.followup.send("Bad index", ephemeral=True)
                except Exception:
                    return
            btn = btns[idx]
            if self.label.value and str(self.label.value).strip(): btn["label"] = str(self.label.value).strip()
            if self.command.value and str(self.command.value).strip(): btn["command"] = str(self.command.value).strip()
            if self.style.value and str(self.style.value).strip(): btn["style"] = str(self.style.value).strip().lower()
            if self.modal_needed.value and str(self.modal_needed.value).strip():
                need = str(self.modal_needed.value).strip().lower() in ("yes","y")
                if need and self.fields.value and str(self.fields.value).strip():
                    flds=[]
                    for f in str(self.fields.value).split(","):
                        f=f.strip()
                        if f: flds.append({"label":f,"required":True})
                    btn["modal_fields"]=flds
                elif not need: btn["modal_fields"]=[]
            btns[idx]=btn
            panels[pid]["buttons"]=btns
            await self.cog.config.guild(interaction.guild).panels.set(panels)
            panel = panels[pid]
            if panel.get("channel_id") and panel.get("message_id"):
                try:
                    ch = interaction.guild.get_channel(panel["channel_id"])
                    if ch:
                        view = PanelButtonView(self.cog, pid, btns)
                        try:
                            msg = await ch.fetch_message(panel["message_id"])
                            await msg.edit(view=view)
                            self.cog.bot.add_view(view, message_id=panel["message_id"])
                        except Exception:
                            pass
                except Exception:
                    pass
            try:
                return await interaction.followup.send("Updated", ephemeral=True)
            except Exception:
                return
        elif action == "post":
            import re
            m = re.findall(r'(\d+)', str(self.channel.value or ""))
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
            panel["message_id"]=msg.id; panel["channel_id"]=ch.id
            panels[pid]=panel
            await self.cog.config.guild(interaction.guild).panels.set(panels)
            try:
                self.cog.bot.add_view(view, message_id=msg.id)
            except Exception:
                pass
            try:
                return await interaction.followup.send("Posted", ephemeral=True)
            except Exception:
                return
        elif action == "sticky":
            import re
            m = re.findall(r'(\d+)', str(self.channel.value or ""))
            ch = interaction.guild.get_channel(int(m[-1])) if m else interaction.channel
            if not ch:
                try:
                    return await interaction.followup.send("Bad channel", ephemeral=True)
                except Exception:
                    return
            sticky = await self.cog.config.guild(interaction.guild).sticky()
            sticky[str(ch.id)] = {"panel_id": pid, "cooldown": 3}
            await self.cog.config.guild(interaction.guild).sticky.set(sticky)
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
            panel["message_id"]=msg.id; panel["channel_id"]=ch.id; panel["sticky_last_id"]=msg.id
            panels[pid]=panel
            await self.cog.config.guild(interaction.guild).panels.set(panels)
            try:
                self.cog.bot.add_view(view, message_id=msg.id)
            except Exception:
                pass
            try:
                return await interaction.followup.send("Sticky set", ephemeral=True)
            except Exception:
                return
        else:
            try:
                return await interaction.followup.send("Unknown action", ephemeral=True)
            except Exception:
                return


class PostPanelModal(discord.ui.Modal, title="Post"):
    panel_id = discord.ui.TextInput(label="Panel ID", required=True)
    channel = discord.ui.TextInput(label="Channel", required=True)
    sticky = discord.ui.TextInput(label="Sticky (yes/no)", required=False)

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
        m = re.findall(r'(\d+)', str(self.channel.value))
        ch = interaction.guild.get_channel(int(m[-1])) if m else interaction.channel
        if not ch:
            try:
                return await interaction.followup.send("Bad", ephemeral=True)
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
        panel["message_id"]=msg.id; panel["channel_id"]=ch.id
        if str(self.sticky.value or "").lower() in ("yes","y"):
            st = await self.cog.config.guild(interaction.guild).sticky()
            st[str(ch.id)]={"panel_id":pid,"cooldown":3}
            await self.cog.config.guild(interaction.guild).sticky.set(st)
            panel["sticky_last_id"]=msg.id
        panels[pid]=panel
        await self.cog.config.guild(interaction.guild).panels.set(panels)
        try:
            self.cog.bot.add_view(view, message_id=msg.id)
        except Exception:
            pass
        try:
            await interaction.followup.send("Done", ephemeral=True)
        except Exception:
            pass
