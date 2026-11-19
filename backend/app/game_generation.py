import argparse
import asyncio
from collections.abc import Sequence
from inspect import isawaitable
from uuid import UUID, uuid4

from loguru import logger
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError
from pydantic import SecretStr

from app.config import RedisConnectionSettings


GAME_NAMESPACE = "railway:{game}"
MAX_CLEANUP_KEYS = 1000
BOOTSTRAP_LOCK_SECONDS = 60

_BOOTSTRAP_GENERATION_SCRIPT = """
if redis.call("GET", KEYS[2]) ~= ARGV[2] then
    return redis.error_reply("GENERATION_BOOTSTRAP_LOCK_LOST")
end
local previous = redis.call("GET", KEYS[1])
redis.call("SET", KEYS[1], ARGV[1])
redis.call("DEL", KEYS[2])
return previous or ""
"""

_RELEASE_LOCK_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""

_FENCED_SCRIPT_PREFIX = """
if redis.call("GET", KEYS[1]) ~= ARGV[#ARGV] then
    return redis.error_reply("STALE_GAME_GENERATION")
end
"""


class GameGenerationError(RuntimeError):
    """Base error for unavailable or unsafe game-generation operations."""


class GameGenerationUnavailable(GameGenerationError):
    """Raised when Redis has no valid active generation."""


class StaleGameGeneration(GameGenerationError):
    """Raised when a worker attempts to use a superseded generation."""


class GenerationBootstrapInProgress(GameGenerationError):
    """Raised when another deployment process is changing the generation."""


def create_redis_client(redis_url: SecretStr | None) -> Redis:
    if redis_url is None:
        raise ValueError("REDIS_URL must be configured to use Redis.")
    return Redis.from_url(redis_url.get_secret_value(), decode_responses=True)


async def _evaluate_redis_script(
    redis: Redis,
    script: str,
    numkeys: int,
    keys_and_args: Sequence[str | int],
) -> object:
    result = redis.eval(
        script,
        numkeys,
        *(str(value) for value in keys_and_args),
    )
    if not isawaitable(result):
        raise TypeError("Game-generation operations require an async Redis client.")
    return await result


class GameGeneration:
    def __init__(
        self,
        redis: Redis,
        generation_id: str,
        *,
        namespace: str = GAME_NAMESPACE,
    ) -> None:
        self.redis = redis
        self.generation_id = generation_id
        self.namespace = namespace

    @property
    def current_generation_key(self) -> str:
        return f"{self.namespace}:current_generation"

    @property
    def generation_key_prefix(self) -> str:
        return f"{self.namespace}:generation:{self.generation_id}:"

    @classmethod
    async def load_current(
        cls,
        redis: Redis,
        *,
        namespace: str = GAME_NAMESPACE,
    ) -> "GameGeneration":
        await redis.ping()
        generation_value = await redis.get(f"{namespace}:current_generation")
        if generation_value is None:
            raise GameGenerationUnavailable(
                "No active game generation. Run the deployment bootstrap step first."
            )

        generation_id = str(generation_value)
        try:
            UUID(generation_id)
        except ValueError as exc:
            raise GameGenerationUnavailable(
                "The active game generation ID is invalid."
            ) from exc

        return cls(redis, generation_id, namespace=namespace)

    def key(self, suffix: str) -> str:
        if not suffix or suffix.startswith(":"):
            raise ValueError("A Redis game-key suffix must be non-empty.")
        return f"{self.generation_key_prefix}{suffix}"

    async def execute(
        self,
        script: str,
        *,
        keys: Sequence[str] = (),
        args: Sequence[str | int] = (),
    ) -> object:
        """Run a Redis script only while this worker's generation is current."""
        if any(not key.startswith(self.generation_key_prefix) for key in keys):
            raise ValueError("Game Redis keys must belong to this worker generation.")

        fenced_script = f"{_FENCED_SCRIPT_PREFIX}\n{script}"
        try:
            return await _evaluate_redis_script(
                self.redis,
                fenced_script,
                len(keys) + 1,
                (
                    self.current_generation_key,
                    *keys,
                    *args,
                    self.generation_id,
                ),
            )
        except ResponseError as exc:
            if "STALE_GAME_GENERATION" in str(exc):
                raise StaleGameGeneration(
                    "This worker belongs to a superseded game generation."
                ) from exc
            raise

    async def assert_current(self) -> None:
        await self.execute("return 1")


async def cleanup_generation_keys(
    redis: Redis,
    *,
    namespace: str,
    generation_id: str,
    max_keys: int = MAX_CLEANUP_KEYS,
) -> int:
    if max_keys < 1:
        raise ValueError("max_keys must be positive.")

    old_prefix = f"{namespace}:generation:{generation_id}:"
    deleted = 0
    async for key in redis.scan_iter(match=f"{old_prefix}*", count=100):
        if not str(key).startswith(old_prefix):
            continue
        await redis.delete(key)
        deleted += 1
        if deleted >= max_keys:
            logger.warning(
                "Stopped game-generation cleanup at the {}-key safety limit",
                max_keys,
            )
            break
    return deleted


async def bootstrap_generation(
    redis: Redis,
    *,
    namespace: str = GAME_NAMESPACE,
    max_cleanup_keys: int = MAX_CLEANUP_KEYS,
) -> str:
    """Publish a new generation once per deployment bootstrap invocation."""
    lock_key = f"{namespace}:bootstrap_lock"
    current_key = f"{namespace}:current_generation"
    lock_token = str(uuid4())
    generation_id = str(uuid4())

    acquired = await redis.set(
        lock_key,
        lock_token,
        nx=True,
        ex=BOOTSTRAP_LOCK_SECONDS,
    )
    if not acquired:
        raise GenerationBootstrapInProgress(
            "Another process is already bootstrapping a game generation."
        )

    published = False
    previous_generation: str | None = None
    try:
        previous_value = await _evaluate_redis_script(
            redis,
            _BOOTSTRAP_GENERATION_SCRIPT,
            2,
            (current_key, lock_key, generation_id, lock_token),
        )
        published = True
        previous_generation = str(previous_value) if previous_value else None
    except ResponseError as exc:
        if "GENERATION_BOOTSTRAP_LOCK_LOST" in str(exc):
            raise GenerationBootstrapInProgress(
                "The generation bootstrap lock expired before publication."
            ) from exc
        raise
    finally:
        if not published:
            await _evaluate_redis_script(
                redis,
                _RELEASE_LOCK_SCRIPT,
                1,
                (lock_key, lock_token),
            )

    if previous_generation is not None:
        await cleanup_generation_keys(
            redis,
            namespace=namespace,
            generation_id=previous_generation,
            max_keys=max_cleanup_keys,
        )

    logger.info("Bootstrapped game generation {}", generation_id)
    return generation_id


async def _run_bootstrap() -> None:
    settings = RedisConnectionSettings()
    redis = create_redis_client(settings.redis_url)
    try:
        await redis.ping()
        await bootstrap_generation(redis)
    except RedisError:
        logger.exception("Game-generation bootstrap failed: Redis is unavailable")
        raise
    finally:
        await redis.aclose()


def main() -> None:
    argparse.ArgumentParser(
        description="Create a new Railway game generation in Redis."
    ).parse_args()
    asyncio.run(_run_bootstrap())


if __name__ == "__main__":
    main()
