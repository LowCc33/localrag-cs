#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
会话持久化管理器

基于 SQLite 的会话持久化管理器，提供会话和消息的 CRUD 操作。
与 ingest/storage.py 的 SQLite 实例隔离，使用独立的数据库文件。

数据表：
- sessions: 会话元数据（id, title, created_at, updated_at）
- messages: 消息记录（id, session_id, role, content, created_at）

核心方法：
- create_session() — 创建新会话
- get_session(session_id) — 获取会话
- add_message(session_id, role, content) — 添加消息
- get_history(session_id, limit=16) — 获取历史（最多8轮对话）
- list_sessions(limit=50, offset=0) — 获取会话列表（按更新时间倒序）
- delete_session(session_id) — 删除会话
- clear_session(session_id) — 清空会话消息
"""

import sqlite3
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

# 数据库文件路径
DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "sessions.db"


class SessionManager:
    """会话持久化管理器"""

    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        """初始化数据库表结构"""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '新会话',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_sessions_updated
                    ON sessions(updated_at DESC);
            """)
            conn.commit()
            logger.info(f"✅ 会话数据库初始化完成: {self.db_path}")
        except Exception as e:
            logger.error(f"❌ 会话数据库初始化失败: {e}")
            raise
        finally:
            conn.close()

    # ========== 会话管理 ==========

    def create_session(self, title: str = "新会话") -> str:
        """
        创建新会话

        Args:
            title: 会话标题

        Returns:
            会话ID
        """
        session_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, title, now, now)
            )
            conn.commit()
            logger.info(f"✅ 创建会话: {session_id}")
            return session_id
        except Exception as e:
            logger.error(f"❌ 创建会话失败: {e}")
            raise
        finally:
            conn.close()

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        获取会话信息

        Args:
            session_id: 会话ID

        Returns:
            会话信息字典，不存在返回 None
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id, title, created_at, updated_at FROM sessions WHERE id = ?",
                (session_id,)
            ).fetchone()
            if row is None:
                return None
            return dict(row)
        finally:
            conn.close()

    def update_session_title(self, session_id: str, title: str) -> bool:
        """
        更新会话标题

        Args:
            session_id: 会话ID
            title: 新标题

        Returns:
            是否更新成功
        """
        conn = self._get_conn()
        try:
            now = datetime.now().isoformat()
            cursor = conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, session_id)
            )
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"❌ 更新会话标题失败: {e}")
            return False
        finally:
            conn.close()

    def delete_session(self, session_id: str) -> bool:
        """
        删除会话及其所有消息

        Args:
            session_id: 会话ID

        Returns:
            是否删除成功
        """
        conn = self._get_conn()
        try:
            cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"❌ 删除会话失败: {e}")
            return False
        finally:
            conn.close()

    def clear_session(self, session_id: str) -> bool:
        """
        清空会话的所有消息（保留会话本身）

        Args:
            session_id: 会话ID

        Returns:
            是否清空成功
        """
        conn = self._get_conn()
        try:
            now = datetime.now().isoformat()
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id)
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"❌ 清空会话失败: {e}")
            return False
        finally:
            conn.close()

    def list_sessions(self, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """
        获取会话列表（按更新时间倒序）

        Args:
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            会话列表信息
        """
        conn = self._get_conn()
        try:
            # 总数
            total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

            # 列表
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at FROM sessions "
                "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()

            sessions = []
            for row in rows:
                row_dict = dict(row)
                # 获取消息数量
                msg_count = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                    (row_dict['id'],)
                ).fetchone()[0]
                row_dict['message_count'] = msg_count
                sessions.append(row_dict)

            return {
                "total": total,
                "sessions": sessions,
                "limit": limit,
                "offset": offset
            }
        finally:
            conn.close()

    # ========== 消息管理 ==========

    def add_message(self, session_id: str, role: str, content: str) -> bool:
        """
        添加消息到会话

        Args:
            session_id: 会话ID
            role: 角色（user/assistant）
            content: 消息内容

        Returns:
            是否添加成功
        """
        conn = self._get_conn()
        try:
            now = datetime.now().isoformat()
            conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, now)
            )
            # 更新会话的更新时间
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id)
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"❌ 添加消息失败: {e}")
            return False
        finally:
            conn.close()

    def get_history(self, session_id: str, limit: int = 16) -> List[Dict[str, str]]:
        """
        获取会话历史消息

        Args:
            session_id: 会话ID
            limit: 返回消息数量上限（默认16条=8轮对话）

        Returns:
            消息列表，按时间正序
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT role, content FROM messages "
                "WHERE session_id = ? ORDER BY created_at ASC LIMIT ?",
                (session_id, limit)
            ).fetchall()
            return [{"role": row["role"], "content": row["content"]} for row in rows]
        finally:
            conn.close()

    def get_message_count(self, session_id: str) -> int:
        """获取会话的消息数量"""
        conn = self._get_conn()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                (session_id,)
            ).fetchone()[0]
        finally:
            conn.close()


# ========== 全局单例 ==========

session_manager = SessionManager()
