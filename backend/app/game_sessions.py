"""Generation-fenced storage for individual game sessions.

The continuous-game HTTP API (``continuous_game_api``) is the only caller: it
obtains user IDs from authentication and supplies city pairs that have already
been validated.
"""
from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from app.game_generation import GameGeneration


_MAX_DATABASE_ID = 2**31 - 1


class SessionStorageError(RuntimeError):
    """The stored session is inconsistent or cannot be safely changed."""


class SessionNotFound(SessionStorageError):
    """The requested session does not belong to this user."""


class NoAvailableCityPair(SessionStorageError):
    """All supplied city pairs are occupied."""


class VictoryFinishing(SessionStorageError):
    """A session whose victory is being saved cannot be replaced or surrendered."""

    def __init__(self, session: PlayerSession) -> None:
        super().__init__("Victory is being saved.")
        self.session = session


class JoinResultUnavailable(SessionStorageError):
    """A previous join succeeded, but its original session no longer exists."""


class IdempotencyKeyConflict(SessionStorageError):
    """An operation key was already used with a different request."""


class PlayerSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    user_id: UUID
    generation_id: UUID
    round_id: UUID
    state: Literal["selecting_route", "playing", "finishing", "won"]
    source_city_id: int
    target_city_id: int
    route_ids: list[int]
    session_score: int
    hints: dict[str, dict[str, str]]
    state_version: int
    started_at: datetime
    victory_snapshot: dict[str, object] | None


_TYPE_CHECK = """
local function check_type(key, expected)
    local actual = redis.call("TYPE", key).ok
    return actual == "none" or actual == expected
end
"""

_COUNT_PAIRS = _TYPE_CHECK + """
if not check_type(KEYS[2], "hash") then return {"corrupt"} end
return {"ok", redis.call("HLEN", KEYS[2])}
"""

_RATE_LIMIT = """
local count = redis.call("INCR", KEYS[2])
if count == 1 then redis.call("EXPIRE", KEYS[2], ARGV[1]) end
if count > tonumber(ARGV[2]) then return {"limited", redis.call("TTL", KEYS[2])} end
return {"ok"}
"""

_READ = _TYPE_CHECK + """
if not check_type(KEYS[2], "hash") then return {"corrupt"} end
if redis.call("EXISTS", KEYS[2]) == 0 then return {"absent"} end
return {"ok", redis.call("HGETALL", KEYS[2])}
"""

_JOIN = _TYPE_CHECK + """
if not check_type(KEYS[2], "hash") or not check_type(KEYS[3], "hash")
   or not check_type(KEYS[4], "hash") or not check_type(KEYS[5], "string") then
    return {"corrupt"}
end
local existing_id = redis.call("HGET", KEYS[2], "id")
local previous_id = redis.call("HGET", KEYS[4], ARGV[3])
if previous_id then
    if existing_id ~= previous_id then return {"join_result_unavailable"} end
    return {"ok", redis.call("HGETALL", KEYS[2])}
end
if redis.call("EXISTS", KEYS[2]) == 1 and not existing_id then return {"corrupt"} end
local state = nil
local existing_round_id = nil
if existing_id then
    state = redis.call("HGET", KEYS[2], "state")
    existing_round_id = redis.call("HGET", KEYS[2], "round_id")
    if not existing_round_id or not redis.call("GET", KEYS[5]) then return {"corrupt"} end
    if state == "finishing" then return {"finishing", redis.call("HGETALL", KEYS[2])} end
    if state == "selecting_route" or state == "playing" then
        redis.call("HSET", KEYS[4], ARGV[3], existing_id)
        return {"ok", redis.call("HGETALL", KEYS[2])}
    end
    if state ~= "won" then return {"corrupt"} end
end
local pair = nil
local source = nil
local target = nil
for i = 6, #ARGV - 1, 3 do
    if not redis.call("HGET", KEYS[3], ARGV[i]) then
        pair, source, target = ARGV[i], ARGV[i + 1], ARGV[i + 2]
        break
    end
end
if not pair then return {"no_pair"} end
local current_round_id = redis.call("GET", KEYS[5])
local round_id = current_round_id
if not round_id or (state == "won" and existing_round_id == current_round_id) then
    round_id = ARGV[5]
    redis.call("SET", KEYS[5], round_id)
end
redis.call("HSET", KEYS[3], pair, ARGV[2])
redis.call("HSET", KEYS[2],
    "id", ARGV[2], "user_id", ARGV[1], "generation_id", ARGV[#ARGV],
    "round_id", round_id,
    "state", "selecting_route", "source_city_id", source, "target_city_id", target,
    "route_ids", "[]", "session_score", "0", "hints", "{}",
    "state_version", "1", "started_at", ARGV[4], "victory_snapshot", "null")
redis.call("HSET", KEYS[4], ARGV[3], ARGV[2])
return {"ok", redis.call("HGETALL", KEYS[2])}
"""

