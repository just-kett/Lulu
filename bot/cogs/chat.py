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
        # Fallback tự động nhận diện nếu chưa cấu hình COMMANDER_ID trong .env
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

        # Bảng tra cứu chứa 100% emote của server
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

    async def _get_channel_recent_context(self, message: discord.Message, limit: int = 4) -> str:
        """Lấy các tin nhắn vừa diễn ra trong kênh ngay trước khi bot được gọi để nắm bắt tình hình phòng chat."""
        if not message.guild or not hasattr(message.channel, "history"):
            return ""

        recent_chats = []
        try:
            async for prev_msg in message.channel.history(limit=limit, before=message):
                if prev_msg.author == self.bot.user:
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
        """Tự động phát hiện khi người dùng dặn bot ghi nhớ thông tin về họ."""
        patterns = [
            r'(?:hãy\s+)?nhớ\s+(?:rằng|là|cho\s+anh|cho\s+em|giúp\s+anh|giúp\s+em)?\s*[:,-]?\s*(.+)',
            r'ghi\s+nhớ\s*[:,-]?\s*(.+)',
            r'remember\s+(?:that)?\s*[:,-]?\s*(.+)',
        ]
        for pat in patterns:
            m = re.search(pat, prompt, re.IGNORECASE)
            if m:
                fact = m.group(1).strip()
                if len(fact) >= 3:
                    return fact
        return None

    def _sanitize_history_for_llm(self, history: list[dict]) -> list[dict]:
        """
        Chuẩn hóa lịch sử chat trước khi gửi cho AI:
        Đổi <:name:id> về lại dạng thuần :name: để AI không học theo cú pháp kèm ID sai,
        và xóa các tag [REACT:] cũ để ngữ cảnh không bị nhiễu.
        """
        sanitized = []
        for item in history:
            text = item.get("content", "")
            # Đổi <:name:id> hoặc <a:name:id> về :name:
            text = re.sub(r'<a?:([a-zA-Z0-9_]+):\d*>', r':\1:', text)
            # Xóa các tag [REACT: ...] cũ
            text = re.sub(r'\[REACT:\s*[^\]]+\]', '', text).strip()
            sanitized.append({"role": item.get("role", "user"), "content": text})
        return sanitized

    def _format_emojis_in_text(self, text: str, emoji_mention_map: dict[str, str]) -> str:
        """Thay thế toàn diện :name:, <:name:id_sai>, <:name:> bằng ID chuẩn từ server."""
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
        """Xử lý mô hình Hybrid: kết hợp Hồ sơ Dài hạn (User Memo) và Ngữ cảnh Ngắn hạn (Phòng chat)."""
        session_id = self._get_session_id(message)
        author_id = str(message.author.id)
        author_name = message.author.display_name
        is_cmd = self._is_commander(message.author)
        formatted_prompt = self._format_user_prompt(message.author, prompt)

        # Phát hiện xem tin nhắn có nhắc tới thành viên khác trong server không
        target_member = None
        if message.guild and message.mentions:
            other_mentions = [m for m in message.mentions if m != self.bot.user and m != message.author]
            if other_mentions:
                target_member = other_mentions[0]

        # 1. Quản lý Hồ sơ Cá nhân Dài hạn (User Memo) của người nói
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

        # Tự động phát hiện ý định ghi nhớ thông tin mới
        new_fact = self._extract_memory_intent(prompt)
        if new_fact:
            if is_cmd and target_member:
                clean_target_fact = re.sub(rf'<@!?{target_member.id}>', '', new_fact).strip()
                if clean_target_fact:
                    t_role = "commander" if self._is_commander(target_member) else "member"
                    await asyncio.to_thread(
                        db.append_user_note,
                        str(target_member.id),
                        clean_target_fact,
                        target_member.display_name,
                        t_role,
                    )
                    print(f"[LuLu Memory] Chỉ huy đã cập nhật ghi nhớ cho {target_member.display_name}: {clean_target_fact}")
            else:
                await asyncio.to_thread(db.append_user_note, author_id, new_fact, author_name, role_type)
                user_notes = f"{user_notes} | {new_fact}" if user_notes else new_fact
                print(f"[LuLu Memory] Đã ghi nhớ thêm cho {author_name}: {new_fact}")

        # 2. Lấy bối cảnh các câu chat trôi nổi vừa diễn ra trong kênh (nếu có)
        channel_recent_text = await self._get_channel_recent_context(message, limit=4)

        # 3. Lấy emote của server
        emoji_guild = self._get_emoji_source_guild(message.guild)
        emoji_instruction, emoji_lookup, emoji_mention_map = self._get_server_emojis_context(emoji_guild)

        # 4. Ghép toàn bộ chỉ dẫn Hybrid cho AI
        hybrid_instruction_parts = []
        if emoji_instruction:
            hybrid_instruction_parts.append(emoji_instruction)

        profile_instruction = (
            f"HỒ SƠ NGƯỜI ĐANG NÓI CHUYỆN:\n"
            f"- Tên: {author_name}\n"
            f"- Vai trò: {'Chỉ huy Kett (Người bạn thầm thích)' if is_cmd else 'Thành viên đồng đội'}\n"
            f"- Những điều bạn đã ghi nhớ về người này: {user_notes}\n\n"
            f"QUY TẮC ỨNG XỬ:\n"
            f"- Với Chỉ huy Kett: Luôn dịu dàng, quan tâm đặc biệt, thỉnh thoảng ngại ngùng và xưng hô 'Chỉ huy'.\n"
            f"- Với thành viên khác: Lịch sự, thân thiện, gọi họ bằng tên hiển thị và hỗ trợ tận tình."
        )
        hybrid_instruction_parts.append(profile_instruction)

        # Nếu có đề cập đến thành viên khác trong câu nói, cung cấp hồ sơ thành viên đó cho AI
        if target_member:
            target_mem = await asyncio.to_thread(db.get_user_memory, str(target_member.id))
            target_notes = (target_mem.get("notes") or "Chưa có ghi chép cụ thể") if target_mem else "Chưa có ghi chép cụ thể"
            target_role_str = "Chỉ huy" if self._is_commander(target_member) else "Thành viên đồng đội"
            target_instruction = (
                f"HỒ SƠ THÀNH VIÊN ĐƯỢC NHẮC TỚI ({target_member.display_name}):\n"
                f"- Tên: {target_member.display_name}\n"
                f"- Vai trò: {target_role_str}\n"
                f"- Những điều bạn đã ghi nhớ về người này: {target_notes}\n"
                f"*(Nếu Chỉ huy dặn nhớ điều gì về người này, hãy xác nhận dịu dàng rằng bạn đã ghi nhận vào hồ sơ của họ)*"
            )
            hybrid_instruction_parts.append(target_instruction)

        if channel_recent_text:
            hybrid_instruction_parts.append(channel_recent_text)

        full_extra_instruction = "\n\n" + ("\n\n---\n\n".join(hybrid_instruction_parts))

        print(f"[LuLu Debug] Session: {session_id} | Người nói: {author_name} ({'Chỉ huy' if is_cmd else 'Thành viên'}) | Emote: {len(emoji_mention_map)}")

        # 5. Lưu tin nhắn người dùng vào session ngắn hạn
        await asyncio.to_thread(
            db.save_message,
            session_id,
            "user",
            formatted_prompt,
            author_id,
            author_name,
        )
        history = await asyncio.to_thread(db.load_history, session_id)
        clean_history = self._sanitize_history_for_llm(history)

        try:
            async with message.channel.typing():
                raw_answer = await asyncio.to_thread(
                    ask_llm,
                    prompt=prompt,
                    history=clean_history,
                    extra_instruction=full_extra_instruction,
                )
        except Exception:
            raw_answer = await asyncio.to_thread(
                ask_llm,
                prompt=prompt,
                history=clean_history,
                extra_instruction=full_extra_instruction,
            )

        print(f"[LuLu Debug] Raw answer từ AI: {repr(raw_answer)}")

        # 6. Trích xuất tag [REACT: emote_name] nếu có
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

        # 7. Format custom emote trong văn bản
        formatted_answer = self._format_emojis_in_text(raw_answer, emoji_mention_map)

        print(f"[LuLu Debug] Formatted answer gửi Discord: {repr(formatted_answer)}")
        if react_emoji:
            print(f"[LuLu Debug] Reaction sẽ thả: {repr(react_emoji)}")

        # 8. Lưu câu trả lời của bot vào session ngắn hạn
        bot_user = self.bot.user
        bot_id = str(bot_user.id) if bot_user else "bot"
        bot_name = bot_user.display_name if bot_user else "LuLu"
        await asyncio.to_thread(
            db.save_message,
            session_id,
            "remi",
            formatted_answer,
            bot_id,
            bot_name,
        )

        # 9. Gửi tin nhắn trả lời
        await message.reply(formatted_answer)

        # 10. Thả reaction vào tin nhắn người dùng (nếu có emote phù hợp)
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

    def _parse_member_and_text(self, ctx, content: str) -> tuple[discord.Member | discord.User, str]:
        """
        Phân tích đối tượng thành viên và nội dung đi kèm từ câu lệnh.
        - Nếu có @mention thành viên (khác bot): chọn thành viên đó.
        - Nếu từ đầu tiên là snowflake ID và tìm thấy trong guild: chọn thành viên đó.
        - Mặc định: đối tượng là chính người gọi lệnh (ctx.author).
        """
        content = content.strip()
        # 1. Kiểm tra mentions trong tin nhắn (loại trừ bot)
        if ctx.message.mentions:
            valid_mentions = [m for m in ctx.message.mentions if m != self.bot.user]
            if valid_mentions:
                target = valid_mentions[0]
                clean_text = re.sub(rf'<@!?{target.id}>', '', content).strip()
                return target, clean_text

        # 2. Kiểm tra nếu từ đầu tiên là user ID bằng số
        tokens = content.split(None, 1)
        if tokens and tokens[0].isdigit() and len(tokens[0]) >= 15:
            uid = int(tokens[0])
            member = ctx.guild.get_member(uid) if ctx.guild else None
            if member:
                return member, tokens[1].strip() if len(tokens) > 1 else ""

        # 3. Mặc định là chính author
        return ctx.author, content

    @commands.command(name='ask')
    async def ask(self, ctx, *, content: str):
        await self._respond(ctx.message, content)

    @commands.command(name='memo', aliases=['profile', 'memory'])
    async def show_memo(self, ctx, *, content: str = ""):
        """
        Xem hồ sơ và những điều Lulu đang ghi nhớ.
        - Xem của mình: !memo
        - Xem của thành viên khác: !memo @Member
        """
        target, _ = self._parse_member_and_text(ctx, content)
        target_id = str(target.id)
        target_name = target.display_name
        is_target_cmd = self._is_commander(target)

        user_mem = await asyncio.to_thread(db.get_user_memory, target_id)

        if not user_mem or not user_mem.get("notes"):
            notes = "Chưa có ghi nhớ đặc biệt nào."
        else:
            notes = user_mem.get("notes")

        role_str = "Chỉ huy (Người quan trọng nhất)" if is_target_cmd else "Thành viên / Đồng đội"

        if target.id == ctx.author.id:
            header = f"📋 **Hồ sơ ký ức của Lulu về {target_name}:**"
            footer = "*(Bạn có thể dặn Lulu nhớ thêm điều gì đó bằng cách nói 'Lulu nhớ là...' hoặc dùng lệnh `!remember <nội dung>`)*"
        else:
            header = f"📋 **Hồ sơ ký ức của Lulu về thành viên {target_name}:**"
            footer = "*(Chỉ huy có thể cập nhật ghi nhớ bằng lệnh `!remember @Member <nội dung>` hoặc `!setmemo @Member <nội dung>`)*"

        reply_msg = (
            f"{header}\n"
            f"• **Vai trò:** {role_str}\n"
            f"• **Những điều đã ghi nhớ:** {notes}\n\n"
            f"{footer}"
        )
        await ctx.reply(reply_msg)

    @commands.command(name='remember', aliases=['nhớ', 'ghinho'])
    async def remember_fact(self, ctx, *, content: str = ""):
        """
        Chủ động dặn Lulu ghi nhớ thêm thông tin.
        - Tự dặn cho bản thân: !remember <nội dung>
        - Chỉ huy dặn cho thành viên khác: !remember @Member <nội dung>
        """
        target, fact = self._parse_member_and_text(ctx, content)
        fact = fact.strip()

        if not fact:
            if target == ctx.author:
                await ctx.reply("Bạn muốn Lulu nhớ điều gì về bạn nào? (Ví dụ: `!remember tôi thích uống trà xanh`)")
            else:
                await ctx.reply(f"Chỉ huy muốn Lulu ghi nhớ điều gì về **{target.display_name}** ạ? (Ví dụ: `!remember @{target.display_name} là tay súng cừ khôi`)")
            return

        is_cmd = self._is_commander(ctx.author)

        # Kiểm tra quyền: Chỉ có Chỉ huy mới được dặn nhớ cho người khác
        if target.id != ctx.author.id and not is_cmd:
            await ctx.reply(
                f"Dạ {ctx.author.display_name}, chỉ có Chỉ huy Kett mới có quyền thiết lập và ghi chú hồ sơ cho các thành viên khác trong đội thôi ạ."
            )
            return

        target_id = str(target.id)
        target_name = target.display_name
        target_role = "commander" if self._is_commander(target) else "member"

        await asyncio.to_thread(db.append_user_note, target_id, fact, target_name, target_role)

        if target.id == ctx.author.id:
            if is_cmd:
                await ctx.reply(f"Dạ Chỉ huy, em đã ghi nhớ về Chỉ huy: *'{fact}'* rồi ạ! :lucia_smile:")
            else:
                await ctx.reply(f"Mình đã ghi nhớ điều này về bạn rồi nhé: *'{fact}'*")
        else:
            await ctx.reply(
                f"Dạ Chỉ huy, em đã ghi nhận thông tin vào hồ sơ của **{target_name}**: *'{fact}'* rồi ạ! :lucia_smile:"
            )

    @commands.command(name='setmemo', aliases=['datghinho', 'ghide'])
    async def set_memo(self, ctx, *, content: str = ""):
        """
        Thiết lập hoặc ghi đè toàn bộ ghi nhớ (chỉ huy có quyền với mọi thành viên).
        - Đặt cho chính mình: !setmemo <nội dung>
        - Chỉ huy đặt cho người khác: !setmemo @Member <nội dung>
        """
        target, fact = self._parse_member_and_text(ctx, content)
        fact = fact.strip()

        if not fact:
            await ctx.reply("Vui lòng nhập nội dung ghi nhớ! Cú pháp: `!setmemo [@Member] <nội dung>`")
            return

        is_cmd = self._is_commander(ctx.author)

        if target.id != ctx.author.id and not is_cmd:
            await ctx.reply(
                f"Dạ {ctx.author.display_name}, chỉ có Chỉ huy Kett mới có quyền thay đổi hồ sơ của các thành viên khác trong đội thôi ạ."
            )
            return

        target_id = str(target.id)
        target_name = target.display_name
        target_role = "commander" if self._is_commander(target) else "member"

        await asyncio.to_thread(db.set_user_notes, target_id, target_name, fact, target_role)

        if target.id == ctx.author.id:
            if is_cmd:
                await ctx.reply(f"Dạ Chỉ huy, em đã cập nhật lại toàn bộ hồ sơ của Chỉ huy: *'{fact}'* rồi ạ! :lucia_smile:")
            else:
                await ctx.reply(f"Đã cập nhật lại toàn bộ hồ sơ của bạn: *'{fact}'*")
        else:
            await ctx.reply(
                f"Dạ Chỉ huy, em đã cập nhật lại hồ sơ của **{target_name}**: *'{fact}'* rồi ạ! :lucia_smile:"
            )

    @commands.command(name='clearmemo', aliases=['resetmemo', 'xoaghinho'])
    async def clear_memo(self, ctx, *, content: str = ""):
        """
        Xóa hồ sơ ghi nhớ dài hạn (User Memo).
        - Tự xóa của mình: !clearmemo
        - Chỉ huy xóa của thành viên khác: !clearmemo @Member
        """
        target, _ = self._parse_member_and_text(ctx, content)
        is_cmd = self._is_commander(ctx.author)

        if target.id != ctx.author.id and not is_cmd:
            await ctx.reply(
                f"Dạ {ctx.author.display_name}, chỉ có Chỉ huy Kett mới có quyền xóa hồ sơ của thành viên khác thôi ạ."
            )
            return

        target_id = str(target.id)
        target_name = target.display_name

        await asyncio.to_thread(db.reset_user_memory, target_id)

        if target.id == ctx.author.id:
            if is_cmd:
                await ctx.reply("Dạ Chỉ huy, hồ sơ ký ức của Chỉ huy đã được làm mới hoàn toàn rồi ạ!")
            else:
                await ctx.reply(f"Hồ sơ ghi nhớ về bạn ({target_name}) đã được xóa khỏi hệ thống.")
        else:
            await ctx.reply(
                f"Dạ Chỉ huy, em đã xóa sạch toàn bộ hồ sơ ghi nhớ của **{target_name}** theo lệnh của Chỉ huy rồi ạ."
            )

    @commands.command(name='clear', aliases=['cleardata', 'reset'])
    async def clear_data(self, ctx):
        session_id = self._get_session_id(ctx.message)
        is_cmd = self._is_commander(ctx.author)

        if is_cmd:
            # Chỉ huy reset làm mới toàn bộ phòng chat / DM
            await asyncio.to_thread(db.clear_history, session_id)
            await ctx.reply("Em sẽ không quên chỉ huy đâu... Lịch sử trò chuyện của kênh này đã được làm mới!")
        else:
            # Thành viên khác chỉ xóa các tin nhắn do chính họ gửi
            await asyncio.to_thread(db.clear_history, session_id, str(ctx.author.id))
            await ctx.reply(f"Dạ {ctx.author.display_name}, các tin nhắn của bạn đã được xóa khỏi dữ liệu của kênh này.")


async def setup(bot):
    await bot.add_cog(Chat(bot))
