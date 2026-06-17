"""Configuration helpers for the TokenHub local MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=False)

TIMEZONE = "Asia/Shanghai"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
TRACE_LOGSTORE_DEFAULT_LIMIT = 1000
TRACE_LOGSTORE_MAX_LIMIT = 10000
TRACE_REQUEST_RESPONSE_DEFAULT_LIMIT = 5000
TRACE_REQUEST_RESPONSE_MAX_LIMIT = 20000
TRACE_PAGE_SIZE = 100
MAX_WINDOW_SECONDS = 6 * 60 * 60
DEFAULT_WINDOW_SECONDS = 15 * 60
MAX_OVERVIEW_TIME_RANGE_DAYS = 180
REQUEST_RESPONSE_CHUNK_LIMIT = 1000
ASSEMBLED_RESPONSE_LIMIT = 12000
ASSEMBLED_RESPONSE_HEAD = 8000
ASSEMBLED_RESPONSE_TAIL = 4000
GATEWAY_ERROR_MESSAGE_LIMIT = 3000
GATEWAY_ERROR_MESSAGE_HEAD = 2000
GATEWAY_ERROR_MESSAGE_TAIL = 1000


@dataclass(frozen=True)
class SLSConfig:
    endpoint: str
    project: str
    default_logstore: str
    topic: str
    access_key_id: str
    access_key_secret: str

    @property
    def client_endpoint(self) -> str:
        if self.project and not self.endpoint.startswith(f"{self.project}."):
            return f"{self.project}.{self.endpoint}"
        return self.endpoint


@dataclass(frozen=True)
class DBConfig:
    host: str
    port: int
    name: str
    username: str
    password: str
    charset: str = "utf8mb4"


@dataclass(frozen=True)
class RuntimeConfig:
    sls: SLSConfig
    db: DBConfig


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return int(value)


def load_config() -> RuntimeConfig:
    return RuntimeConfig(
        sls=SLSConfig(
            endpoint=_env("SLS_ENDPOINT", "cn-hangzhou.log.aliyuncs.com"),
            project=_env("SLS_PROJECT", "zerozeroplatform-tokenhub-prod"),
            default_logstore=_env("SLS_LOGSTORE", "gateway"),
            topic=_env("SLS_TOPIC", "auth"),
            access_key_id=_env("ALIBABA_CLOUD_ACCESS_KEY_ID", ""),
            access_key_secret=_env("ALIBABA_CLOUD_ACCESS_KEY_SECRET", ""),
        ),
        db=DBConfig(
            host=_env("PROD_DB_HOST", "localhost"),
            port=_env_int("PROD_DB_PORT", 3306),
            name=_env("PROD_DB_NAME", "tokenplan_test"),
            username=_env("PROD_DB_USERNAME", "root"),
            password=_env("PROD_DB_PASSWORD", ""),
        ),
    )
