"""Item-level functions (round 3c): downloads, summary versions, main transcript, transcript
removal, re-transcription, chosen models, library filters, bulk backfill, progress."""
from conftest import CAPS, claim, pair_runner, post, runner_headers, transcript_result
from sqlalchemy import text
from test_library import deliver, submit, translate_next

MODEL_CAPS = {**CAPS, "llm_models": {"public": ["osszefoglalo", "cloud/okos"], "private": ["helyi"]}}


def summarize_next(anon, token, body="## Összefoglaló\nA kert [00:05] **fontos**.", model="osszefoglalo",
                   caps=None):
    job = claim(anon, token, caps).json()
    assert job["kind"] == "summarize", job
    r = anon.put(f"/runner/v1/jobs/{job['job_id']}/result",
                 json={"text": body, "model": model, "language": "hu", "prompt_version": 1},
                 headers=runner_headers(token))
    assert r.status_code == 200, r.text
    return job


def db_rows(sql: str):
    from central import db
    with db.system_session() as s:
        return s.execute(text(sql)).all()


def test_transcript_downloads_original_and_hungarian(alice, bob, anon):
    token = pair_runner(alice, anon)
    item_id = submit(alice)
    deliver(anon, token, language="en", texts=("Good morning.", "The garden."))
    translate_next(anon, token)
    tid = db_rows("SELECT id FROM transcripts")[0][0]
    txt = alice.get(f"/elem/{item_id}/atirat/{tid}.txt")
    assert txt.status_code == 200 and "attachment" in txt.headers["content-disposition"]
    assert "Kerti%20vide%C3%B3.en.txt" in txt.headers["content-disposition"]
    assert "# Kerti videó" in txt.text and "[00:00] Good morning. The garden." in txt.text
    srt = alice.get(f"/elem/{item_id}/atirat/{tid}.srt").text
    assert srt.startswith("1\n00:00:00,000 --> 00:00:04,000\nGood morning.\n")
    hu = alice.get(f"/elem/{item_id}/atirat/{tid}.srt?nyelv=hu").text
    assert "HU Good morning." in hu
    assert "Nyelv: hu (fordítás, eredeti: en)" in alice.get(f"/elem/{item_id}/atirat/{tid}.txt?nyelv=hu").text
    assert alice.get(f"/elem/{item_id}/atirat/{tid}.pdf").status_code == 404
    assert bob.get(f"/elem/{item_id}/atirat/{tid}.txt").status_code == 404  # not his item


def test_summary_versions_current_choice_markdown_and_timestamp_links(alice, anon):
    token = pair_runner(alice, anon)
    item_id = submit(alice, summarize=True)
    deliver(anon, token)
    first = summarize_next(anon, token)
    assert post(alice, f"/elem/{item_id}/osszefoglalo").status_code == 303
    summarize_next(anon, token, body="## Összefoglaló\n- Második <script>", model="masik")
    page = alice.get(f"/elem/{item_id}").text
    assert "Második &lt;script&gt;" in page and "Változatok (2)" in page
    first_id = db_rows("SELECT min(id) FROM summaries")[0][0]
    assert first["input"]["role"] == "public"
    old = alice.get(f"/elem/{item_id}?osszefoglalo={first_id}").text
    assert "<strong>fontos</strong>" in old and "nem ez az aktuális" in old
    assert 'href="https://www.youtube.com/watch?v=dQw4w9WgXcQ&amp;t=5s"' in old
    assert post(alice, f"/elem/{item_id}/osszefoglalo/aktualis", {"summary_id": str(first_id)}).status_code == 303
    assert "<strong>fontos</strong>" in alice.get(f"/elem/{item_id}").text
    md = alice.get(f"/elem/{item_id}/osszefoglalo/{first_id}.md")
    assert md.status_code == 200 and md.text.startswith("# Kerti videó\nURL: https://www.youtube.com/watch")
    assert "Összefoglaló: osszefoglalo," in md.text
    assert post(alice, f"/elem/{item_id}/osszefoglalo/aktualis", {"summary_id": "999999"}).status_code == 404


