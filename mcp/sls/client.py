"""Thin Aliyun SLS SDK wrapper."""

from __future__ import annotations

import logging
from typing import Any

from config import SLSConfig

logger = logging.getLogger(__name__)


def _uses_sql_query(query: str) -> bool:
    return "|" in query


class SLSClient:
    def __init__(self, config: SLSConfig):
        self.config = config
        try:
            from aliyun.log import GetLogsRequest, LogClient
        except ImportError as exc:
            raise RuntimeError("aliyun-log-python-sdk is required for SLS tools") from exc
        self._request_cls = GetLogsRequest
        logger.info(
            "SLS credential loaded: endpoint=%s project=%s access_key_id_present=%s "
            "access_key_secret_present=%s access_key_id_len=%s access_key_secret_len=%s",
            config.client_endpoint,
            config.project,
            bool(config.access_key_id),
            bool(config.access_key_secret),
            len(config.access_key_id or ""),
            len(config.access_key_secret or ""),
        )
        self._client = LogClient(config.client_endpoint, config.access_key_id, config.access_key_secret)

    def query_logs(
        self,
        logstore: str,
        sql: str,
        start_ts: int,
        end_ts: int,
        limit: int,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        uses_sql_query = _uses_sql_query(sql)
        request_args = [
            self.config.project,
            logstore,
            start_ts,
            end_ts,
            self.config.topic,
            sql,
        ]
        if not uses_sql_query:
            request_args.append(limit)
            if offset:
                request_args.extend([offset, False])
        request = self._request_cls(*request_args)
        response = self._client.get_logs(request)
        return [dict(log_item.contents) for log_item in response.get_logs()]
