from discord.ext import commands
from dotenv import load_dotenv
import os
import asyncio

from gemini.serv import ask_llm
from data import db

load_dotenv()
llm_token = os.getenv('LLM')

class Chat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await asyncio.to_thread(db.init_db)

    async def _answer(self, user_id: str, prompt: str) -> str:
        await asyncio.to_thread(db.save_message, user_id, "user", prompt)
        history = await asyncio.to_thread(db.load_history, user_id)
        answer = await asyncio.to_thread(ask_llm, prompt=prompt, history=history)
        await asyncio.to_thread(db.save_message, user_id, "remi", answer)
        return answer

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user:
            return

        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        user_id = str(message.author.id)

        if "hey lucia" in message.content.lower() or "hey lulu" in message.content.lower():
            answer = await self._answer(user_id, message.content)
            await message.reply(answer)

        elif message.reference:
            replied_message = await message.channel.fetch_message(
                message.reference.message_id
            )

            if replied_message.author == self.bot.user:
                answer = await self._answer(user_id, message.content)
                await message.reply(answer)  

        elif self.bot.user in message.mentions:
            prompt = message.clean_content.replace(f"@{self.bot.user.name}", '').strip()
            answer = await self._answer(user_id, prompt)
            await message.reply(answer)

        await self.bot.process_commands(message)

    @commands.command(name='ask')
    async def ask(self, ctx, *, content: str):
        answer = await self._answer(str(ctx.author.id), content)
        await ctx.reply(answer)
    
async def setup(bot):
    await bot.add_cog(Chat(bot))