def test_main_transcript_choice_and_transcript_removal(alice, anon):
    token = pair_runner(alice, anon)
    item_id = submit(alice, summarize=True)
    deliver(anon, token)  # Hungarian captions: main
    summarize_next(anon, token)
    r = post(alice, f"/elem/{item_id}/ujrairas", {"mode": "cloud", "model_cloud": "auto", "language": "en",
                                                   "summarize": "on"})
    assert r.status_code == 303, r.text
    job = claim(anon, token).json()
    assert job["input"]["options"]["mode"] == "cloud" and job["input"]["options"]["language"] == "en"
    r = anon.put(f"/runner/v1/jobs/{job['job_id']}/result", headers=runner_headers(token),
                 json=transcript_result(mode="cloud", language="en", texts=("Hello.",)))
    assert r.status_code == 200
    # make_main was not ticked: the captions stay the main transcript
    first, second = (row[0] for row in db_rows("SELECT id FROM transcripts ORDER BY id"))
    assert db_rows(f"SELECT main_transcript_id FROM items WHERE id = {item_id}")[0][0] == first
    assert claim(anon, token).status_code == 204

    assert post(alice, f"/elem/{item_id}/fo", {"transcript_id": str(second)}).status_code == 303
    assert db_rows(f"SELECT main_transcript_id, current_summary_id FROM items WHERE id = {item_id}")[0] == \
        (second, None)
    kinds = sorted(claim(anon, token).json()["kind"] for _ in range(2))
    assert kinds == ["summarize", "translate"]  # it had a summary; English is translated automatically

    # back to the first: its own summary comes back as current, nothing new is queued
    assert post(alice, f"/elem/{item_id}/fo", {"transcript_id": str(first)}).status_code == 303
    row = db_rows(f"SELECT main_transcript_id, current_summary_id IS NOT NULL FROM items WHERE id = {item_id}")[0]
    assert row == (first, True)

    assert post(alice, f"/elem/{item_id}/atirat/{first}/torles").status_code == 303
    assert db_rows(f"SELECT main_transcript_id FROM items WHERE id = {item_id}")[0][0] == second
    assert db_rows(f"SELECT count(*) FROM item_summaries WHERE item_id = {item_id}")[0][0] == 0
    page = alice.get(f"/elem/{item_id}").text
    assert "Hello." in page and "Jó napot" not in page


def test_deleted_items_leave_no_results_behind_unless_someone_else_has_them(alice, bob, anon):
    from central import indexing
    a = pair_runner(alice, anon)
    item_id = submit(alice, summarize=True)
    deliver(anon, a)
    summarize_next(anon, a)
    bob_item = submit(bob, summarize=True)  # reused: bob links the same transcript and summary
    assert post(alice, f"/elem/{item_id}/torles").status_code == 303
    indexing.drop_unreferenced_results("0 seconds")
    assert db_rows("SELECT count(*) FROM transcripts")[0][0] == 1
    assert post(bob, f"/elem/{bob_item}/torles").status_code == 303
    indexing.drop_unreferenced_results("1 hour")  # too young: kept
    assert db_rows("SELECT count(*) FROM transcripts")[0][0] == 1
    indexing.drop_unreferenced_results("0 seconds")
    assert db_rows("SELECT (SELECT count(*) FROM transcripts) + (SELECT count(*) FROM summaries)")[0][0] == 0


