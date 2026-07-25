import asyncio
import logging 
import logging.handlers
from dotenv import load_dotenv
import os

from typing import List, Optional

import discord
from discord.ext import commands
from aiohttp import ClientSession

import server

load_dotenv()
token = os.getenv('TOKEN')

class CustomBot(commands.Bot):
    def __init__(
            self,
            *args,
            initial_extentions: List[str],
            web_client: ClientSession,
            testing_guild_id: Optional[int] = None,
            **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.initial_extentions = initial_extentions
        self.web_client = web_client
        self.testitng_guild_id = testing_guild_id

    async def setup_hook(self) -> None:
        for extentions in self.initial_extentions:
            await self.load_extension(extentions)
        if self.testitng_guild_id:
            guild = discord.Object(self.testitng_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

async def main():
    logger = logging.getLogger('discord')
    logger.setLevel(logging.INFO)

    handler = logging.handlers.RotatingFileHandler(
        filename='discord.log',
        encoding='utf-8',
        maxBytes=32*1024*1024,
        backupCount=5
    )

    dt_fmt = '%Y-%m-%d %H:%M:%S'    
    formatter = logging.Formatter('[{asctime}] [{levelname:<8}] {name}: {message}', dt_fmt, style='{')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    async with ClientSession() as our_client:
        exts = ['cogs.chat']
        intents = discord.Intents.default()
        intents.message_content = True
        async with CustomBot(
            commands.when_mentioned_or("!"),
            web_client=our_client,
            initial_extentions=exts,
            intents=intents,
        ) as bot:
            await bot.start(token)

server.keep_alive()
asyncio.run(main())