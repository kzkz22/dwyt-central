"""What runs behind the runner's model names: Runners page and model pickers (decision 17.4)."""
from conftest import CAPS, claim, pair_runner, post
from sqlalchemy import text
from test_library import deliver, submit

KERDES = {"where": "local", "target": "Qwen3.6-35B-A3B-Gnrl", "target_label": "Qwen3.6-35B-A3B, általános beállítás",
          "context": 262144, "vision": True, "thinking": True, "thinking_budget": 8192,
          "description": "Kérdések a videókról és a könyvtárról, alapos válasz.",
          "fallback": [{"name": "cloud/qwen3.8-flash", "target_label": "Qwen3.8 Flash", "where": "cloud",
                        "provider": "OpenRouter", "thinking": True}]}
OSSZ = {"where": "local", "target_label": "Qwen3.6-35B-A3B, általános beállítás", "thinking": False,
        "description": "Összefoglaló és fordítás.",
        "fallback": [{"name": "cloud/qwen3.8-flash-gyors", "target_label": "Qwen3.8 Flash", "where": "cloud",
                      "thinking": False}]}
HELYI = {"where": "local", "target_label": "Qwen3.6-35B-A3B, általános beállítás", "thinking": False,
         "description": "Saját felvételek, felhő nélkül."}

INFO_CAPS = {**CAPS, "kinds": [*CAPS["kinds"], "ask"],
             "llm_models": {"public": ["osszefoglalo", "kerdes"], "private": ["osszefoglalo-helyi"]},
             "llm_model_info": {"kerdes": KERDES, "osszefoglalo": OSSZ, "osszefoglalo-helyi": HELYI},
             "llm_server": "LiteLLM",
             "llm_defaults": {"summarize": "osszefoglalo", "translate": "osszefoglalo", "ask": "kerdes",
                              "private": "osszefoglalo-helyi"}}


def stored_caps():
    from central import db
    with db.system_session() as s:
        return s.execute(text("SELECT capabilities FROM runners")).scalar_one()


def test_runners_page_shows_what_runs_behind_each_name(alice, anon):
    token = pair_runner(alice, anon)
    assert claim(anon, token, INFO_CAPS).status_code == 204
    page = alice.get("/futtatok").text
    assert "LLM-szerver: LiteLLM" in page and "Nyelvi modellek" in page
    assert "Qwen3.6-35B-A3B, általános beállítás" in page and "262 144 token kontextus" in page
    assert "képet is ért" in page and "legfeljebb 8 192 token" in page
    assert "alap: kérdések" in page and "alap: összefoglaló" in page and "saját felvételek" in page
    assert "Qwen3.8 Flash" in page and "OpenRouter" in page
    assert "csak helyi" in page  # osszefoglalo-helyi has no cloud fallback
    assert "Kérdések a videókról és a könyvtárról" in page
    assert "régebbi" not in page


def test_old_runner_without_model_info_gets_a_hint(alice, anon):
    token = pair_runner(alice, anon)
    claim(anon, token, {**CAPS, "llm_models": {"public": ["osszefoglalo"], "private": []}})
    page = alice.get("/futtatok").text
    assert "Ez a futtató régebbi" in page and "uv tool install --force" in page
    assert "a futtató nem küldött részleteket" in page


def test_model_info_is_reduced_to_known_fields(alice, anon):
    token = pair_runner(alice, anon)
    junk = {**KERDES, "api_key": "sk-secret", "api_base": "http://192.168.1.20:8001", "context": "sok",
            "where": "mars", "fallback": [{**KERDES["fallback"][0], "api_key": "sk-2"}, "x"]}
    assert claim(anon, token, {**INFO_CAPS, "llm_model_info": {"kerdes": junk}}).status_code == 204
    info = stored_caps()["llm_model_info"]["kerdes"]
    assert "api_key" not in info and "api_base" not in info and "context" not in info and "where" not in info
    assert info["fallback"] == [{"name": "cloud/qwen3.8-flash", "target_label": "Qwen3.8 Flash", "where": "cloud",
                                 "provider": "OpenRouter", "thinking": True}]
    assert "sk-secret" not in alice.get("/futtatok").text


def test_oversized_model_info_is_refused(alice, anon):
    token = pair_runner(alice, anon)
    many = {f"m{i}": {"description": "x"} for i in range(41)}
    assert claim(anon, token, {**INFO_CAPS, "llm_model_info": many}).status_code == 422
    big = {f"m{i}": {"description": "x" * 1200} for i in range(39)}
    assert claim(anon, token, {**INFO_CAPS, "llm_model_info": big}).status_code == 422


def test_pickers_say_what_the_name_means(alice, anon):
    token = pair_runner(alice, anon)
    claim(anon, token, INFO_CAPS)
    item_id = submit(alice)
    deliver(anon, token)
    assert claim(anon, token, INFO_CAPS).status_code == 204
    page = alice.get(f"/elem/{item_id}").text
    # chat: the default is the runner's ask model, and each option says what runs behind it
    assert "A futtató alapértelmezése (kerdes)" in page
    assert "kerdes – Qwen3.6-35B-A3B, általános beállítás, gondolkodással" in page
    assert "osszefoglalo – Qwen3.6-35B-A3B, általános beállítás, gondolkodás nélkül" in page
    assert 'data-hint-target="chat-model-hint"' in page and 'id="chat-model-hint"' in page
    assert "Ha a GPU-gép ki van kapcsolva: felhőben válaszol: Qwen3.8 Flash, gondolkodással." in page
    # the summary picker defaults to the summary model
    assert "A futtató alapértelmezése (osszefoglalo)" in page and 'id="summary-model-hint"' in page


def test_own_recording_pickers_use_the_private_default(alice, anon):
    from central import db
    token = pair_runner(alice, anon)
    claim(anon, token, INFO_CAPS)
    item_id = submit(alice)
    deliver(anon, token)
    assert claim(anon, token, INFO_CAPS).status_code == 204
    with db.system_session() as s:
        s.execute(text("UPDATE items SET is_public = false WHERE id = :i"), {"i": item_id})
    page = alice.get(f"/elem/{item_id}").text
    assert "A futtató alapértelmezése (osszefoglalo-helyi)" in page
    assert "felhőbe nem kerül" in page
    assert "kerdes – " not in page
    r = post(alice, f"/elem/{item_id}/osszefoglalo", {"model": "kerdes"})
    assert r.status_code in (400, 422)
