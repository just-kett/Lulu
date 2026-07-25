from discord.ext import commands
from dotenv import load_dotenv
import os
import asyncio

from gemini.serv import ask_llm

load_dotenv()
llm_token = os.getenv('LLM')

class Chat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user:
            return

        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        if "hey remi" in message.content.lower() or "remi ơi" in message.content.lower():
            prompt = message.content
            answer = await asyncio.to_thread(
                ask_llm,
                prompt=prompt
            )
            await message.reply(answer)

        elif message.reference:
            replied_message = await message.channel.fetch_message(
                message.reference.message_id
            )

            if replied_message.author == self.bot.user:
                prompt = message.content
                answer = await asyncio.to_thread(
                    ask_llm,
                    prompt=prompt
                )
                await message.reply(answer)  

        elif self.bot.user in message.mentions:
            prompt = message.clean_content.replace(f"@{self.bot.user.name}", '').strip()
            answer = await asyncio.to_thread(
                ask_llm,
                prompt=prompt
            )
            await message.reply(answer)

        await self.bot.process_commands(message)

    @commands.command(name='ask')
    async def ask(self, ctx, *, content: str):
        prompt = content
        answer = await asyncio.to_thread(
            ask_llm,
            prompt=prompt
        )
        await ctx.reply(answer)
    
async def setup(bot):
    await bot.add_cog(Chat(bot))