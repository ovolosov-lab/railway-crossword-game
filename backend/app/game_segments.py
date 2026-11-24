"""Atomic shared crossword state for the future continuous-game API."""
from __future__ import annotations

import asyncio
import json
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from app.game_sessions import GameSessions, PlayerSession, SessionNotFound, SessionStorageError


_WHITESPACE = frozenset(
    "\u0009\u000a\u000b\u000c\u000d\u0020\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
_DASHES = frozenset("-\u2010\u2011\u2012\u2013\u2014\u2015")


def normalize_answer(value: str) -> str:
    value = unicodedata.normalize("NFC", value).lower().replace("ё", "е")
    return "".join(char for char in value if char not in _WHITESPACE | _DASHES)


class WordProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    placement_id: UUID
    dictionary_word_id: int = Field(gt=0, le=2**31 - 1)
    word_text: str = Field(min_length=1, max_length=100)
    definition: str = Field(min_length=1)
    offset_px: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def require_letters(self) -> WordProposal:
        if not any(unicodedata.category(char).startswith("L") for char in self.word_text):
            raise ValueError("A crossword word must contain a letter.")
        if not self.definition.strip():
            raise ValueError("A crossword definition must not be blank.")
        return self

    def stored(self) -> dict[str, object]:
        letters = [
            char for char in unicodedata.normalize("NFC", self.word_text)
            if unicodedata.category(char).startswith("L")
        ]
        return {
            "placement_id": str(self.placement_id),
            "dictionary_word_id": self.dictionary_word_id,
            "word_text": self.word_text,
            "definition": self.definition,
            "offset_px": self.offset_px,
            "letter_count": len(self.word_text),
            "normalized": normalize_answer(self.word_text),
            "first_letter": letters[0],
            "last_letter": letters[-1],
            "points": len(letters) * 10,
            "solved_by": None,
        }


class _StoredWord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    placement_id: UUID
    dictionary_word_id: int
    word_text: str
    definition: str
    offset_px: float
    letter_count: int
    normalized: str
    first_letter: str
    last_letter: str
    points: int
    solved_by: UUID | None

    @model_validator(mode="after")
    def validate_stored_word(self) -> _StoredWord:
        letters = [
            char for char in unicodedata.normalize("NFC", self.word_text)
            if unicodedata.category(char).startswith("L")
        ]
        if (
            not letters
            or not self.definition.strip()
            or not 0 < self.dictionary_word_id <= 2**31 - 1
            or self.letter_count != len(self.word_text)
            or self.normalized != normalize_answer(self.word_text)
            or self.first_letter != letters[0]
            or self.last_letter != letters[-1]
            or self.points != len(letters) * 10
            or not 0 <= self.offset_px < float("inf")
        ):
            raise ValueError("Invalid stored crossword word.")
        return self


class _SnapshotSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_id: int
    words: list[_StoredWord]


class VictorySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    user_id: UUID
    round_id: UUID
    started_at: datetime
    finished_at_unix: str
    session_score: int
    route_ids: list[int]
    hints: dict[str, dict[str, str]]
    segments: list[_SnapshotSegment]

    def finished_at(self) -> datetime:
        seconds = Decimal(self.finished_at_unix)
        micros = seconds * 1_000_000
        if not seconds.is_finite() or micros != micros.to_integral_value():
            raise ValueError("Invalid snapshot timestamp.")
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            microseconds=int(micros)
        )

    @classmethod
    def from_session(cls, session: PlayerSession) -> VictorySnapshot:
        try:
            snapshot = cls.model_validate(session.victory_snapshot)
            finished_at = snapshot.finished_at()
            words = [word for segment in snapshot.segments for word in segment.words]
            if (
                snapshot.session_id != session.id
                or snapshot.user_id != session.user_id
                or snapshot.round_id != session.round_id
                or snapshot.started_at != session.started_at
                or snapshot.started_at.utcoffset() is None
                or finished_at < snapshot.started_at
                or snapshot.session_score != session.session_score
                or snapshot.route_ids != session.route_ids
                or snapshot.hints != session.hints
                or [segment.route_id for segment in snapshot.segments] != session.route_ids
                or not words
                or any(word.solved_by is None for word in words)
                or len({word.placement_id for word in words}) != len(words)
            ):
                raise ValueError("Snapshot does not match the winning session.")
            return snapshot
        except (ValidationError, ValueError, TypeError, InvalidOperation, OverflowError) as exc:
            raise SessionStorageError("Invalid victory snapshot.") from exc


