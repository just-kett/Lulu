import asyncio
import os
import re
import discord
from discord.ext import commands
from dotenv import load_dotenv

from data import db
from gemini.serv import ask_llm, condense_memories

load_dotenv()
llm_token = os.getenv('LLM')

raw_emoji_guild = os.getenv('EMOJI_GUILD_ID', '').strip().strip('"').strip("'")
EMOJI_GUILD_ID = int(raw_emoji_guild) if raw_emoji_guild.isdigit() else None

raw_commander = os.getenv('COMMANDER_ID', '').strip().strip('"').strip("'")
COMMANDER_ID = int(raw_commander) if raw_commander.isdigit() else None


class Chat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.emoji_guild_id = EMOJI_GUILD_ID
        self.commander_id = COMMANDER_ID

    async def cog_load(self):
        await asyncio.to_thread(db.init_db)

    def _is_commander(self, author: discord.User | discord.Member) -> bool:
        """Xác định xem người nói có phải là Chỉ huy Kett hay không (ưu tiên so khớp ID)."""
        if self.commander_id:
            return author.id == self.commander_id
        name_lower = author.name.lower()
        display_lower = author.display_name.lower()
        return "kett" in name_lower or "kett" in display_lower

    def _get_session_id(self, message: discord.Message) -> str:
        """
        Xác định phiên hội thoại ngắn hạn (session_id):
        - Nếu trong server: dùng channel_id để mọi người trong kênh chia sẻ chung dòng hội thoại.
        - Nếu trong DM: dùng author.id để giữ sự riêng tư cho từng người.
        """
        if message.guild:
            return f"channel_{message.channel.id}"
        return f"dm_{message.author.id}"

    def _format_user_prompt(self, author: discord.User | discord.Member, prompt: str) -> str:
        """Gắn nhãn người nói để AI nhận biết ai đang trò chuyện và phân biệt Chỉ huy."""
        if self._is_commander(author):
            speaker_label = f"Chỉ huy - {author.display_name}"
        else:
            speaker_label = f"{author.display_name}"
        return f"[{speaker_label}]: {prompt}"

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
        all_emojis = []
        if guild and guild.emojis:
            all_emojis = [e for e in guild.emojis if getattr(e, 'available', True)]
        elif hasattr(self.bot, 'emojis') and self.bot.emojis:
            all_emojis = [e for e in self.bot.emojis if getattr(e, 'available', True)]

        if not all_emojis:
            return None, {}, {}

        emoji_lookup = {e.name.lower(): e for e in all_emojis}
        emoji_mention_map = {e.name.lower(): str(e) for e in all_emojis}

        names_str = ", ".join(f":{e.name}:" for e in all_emojis)
        instruction = (
            f"Danh sách emote tùy chỉnh của server:\n{names_str}\n"
            f"- Thả reaction vào tin nhắn: Bạn có thể chọn 1 emote hợp nhất với cảm xúc của bạn để thả reaction vào tin nhắn người dùng bằng cách thêm tag `[REACT: tên_emote]` ở cuối câu trả lời (ví dụ: `[REACT: {all_emojis[0].name}]`). Nếu không có emote nào hợp thì không cần thêm tag này.\n"
            f"- Dùng emote khi trả lời: Bạn có thể chèn emote vào lời thoại bằng cách viết `:tên_emote:`."
        )
        return instruction, emoji_lookup, emoji_mention_map

    async def _get_channel_recent_context(self, message: discord.Message, limit: int = 50) -> str:
        """Lấy các tin nhắn vừa diễn ra trong kênh ngay trước khi bot được gọi để nắm bắt tình hình phòng chat."""
        if not message.guild or not hasattr(message.channel, "history"):
            return ""

        recent_chats = []
        try:
            async for prev_msg in message.channel.history(limit=limit, before=message):
                if getattr(prev_msg.author, "bot", False) or prev_msg.author == self.bot.user:
                    continue
                content = prev_msg.clean_content.strip()
                if content and not content.startswith(("!", "/", ".")):
                    recent_chats.append(f"[{prev_msg.author.display_name}]: {content}")
        except Exception:
            return ""

        if not recent_chats:
            return ""

        recent_chats.reverse()
        return "BỐI CẢNH VỪA DIỄN RA TRONG PHÒNG CHAT TRƯỚC ĐÓ:\n" + "\n".join(recent_chats)

    def _extract_memory_intent(self, prompt: str) -> str | None:
        """Tự động phát hiện khi người dùng dặn bot ghi nhớ thông tin."""
        patterns = [
            r'(?:hãy\s+)?nhớ\s+(?:rằng|là|về|cho\s+(?:anh|em|mình|tớ)|giúp\s+(?:anh|em|mình|tớ)|hộ\s+(?:anh|em|mình|tớ)|giùm\s+(?:anh|em|mình|tớ))?\s*[:,-]?\s*(.+)',
            r'ghi\s+nhớ\s*(?:rằng|là|về)?\s*[:,-]?\s*(.+)',
            r'remember\s+(?:that|about)?\s*[:,-]?\s*(.+)',
        ]
        for pat in patterns:
            m = re.search(pat, prompt, re.IGNORECASE)
            if m:
                fact = m.group(1).strip()
                if len(fact) >= 3:
                    return fact
        return None

    def _extract_mention_fact(self, prompt: str, target: discord.Member | discord.User) -> str | None:
        """Trích xuất thông tin về thành viên được mention ngay cả khi không dùng từ 'nhớ là'."""
        clean = prompt.strip()
        if clean.endswith('?') or any(q in clean.lower() for q in ['không?', 'chưa?', 'sao thế?', 'đâu rồi?', 'thế nào?', 'ở đâu?']):
            return None

        names = [rf'<@!?{target.id}>', rf'@{re.escape(target.display_name)}', rf'@{re.escape(target.name)}']
        name_pat = f'(?:{"|".join(names)})'

        pat1 = rf'{name_pat}(?:[\s,vàwith+]+(?:<@!?\d+>|@[\w\d_]+))*\s+((?:là|thích|rất|hay|ghét|sợ|mê|chuyên|khoái|bị|đang|vừa|cũng)\s+.+)'
        m1 = re.search(pat1, clean, re.IGNORECASE)
        if m1:
            fact = m1.group(1).strip()
            if len(fact) >= 3:
                return fact

        pat2 = rf'{name_pat}[,:-]?\s*(?:bạn\s+này|người\s+này|ông\s+này|bà\s+này|anh\s+này|chị\s+này|thằng\s+này|nó)\s+(.+)'
        m2 = re.search(pat2, clean, re.IGNORECASE)
        if m2:
            fact = m2.group(1).strip()
            if len(fact) >= 3:
                return fact

        pat3 = rf'^[^<@]*{name_pat}\s*[:\-=]\s*([^?!\n]+)$'
        m3 = re.search(pat3, clean, re.IGNORECASE)
        if m3:
            candidate = m3.group(1).strip()
            if candidate.lower() not in ['ơi', 'à', 'ê', 'chào', 'hello', 'hi'] and len(candidate) >= 3:
                return candidate

        return None

    async def _condense_user_memory(self, user_id: str, display_name: str, current_notes: str):
        """Tác vụ chạy nền (asynchronous background task) để cô đọng hồ sơ khi quá dài."""
        try:
            condensed = await asyncio.to_thread(condense_memories, current_notes)
            if condensed and condensed != current_notes:
                await asyncio.to_thread(db.set_user_notes, user_id, display_name, condensed)
                print(f"[LuLu Memory] Đã cô đọng hồ sơ {display_name} thành công: {condensed}")
        except Exception as e:
            print(f"[LuLu Memory] Lỗi khi cô đọng hồ sơ cho {display_name}: {e}")

    def _sanitize_history_for_llm(self, history: list[dict]) -> list[dict]:
        """
        Chuẩn hóa lịch sử chat trước khi gửi cho AI:
        Đổi <:name:id> về lại dạng thuần :name: để AI không học theo cú pháp kèm ID sai,
        và xóa các tag [REACT:] cũ để ngữ cảnh không bị nhiễu.
        """
        sanitized = []
        for item in history:
            text = item.get("content", "")
            text = re.sub(r'<a?:([a-zA-Z0-9_]+):[^>]*>', r':\1:', text)
            text = re.sub(r'\[REACT:\s*[^\]]+\]', '', text).strip()
            text = re.sub(r'\[MEMO:\s*[^\]]+\]', '', text).strip()
            sanitized.append({"role": item.get("role", "user"), "content": text})
        return sanitized

    def _format_emojis_in_text(self, text: str, emoji_mention_map: dict[str, str]) -> str:
        """Định dạng custom emote trong văn bản: chuẩn hóa và thay thế :name: -> <:name:id> hoặc <a:name:id>."""
        if not emoji_mention_map:
            return text

        normalized = re.sub(r'<a?:([a-zA-Z0-9_]+):[^>]*>', r':\1:', text)

        return re.sub(
            r'(?<!<)(?<!<a):([a-zA-Z0-9_]+):(?!\d+>)',
            lambda m: emoji_mention_map.get(m.group(1).lower(), m.group(0)),
            normalized,
        )

    async def _respond(self, message: discord.Message, prompt: str):
        """Xử lý mô hình Hybrid: kết hợp Hồ sơ Dài hạn (User Memo) và Ngữ cảnh Ngắn hạn (Phòng chat)."""
        session_id = self._get_session_id(message)
        author_id = str(message.author.id)
        author_name = message.author.display_name
        is_cmd = self._is_commander(message.author)
        formatted_prompt = self._format_user_prompt(message.author, prompt)

        target_members = []
        if message.guild and message.mentions:
            target_members = [m for m in message.mentions if m != self.bot.user and m != message.author][:3]

        user_mem = await asyncio.to_thread(db.get_user_memory, author_id)
        role_type = "commander" if is_cmd else "member"

        if not user_mem:
            default_notes = (
                "Chỉ huy của đội Gray Raven, người mà Lucia thầm yêu thương và tin tưởng tuyệt đối."
                if is_cmd
                else "Thành viên đồng đội trong server."
            )
            await asyncio.to_thread(db.upsert_user_memory, author_id, author_name, role_type, default_notes)
            user_notes = default_notes
        else:
            user_notes = user_mem.get("notes", "")

        saved_target_ids = set()

        if target_members:
            for tm in target_members:
                t_fact = self._extract_mention_fact(prompt, tm)
                if not t_fact:
                    new_fact = self._extract_memory_intent(prompt)
                    if new_fact:
                        clean_t_fact = re.sub(
                            rf'<@!?{tm.id}>|@{re.escape(tm.display_name)}|@{re.escape(tm.name)}',
                            '',
                            new_fact,
                        ).strip()
                        clean_t_fact = re.sub(r'^(?:về|rằng|là|:\s*|-)\s*', '', clean_t_fact, flags=re.IGNORECASE).strip()
                        t_fact = clean_t_fact if len(clean_t_fact) >= 3 else None

                if t_fact:
                    t_role = "commander" if self._is_commander(tm) else "member"
                    added, final_notes = await asyncio.to_thread(
                        db.append_user_note,
                        str(tm.id),
                        t_fact,
                        tm.display_name,
                        t_role,
                    )
                    saved_target_ids.add(tm.id)
                    if added:
                        print(f"[LuLu Memory Auto] {author_name} đã cập nhật ghi nhớ cho {tm.display_name}: {t_fact}")
                        if len(final_notes) > 250 or final_notes.count('|') >= 4:
                            asyncio.create_task(self._condense_user_memory(str(tm.id), tm.display_name, final_notes))
        else:
            new_fact = self._extract_memory_intent(prompt)
            if new_fact:
                added, final_notes = await asyncio.to_thread(db.append_user_note, author_id, new_fact, author_name, role_type)
                user_notes = final_notes
                if added:
                    print(f"[LuLu Memory] Đã ghi nhớ thêm cho {author_name}: {new_fact}")
                    if len(final_notes) > 250 or final_notes.count('|') >= 4:
                        asyncio.create_task(self._condense_user_memory(author_id, author_name, final_notes))

        channel_recent_text = await self._get_channel_recent_context(message, limit=50)

        emoji_guild = self._get_emoji_source_guild(message.guild)
        emoji_instruction, emoji_lookup, emoji_mention_map = self._get_server_emojis_context(emoji_guild)

        hybrid_instruction_parts = []
        if emoji_instruction:
            hybrid_instruction_parts.append(emoji_instruction)

        profile_instruction = (
            f"HỒ SƠ NGƯỜI ĐANG NÓI CHUYỆN:\n"
            f"- Tên: {author_name}\n"
            f"- Vai trò: {'Chỉ huy Kett' if is_cmd else 'Chỉ huy của các đội khác'}\n"
            f"- Những điều bạn đã ghi nhớ về người này: {user_notes}\n\n"
            f"QUY TẮC ỨNG XỬ:\n"
            f"- Với Chỉ huy Kett: Luôn dịu dàng, quan tâm đặc biệt và xưng hô 'Chỉ huy'.\n"
            f"- Với chỉ huy khác: Thân thiện, hòa đồng với các chỉ huy khác và cũng xưng hô 'Chỉ huy'.\n"
            f"- Với chỉ huy có tên hiển thị là Cocoa: Đây là đàn anh đáng kinh của bạn và bạn rất quý anh ấy"
            )
        hybrid_instruction_parts.append(profile_instruction)

        if target_members:
            target_sections = []
            for tm in target_members:
                t_mem = await asyncio.to_thread(db.get_user_memory, str(tm.id))
                t_notes = (t_mem.get("notes") or "Chưa có ghi chép cụ thể") if t_mem else "Chưa có ghi chép cụ thể"
                t_role = "Chỉ huy" if self._is_commander(tm) else "Thành viên đồng đội"
                target_sections.append(
                    f"• {tm.display_name} (ID: {tm.id}, Vai trò: {t_role}): {t_notes}"
                )
            target_list_str = "\n".join(target_sections)
            target_instruction = (
                f"CÁC THÀNH VIÊN ĐƯỢC NHẮC TỚI TRONG TIN NHẮN:\n"
                f"{target_list_str}\n\n"
                f"QUY TẮC:\n"
                f"- Nếu tin nhắn chia sẻ thông tin hoặc đặc điểm mới về bất kỳ ai trong số họ, hãy tự nhiên ghi nhận vào câu trả lời.\n"
                f"- Bạn có thể đặt tag [MEMO: <Tên_hoặc_ID> | tóm tắt ngắn gọn] ở cuối câu để cập nhật cho người đó."
            )
            hybrid_instruction_parts.append(target_instruction)

        if channel_recent_text:
            hybrid_instruction_parts.append(channel_recent_text)

        full_extra_instruction = "\n\n" + ("\n\n---\n\n".join(hybrid_instruction_parts))

        print(f"[LuLu Debug] Session: {session_id} | Người nói: {author_name} ({'Chỉ huy' if is_cmd else 'Thành viên'}) | Emote: {len(emoji_mention_map)}")

        clean_user_prompt = re.sub(r'<a?:([a-zA-Z0-9_]+):[^>]*>', r':\1:', formatted_prompt)
        history = await asyncio.to_thread(db.load_history, session_id)
        clean_history = self._sanitize_history_for_llm(history)
        llm_history = clean_history + [{"role": "user", "content": clean_user_prompt}]

        try:
            async with message.channel.typing():
                raw_answer = await asyncio.to_thread(
                    ask_llm,
                    prompt=clean_user_prompt,
                    history=llm_history,
                    extra_instruction=full_extra_instruction,
                )
        except Exception:
            raw_answer = await asyncio.to_thread(
                ask_llm,
                prompt=clean_user_prompt,
                history=llm_history,
                extra_instruction=full_extra_instruction,
            )

        print(f"[LuLu Debug] Raw answer từ AI: {repr(raw_answer)}")

        memo_matches = re.findall(r'\[MEMO:\s*([^\]]+)\]', raw_answer, re.IGNORECASE)
        if memo_matches:
            raw_answer = re.sub(r'\[MEMO:\s*[^\]]+\]', '', raw_answer).strip()
            for ai_memo in memo_matches:
                ai_memo = ai_memo.strip()
                target_tm = None
                memo_content = ai_memo

                if '|' in ai_memo:
                    target_ident, memo_content = [part.strip() for part in ai_memo.split('|', 1)]
                    for tm in target_members:
                        if target_ident.lower() in [str(tm.id), tm.name.lower(), tm.display_name.lower()]:
                            target_tm = tm
                            break
                elif target_members:
                    target_tm = target_members[0]

                if target_tm and target_tm.id not in saved_target_ids:
                    t_role = "commander" if self._is_commander(target_tm) else "member"
                    added, final_notes = await asyncio.to_thread(
                        db.append_user_note,
                        str(target_tm.id),
                        memo_content,
                        target_tm.display_name,
                        t_role,
                    )
                    saved_target_ids.add(target_tm.id)
                    if added:
                        print(f"[LuLu Memory AI] Đã tự động ghi nhớ từ AI cho {target_tm.display_name}: {memo_content}")
                        if len(final_notes) > 250 or final_notes.count('|') >= 4:
                            asyncio.create_task(self._condense_user_memory(str(target_tm.id), target_tm.display_name, final_notes))

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

        bot_user = self.bot.user
        bot_id = str(bot_user.id) if bot_user else "bot"
        bot_name = bot_user.display_name if bot_user else "LuLu"

        await asyncio.to_thread(
            db.save_message_pair,
            session_id,
            clean_user_prompt,
            author_id,
            author_name,
            raw_answer,
            bot_id,
            bot_name,
        )

        formatted_answer = self._format_emojis_in_text(raw_answer, emoji_mention_map)

        print(f"[LuLu Debug] Formatted answer gửi Discord: {repr(formatted_answer)}")
        if react_emoji:
            print(f"[LuLu Debug] Reaction sẽ thả: {repr(react_emoji)}")

        await message.reply(formatted_answer)

        if react_emoji:
            try:
                await message.add_reaction(react_emoji)
            except (discord.DiscordException, TypeError, ValueError) as e:
                print(f"[LuLu Warning] Lỗi khi thả reaction: {e}")

    async def _answer(self, user_id: str, prompt: str) -> str:
        """Hàm dự phòng tương thích ngược."""
        clean_prompt = re.sub(r'<a?:([a-zA-Z0-9_]+):[^>]*>', r':\1:', prompt)
        history = await asyncio.to_thread(db.load_history, user_id)
        clean_history = self._sanitize_history_for_llm(history)
        llm_history = clean_history + [{"role": "user", "content": clean_prompt}]
        answer = await asyncio.to_thread(ask_llm, prompt=clean_prompt, history=llm_history)
        await asyncio.to_thread(
            db.save_message_pair,
            user_id,
            clean_prompt,
            user_id,
            user_id,
            answer,
        )
        return answer

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author == self.bot.user:
            return

        mod_cog = self.bot.get_cog("Moderation")
        if mod_cog and message.guild and not mod_cog._is_exempt(message.author):
            if await mod_cog.find_violation(message) is not None:
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
    async def clear_data(self, ctx, *, extra: str = ""):
        session_id = self._get_session_id(ctx.message)
        is_cmd = self._is_commander(ctx.author)

        if is_cmd:
            valid_mentions = [m for m in ctx.message.mentions if m != self.bot.user]
            tokens = extra.strip().split()
            if not valid_mentions and tokens and tokens[0].isdigit() and len(tokens[0]) >= 15:
                uid = int(tokens[0])
                m = ctx.guild.get_member(uid) if ctx.guild else None
                if not m:
                    m = self.bot.get_user(uid)
                if m:
                    valid_mentions = [m]
                else:
                    target_id = tokens[0]
                    await asyncio.to_thread(db.clear_history, session_id, target_id)
                    await asyncio.to_thread(db.reset_user_memory, target_id)
                    await ctx.reply(f"Dạ Chỉ huy, toàn bộ tin nhắn và hồ sơ ghi nhớ của ID `{target_id}` đã được xóa sạch theo lệnh rồi ạ!")
                    return

            if valid_mentions:
                for target in valid_mentions:
                    await asyncio.to_thread(db.clear_history, session_id, str(target.id))
                    await asyncio.to_thread(db.reset_user_memory, str(target.id))

                target_names = ", ".join(f"**{m.display_name}**" for m in valid_mentions)
                await ctx.reply(f"Dạ Chỉ huy, toàn bộ tin nhắn và hồ sơ ghi nhớ của {target_names} đã được xóa sạch theo lệnh rồi ạ!")
            else:
                await asyncio.to_thread(db.clear_history, session_id)
                await asyncio.to_thread(db.reset_user_memory, str(ctx.author.id))
                await ctx.reply("Em sẽ không quên chỉ huy đâu... Lịch sử trò chuyện của kênh và hồ sơ ký ức đã được làm mới hoàn toàn!")
        else:
            await asyncio.to_thread(db.clear_history, session_id, str(ctx.author.id))
            await asyncio.to_thread(db.reset_user_memory, str(ctx.author.id))
            await ctx.reply(f"Dạ {ctx.author.display_name}, toàn bộ tin nhắn và hồ sơ ghi nhớ về bạn đã được xóa sạch khỏi hệ thống.")


async def setup(bot):
    await bot.add_cog(Chat(bot))
