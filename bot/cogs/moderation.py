import asyncio
from datetime import timedelta
import os
import re
import discord
from discord.ext import commands
from dotenv import load_dotenv

from data import db

load_dotenv()

raw_commander = os.getenv('COMMANDER_ID', '').strip().strip('"').strip("'")
COMMANDER_ID = int(raw_commander) if raw_commander.isdigit() else None

CUSTOM_EMOTE_RE = re.compile(r'<a?:([a-zA-Z0-9_]+):(\d+)>')
URL_RE = re.compile(r'https?://[^\s<>()]+', re.IGNORECASE)
TENOR_VIEW_RE = re.compile(r'^https?://(?:www\.)?tenor\.com/view/(?:[^/?#\s]*-)?(\d+)', re.IGNORECASE)
TENOR_MEDIA_RE = re.compile(r'^https?://(?:media\d*|c)\.tenor\.com/([^/?#\s]+)/', re.IGNORECASE)
GIPHY_PAGE_RE = re.compile(r'^https?://(?:www\.)?giphy\.com/gifs/(?:[^/?#\s]*-)?([a-zA-Z0-9]+)', re.IGNORECASE)
GIPHY_MEDIA_RE = re.compile(r'^https?://(?:media\d*\.)?giphy\.com/media/(?:v\d+\.[^/]+/)?([a-zA-Z0-9]+)/', re.IGNORECASE)
GIPHY_I_RE = re.compile(r'^https?://i\.giphy\.com/(?:media/)?([a-zA-Z0-9]+)\.', re.IGNORECASE)


def _normalize_url(raw_url: str) -> str:
    clean = raw_url.strip().strip("<>").rstrip(".,!?)")
    base = clean.split("?", 1)[0].split("#", 1)[0]
    if "://media.discordapp.net/" in base.lower():
        base = re.sub(r'://media\.discordapp\.net/', '://cdn.discordapp.com/', base, flags=re.IGNORECASE)
    return base


def _parse_gif_url(raw_url: str) -> tuple[str, str] | None:
    """Chuẩn hóa URL GIF (Tenor, Giphy, Discord CDN, link .gif) thành (item_value, display_label)."""
    norm = _normalize_url(raw_url)
    if not norm.lower().startswith(("http://", "https://")):
        return None

    m = TENOR_VIEW_RE.match(norm)
    if m:
        return f"tenor:{m.group(1)}", norm

    m = TENOR_MEDIA_RE.match(norm)
    if m:
        return f"tenor_media:{m.group(1)}", norm

    for pat in (GIPHY_PAGE_RE, GIPHY_MEDIA_RE, GIPHY_I_RE):
        m = pat.match(norm)
        if m:
            return f"giphy:{m.group(1)}", norm

    return f"url:{norm.lower()}", norm


def _is_gif_attachment(att) -> bool:
    fname = (getattr(att, "filename", "") or "").lower()
    ctype = (getattr(att, "content_type", "") or "").lower()
    return fname.endswith(".gif") or "gif" in ctype


def _compile_word_pattern(word: str) -> re.Pattern:
    """Biên dịch regex bắt từ/cụm từ bị cấm theo ranh giới từ để tránh bắt nhầm từ ghép."""
    clean = word.strip()
    parts = [re.escape(p) for p in clean.split()]
    escaped = r"\s+".join(parts)
    prefix = r"(?<!\w)" if (clean[0].isalnum() or clean[0] == "_") else ""
    suffix = r"(?!\w)" if (clean[-1].isalnum() or clean[-1] == "_") else ""
    return re.compile(f"{prefix}{escaped}{suffix}", re.IGNORECASE | re.UNICODE)


def _parse_emote_token(raw: str) -> tuple[str, str] | None:
    """Chuẩn hóa đầu vào emote thành (item_value, display_label)."""
    token = raw.strip()
    if not token:
        return None

    m = CUSTOM_EMOTE_RE.fullmatch(token)
    if m:
        name, emote_id = m.group(1), m.group(2)
        return f"id:{emote_id}", f":{name}: (ID: {emote_id})"

    if token.isdigit() and len(token) >= 15:
        return f"id:{token}", f"Emote ID: {token}"

    if token.startswith(":") and token.endswith(":") and len(token) > 2:
        name = token[1:-1].strip()
        if name:
            return f"name:{name.lower()}", f":{name}:"

    norm_unicode = token.replace("\ufe0f", "")
    return f"unicode:{norm_unicode}", token