class Placement(BaseModel):
    placement_id: UUID
    route_id: int
    offset_px: float
    letter_count: int
    definition: str
    is_guessed: bool
    guessed_by_me: bool
    word_text: str | None = None
    hints: dict[str, str] = Field(default_factory=dict)


class GuessResult(BaseModel):
    session: PlayerSession
    result: Literal["incorrect", "correct", "already_solved"]
    points_awarded: int
    revealed_word: str | None = None


class HintResult(BaseModel):
    session: PlayerSession
    hint_type: Literal["first_letter", "last_letter", "unknown"]
    value: str | None
    points_charged: int


class RouteAlreadyConfirmed(SessionStorageError):
    pass


class RouteAlreadySolved(SessionStorageError):
    pass


class CrosswordGenerationFailed(SessionStorageError):
    pass


class PlacementNotFound(SessionStorageError):
    pass


class InsufficientPoints(SessionStorageError):
    pass


class SessionNotPlaying(SessionStorageError):
    def __init__(self, session: PlayerSession) -> None:
        super().__init__("The session is not playing.")
        self.session = session


_TYPE_CHECK = """
local function key_type(key)
    return redis.call("TYPE", key).ok
end
local function valid_type(key, expected)
    local actual = key_type(key)
    return actual == "none" or actual == expected
end
"""

_CONFIRM = _TYPE_CHECK + """
if not valid_type(KEYS[2], "hash") or not valid_type(KEYS[3], "hash") then return {"corrupt"} end
if redis.call("HGET", KEYS[2], "id") ~= ARGV[1] then return {"not_found"} end
if redis.call("HGET", KEYS[2], "user_id") ~= ARGV[4]
   or redis.call("HGET", KEYS[2], "generation_id") ~= ARGV[#ARGV]
   or redis.call("HGET", KEYS[2], "round_id") ~= ARGV[5] then return {"corrupt"} end
local state = redis.call("HGET", KEYS[2], "state")
if state ~= "selecting_route" then
    if redis.call("HGET", KEYS[2], "route_ids") == ARGV[2] then
        return {"ok", redis.call("HGETALL", KEYS[2])}
    end
    return {"route_confirmed"}
end
local version = redis.call("HGET", KEYS[2], "state_version")
if not version or not string.match(version, "^%d+$") or tonumber(version) < 1
   or tonumber(version) >= 9007199254740991 then
    return {"corrupt"}
end
local proposals = cjson.decode(ARGV[3])
local source = tonumber(redis.call("HGET", KEYS[2], "source_city_id"))
local target = tonumber(redis.call("HGET", KEYS[2], "target_city_id"))
if not source or not target or source == target then return {"corrupt"} end
local pair = tostring(math.min(source, target)) .. ":" .. tostring(math.max(source, target))
if redis.call("HGET", KEYS[3], pair) ~= ARGV[1] then return {"corrupt"} end
local all_words = {}
local seen = {}
local total, unsolved = 0, 0
for i = 4, #KEYS, 2 do
    local index = (i - 4) / 2 + 1
    local refs, segment = KEYS[i], KEYS[i + 1]
    if not valid_type(refs, "set") or not valid_type(segment, "hash") then return {"corrupt"} end
    local has_segment = redis.call("EXISTS", segment) == 1
    local has_refs = redis.call("SCARD", refs) > 0
    if has_segment ~= has_refs then return {"corrupt"} end
    if has_segment and redis.call("HGET", segment, "round_id") ~= ARGV[5] then
        return {"corrupt"}
    end
    local encoded = has_segment and redis.call("HGET", segment, "words") or proposals[index]
    if not encoded or encoded == cjson.null then return {"missing_proposal"} end
    local ok, words = pcall(cjson.decode, encoded)
    if not ok or type(words) ~= "table" then return {"corrupt"} end
    for _, word in ipairs(words) do
        if type(word) ~= "table" or type(word.placement_id) ~= "string"
           or type(word.word_text) ~= "string" or type(word.definition) ~= "string"
           or type(word.normalized) ~= "string" or type(word.points) ~= "number"
           or (word.solved_by ~= cjson.null and type(word.solved_by) ~= "string")
           or seen[word.placement_id] then
            return {"corrupt"}
        end
        seen[word.placement_id] = true
        total = total + 1
        if word.solved_by == cjson.null then unsolved = unsolved + 1 end
    end
    all_words[index] = encoded
end
if total == 0 then
    redis.call("HDEL", KEYS[3], pair)
    redis.call("DEL", KEYS[2])
    return {"empty"}
end
if unsolved == 0 then return {"solved"} end
for i = 4, #KEYS, 2 do
    local index = (i - 4) / 2 + 1
    if redis.call("EXISTS", KEYS[i + 1]) == 0 then
        redis.call("HSET", KEYS[i + 1], "round_id", ARGV[5], "words", all_words[index])
    end
    redis.call("HSET", KEYS[i + 1], "owner:" .. ARGV[1], ARGV[4])
    redis.call("SADD", KEYS[i], ARGV[1])
end
redis.call("HSET", KEYS[2], "state", "playing", "route_ids", ARGV[2])
redis.call("HSET", KEYS[2], "state_version", string.format("%.0f", tonumber(version) + 1))
return {"ok", redis.call("HGETALL", KEYS[2])}
"""

