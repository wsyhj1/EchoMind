"""User-owned profiles, independent settings and optimistic concurrency control."""
import json
from pathlib import Path

FIELD_LABELS = {"background": "背景与经验", "goals": "当前目标", "interests": "关注领域", "preferences": "回答偏好"}


def effective_profile(profile):
    """Manual content is authoritative; don't mix it with inferred contradictions."""
    fields = profile["fields"]
    manual = "\n".join(f"{label}：{fields[key]}" for key, label in FIELD_LABELS.items() if fields.get(key))
    return manual or profile["automatic_summary"] or ""


def describe_personalization(profile, *, allowed=True):
    enabled = allowed and profile["personalization_enabled"]
    text = effective_profile(profile)
    return {"enabled": enabled, "injected": False, "revision": profile["revision"],
            "source": profile["source"], "profile_text": text if enabled else "",
            "reason": "pending" if enabled and text else ("empty" if enabled else "disabled")}


class ProfileConflict(ValueError):
    pass


class ProfileStore:
    def __init__(self, pool):
        self.pool = pool

    async def initialize(self):
        sql = (Path(__file__).parent / "migrations" / "002_user_profile.sql").read_text(encoding="utf-8")
        await self.pool.execute(sql)

    async def get(self, user_id):
        row = await self.pool.fetchrow("""SELECT user_id,user_profile,profile_fields,personalization_enabled,
            profile_auto_update,profile_revision,profile_content_updated_at,profile_settings_updated_at
            FROM users WHERE user_id=$1""", user_id)
        if row is None:
            raise LookupError("用户不存在")
        fields = row["profile_fields"]
        if isinstance(fields, str):
            fields = json.loads(fields)
        fields = {key: (fields.get(key) or "").strip() for key in FIELD_LABELS}
        automatic = (row["user_profile"] or "").strip()
        return {"user_id": user_id, "fields": fields, "automatic_summary": automatic,
                "personalization_enabled": row["personalization_enabled"], "auto_update_enabled": row["profile_auto_update"],
                "revision": row["profile_revision"], "source": "manual" if any(fields.values()) else ("automatic" if automatic else "empty"),
                "updated_at": row["profile_content_updated_at"].isoformat() if row["profile_content_updated_at"] else None,
                "settings_updated_at": row["profile_settings_updated_at"].isoformat() if row["profile_settings_updated_at"] else None}

    async def _checked(self, row, user_id):
        if row is None:
            await self.get(user_id)  # distinguish a missing user from a concurrent edit
            raise ProfileConflict("画像已发生变化，请刷新后再保存。")
        return await self.get(user_id)

    async def save_fields(self, user_id, fields, revision):
        clean = {key: fields.get(key, "").strip() for key in FIELD_LABELS}
        row = await self.pool.fetchrow("""UPDATE users SET profile_fields=$2::jsonb,
            profile_revision=profile_revision+1,profile_content_updated_at=now()
            WHERE user_id=$1 AND profile_revision=$3 RETURNING user_id""",
            user_id, json.dumps(clean, ensure_ascii=False), revision)
        return await self._checked(row, user_id)

    async def settings(self, user_id, revision, *, personalization_enabled=None, auto_update_enabled=None):
        row = await self.pool.fetchrow("""UPDATE users SET
            personalization_enabled=coalesce($3,personalization_enabled),
            profile_auto_update=coalesce($4,profile_auto_update),profile_revision=profile_revision+1,
            profile_settings_updated_at=now()
            WHERE user_id=$1 AND profile_revision=$2 RETURNING user_id""",
            user_id, revision, personalization_enabled, auto_update_enabled)
        return await self._checked(row, user_id)

    async def clear(self, user_id, revision):
        row = await self.pool.fetchrow("""UPDATE users SET profile_fields='{}'::jsonb,user_profile='',
            profile_auto_update=FALSE,profile_revision=profile_revision+1,
            profile_content_updated_at=now(),profile_settings_updated_at=now()
            WHERE user_id=$1 AND profile_revision=$2 RETURNING user_id""", user_id, revision)
        result = await self._checked(row, user_id)
        await self._invalidate(user_id)
        return result

    async def save_automatic(self, user_id, text, revision):
        """A late background result must not undo edits, clears or disabled learning."""
        result = await self.pool.fetchval("""UPDATE users SET user_profile=$2,
            profile_revision=profile_revision+1,profile_content_updated_at=now()
            WHERE user_id=$1 AND profile_auto_update AND profile_revision=$3
            RETURNING profile_revision""", user_id, text.strip(), revision)
        if result is not None:
            await self._invalidate(user_id)
        return result

    async def restore_automatic(self, user_id, text, revision):
        # Roll back only the exact write owned by this background operation.
        result = await self.pool.fetchval("""UPDATE users SET user_profile=$2,
            profile_revision=profile_revision+1,profile_content_updated_at=now()
            WHERE user_id=$1 AND profile_revision=$3 RETURNING profile_revision""", user_id, text or "", revision)
        if result is not None:
            await self._invalidate(user_id)
        return result

    async def _invalidate(self, user_id):
        # Settings and prompt snapshots always read PostgreSQL, never this legacy cache.
        try:
            from redis_cache import get_redis_client
            redis = await get_redis_client()
            await redis.delete(f"user_profile:{user_id}")
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Could not invalidate legacy profile cache for user %s", user_id)
