from contextlib import contextmanager
import os
import queue
import time
import psycopg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
MAX_MESSAGES = 36
HISTORY_TTL_HOURS = 24
MAX_CONN_AGE_SECONDS = 240
_POOL: queue.LifoQueue[tuple[psycopg.Connection, float]] = queue.LifoQueue(maxsize=4)


def _close_quietly(conn: psycopg.Connection) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _return_to_pool(conn: psycopg.Connection) -> None:
    if conn.closed or conn.broken:
        _close_quietly(conn)
        return
    try:
        _POOL.put_nowait((conn, time.monotonic()))
    except queue.Full:
        _close_quietly(conn)


@contextmanager
def _db_conn():
    conn = None
    now = time.monotonic()
    while not _POOL.empty():
        try:
            candidate, last_used = _POOL.get_nowait()
        except queue.Empty:
            break
        if candidate.closed or candidate.broken or (now - last_used > MAX_CONN_AGE_SECONDS):
            _close_quietly(candidate)
            continue
        if now - last_used > 30:
            try:
                with candidate.transaction():
                    candidate.execute("SELECT 1")
            except Exception:
                _close_quietly(candidate)
                continue
        conn = candidate
        break

    if conn is None:
        conn = psycopg.connect(DATABASE_URL, autocommit=True)

    try:
        with conn.transaction():
            yield conn
    except Exception:
        _close_quietly(conn)
        raise
    else:
        _return_to_pool(conn)