_READ_SEGMENTS = _TYPE_CHECK + """
if not valid_type(KEYS[2], "hash") then return {"corrupt"} end
if redis.call("HGET", KEYS[2], "id") ~= ARGV[1] then return {"not_found"} end
if redis.call("HGET", KEYS[2], "user_id") ~= ARGV[3]
   or redis.call("HGET", KEYS[2], "generation_id") ~= ARGV[#ARGV]
   or redis.call("HGET", KEYS[2], "round_id") ~= ARGV[4] then return {"corrupt"} end
if redis.call("HGET", KEYS[2], "route_ids") ~= ARGV[2] then return {"retry"} end
local state = redis.call("HGET", KEYS[2], "state")
if state ~= "playing" and state ~= "finishing" then
    return {"not_playing", redis.call("HGETALL", KEYS[2])}
end
local result = {"ok", redis.call("HGETALL", KEYS[2]), {}}
for i = 3, #KEYS, 2 do
    if key_type(KEYS[i]) ~= "set" or key_type(KEYS[i + 1]) ~= "hash"
       or redis.call("SISMEMBER", KEYS[i], ARGV[1]) ~= 1
       or redis.call("HGET", KEYS[i + 1], "round_id") ~= ARGV[4] then
        return {"corrupt"}
    end
    local words = redis.call("HGET", KEYS[i + 1], "words")
    if not words then return {"corrupt"} end
    if redis.call("HGET", KEYS[i + 1], "owner:" .. ARGV[1]) ~= ARGV[3] then return {"corrupt"} end
    local members = redis.call("SMEMBERS", KEYS[i])
    local owners = {}
    for _, member in ipairs(members) do
        local owner = redis.call("HGET", KEYS[i + 1], "owner:" .. member)
        if not owner then return {"corrupt"} end
        table.insert(owners, member)
        table.insert(owners, owner)
    end
    table.insert(result[3], {words, owners})
end
return result
"""

