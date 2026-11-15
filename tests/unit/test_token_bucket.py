"""The shared throttle must attribute each correction to its own reservation.

The bid graph fans out three analyses in one superstep, so three reservations
are outstanding before any response lands. An earlier version corrected
`self._tokens[-1]`, which meant the first response to return rewrote the last
reservation made -- a different request's -- and pruning could shift that onto
a request from a later superstep entirely.

That matters because this bucket is the only thing standing between the
fan-out and a free tier's per-minute ceiling. Accounting that drifts makes the
throttle admit requests it should hold, and the provider answers with 429s.
"""

from __future__ import annotations

import pytest

from agent.llm import TokenBucket


def _spent(bucket: TokenBucket) -> float:
    return sum(cell[1] for cell in bucket._tokens)


class TestReservationAttribution:
    @pytest.mark.asyncio
    async def test_a_correction_lands_on_its_own_reservation(self):
        """Three outstanding reservations, the first to return corrects itself."""
        bucket = TokenBucket(requests_per_minute=100, tokens_per_minute=1_000_000)

        analyst = await bucket.acquire(2000)
        risk = await bucket.acquire(1500)
        fit = await bucket.acquire(1800)

        # The analyst responds first, using far less than reserved.
        bucket.record_actual(analyst, 900)

        assert analyst[1] == 900, "the analyst's own reservation was not corrected"
        assert risk[1] == 1500, "risk_assessor's reservation was altered by another call"
        assert fit[1] == 1800, "fit_matcher's reservation was altered by another call"
        assert _spent(bucket) == 900 + 1500 + 1800

    @pytest.mark.asyncio
    async def test_corrections_out_of_order_all_land_correctly(self):
        bucket = TokenBucket(requests_per_minute=100, tokens_per_minute=1_000_000)
        a = await bucket.acquire(2000)
        b = await bucket.acquire(2000)
        c = await bucket.acquire(2000)

        bucket.record_actual(c, 300)
        bucket.record_actual(a, 400)
        bucket.record_actual(b, 500)

        assert [a[1], b[1], c[1]] == [400, 500, 300]
        assert _spent(bucket) == 1200

    @pytest.mark.asyncio
    async def test_correcting_a_pruned_reservation_is_harmless(self):
        """A response that arrives after its minute has rolled off must not crash."""
        bucket = TokenBucket(requests_per_minute=100, tokens_per_minute=1_000_000)
        stale = await bucket.acquire(2000)
        bucket._tokens.clear()  # simulate the window rolling over

        bucket.record_actual(stale, 700)  # must not raise
        assert _spent(bucket) == 0

    @pytest.mark.asyncio
    async def test_none_reservation_is_ignored(self):
        """An offline call reserves nothing and must still be safe to record."""
        bucket = TokenBucket(requests_per_minute=100, tokens_per_minute=1_000_000)
        bucket.record_actual(None, 1234)
        assert _spent(bucket) == 0

    @pytest.mark.asyncio
    async def test_the_bucket_still_blocks_when_the_budget_is_spent(self):
        """The fix must not loosen the throttle it exists to enforce."""
        import asyncio

        bucket = TokenBucket(requests_per_minute=100, tokens_per_minute=3000)
        await bucket.acquire(2500)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bucket.acquire(1000), timeout=0.3)