def init_db() -> None:
    with _db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id)"
        )
        conn.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS author_id TEXT;"
        )
        conn.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS author_name TEXT;"
        )
        conn.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS target_user_id TEXT;"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session_created ON messages(user_id, created_at DESC, id DESC)"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_memories (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                role_type TEXT DEFAULT 'member',
                notes TEXT DEFAULT '',
                updated_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS banned_items (
                id BIGSERIAL PRIMARY KEY,
                guild_id TEXT NOT NULL,
                item_type TEXT NOT NULL,
                item_value TEXT NOT NULL,
                display_label TEXT DEFAULT '',
                created_by TEXT DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE (guild_id, item_type, item_value)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_banned_items_guild ON banned_items(guild_id, item_type)"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_warnings (
                id BIGSERIAL PRIMARY KEY,
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                issued_by TEXT DEFAULT 'auto',
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_warnings_guild_user ON user_warnings(guild_id, user_id, active)"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_mod_settings (
                guild_id TEXT PRIMARY KEY,
                max_warnings INT DEFAULT 3,
                mute_minutes INT DEFAULT 10,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        conn.execute(
            "ALTER TABLE guild_mod_settings ADD COLUMN IF NOT EXISTS mute_minutes INT DEFAULT 10;"
        )


def save_message(
    session_id: str,
    role: str,
    content: str,
    author_id: str | None = None,
    author_name: str | None = None,
    target_user_id: str | None = None,
) -> None:
    with _db_conn() as conn:
        conn.execute(
            """
            INSERT INTO messages (user_id, role, content, author_id, author_name, target_user_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (session_id, role, content, author_id, author_name, target_user_id),
        )
        conn.execute(
            """
            DELETE FROM messages
            WHERE user_id = %s
              AND id NOT IN (
                  SELECT id FROM messages
                  WHERE user_id = %s
                  ORDER BY id DESC
                  LIMIT %s
                )
            """,
            (session_id, session_id, MAX_MESSAGES),
        )


def save_message_pair(
    session_id: str,
    user_content: str,
    user_author_id: str,
    user_author_name: str,
    model_content: str,
    bot_id: str = "bot",
    bot_name: str = "LuLu",
) -> None:
    with _db_conn() as conn:
        conn.execute(
            """
            INSERT INTO messages (user_id, role, content, author_id, author_name, target_user_id)
            VALUES (%s, 'user', %s, %s, %s, %s),
                   (%s, 'model', %s, %s, %s, %s)
            """,
            (
                session_id, user_content, user_author_id, user_author_name, user_author_id,
                session_id, model_content, bot_id, bot_name, user_author_id,
            ),
        )
        conn.execute(
            """
            DELETE FROM messages
            WHERE user_id = %s
              AND id NOT IN (
                  SELECT id FROM messages
                  WHERE user_id = %s
                  ORDER BY id DESC
                  LIMIT %s
                )
            """,
            (session_id, session_id, MAX_MESSAGES),
        )


def load_history(session_id: str, max_age_hours: int = HISTORY_TTL_HOURS) -> list[dict]:
    with _db_conn() as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE user_id = %s
              AND created_at >= now() - (%s * interval '1 hour')
            ORDER BY id DESC
            LIMIT %s
            """,
            (session_id, max_age_hours, MAX_MESSAGES),
        ).fetchall()

    history = [{"role": role, "content": content} for role, content in reversed(rows)]

    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


def clear_history(session_id: str, author_id: str | None = None) -> None:
    with _db_conn() as conn:
        if author_id:
            conn.execute(
                """
                DELETE FROM messages
                WHERE user_id = %s AND (author_id = %s OR target_user_id = %s)
                """,
                (session_id, author_id, author_id),
            )
        else:
            conn.execute(
                "DELETE FROM messages WHERE user_id = %s",
                (session_id,),
            )

def get_user_memory(user_id: str) -> dict | None:
    with _db_conn() as conn:
        row = conn.execute(
            "SELECT user_id, display_name, role_type, notes FROM user_memories WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        if row:
            return {
                "user_id": row[0],
                "display_name": row[1],
                "role_type": row[2],
                "notes": row[3] or "",
            }
        return None


def upsert_user_memory(
    user_id: str,
    display_name: str,
    role_type: str = "member",
    notes: str = "",
) -> None:
    with _db_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_memories (user_id, display_name, role_type, notes, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (user_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                role_type = EXCLUDED.role_type,
                notes = CASE WHEN EXCLUDED.notes <> '' THEN EXCLUDED.notes ELSE user_memories.notes END,
                updated_at = now()
            """,
            (user_id, display_name, role_type, notes),
        )


def set_user_notes(
    user_id: str,
    display_name: str,
    notes: str,
    role_type: str = "member",
) -> None:
    with _db_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_memories (user_id, display_name, role_type, notes, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (user_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                role_type = EXCLUDED.role_type,
                notes = EXCLUDED.notes,
                updated_at = now()
            """,
            (user_id, display_name, role_type, notes),
        )


def append_user_note(
    user_id: str,
    new_note: str,
    display_name: str = "",
    role_type: str = "member",
) -> tuple[bool, str]:
    """
    Thêm một ghi nhớ mới vào hồ sơ cá nhân.
    - Tự động khử trùng lặp (không thêm nếu đã tồn tại thông tin tương tự).
    - Trả về tuple (đã_thêm_hay_chưa, toàn_bộ_notes_hiện_tại).
    """
    clean_note = new_note.strip()
    if not clean_note:
        return False, ""

    with _db_conn() as conn:
        row = conn.execute(
            "SELECT notes, display_name, role_type FROM user_memories WHERE user_id = %s",
            (user_id,),
        ).fetchone()

        if row:
            curr_notes, curr_name, curr_role = row[0] or "", row[1], row[2]
            items = [item.strip() for item in curr_notes.split('|') if item.strip()]

            clean_lower = clean_note.lower()
            for item in items:
                item_lower = item.lower()
                if clean_lower == item_lower or (len(clean_lower) >= 5 and clean_lower in item_lower):
                    return False, curr_notes

            items.append(clean_note)
            final_notes = " | ".join(items)
            name_to_save = display_name if display_name else curr_name
            role_to_save = role_type if role_type != "member" else curr_role

            conn.execute(
                """
                UPDATE user_memories
                SET notes = %s, display_name = %s, role_type = %s, updated_at = now()
                WHERE user_id = %s
                """,
                (final_notes, name_to_save, role_to_save, user_id),
            )
            return True, final_notes
        else:
            final_notes = clean_note
            conn.execute(
                """
                INSERT INTO user_memories (user_id, display_name, role_type, notes, updated_at)
                VALUES (%s, %s, %s, %s, now())
                """,
                (user_id, display_name or user_id, role_type, final_notes),
            )
            return True, final_notes


def reset_user_memory(user_id: str) -> None:
    with _db_conn() as conn:
        conn.execute("DELETE FROM user_memories WHERE user_id = %s", (user_id,))


def add_banned_item(
    guild_id: str,
    item_type: str,
    item_value: str,
    display_label: str = "",
    created_by: str = "",
) -> bool:
    """Thêm một từ, emote hoặc sticker vào danh sách cấm của server. Trả về True nếu mới thêm."""
    with _db_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO banned_items (guild_id, item_type, item_value, display_label, created_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (guild_id, item_type, item_value) DO NOTHING
            """,
            (guild_id, item_type, item_value, display_label or item_value, created_by),
        )
        return cur.rowcount > 0


def remove_banned_item(guild_id: str, item_type: str, item_value: str) -> bool:
    """Gỡ một mục khỏi danh sách cấm theo guild_id, item_type, item_value."""
    with _db_conn() as conn:
        cur = conn.execute(
            """
            DELETE FROM banned_items
            WHERE guild_id = %s AND item_type = %s AND item_value = %s
            """,
            (guild_id, item_type, item_value),
        )
        return cur.rowcount > 0


def list_banned_items(guild_id: str, item_type: str | None = None) -> list[dict]:
    """Lấy danh sách các mục bị cấm của một server."""
    with _db_conn() as conn:
        if item_type:
            rows = conn.execute(
                """
                SELECT item_type, item_value, display_label, created_by, created_at
                FROM banned_items
                WHERE guild_id = %s AND item_type = %s
                ORDER BY id ASC
                """,
                (guild_id, item_type),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT item_type, item_value, display_label, created_by, created_at
                FROM banned_items
                WHERE guild_id = %s
                ORDER BY item_type ASC, id ASC
                """,
                (guild_id,),
            ).fetchall()
    return [
        {
            "item_type": r[0],
            "item_value": r[1],
            "display_label": r[2],
            "created_by": r[3],
            "created_at": r[4],
        }
        for r in rows
    ]


def add_warning(guild_id: str, user_id: str, reason: str, issued_by: str = "auto") -> int:
    """Ghi nhận một lần cảnh cáo mới và trả về tổng số cảnh cáo đang hiệu lực."""
    with _db_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_warnings (guild_id, user_id, reason, issued_by, active)
            VALUES (%s, %s, %s, %s, TRUE)
            """,
            (guild_id, user_id, reason, issued_by),
        )
        row = conn.execute(
            """
            SELECT COUNT(*) FROM user_warnings
            WHERE guild_id = %s AND user_id = %s AND active = TRUE
            """,
            (guild_id, user_id),
        ).fetchone()
    return int(row[0]) if row else 1


def get_active_warnings(guild_id: str, user_id: str) -> list[dict]:
    """Lấy danh sách các lần cảnh cáo đang hiệu lực của thành viên."""
    with _db_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, reason, issued_by, created_at
            FROM user_warnings
            WHERE guild_id = %s AND user_id = %s AND active = TRUE
            ORDER BY id ASC
            """,
            (guild_id, user_id),
        ).fetchall()
    return [
        {"id": r[0], "reason": r[1], "issued_by": r[2], "created_at": r[3]}
        for r in rows
    ]


def clear_active_warnings(guild_id: str, user_id: str) -> int:
    """Đặt lại toàn bộ cảnh cáo đang hiệu lực của thành viên về đã xóa."""
    with _db_conn() as conn:
        cur = conn.execute(
            """
            UPDATE user_warnings
            SET active = FALSE
            WHERE guild_id = %s AND user_id = %s AND active = TRUE
            """,
            (guild_id, user_id),
        )
        return cur.rowcount


def get_guild_max_warnings(guild_id: str) -> int:
    """Lấy ngưỡng cảnh cáo tối đa trước khi tự động ban (mặc định: 3)."""
    with _db_conn() as conn:
        row = conn.execute(
            "SELECT max_warnings FROM guild_mod_settings WHERE guild_id = %s",
            (guild_id,),
        ).fetchone()
    return int(row[0]) if row and row[0] >= 1 else 3


def set_guild_max_warnings(guild_id: str, max_warnings: int) -> None:
    """Cập nhật ngưỡng cảnh cáo tối đa trước khi tự động mute."""
    with _db_conn() as conn:
        conn.execute(
            """
            INSERT INTO guild_mod_settings (guild_id, max_warnings, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (guild_id) DO UPDATE SET
                max_warnings = EXCLUDED.max_warnings,
                updated_at = now()
            """,
            (guild_id, max(1, max_warnings)),
        )


def get_guild_mute_minutes(guild_id: str) -> int:
    """Lấy thời gian mute mặc định (phút) khi vượt ngưỡng cảnh cáo (mặc định: 10)."""
    with _db_conn() as conn:
        row = conn.execute(
            "SELECT mute_minutes FROM guild_mod_settings WHERE guild_id = %s",
            (guild_id,),
        ).fetchone()
    return int(row[0]) if row and row[0] and row[0] >= 1 else 10


def set_guild_mute_minutes(guild_id: str, mute_minutes: int) -> None:
    """Cập nhật thời gian mute (phút, tối đa 40320 phút = 28 ngày theo giới hạn Discord)."""
    clamped = max(1, min(40320, mute_minutes))
    with _db_conn() as conn:
        conn.execute(
            """
            INSERT INTO guild_mod_settings (guild_id, mute_minutes, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (guild_id) DO UPDATE SET
                mute_minutes = EXCLUDED.mute_minutes,
                updated_at = now()
            """,
            (guild_id, clamped),
        )
