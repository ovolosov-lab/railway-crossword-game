"""Pending-victory processing with a generation-fenced PostgreSQL commit.

Create the service with GameVictories.create() once the application has
GameSessions for its loaded generation.
"""
from collections.abc import Awaitable, Callable
from uuid import UUID

from loguru import logger
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.game_generation import GameGeneration, StaleGameGeneration
from app.game_segments import GameSegments, VictorySnapshot
from app.game_sessions import GameSessions, PlayerSession, SessionNotFound, SessionStorageError
from app.victory_results import SessionVictory, VictoryResultConflict, find_session_victory, persist_session_victory


_LOAD = """
local session_type = redis.call("TYPE", KEYS[2]).ok
if session_type == "none" then return {"not_found"} end
if session_type ~= "hash" then return {"corrupt"} end
local queue_type = redis.call("TYPE", KEYS[3]).ok
if queue_type ~= "none" and queue_type ~= "hash" then return {"corrupt"} end
if redis.call("HGET", KEYS[2], "id") ~= ARGV[1] then return {"not_found"} end
if redis.call("HGET", KEYS[2], "user_id") ~= ARGV[2] then return {"corrupt"} end
local state = redis.call("HGET", KEYS[2], "state")
if state == "finishing" then
    if redis.call("HGET", KEYS[3], ARGV[1]) ~= ARGV[2] then return {"corrupt"} end
elseif state ~= "won" then
    return {"not_finishing"}
end
return {"ok", redis.call("HGETALL", KEYS[2])}
"""

_PUBLISH = """
local session_type = redis.call("TYPE", KEYS[2]).ok
if session_type == "none" then return {"not_found"} end
if session_type ~= "hash" then return {"corrupt"} end
if redis.call("HGET", KEYS[2], "id") ~= ARGV[1] then return {"not_found"} end
if redis.call("HGET", KEYS[2], "user_id") ~= ARGV[2]
   or redis.call("HGET", KEYS[2], "route_ids") ~= ARGV[3]
   or redis.call("HGET", KEYS[2], "victory_snapshot") ~= ARGV[4] then return {"corrupt"} end
local state = redis.call("HGET", KEYS[2], "state")
if state == "won" then return {"ok", redis.call("HGETALL", KEYS[2])} end
if state ~= "finishing" or redis.call("HGET", KEYS[2], "state_version") ~= ARGV[5] then
    return {"corrupt"}
end
if redis.call("TYPE", KEYS[3]).ok ~= "hash"
   or redis.call("TYPE", KEYS[4]).ok ~= "hash"
   or redis.call("HGET", KEYS[3], ARGV[6]) ~= ARGV[1]
   or redis.call("HGET", KEYS[4], ARGV[1]) ~= ARGV[2] then return {"corrupt"} end
local version = tonumber(ARGV[5])
if not version or version < 1 or version >= 9007199254740991 then return {"corrupt"} end
for i = 5, #KEYS, 2 do
    if redis.call("TYPE", KEYS[i]).ok ~= "set"
       or redis.call("TYPE", KEYS[i + 1]).ok ~= "hash"
       or redis.call("SISMEMBER", KEYS[i], ARGV[1]) ~= 1
       or redis.call("HGET", KEYS[i + 1], "owner:" .. ARGV[1]) ~= ARGV[2] then
        return {"corrupt"}
    end
end
redis.call("HDEL", KEYS[3], ARGV[6])
for i = 5, #KEYS, 2 do
    redis.call("SREM", KEYS[i], ARGV[1])
    if redis.call("SCARD", KEYS[i]) == 0 then
        redis.call("DEL", KEYS[i], KEYS[i + 1])
    else
        redis.call("HDEL", KEYS[i + 1], "owner:" .. ARGV[1])
    end
end
redis.call("HSET", KEYS[2], "state", "won",
    "state_version", string.format("%.0f", version + 1))
redis.call("HDEL", KEYS[4], ARGV[1])
return {"ok", redis.call("HGETALL", KEYS[2])}
"""


