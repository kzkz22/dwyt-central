"""Runner sharing (decision 17.5): an owner's runner works for another user within the share."""
from conftest import CAPS, claim, pair_runner, post, runner_headers, transcript_result
from sqlalchemy import text
from test_library import submit
from test_modelinfo import INFO_CAPS


def rows(sql: str, **params):
    from central import db
    with db.system_session() as s:
        return s.execute(text(sql), params).all()


def runner_id(name="otthoni gép") -> int:
    return rows("SELECT id FROM runners WHERE name = :n ORDER BY id DESC LIMIT 1", n=name)[0][0]


def share(alice, rid: int, email="bob@example.com", cloud_hours=0, llm_answers=0, **flags):
    data = {"email": email, "cloud_hours": str(cloud_hours), "llm_answers": str(llm_answers)}
    for key in ("allow_captions", "allow_local", "allow_llm", "allow_own"):
        if flags.get(key, key != "allow_own"):
            data[key] = "on"
    return post(alice, f"/futtatok/{rid}/megosztas", data)


def accept(bob) -> int:
    sid = rows("SELECT id FROM runner_shares ORDER BY id DESC LIMIT 1")[0][0]
    r = post(bob, f"/futtatok/megosztas/{sid}/elfogadas")
    assert r.status_code == 200 and "Elfogadtad" in r.text, r.text
    return sid


def finish(anon, token, job, result):
    r = anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=result, headers=runner_headers(token))
    assert r.status_code == 200, r.text


def test_offer_accept_and_work_for_another_user(alice, bob, anon):
    token = pair_runner(alice, anon)
    claim(anon, token, INFO_CAPS)
    rid = runner_id()
    assert "Nincs ilyen dwyt-felhasználó" in share(alice, rid, email="senki@example.com").text
    assert "Magaddal" in share(alice, rid, email="alice@example.com").text
    assert "Nincs ilyen futtatód" in share(bob, rid, email="alice@example.com").text
    r = share(alice, rid)
    assert r.status_code == 200 and "Felajánlottad" in r.text and "vár az elfogadásra" in r.text
    # not accepted yet: bob's job stays his
    assert "megosztotta veled a futtatóját" in bob.get("/").text
    page = bob.get("/futtatok").text
    assert "Elfogadom" in page and "elvben láthatja" in page
    item_id = submit(bob)
    assert claim(anon, token, INFO_CAPS).status_code == 204
    accept(bob)
    page = bob.get("/futtatok").text
    assert "Megosztott futtatók" in page and " futtatója" in page
    assert "Elfogadom" not in page and "Még nincs saját futtatód" in page
    assert "elfogadta" in alice.get("/futtatok").text
    job = claim(anon, token, INFO_CAPS).json()
    assert job["kind"] == "transcribe"
    finish(anon, token, job, transcript_result(title="Bob videója"))
    assert "Bob videója" in bob.get(f"/elem/{item_id}").text
    assert "Bob videója" not in alice.get("/").text
    assert alice.get(f"/elem/{item_id}").status_code == 404
    # bob's pickers offer the shared runner's models
    assert "osszefoglalo-helyi – " in bob.get(f"/elem/{item_id}").text  # no cloud budget: local only


def test_cloud_transcription_stays_within_the_monthly_hours(alice, bob, anon):
    token = pair_runner(alice, anon)
    claim(anon, token, CAPS)
    share(alice, runner_id())
    accept(bob)
    submit(bob, mode="cloud")
    assert claim(anon, token, CAPS).status_code == 204  # cloud limit 0: not for bob
    assert "nincs" in bob.get("/futtatok").text
    sid = rows("SELECT id FROM runner_shares")[0][0]
    data = {"cloud_hours": "1", "llm_answers": "0", "allow_captions": "on", "allow_local": "on", "allow_llm": "on"}
    assert post(alice, f"/futtatok/megosztas/{sid}/modositas", data).status_code == 200
    assert "még 1,0 óra" in bob.get("/uj").text
    job = claim(anon, token, CAPS).json()
    assert job["input"]["options"]["mode"] == "cloud"
    result = transcript_result(mode="cloud")
    result["transcript"]["cloud"] = {"provider": "assemblyai", "audio_seconds": 3600}
    finish(anon, token, job, result)
    assert rows("SELECT cloud_seconds FROM share_usage")[0][0] == 3600
    assert "1,0 / 1,0 óra" in alice.get("/futtatok").text
    submit(bob, video="aaaaaaaaaaa", mode="cloud")
    assert claim(anon, token, CAPS).status_code == 204  # used up for this month
    assert "elfogyott" in bob.get("/uj").text
    # the owner's own cloud jobs have no limit
    submit(alice, video="bbbbbbbbbbb", mode="cloud")
    assert claim(anon, token, CAPS).json()["input"]["options"]["mode"] == "cloud"


def test_without_llm_budget_the_local_only_model_answers(alice, bob, anon):
    token = pair_runner(alice, anon)
    claim(anon, token, INFO_CAPS)
    share(alice, runner_id(), llm_answers=1)
    accept(bob)
    item_id = submit(bob, summarize=True)
    finish(anon, token, claim(anon, token, INFO_CAPS).json(), transcript_result())
    job = claim(anon, token, INFO_CAPS).json()
    assert job["kind"] == "summarize" and job["input"]["role"] == "public"  # one cloud answer allowed
    # the GPU host was off: LiteLLM answered with the cloud fallback (counted)
    finish(anon, token, job, {"text": "Össze.", "model": "kerdes", "model_used": "openrouter/qwen/Qwen3.8-Flash",
                              "prompt_version": 1, "prompt_tokens": 1000, "completion_tokens": 200})
    assert rows("SELECT llm_answers, llm_tokens FROM share_usage")[0] == (1, 1200)
    assert post(bob, f"/elem/{item_id}/osszefoglalo", {}).status_code == 303
    job = claim(anon, token, INFO_CAPS).json()
    assert job["kind"] == "summarize" and job["input"]["role"] == "private"  # budget used: local only
    page = bob.get(f"/elem/{item_id}").text
    assert "A futtató alapértelmezése (osszefoglalo-helyi)" in page and "kerdes – " not in page
    r = post(bob, f"/elem/{item_id}/osszefoglalo", {"model": "kerdes"})
    assert r.status_code in (400, 422) or "nem ajánlja" in r.text


