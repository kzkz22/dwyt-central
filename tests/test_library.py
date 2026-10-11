"""Translations, the search index and search (round 3a)."""
from conftest import CAPS, claim, pair_runner, post, runner_headers, transcript_result
from sqlalchemy import text


def submit(client, video="dQw4w9WgXcQ", summarize=False, mode="captions"):
    data = {"url": f"https://www.youtube.com/watch?v={video}", "mode": mode}
    if summarize:
        data["summarize"] = "on"
    r = post(client, "/uj", data)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def deliver(anon, token, **result_kw):
    job = claim(anon, token).json()
    assert job["kind"] == "transcribe", job
    r = anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=transcript_result(**result_kw),
                 headers=runner_headers(token))
    assert r.status_code == 200, r.text
    return job


def translate_next(anon, token, prefix="HU"):
    job = claim(anon, token).json()
    assert job["kind"] == "translate", job
    lines = [{"idx": s["idx"], "text": f"{prefix} {s['text']}"} for s in job["input"]["segments"]]
    r = anon.put(f"/runner/v1/jobs/{job['job_id']}/result",
                 json={"language": "hu", "model": "forditas", "prompt_version": 1, "segments": lines, "missing": 0},
                 headers=runner_headers(token))
    assert r.status_code == 200, r.text
    return job


def test_english_transcript_is_translated_and_shown_side_by_side(alice, anon):
    token = pair_runner(alice, anon)
    item_id = submit(alice)
    deliver(anon, token, language="en", texts=("Good morning.", "Today we talk about the garden."))
    job = claim(anon, token).json()
    assert job["kind"] == "translate"
    assert job["input"]["source_language"] == "en" and job["input"]["role"] == "public"
    assert [s["idx"] for s in job["input"]["segments"]] == [0, 1]
    r = anon.put(f"/runner/v1/jobs/{job['job_id']}/result",
                 json={"language": "hu", "model": "forditas", "prompt_version": 1, "missing": 0,
                       "segments": [{"idx": 0, "text": "Jó reggelt."}, {"idx": 1, "text": "Ma a kertről beszélünk."},
                                    {"idx": 99, "text": "nem létező sor"}]},
                 headers=runner_headers(token))
    assert r.status_code == 200, r.text
    page = alice.get(f"/elem/{item_id}").text
    assert "Good morning." in page and "Jó reggelt." in page and "nem létező sor" not in page
    assert 'class="active">Mindkettő' in page
    only_hu = alice.get(f"/elem/{item_id}?nezet=magyar").text
    assert "Jó reggelt." in only_hu and "Good morning." not in only_hu
    assert "Fordítás" in alice.get("/munkak/lista").text


def test_hungarian_is_not_translated_and_the_setting_switches_auto_translation_off(alice, anon):
    token = pair_runner(alice, anon)
    submit(alice)
    deliver(anon, token)  # Hungarian
    assert claim(anon, token).status_code == 204
    r = post(alice, "/beallitasok", {})  # checkbox off
    assert r.status_code == 200 and "Mentve" in r.text
    item_id = submit(alice, video="aaaaaaaaaaa")
    deliver(anon, token, language="en", texts=("Hello.",))
    assert claim(anon, token).status_code == 204  # no automatic translation
    assert "Magyar fordítás kérése" in alice.get(f"/elem/{item_id}").text
    assert post(alice, f"/elem/{item_id}/forditas").status_code == 303
    translate_next(anon, token)
    assert "HU Hello." in alice.get(f"/elem/{item_id}").text


def test_runner_without_translate_gets_no_translation_job(alice, anon):
    token = pair_runner(alice, anon)
    submit(alice)
    deliver(anon, token, language="en", texts=("Hello.",))
    caps = {**CAPS, "kinds": ["transcribe", "summarize", "something-new"]}
    assert claim(anon, token, caps).status_code == 204  # unknown kinds are fine, translate is not offered


# ---- search -------------------------------------------------------------------------------

def _index(fake_embed, monkeypatch):
    from central import indexing
    from central.config import get_settings
    monkeypatch.setenv("EMBED_URL", fake_embed.url)
    get_settings.cache_clear()
    indexing.run_pending()


