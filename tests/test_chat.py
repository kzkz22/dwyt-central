"""Questions about an item or the library (round 6, plan 15a)."""
import io
import zipfile

from conftest import CAPS, claim, pair_runner, post, runner_headers
from sqlalchemy import text
from test_library import _index, deliver, submit, translate_next

ASK_CAPS = {**CAPS, "kinds": [*CAPS["kinds"], "ask"],
            "llm_models": {"public": ["osszefoglalo", "kerdes"], "private": ["helyi"]}}


def rows(sql: str, **params):
    from central import db
    with db.system_session() as s:
        return s.execute(text(sql), params).all()


def answer_next(anon, token, body: str, caps=None) -> dict:
    job = claim(anon, token, caps or ASK_CAPS).json()
    assert job["kind"] == "ask", job
    r = anon.put(f"/runner/v1/jobs/{job['job_id']}/result", headers=runner_headers(token),
                 json={"text": body, "model": "kerdes", "model_used": "Qwen3.6-35B-A3B-Gnrl", "prompt_version": 1,
                       "prompt_tokens": 1200, "completion_tokens": 80, "reasoning_tokens": 300, "duration_s": 4.2})
    assert r.status_code == 200, r.text
    return job


def test_a_conversation_about_an_item(alice, anon):
    token = pair_runner(alice, anon)
    item_id = submit(alice)
    deliver(anon, token, language="en", texts=("Good morning.", "The garden needs water."))
    translate_next(anon, token)
    page = alice.get(f"/elem/{item_id}").text
    assert 'id="kerdes"' in page and "Kérdezz a videóról" in page
    r = post(alice, f"/elem/{item_id}/kerdes", {"question": "Mit mond a kertről?"})
    assert r.status_code == 303 and "#kerdes" in r.headers["location"]
    conv = rows("SELECT id FROM conversations")[0][0]
    # a runner without the ask kind gets nothing; the page says the runner must be updated
    assert claim(anon, token).status_code == 204
    assert "Frissítsd a futtatót" in alice.get(f"/elem/{item_id}").text
    job = claim(anon, token, ASK_CAPS).json()
    assert job["kind"] == "ask"
    data = job["input"]
    assert data["scope"] == "item" and data["role"] == "public" and data["language"] == "hu"
    assert data["messages"] == [{"role": "user", "content": "Mit mond a kertről?"}]
    src = data["sources"][0]
    assert src["n"] == 1 and src["title"] == "Kerti videó" and src["partial"] is False
    assert [x["text"] for x in src["transcript"]] == ["Good morning.", "The garden needs water."]
    assert src["transcript"][1]["hu"] == "HU The garden needs water." and src["transcript"][1]["start"] == 5.0
    # only one open question per conversation
    r = post(alice, f"/elem/{item_id}/kerdes", {"question": "Még valami?", "conversation_id": str(conv)},
             headers={"HX-Request": "true"})
    assert "Várd meg az előző kérdés válaszát" in r.text
    # the panel polls while the answer is being written
    part = alice.get(f"/beszelgetes/{conv}/reszlet").text
    assert 'hx-trigger="every 3s"' in part and "írja a választ" in part
    r = anon.put(f"/runner/v1/jobs/{job['job_id']}/result", headers=runner_headers(token),
                 json={"text": "## Kert\nA kertnek **víz** kell [00:05].\n\n1. első\n2. <script>x</script>",
                       "model": "kerdes", "model_used": "Qwen3.6", "prompt_version": 1})
    assert r.status_code == 200
    page = alice.get(f"/elem/{item_id}?beszelgetes={conv}").text
    assert "<strong>víz</strong>" in page and "<h3>Kert</h3>" in page and "<ol>" in page
    assert 'href="https://www.youtube.com/watch?v=dQw4w9WgXcQ&amp;t=5s"' in page
    assert "<script>x</script>" not in page and "&lt;script&gt;x&lt;/script&gt;" in page
    assert "**víz**" not in page
    # a follow-up carries the conversation
    r = post(alice, f"/elem/{item_id}/kerdes", {"question": "És a reggel?", "conversation_id": str(conv),
                                                "model": "osszefoglalo"})
    assert r.status_code == 303
    job = answer_next(anon, token, "Jó reggelt kívánt [00:00].")
    assert job["input"]["model"] == "osszefoglalo"
    assert [m["role"] for m in job["input"]["messages"]] == ["user", "assistant", "user"]
    assert job["input"]["messages"][1]["content"].startswith("## Kert")
    # the whole conversation as Markdown
    md = alice.get(f"/beszelgetes/{conv}.md")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
    assert md.text.startswith("# Mit mond a kertről?") and "## 2. kérdés" in md.text
    assert "A kertnek **víz** kell [00:05]." in md.text and "Jó reggelt kívánt" in md.text
    # questions are followed in the conversation, not on the Jobs page
    assert "Kérdés:" not in alice.get("/munkak/lista").text
    listing = alice.get("/beszelgetesek").text
    assert "Mit mond a kertről?" in listing and "2 kérdés" in listing
    # in the export
    z = zipfile.ZipFile(io.BytesIO(alice.get("/export?formatum=md").content))
    assert any(n.startswith("dwyt/beszelgetesek/") for n in z.namelist())
    # deleting the conversation
    assert post(alice, f"/beszelgetes/{conv}/torles").status_code == 303
    assert rows("SELECT count(*) FROM conversation_messages")[0][0] == 0