_SURRENDER = _TYPE_CHECK + """
if not check_type(KEYS[2], "hash") or not check_type(KEYS[3], "hash")
   or not check_type(KEYS[4], "hash") then return {"corrupt"} end
local previous_id = redis.call("HGET", KEYS[4], ARGV[2])
if previous_id then
    if previous_id ~= ARGV[1] then return {"key_conflict"} end
    return {"surrendered"}
end
local session_id = redis.call("HGET", KEYS[2], "id")
if not session_id or session_id ~= ARGV[1] then return {"not_found"} end
if redis.call("HGET", KEYS[2], "round_id") ~= ARGV[5] then return {"corrupt"} end
local state = redis.call("HGET", KEYS[2], "state")
if state == "finishing" then return {"finishing", redis.call("HGETALL", KEYS[2])} end
if state == "won" then return {"won", redis.call("HGETALL", KEYS[2])} end
if state ~= "selecting_route" and state ~= "playing" then return {"corrupt"} end
if redis.call("HGET", KEYS[2], "route_ids") ~= ARGV[3] then return {"retry"} end
local source = tonumber(redis.call("HGET", KEYS[2], "source_city_id"))
local target = tonumber(redis.call("HGET", KEYS[2], "target_city_id"))
if not source or not target or source == target then return {"corrupt"} end
local pair = tostring(math.min(source, target)) .. ":" .. tostring(math.max(source, target))
if redis.call("HGET", KEYS[3], pair) ~= session_id then return {"corrupt"} end
for i = 5, #KEYS, 2 do
    if not check_type(KEYS[i], "set")
       or redis.call("SISMEMBER", KEYS[i], session_id) ~= 1
       or redis.call("TYPE", KEYS[i + 1]).ok ~= "hash"
       or redis.call("HGET", KEYS[i + 1], "round_id") ~= ARGV[5] then
        return {"corrupt"}
    end
    if redis.call("TYPE", KEYS[i + 1]).ok == "hash"
       and redis.call("HGET", KEYS[i + 1], "owner:" .. session_id) ~= ARGV[4] then
        return {"corrupt"}
    end
end
redis.call("HDEL", KEYS[3], pair)
for i = 5, #KEYS, 2 do
    redis.call("SREM", KEYS[i], session_id)
    if redis.call("SCARD", KEYS[i]) == 0 then
        redis.call("DEL", KEYS[i], KEYS[i + 1])
    elseif redis.call("TYPE", KEYS[i + 1]).ok == "hash" then
        redis.call("HDEL", KEYS[i + 1], "owner:" .. session_id)
    end
end
redis.call("DEL", KEYS[2])
redis.call("HSET", KEYS[4], ARGV[2], session_id)
return {"surrendered"}
"""


