import asyncio
import os
import re
import discord
from discord.ext import commands
from dotenv import load_dotenv

from data import db
from gemini.serv import ask_llm

load_dotenv()
llm_token = os.getenv('LLM')
raw_emoji_guild = os.getenv('EMOJI_GUILD_ID', '').strip()
EMOJI_GUILD_ID = int(raw_emoji_guild) if raw_emoji_guild.isdigit() else None


class Chat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.emoji_guild_id = EMOJI_GUILD_ID

    async def cog_load(self):
        await asyncio.to_thread(db.init_db)

    def _get_emoji_source_guild(self, current_guild: discord.Guild | None) -> discord.Guild | None:
        """Xác định server chứa emote: ưu tiên server riêng chuyên dụng nếu đã cấu hình."""
        if self.emoji_guild_id:
            dedicated_guild = self.bot.get_guild(self.emoji_guild_id)
            if dedicated_guild:
                return dedicated_guild
        return current_guild

    def _get_server_emojis_context(self, guild: discord.Guild | None):
        """Lấy danh sách emote của server nguồn để cung cấp ngữ cảnh cho AI."""
        if not guild or not guild.emojis:
            return None, {}, {}

        usable = [e for e in guild.emojis if e.is_usable()]
        if not usable:
            return None, {}, {}

        usable = usable[:50]
        emoji_lookup = {e.name.lower(): e for e in usable}
        emoji_mention_map = {e.name.lower(): str(e) for e in usable}

        names_str = ", ".join(f":{e.name}:" for e in usable)
        instruction = (
            f"Danh sách emote tùy chỉnh của server:\n{names_str}\n"
            f"- Thả reaction vào tin nhắn: Bạn có thể chọn 1 emote hợp nhất với cảm xúc của bạn để thả reaction vào tin nhắn người dùng bằng cách thêm tag `[REACT: tên_emote]` ở cuối câu trả lời (ví dụ: `[REACT: {usable[0].name}]`). Nếu không có emote nào hợp thì không cần thêm tag này.\n"
            f"- Dùng emote khi trả lời: Bạn có thể chèn emote vào lời thoại bằng cách viết `:tên_emote:`."
        )
        return instruction, emoji_lookup, emoji_mention_map

    async def _respond(self, message: discord.Message, prompt: str):
        """Xử lý trả lời tin nhắn kết hợp thả reaction và dùng emote server."""
        user_id = str(message.author.id)
        emoji_guild = self._get_emoji_source_guild(message.guild)
        instruction, emoji_lookup, emoji_mention_map = self._get_server_emojis_context(emoji_guild)

        await asyncio.to_thread(db.save_message, user_id, "user", prompt)
        history = await asyncio.to_thread(db.load_history, user_id)

        try:
            async with message.channel.typing():
                raw_answer = await asyncio.to_thread(
                    ask_llm,
                    prompt=prompt,
                    history=history,
                    extra_instruction=instruction
                )
        except Exception:
            raw_answer = await asyncio.to_thread(
                ask_llm,
                prompt=prompt,
                history=history,
                extra_instruction=instruction
            )

        react_emoji = None
        react_match = re.search(r'\[REACT:\s*([^\]]+)\]', raw_answer, re.IGNORECASE)
        if react_match:
            raw_react = react_match.group(1).strip()
            raw_answer = re.sub(r'\[REACT:\s*[^\]]+\]', '', raw_answer).strip()

            clean_name = raw_react.lower().strip(':')
            if clean_name in emoji_lookup:
                react_emoji = emoji_lookup[clean_name]
            else:
                react_emoji = raw_react

        if emoji_mention_map:
            formatted_answer = re.sub(
                r'(?<!<)(?<!<a):([a-zA-Z0-9_]+):(?!\d+>)',
                lambda m: emoji_mention_map.get(m.group(1).lower(), m.group(0)),
                raw_answer
            )
        else:
            formatted_answer = raw_answer

        await asyncio.to_thread(db.save_message, user_id, "remi", formatted_answer)

        await message.reply(formatted_answer)

        if react_emoji:
            try:
                await message.add_reaction(react_emoji)
            except (discord.DiscordException, TypeError, ValueError):
                pass

    async def _answer(self, user_id: str, prompt: str) -> str:
        """Hàm dự phòng tương thích ngược."""
        await asyncio.to_thread(db.save_message, user_id, "user", prompt)
        history = await asyncio.to_thread(db.load_history, user_id)
        answer = await asyncio.to_thread(ask_llm, prompt=prompt, history=history)
        await asyncio.to_thread(db.save_message, user_id, "remi", answer)
        return answer

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author == self.bot.user:
            return

        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        if "hey lucia" in message.content.lower() or "hey lulu" in message.content.lower():
            await self._respond(message, message.content)

        elif message.reference:
            replied_message = await message.channel.fetch_message(
                message.reference.message_id
            )

            if replied_message.author == self.bot.user:
                await self._respond(message, message.content)

        elif self.bot.user in message.mentions:
            prompt = message.clean_content.replace(f"@{self.bot.user.name}", '').strip()
            await self._respond(message, prompt)

        await self.bot.process_commands(message)

    @commands.command(name='ask')
    async def ask(self, ctx, *, content: str):
        await self._respond(ctx.message, content)

    @commands.command(name='clear', aliases=['cleardata', 'reset'])
    async def clear_data(self, ctx):
        await asyncio.to_thread(db.clear_history, str(ctx.author.id))
        await ctx.reply("Em sẽ không quên chỉ huy đâu...")


async def setup(bot):
    await bot.add_cog(Chat(bot))
