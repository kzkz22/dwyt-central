"""Notifications (round 4b): ntfy and Home Assistant, instant, digest, errors, address safety."""
from datetime import datetime, timezone

from conftest import claim, pair_runner, post, runner_headers, transcript_result
from sqlalchemy import text
from test_items import summarize_next
from test_subscriptions import GARDEN, follow, iso, poll, rows


def use_ntfy(client, server, topic="dwyt-titkos-tema"):
    r = post(client, "/beallitasok/ertesites", {"channel": "ntfy", "ntfy_server": server, "ntfy_topic": topic,
                                                "digest_time": "19:00", "on_manual": "on", "on_error": "on"})
    assert r.status_code == 200 and "Mentve" in r.text, r.text
    return r


def send():
    from central import notify
    return notify.send_pending()


def test_ntfy_test_message_and_settings(alice, fake_notify):
    use_ntfy(alice, fake_notify.url)
    r = post(alice, "/beallitasok/ertesites/proba")
    assert "A próbaértesítés elment." in r.text
    body = fake_notify.ntfy[0]["body"]
    assert body["topic"] == "dwyt-titkos-tema" and body["title"] == "dwyt próba" and body["click"].endswith("/")
    assert "Utolsó: dwyt próba" in alice.get("/beallitasok").text
    r = post(alice, "/beallitasok/ertesites", {"channel": "ntfy", "ntfy_server": fake_notify.url,
                                               "ntfy_topic": "rossz téma!", "digest_time": "19:00"})
    assert r.status_code == 422 and "ntfy-téma" in r.text
    r = post(alice, "/beallitasok/ertesites", {"channel": "none", "digest_time": "25:00"})
    assert r.status_code == 422 and "ÓÓ:PP" in r.text


def test_private_addresses_are_refused_unless_allowed(alice, fake_notify_server):
    r = post(alice, "/beallitasok/ertesites", {"channel": "ntfy", "ntfy_server": fake_notify_server.url,
                                               "ntfy_topic": "dwyt-x", "digest_time": "19:00"})
    assert r.status_code == 422 and "belső hálózatra" in r.text
    r = post(alice, "/beallitasok/ertesites", {"channel": "ntfy", "ntfy_server": "file:///etc/passwd",
                                               "ntfy_topic": "dwyt-x", "digest_time": "19:00"})
    assert r.status_code == 422


def test_home_assistant_with_an_encrypted_token(alice, fake_notify):
    r = post(alice, "/beallitasok/ertesites", {"channel": "ha", "ha_url": fake_notify.url,
                                               "ha_service": "notify.mobile_app_telefon", "digest_time": "07:30"})
    assert r.status_code == 422 and "token kell" in r.text
    r = post(alice, "/beallitasok/ertesites", {"channel": "ha", "ha_url": fake_notify.url, "ha_token": "ha-token",
                                               "ha_service": "notify.mobile_app_telefon", "digest_time": "07:30"})
    assert r.status_code == 200
    assert "ha-token" not in str(rows("SELECT ha_token_enc FROM notify_settings")[0][0])
    assert "ha-token" not in alice.get("/beallitasok").text
    assert "elment" in post(alice, "/beallitasok/ertesites/proba").text
    sent = fake_notify.ha[0]
    assert sent["service"] == "mobile_app_telefon" and sent["body"]["data"]["clickAction"].startswith("http")
    # saving again without a token keeps the stored one
    post(alice, "/beallitasok/ertesites", {"channel": "ha", "ha_url": fake_notify.url,
                                          "ha_service": "mobile_app_telefon", "digest_time": "07:30"})
    assert "elment" in post(alice, "/beallitasok/ertesites/proba").text


