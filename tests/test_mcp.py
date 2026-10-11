"""MCP (round 4c): per-user tokens, reading and starting tools, own recordings, rate limit."""
import json
import re

from conftest import claim, pair_runner, post, runner_headers, transcript_result
from test_library import deliver, submit, translate_next

BASE = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18"}


def make_token(client, name="Teszt-kliens", can_start=False, include_private=False) -> str:
    data = {"name": name}
    if can_start:
        data["can_start"] = "on"
    if include_private:
        data["include_private"] = "on"
    r = post(client, "/beallitasok/mcp", data)
    assert r.status_code == 200, r.text
    return re.search(r'<code class="token">([^<]+)</code>', r.text).group(1)


def rpc(c, token, method, params=None, rid=1):
    body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
    return c.post("/mcp", content=json.dumps(body), headers={**BASE, "Authorization": f"Bearer {token}"})


def call(c, token, name, **args):
    r = rpc(c, token, "tools/call", {"name": name, "arguments": args})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def text_of(result) -> str:
    return "\n".join(x["text"] for x in result["content"] if x["type"] == "text")


def test_tokens_are_per_user_hashed_and_revocable(alice, bob, anon):
    token = make_token(alice)
    assert token.startswith("dwyt_mcp_")
    page = alice.get("/beallitasok").text
    assert "Teszt-kliens" in page and token not in page  # shown once
    assert "Teszt-kliens" not in bob.get("/beallitasok").text
    from central import db
    from sqlalchemy import text
    with db.system_session() as s:
        assert token not in str(s.execute(text("SELECT token_hash FROM mcp_tokens")).scalar())
    r = rpc(anon, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "teszt", "version": "1"}})
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "dwyt"
    assert rpc(anon, "dwyt_mcp_rossz", "tools/list").status_code == 401
    no_auth = anon.post("/mcp", content="{}", headers=BASE)
    assert no_auth.status_code == 401 and no_auth.headers["www-authenticate"].startswith("Bearer")
    token_id = re.search(r'/beallitasok/mcp/(\d+)/visszavonas', page).group(1)
    assert post(alice, f"/beallitasok/mcp/{token_id}/visszavonas").status_code == 303
    assert rpc(anon, token, "tools/list").status_code == 401


def test_reading_tools_see_only_the_tokens_user(alice, bob, anon):
    a = pair_runner(alice, anon)
    item_id = submit(alice, summarize=True)
    deliver(anon, a, language="en", texts=("Good morning.", "We plant tulips in the garden."))
    job = claim(anon, a).json()
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", headers=runner_headers(a),
             json={"text": "## Összefoglaló\nTulipánültetés [00:05].", "model": "m", "language": "hu",
                   "prompt_version": 1})
    translate_next(anon, a)
    from central import indexing
    indexing.process(item_id)
    token = make_token(alice)
    tools = {t["name"] for t in rpc(anon, token, "tools/list").json()["result"]["tools"]}
    assert tools == {"kereses", "atirat", "osszefoglalo", "legutobbiak"}  # reading only
    found = call(anon, token, "kereses", kerdes="tulips")["structuredContent"]
    assert found["talalatok_szama"] == 1 and found["elemek"][0]["elem_id"] == item_id
    hit = next(h for h in found["elemek"][0]["talalatok"] if h["resz"] == "átirat")
    assert hit["video_link"].startswith("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=")
    both = text_of(call(anon, token, "atirat", elem_id=item_id, nyelv="mindketto"))
    assert "Good morning." in both and "[magyarul] HU Good morning." in both
    assert f"Megnyitás: http://testserver/elem/{item_id}" in both
    assert "Tulipánültetés" in text_of(call(anon, token, "osszefoglalo", elem_id=item_id))
    recent = call(anon, token, "legutobbiak")["structuredContent"]["elemek"]
    assert recent[0]["cim"] == "Kerti videó" and recent[0]["van_osszefoglalo"] is True

    other = make_token(bob)
    result = call(anon, other, "atirat", elem_id=item_id)
    assert result["isError"] and "Nincs" in text_of(result)  # not bob's item
    assert call(anon, other, "kereses", kerdes="tulips")["structuredContent"]["talalatok_szama"] == 0


def test_starting_tools_need_permission_and_follow_the_rules(alice, anon):
    runner = pair_runner(alice, anon)
    token = make_token(alice, "OpenCode", can_start=True)
    tools = {t["name"]: t for t in rpc(anon, token, "tools/list").json()["result"]["tools"]}
    assert "CSAK ha a felhasználó kifejezetten kéri" in " ".join(tools["atiras_inditasa"]["description"].split())
    started = call(anon, token, "atiras_inditasa",
                   url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")["structuredContent"]
    assert started["allapot"] == "Sorban"
    job = claim(anon, runner).json()
    assert job["input"]["options"]["mode"] == "captions"
    status = call(anon, token, "munka_allapota", munka_id=started["munka_id"])["structuredContent"]
    assert status["allapot"] == "Fut"
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=transcript_result(), headers=runner_headers(runner))
    again = call(anon, token, "atiras_inditasa", elem_id=started["elem_id"], mod="cloud", fo_atirat=True)
    assert again["structuredContent"]["munka_id"] != started["munka_id"]
    bad = call(anon, token, "atiras_inditasa", url="https://example.com/x")  # captions only for YouTube
    assert bad["isError"]
    bad = call(anon, token, "atiras_inditasa", url="https://www.youtube.com/watch?v=aaaaaaaaaaa", mod="local",
               modell="tiny")
    assert bad["isError"] and "Whisper-modell" in text_of(bad)


def test_own_recordings_only_with_permission(alice, anon):
    from test_ownfiles import FILE_CAPS, upload
    from test_ownfiles import deliver as deliver_file
    token_runner = pair_runner(alice, anon)
    r = post(alice, "/uj", {"source": "upload", "staging_id": str(upload(alice).json()["staging_id"]),
                            "mode": "local", "model_local": "large-v3"})
    item_id = int(r.headers["location"].rsplit("/", 1)[1])
    deliver_file(anon, token_runner, claim(anon, token_runner, FILE_CAPS).json())
    hidden = make_token(alice)
    assert call(anon, hidden, "atirat", elem_id=item_id)["isError"]
    assert call(anon, hidden, "legutobbiak")["structuredContent"]["elemek"] == []
    shown = make_token(alice, "Helyi", include_private=True)
    assert "Interjú 2026.wav" in text_of(call(anon, shown, "atirat", elem_id=item_id))


def test_mcp_calls_are_rate_limited_per_token(alice, anon, monkeypatch):
    from central import ratelimit
    monkeypatch.setitem(ratelimit.LIMITS, "mcp", (3, 300))
    token = make_token(alice)
    codes = [rpc(anon, token, "tools/list").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_the_official_mcp_client_works(alice, anon):
    """End to end with the MCP SDK's own client over real HTTP (as Hermes and OpenCode connect)."""
    import asyncio
    import threading
    import time

    import uvicorn
    from fakes import free_port
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    from central.web import app
    runner = pair_runner(alice, anon)
    submit(alice)
    deliver(anon, runner)
    token = make_token(alice)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)

        async def main():
            http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
            async with http, Client(streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http)) as client:
                tools = await client.list_tools()
                result = await client.call_tool("legutobbiak", {"darab": 3})
                return [t.name for t in tools.tools], result
        names, result = asyncio.run(main())
        assert "kereses" in names and "Kerti videó" in json.dumps(result.structured_content, ensure_ascii=False)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
