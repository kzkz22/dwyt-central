"""When YouTube captions are good enough (decision 18.9), and the silent-runner warning (round 7a)."""
from conftest import claim, pair_runner, post, runner_headers, transcript_result
from sqlalchemy import text
from test_library import submit
import pytest
from test_subscriptions import GARDEN, LOCAL_CAPS, follow, iso, poll


@pytest.fixture
def emails(alice, bob):
    alice.email, bob.email = "alice@example.com", "bob@example.com"


def rows(sql: str, **params):
    from central import db
    with db.system_session() as s:
        return s.execute(text(sql), params).all()


def put(anon, token, job, result):
    r = anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=result, headers=runner_headers(token))
    assert r.status_code == 200, r.text
    return r.json()


def settings(client, policy, langs=""):
    # translate_auto left out: no automatic translation jobs in between
    r = post(client, "/beallitasok", {"captions_policy": policy, "captions_auto_langs": langs})
    assert r.status_code == 200


def test_machine_captions_are_refused_by_default_and_said_so(alice, anon):
    token = pair_runner(alice, anon)
    assert "Csak a feltöltő saját felirata" in alice.get("/beallitasok").text
    item_id = submit(alice)
    job = claim(anon, token).json()
    assert job["input"]["options"]["mode"] == "captions"  # the runner fetches whatever track it finds
    assert put(anon, token, job, transcript_result(caption_kind="auto")) == {
        "rejected": "machine captions not accepted by the user's rule"}
    state, error = rows("SELECT state, error FROM jobs WHERE id = :j", j=job["job_id"])[0]
    assert state == "failed" and "Csak gépi felirata van (hu)" in error and "átírást" in error
    assert rows("SELECT count(*) FROM item_transcripts WHERE item_id = :i", i=item_id)[0][0] == 0
    # the uploader's own captions are fine
    submit(alice, video="ownsubs0001")
    job = claim(anon, token).json()
    assert "transcript_id" in put(anon, token, job, transcript_result(caption_kind="manual"))


def test_machine_captions_allowed_in_general_per_language_or_per_submission(alice, anon):
    token = pair_runner(alice, anon)
    settings(alice, "manual", "EN, de-AT; xx1")
    assert rows("SELECT captions_auto_langs FROM users WHERE email = 'alice@example.com'")[0][0] == "en, de"
    submit(alice, video="english0001")
    job = claim(anon, token).json()
    assert "transcript_id" in put(anon, token, job, transcript_result(caption_kind="auto", language="en"))
    submit(alice, video="hungari0001")
    job = claim(anon, token).json()
    assert "rejected" in put(anon, token, job, transcript_result(caption_kind="auto", language="hu"))
    # this one submission: machine captions are fine
    r = post(alice, "/uj", {"url": "https://www.youtube.com/watch?v=hungari0002", "mode": "captions",
                            "machine_captions": "on"})
    assert r.status_code == 303
    job = claim(anon, token).json()
    assert "transcript_id" in put(anon, token, job, transcript_result(caption_kind="auto", language="hu"))
    # in general: the submit form's box starts ticked; a shared result is taken over (submit_url without a choice)
    settings(alice, "any")
    assert 'name="machine_captions" checked' in alice.get("/uj").text
    from central import captionpolicy
    user_id = rows("SELECT id FROM users WHERE email = 'alice@example.com'")[0][0]
    assert captionpolicy.for_user(user_id).mode == "any"
    # never: the submit form starts with local Whisper
    settings(alice, "never")
    assert 'name="mode" value="local" checked' in alice.get("/uj").text


def test_shared_machine_captions_are_not_taken_over_against_the_rule(alice, bob, anon):
    settings(bob, "any")
    b = pair_runner(bob, anon, name="bob gépe")
    submit(bob)
    put(anon, b, claim(anon, b).json(), transcript_result(caption_kind="auto"))
    a = pair_runner(alice, anon)
    item_id = submit(alice)  # default rule: Bob's machine captions are not for Alice
    assert rows("SELECT count(*) FROM item_transcripts WHERE item_id = :i", i=item_id)[0][0] == 0
    assert claim(anon, a).json()["kind"] == "transcribe"


def test_subscription_chain_moves_on_from_machine_captions(alice, emails, fake_youtube, anon):
    fake_youtube.feeds[GARDEN] = [("vid-new-001", "Új videó", iso(-1))]
    follow(alice, GARDEN, rule="always")
    token = pair_runner(alice, anon)
    claim(anon, token, LOCAL_CAPS)
    poll()
    job = claim(anon, token, LOCAL_CAPS).json()
    assert job["input"]["options"]["mode"] == "captions"
    put(anon, token, job, transcript_result(caption_kind="auto"))
    assert "Következik: helyi Whisper" in alice.get("/postalada?allapot=folyamatban").text
    job = claim(anon, token, LOCAL_CAPS).json()
    assert job["input"]["options"]["mode"] == "local"


def test_channel_rule_overrides_the_users(alice, emails, fake_youtube, anon):
    fake_youtube.feeds[GARDEN] = [("vid-new-001", "Új videó", iso(-1))]
    follow(alice, GARDEN, rule="always")
    sub_id = rows("SELECT id FROM subscriptions")[0][0]
    page = alice.get("/csatornak").text
    assert "Felirat: a beállításod szerint" in page
    r = post(alice, f"/csatornak/{sub_id}/beallitas", {"rule": "always", "notify": "digest", "captions": "never"})
    assert r.status_code == 303
    assert rows("SELECT captions_policy FROM subscriptions")[0][0] == "never"
    token = pair_runner(alice, anon)
    claim(anon, token, LOCAL_CAPS)
    poll()
    job = claim(anon, token, LOCAL_CAPS).json()
    assert job["input"]["options"] == {"mode": "local", "language": None, "diarize": True, "model": "large-v3-turbo"}
    post(alice, f"/csatornak/{sub_id}/beallitas", {"rule": "always", "notify": "digest", "captions": ""})
    assert rows("SELECT captions_policy FROM subscriptions")[0][0] is None  # back to the user's rule


def test_silent_runner_is_reported_while_jobs_wait(alice, anon):
    token = pair_runner(alice, anon)
    claim(anon, token)
    submit(alice)
    assert "nem jelentkezett" not in alice.get("/").text  # seen just now
    rows("UPDATE runners SET last_seen_at = now() - interval '3 hours' RETURNING id")
    page = alice.get("/").text
    assert "A futtatód (otthoni gép) 3 órája nem jelentkezett, és 1 munka vár rá." in page
    assert "3 órája nem jelentkezett" in alice.get("/munkak").text
    rows("UPDATE jobs SET state = 'cancelled' RETURNING id")
    assert "nem jelentkezett" not in alice.get("/").text  # nothing waits: nothing to say