def test_manual_work_notifies_when_the_summary_is_ready_and_errors_too(alice, anon, fake_notify):
    use_ntfy(alice, fake_notify.url)
    token = pair_runner(alice, anon)
    r = post(alice, "/uj", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "captions",
                            "summarize": "on"})
    item_id = int(r.headers["location"].rsplit("/", 1)[1])
    job = claim(anon, token).json()
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=transcript_result(), headers=runner_headers(token))
    assert send() == 0  # not yet: the summary is coming
    summarize_next(anon, token, body="## Összefoglaló\nElső mondat. Második mondat. Harmadik mondat.")
    assert send() == 1
    note = fake_notify.ntfy[0]["body"]
    assert note["title"] == "Kerti videó" and note["message"] == "Első mondat. Második mondat."
    assert note["click"] == f"http://testserver/elem/{item_id}"
    post(alice, f"/elem/{item_id}/osszefoglalo")
    summarize_next(anon, token)
    assert send() == 0  # an item notifies once

    post(alice, "/uj", {"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "mode": "captions"})
    job = claim(anon, token).json()
    anon.post(f"/runner/v1/jobs/{job['job_id']}/fail", headers=runner_headers(token),
              json={"error": "Nem engedélyezett tartomány.", "retry": "never"})
    assert send() == 1 and fake_notify.ntfy[-1]["body"]["title"].startswith("Hiba:")


def test_subscription_videos_go_into_the_daily_digest(alice, anon, fake_notify, fake_youtube):
    from central import notify
    from central.config import get_settings
    alice.email = "alice@example.com"
    use_ntfy(alice, fake_notify.url)
    fake_youtube.feeds[GARDEN] = [("vid-new-001", "Új videó", iso(-1)), ("vid-new-002", "Másik", iso(-1))]
    follow(alice, GARDEN, rule="always")
    token = pair_runner(alice, anon)
    poll()
    job = claim(anon, token).json()
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=transcript_result(), headers=runner_headers(token))
    claim(anon, token)  # the second video's captions: still running
    summarize_next(anon, token)
    assert send() == 0  # collected for the digest
    morning = datetime.now(timezone.utc).replace(hour=5, minute=0)  # 07:00 in Budapest: before 19:00
    assert notify.make_digests(get_settings(), morning) == 0
    evening = datetime.now(timezone.utc).replace(hour=18, minute=0)
    assert notify.make_digests(get_settings(), evening) == 1
    assert notify.make_digests(get_settings(), evening) == 0  # once a day
    assert send() == 1
    digest = fake_notify.ntfy[0]["body"]
    assert digest["title"] == "Napi összesítő: 1 új összefoglaló" and "• Kerti videó" in digest["message"]
    assert rows("SELECT count(*) FROM notifications WHERE sent_at IS NULL")[0][0] == 0


def test_instant_channel_and_failing_service_is_retried(alice, anon, fake_notify, fake_youtube):
    alice.email = "alice@example.com"
    use_ntfy(alice, fake_notify.url)
    fake_youtube.feeds[GARDEN] = [("vid-new-001", "Új videó", iso(-1))]
    follow(alice, GARDEN, rule="always")
    from test_subscriptions import run_sql
    run_sql("UPDATE subscriptions SET notify = 'instant'")
    token = pair_runner(alice, anon)
    poll()
    job = claim(anon, token).json()
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=transcript_result(), headers=runner_headers(token))
    summarize_next(anon, token)
    fake_notify.fail = True
    assert send() == 0
    assert "HTTP 503" in rows("SELECT error FROM notifications")[0][0]
    fake_notify.fail = False
    assert send() == 0  # backoff: not yet
    run_sql("UPDATE notifications SET created_at = now() - interval '5 minutes'")
    assert send() == 1


def test_notifications_are_per_user(alice, bob, fake_notify):
    use_ntfy(alice, fake_notify.url)
    post(alice, "/beallitasok/ertesites/proba")
    assert "Utolsó" not in bob.get("/beallitasok").text
    from central import db
    with db.user_session(2) as s:  # bob cannot read alice's outbox
        assert s.execute(text("SELECT count(*) FROM notifications")).scalar() == 0
