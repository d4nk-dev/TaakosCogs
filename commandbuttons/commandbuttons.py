"""CommandButtons cog - Sticky command button panels with modal support."""
import asyncio
import contextlib
import logging
from typing import Any, Dict, List

import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

UNIQUE_ID = 0xCB5E9002
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


class ContextModal(discord.ui.Modal):
    def __init__(self, cog, panel_id: str, btn_idx: int, command: str, fields: List[dict]):
        title = "Command Options"
        if fields and fields[0].get("modal_title"):
            title = fields[0]["modal_title"]
        super().__init__(title=title[:45], timeout=300)
        self.cog = cog
        self.panel_id = panel_id
        self.btn_idx = btn_idx
        self.command = command
        self.fields = fields

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
        await interaction.response.defer(ephemeral=True)
        parts = []
        for child in self.children:
            if isinstance(child, discord.ui.TextInput):
                val = child.value
                if " " in str(val):
                    parts.append(f'"{val}"')
                else:
                    parts.append(str(val))
        cmd = f"{self.command} {' '.join(parts)}".strip()
        await self.cog.invoke_command(interaction, cmd)


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
    @commands.group(name="cbuttons", aliases=["commandbuttons", "btnpanel"], invoke_without_command=True)
    async def cbuttons(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @cbuttons.command(name="new")
    async def cbuttons_new(self, ctx: commands.Context, name: str, *, description: str = None):
        panels = await self.config.guild(ctx.guild).panels()
        pid = name.lower().replace(" ", "-")
        panels[pid] = {
            "name": name,
            "description": description,
            "buttons": [],
            "message_id": None,
            "channel_id": None,
            "sticky_last_id": None,
        }
        await self.config.guild(ctx.guild).panels.set(panels)
        await ctx.send(f"Created panel `{pid}`. Add buttons with `{ctx.clean_prefix}cbuttons add {pid}`")

    @cbuttons.command(name="add")
    async def cbuttons_add(self, ctx: commands.Context, panel_id: str):
        panels = await self.config.guild(ctx.guild).panels()
        if panel_id not in panels:
            await ctx.send("Panel not found.")
            return

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        await ctx.send("Command to run (e.g. `kick @user reason`):")
        try:
            m = await self.bot.wait_for("message", check=check, timeout=120)
            command = m.content.strip()
        except asyncio.TimeoutError:
            return await ctx.send("Timeout.")

        await ctx.send("Button label:")
        try:
            m = await self.bot.wait_for("message", check=check, timeout=60)
            label = m.content.strip()
        except asyncio.TimeoutError:
            return await ctx.send("Timeout.")

        await ctx.send("Style (primary/secondary/success/danger) [secondary]:")
        try:
            m = await self.bot.wait_for("message", check=check, timeout=30)
            style = m.content.strip().lower() or "secondary"
        except asyncio.TimeoutError:
            style = "secondary"

        await ctx.send("Need modal for extra context? (y/n) [n]:")
        try:
            m = await self.bot.wait_for("message", check=check, timeout=30)
            need = m.content.strip().lower() in ("y", "yes")
        except asyncio.TimeoutError:
            need = False

        fields = []
        if need:
            await ctx.send("Number of inputs (1-5):")
            try:
                m = await self.bot.wait_for("message", check=check, timeout=30)
                n = max(1, min(5, int(m.content.strip())))
            except Exception:
                n = 1
            for i in range(n):
                await ctx.send(f"Input {i+1} label:")
                try:
                    m2 = await self.bot.wait_for("message", check=check, timeout=60)
                    fields.append({"label": m2.content.strip(), "required": True})
                except Exception:
                    fields.append({"label": f"Input {i+1}", "required": True})

        panels[panel_id]["buttons"].append({
            "command": command,
            "label": label,
            "style": style,
            "modal_fields": fields,
        })
        await self.config.guild(ctx.guild).panels.set(panels)
        await ctx.send("Button added.")

    @cbuttons.command(name="post")
    async def cbuttons_post(self, ctx: commands.Context, panel_id: str, channel: discord.TextChannel = None):
        panels = await self.config.guild(ctx.guild).panels()
        if panel_id not in panels:
            return await ctx.send("Panel not found.")
        channel = channel or ctx.channel
        panel = panels[panel_id]
        view = PanelButtonView(self, panel_id, panel["buttons"])
        content = panel.get("description") or f"**{panel['name']}**"
        msg = await channel.send(content, view=view)
        panel["message_id"] = msg.id
        panel["channel_id"] = channel.id
        panels[panel_id] = panel
        await self.config.guild(ctx.guild).panels.set(panels)
        self.bot.add_view(view, message_id=msg.id)
        await ctx.send(f"Posted in {channel.mention}.")

    @cbuttons.command(name="sticky")
    async def cbuttons_sticky(self, ctx: commands.Context, panel_id: str, channel: discord.TextChannel = None):
        panels = await self.config.guild(ctx.guild).panels()
        if panel_id not in panels:
            return await ctx.send("Panel not found.")
        channel = channel or ctx.channel
        sticky = await self.config.guild(ctx.guild).sticky()
        sticky[str(channel.id)] = {"panel_id": panel_id, "cooldown": self.DEFAULT_COOLDOWN}
        await self.config.guild(ctx.guild).sticky.set(sticky)

        panel = panels[panel_id]
        if panel.get("sticky_last_id"):
            try:
                old = channel.get_partial_message(panel["sticky_last_id"])
                await old.delete()
            except Exception:
                pass

        view = PanelButtonView(self, panel_id, panel["buttons"])
        content = panel.get("description") or f"**{panel['name']}**"
        msg = await channel.send(content, view=view)
        panel["message_id"] = msg.id
        panel["channel_id"] = channel.id
        panel["sticky_last_id"] = msg.id
        panels[panel_id] = panel
        await self.config.guild(ctx.guild).panels.set(panels)
        self.bot.add_view(view, message_id=msg.id)
        await ctx.send(f"Panel `{panel_id}` is now sticky in {channel.mention}.")

    @cbuttons.command(name="unsticky")
    async def cbuttons_unsticky(self, ctx: commands.Context, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        sticky = await self.config.guild(ctx.guild).sticky()
        key = str(channel.id)
        if key in sticky:
            del sticky[key]
            await self.config.guild(ctx.guild).sticky.set(sticky)
            await ctx.send("Unstuck.")
        else:
            await ctx.send("Not sticky.")

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
                modal = ContextModal(self, panel_id, btn_idx, command, fields)
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
            ctx = await self.bot.get_context(msg)
            if isinstance(self.bot.command_prefix, (list, tuple, set)):
                prefix = self.bot.command_prefix[0]
            else:
                prefix = self.bot.command_prefix
            if not prefix:
                prefix = "!"
            ctx.message.content = f"{prefix}{command}"
            ctx2 = await self.bot.get_context(ctx.message)
            ctx2.author = interaction.user
            ctx2.channel = interaction.channel
            ctx2.guild = interaction.guild
            if ctx2.valid and ctx2.command:
                await self.bot.invoke(ctx2)
                return
            await interaction.followup.send(f"Executed: `{command}`", ephemeral=True)
        except Exception as e:
            try:
                await interaction.followup.send(f"Error: {e}", ephemeral=True)
            except Exception:
                pass


    @cbuttons.command(name="list")
    async def cbuttons_list(self, ctx: commands.Context):
        """List all panels in this guild."""
        panels = await self.config.guild(ctx.guild).panels()
        if not panels:
            return await ctx.send("No panels found.")
        embed = discord.Embed(title="Command Button Panels", color=await ctx.embed_color())
        for pid, panel in panels.items():
            btn_count = len(panel.get("buttons", []))
            sticky_info = ""
            sticky = await self.config.guild(ctx.guild).sticky()
            for ch_id, cfg in sticky.items():
                if cfg.get("panel_id") == pid:
                    sticky_info = f"\nSticky: <#{ch_id}>"
                    break
            ch_info = f"<#{panel.get('channel_id')}>" if panel.get('channel_id') else "Not posted"
            embed.add_field(
                name=f"{panel.get('name')} (`{pid}`)",
                value=f"Buttons: {btn_count}{sticky_info}\nChannel: {ch_info}",
                inline=False,
            )
        await ctx.send(embed=embed)

    @cbuttons.command(name="view")
    async def cbuttons_view(self, ctx: commands.Context, panel_id: str):
        """View panel details and buttons."""
        panels = await self.config.guild(ctx.guild).panels()
        if panel_id not in panels:
            return await ctx.send("Panel not found.")
        panel = panels[panel_id]
        embed = discord.Embed(title=panel.get("name"), description=panel.get("description") or "", color=await ctx.embed_color())
        buttons = panel.get("buttons", [])
        if not buttons:
            embed.add_field(name="Buttons", value="None", inline=False)
        else:
            for i, btn in enumerate(buttons):
                modal = " (modal)" if btn.get("modal_fields") else ""
                embed.add_field(
                    name=f"{i}: {btn.get('label')}",
                    value=f"Command: `{btn.get('command')}`{modal} [style: {btn.get('style')}]",
                    inline=False,
                )
        await ctx.send(embed=embed)

    @cbuttons.command(name="delete")
    async def cbuttons_delete(self, ctx: commands.Context, panel_id: str):
        """Delete a panel."""
        panels = await self.config.guild(ctx.guild).panels()
        if panel_id not in panels:
            return await ctx.send("Panel not found.")
        panel = panels[panel_id]
        # Remove sticky if set to this panel
        sticky = await self.config.guild(ctx.guild).sticky()
        to_del = []
        for ch_id, cfg in sticky.items():
            if cfg.get("panel_id") == panel_id:
                to_del.append(ch_id)
        for ch_id in to_del:
            sticky.pop(ch_id)
        if to_del:
            await self.config.guild(ctx.guild).sticky.set(sticky)
        # Try to delete message if exists
        if panel.get("channel_id") and panel.get("message_id"):
            try:
                ch = ctx.guild.get_channel(panel["channel_id"])
                if ch:
                    msg = ch.get_partial_message(panel["message_id"])
                    await msg.delete()
            except Exception:
                pass
        del panels[panel_id]
        await self.config.guild(ctx.guild).panels.set(panels)
        await ctx.send(f"Deleted panel `{panel_id}`.")

    @cbuttons.command(name="removebutton")
    async def cbuttons_removebutton(self, ctx: commands.Context, panel_id: str, index: int):
        """Remove a button by index."""
        panels = await self.config.guild(ctx.guild).panels()
        if panel_id not in panels:
            return await ctx.send("Panel not found.")
        buttons = panels[panel_id]["buttons"]
        if index < 0 or index >= len(buttons):
            return await ctx.send("Invalid index.")
        removed = buttons.pop(index)
        await self.config.guild(ctx.guild).panels.set(panels)
        await ctx.send(f"Removed button '{removed.get('label')}'.")
        # Update posted message if exists
        panel = panels[panel_id]
        if panel.get("channel_id") and panel.get("message_id") and buttons:
            try:
                ch = ctx.guild.get_channel(panel["channel_id"])
                if ch:
                    view = PanelButtonView(self, panel_id, buttons)
                    msg = await ch.fetch_message(panel["message_id"])
                    await msg.edit(view=view)
                    self.bot.add_view(view, message_id=msg.id)
            except Exception:
                pass
        elif panel.get("channel_id") and panel.get("message_id") and not buttons:
            try:
                ch = ctx.guild.get_channel(panel["channel_id"])
                if ch:
                    msg = ch.get_partial_message(panel["message_id"])
                    await msg.edit(view=None)
            except Exception:
                pass

    @cbuttons.command(name="editbutton")
    async def cbuttons_editbutton(self, ctx: commands.Context, panel_id: str, index: int):
        """Edit a button interactively."""
        panels = await self.config.guild(ctx.guild).panels()
        if panel_id not in panels:
            return await ctx.send("Panel not found.")
        buttons = panels[panel_id]["buttons"]
        if index < 0 or index >= len(buttons):
            return await ctx.send("Invalid index.")
        btn = buttons[index]

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        await ctx.send(f"Current label: {btn.get('label')}\nNew label (or 'skip'):")
        try:
            m = await self.bot.wait_for("message", check=check, timeout=60)
            if m.content.strip().lower() != 'skip':
                btn['label'] = m.content.strip()
        except Exception:
            pass

        await ctx.send(f"Current command: {btn.get('command')}\nNew command (or 'skip'):")
        try:
            m = await self.bot.wait_for("message", check=check, timeout=120)
            if m.content.strip().lower() != 'skip':
                btn['command'] = m.content.strip()
        except Exception:
            pass

        await ctx.send(f"Current style: {btn.get('style')}\nNew style (or 'skip'):")
        try:
            m = await self.bot.wait_for("message", check=check, timeout=30)
            if m.content.strip().lower() != 'skip':
                btn['style'] = m.content.strip().lower()
        except Exception:
            pass

        panels[panel_id]["buttons"][index] = btn
        await self.config.guild(ctx.guild).panels.set(panels)
        await ctx.send("Button updated.")
        # Update message
        panel = panels[panel_id]
        if panel.get("channel_id") and panel.get("message_id"):
            try:
                ch = ctx.guild.get_channel(panel["channel_id"])
                if ch:
                    view = PanelButtonView(self, panel_id, panels[panel_id]["buttons"])
                    msg = await ch.fetch_message(panel["message_id"])
                    await msg.edit(view=view)
                    self.bot.add_view(view, message_id=msg.id)
            except Exception:
                pass

    async def invoke_command(self, interaction: discord.Interaction, command: str):
        try:
            msg = interaction.message
            if not msg:
                return await interaction.followup.send("Cannot invoke command.", ephemeral=True)
            # Determine prefix
            prefix = "!"
            try:
                cp = self.bot.command_prefix
                if callable(cp):
                    try:
                        cp = cp(self.bot, msg)
                    except Exception:
                        cp = "!"
                if isinstance(cp, (list, tuple, set)):
                    for p in cp:
                        if p and isinstance(p, str):
                            prefix = p
                            break
                elif isinstance(cp, str):
                    prefix = cp
            except Exception:
                prefix = "!"
            # Ensure command doesn't already have prefix
            cmd_clean = command.strip()
            if cmd_clean.startswith(prefix):
                cmd_clean = cmd_clean[len(prefix):].strip()
            ctx = await self.bot.get_context(msg)
            # Build new message content
            new_content = f"{prefix}{cmd_clean}"
            # Create a fake message object by copying
            class FakeMessage:
                pass
            # Easier: just set content on a copy approach - use the original message's channel/guild
            ctx.message.content = new_content
            ctx2 = await self.bot.get_context(ctx.message)
            ctx2.author = interaction.user
            ctx2.channel = interaction.channel
            ctx2.guild = interaction.guild
            if ctx2.valid and ctx2.command:
                await self.bot.invoke(ctx2)
                return
            # Try to process directly
            await self.bot.process_commands(ctx2.message)
            return
        except Exception as e:
            try:
                await interaction.followup.send(f"Error: {e}", ephemeral=True)
            except Exception:
                pass
