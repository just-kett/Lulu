import asyncio
import difflib
import os
import re
import discord
from discord.ext import commands
from dotenv import load_dotenv

from data import db
from gemini.serv import ask_llm

load_dotenv()
llm_token = os.getenv('LLM')
raw_emoji_guild = os.getenv('EMOJI_GUILD_ID', '').strip().strip('"').strip("'")
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
            print(f"[LuLu Warning] Không tìm thấy server emote có ID {self.emoji_guild_id} trong cache của bot!")
        return current_guild

    def _get_server_emojis_context(self, guild: discord.Guild | None):
        """Lấy danh sách emote của server nguồn để cung cấp ngữ cảnh cho AI."""
        if not guild or not guild.emojis:
            return None, {}, {}
        
        all_emojis = [e for e in guild.emojis if getattr(e, 'available', True)]
        if not all_emojis:
            return None, {}, {}

        emoji_lookup = {e.name.lower(): e for e in all_emojis}
        emoji_mention_map = {e.name.lower(): str(e) for e in all_emojis}

        names_str = ", ".join(f":{e.name}:" for e in all_emojis)

        instruction = (
            f"DANH SÁCH EMOTE CỦA SERVER ({len(all_emojis)} emote):\n{names_str}\n\n"
            "QUY TẮC BẮT BUỘC KHI DÙNG EMOTE:\n"
            "- Dùng emote trong lời nói: Viết `:tên_emote:` ngay trong câu (ví dụ: `:lucia_surprise:`).\n"
            "- Thả reaction vào tin nhắn: Đặt tag `[REACT: tên_emote]` ở cuối câu (ví dụ: `[REACT: {all_emojis[0].name}]`).\n"
            "- Bạn CHỈ ĐƯỢC CHỌN tên emote có trong danh sách trên! TUYỆT ĐỐI KHÔNG TỰ BỊA RA TÊN EMOTE KHÔNG CÓ TRONG DANH SÁCH."
        )
        return instruction, emoji_lookup, emoji_mention_map

    def _sanitize_history_for_llm(self, history: list[dict]) -> list[dict]:
        """
        Chuẩn hóa lịch sử chat trước khi gửi cho AI:
        Đổi <:name:id> về lại dạng thuần :name: để AI không học theo cú pháp kèm ID sai,
        và xóa các tag [REACT:] cũ để ngữ cảnh không bị nhiễu.
        """
        sanitized = []
        for item in history:
            text = item.get("content", "")
            text = re.sub(r'<a?:([a-zA-Z0-9_]+):\d*>', r':\1:', text)
            text = re.sub(r'\[REACT:\s*[^\]]+\]', '', text).strip()
            sanitized.append({"role": item.get("role", "user"), "content": text})
        return sanitized

    def _format_emojis_in_text(self, text: str, emoji_mention_map: dict[str, str]) -> str:
        """
        Thay thế toàn diện:
        - Bắt cả dạng thuần :name:
        - Bắt cả dạng <:name:id_sai> hoặc <:name:> do AI tự 'bắt chước' cú pháp từ lịch sử
        và thay bằng mã emote chuẩn với ID thật từ server.
        """
        if not emoji_mention_map:
            return text

        all_names = list(emoji_mention_map.keys())

        def replace_match(match):
            raw_name = (match.group(1) or match.group(2) or "").lower()

            if raw_name in emoji_mention_map:
                return emoji_mention_map[raw_name]

            close = difflib.get_close_matches(raw_name, all_names, n=1, cutoff=0.6)
            if close:
                return emoji_mention_map[close[0]]

            if raw_name.startswith(('lucia_', 'lulu_')):
                return ""

            return match.group(0)

        pattern = r'<a?:([a-zA-Z0-9_]+):\d*>?|(?<!<)(?<!<a):([a-zA-Z0-9_]+):(?!\d*>)'
        formatted = re.sub(pattern, replace_match, text)
        formatted = re.sub(r' +', ' ', formatted).strip()
        return formatted

    async def _respond(self, message: discord.Message, prompt: str):
        """Xử lý trả lời tin nhắn kết hợp thả reaction và dùng emote server."""
        user_id = str(message.author.id)
        emoji_guild = self._get_emoji_source_guild(message.guild)
        instruction, emoji_lookup, emoji_mention_map = self._get_server_emojis_context(emoji_guild)

        print(f"[LuLu Debug] User: {user_id} | Guild emote: {emoji_guild} | Số emote nạp: {len(emoji_mention_map)}")

        await asyncio.to_thread(db.save_message, user_id, "user", prompt)
        history = await asyncio.to_thread(db.load_history, user_id)
        clean_history = self._sanitize_history_for_llm(history)

        try:
            async with message.channel.typing():
                raw_answer = await asyncio.to_thread(
                    ask_llm,
                    prompt=prompt,
                    history=clean_history,
                    extra_instruction=instruction
                )
        except Exception:
            raw_answer = await asyncio.to_thread(
                ask_llm,
                prompt=prompt,
                history=clean_history,
                extra_instruction=instruction
            )

        print(f"[LuLu Debug] Raw answer từ AI: {repr(raw_answer)}")

        react_emoji = None
        react_match = re.search(r'\[REACT:\s*([^\]]+)\]', raw_answer, re.IGNORECASE)
        if react_match:
            raw_react = react_match.group(1).strip()
            raw_answer = re.sub(r'\[REACT:\s*[^\]]+\]', '', raw_answer).strip()

            clean_name = raw_react.lower().strip(':')
            if clean_name in emoji_lookup:
                react_emoji = emoji_lookup[clean_name]
            else:
                close = difflib.get_close_matches(clean_name, list(emoji_lookup.keys()), n=1, cutoff=0.6)
                if close:
                    react_emoji = emoji_lookup[close[0]]
                else:
                    react_emoji = raw_react

        formatted_answer = self._format_emojis_in_text(raw_answer, emoji_mention_map)

        print(f"[LuLu Debug] Formatted answer gửi Discord: {repr(formatted_answer)}")
        if react_emoji:
            print(f"[LuLu Debug] Reaction sẽ thả: {repr(react_emoji)}")

        await asyncio.to_thread(db.save_message, user_id, "remi", formatted_answer)

        await message.reply(formatted_answer)

        if react_emoji:
            try:
                await message.add_reaction(react_emoji)
            except (discord.DiscordException, TypeError, ValueError) as e:
                print(f"[LuLu Warning] Lỗi khi thả reaction: {e}")

    async def _answer(self, user_id: str, prompt: str) -> str:
        """Hàm dự phòng tương thích ngược."""
        await asyncio.to_thread(db.save_message, user_id, "user", prompt)
        history = await asyncio.to_thread(db.load_history, user_id)
        clean_history = self._sanitize_history_for_llm(history)
        answer = await asyncio.to_thread(ask_llm, prompt=prompt, history=clean_history)
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