def test_search_finds_own_items_by_text_translation_and_meaning(alice, bob, anon, fake_embed, monkeypatch):
    a = pair_runner(alice, anon)
    b = pair_runner(bob, anon)
    garden = submit(alice)
    deliver(anon, a, language="en", texts=("Today we plant tomatoes in the garden.", "Bees love the flowers."),
            title="Spring work", channel="Green Channel")
    translate_next(anon, a, prefix="Ma paradicsomot ültetünk a kertben. Méhek.")
    trains = submit(alice, video="bbbbbbbbbbb")
    deliver(anon, a, texts=("A vonat pontosan érkezett.",), title="Utazás", channel="Vasút TV")
    secret = submit(bob, video="ccccccccccc")
    deliver(anon, b, texts=("Bob titkos kertje.",), title="Bob videója", channel="Bob")
    _index(fake_embed, monkeypatch)

    # stemmed and accent-free Hungarian matches in the translation of an English video
    page = alice.get("/kereses", params={"q": "paradicsomot ultetunk"}).text
    assert f"/elem/{garden}" in page and "a magyar fordításból" in page
    # meaning: "railway" is close to "vonat" through the fake embedder's synonyms
    page = alice.get("/kereses", params={"q": "railway"}).text
    assert f"/elem/{trains}" in page and "hasonló jelentés" in page
    # timestamp links to YouTube
    assert "watch?v=dQw4w9WgXcQ&amp;t=0s" in alice.get("/kereses", params={"q": "tomatoes"}).text
    # never another user's library, not even through the same word
    page = alice.get("/kereses", params={"q": "titkos kertje"}).text
    assert f"/elem/{secret}" not in page and "Bob" not in page
    assert f"/elem/{secret}" in bob.get("/kereses", params={"q": "titkos kertje"}).text
    # exact only: substring, no meaning
    page = alice.get("/kereses", params={"q": "railway", "pontos": "on"}).text
    assert "Nincs találat" in page
    # filters
    page = alice.get("/kereses", params={"q": "vonat", "csatorna": "Green Channel"}).text
    assert f"/elem/{trains}" not in page


def test_search_works_without_the_embedding_service(alice, anon, fake_embed, monkeypatch):
    a = pair_runner(alice, anon)
    item_id = submit(alice)
    deliver(anon, a, texts=("A kertészet titkai.",))
    fake_embed.down = True
    try:
        _index(fake_embed, monkeypatch)
        page = alice.get("/kereses", params={"q": "kertészet"}).text
        assert f"/elem/{item_id}" in page
        assert "jelentés szerinti keresés most nem érhető el" in page
        assert "indexelés alatt" in page  # vectors still missing: the item waits for a retry
    finally:
        fake_embed.down = False


def test_chunks_are_shared_and_protected_by_row_level_security(alice, bob, anon, fake_embed, monkeypatch):
    from central import db
    a = pair_runner(alice, anon)
    b = pair_runner(bob, anon)
    submit(alice)
    deliver(anon, a, texts=("Alice kertje.",))
    submit(bob, video="ccccccccccc")
    deliver(anon, b, texts=("Bob kertje.",))
    _index(fake_embed, monkeypatch)
    with db.system_session() as s:
        total = s.scalar(text("SELECT count(*) FROM chunks"))
        alice_id = s.scalar(text("SELECT id FROM users WHERE email = 'alice@example.com'"))
    with db.user_session(alice_id) as s:
        seen = s.execute(text("SELECT text FROM chunks")).scalars().all()  # deliberately no filter
    assert total == 4 and len(seen) == 2 and not any("Bob" in t for t in seen)