class GameVictories:
    def __init__(
        self,
        sessions: GameSessions,
        database_sessions: async_sessionmaker[AsyncSession],
        *,
        commit_victory: Callable[[SessionVictory], Awaitable[None]],
    ) -> None:
        self.sessions = sessions
        self.generation: GameGeneration = sessions.generation
        self.database_sessions = database_sessions
        self.commit_victory = commit_victory
        self.queue_key = GameSegments(sessions).pending_victories_key()
        self._cursor = "0"

    @classmethod
    async def create(
        cls,
        sessions: GameSessions,
        database_sessions: async_sessionmaker[AsyncSession],
    ) -> "GameVictories":
        """Create the service with a PostgreSQL commit fenced by the worker generation."""
        async def protected_commit(victory: SessionVictory) -> None:
            async with database_sessions() as session:
                await persist_session_victory(
                    session, victory, generation=sessions.generation
                )

        return cls(sessions, database_sessions, commit_victory=protected_commit)

    async def finish(self, user_id: UUID, session_id: UUID) -> PlayerSession:
        result = await self.generation.execute(
            _LOAD,
            keys=[self.sessions.session_key(user_id), self.queue_key],
            args=[str(session_id), str(user_id)],
        )
        if not isinstance(result, list) or not result:
            raise SessionStorageError("Invalid pending-victory response.")
        if result[0] == "not_found":
            raise SessionNotFound("The winning session is unavailable.")
        if result[0] != "ok" or len(result) != 2 or not isinstance(result[1], list):
            raise SessionStorageError("Invalid pending-victory state.")
        fields = result[1]
        session, routes_json = self.sessions._decode_session(fields, user_id)
        snapshot = VictorySnapshot.from_session(session)
        if session.state == "won":
            return session
        raw_fields = dict(zip(fields[::2], fields[1::2]))
        snapshot_json = raw_fields.get("victory_snapshot")
        if not isinstance(snapshot_json, str):
            raise SessionStorageError("The victory snapshot is missing.")
        victory = SessionVictory(
            session.id, user_id, session.started_at, snapshot.finished_at(),
            max(session.session_score, 0),
        )
        try:
            await self.commit_victory(victory)
        except (SQLAlchemyError, OSError, TimeoutError):
            logger.warning("Reconciling an uncertain victory commit for {}", session_id)
            async with self.database_sessions() as fresh:
                stored = await find_session_victory(fresh, victory)
            if stored is None:
                raise

        keys = [
            self.sessions.session_key(user_id), self.sessions.city_pairs_key(), self.queue_key
        ]
        for route_id in session.route_ids:
            keys.extend(self.sessions.segment_keys(route_id))
        pair = f"{min(session.source_city_id, session.target_city_id)}:{max(session.source_city_id, session.target_city_id)}"
        result = await self.generation.execute(
            _PUBLISH, keys=keys,
            args=[str(session_id), str(user_id), routes_json, snapshot_json,
                  str(session.state_version), pair],
        )
        if not isinstance(result, list) or not result:
            raise SessionStorageError("Invalid victory publication response.")
        if result[0] == "not_found":
            raise SessionNotFound("The winning session is unavailable.")
        if result[0] != "ok" or len(result) != 2:
            raise SessionStorageError("Cannot safely publish the winning session.")
        return self.sessions._decode_session(result[1], user_id)[0]

    async def retry_pending(self) -> None:
        result = await self.generation.execute(
            'return redis.call("HSCAN", KEYS[2], ARGV[1], "COUNT", 50)',
            keys=[self.queue_key], args=[self._cursor],
        )
        if (
            not isinstance(result, list) or len(result) != 2
            or not isinstance(result[0], str) or not isinstance(result[1], list)
            or len(result[1]) % 2
        ):
            raise SessionStorageError("Invalid pending-victory queue.")
        self._cursor = result[0]
        for index in range(0, len(result[1]), 2):
            try:
                session_id = UUID(result[1][index])
                user_id = UUID(result[1][index + 1])
                await self.finish(user_id, session_id)
            except StaleGameGeneration:
                raise
            except (ValueError, SessionStorageError, VictoryResultConflict, SQLAlchemyError, RedisError, OSError):
                logger.exception("Could not process pending victory; it remains queued")