class GameSessions:
    def __init__(self, generation: GameGeneration) -> None:
        self.generation = generation

    async def participant_count(self) -> int:
        """Count active sessions, including those whose victory is being saved."""
        result = await self.generation.execute(_COUNT_PAIRS, keys=[self.city_pairs_key()])
        if not isinstance(result, list) or len(result) != 2 or result[0] != "ok":
            raise SessionStorageError("Invalid Redis session storage.")
        if type(result[1]) is not int or result[1] < 0:
            raise SessionStorageError("Invalid participant count.")
        return result[1]

    async def record_guess_attempt(
        self, user_id: UUID, limit: int, window_seconds: int = 60
    ) -> int | None:
        """Count a guess attempt; return seconds until the window reopens if over the limit."""
        result = await self.generation.execute(
            _RATE_LIMIT,
            keys=[self.generation.key(f"rate:guess:{user_id}")],
            args=[window_seconds, limit],
        )
        if not isinstance(result, list) or not result:
            raise SessionStorageError("Invalid Redis response.")
        if result[0] == "ok":
            return None
        if result[0] == "limited" and len(result) == 2 and type(result[1]) is int:
            return max(1, result[1])
        raise SessionStorageError("Invalid Redis rate-limit state.")

    def session_key(self, user_id: UUID) -> str:
        return self.generation.key(f"session:user:{user_id}")

    def city_pairs_key(self) -> str:
        return self.generation.key("city_pairs")

    def current_round_key(self) -> str:
        return self.generation.key("current_round_id")

    def _operation_key(self, user_id: UUID, operation: str) -> str:
        return self.generation.key(f"operations:{operation}:{user_id}")

    def segment_keys(self, round_id: UUID, route_id: int) -> tuple[str, str]:
        if not isinstance(round_id, UUID):
            raise ValueError("Round IDs must be UUIDs.")
        if type(route_id) is not int or not 0 < route_id <= _MAX_DATABASE_ID:
            raise ValueError("Route IDs must be positive integers.")
        return (
            self.generation.key(f"segment:{round_id}:{route_id}:sessions"),
            self.generation.key(f"segment:{round_id}:{route_id}:state"),
        )

    def _decode_session(self, fields: object, user_id: UUID) -> tuple[PlayerSession, str]:
        if not isinstance(fields, list) or len(fields) % 2:
            raise SessionStorageError("Invalid Redis session fields.")
        data = dict(zip(fields[::2], fields[1::2]))
        try:
            route_ids_json = data["route_ids"]
            values = {
                **data,
                "route_ids": json.loads(route_ids_json),
                "hints": json.loads(data["hints"]),
                "victory_snapshot": json.loads(data["victory_snapshot"]),
            }
            session = PlayerSession.model_validate(values)
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise SessionStorageError("Invalid Redis session data.") from exc
        if (
            session.user_id != user_id
            or str(session.generation_id) != self.generation.generation_id
            or not 0 < session.source_city_id <= _MAX_DATABASE_ID
            or not 0 < session.target_city_id <= _MAX_DATABASE_ID
            or session.source_city_id == session.target_city_id
            or session.state_version <= 0
            or any(not 0 < route <= _MAX_DATABASE_ID for route in session.route_ids)
            or len(session.route_ids) != len(set(session.route_ids))
            or session.started_at.utcoffset() is None
            or (session.state == "selecting_route" and bool(session.route_ids))
            or (session.state != "selecting_route" and not session.route_ids)
            or (session.state == "won" and session.victory_snapshot is None)
        ):
            raise SessionStorageError("Inconsistent Redis session data.")
        return session, route_ids_json

    async def _read(self, user_id: UUID) -> tuple[PlayerSession, str] | None:
        result = await self.generation.execute(_READ, keys=[self.session_key(user_id)])
        if not isinstance(result, list) or not result:
            raise SessionStorageError("Invalid Redis response.")
        if result[0] == "absent":
            return None
        if result[0] != "ok" or len(result) != 2:
            raise SessionStorageError("Invalid Redis session storage.")
        return self._decode_session(result[1], user_id)

    async def restore(self, user_id: UUID) -> PlayerSession | None:
        """Restore the current session for a user already authenticated by the caller."""
        result = await self._read(user_id)
        return result[0] if result is not None else None

    async def join(
        self,
        user_id: UUID,
        idempotency_key: UUID,
        eligible_pairs: Sequence[tuple[int, int]],
    ) -> PlayerSession:
        """Create or recover a session, atomically reserving an eligible city pair."""
        pairs: dict[tuple[int, int], tuple[int, int]] = {}
        for source, target in eligible_pairs:
            if (
                type(source) is not int or type(target) is not int
                or not 0 < source <= _MAX_DATABASE_ID
                or not 0 < target <= _MAX_DATABASE_ID
                or source == target
            ):
                raise ValueError("City pairs must contain two distinct positive integer IDs.")
            pairs.setdefault((min(source, target), max(source, target)), (source, target))
        candidates = list(pairs.items())
        secrets.SystemRandom().shuffle(candidates)
        args: list[str | int] = [
            str(user_id), str(uuid4()), str(idempotency_key),
            datetime.now(timezone.utc).isoformat(), str(uuid4()),
        ]
        for (first, second), (source, target) in candidates:
            args.extend((f"{first}:{second}", source, target))
        result = await self.generation.execute(
            _JOIN,
            keys=[
                self.session_key(user_id), self.city_pairs_key(),
                self._operation_key(user_id, "join"), self.current_round_key(),
            ],
            args=args,
        )
        if not isinstance(result, list) or not result:
            raise SessionStorageError("Invalid Redis response.")
        if result[0] == "ok" and len(result) == 2:
            return self._decode_session(result[1], user_id)[0]
        if result[0] == "finishing" and len(result) == 2:
            raise VictoryFinishing(self._decode_session(result[1], user_id)[0])
        if result[0] == "join_result_unavailable":
            raise JoinResultUnavailable("The original join session is no longer available.")
        if result[0] == "no_pair":
            raise NoAvailableCityPair("No eligible city pair is available.")
        raise SessionStorageError("Invalid Redis session storage.")

    async def surrender(
        self, user_id: UUID, session_id: UUID, idempotency_key: UUID
    ) -> PlayerSession | None:
        """Return None after surrender, or the already confirmed winning session."""
        for _ in range(5):
            current = await self._read(user_id)
            route_ids_json = "[]"
            segment_keys: list[str] = []
            if current is not None and current[0].id == session_id:
                route_ids_json = current[1]
                for route_id in current[0].route_ids:
                    segment_keys.extend(self.segment_keys(current[0].round_id, route_id))
            result = await self.generation.execute(
                _SURRENDER,
                keys=[
                    self.session_key(user_id), self.city_pairs_key(),
                    self._operation_key(user_id, "surrender"), *segment_keys,
                ],
                args=[
                    str(session_id), str(idempotency_key), route_ids_json,
                    str(user_id), str(current[0].round_id) if current is not None else "",
                ],
            )
            if not isinstance(result, list) or not result:
                raise SessionStorageError("Invalid Redis response.")
            if result[0] == "surrendered":
                return None
            if result[0] == "won" and len(result) == 2:
                return self._decode_session(result[1], user_id)[0]
            if result[0] == "not_found":
                raise SessionNotFound("The session is not available for this user.")
            if result[0] == "finishing" and len(result) == 2:
                raise VictoryFinishing(self._decode_session(result[1], user_id)[0])
            if result[0] == "key_conflict":
                raise IdempotencyKeyConflict("The surrender key was used for another session.")
            if result[0] != "retry":
                raise SessionStorageError("Invalid Redis session storage.")
        raise SessionStorageError("The session changed repeatedly during surrender.")
