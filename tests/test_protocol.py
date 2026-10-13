from conftest import CAPS, PROTO, claim, pair_runner, post, runner_headers, transcript_result
from sqlalchemy import select


def _submit(client, url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", mode="captions", summarize=True):
    data = {"url": url, "mode": mode}
    if summarize:
        data["summarize"] = "on"
    r = post(client, "/uj", data)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_full_round_trip_with_summary(alice, anon):
    token = pair_runner(alice, anon)
    assert "otthoni gép" in alice.get("/futtatok").text
    item_id = _submit(alice)
    job = claim(anon, token).json()
    assert job["kind"] == "transcribe"
    page = alice.get("/futtatok").text  # capabilities are shown in Hungarian
    assert "YouTube-felirat" in page and "nyilvános videók" in page
    assert job["input"]["source"] == {"kind": "youtube", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                                      "video_id": "dQw4w9WgXcQ"}
    assert job["input"]["options"]["mode"] == "captions"
    hb = anon.post(f"/runner/v1/jobs/{job['job_id']}/heartbeat", json={"stage": "captions"},
                   headers=runner_headers(token)).json()
    assert hb["cancel"] is False
    assert "Felirat letöltése" in alice.get("/munkak/lista").text
    anon.post(f"/runner/v1/jobs/{job['job_id']}/heartbeat", json={"stage": "download-model"},
              headers=runner_headers(token))
    assert "Modell letöltése (első használat)" in alice.get("/munkak/lista").text  # built-in Whisper, first use
    r = anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=transcript_result(), headers=runner_headers(token))
    assert r.status_code == 200, r.text
    # delivered twice: acknowledged, not stored twice
    again = anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=transcript_result(),
                     headers=runner_headers(token))
    assert again.status_code == 200 and again.json() == r.json()
    summary_job = claim(anon, token).json()
    assert summary_job["kind"] == "summarize" and summary_job["input"]["role"] == "public"
    assert summary_job["input"]["transcript"]["segments"][1]["text"] == "Ma a kertről beszélünk."
    assert summary_job["input"]["meta"]["title"] == "Kerti videó"
    r = anon.put(f"/runner/v1/jobs/{summary_job['job_id']}/result",
                 json={"text": "## Összefoglaló\nA kertről szól.", "model": "osszefoglalo", "prompt_version": 1,
                       "prompt_tokens": 100, "completion_tokens": 20}, headers=runner_headers(token))
    assert r.status_code == 200, r.text
    page = alice.get(f"/elem/{item_id}").text
    assert "Kerti videó" in page and "Ma a kertről beszélünk." in page and "A kertről szól." in page
    assert "watch?v=dQw4w9WgXcQ&amp;t=0s" in page
    assert "Kerti videó" in alice.get("/").text


def test_capabilities_and_domains_decide_what_a_runner_gets(alice, anon):
    token = pair_runner(alice, anon)
    _submit(alice, mode="cloud", summarize=False)
    _submit(alice, url="https://example.org/video.mp4", mode="cloud", summarize=False)
    assert claim(anon, token, {**CAPS, "transcribe_modes": ["captions"]}).status_code == 204
    first = claim(anon, token).json()
    assert first["input"]["source"]["kind"] == "youtube" and first["input"]["options"]["model"] == "auto"
    assert claim(anon, token).status_code == 204  # example.org is not on the allow-list
    other = claim(anon, token, {**CAPS, "allowed_domains": ["*"]}).json()
    assert other["input"]["source"] == {"kind": "url", "url": "https://example.org/video.mp4", "video_id": None}


def test_runners_only_get_their_own_users_jobs(alice, bob, anon):
    alice_token = pair_runner(alice, anon, "alice gépe")
    bob_token = pair_runner(bob, anon, "bob gépe")
    _submit(alice)
    assert claim(anon, bob_token).status_code == 204
    job = claim(anon, alice_token).json()
    # bob's runner cannot touch alice's job
    assert anon.post(f"/runner/v1/jobs/{job['job_id']}/heartbeat", json={},
                     headers=runner_headers(bob_token)).status_code == 404
    assert anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=transcript_result(),
                    headers=runner_headers(bob_token)).status_code == 404
    assert "alice gépe" not in bob.get("/futtatok").text


def test_failures_retry_later_or_fail(alice, anon):
    token = pair_runner(alice, anon)
    _submit(alice, summarize=False)
    job = claim(anon, token).json()
    r = anon.post(f"/runner/v1/jobs/{job['job_id']}/fail", json={"error": "HTTP 429", "retry": "later"},
                  headers=runner_headers(token))
    assert r.status_code == 200
    assert claim(anon, token).status_code == 204  # waits a minute
    from central import db
    from central.db import utcnow
    from central.models import Job
    with db.system_session() as s:
        j = s.get(Job, job["job_id"])
        assert j.state == "queued" and "429" in j.error
        j.run_after = utcnow()
    job = claim(anon, token).json()
    anon.post(f"/runner/v1/jobs/{job['job_id']}/fail", json={"error": "Nincs felirat.", "retry": "never"},
              headers=runner_headers(token))
    page = alice.get("/munkak").text
    assert "Nincs felirat." in page and "Újra" in page


