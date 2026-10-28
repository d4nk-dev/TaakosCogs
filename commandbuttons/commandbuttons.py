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