def test_retranscription_reuses_another_users_result(alice, bob, anon):
    a = pair_runner(alice, anon)
    submit(alice)
    job = claim(anon, a).json()
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", headers=runner_headers(a),
             json=transcript_result())
    post(alice, "/uj", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "cloud",
                        "model_cloud": "auto", "num_speakers": "2"})
    job = claim(anon, a).json()
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", headers=runner_headers(a),
             json=transcript_result(mode="cloud"))
    b = pair_runner(bob, anon)
    bob_item = submit(bob)  # captions: reused at once
    r = post(bob, f"/elem/{bob_item}/ujrairas", {"mode": "cloud", "model_cloud": "auto", "num_speakers": "2",
                                                  "make_main": "on"})
    assert r.status_code == 303, r.text
    assert claim(anon, b).status_code == 204  # nothing to do: alice's cloud transcript was taken over
    assert db_rows(f"SELECT t.mode FROM items i JOIN transcripts t ON t.id = i.main_transcript_id "
                   f"WHERE i.id = {bob_item}")[0][0] == "cloud"
    page = bob.get(f"/elem/{bob_item}").text
    assert "<strong>Felhős átírás (universal-2)</strong>" in page and "fő átirat</span>" in page
    r = post(bob, f"/elem/{bob_item}/ujrairas", {"mode": "cloud", "model_cloud": "auto", "num_speakers": "2"})
    assert r.status_code == 422 and "már van átirata" in r.text
    r = post(bob, f"/elem/{bob_item}/ujrairas", {"mode": "captions", "language": "xyz1"})
    assert r.status_code == 422


def test_uploaded_recording_cannot_be_retranscribed_without_the_file(alice, anon):
    from test_ownfiles import FILE_CAPS, upload
    from test_ownfiles import deliver as deliver_file
    token = pair_runner(alice, anon)
    r = post(alice, "/uj", {"source": "upload", "staging_id": str(upload(alice).json()["staging_id"]),
                            "mode": "local", "model_local": "large-v3"})
    item_id = int(r.headers["location"].rsplit("/", 1)[1])
    deliver_file(anon, token, claim(anon, token, FILE_CAPS).json())
    assert "Újraírás más beállításokkal" not in alice.get(f"/elem/{item_id}").text
    r = post(alice, f"/elem/{item_id}/ujrairas", {"mode": "cloud", "model_cloud": "auto"})
    assert r.status_code == 422 and "Töltsd fel újra" in r.text


def test_summary_and_translation_with_a_chosen_model(alice, anon):
    token = pair_runner(alice, anon)
    item_id = submit(alice)
    deliver(anon, token, language="en", texts=("Hello.",))
    translate_next(anon, token)
    assert claim(anon, token, MODEL_CAPS).status_code == 204  # announces the models
    page = alice.get(f"/elem/{item_id}").text
    assert '<option value="cloud/okos">' in page and "helyi" not in page.split("Összefoglaló kérése")[1][:600]
    r = post(alice, f"/elem/{item_id}/osszefoglalo", {"model": "nincs-ilyen"})
    assert r.status_code == 422 and "egyik futtatód sem ajánlja fel" in r.text
    assert post(alice, f"/elem/{item_id}/osszefoglalo", {"model": "cloud/okos"}).status_code == 303
    assert claim(anon, token).status_code == 204  # a runner that does not offer the model gets nothing
    job = summarize_next(anon, token, model="cloud/okos", caps=MODEL_CAPS)
    assert job["input"]["model"] == "cloud/okos"
    assert "cloud/okos" in alice.get(f"/elem/{item_id}").text


def test_library_filter_and_summary_preview(alice, anon):
    token = pair_runner(alice, anon)
    garden = submit(alice, summarize=True)
    deliver(anon, token)
    summarize_next(anon, token, body="## Összefoglaló\nA **kertről** szól a videó.\n\n## Fő pontok\n- egy")
    submit(alice, video="aaaaaaaaaaa")
    deliver(anon, token, title="Főzőműsor", channel="Szakács")
    page = alice.get("/").text
    assert "A kertről szól a videó." in page and "Fő pontok" not in page and "2 elem" in page
    only = alice.get("/", params={"q": "szakács"}).text
    assert "Főzőműsor" in only and "Kerti videó" not in only and "1 elem" in only
    nosum = alice.get("/", params={"szures": "nincs-osszefoglalo"}).text
    assert "Főzőműsor" in nosum and f"/elem/{garden}" not in nosum
    assert "Saját felvételek" in page and "0 elem" in alice.get("/", params={"szures": "sajat"}).text
    assert "0 elem" in alice.get("/", params={"q": "100%_"}).text  # LIKE wildcards are taken literally


