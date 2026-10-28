"""Validated per-user retrieval defaults, saved without changing server credentials."""
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from rag.settings import env_bool


class SettingsValues(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retrieval_mode: Literal["hybrid", "dense"] = "hybrid"
    top_k: int = Field(default=5, ge=1, le=20, strict=True)
    rerank_enabled: StrictBool = True
    graph_enabled: StrictBool | None = None  # None follows the server default.
    evidence_enabled: StrictBool | None = None
    memory_enabled: StrictBool = True


class SettingsEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: SettingsValues
    expected_revision: int = Field(ge=1, strict=True)


class SettingsConflict(ValueError):
    pass


def public_runtime_settings():
    # Explicit allowlist: never return URLs, environment contents or API keys.
    return {"agent_model": os.getenv("AGENT_BASE_MODEL", ""),
            "embedding_model": os.getenv("EMBEDDING_MODEL", ""),
            "rerank_model": os.getenv("RERANK_MODEL", ""),
            "graph_default_enabled": env_bool("GRAPH_RAG_ENABLED"),
            "evidence_default_enabled": env_bool("EVIDENCE_CHECK_ENABLED"),
            "graph_extraction_enabled": env_bool("GRAPH_EXTRACTION_ENABLED")}


class UserSettingsStore:
    def __init__(self, pool):
        self.pool = pool

    async def initialize(self):
        sql = (Path(__file__).parent / "migrations" / "004_user_settings.sql").read_text(encoding="utf-8")
        await self.pool.execute(sql)

    async def get(self, user_id):
        row = await self.pool.fetchrow("""SELECT app_settings,app_settings_revision,app_settings_updated_at
            FROM users WHERE user_id=$1""", user_id)
        if row is None:
            raise LookupError("用户不存在")
        values = row["app_settings"]
        if isinstance(values, str):
            values = json.loads(values)
        return {"user_id": user_id, "values": SettingsValues.model_validate(values).model_dump(),
                "revision": row["app_settings_revision"],
                "updated_at": row["app_settings_updated_at"].isoformat() if row["app_settings_updated_at"] else None}

    async def save(self, user_id, values: SettingsValues, revision):
        row = await self.pool.fetchrow("""UPDATE users SET app_settings=$2::jsonb,
            app_settings_revision=app_settings_revision+1,app_settings_updated_at=now()
            WHERE user_id=$1 AND app_settings_revision=$3 RETURNING user_id""",
            user_id, values.model_dump_json(), revision)
        if row is None:
            await self.get(user_id)
            raise SettingsConflict("设置已在其他地方更新，请重新加载后保存。")
        return await self.get(user_id)
