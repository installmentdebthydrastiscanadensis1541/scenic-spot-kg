"""对话历史持久化存储 — SQLite

功能：
- 用户管理（默认visitor账户）
- 对话列表（创建/删除/重命名）
- 消息存储（追加/查询）
"""
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

DB_PATH = os.getenv("CHAT_DB_PATH", "chat_history.db")

_DEFAULT_USER_ID = "visitor"
_DEFAULT_USER_NAME = "访客"


@dataclass
class Conversation:
    id: str
    user_id: str
    title: str
    created_at: float
    updated_at: float


@dataclass
class Message:
    id: str
    conversation_id: str
    role: str  # "user" | "assistant"
    content: str
    created_at: float


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """初始化数据库表和默认用户"""
    conn = _get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '新对话',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id);
        """)
        row = conn.execute("SELECT id FROM users WHERE id = ?", (_DEFAULT_USER_ID,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (id, name, created_at) VALUES (?, ?, ?)",
                (_DEFAULT_USER_ID, _DEFAULT_USER_NAME, time.time()),
            )
        conn.commit()
    finally:
        conn.close()


# ── 用户 ──

def get_or_create_user(user_id: Optional[str] = None) -> dict:
    """获取或创建用户，未指定则返回默认访客"""
    uid = user_id or _DEFAULT_USER_ID
    conn = _get_conn()
    try:
        row = conn.execute("SELECT id, name FROM users WHERE id = ?", (uid,)).fetchone()
        if row:
            return {"id": row["id"], "name": row["name"]}
        # 自动创建新访客
        now = time.time()
        name = f"访客{uid[-4:]}" if uid.startswith("u_") else _DEFAULT_USER_NAME
        conn.execute(
            "INSERT INTO users (id, name, created_at) VALUES (?, ?, ?)",
            (uid, name, now),
        )
        conn.commit()
        return {"id": uid, "name": name}
    finally:
        conn.close()


# ── 对话 ──

def list_conversations(user_id: Optional[str] = None) -> list[dict]:
    """列出用户的所有对话，按更新时间倒序"""
    uid = user_id or _DEFAULT_USER_ID
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE user_id = ? ORDER BY updated_at DESC",
            (uid,),
        ).fetchall()
        return [
            {"id": r["id"], "title": r["title"],
             "created_at": r["created_at"], "updated_at": r["updated_at"]}
            for r in rows
        ]
    finally:
        conn.close()


def create_conversation(user_id: Optional[str] = None, title: str = "新对话") -> dict:
    """创建新对话"""
    uid = user_id or _DEFAULT_USER_ID
    conv_id = uuid.uuid4().hex[:12]
    now = time.time()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (conv_id, uid, title, now, now),
        )
        conn.commit()
        return {"id": conv_id, "title": title, "created_at": now, "updated_at": now}
    finally:
        conn.close()


def rename_conversation(conv_id: str, title: str) -> bool:
    """重命名对话"""
    conn = _get_conn()
    try:
        conn.execute("UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?", (title, time.time(), conv_id))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def delete_conversation(conv_id: str) -> bool:
    """删除对话及其所有消息"""
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


# ── 消息 ──

def append_message(conv_id: str, role: str, content: str) -> dict:
    """追加一条消息"""
    msg_id = uuid.uuid4().hex[:12]
    now = time.time()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (msg_id, conv_id, role, content, now),
        )
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id))
        conn.commit()
        return {"id": msg_id, "role": role, "content": content, "created_at": now}
    finally:
        conn.close()


def get_messages(conv_id: str) -> list[dict]:
    """获取对话的所有消息"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY rowid",
            (conv_id,),
        ).fetchall()
        return [{"id": r["id"], "role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in rows]
    finally:
        conn.close()


def get_conversation_title(conv_id: str) -> Optional[str]:
    """获取对话标题"""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT title FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        return row["title"] if row else None
    finally:
        conn.close()


# ── 初始化 ──
init_db()
