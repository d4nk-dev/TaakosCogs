"""CommandButtons cog - Sticky command button panels with modal support."""
import asyncio
import logging
from typing import Any, Dict, List

import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

UNIQUE_ID = 0xCB5E9004
log = logging.getLogger("red.commandbuttons")

# Also add alternative invoke for better compatibility

    async def cog_command_error(self, ctx, error):
        pass