def test_a_local_answer_is_not_counted(alice, bob, anon):
    token = pair_runner(alice, anon)
    claim(anon, token, INFO_CAPS)
    share(alice, runner_id(), llm_answers=5)
    accept(bob)
    submit(bob, summarize=True)
    finish(anon, token, claim(anon, token, INFO_CAPS).json(), transcript_result())
    job = claim(anon, token, INFO_CAPS).json()
    finish(anon, token, job, {"text": "Össze.", "model": "osszefoglalo", "model_used": "Qwen3.6-35B-A3B-Gnrl",
                              "prompt_version": 1})
    assert rows("SELECT count(*) FROM share_usage")[0][0] == 0


def test_the_users_own_runner_comes_first(alice, bob, anon):
    token = pair_runner(alice, anon)
    claim(anon, token, CAPS)
    share(alice, runner_id())
    accept(bob)
    bob_token = pair_runner(bob, anon, name="bob gépe")
    assert claim(anon, bob_token, {**CAPS, "kinds": []}).status_code == 204  # online, but cannot transcribe
    submit(bob)
    assert claim(anon, token, CAPS).json()["kind"] == "transcribe"  # so the shared runner does it
    assert claim(anon, bob_token, CAPS).status_code == 204  # now able to
    submit(bob, video="ccccccccccc")
    assert claim(anon, token, CAPS).status_code == 204  # bob's own runner is online and can do it
    assert claim(anon, bob_token, CAPS).json()["kind"] == "transcribe"


def test_users_take_turns_and_permissions_hold(alice, bob, anon):
    token = pair_runner(alice, anon)
    claim(anon, token, CAPS)
    share(alice, runner_id(), allow_local=False)
    accept(bob)
    a1, b1 = submit(alice, video="aaaaaaaaaaa"), submit(bob, video="bbbbbbbbbbb")
    a2 = submit(alice, video="ccccccccccc")
    local = post(bob, "/uj", {"url": "https://www.youtube.com/watch?v=ddddddddddd", "mode": "local",
                              "model_local": "large-v3"})
    assert local.status_code == 303
    caps = {**CAPS, "transcribe_modes": ["captions", "cloud", "local"], "whisper_models": ["large-v3"]}
    order = []
    for _ in range(3):
        job = claim(anon, token, caps).json()
        order.append(rows("SELECT item_id FROM jobs WHERE id = :j", j=job["job_id"])[0][0])
        finish(anon, token, job, transcript_result())
    assert order == [a1, b1, a2]  # in turn
    assert claim(anon, token, caps).status_code == 204  # local Whisper is not shared with bob
    # bob leaves: nothing more for him; the owner's page no longer lists him
    sid = rows("SELECT id FROM runner_shares")[0][0]
    assert post(bob, f"/futtatok/megosztas/{sid}/torles").status_code == 200
    assert "bob@example.com" not in alice.get("/futtatok").text


def test_own_recordings_only_when_allowed(alice, bob, anon):
    from central import sharing
    from central.models import RunnerShare
    caps = {**CAPS, "source_kinds": ["upload", "local"], "llm_models": {"public": ["a"], "private": ["helyi"]}}
    sh = RunnerShare(allow_captions=True, allow_local=True, allow_llm=True, allow_own=False, cloud_hours=0,
                     llm_answers=0)
    eff = sharing.effective_caps(caps, sh, sharing.Remaining(0, 0))
    assert eff["source_kinds"] == [] and eff["llm_roles"] == ["public"] and eff["llm_models"] == {"public": ["helyi"]}
    assert eff["transcribe_modes"] == ["captions"] and eff["shared_private_llm"] is True
    sh.allow_own = True
    eff = sharing.effective_caps(caps, sh, sharing.Remaining(3600, 2))
    assert eff["source_kinds"] == ["upload"] and eff["llm_roles"] == ["public", "private"]
    assert eff["llm_models"] == {"public": ["a"], "private": ["helyi"]} and "cloud" in eff["transcribe_modes"]


def test_cloud_answer_detection():
    from central.sharing import ran_in_cloud
    local = {"where": "local", "target": "Qwen3.6-35B-A3B-Gnrl",
             "fallback": [{"target": "openrouter/qwen/qwen3.8-flash"}]}
    assert ran_in_cloud(local, "qwen/qwen3.8-flash") and ran_in_cloud(local, "Qwen3.8-Flash")
    assert not ran_in_cloud(local, "Qwen3.6-35B-A3B-Gnrl") and not ran_in_cloud(local, None)
    assert ran_in_cloud({"where": "cloud"}, None) and not ran_in_cloud(None, "x")
    assert not ran_in_cloud({"where": "local", "target": "a"}, "b")  # unknown: not a known fallback


def test_revoked_runner_is_no_longer_shared(alice, bob, anon):
    token = pair_runner(alice, anon)
    claim(anon, token, CAPS)
    rid = runner_id()
    share(alice, rid)
    accept(bob)
    assert "Megosztott futtatók" in bob.get("/futtatok").text
    post(alice, f"/futtatok/{rid}/visszavonas")
    assert "Megosztott futtatók" not in bob.get("/futtatok").text