def test_bulk_backfill(alice, anon):
    token = pair_runner(alice, anon)
    post(alice, "/beallitasok", {})  # no automatic translation
    for video in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        submit(alice, video=video)
        deliver(anon, token, language="en", texts=("Hi.",))
    page = alice.get("/munkak").text
    assert "Hiányzó összefoglalók (2)" in page and "Hiányzó fordítások (2)" in page
    assert post(alice, "/munkak/potlas/osszefoglalok").status_code == 303
    assert post(alice, "/munkak/potlas/forditasok").status_code == 303
    assert sorted(claim(anon, token).json()["kind"] for _ in range(4)) == \
        ["summarize", "summarize", "translate", "translate"]
    assert "Hiányzó összefoglalók (0)" in alice.get("/munkak").text  # the running ones are not counted again
    from central import db
    with db.system_session() as s:
        s.execute(text("DELETE FROM index_queue"))
    assert "Újraindexelés (2 elem)" in alice.get("/munkak").text
    assert post(alice, "/munkak/potlas/indexeles").status_code == 303
    assert db_rows("SELECT count(*) FROM index_queue")[0][0] == 2
    assert post(alice, "/munkak/potlas/semmi").status_code == 404


def test_progress_is_shown_and_the_page_refreshes_when_done(alice, anon):
    token = pair_runner(alice, anon)
    item_id = submit(alice)
    deliver(anon, token, language="en", texts=("Hello.",))
    job = claim(anon, token).json()
    assert job["kind"] == "translate"
    r = anon.post(f"/runner/v1/jobs/{job['job_id']}/heartbeat", headers=runner_headers(token),
                  json={"stage": "translate", "progress": {"part": 3, "parts": 12, "model": "forditas"}})
    assert r.status_code == 200
    page = alice.get(f"/elem/{item_id}").text
    assert "Fordítás folyamatban (3/12. rész) · forditas" in page and 'hx-get="/elem/' in page
    assert "(3/12)" in alice.get("/munkak/lista").text
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", headers=runner_headers(token),
             json={"language": "hu", "model": "forditas", "prompt_version": 1, "missing": 0,
                   "segments": [{"idx": 0, "text": "Szia."}]})
    done = alice.get(f"/elem/{item_id}/allapot")
    assert done.status_code == 200 and done.headers.get("HX-Refresh") == "true"


def test_watched_folder_recording_is_retranscribed_by_the_runner_that_has_it(alice, anon):
    from test_ownfiles import FILE_CAPS
    from test_ownfiles import deliver as deliver_file
    first = pair_runner(alice, anon, name="laptop")
    other = pair_runner(alice, anon, name="vps")
    body = {"sha256": "c" * 64, "name": "megbeszélés.m4a", "size": 10, "summarize": False,
            "options": {"mode": "local", "model": "large-v3", "language": "hu"}}
    reg = anon.post("/runner/v1/local-items", json=body, headers=runner_headers(first)).json()
    deliver_file(anon, first, claim(anon, first, FILE_CAPS).json())
    r = post(alice, f"/elem/{reg['item_id']}/ujrairas", {"mode": "local", "model_local": "large-v3-turbo",
                                                          "make_main": "on"})
    assert r.status_code == 303, r.text
    caps = {**FILE_CAPS, "whisper_models": ["large-v3", "large-v3-turbo"]}
    assert claim(anon, other, caps).status_code == 204
    job = claim(anon, first, caps).json()
    assert job["input"]["source"]["kind"] == "local" and job["input"]["options"]["model"] == "large-v3-turbo"