_GUESS = _TYPE_CHECK + """
if key_type(KEYS[2]) ~= "hash" or key_type(KEYS[3]) ~= "set"
   or key_type(KEYS[4]) ~= "hash" or not valid_type(KEYS[5], "hash") then return {"corrupt"} end
if redis.call("HGET", KEYS[2], "id") ~= ARGV[1] then return {"not_found"} end
if redis.call("HGET", KEYS[2], "user_id") ~= ARGV[6]
   or redis.call("HGET", KEYS[2], "generation_id") ~= ARGV[#ARGV]
   or redis.call("HGET", KEYS[2], "round_id") ~= ARGV[8]
   or redis.call("HGET", KEYS[4], "round_id") ~= ARGV[8] then return {"corrupt"} end
if redis.call("HGET", KEYS[2], "state") ~= "playing" then
    return {"not_playing", redis.call("HGETALL", KEYS[2])}
end
if redis.call("HGET", KEYS[2], "route_ids") ~= ARGV[5] then return {"retry"} end
if redis.call("SISMEMBER", KEYS[3], ARGV[1]) ~= 1 then return {"retry"} end
local encoded = redis.call("HGET", KEYS[4], "words")
local ok, words = pcall(cjson.decode, encoded or "")
if not ok or type(words) ~= "table" then return {"corrupt"} end
local selected = nil
for _, word in ipairs(words) do
    if word.placement_id == ARGV[2] then selected = word; break end
end
if not selected then return {"retry"} end
if selected.solved_by == nil or type(selected.word_text) ~= "string" then return {"corrupt"} end
if selected.solved_by ~= cjson.null then
    return {"already_solved", redis.call("HGETALL", KEYS[2]), selected.word_text}
end
if selected.normalized ~= ARGV[3] then return {"incorrect", redis.call("HGETALL", KEYS[2])} end
if type(selected.points) ~= "number" or selected.points <= 0 or selected.points % 1 ~= 0 then
    return {"corrupt"}
end
local plan = cjson.decode(ARGV[4])
if redis.call("SCARD", KEYS[3]) ~= #plan then return {"retry"} end
local updates = {}
selected.solved_by = ARGV[6]
local updated_words = cjson.encode(words)
local clock = redis.call("TIME")
local redis_time = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
for _, participant in ipairs(plan) do
    if redis.call("SISMEMBER", KEYS[3], participant.id) ~= 1
       or redis.call("HGET", KEYS[4], "owner:" .. participant.id) ~= participant.user_id then
        return {"retry"}
    end
    local session_key = KEYS[participant.key]
    if key_type(session_key) ~= "hash" then return {"retry"} end
    if redis.call("HGET", session_key, "id") ~= participant.id
       or redis.call("HGET", session_key, "user_id") ~= participant.user_id
       or redis.call("HGET", session_key, "round_id") ~= ARGV[8]
       or redis.call("HGET", session_key, "state") ~= "playing"
       or redis.call("HGET", session_key, "route_ids") ~= participant.route_ids then
        return {"retry"}
    end
    local version = redis.call("HGET", session_key, "state_version")
    local score = redis.call("HGET", session_key, "session_score")
    if not version or not string.match(version, "^%d+$") or tonumber(version) < 1
       or tonumber(version) >= 9007199254740991
       or not score or not string.match(score, "^%-?%d+$") or math.abs(tonumber(score)) > 9007199254740000 then
        return {"corrupt"}
    end
    local new_score = tonumber(score)
    if participant.id == ARGV[1] then new_score = new_score + selected.points end
    if math.abs(new_score) > 9007199254740000 then return {"corrupt"} end
    local solved = true
    local snapshots = {}
    local includes_target = false
    for _, route in ipairs(participant.routes) do
        if key_type(KEYS[route.refs]) ~= "set" or key_type(KEYS[route.state]) ~= "hash"
           or redis.call("SISMEMBER", KEYS[route.refs], participant.id) ~= 1
           or redis.call("HGET", KEYS[route.state], "round_id") ~= ARGV[8] then
            return {"retry"}
        end
        local route_words = redis.call("HGET", KEYS[route.state], "words")
        if route.id == ARGV[7] then
            route_words = updated_words
            includes_target = true
        end
        local valid, route_data = pcall(cjson.decode, route_words or "")
        if not valid or type(route_data) ~= "table" then return {"corrupt"} end
        for _, entry in ipairs(route_data) do
            if entry.solved_by == nil then return {"corrupt"} end
            if entry.solved_by == cjson.null then solved = false end
        end
        table.insert(snapshots, '{"route_id":' .. route.id .. ',"words":' .. route_words .. '}')
    end
    if not includes_target then return {"corrupt"} end
    local snapshot = nil
    if solved then
        local hints = redis.call("HGET", session_key, "hints")
        local started = redis.call("HGET", session_key, "started_at")
        local valid_hints = pcall(cjson.decode, hints or "")
        if not valid_hints or not started then return {"corrupt"} end
        local finished_at = string.format("%.6f", math.max(redis_time, participant.started_unix))
        snapshot = '{"session_id":' .. cjson.encode(participant.id)
            .. ',"user_id":' .. cjson.encode(participant.user_id)
            .. ',"round_id":' .. cjson.encode(ARGV[8])
            .. ',"started_at":' .. cjson.encode(started)
            .. ',"finished_at_unix":' .. cjson.encode(finished_at)
            .. ',"session_score":' .. string.format("%.0f", new_score)
            .. ',"route_ids":' .. participant.route_ids
            .. ',"hints":' .. hints
            .. ',"segments":[' .. table.concat(snapshots, ",") .. ']}'
    end
    table.insert(updates, {key=session_key, id=participant.id, user_id=participant.user_id,
                           score=new_score, version=tonumber(version) + 1, snapshot=snapshot})
end
redis.call("HSET", KEYS[4], "words", updated_words)
for _, update in ipairs(updates) do
    redis.call("HSET", update.key, "session_score", string.format("%.0f", update.score),
               "state_version", string.format("%.0f", update.version))
    if update.snapshot then
        redis.call("HSET", update.key, "state", "finishing", "victory_snapshot", update.snapshot)
        redis.call("HSET", KEYS[5], update.id, update.user_id)
    end
end
return {"correct", redis.call("HGETALL", KEYS[2]), tostring(selected.points), selected.word_text}
"""

