"""Auto-capture notes a NEW registration once, not every later update.

Regression: every service_add/mqtt_add/... call staged a memory candidate, so
updating a service after its candidate was reviewed and rejected put it right
back into the review queue.
"""
import asyncio
import sys

from fastmcp import Client


def test_update_of_existing_service_stages_no_candidate(fresh_server):
    async def go():
        async with Client(fresh_server.mcp) as client:
            await client.call_tool("service_add", {"name": "birdhouse", "base_url": "http://nas:8100",
                                                   "category": "Birdhouse"})
            memory = sys.modules["memory"]
            first = memory.candidate_count()
            names = [p.stem for p in (memory.MEMORY_DIR / memory.CANDIDATES_SCOPE).glob("*.md")]
            for n in names:
                await client.call_tool("memory_reject", {"name": n})
            await client.call_tool("service_add", {"name": "birdhouse", "base_url": "http://nas:8100",
                                                   "description": "CAD and slicing"})
            return first, memory.candidate_count()

    first, after_update = asyncio.run(go())
    assert first == 1
    assert after_update == 0
