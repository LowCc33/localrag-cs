# -*- coding: utf-8 -*-
"""
客户档案数据库模块
基于 SQLite，提供客户档案的 CRUD 操作
架构位置：customer_service/customer/customer_db.py
数据表：customers
"""

import sqlite3
import logging
import random
import string
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)

# 数据库文件路径（跟会话库放一起，方便管理）
DATA_DIR = Path(__file__).parent.parent.parent / "data"
DB_PATH = DATA_DIR / "customers.db"


class CustomerDB:
    """客户档案数据库"""

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
                CREATE TABLE IF NOT EXISTS customers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id VARCHAR(64) UNIQUE NOT NULL,
                    secret_code VARCHAR(8) UNIQUE,
                    customer_type VARCHAR(16) DEFAULT 'c_end',
                    scenes TEXT,
                    area VARCHAR(32),
                    preference VARCHAR(32),
                    pricing_method VARCHAR(16),
                    wechat_nick VARCHAR(64),
                    phone VARCHAR(16),
                    community VARCHAR(64),
                    decoration_progress VARCHAR(32),
                    has_measurement INTEGER DEFAULT 0,
                    intent_level VARCHAR(8),
                    wechat_status VARCHAR(16) DEFAULT 'none',
                    source_channel VARCHAR(32),
                    remark TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_customers_secret_code
                    ON customers(secret_code);
                CREATE INDEX IF NOT EXISTS idx_customers_wechat_status
                    ON customers(wechat_status);
                CREATE INDEX IF NOT EXISTS idx_customers_intent_level
                    ON customers(intent_level);
                CREATE INDEX IF NOT EXISTS idx_customers_created_at
                    ON customers(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_customers_phone
                    ON customers(phone);
                CREATE INDEX IF NOT EXISTS idx_customers_wechat_nick
                    ON customers(wechat_nick);
            """)
            conn.commit()
            logger.info(f"✅ 客户数据库初始化完成: {self.db_path}")
        except Exception as e:
            logger.error(f"❌ 客户数据库初始化失败: {e}")
            raise
        finally:
            conn.close()

    # ========== 暗号生成 ==========

    def _generate_unique_secret_code(self, conn: sqlite3.Connection) -> str:
        """
        生成唯一的3-4位暗号（字母+数字组合）
        保证数据库中不重复
        """
        # 字母池：去掉容易混淆的 I、O、l
        letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
        digits = string.digits
        chars = letters + digits

        for _ in range(100):  # 最多尝试100次，理论上极难冲突
            # 格式：1个字母 + 3个数字（如 A857）
            code = (
                random.choice(letters)
                + random.choice(digits)
                + random.choice(digits)
                + random.choice(digits)
            )
            row = conn.execute(
                "SELECT id FROM customers WHERE secret_code = ?",
                (code,)
            ).fetchone()
            if row is None:
                return code

        # 极端情况：退化成5位
        code = (
            random.choice(letters)
            + "".join(random.choice(chars) for _ in range(4))
        )
        logger.warning(f"⚠️ 4位暗号资源紧张，使用5位暗号: {code}")
        return code

    # ========== 客户CRUD ==========

    def create_customer(
        self,
        session_id: str,
        shop_id: str = None,
        source_channel: str = None,
    ) -> Dict[str, Any]:
        """
        创建客户档案（会话开始时调用）
        自动生成唯一暗号

        Args:
            session_id: 会话ID
            shop_id: 商家ID（可选）
            source_channel: 来源渠道（可选）

        Returns:
            客户信息字典
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._get_conn()
        try:
            # 检查是否已存在
            existing = conn.execute(
                "SELECT * FROM customers WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            if existing:
                logger.info(f"客户已存在，直接返回: {session_id}")
                return dict(existing)

            # 生成唯一暗号
            secret_code = self._generate_unique_secret_code(conn)

            conn.execute(
                """
                INSERT INTO customers (
                    session_id, secret_code, source_channel,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, secret_code, source_channel, now, now)
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM customers WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            logger.info(f"✅ 创建客户档案: {session_id}, 暗号: {secret_code}")
            return dict(row)
        except Exception as e:
            logger.error(f"❌ 创建客户档案失败: {e}")
            raise
        finally:
            conn.close()

    def get_customer(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        按会话ID获取客户信息

        Args:
            session_id: 会话ID

        Returns:
            客户信息字典，不存在返回 None
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM customers WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            if row is None:
                return None
            return dict(row)
        finally:
            conn.close()

    def get_customer_by_secret_code(self, secret_code: str) -> Optional[Dict[str, Any]]:
        """
        按暗号查找客户

        Args:
            secret_code: 暗号

        Returns:
            客户信息字典，不存在返回 None
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM customers WHERE secret_code = ?",
                (secret_code.upper(),)
            ).fetchone()
            if row is None:
                return None
            return dict(row)
        finally:
            conn.close()

    def update_customer(
        self,
        session_id: str,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        """
        更新客户信息

        Args:
            session_id: 会话ID
            **kwargs: 要更新的字段（scenes, area, preference 等）

        Returns:
            更新后的客户信息，不存在返回 None
        """
        if not kwargs:
            return self.get_customer(session_id)

        # 过滤掉非法字段（防止SQL注入之外的意外）
        allowed_fields = {
            "customer_type", "scenes", "area", "preference",
            "pricing_method", "wechat_nick", "phone", "community",
            "decoration_progress", "has_measurement", "intent_level",
            "wechat_status", "source_channel", "remark",
        }
        update_data = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not update_data:
            return self.get_customer(session_id)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._get_conn()
        try:
            # 构造 SET 子句
            set_clause = ", ".join(f"{k} = ?" for k in update_data.keys())
            values = list(update_data.values()) + [now, session_id]

            cursor = conn.execute(
                f"UPDATE customers SET {set_clause}, updated_at = ? WHERE session_id = ?",
                values
            )
            conn.commit()

            if cursor.rowcount == 0:
                return None

            row = conn.execute(
                "SELECT * FROM customers WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            return dict(row)
        except Exception as e:
            logger.error(f"❌ 更新客户信息失败: {e}")
            raise
        finally:
            conn.close()

    def mark_wechat_added(
        self,
        session_id: str,
        wechat_nick: str,
    ) -> Optional[Dict[str, Any]]:
        """
        标记已加微信

        Args:
            session_id: 会话ID
            wechat_nick: 微信昵称

        Returns:
            更新后的客户信息
        """
        return self.update_customer(
            session_id,
            wechat_nick=wechat_nick,
            wechat_status="added",
        )

    def list_customers(
        self,
        page: int = 1,
        page_size: int = 20,
        wechat_status: str = None,
        intent_level: str = None,
        search: str = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> Dict[str, Any]:
        """
        客户列表（分页 + 筛选 + 搜索）

        Args:
            page: 页码（从1开始）
            page_size: 每页数量
            wechat_status: 按加微信状态筛选（none/added/lost）
            intent_level: 按意向等级筛选（A/B/C）
            search: 搜索关键词（暗号/微信昵称/手机号）
            sort_by: 排序字段
            sort_order: 排序方向（asc/desc）

        Returns:
            { "total": 总数, "customers": 列表, "page": 当前页, "page_size": 每页数量 }
        """
        conn = self._get_conn()
        try:
            # 构造 WHERE 条件
            where_clauses = []
            params = []

            if wechat_status:
                where_clauses.append("wechat_status = ?")
                params.append(wechat_status)

            if intent_level:
                where_clauses.append("intent_level = ?")
                params.append(intent_level)

            if search:
                search_like = f"%{search}%"
                where_clauses.append(
                    "(secret_code LIKE ? OR wechat_nick LIKE ? OR phone LIKE ? "
                    "OR community LIKE ?)"
                )
                params.extend([search_like, search_like, search_like, search_like])

            where_sql = ""
            if where_clauses:
                where_sql = "WHERE " + " AND ".join(where_clauses)

            # 总数
            total = conn.execute(
                f"SELECT COUNT(*) FROM customers {where_sql}",
                params
            ).fetchone()[0]

            # 排序
            allowed_sort = {"created_at", "updated_at", "intent_level", "wechat_status"}
            if sort_by not in allowed_sort:
                sort_by = "created_at"
            if sort_order.lower() not in ("asc", "desc"):
                sort_order = "desc"

            # 分页
            offset = (page - 1) * page_size
            rows = conn.execute(
                f"""
                SELECT * FROM customers {where_sql}
                ORDER BY {sort_by} {sort_order}
                LIMIT ? OFFSET ?
                """,
                params + [page_size, offset]
            ).fetchall()

            customers = [dict(row) for row in rows]

            return {
                "total": total,
                "customers": customers,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size if total > 0 else 0,
            }
        finally:
            conn.close()

    def delete_customer(self, session_id: str) -> bool:
        """
        删除客户档案（慎用）

        Args:
            session_id: 会话ID

        Returns:
            是否删除成功
        """
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "DELETE FROM customers WHERE session_id = ?",
                (session_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"❌ 删除客户档案失败: {e}")
            return False
        finally:
            conn.close()


# ========== 全局单例 ==========

customer_db = CustomerDB()