_HINT = _TYPE_CHECK + """
if key_type(KEYS[2]) ~= "hash" or key_type(KEYS[3]) ~= "set" or key_type(KEYS[4]) ~= "hash" then
    return {"corrupt"}
end
if redis.call("HGET", KEYS[2], "id") ~= ARGV[1] then return {"not_found"} end
if redis.call("HGET", KEYS[2], "user_id") ~= ARGV[6]
   or redis.call("HGET", KEYS[2], "generation_id") ~= ARGV[#ARGV]
   or redis.call("HGET", KEYS[2], "round_id") ~= ARGV[7]
   or redis.call("HGET", KEYS[4], "round_id") ~= ARGV[7] then return {"corrupt"} end
if redis.call("HGET", KEYS[2], "state") ~= "playing" then
    return {"not_playing", redis.call("HGETALL", KEYS[2])}
end
if redis.call("HGET", KEYS[2], "route_ids") ~= ARGV[5]
   or redis.call("SISMEMBER", KEYS[3], ARGV[1]) ~= 1 then return {"retry"} end
local ok, words = pcall(cjson.decode, redis.call("HGET", KEYS[4], "words") or "")
local valid_hints, hints = pcall(cjson.decode, redis.call("HGET", KEYS[2], "hints") or "")
if not ok or type(words) ~= "table" or not valid_hints or type(hints) ~= "table" then
    return {"corrupt"}
end
local selected = nil
for _, word in ipairs(words) do
    if word.placement_id == ARGV[2] then selected = word; break end
end
if not selected then return {"retry"} end
if selected.solved_by == nil then return {"corrupt"} end
local used = hints[ARGV[2]]
if used and used[ARGV[3]] then
    return {"repeat", redis.call("HGETALL", KEYS[2]), used[ARGV[3]]}
end
if selected.solved_by ~= cjson.null then return {"unknown", redis.call("HGETALL", KEYS[2])} end
local score = redis.call("HGET", KEYS[2], "session_score")
local version = redis.call("HGET", KEYS[2], "state_version")
if not score or not string.match(score, "^%-?%d+$") or math.abs(tonumber(score)) > 9007199254740000
   or not version or not string.match(version, "^%d+$") or tonumber(version) < 1
   or tonumber(version) >= 9007199254740991 then
    return {"corrupt"}
end
if tonumber(score) < tonumber(ARGV[4]) then return {"insufficient"} end
local value = selected[ARGV[3]]
if type(value) ~= "string" or value == "" then return {"corrupt"} end
if not used then used = {}; hints[ARGV[2]] = used end
used[ARGV[3]] = value
redis.call("HSET", KEYS[2], "hints", cjson.encode(hints),
           "session_score", string.format("%.0f", tonumber(score) - tonumber(ARGV[4])),
           "state_version", string.format("%.0f", tonumber(version) + 1))
return {"new", redis.call("HGETALL", KEYS[2]), value}
"""


_WORD_LIST = TypeAdapter(list[_StoredWord])

_PUBLISHED = _TYPE_CHECK + """
local result = {}
for i = 2, #KEYS, 2 do
    if not valid_type(KEYS[i], "set") or not valid_type(KEYS[i + 1], "hash") then
        return {"corrupt"}
    end
    local has_refs = redis.call("SCARD", KEYS[i]) > 0
    local has_segment = redis.call("EXISTS", KEYS[i + 1]) == 1
    if has_refs ~= has_segment then return {"corrupt"} end
    if has_segment then
        if redis.call("HGET", KEYS[i + 1], "round_id") ~= ARGV[1] then
            return {"corrupt"}
        end
        result[#result + 1] = redis.call("HGET", KEYS[i + 1], "words") or false
    else
        result[#result + 1] = false
    end
end
return {"ok", result}
"""