class Moderation(commands.Cog):
    """Cog quản trị máy chủ: lọc từ/emote/sticker cấm, cảnh cáo, xóa tin nhắn và ban thành viên."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.commander_id = COMMANDER_ID
        self._filter_cache: dict[int, dict] = {}

    async def cog_load(self):
        await asyncio.to_thread(db.init_db)

    def _is_commander(self, author: discord.User | discord.Member) -> bool:
        """Xác định xem người dùng có phải là Chỉ huy Kett hay không."""
        if self.commander_id:
            return author.id == self.commander_id
        name_lower = author.name.lower()
        display_lower = author.display_name.lower()
        return "kett" in name_lower or "kett" in display_lower

    def _is_admin(self, member: discord.User | discord.Member) -> bool:
        """Kiểm tra quyền quản trị: Chỉ huy Kett, chủ server, Administrator hoặc Ban Members."""
        if self._is_commander(member):
            return True
        if isinstance(member, discord.Member):
            if member.id == member.guild.owner_id:
                return True
            perms = member.guild_permissions
            return perms.administrator or perms.ban_members
        return False

    def _is_exempt(self, member: discord.User | discord.Member) -> bool:
        """Miễn trừ kiểm duyệt tự động cho Bot, Chỉ huy Kett và Quản trị viên."""
        if member.bot:
            return True
        return self._is_admin(member)

    def _invalidate_cache(self, guild_id: int) -> None:
        self._filter_cache.pop(guild_id, None)

    async def _get_guild_rules(self, guild_id: int) -> dict:
        """Nạp danh sách cấm và ngưỡng cảnh cáo của server (có cache trong RAM)."""
        cached = self._filter_cache.get(guild_id)
        if cached is not None:
            return cached

        gid_str = str(guild_id)
        items, max_warnings, mute_minutes = await asyncio.gather(
            asyncio.to_thread(db.list_banned_items, gid_str),
            asyncio.to_thread(db.get_guild_max_warnings, gid_str),
            asyncio.to_thread(db.get_guild_mute_minutes, gid_str),
        )

        word_patterns: list[tuple[str, re.Pattern]] = []
        emote_ids: set[str] = set()
        emote_names: set[str] = set()
        unicode_emotes: list[tuple[str, str]] = []
        sticker_ids: set[str] = set()
        sticker_names: set[str] = set()
        gif_keys: set[str] = set()
        gif_names: set[str] = set()

        for item in items:
            itype = item["item_type"]
            ival = item["item_value"]
            label = item["display_label"] or ival

            if itype == "word" and ival:
                try:
                    word_patterns.append((label, _compile_word_pattern(ival)))
                except re.error:
                    continue
            elif itype == "emote":
                if ival.startswith("id:"):
                    emote_ids.add(ival[3:])
                elif ival.startswith("name:"):
                    emote_names.add(ival[5:])
                elif ival.startswith("unicode:"):
                    unicode_emotes.append((ival[8:], label))
                else:
                    unicode_emotes.append((ival, label))
            elif itype == "sticker":
                if ival.startswith("id:"):
                    sticker_ids.add(ival[3:])
                elif ival.startswith("name:"):
                    sticker_names.add(ival[5:])
                else:
                    sticker_ids.add(ival)
            elif itype == "gif" and ival:
                if ival.startswith("name:"):
                    gif_names.add(ival[5:])
                else:
                    gif_keys.add(ival)

        rules = {
            "word_patterns": word_patterns,
            "emote_ids": emote_ids,
            "emote_names": emote_names,
            "unicode_emotes": unicode_emotes,
            "sticker_ids": sticker_ids,
            "sticker_names": sticker_names,
            "gif_keys": gif_keys,
            "gif_names": gif_names,
            "max_warnings": max_warnings,
            "mute_minutes": mute_minutes,
        }
        self._filter_cache[guild_id] = rules
        return rules

    @staticmethod
    def _extract_message_payload(message: discord.Message) -> tuple[list[str], list, list, list]:
        """Trích xuất nội dung văn bản, sticker, tệp đính kèm và embed từ tin nhắn gốc lẫn tin nhắn forward."""
        contents: list[str] = []
        stickers: list = []
        attachments: list = []
        embeds: list = []

        if message.content:
            contents.append(message.content)
        if message.stickers:
            stickers.extend(message.stickers)
        if getattr(message, "attachments", None):
            attachments.extend(message.attachments)
        if getattr(message, "embeds", None):
            embeds.extend(message.embeds)

        snapshots = getattr(message, "message_snapshots", None) or []
        for snap in snapshots:
            snap_content = getattr(snap, "content", None)
            if snap_content:
                contents.append(snap_content)
            snap_stickers = getattr(snap, "stickers", None)
            if snap_stickers:
                stickers.extend(snap_stickers)
            snap_atts = getattr(snap, "attachments", None)
            if snap_atts:
                attachments.extend(snap_atts)
            snap_embeds = getattr(snap, "embeds", None)
            if snap_embeds:
                embeds.extend(snap_embeds)

        if not snapshots and message.reference and getattr(message.reference, "type", None) == discord.MessageReferenceType.forward:
            ref_cached = getattr(message.reference, "cached_message", None)
            if ref_cached:
                if ref_cached.content:
                    contents.append(ref_cached.content)
                if ref_cached.stickers:
                    stickers.extend(ref_cached.stickers)
                if getattr(ref_cached, "attachments", None):
                    attachments.extend(ref_cached.attachments)
                if getattr(ref_cached, "embeds", None):
                    embeds.extend(ref_cached.embeds)

        return contents, stickers, attachments, embeds

    @staticmethod
    def _iter_embed_urls(embed) -> list[str]:
        urls: list[str] = []
        if getattr(embed, "url", None):
            urls.append(embed.url)
        for attr in ("image", "thumbnail", "video"):
            media = getattr(embed, attr, None)
            media_url = getattr(media, "url", None) if media else None
            if media_url:
                urls.append(media_url)
        return urls

    async def find_violation(self, message: discord.Message) -> str | None:
        """Kiểm tra tin nhắn (kể cả nội dung forward) có chứa sticker, GIF, emote hoặc từ bị cấm không."""
        if not message.guild:
            return None

        rules = await self._get_guild_rules(message.guild.id)
        contents, stickers, attachments, embeds = self._extract_message_payload(message)

        for sticker in stickers:
            if str(sticker.id) in rules["sticker_ids"] or sticker.name.lower() in rules["sticker_names"]:
                return f"Sử dụng sticker bị cấm: `{sticker.name}`"

        if rules["gif_keys"] or rules["gif_names"]:
            for att in attachments:
                if not _is_gif_attachment(att):
                    continue
                fname = (getattr(att, "filename", "") or "").lower()
                size = getattr(att, "size", 0) or 0
                if fname and fname in rules["gif_names"]:
                    return f"Gửi GIF bị cấm: `{att.filename}`"
                if fname and size and f"file:{size}:{fname}" in rules["gif_keys"]:
                    return f"Gửi GIF bị cấm: `{att.filename}`"
                att_url = getattr(att, "url", None)
                if att_url:
                    parsed = _parse_gif_url(att_url)
                    if parsed and parsed[0] in rules["gif_keys"]:
                        return f"Gửi GIF bị cấm: `{att.filename or parsed[1]}`"

            for emb in embeds:
                for emb_url in self._iter_embed_urls(emb):
                    parsed = _parse_gif_url(emb_url)
                    if parsed and parsed[0] in rules["gif_keys"]:
                        return f"Gửi GIF bị cấm: {parsed[1]}"

            for text in contents:
                for raw_url in URL_RE.findall(text):
                    parsed = _parse_gif_url(raw_url)
                    if parsed and parsed[0] in rules["gif_keys"]:
                        return f"Gửi GIF bị cấm: {parsed[1]}"

        if not contents:
            return None

        combined_content = "\n".join(contents)

        for name, emote_id in CUSTOM_EMOTE_RE.findall(combined_content):
            if emote_id in rules["emote_ids"] or name.lower() in rules["emote_names"]:
                return f"Sử dụng emote bị cấm: `:{name}:`"

        norm_content = combined_content.replace("\ufe0f", "")
        for u_val, u_label in rules["unicode_emotes"]:
            if u_val and u_val in norm_content:
                return f"Sử dụng emote bị cấm: {u_label}"

        text_only = CUSTOM_EMOTE_RE.sub(" ", combined_content)
        for label, pattern in rules["word_patterns"]:
            if pattern.search(text_only):
                return f"Sử dụng từ ngữ bị cấm: `{label}`"

        return None

    async def _mute_member_with_reason(
        self,
        guild: discord.Guild,
        target: discord.Member | discord.User,
        minutes: int,
        reason: str,
    ) -> tuple[bool, str]:
        """Thực hiện mute (timeout) thành viên và trả về (thành_công, thông_báo_lỗi_nếu_có)."""
        member = target if isinstance(target, discord.Member) else guild.get_member(target.id)
        if not member:
            return False, "Không tìm thấy thành viên trong máy chủ."
        try:
            await member.timeout(timedelta(minutes=minutes), reason=reason)
            return True, ""
        except discord.Forbidden:
            return False, "LuLu thiếu quyền **Moderate Members** hoặc vai trò của thành viên này cao hơn LuLu."
        except discord.HTTPException as e:
            return False, f"Lỗi Discord API khi mute: {e}"

    async def _ban_member_with_reason(
        self,
        guild: discord.Guild,
        target: discord.Member | discord.User,
        reason: str,
    ) -> tuple[bool, str]:
        """Thực hiện ban thành viên và trả về (thành_công, thông_báo_lỗi_nếu_có)."""
        try:
            await guild.ban(target, reason=reason, delete_message_seconds=0)
            return True, ""
        except discord.Forbidden:
            return False, "LuLu thiếu quyền **Ban Members** hoặc vai trò của thành viên này cao hơn LuLu."
        except discord.HTTPException as e:
            return False, f"Lỗi Discord API khi ban: {e}"

    async def _record_warn_and_punish(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        target: discord.Member | discord.User,
        reason: str,
        warn_message: str | None = None,
    ) -> None:
        """Ghi nhận cảnh cáo, gửi thông báo nhắc nhở hoặc mute khi đạt ngưỡng."""
        warn_count = await asyncio.to_thread(
            db.add_warning,
            str(guild.id),
            str(target.id),
            reason,
            str(self.bot.user.id) if self.bot.user else "auto",
        )
        rules = await self._get_guild_rules(guild.id)
        max_warnings = rules["max_warnings"]
        mute_minutes = rules["mute_minutes"]
        msg_text = warn_message or f"Chỉ huy {target.mention}, đừng có gửi mấy cái biến thái như thế nữa!"

        if warn_count >= max_warnings:
            mute_reason = f"Đạt giới hạn {max_warnings} lần cảnh cáo ({reason})"
            muted, mute_err = await self._mute_member_with_reason(guild, target, mute_minutes, mute_reason)
            if muted:
                await asyncio.to_thread(db.clear_active_warnings, str(guild.id), str(target.id))
                await channel.send(
                    f"Em đã khóa mõm chỉ huy {target.mention} trong {mute_minutes} phút"
                )
            else:
                await channel.send(
                    f"{msg_text} Lulu vẫn chưa thể khóa mõm anh {mute_err}..."
                )
        else:
            await channel.send(msg_text)

    async def _handle_violation(self, message: discord.Message, reason: str) -> None:
        """Xóa tin nhắn vi phạm, gửi cảnh cáo trực tiếp và mute nếu vượt ngưỡng."""
        guild = message.guild
        author = message.author
        if not guild:
            return

        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        await self._record_warn_and_punish(guild, message.channel, author, reason)

    async def _find_reaction_violation(self, guild_id: int, emoji: discord.PartialEmoji) -> str | None:
        """Kiểm tra reaction có sử dụng emote hoặc emoji bị cấm không."""
        rules = await self._get_guild_rules(guild_id)
        if emoji.id is not None:
            e_name = emoji.name or ""
            if str(emoji.id) in rules["emote_ids"] or (e_name and e_name.lower() in rules["emote_names"]):
                return f"Thả reaction bị cấm: `:{e_name}:`"
            return None

        norm_name = (emoji.name or "").replace("\ufe0f", "")
        for u_val, u_label in rules["unicode_emotes"]:
            if u_val and u_val == norm_name:
                return f"Thả reaction bị cấm: {u_label}"
        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or self._is_exempt(message.author):
            return

        reason = await self.find_violation(message)
        if reason:
            await self._handle_violation(message, reason)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild or self._is_exempt(after.author):
            return
        if before.content == after.content and before.stickers == after.stickers:
            return

        reason = await self.find_violation(after)
        if reason:
            await self._handle_violation(after, reason)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id:
            return
        if self.bot.user and payload.user_id == self.bot.user.id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = payload.member or guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        if self._is_exempt(member):
            return

        reason = await self._find_reaction_violation(guild.id, payload.emoji)
        if not reason:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        if hasattr(channel, "get_partial_message"):
            try:
                partial_msg = channel.get_partial_message(payload.message_id)
                await partial_msg.remove_reaction(payload.emoji, member)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        if isinstance(channel, discord.abc.Messageable):
            await self._record_warn_and_punish(
                guild,
                channel,
                member,
                reason,
                warn_message=f"Chỉ huy {member.mention}, đừng có dùng mấy cái emoji biến thái đó để react tin nhắn nữa!",
            )

    async def _require_admin(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            await ctx.reply("Lệnh quản trị chỉ hoạt động bên trong máy chủ thôi ạ.")
            return False
        if not self._is_admin(ctx.author):
            await ctx.reply("Dạ, chỉ có Chỉ huy Kett hoặc Quản trị viên mới có quyền dùng lệnh quản trị này thôi ạ.")
            return False
        return True

    @commands.command(name='banword')
    async def ban_word(self, ctx: commands.Context, *, word: str = ""):
        """Thêm từ hoặc cụm từ vào danh sách cấm."""
        if not await self._require_admin(ctx):
            return

        clean_word = word.strip()
        if not clean_word:
            await ctx.reply("Chỉ huy vui lòng nhập từ cần cấm, ví dụ: `!banword từ_xấu`")
            return

        added = await asyncio.to_thread(
            db.add_banned_item,
            str(ctx.guild.id),
            "word",
            clean_word.lower(),
            clean_word,
            str(ctx.author.id),
        )
        self._invalidate_cache(ctx.guild.id)

        if added:
            await ctx.reply(f"Dạ, em đã thêm từ `{clean_word}` vào danh sách cấm của máy chủ rồi ạ!")
        else:
            await ctx.reply(f"Từ `{clean_word}` đã có sẵn trong danh sách cấm từ trước rồi ạ.")

    @commands.command(name='unbanword')
    async def unban_word(self, ctx: commands.Context, *, word: str = ""):
        """Gỡ từ hoặc cụm từ khỏi danh sách cấm."""
        if not await self._require_admin(ctx):
            return

        clean_word = word.strip()
        if not clean_word:
            await ctx.reply("Chỉ huy vui lòng nhập từ cần gỡ, ví dụ: `!unbanword từ_xấu`")
            return

        removed = await asyncio.to_thread(
            db.remove_banned_item, str(ctx.guild.id), "word", clean_word.lower()
        )
        self._invalidate_cache(ctx.guild.id)

        if removed:
            await ctx.reply(f"Dạ, em đã gỡ `{clean_word}` khỏi danh sách từ cấm rồi ạ.")
        else:
            await ctx.reply(f"Không tìm thấy từ `{clean_word}` trong danh sách từ cấm.")

    @commands.command(name='banemote')
    async def ban_emote(self, ctx: commands.Context, *, emotes_text: str = ""):
        """Thêm emote (custom emote hoặc emoji mặc định) vào danh sách cấm."""
        if not await self._require_admin(ctx):
            return

        tokens = emotes_text.strip().split()
        if not tokens:
            await ctx.reply("Chỉ huy hãy gửi kèm emote muốn cấm, ví dụ: `!banemote <emote>` hoặc `!banemote <:ten_emote:id>`")
            return

        added_labels = []
        existed_labels = []
        for tok in tokens:
            parsed = _parse_emote_token(tok)
            if not parsed:
                continue
            val, label = parsed
            added = await asyncio.to_thread(
                db.add_banned_item,
                str(ctx.guild.id),
                "emote",
                val,
                label,
                str(ctx.author.id),
            )
            if added:
                added_labels.append(label)
            else:
                existed_labels.append(label)

        self._invalidate_cache(ctx.guild.id)

        parts = []
        if added_labels:
            parts.append("Đã thêm emote cấm: " + ", ".join(added_labels))
        if existed_labels:
            parts.append("Đã có sẵn trong danh sách: " + ", ".join(existed_labels))
        await ctx.reply("\n".join(parts) if parts else "Không nhận diện được emote hợp lệ.")

    @commands.command(name='unbanemote')
    async def unban_emote(self, ctx: commands.Context, *, emotes_text: str = ""):
        """Gỡ emote khỏi danh sách cấm."""
        if not await self._require_admin(ctx):
            return

        tokens = emotes_text.strip().split()
        if not tokens:
            await ctx.reply("Chỉ huy hãy nhập emote hoặc ID emote cần gỡ, ví dụ: `!unbanemote <emote>`")
            return

        removed_labels = []
        not_found = []
        current_items = await asyncio.to_thread(db.list_banned_items, str(ctx.guild.id), "emote")

        for tok in tokens:
            parsed = _parse_emote_token(tok)
            if not parsed:
                continue
            val, label = parsed
            removed = await asyncio.to_thread(db.remove_banned_item, str(ctx.guild.id), "emote", val)
            if not removed and val.startswith("name:"):
                target_name = val[5:]
                for item in current_items:
                    if f":{target_name}:" in item["display_label"].lower():
                        removed = await asyncio.to_thread(
                            db.remove_banned_item, str(ctx.guild.id), "emote", item["item_value"]
                        )
                        if removed:
                            label = item["display_label"]
                            break
            if removed:
                removed_labels.append(label)
            else:
                not_found.append(label)

        self._invalidate_cache(ctx.guild.id)

        parts = []
        if removed_labels:
            parts.append("Đã gỡ emote cấm: " + ", ".join(removed_labels))
        if not_found:
            parts.append("Không tìm thấy trong danh sách: " + ", ".join(not_found))
        await ctx.reply("\n".join(parts) if parts else "Không nhận diện được emote hợp lệ.")

    async def _resolve_stickers(self, ctx: commands.Context, arg: str) -> list[tuple[str, str]]:
        """Trích xuất sticker từ tin nhắn lệnh, tin nhắn được reply hoặc từ ID/tên."""
        targets: list[tuple[str, str]] = []
        if ctx.message.stickers:
            for s in ctx.message.stickers:
                targets.append((f"id:{s.id}", f"{s.name} (ID: {s.id})"))
            return targets

        clean_arg = arg.strip()
        if not clean_arg and ctx.message.reference and ctx.message.reference.message_id:
            try:
                ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                _, ref_stickers, _, _ = self._extract_message_payload(ref_msg)
                for s in ref_stickers:
                    targets.append((f"id:{s.id}", f"{s.name} (ID: {s.id})"))
                if targets:
                    return targets
            except Exception:
                pass

        if clean_arg:
            if clean_arg.isdigit() and len(clean_arg) >= 10:
                targets.append((f"id:{clean_arg}", f"Sticker ID: {clean_arg}"))
            else:
                targets.append((f"name:{clean_arg.lower()}", clean_arg))
        return targets

    @commands.command(name='bansticker')
    async def ban_sticker(self, ctx: commands.Context, *, arg: str = ""):
        """Cấm sticker (bằng cách reply tin nhắn chứa sticker hoặc nhập ID/tên sticker)."""
        if not await self._require_admin(ctx):
            return

        targets = await self._resolve_stickers(ctx, arg)
        if not targets:
            await ctx.reply(
                "Chỉ huy hãy **reply** vào tin nhắn có sticker rồi gõ `!bansticker`, hoặc nhập tên/ID sticker (VD: `!bansticker Tên Sticker`)."
            )
            return

        added_labels = []
        existed_labels = []
        for val, label in targets:
            added = await asyncio.to_thread(
                db.add_banned_item,
                str(ctx.guild.id),
                "sticker",
                val,
                label,
                str(ctx.author.id),
            )
            if added:
                added_labels.append(f"`{label}`")
            else:
                existed_labels.append(f"`{label}`")

        self._invalidate_cache(ctx.guild.id)

        parts = []
        if added_labels:
            parts.append("Đã thêm sticker cấm: " + ", ".join(added_labels))
        if existed_labels:
            parts.append("Đã có sẵn trong danh sách: " + ", ".join(existed_labels))
        await ctx.reply("\n".join(parts))

    @commands.command(name='unbansticker')
    async def unban_sticker(self, ctx: commands.Context, *, arg: str = ""):
        """Gỡ sticker khỏi danh sách cấm."""
        if not await self._require_admin(ctx):
            return

        targets = await self._resolve_stickers(ctx, arg)
        if not targets:
            await ctx.reply("Chỉ huy hãy **reply** tin nhắn có sticker hoặc nhập ID/tên sticker cần gỡ.")
            return

        current_items = await asyncio.to_thread(db.list_banned_items, str(ctx.guild.id), "sticker")
        removed_labels = []
        not_found = []

        for val, label in targets:
            removed = await asyncio.to_thread(db.remove_banned_item, str(ctx.guild.id), "sticker", val)
            if not removed:
                search_key = val.split(":", 1)[-1].lower()
                for item in current_items:
                    if search_key in item["display_label"].lower() or item["item_value"].endswith(search_key):
                        removed = await asyncio.to_thread(
                            db.remove_banned_item, str(ctx.guild.id), "sticker", item["item_value"]
                        )
                        if removed:
                            label = item["display_label"]
                            break
            if removed:
                removed_labels.append(f"`{label}`")
            else:
                not_found.append(f"`{label}`")

        self._invalidate_cache(ctx.guild.id)

        parts = []
        if removed_labels:
            parts.append("Đã gỡ sticker cấm: " + ", ".join(removed_labels))
        if not_found:
            parts.append("Không tìm thấy trong danh sách: " + ", ".join(not_found))
        await ctx.reply("\n".join(parts))

    async def _resolve_gifs(self, ctx: commands.Context, arg: str) -> list[tuple[str, str]]:
        """Trích xuất GIF từ file đính kèm, link trong tham số, hoặc tin nhắn được reply."""
        targets: list[tuple[str, str]] = []
        seen_vals: set[str] = set()

        def add_target(val: str, label: str) -> None:
            if val not in seen_vals:
                seen_vals.add(val)
                targets.append((val, label))

        for att in getattr(ctx.message, "attachments", []) or []:
            if _is_gif_attachment(att):
                fname = (att.filename or "gif").lower()
                size = att.size or 0
                if size and fname:
                    add_target(f"file:{size}:{fname}", f"{att.filename} ({size}B)")
                elif att.url:
                    parsed = _parse_gif_url(att.url)
                    if parsed:
                        add_target(*parsed)
        if targets:
            return targets

        clean_arg = arg.strip()
        if clean_arg:
            urls = URL_RE.findall(clean_arg)
            if urls:
                for u in urls:
                    parsed = _parse_gif_url(u)
                    if parsed:
                        add_target(*parsed)
                return targets
            if clean_arg.isdigit() and len(clean_arg) >= 8:
                add_target(f"tenor:{clean_arg}", f"Tenor ID: {clean_arg}")
            else:
                add_target(f"name:{clean_arg.lower()}", clean_arg)
            return targets

        if ctx.message.reference and ctx.message.reference.message_id:
            try:
                ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                ref_contents, _, ref_atts, ref_embeds = self._extract_message_payload(ref_msg)

                for att in ref_atts:
                    if _is_gif_attachment(att):
                        fname = (att.filename or "gif").lower()
                        size = att.size or 0
                        if size and fname:
                            add_target(f"file:{size}:{fname}", f"{att.filename} ({size}B)")
                        elif att.url:
                            parsed = _parse_gif_url(att.url)
                            if parsed:
                                add_target(*parsed)

                if not targets:
                    for text in ref_contents:
                        for u in URL_RE.findall(text):
                            parsed = _parse_gif_url(u)
                            if parsed:
                                add_target(*parsed)

                if not targets:
                    for emb in ref_embeds:
                        for emb_url in self._iter_embed_urls(emb):
                            parsed = _parse_gif_url(emb_url)
                            if parsed:
                                add_target(*parsed)
                                break
            except Exception:
                pass

        return targets

    @commands.command(name='bangif')
    async def ban_gif(self, ctx: commands.Context, *, arg: str = ""):
        """Cấm một GIF cụ thể (reply vào GIF, gửi kèm file .gif hoặc dán link Tenor/Giphy)."""
        if not await self._require_admin(ctx):
            return

        targets = await self._resolve_gifs(ctx, arg)
        if not targets:
            await ctx.reply(
                "Chỉ huy hãy **reply** vào tin nhắn chứa GIF rồi gõ `!bangif`, gửi kèm file `.gif`, hoặc dán link GIF (VD: `!bangif https://tenor.com/view/...`)."
            )
            return

        added_labels = []
        existed_labels = []
        for val, label in targets:
            added = await asyncio.to_thread(
                db.add_banned_item,
                str(ctx.guild.id),
                "gif",
                val,
                label,
                str(ctx.author.id),
            )
            if added:
                added_labels.append(f"`{label}`")
            else:
                existed_labels.append(f"`{label}`")

        self._invalidate_cache(ctx.guild.id)

        parts = []
        if added_labels:
            parts.append("Đã thêm GIF cấm: " + ", ".join(added_labels))
        if existed_labels:
            parts.append("Đã có sẵn trong danh sách: " + ", ".join(existed_labels))
        await ctx.reply("\n".join(parts))

    @commands.command(name='unbangif')
    async def unban_gif(self, ctx: commands.Context, *, arg: str = ""):
        """Gỡ GIF khỏi danh sách cấm."""
        if not await self._require_admin(ctx):
            return

        targets = await self._resolve_gifs(ctx, arg)
        if not targets:
            await ctx.reply("Chỉ huy hãy **reply** vào GIF hoặc nhập link/tên GIF cần gỡ.")
            return

        current_items = await asyncio.to_thread(db.list_banned_items, str(ctx.guild.id), "gif")
        removed_labels = []
        not_found = []

        for val, label in targets:
            removed = await asyncio.to_thread(db.remove_banned_item, str(ctx.guild.id), "gif", val)
            if not removed:
                search_key = val.split(":", 1)[-1].lower()
                for item in current_items:
                    if search_key in item["display_label"].lower() or search_key in item["item_value"].lower():
                        removed = await asyncio.to_thread(
                            db.remove_banned_item, str(ctx.guild.id), "gif", item["item_value"]
                        )
                        if removed:
                            label = item["display_label"]
                            break
            if removed:
                removed_labels.append(f"`{label}`")
            else:
                not_found.append(f"`{label}`")

        self._invalidate_cache(ctx.guild.id)

        parts = []
        if removed_labels:
            parts.append("Đã gỡ GIF cấm: " + ", ".join(removed_labels))
        if not_found:
            parts.append("Không tìm thấy trong danh sách: " + ", ".join(not_found))
        await ctx.reply("\n".join(parts))

    @commands.command(name='banlist')
    async def list_banned(self, ctx: commands.Context):
        """Hiển thị toàn bộ danh sách từ, emote, sticker, GIF đang bị cấm trong server."""
        if not await self._require_admin(ctx):
            return

        items = await asyncio.to_thread(db.list_banned_items, str(ctx.guild.id))
        max_warn = await asyncio.to_thread(db.get_guild_max_warnings, str(ctx.guild.id))
        mute_mins = await asyncio.to_thread(db.get_guild_mute_minutes, str(ctx.guild.id))

        words = [f"`{i['display_label']}`" for i in items if i["item_type"] == "word"]
        emotes = [i["display_label"] for i in items if i["item_type"] == "emote"]
        stickers = [f"`{i['display_label']}`" for i in items if i["item_type"] == "sticker"]
        gifs = [f"`{i['display_label']}`" for i in items if i["item_type"] == "gif"]

        embed = discord.Embed(
            title=f"Danh sách kiểm duyệt — {ctx.guild.name}",
            description=f"Ngưỡng cảnh cáo: **{max_warn} lần** → Tự động Mute **{mute_mins} phút**",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name=f"Từ bị cấm ({len(words)})",
            value=", ".join(words)[:1000] if words else "*Trống*",
            inline=False,
        )
        embed.add_field(
            name=f"Emote bị cấm ({len(emotes)})",
            value=", ".join(emotes)[:1000] if emotes else "*Trống*",
            inline=False,
        )
        embed.add_field(
            name=f"Sticker bị cấm ({len(stickers)})",
            value=", ".join(stickers)[:1000] if stickers else "*Trống*",
            inline=False,
        )
        embed.add_field(
            name=f"GIF bị cấm ({len(gifs)})",
            value=", ".join(gifs)[:1000] if gifs else "*Trống*",
            inline=False,
        )
        await ctx.reply(embed=embed)

    @commands.command(name='warn')
    async def warn_member(self, ctx: commands.Context, member: discord.Member, *, reason: str = "Vi phạm nội quy máy chủ"):
        """Gửi cảnh cáo thủ công tới thành viên và tự động mute nếu đạt ngưỡng."""
        if not await self._require_admin(ctx):
            return

        if member.id == ctx.author.id:
            await ctx.reply("Chỉ huy không thể tự cảnh cáo chính mình được đâu ạ.")
            return
        if self._is_exempt(member):
            await ctx.reply("Không thể cảnh cáo Bot, Chỉ huy Kett hoặc Quản trị viên ạ.")
            return

        guild_id = str(ctx.guild.id)
        warn_count = await asyncio.to_thread(
            db.add_warning, guild_id, str(member.id), reason, str(ctx.author.id)
        )
        rules = await self._get_guild_rules(ctx.guild.id)
        max_warn = rules["max_warnings"]
        mute_minutes = rules["mute_minutes"]

        if warn_count >= max_warn:
            muted, mute_err = await self._mute_member_with_reason(
                ctx.guild, member, mute_minutes, f"Đạt {max_warn} lần cảnh cáo ({reason})"
            )
            if muted:
                await asyncio.to_thread(db.clear_active_warnings, guild_id, str(member.id))
                await ctx.send(
                    f"Em đã khóa mõm chỉ huy {member.mention} trong {mute_minutes} phút"
                )
            else:
                await ctx.send(
                    f"Chỉ huy {member.mention}, đừng có gửi mấy cái biến thái như thế nữa! *(Chưa thể mute: {mute_err})*"
                )
        else:
            await ctx.send(
                f"Chỉ huy {member.mention}, đừng có gửi mấy cái biến thái như thế nữa!"
            )

    @commands.command(name='warns')
    async def view_warnings(self, ctx: commands.Context, member: discord.Member | None = None):
        """Xem số lần và lịch sử cảnh cáo của bản thân hoặc thành viên."""
        if not ctx.guild:
            return

        target = member or ctx.author
        if target.id != ctx.author.id and not self._is_admin(ctx.author):
            await ctx.reply("Chỉ có Chỉ huy hoặc Quản trị viên mới xem được cảnh cáo của người khác ạ.")
            return

        gid = str(ctx.guild.id)
        warnings_list = await asyncio.to_thread(db.get_active_warnings, gid, str(target.id))
        max_warn = await asyncio.to_thread(db.get_guild_max_warnings, gid)

        if not warnings_list:
            await ctx.reply(f"**{target.display_name}** hiện chưa có cảnh cáo nào (0/{max_warn}).")
            return

        lines = [
            f"`#{idx}` {w['reason']} *({w['created_at'].strftime('%d/%m/%Y %H:%M')})*"
            for idx, w in enumerate(warnings_list[-10:], start=1)
        ]
        embed = discord.Embed(
            title=f"Hồ sơ cảnh cáo: {target.display_name} ({len(warnings_list)}/{max_warn})",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        await ctx.reply(embed=embed)

    @commands.command(name='clearwarn')
    async def clear_warnings(self, ctx: commands.Context, member: discord.Member):
        """Xóa toàn bộ cảnh cáo đang hiệu lực cho một thành viên."""
        if not await self._require_admin(ctx):
            return

        cleared = await asyncio.to_thread(db.clear_active_warnings, str(ctx.guild.id), str(member.id))
        await ctx.reply(f"Dạ, em đã xóa **{cleared}** cảnh cáo cho **{member.display_name}** rồi ạ.")

    @commands.command(name='maxwarn')
    async def set_max_warn(self, ctx: commands.Context, count: int | None = None):
        """Xem hoặc thay đổi ngưỡng cảnh cáo tối đa trước khi tự động Mute."""
        if not await self._require_admin(ctx):
            return

        gid = str(ctx.guild.id)
        if count is None:
            curr = await asyncio.to_thread(db.get_guild_max_warnings, gid)
            await ctx.reply(f"Ngưỡng cảnh cáo tự động Mute hiện tại là **{curr} lần**.")
            return

        if count < 1 or count > 20:
            await ctx.reply("Chỉ huy vui lòng chọn số lần cảnh cáo từ `1` đến `20` nhé ạ.")
            return

        await asyncio.to_thread(db.set_guild_max_warnings, gid, count)
        self._invalidate_cache(ctx.guild.id)
        await ctx.reply(f"Dạ, em đã cập nhật ngưỡng cảnh cáo tự động Mute thành **{count} lần** rồi ạ!")

    @commands.command(name='mutetime')
    async def set_mute_time(self, ctx: commands.Context, minutes: int | None = None):
        """Xem hoặc thay đổi thời gian Mute (phút) khi vượt ngưỡng cảnh cáo."""
        if not await self._require_admin(ctx):
            return

        gid = str(ctx.guild.id)
        if minutes is None:
            curr = await asyncio.to_thread(db.get_guild_mute_minutes, gid)
            await ctx.reply(f"Thời gian tự động Mute hiện tại là **{curr} phút**.")
            return

        if minutes < 1 or minutes > 40320:
            await ctx.reply("Chỉ huy vui lòng chọn thời gian Mute từ `1` đến `40320` phút (28 ngày) nhé ạ.")
            return

        await asyncio.to_thread(db.set_guild_mute_minutes, gid, minutes)
        self._invalidate_cache(ctx.guild.id)
        await ctx.reply(f"Dạ, em đã cập nhật thời gian tự động Mute thành **{minutes} phút** rồi ạ!")

    @commands.command(name='mute')
    async def mute_member(self, ctx: commands.Context, member: discord.Member, minutes: int | None = None, *, reason: str = "Vi phạm quy định máy chủ"):
        """Mute (timeout) trực tiếp một thành viên trong số phút chỉ định."""
        if not await self._require_admin(ctx):
            return

        if member.id == ctx.author.id or self._is_exempt(member):
            await ctx.reply("Không thể mute Chỉ huy Kett, Quản trị viên hoặc Bot ạ.")
            return

        mute_minutes = minutes if minutes is not None else await asyncio.to_thread(db.get_guild_mute_minutes, str(ctx.guild.id))
        if mute_minutes < 1 or mute_minutes > 40320:
            await ctx.reply("Thời gian mute phải nằm trong khoảng `1` đến `40320` phút ạ.")
            return

        muted, mute_err = await self._mute_member_with_reason(
            ctx.guild, member, mute_minutes, f"Bởi {ctx.author}: {reason}"
        )
        if muted:
            await ctx.send(f"Em đã khóa mõm chỉ huy {member.mention} trong {mute_minutes} phút")
        else:
            await ctx.reply(f"Không thể mute **{member.display_name}**: {mute_err}")

    @commands.command(name='unmute')
    async def unmute_member(self, ctx: commands.Context, member: discord.Member):
        """Gỡ Mute (timeout) cho một thành viên."""
        if not await self._require_admin(ctx):
            return

        try:
            await member.timeout(None, reason=f"Gỡ mute bởi {ctx.author}")
            await ctx.reply(f"Đã gỡ mute cho **{member.display_name}** rồi ạ.")
        except discord.Forbidden:
            await ctx.reply("LuLu thiếu quyền **Moderate Members** hoặc vai trò của thành viên này cao hơn LuLu.")
        except discord.HTTPException as e:
            await ctx.reply(f"Lỗi Discord API khi gỡ mute: {e}")

    @commands.command(name='ban')
    async def ban_member(self, ctx: commands.Context, member: discord.Member, *, reason: str = "Vi phạm quy định máy chủ"):
        """Ban trực tiếp một thành viên khỏi máy chủ."""
        if not await self._require_admin(ctx):
            return

        if member.id == ctx.author.id:
            await ctx.reply("Chỉ huy không thể tự ban chính mình được đâu ạ.")
            return
        if self._is_commander(member):
            await ctx.reply("Em không bao giờ ban Chỉ huy Kett đâu ạ!")
            return
        if member.id == self.bot.user.id:
            await ctx.reply("Em không thể tự ban chính mình được ạ.")
            return

        banned, ban_err = await self._ban_member_with_reason(
            ctx.guild, member, f"Bởi {ctx.author}: {reason}"
        )
        if banned:
            await asyncio.to_thread(db.clear_active_warnings, str(ctx.guild.id), str(member.id))
            await ctx.reply(f"Đã Ban **{member.display_name}** (`{member.id}`).\n• **Lý do:** {reason}")
        else:
            await ctx.reply(f"Không thể Ban **{member.display_name}**: {ban_err}")

    @commands.command(name='unban')
    async def unban_member(self, ctx: commands.Context, user_id: str = ""):
        """Gỡ Ban cho thành viên theo User ID."""
        if not await self._require_admin(ctx):
            return

        clean_id = user_id.strip().strip("<@!>")
        if not clean_id.isdigit():
            await ctx.reply("Chỉ huy vui lòng nhập đúng User ID cần gỡ ban, ví dụ: `!unban 123456789012345678`")
            return

        try:
            user = discord.Object(id=int(clean_id))
            await ctx.guild.unban(user, reason=f"Gỡ ban bởi {ctx.author}")
            await ctx.reply(f"Đã gỡ Ban cho người dùng ID `{clean_id}` rồi ạ.")
        except discord.NotFound:
            await ctx.reply(f"Không tìm thấy người dùng ID `{clean_id}` trong danh sách bị Ban.")
        except discord.Forbidden:
            await ctx.reply("LuLu thiếu quyền **Ban Members** để gỡ Ban.")
        except discord.HTTPException as e:
            await ctx.reply(f"Lỗi Discord API khi gỡ ban: {e}")

    @commands.command(name='purge')
    async def purge_messages(self, ctx: commands.Context, amount: int = 0):
        """Xóa nhanh N tin nhắn gần nhất trong kênh (từ 1 đến 100)."""
        if not await self._require_admin(ctx):
            return

        if amount < 1 or amount > 100:
            await ctx.reply("Chỉ huy vui lòng nhập số lượng tin nhắn cần xóa từ `1` đến `100` (VD: `!purge 10`).")
            return

        try:
            deleted = await ctx.channel.purge(limit=amount + 1)
            actual_deleted = max(0, len(deleted) - 1)
            msg = await ctx.send(f"Đã dọn dẹp **{actual_deleted}** tin nhắn theo lệnh của {ctx.author.mention}.")
            await asyncio.sleep(4)
            await msg.delete()
        except discord.Forbidden:
            await ctx.reply("LuLu thiếu quyền **Manage Messages** để xóa tin nhắn trong kênh này.")
        except discord.HTTPException as e:
            await ctx.reply(f"Không thể xóa tin nhắn: {e}")

    @commands.command(name='modhelp')
    async def mod_help(self, ctx: commands.Context):
        """Hiển thị hướng dẫn các lệnh quản trị của LuLu."""
        embed = discord.Embed(
            title="Bảng lệnh Quản trị",
            description="Tự động xóa tin nhắn vi phạm, nhắc nhở trực tiếp và Mute khi vượt ngưỡng.",
            color=discord.Color.teal(),
        )
        if self.bot.user:
            embed.set_author(name=self.bot.user.display_name, icon_url=self.bot.user.display_avatar.url)

        embed.add_field(
            name="Bộ lọc nội dung cấm",
            value=(
                "`!banword` / `!unbanword <từ>` — Thêm, gỡ từ cấm\n"
                "`!banemote` / `!unbanemote <emote>` — Thêm, gỡ emote cấm\n"
                "`!bansticker` / `!unbansticker [ID/tên]` — Thêm, gỡ sticker (có thể reply)\n"
                "`!bangif` / `!unbangif [link/tên]` — Thêm, gỡ GIF (có thể reply)\n"
                "`!banlist` — Xem toàn bộ danh sách cấm và cấu hình"
            ),
            inline=False,
        )
        embed.add_field(
            name="Cảnh cáo & Ngưỡng",
            value=(
                "`!warn @user [lý do]` — Cảnh cáo\n"
                "`!warns [@user]` — Xem cảnh cáo\n"
                "`!clearwarn @user` — Xóa cảnh cáo\n"
                "`!maxwarn [số]` — Ngưỡng Mute\n"
                "`!mutetime [phút]` — Thời gian"
            ),
            inline=True,
        )
        embed.add_field(
            name="Xử lý trực tiếp",
            value=(
                "`!mute @user [phút]` — Mute\n"
                "`!unmute @user` — Gỡ Mute\n"
                "`!ban @user [lý do]` — Ban\n"
                "`!unban <ID>` — Gỡ Ban\n"
                "`!purge <1-100>` — Xóa tin nhắn"
            ),
            inline=True,
        )
        embed.set_footer(text="Tiền tố: ! hoặc @LuLu  |  Yêu cầu quyền Quản trị viên hoặc Chỉ huy")
        await ctx.reply(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