def test_cancel_and_expired_lease(alice, anon):
    token = pair_runner(alice, anon)
    _submit(alice, summarize=False)
    job = claim(anon, token).json()
    post(alice, f"/munkak/{job['job_id']}/megszakitas")
    hb = anon.post(f"/runner/v1/jobs/{job['job_id']}/heartbeat", json={}, headers=runner_headers(token)).json()
    assert hb["cancel"] is True
    anon.post(f"/runner/v1/jobs/{job['job_id']}/fail", json={"error": "megszakítva"}, headers=runner_headers(token))
    from central import db
    from central.db import utcnow
    from central.models import Job
    with db.system_session() as s:
        assert s.get(Job, job["job_id"]).state == "cancelled"
    # a runner that disappears: the lease runs out and the job goes back to the queue
    _submit(alice, url="https://youtu.be/BBBBBBBBBBB", summarize=False)
    job = claim(anon, token).json()
    with db.system_session() as s:
        s.get(Job, job["job_id"]).lease_until = utcnow()
    again = claim(anon, token).json()
    assert again["job_id"] == job["job_id"]
    assert anon.post(f"/runner/v1/jobs/{job['job_id']}/heartbeat", json={},
                     headers=runner_headers(token)).status_code == 200


def test_protocol_version_token_and_revocation(alice, anon):
    token = pair_runner(alice, anon)
    r = anon.post("/runner/v1/claim", json={"capabilities": CAPS}, headers={**runner_headers(token),
                                                                            "Dwyt-Protocol": "99"})
    assert r.status_code == 426 and "frissítsd" in r.json()["error"]
    assert anon.post("/runner/v1/claim", json={"capabilities": CAPS},
                     headers={**PROTO, "Authorization": "Bearer nope"}).status_code == 401
    from central import db
    from central.models import Runner
    with db.system_session() as s:
        runner_id = s.scalar(select(Runner.id))
    post(alice, f"/futtatok/{runner_id}/visszavonas")
    assert claim(anon, token).status_code == 401
    assert "visszavonva" in alice.get("/futtatok").text


def test_bad_pair_codes_and_results_are_refused(alice, anon):
    r = post(alice, "/futtatok/parositas", {"code": "XXX-XXX", "name": "x"})
    assert r.status_code == 422
    token = pair_runner(alice, anon)
    _submit(alice, summarize=False)
    job = claim(anon, token).json()
    bad = transcript_result()
    bad["transcript"]["segments"] = []
    assert anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=bad,
                    headers=runner_headers(token)).status_code == 422


def test_submit_validation(alice):
    r = post(alice, "/uj", {"url": "https://www.youtube.com/playlist?list=PL1", "mode": "captions"})
    assert r.status_code == 422 and "lejátszási lista" in r.text
    r = post(alice, "/uj", {"url": "https://example.org/a.mp4", "mode": "captions"})
    assert r.status_code == 422 and "csak YouTube-videóhoz" in r.text
    r = post(alice, "/uj", {"url": "ftp://x", "mode": "captions"})
    assert r.status_code == 422


def test_oversized_runner_fields_are_refused(alice, anon):
    token = pair_runner(alice, anon)
    caps = {**CAPS, "allowed_domains": ["x" * 300]}
    assert claim(anon, token, caps).status_code == 422
    _submit(alice, summarize=False)
    job = claim(anon, token).json()
    r = anon.post(f"/runner/v1/jobs/{job['job_id']}/heartbeat", json={"progress": {"x": "y" * 5000}},
                  headers=runner_headers(token))
    assert r.status_code == 422


def test_local_whisper_jobs_go_only_to_runners_that_can_do_them(alice, anon):
    token = pair_runner(alice, anon)
    r = post(alice, "/uj", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "local",
                            "model_local": "large-v3-turbo", "language": "hu", "num_speakers": "2"})
    assert r.status_code == 303, r.text
    item_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert claim(anon, token).status_code == 204  # captions + cloud only: not for this runner
    assert "egyik futtatód sem jelentette" in alice.get("/munkak/lista").text
    local_caps = {**CAPS, "transcribe_modes": ["captions", "local"], "whisper_models": ["large-v3"]}
    assert claim(anon, token, local_caps).status_code == 204  # has local, but not this model
    local_caps["whisper_models"] = ["large-v3", "large-v3-turbo"]
    job = claim(anon, token, local_caps).json()
    assert job["input"]["options"] == {"mode": "local", "language": "hu", "diarize": True,
                                       "model": "large-v3-turbo", "num_speakers": 2}
    hb = anon.post(f"/runner/v1/jobs/{job['job_id']}/heartbeat", json={"stage": "waiting-for-gpu"},
                   headers=runner_headers(token))
    assert hb.status_code == 200
    assert "Vár a GPU-ra" in alice.get("/munkak/lista").text
    r = anon.put(f"/runner/v1/jobs/{job['job_id']}/result",
                 json={**transcript_result(mode="local"),
                       "transcript": {**transcript_result(mode="local")["transcript"], "model": "large-v3-turbo"}},
                 headers=runner_headers(token))
    assert r.status_code == 200, r.text
    assert "Helyi Whisper (large-v3-turbo)" in alice.get(f"/elem/{item_id}").text


def test_unknown_whisper_model_is_refused(alice):
    r = post(alice, "/uj", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "local",
                            "model_local": "tiny"})
    assert r.status_code == 422 and "Ismeretlen Whisper-modell" in r.text


def test_the_runner_window_opens_the_page_with_its_code_filled_in(alice):
    page = alice.get("/futtatok?kod=kx7-4pm&nev=dolgozószoba").text
    assert 'value="KX7-4PM"' in page and 'value="dolgozószoba"' in page
    assert "ugyanez a kód látszik: KX7-4PM" in page  # compare it with the window before approving
    odd = alice.get('/futtatok?kod="><script>x</script>&nev=' + "n" * 200).text
    assert "<script>x" not in odd and 'value=""' in odd and "n" * 81 not in odd