def test_every_transcript_keeps_and_can_get_its_own_translation(alice, anon):
    token = pair_runner(alice, anon)
    item_id = submit(alice)
    deliver(anon, token, language="en", texts=("First version.",))
    translate_next(anon, token, prefix="HU1")
    first = db_rows("SELECT id FROM transcripts")[0][0]
    post(alice, f"/elem/{item_id}/ujrairas", {"mode": "cloud", "model_cloud": "auto", "make_main": "on"})
    job = claim(anon, token).json()
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", headers=runner_headers(token),
             json=transcript_result(mode="cloud", language="en", texts=("Second version.",)))
    translate_next(anon, token, prefix="HU2")  # the new main one is translated automatically
    assert "HU1 First version." in alice.get(f"/elem/{item_id}?atirat={first}").text  # the old one keeps its own
    assert "HU2 Second version." in alice.get(f"/elem/{item_id}").text
    # a third, non-main transcript: its translation is asked for on its own
    post(alice, f"/elem/{item_id}/ujrairas", {"mode": "local", "model_local": "large-v3"})
    caps = {**CAPS, "transcribe_modes": ["captions", "cloud", "local"], "whisper_models": ["large-v3"]}
    job = claim(anon, token, caps).json()
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", headers=runner_headers(token),
             json=transcript_result(mode="local", language="en", texts=("Third version.",)))
    third = db_rows("SELECT max(id) FROM transcripts")[0][0]
    page = alice.get(f"/elem/{item_id}?atirat={third}").text
    assert "Magyar fordítás kérése" in page and 'name="transcript_id" value="%d"' % third in page
    r = post(alice, f"/elem/{item_id}/forditas", {"transcript_id": str(third)})
    assert r.status_code == 303 and r.headers["location"].endswith("#atirat")
    job = translate_next(anon, token, prefix="HU3")
    assert job["input"]["segments"][0]["text"] == "Third version."
    assert "HU3 Third version." in alice.get(f"/elem/{item_id}?atirat={third}").text
    assert db_rows(f"SELECT current_translation_id IS NOT NULL FROM items WHERE id = {item_id}")[0][0]
    assert "HU2 Second version." in alice.get(f"/elem/{item_id}").text  # the main one's stays current


def test_item_page_metadata_and_speaker_settings(alice, anon):
    token = pair_runner(alice, anon)
    r = post(alice, "/uj", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "cloud",
                            "model_cloud": "auto", "speakers": "range", "min_speakers": "2", "max_speakers": "4"})
    item_id = int(r.headers["location"].rsplit("/", 1)[1])
    job = claim(anon, token).json()
    assert job["input"]["options"]["min_speakers"] == 2 and job["input"]["options"]["max_speakers"] == 4
    result = transcript_result(mode="cloud", language="en")
    result["video"]["published"] = "2025-10-19"
    result["transcript"]["cloud"] = {"provider": "assemblyai", "audio_seconds": 120, "estimated_cost_usd": 0.01,
                                     "deleted_at_provider": True}
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=result, headers=runner_headers(token))
    page = alice.get(f"/elem/{item_id}").text
    assert "hqdefault.jpg" in page and "YouTube" in page and "2025-10-19" in page and "nyelv: en" in page
    assert ">https://www.youtube.com/watch?v=dQw4w9WgXcQ</a>" in page
    assert "2 sor" in page and "1 beszélő" in page and "legalább 2 legfeljebb 4" in page and "$0.01" in page
    for form, error in (({"speakers": "exact"}, "Add meg a beszélők számát"),
                        ({"speakers": "range", "min_speakers": "5", "max_speakers": "2"}, "nem lehet nagyobb"),
                        ({"speakers": "range"}, "legalább egy határt"), ({"speakers": "valami"}, "Ismeretlen")):
        r = post(alice, "/uj", {"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "mode": "local", **form})
        assert r.status_code == 422 and error in r.text
    post(alice, "/uj", {"url": "https://www.youtube.com/watch?v=bbbbbbbbbbb", "mode": "local", "speakers": "none"})
    caps = {**CAPS, "transcribe_modes": ["captions", "cloud", "local"], "whisper_models": ["large-v3"]}
    jobs_seen = [claim(anon, token, caps).json() for _ in range(2)]  # the first item's translation, then this
    local = next(j for j in jobs_seen if j["kind"] == "transcribe")
    assert local["input"]["options"]["diarize"] is False
