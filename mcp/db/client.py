"""MySQL client wrapper for the narrow database tools."""

from __future__ import annotations

from typing import Any

from config import DBConfig
from db.sql_guard import ensure_readonly_sql


class DatabaseClient:
    def __init__(self, config: DBConfig):
        self.config = config

    def _connect(self):
        try:
            import pymysql
        except ImportError as exc:
            raise RuntimeError("pymysql is required for database tools") from exc

        return pymysql.connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.username,
            password=self.config.password,
            database=self.config.name,
            charset=self.config.charset,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        ensure_readonly_sql(sql)
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
                return list(rows)
        finally:
            connection.close()
