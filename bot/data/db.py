import os 
import psycopg
from dotenv import load_dotenv

load_dotenv() 

DATABASE_URL = os.environ["DATABASE_URL"]
MAX_MESSAGES = 20

def init_db() -> None:
    with psycopg.connect(DATABASE_URL) as conn:
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

def save_message(user_id: str, role: str, content: str) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (%s, %s, %s)",
            (user_id, role, content)
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
            (user_id, user_id, MAX_MESSAGES)
        )

def load_history(user_id: str) -> list[dict]:
    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT %s",
            (user_id, MAX_MESSAGES),
        ).fetchall()

    history = [{"role": role, "content": content} for role, content in reversed(rows)]

    while history and history[0]["role"] != "user":
        history.pop(0)
    return history

def clear_history(user_id: str) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("DELETE FROM messages WHERE user_id = %s", (user_id))