PublishedWord = _StoredWord


class _Segment(BaseModel):
    route_id: int
    words: list[_StoredWord]
    owners: dict[UUID, UUID]


class GameSegments:
    def __init__(self, sessions: GameSessions) -> None:
        self.sessions = sessions
        self.generation = sessions.generation

    def pending_victories_key(self) -> str:
        return self.generation.key("pending_victories")

    async def confirm_route(
        self,
        user_id: UUID,
        session_id: UUID,
        route_ids: Sequence[int],
        proposals: Mapping[int, Sequence[WordProposal]],
    ) -> PlayerSession:
        """Publish or reuse segments for a route validated by the caller."""
        routes = list(route_ids)
        if not routes or len(routes) != len(set(routes)):
            raise ValueError("A route must contain distinct segments.")
        if any(route_id not in routes for route_id in proposals):
            raise ValueError("Proposals must belong to the requested route.")
        current = await self.sessions._read(user_id)
        if current is None or current[0].id != session_id:
            raise SessionNotFound("The session is not available for this user.")
        session = current[0]
        keys = [self.sessions.session_key(user_id), self.sessions.city_pairs_key()]
        encoded_proposals: list[str | None] = []
        seen: set[UUID] = set()
        for route_id in routes:
            keys.extend(self.sessions.segment_keys(session.round_id, route_id))
            words = proposals.get(route_id)
            if words is None:
                encoded_proposals.append(None)
                continue
            for word in words:
                if word.placement_id in seen:
                    raise ValueError("Placement IDs must be unique within the route.")
                seen.add(word.placement_id)
            encoded_proposals.append(json.dumps(
                [word.stored() for word in words], ensure_ascii=False, separators=(",", ":")
            ))
        result = await self.generation.execute(
            _CONFIRM,
            keys=keys,
            args=[
                str(session_id),
                json.dumps(routes, separators=(",", ":")),
                json.dumps(encoded_proposals, ensure_ascii=False),
                str(user_id),
                str(session.round_id),
            ],
        )
        if not isinstance(result, list) or not result:
            raise SessionStorageError("Invalid Redis response.")
        if result[0] == "ok" and len(result) == 2:
            return self.sessions._decode_session(result[1], user_id)[0]
        if result[0] == "not_found":
            raise SessionNotFound("The session is not available for this user.")
        if result[0] == "route_confirmed":
            raise RouteAlreadyConfirmed("The session already has another route.")
        if result[0] == "solved":
            raise RouteAlreadySolved("The route is already solved.")
        if result[0] == "empty":
            raise CrosswordGenerationFailed("The whole route is empty; participation was released.")
        if result[0] == "missing_proposal":
            raise CrosswordGenerationFailed("A new segment has no crossword proposal.")
        raise SessionStorageError("Invalid shared segment state.")

    async def published_words(
        self, round_id: UUID, route_ids: Sequence[int]
    ) -> dict[int, list[PublishedWord]]:
        """Read the words of already published segments; unpublished routes are omitted."""
        routes = list(dict.fromkeys(route_ids))
        if not routes:
            return {}
        keys: list[str] = []
        for route_id in routes:
            keys.extend(self.sessions.segment_keys(round_id, route_id))
        result = await self.generation.execute(_PUBLISHED, keys=keys, args=[str(round_id)])
        if (
            not isinstance(result, list) or len(result) != 2 or result[0] != "ok"
            or not isinstance(result[1], list) or len(result[1]) != len(routes)
        ):
            raise SessionStorageError("Invalid shared segment state.")
        published: dict[int, list[PublishedWord]] = {}
        try:
            for route_id, encoded in zip(routes, result[1]):
                if encoded is not None:
                    published[route_id] = _WORD_LIST.validate_json(encoded)
        except ValidationError as exc:
            raise SessionStorageError("Invalid shared segment data.") from exc
        return published

    async def _read_segments(
        self, user_id: UUID, session_id: UUID
    ) -> tuple[PlayerSession, str, list[_Segment]]:
        for _ in range(5):
            current = await self.sessions._read(user_id)
            if current is None or current[0].id != session_id:
                raise SessionNotFound("The session is not available for this user.")
            if current[0].state not in ("playing", "finishing"):
                raise SessionNotPlaying(current[0])
            keys = [self.sessions.session_key(user_id)]
            for route_id in current[0].route_ids:
                keys.extend(self.sessions.segment_keys(current[0].round_id, route_id))
            result = await self.generation.execute(
                _READ_SEGMENTS, keys=keys,
                args=[str(session_id), current[1], str(user_id), str(current[0].round_id)],
            )
            if not isinstance(result, list) or not result:
                raise SessionStorageError("Invalid Redis response.")
            if result[0] == "retry":
                continue
            if result[0] == "not_found":
                raise SessionNotFound("The session is not available for this user.")
            if result[0] == "not_playing" and len(result) == 2:
                raise SessionNotPlaying(self.sessions._decode_session(result[1], user_id)[0])
            if result[0] != "ok" or len(result) != 3 or not isinstance(result[2], list):
                raise SessionStorageError("Invalid shared segment state.")
            session = self.sessions._decode_session(result[1], user_id)[0]
            if len(result[2]) != len(session.route_ids):
                raise SessionStorageError("Invalid shared segment count.")
            segments = []
            seen: set[UUID] = set()
            try:
                for route_id, item in zip(session.route_ids, result[2]):
                    if not isinstance(item, list) or len(item) != 2 or not isinstance(item[1], list):
                        raise ValueError("Invalid shared segment response.")
                    words = _WORD_LIST.validate_json(item[0])
                    owners_list = item[1]
                    if len(owners_list) % 2:
                        raise ValueError("Invalid segment owners.")
                    owners = {
                        UUID(owners_list[index]): UUID(owners_list[index + 1])
                        for index in range(0, len(owners_list), 2)
                    }
                    for word in words:
                        if word.placement_id in seen:
                            raise ValueError("Duplicate placement.")
                        seen.add(word.placement_id)
                    segments.append(_Segment(route_id=route_id, words=words, owners=owners))
            except (TypeError, ValueError, ValidationError) as exc:
                raise SessionStorageError("Invalid shared segment data.") from exc
            return session, current[1], segments
        raise SessionStorageError("The route changed repeatedly while reading.")

    async def crossword(
        self, user_id: UUID, session_id: UUID
    ) -> tuple[PlayerSession, list[Placement]]:
        try:
            session, _, live_segments = await self._read_segments(user_id, session_id)
            segments: Sequence[_Segment | _SnapshotSegment] = live_segments
        except SessionNotPlaying as exc:
            if exc.session.state != "won":
                raise
            session = exc.session
            segments = VictorySnapshot.from_session(session).segments
        placements = [
            Placement(
                placement_id=word.placement_id,
                route_id=segment.route_id,
                offset_px=word.offset_px,
                letter_count=word.letter_count,
                definition=word.definition,
                is_guessed=word.solved_by is not None,
                guessed_by_me=word.solved_by == user_id,
                word_text=word.word_text if word.solved_by is not None else None,
                hints=session.hints.get(str(word.placement_id), {}),
            )
            for segment in segments for word in segment.words
        ]
        return session, placements

    async def _locate(
        self, user_id: UUID, session_id: UUID, placement_id: UUID
    ) -> tuple[PlayerSession, str, _Segment]:
        session, routes_json, segments = await self._read_segments(user_id, session_id)
        if session.state != "playing":
            raise SessionNotPlaying(session)
        for segment in segments:
            if any(word.placement_id == placement_id for word in segment.words):
                return session, routes_json, segment
        raise PlacementNotFound("The placement does not belong to this session.")

    async def guess(
        self, user_id: UUID, session_id: UUID, placement_id: UUID, answer: str
    ) -> GuessResult:
        for _ in range(5):
            session, routes_json, segment = await self._locate(user_id, session_id, placement_id)
            owners = list(segment.owners.items())
            current = await asyncio.gather(
                *(self.sessions._read(owner) for _, owner in owners)
            )
            if any(item is None or item[0].id != pair[0] for pair, item in zip(owners, current)):
                continue
            refs, state = self.sessions.segment_keys(session.round_id, segment.route_id)
            keys = [
                self.sessions.session_key(user_id), refs, state,
                self.pending_victories_key(),
            ]
            key_indexes = {key: index + 2 for index, key in enumerate(keys)}

            def index_for(key: str) -> int:
                if key not in key_indexes:
                    key_indexes[key] = len(keys) + 2
                    keys.append(key)
                return key_indexes[key]

            plan = []
            for (member_id, owner), item in zip(owners, current):
                if item is None:
                    raise SessionStorageError("A shared session disappeared.")
                other, other_routes_json = item
                if other.round_id != session.round_id:
                    raise SessionStorageError("A shared segment contains a member from another round.")
                routes = []
                for route_id in other.route_ids:
                    route_refs, route_state = self.sessions.segment_keys(session.round_id, route_id)
                    routes.append({
                        "id": str(route_id), "refs": index_for(route_refs),
                        "state": index_for(route_state),
                    })
                plan.append({
                    "id": str(member_id), "user_id": str(owner),
                    "key": index_for(self.sessions.session_key(owner)),
                    "route_ids": other_routes_json, "routes": routes,
                    "started_unix": other.started_at.timestamp(),
                })
            result = await self.generation.execute(
                _GUESS, keys=keys,
                args=[
                    str(session_id), str(placement_id), normalize_answer(answer),
                    json.dumps(plan, separators=(",", ":")), routes_json,
                    str(user_id), str(segment.route_id),
                    str(session.round_id),
                ],
            )
            if not isinstance(result, list) or not result:
                raise SessionStorageError("Invalid Redis response.")
            if result[0] == "retry":
                continue
            if result[0] == "not_found":
                raise SessionNotFound("The session is not available for this user.")
            if result[0] == "not_playing" and len(result) == 2:
                raise SessionNotPlaying(self.sessions._decode_session(result[1], user_id)[0])
            if result[0] in ("incorrect", "correct", "already_solved") and len(result) >= 2:
                session = self.sessions._decode_session(result[1], user_id)[0]
                if result[0] == "correct" and len(result) == 4:
                    return GuessResult(session=session, result="correct", points_awarded=int(result[2]), revealed_word=result[3])
                if result[0] == "already_solved" and len(result) == 3:
                    return GuessResult(session=session, result="already_solved", points_awarded=0, revealed_word=result[2])
                if result[0] == "incorrect" and len(result) == 2:
                    return GuessResult(session=session, result="incorrect", points_awarded=0)
            raise SessionStorageError("Invalid shared segment state.")
        raise SessionStorageError("Shared segment membership changed repeatedly.")

    async def hint(
        self, user_id: UUID, session_id: UUID, placement_id: UUID,
        hint_type: Literal["first_letter", "last_letter"], cost: int,
    ) -> HintResult:
        if hint_type not in ("first_letter", "last_letter") or type(cost) is not int or not 0 <= cost <= 2**31 - 1:
            raise ValueError("Invalid hint type or cost.")
        for _ in range(5):
            session, routes_json, segment = await self._locate(user_id, session_id, placement_id)
            refs, state = self.sessions.segment_keys(session.round_id, segment.route_id)
            result = await self.generation.execute(
                _HINT,
                keys=[self.sessions.session_key(user_id), refs, state],
                args=[
                    str(session_id), str(placement_id), hint_type, cost,
                    routes_json, str(user_id), str(session.round_id),
                ],
            )
            if not isinstance(result, list) or not result:
                raise SessionStorageError("Invalid Redis response.")
            if result[0] == "retry":
                continue
            if result[0] == "not_found":
                raise SessionNotFound("The session is not available for this user.")
            if result[0] == "not_playing" and len(result) == 2:
                raise SessionNotPlaying(self.sessions._decode_session(result[1], user_id)[0])
            if result[0] == "insufficient":
                raise InsufficientPoints("The session has insufficient points.")
            if result[0] == "unknown" and len(result) == 2:
                return HintResult(
                    session=self.sessions._decode_session(result[1], user_id)[0],
                    hint_type="unknown", value=None, points_charged=0,
                )
            if result[0] in ("new", "repeat") and len(result) == 3:
                return HintResult(
                    session=self.sessions._decode_session(result[1], user_id)[0],
                    hint_type=hint_type, value=result[2],
                    points_charged=cost if result[0] == "new" else 0,
                )
            raise SessionStorageError("Invalid shared segment state.")
        raise SessionStorageError("Shared segment membership changed repeatedly.")