def test_library_questions_leave_own_recordings_out(alice, bob, anon, fake_embed, monkeypatch):
    a = pair_runner(alice, anon)
    garden = submit(alice)
    deliver(anon, a, texts=("A paradicsomot tavasszal ültetjük a kertben.",), title="Kerti munkák",
            channel="Kertész")
    own = submit(alice, video="bbbbbbbbbbb")
    deliver(anon, a, texts=("A paradicsom titkos receptje a családban.",), title="Saját", channel="Én")
    rows("UPDATE items SET is_public = false WHERE id = :i RETURNING id", i=own)
    b = pair_runner(bob, anon)
    submit(bob, video="ccccccccccc")
    deliver(anon, b, texts=("Bob paradicsoma.",), title="Bob", channel="Bob")
    _index(fake_embed, monkeypatch)
    r = post(alice, "/kereses/kerdes", {"question": "Mikor ültessem a paradicsomot?"})
    assert r.status_code == 303 and r.headers["location"].startswith("/kereses?beszelgetes=")
    job = claim(anon, a, ASK_CAPS).json()
    data = job["input"]
    assert data["scope"] == "library" and data["role"] == "public"
    titles = [src["title"] for src in data["sources"]]
    assert titles == ["Kerti munkák"]  # not the own recording, never Bob's
    assert "tavasszal" in data["sources"][0]["transcript"][0]["text"] and data["sources"][0]["partial"] is True
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", headers=runner_headers(a),
             json={"text": "Tavasszal [#1 00:00], lásd még [#1] és [#9].", "model": "kerdes"})
    page = alice.get("/kereses").text
    assert "Kérdezz a könyvtáradról" in page and "a saját felvételeid kimaradnak" in page
    assert 'class="cite" href="https://www.youtube.com/watch?v=dQw4w9WgXcQ&amp;t=0s"' in page
    assert f'class="cite" href="/elem/{garden}"' in page and "[#9]" in page  # an unknown source stays text
    assert f'<a href="/elem/{garden}">Kerti munkák</a>' in page  # the sources under the answer
    # nothing to go on: no job
    r = post(alice, "/kereses/kerdes", {"question": "xyzzy qwerty plugh"}, headers={"HX-Request": "true"})
    assert "nem találtam semmit" in r.text


