"""Tool schemas say exactly what a model must send — fewer wasted round trips.

- memory_write: `type` used to look optional ("") and was then refused; it is
  now required and advertises its four values.
- call_service: JSON arrays could not be sent (dict only), and long responses
  were cut at 4,000 characters with no way to read on.
"""
import asyncio
import sys

from fastmcp import Client


def _tools(server):
    async def go():
        async with Client(server.mcp) as client:
            return {t.name: t for t in await client.list_tools()}
    return asyncio.run(go())


def _call(server, tool, args):
    async def go():
        async with Client(server.mcp) as client:
            return await client.call_tool(tool, args, raise_on_error=False)
    return asyncio.run(go())


def test_memory_write_type_is_required_with_its_values(fresh_server):
    schema = _tools(fresh_server)["memory_write"].input_schema
    assert "type" in schema["required"]
    assert schema["properties"]["type"]["enum"] == ["user", "feedback", "project", "reference"]
    # synonyms keep working for clients that don't validate enums
    res = _call(fresh_server, "memory_write", {"title": "t", "content": "c", "type": "preference"})
    assert "[user]" in res.content[0].text


def _fake_service(monkeypatch, server, body_text):
    sys.modules["services"]._load = lambda name: {"name": name, "base_url": "http://svc.test"}
    netguard = sys.modules["netguard"]
    monkeypatch.setattr(netguard, "check_url", lambda url: (True, ""))
    sent = {}

    class Resp:
        status_code = 200
        text = body_text

    import httpx
    monkeypatch.setattr(httpx, "request", lambda m, url, **kw: sent.update(kw) or Resp())
    return sent


def test_call_service_sends_json_arrays(monkeypatch, fresh_server):
    sent = _fake_service(monkeypatch, fresh_server, "ok")
    _call(fresh_server, "call_service", {"service": "ha", "path": "/api/x", "method": "POST",
                                         "json_body": [{"entity_id": "light.a"}, {"entity_id": "light.b"}]})
    assert sent["json"] == [{"entity_id": "light.a"}, {"entity_id": "light.b"}]


def test_call_service_long_response_can_be_read_on(monkeypatch, fresh_server):
    body = "".join(f"{i:05d}," for i in range(4000))          # 24,000 characters
    _fake_service(monkeypatch, fresh_server, body)
    first = _call(fresh_server, "call_service", {"service": "ha", "path": "/big"}).content[0].text
    assert "offset=" in first
    offset = int(first.rsplit("offset=", 1)[1].split(")")[0].split()[0].rstrip(".,"))
    second = _call(fresh_server, "call_service", {"service": "ha", "path": "/big",
                                                  "offset": offset}).content[0].text
    assert body[offset:offset + 50] in second