def test_index_is_served_round_robin_between_users():
    from central import db, indexing
    indexing._last_served.clear()
    from central.models import IndexQueue, Item, Source, User
    with db.system_session() as s:
        u1, u2 = User(email="u1@example.com"), User(email="u2@example.com")
        s.add_all([u1, u2])
        s.flush()
        src = [Source(kind="youtube", key=f"youtube:{i:011d}", url="u", meta={}) for i in range(4)]
        s.add_all(src)
        s.flush()
        items = [Item(user_id=u1.id, source_id=src[i].id, is_public=True) for i in range(3)]
        items.append(Item(user_id=u2.id, source_id=src[3].id, is_public=True))
        s.add_all(items)
        s.flush()
        for it in items:  # user 1 queued three items before user 2's one
            s.add(IndexQueue(item_id=it.id, user_id=it.user_id))
            s.flush()
        ids = [it.id for it in items]
    order = []
    for _ in range(4):
        nxt = indexing.next_item()
        order.append(nxt)
        with db.system_session() as s:
            s.execute(text("DELETE FROM index_queue WHERE item_id = :i"), {"i": nxt})
    assert order[:2] == [ids[0], ids[3]]  # the second user does not wait behind the first one's backlog


# ---- reuse --------------------------------------------------------------------------------

def _alice_full_result(alice, anon, token):
    item_id = submit(alice, summarize=True)
    deliver(anon, token, language="en", texts=("Good morning.", "Today: the garden."))
    for _ in range(2):  # summary and translation, in queue order
        job = claim(anon, token).json()
        if job["kind"] == "summarize":
            body = {"text": "## Összefoglaló\\nA kertről.", "model": "osszefoglalo", "prompt_version": 1}
        else:
            body = {"language": "hu", "model": "forditas", "prompt_version": 1, "missing": 0,
                    "segments": [{"idx": s["idx"], "text": "HU " + s["text"]} for s in job["input"]["segments"]]}
        assert anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=body,
                        headers=runner_headers(token)).status_code == 200
    return item_id


def test_a_finished_transcript_is_taken_over_with_summary_and_translation(alice, bob, anon, fake_embed, monkeypatch):
    a = pair_runner(alice, anon)
    b = pair_runner(bob, anon)
    _alice_full_result(alice, anon, a)
    _index(fake_embed, monkeypatch)
    embedded = sum(len(c) for c in fake_embed.calls)

    item_id = submit(bob, summarize=True)
    assert claim(anon, b).status_code == 204  # nothing for bob's runner: no cost, no wait
    page = bob.get(f"/elem/{item_id}").text
    assert "átvett átirat" in page and "Good morning." in page and "HU Good morning." in page
    assert "A kertről." in page and page.count(">átvett<") == 2  # summary and translation
    assert "alice" not in page.lower()  # never whose it was
    assert "Újra, a saját futtatómmal" in page

    _index(fake_embed, monkeypatch)
    assert sum(len(c) for c in fake_embed.calls) == embedded  # shared chunks: nothing embedded again
    assert f"/elem/{item_id}" in bob.get("/kereses", params={"q": "garden"}).text


def test_different_settings_or_private_results_are_not_taken_over(alice, bob, anon):
    from central import db
    from central.models import Transcript
    a = pair_runner(alice, anon)
    b = pair_runner(bob, anon)
    submit(alice)
    deliver(anon, a, texts=("Szia.",))
    submit(bob, mode="cloud")
    job = claim(anon, b, {**CAPS, "transcribe_modes": ["captions", "cloud"]}).json()
    assert job["kind"] == "transcribe" and job["input"]["options"]["mode"] == "cloud"
    with db.system_session() as s:
        for t in s.query(Transcript).all():
            t.shareable = False
    submit(bob, video="dQw4w9WgXcQ")  # captions again, but the result is no longer shareable
    assert claim(anon, b).json()["kind"] == "transcribe"


def test_own_rerun_replaces_a_taken_over_main_transcript(alice, bob, anon):
    a = pair_runner(alice, anon)
    b = pair_runner(bob, anon)
    submit(alice)
    deliver(anon, a, texts=("Alice szövege.",))
    item_id = submit(bob)
    assert post(bob, f"/elem/{item_id}/sajat").status_code == 303
    deliver(anon, b, texts=("Bob saját szövege.",))
    page = bob.get(f"/elem/{item_id}").text
    assert "Bob saját szövege." in page and "Újra, a saját futtatómmal" not in page
    assert "(fő)" in page  # both transcripts listed, the own one is the main