def test_own_recording_questions_use_the_private_role(alice, anon):
    token = pair_runner(alice, anon)
    item_id = submit(alice)
    deliver(anon, token)
    rows("UPDATE items SET is_public = false WHERE id = :i RETURNING id", i=item_id)
    page = alice.get(f"/elem/{item_id}").text
    assert "csak a futtatód helyi modellje" in page
    assert post(alice, f"/elem/{item_id}/kerdes", {"question": "Miről szól?"}).status_code == 303
    public_only = {**ASK_CAPS, "llm_roles": ["public"]}
    assert claim(anon, token, public_only).status_code == 204
    job = claim(anon, token, ASK_CAPS).json()
    assert job["kind"] == "ask" and job["input"]["role"] == "private"
    # a public model cannot be chosen for an own recording
    r = post(alice, f"/elem/{item_id}/kerdes", {"question": "Más?", "model": "kerdes"}, headers={"HX-Request": "true"})
    assert "nem ajánlja fel" in r.text


def test_failures_stay_in_the_conversation_and_can_be_retried(alice, anon, fake_notify):
    token = pair_runner(alice, anon)
    item_id = submit(alice)
    deliver(anon, token)
    post(alice, "/beallitasok/ertesites", {"channel": "ntfy", "ntfy_topic": "dwyt-teszt", "on_error": "on"})
    post(alice, f"/elem/{item_id}/kerdes", {"question": "Miről szól?"})
    conv = rows("SELECT id FROM conversations")[0][0]
    job = claim(anon, token, ASK_CAPS).json()
    anon.post(f"/runner/v1/jobs/{job['job_id']}/fail", headers=runner_headers(token),
              json={"error": "A modell nem érhető el.", "retry": "never"})
    part = alice.get(f"/beszelgetes/{conv}/reszlet").text
    assert "Nem jött válasz: A modell nem érhető el." in part and "/ujra" in part
    assert rows("SELECT count(*) FROM notifications WHERE kind = 'error'")[0][0] == 0
    r = post(alice, f"/beszelgetes/{conv}/ujra", headers={"HX-Request": "true"})
    assert "Sorban áll" in r.text
    answer_next(anon, token, "Egy kertről [00:00].")
    assert "Egy kertről" in alice.get(f"/beszelgetes/{conv}/reszlet").text


def test_limits(alice, anon):
    from central import ratelimit
    token = pair_runner(alice, anon)
    item_id = submit(alice)
    deliver(anon, token)
    r = post(alice, f"/elem/{item_id}/kerdes", {"question": "x" * 4001}, headers={"HX-Request": "true"})
    assert "legfeljebb 4000 karakter" in r.text and 'x' * 50 in r.text  # the draft is kept
    r = post(alice, f"/elem/{item_id}/kerdes", {"question": "   "})
    assert r.status_code == 422
    for _ in range(ratelimit.LIMITS["ask"][0]):
        ratelimit.hit("ask", str(rows("SELECT id FROM users WHERE email = 'alice@example.com'")[0][0]))
    r = post(alice, f"/elem/{item_id}/kerdes", {"question": "Miről?"}, headers={"HX-Request": "true"})
    assert "Túl sok kérdés" in r.text
    assert rows("SELECT count(*) FROM jobs WHERE kind = 'ask'")[0][0] == 0
    # another user's conversation cannot be continued or read
    assert alice.get("/beszelgetes/999999/reszlet").status_code == 404


def test_long_transcripts_send_the_best_passages(alice, anon, fake_embed, monkeypatch):
    from central import chat
    monkeypatch.setattr(chat, "ITEM_MAX_CHARS", 60)
    token = pair_runner(alice, anon)
    item_id = submit(alice)
    deliver(anon, token, texts=("Az első rész a vonatokról szól.", "A második rész a paradicsomról.",
                                "A harmadik rész az időjárásról."))
    _index(fake_embed, monkeypatch)
    post(alice, f"/elem/{item_id}/kerdes", {"question": "paradicsom"})
    src = claim(anon, token, ASK_CAPS).json()["input"]["sources"][0]
    assert src["partial"] is True and any("paradicsom" in x["text"] for x in src["transcript"])
