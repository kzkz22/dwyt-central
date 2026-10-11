"""YouTube subscriptions and the inbox (round 4a)."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import CAPS, claim, pair_runner, post, runner_headers, transcript_result
from sqlalchemy import text

GARDEN = "UC" + "a" * 22
COOKING = "UC" + "b" * 22
TRAINS = "UC" + "c" * 22
LOCAL_CAPS = {**CAPS, "transcribe_modes": ["captions", "cloud", "local"], "whisper_models": ["large-v3-turbo"]}


def iso(delta_h: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=delta_h)).isoformat()


def rows(sql: str, **params):
    from central import db
    with db.system_session() as s:
        return s.execute(text(sql), params).all()


def run_sql(sql: str, **params) -> None:
    from central import db
    with db.system_session() as s:
        s.execute(text(sql), params)


def connect_youtube(client, fake_google, refresh="rt-alice", scope=True, email="alice@example.com"):
    r = post(client, "/csatornak/youtube")
    assert r.status_code == 303, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert "youtube.readonly" in q["scope"][0] and q["access_type"] == ["offline"]
    code = f"code-{refresh}"
    fake_google.codes[code] = {"challenge": q["code_challenge"][0], "nonce": q["nonce"][0], "refresh_token": refresh,
                               "scope": "openid email https://www.googleapis.com/auth/youtube.readonly"
                               if scope else "openid email",
                               "claims": {"sub": "yt-" + refresh, "email": email, "email_verified": True}}
    return client.get(f"/auth/google/callback?state={q['state'][0]}&code={code}")


def poll(fake_youtube=None):
    from central import subscriptions
    from central.config import get_settings
    return subscriptions.poll_due(get_settings())


def test_connect_youtube_reads_the_list_and_keeps_the_token_encrypted(alice, bob, fake_google):
    fake_google.subscriptions["rt-alice"] = [(GARDEN, "Kert"), (COOKING, "Főzés"), (TRAINS, "Vonatok")]
    r = connect_youtube(alice, fake_google)
    assert r.status_code == 303 and r.headers["location"] == "/csatornak", r.text
    page = alice.get("/csatornak").text
    assert "a(z) <strong>Csatorna rt-alice</strong> YouTube-csatornával (alice@example.com)" in page
    assert "3 feliratkozás jött le, a YouTube szerint 3 van" in page
    assert "Kert" in page and "Vonatok" in page and "YouTube-fiók" in page
    stored = rows("SELECT refresh_token_enc FROM youtube_links")[0][0]
    assert "rt-alice" not in stored
    assert "Kert" not in bob.get("/csatornak").text  # strictly per user

    assert set(fake_google.orders) == {"alphabetical"}
    fake_google.subscriptions["rt-alice"] = [(GARDEN, "Kert"), (COOKING, "Főzés"), (COOKING, "Főzés")]
    fake_google.total_reported["rt-alice"] = 5
    r = post(alice, "/csatornak/youtube/frissites")
    assert "2 feliratkozás jött le (a YouTube szerint 5)" in r.text and "1 leiratkozott" in r.text
    assert "2 feliratkozás jött le, a YouTube szerint 5 van" in alice.get("/csatornak").text
    assert "már nem vagy feliratkozva" in r.text

    assert post(alice, "/csatornak/youtube/levalasztas").status_code == 303
    assert fake_google.revoked == ["rt-alice"]
    assert rows("SELECT count(*) FROM youtube_links")[0][0] == 0
    assert rows("SELECT DISTINCT source FROM subscriptions") == [("manual",)]


def test_youtube_consent_without_the_scope_or_from_another_user_is_refused(alice, bob, fake_google):
    r = connect_youtube(alice, fake_google, scope=False)
    assert r.status_code == 400 and "jogát" in r.text
    # alice starts, bob's browser finishes: refused (bound to the browser and the user)
    r = post(alice, "/csatornak/youtube")
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    r = bob.get(f"/auth/google/callback?state={state}&code=x")
    assert r.status_code in (400, 403) and rows("SELECT count(*) FROM youtube_links")[0][0] == 0


def test_channels_by_hand_and_from_takeout(alice, fake_youtube):
    fake_youtube.handles["@kertesz"] = (GARDEN, "Kertész Józsi")
    r = post(alice, "/csatornak/hozzaad", {"channel": "https://www.youtube.com/@kertesz/videos"})
    assert "Felvéve: Kertész Józsi" in r.text
    r = post(alice, "/csatornak/hozzaad", {"channel": GARDEN})
    assert "Már követed" in r.text
    r = post(alice, "/csatornak/hozzaad", {"channel": "valami"})
    assert r.status_code == 422
    csv = f"Channel Id,Channel Url,Channel Title\n{COOKING},http://x,Főzés\n{GARDEN},http://y,Kert\n".encode()
    r = alice.post("/csatornak/takeout", data={"csrf_token": alice.csrf}, files={"file": ("subscriptions.csv", csv)})
    assert "2 csatorna a fájlban, ebből 1 új" in r.text
    assert rows("SELECT count(*) FROM subscriptions")[0][0] == 2


def follow(client, channel, rule="ask", allow_cloud=False):
    from central import subscriptions
    from central.db import utcnow
    user_id = rows("SELECT id FROM users WHERE email = :e", e=client.email)[0][0]
    subscriptions.add_subscriptions(user_id, [(channel, None)], "manual")
    run_sql("UPDATE subscriptions SET rule = :r, allow_cloud = :c, baseline_at = :b WHERE user_id = :u "
            "AND channel_id = :ch", r=rule, c=allow_cloud, b=utcnow() - timedelta(hours=10), u=user_id, ch=channel)
    return user_id


@pytest.fixture
def emails(alice, bob):
    alice.email, bob.email = "alice@example.com", "bob@example.com"


def test_feeds_are_polled_once_and_new_videos_reach_each_follower(alice, bob, emails, fake_youtube):
    fake_youtube.titles[GARDEN] = "Kertész"
    fake_youtube.feeds[GARDEN] = [("vid-new-001", "Új videó", iso(-1)), ("vid-short01", "Rövid", iso(-2)),
                                  ("vid-old-001", "Régi videó", iso(-48))]
    fake_youtube.shorts.add("vid-short01")
    follow(alice, GARDEN)
    follow(bob, GARDEN)
    assert poll() == 3
    assert fake_youtube.feed_calls == [GARDEN]  # one request for both followers
    page = alice.get("/postalada").text
    assert "Új videó" in page and "Régi videó" not in page and "Rövid" not in page
    assert "Shorts (1)" in page and "Rövid" in alice.get("/postalada?allapot=shorts").text
    assert "Új videó" in bob.get("/postalada").text
    assert poll() == 0  # checked recently: not again
    assert "Kertész" in alice.get("/csatornak").text


def test_ask_request_skip_and_reuse_of_another_users_transcript(alice, bob, emails, fake_youtube, anon):
    fake_youtube.feeds[GARDEN] = [("vid-new-001", "Új videó", iso(-1)), ("vid-new-002", "Másik", iso(-1))]
    follow(alice, GARDEN)
    follow(bob, GARDEN)
    poll()
    entry_a = rows("SELECT id FROM inbox_entries WHERE video_id = 'vid-new-001' AND user_id = "
                   "(SELECT id FROM users WHERE email = 'alice@example.com')")[0][0]
    assert post(alice, f"/postalada/{entry_a}/keres").status_code == 303
    token = pair_runner(alice, anon)
    job = claim(anon, token).json()
    assert job["input"]["options"]["mode"] == "captions"
    assert job["input"]["source"]["url"] == "https://www.youtube.com/watch?v=vid-new-001"
    r = anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=transcript_result(), headers=runner_headers(token))
    assert r.status_code == 200
    assert claim(anon, token).json()["kind"] == "summarize"  # subscription videos are always summarised
    done = alice.get("/postalada?allapot=kesz").text
    assert "Új videó" in done and 'href="/elem/' in done

    entry_b = rows("SELECT id FROM inbox_entries WHERE video_id = 'vid-new-001' AND user_id = "
                   "(SELECT id FROM users WHERE email = 'bob@example.com')")[0][0]
    assert post(bob, f"/postalada/{entry_b}/keres").status_code == 303
    assert rows(f"SELECT state FROM inbox_entries WHERE id = {entry_b}")[0][0] == "done"  # taken over at once

    other = rows("SELECT id FROM inbox_entries WHERE video_id = 'vid-new-002' AND user_id = "
                 "(SELECT id FROM users WHERE email = 'bob@example.com')")[0][0]
    assert post(bob, f"/postalada/{other}/kihagy").status_code == 303
    assert "Másik" in bob.get("/postalada?allapot=kihagyott").text
    assert post(alice, "/postalada/kihagy-mind").status_code == 303
    assert "Másik" not in alice.get("/postalada").text
    assert post(bob, f"/postalada/{entry_a}/keres").status_code == 303  # not his entry: nothing happens
    assert rows(f"SELECT user_id = (SELECT id FROM users WHERE email = 'alice@example.com') "
                f"FROM inbox_entries WHERE id = {entry_a}")[0][0]


def test_always_rule_runs_the_chain_captions_local_cloud(alice, emails, fake_youtube, anon):
    fake_youtube.feeds[GARDEN] = [("vid-new-001", "Új videó", iso(-1))]
    follow(alice, GARDEN, rule="always", allow_cloud=True)
    token = pair_runner(alice, anon)
    assert claim(anon, token, LOCAL_CAPS).status_code == 204  # announces local Whisper (turbo)
    poll()
    job = claim(anon, token, LOCAL_CAPS).json()
    assert job["input"]["options"]["mode"] == "captions"
    r = anon.post(f"/runner/v1/jobs/{job['job_id']}/fail", headers=runner_headers(token),
                  json={"error": "A videónak nincs felirata a nyelvén.", "retry": "next_step"})
    assert r.status_code == 200
    page = alice.get("/postalada?allapot=folyamatban").text
    assert "Következik: helyi Whisper" in page
    job = claim(anon, token, LOCAL_CAPS).json()
    assert job["input"]["options"] == {"mode": "local", "language": None, "diarize": True, "model": "large-v3-turbo"}
    anon.post(f"/runner/v1/jobs/{job['job_id']}/fail", headers=runner_headers(token),
              json={"error": "A Whisper-worker nem érhető el.", "retry": "later"})
    assert claim(anon, token, LOCAL_CAPS).status_code == 204  # waits for the GPU (90 minutes)
    assert "1 videó helyi Whisperre vár" in alice.get("/postalada?allapot=folyamatban").text
    # the GPU stays off past GPU_WAIT_H: the channel allows the cloud
    run_sql("UPDATE jobs SET run_after = now(), payload = jsonb_set(payload, '{step_since}', "
            "to_jsonb((now() - interval '80 hours')::text))")
    job = claim(anon, token, LOCAL_CAPS).json()
    anon.post(f"/runner/v1/jobs/{job['job_id']}/fail", headers=runner_headers(token),
              json={"error": "A Whisper-worker nem érhető el.", "retry": "later"})
    job = claim(anon, token, LOCAL_CAPS).json()
    assert job["input"]["options"]["mode"] == "cloud" and job["input"]["options"]["model"] == "auto"
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=transcript_result(mode="cloud"),
             headers=runner_headers(token))
    page = alice.get("/postalada?allapot=kesz").text
    assert "Új videó" in page and 'href="/elem/' in page


def test_chain_without_cloud_ends_in_failure_and_can_be_retried(alice, emails, fake_youtube, anon):
    fake_youtube.feeds[GARDEN] = [("vid-new-001", "Új videó", iso(-1))]
    follow(alice, GARDEN, rule="always")
    token = pair_runner(alice, anon)
    poll()
    job = claim(anon, token, LOCAL_CAPS).json()
    anon.post(f"/runner/v1/jobs/{job['job_id']}/fail", headers=runner_headers(token),
              json={"error": "Nincs felirat.", "retry": "next_step"})
    job = claim(anon, token, LOCAL_CAPS).json()
    anon.post(f"/runner/v1/jobs/{job['job_id']}/fail", headers=runner_headers(token),
              json={"error": "A worker elutasította.", "retry": "never"})
    page = alice.get("/postalada?allapot=kihagyott").text
    assert "A worker elutasította." in page and "Újra" in page


def test_manual_next_step_is_still_final(alice, anon):
    token = pair_runner(alice, anon)
    post(alice, "/uj", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "captions"})
    job = claim(anon, token).json()
    anon.post(f"/runner/v1/jobs/{job['job_id']}/fail", headers=runner_headers(token),
              json={"error": "Nincs felirat.", "retry": "next_step"})
    assert rows("SELECT state FROM jobs")[0][0] == "failed"


def test_video_already_in_the_library_is_linked_not_redone(alice, emails, fake_youtube, anon):
    token = pair_runner(alice, anon)
    post(alice, "/uj", {"url": "https://www.youtube.com/watch?v=vid-new-001", "mode": "captions"})
    claim(anon, token)
    fake_youtube.feeds[GARDEN] = [("vid-new-001", "Új videó", iso(-1))]
    follow(alice, GARDEN, rule="always")
    poll()
    assert rows("SELECT state, item_id IS NOT NULL FROM inbox_entries")[0] == ("done", True)
    assert rows("SELECT count(*) FROM jobs WHERE kind = 'transcribe'")[0][0] == 1


def test_rule_never_and_removal(alice, emails, fake_youtube):
    fake_youtube.feeds[GARDEN] = [("vid-new-001", "Új videó", iso(-1))]
    follow(alice, GARDEN, rule="never")
    assert poll() == 0 and fake_youtube.feed_calls == []
    sub_id = rows("SELECT id FROM subscriptions")[0][0]
    assert post(alice, f"/csatornak/{sub_id}/beallitas", {"rule": "ask", "notify": "instant"}).status_code == 303
    assert rows("SELECT rule, notify FROM subscriptions")[0] == ("ask", "instant")
    run_sql("UPDATE subscriptions SET baseline_at = now() - interval '10 hours'")
    poll()
    assert rows("SELECT count(*) FROM inbox_entries")[0][0] == 1
    assert post(alice, f"/csatornak/{sub_id}/torles").status_code == 303
    assert rows("SELECT count(*) FROM inbox_entries")[0][0] == 0
    assert post(alice, f"/csatornak/{sub_id}/beallitas", {"rule": "rossz"}).status_code == 422


def test_the_app_role_cannot_read_youtube_tokens(alice, fake_google):
    fake_google.subscriptions["rt-alice"] = [(GARDEN, "Kert")]
    connect_youtube(alice, fake_google)
    from central import db
    with pytest.raises(Exception, match="permission denied"):
        with db.user_session(1) as s:
            s.execute(text("SELECT * FROM youtube_links")).all()
