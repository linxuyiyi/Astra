import asyncio

from app.limiter import RateLimiter


def test_rate_limiter_counts_and_resets():
    async def run():
        limiter = RateLimiter(limit=2, window_seconds=60)
        assert await limiter.check("a") is None
        assert await limiter.check("a") is None
        wait = await limiter.check("a")
        assert wait is not None and wait > 0
        assert await limiter.check("b") is None

    asyncio.run(run())


def test_rate_limiter_disabled():
    async def run():
        limiter = RateLimiter(limit=0)
        for _ in range(10):
            assert await limiter.check("a") is None

    asyncio.run(run())
