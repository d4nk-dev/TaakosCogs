from .commandbuttons import CommandButtons

__red_end_user_data_statement__ = (
    "This cog stores per-guild sticky button panel configuration, message IDs, "
    "channel IDs, and command/button mappings. It does not store message content "
    "except panel text sent directly to Discord."
)


async def setup(bot):
    await bot.add_cog(CommandButtons(bot))
