"""Round 8 (decisions 19.1-19.5): the Megnézendő list in the user's YouTube account, read marks, the inbox's quick
decisions and sharing from the phone."""
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import claim, google_login, pair_runner, post, runner_headers, transcript_result
from test_subscriptions import GARDEN, follow, iso, poll, rows, run_sql

VIDEO = "dQw4w9WgXcQ"


@pytest.fixture
def emails(alice, bob):
    alice.email, bob.email = "alice@example.com", "bob@example.com"
WRITE = "https://www.googleapis.com/auth/youtube"


def submit(client, video=VIDEO, summarize=True):
    data = {"url": f"https://www.youtube.com/watch?v={video}", "mode": "captions"}
    if summarize:
        data["summarize"] = "on"
    r = post(client, "/uj", data)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def finish(anon, token, summary=True):
    """The runner does the transcription (and the summary)."""
    job = claim(anon, token).json()
    assert job["kind"] == "transcribe", job
    assert anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=transcript_result(),
                    headers=runner_headers(token)).status_code == 200
    if summary:
        job = claim(anon, token).json()
        assert job["kind"] == "summarize", job
        body = {"text": "A videó a kertről szól, és a metszésről.", "model": "osszefoglalo", "prompt_version": 1}
        assert anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=body,
                        headers=runner_headers(token)).status_code == 200


def give_consent(client, fake_google, response, refresh="rt-alice-w", scope=f"openid email {WRITE}"):
    """Google's side of the write consent the Megnézendő button started; returns the callback response."""
    assert response.status_code == 303, response.text
    q = parse_qs(urlsplit(response.headers["location"]).query)
    assert WRITE in q["scope"][0].split()
    code = f"code-{refresh}"
    fake_google.codes[code] = {"challenge": q["code_challenge"][0], "nonce": q["nonce"][0], "refresh_token": refresh,
                               "scope": scope, "claims": {"sub": "yt-" + refresh, "email": client.email,
                                                          "email_verified": True}}
    return client.get(f"/auth/google/callback?state={q['state'][0]}&code={code}")


def watch(client, video, on=True, back="/"):
    return post(client, f"/megnezendo/{video}", {"be": "1" if on else "0", "vissza": back, "cim": "Kerti videó"})


# ---- 19.1 Megnézendő ---------------------------------------------------------------------------------------------

def test_first_watch_press_asks_for_consent_then_adds_and_the_toggle_removes(alice, emails, fake_google):
    fake_google.subscriptions["rt-alice-w"] = [(GARDEN, "Kert")]
    item_id = submit(alice)
    page = alice.get(f"/elem/{item_id}").text
    assert 'action="/megnezendo/dQw4w9WgXcQ"' in page and "consent-hint" in page  # not allowed yet: says so
    r = give_consent(alice, fake_google, watch(alice, VIDEO, back=f"/elem/{item_id}"))
    assert r.status_code == 303 and r.headers["location"] == f"/elem/{item_id}", r.text
    (playlist, title), = fake_google.playlists["rt-alice-w"].items()
    assert title == "dwyt – megnézendő" and fake_google.privacy[playlist] == "private"
    assert [v for _, v, _ in fake_google.items[playlist]] == [VIDEO]
    assert rows("SELECT video_id, title FROM watch_later") == [(VIDEO, "YouTube-cím")]  # YouTube names it
    page = alice.get(f"/elem/{item_id}").text
    assert "A megnézendő listán" in page and "consent-hint" not in page
    # connected for the list only: the subscriptions are not taken over, and the Channels page says why
    assert rows("SELECT count(*) FROM subscriptions")[0][0] == 0
    assert "csak a Megnézendő listához" in alice.get("/csatornak").text

    r = watch(alice, VIDEO, on=False, back=f"/elem/{item_id}")
    assert r.status_code == 303 and r.headers["location"] == f"/elem/{item_id}"
    assert fake_google.items[playlist] == [] and len(fake_google.deleted) == 1
    assert rows("SELECT count(*) FROM watch_later")[0][0] == 0
    r = watch(alice, VIDEO, back="/?szures=megnezendo")  # allowed now: no detour
    assert r.status_code == 303 and r.headers["location"] == "/?szures=megnezendo"
    assert len(fake_google.playlists["rt-alice-w"]) == 1  # the same playlist again


def test_consent_without_the_write_scope_adds_nothing(alice, emails, fake_google):
    r = give_consent(alice, fake_google, watch(alice, VIDEO), scope="openid email "
                     "https://www.googleapis.com/auth/youtube.readonly")
    assert r.status_code == 400 and "lejátszási listák kezelésének" in r.text
    assert rows("SELECT count(*) FROM watch_later")[0][0] == 0


def test_daily_sync_takes_over_changes_made_on_youtube_and_a_deleted_playlist(alice, emails, fake_google):
    from central import subscriptions, watchlater
    from central.config import get_settings
    give_consent(alice, fake_google, watch(alice, VIDEO))
    user_id = rows("SELECT id FROM users WHERE email = 'alice@example.com'")[0][0]
    playlist = rows("SELECT watch_playlist_id FROM youtube_links")[0][0]
    fake_google.items[playlist] = [("PLI-yt", "abcdefghijk", "Kézzel felvett")]  # removed one, added one there
    got = subscriptions.sync_google(get_settings(), user_id)  # the write-only link: only the list is synced
    assert got["count"] == 0
    assert rows("SELECT video_id, title, playlist_item_id FROM watch_later") == [("abcdefghijk", "Kézzel felvett",
                                                                                   "PLI-yt")]
    del fake_google.items[playlist]  # the whole playlist deleted on YouTube
    del fake_google.playlists["rt-alice-w"][playlist]
    assert watchlater.sync(get_settings(), user_id) == {"removed": 1, "added": 0}
    assert rows("SELECT watch_playlist_id FROM youtube_links")[0][0] is None
    assert watch(alice, VIDEO).status_code == 303  # a new playlist is made
    assert rows("SELECT count(*) FROM watch_later")[0][0] == 1 and len(fake_google.playlists["rt-alice-w"]) == 1


def test_library_watch_filter_toggle_and_unlink(alice, bob, emails, fake_google):
    first, second = submit(alice), submit(alice, video="abcdefghijk")
    give_consent(alice, fake_google, watch(alice, VIDEO))
    page = alice.get("/?szures=megnezendo").text
    assert f'href="/elem/{first}"' in page and f'href="/elem/{second}"' not in page
    assert "badge cloud\">megnézendő" in page and "side-toggle" in page
    assert "megnézendő" not in bob.get("/?szures=megnezendo").text.split("<ul")[-1]  # strictly per user
    assert post(alice, "/csatornak/youtube/levalasztas").status_code == 303
    assert rows("SELECT count(*) FROM watch_later")[0][0] == 0  # dwyt's copy goes, YouTube's playlist stays


def test_bad_video_id_and_a_back_address_elsewhere(alice):
    assert post(alice, "/megnezendo/nem-video", {"be": "1"}).status_code == 404
    r = post(alice, "/elem/1/olvasott", {"olvasott": "1", "vissza": "//evil.example/x"})
    assert r.status_code == 303 and r.headers["location"] == "/elem/1"


# ---- 19.3 read marks ---------------------------------------------------------------------------------------------

def test_opening_the_item_reads_it_and_the_button_makes_it_unread(alice, anon):
    token = pair_runner(alice, anon)
    item_id = submit(alice)
    alice.get(f"/elem/{item_id}")  # nothing to read yet: stays unread
    assert rows("SELECT read_at FROM items")[0][0] is None
    finish(anon, token)
    library = alice.get("/").text
    assert "unread-dot" in library
    assert f'href="/elem/{item_id}"' in alice.get("/?szures=olvasatlan").text
    page = alice.get(f"/elem/{item_id}").text
    assert rows("SELECT read_at IS NOT NULL FROM items")[0][0] and "Olvasatlannak jelölés" in page
    assert "unread-dot" not in alice.get("/").text
    assert f'href="/elem/{item_id}"' not in alice.get("/?szures=olvasatlan").text
    r = post(alice, f"/elem/{item_id}/olvasott", {"olvasott": "0", "vissza": "/"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert rows("SELECT read_at FROM items")[0][0] is None


# ---- 19.4 inbox decisions ----------------------------------------------------------------------------------------

def test_inbox_shows_descriptions_and_hides_read_results(alice, emails, fake_youtube, anon):
    fake_youtube.feeds[GARDEN] = [("vid-new-001", "Metszés", iso(-1), "Ősszel   metszünk.\n A rózsáról is."),
                                  ("vid-new-002", "Ültetés", iso(-2))]
    follow(alice, GARDEN)
    poll()
    new = alice.get("/postalada").text
    assert "A videó leírása:</b> Ősszel metszünk. A rózsáról is." in new
    assert "data-keys" in new and 'data-key="ö"' in new and 'data-key="x"' in new and "/megnezendo/vid-new-001" in new
    assert new.count("A videó leírása") == 1
    entry = rows("SELECT id FROM inbox_entries WHERE video_id = 'vid-new-001'")[0][0]
    token = pair_runner(alice, anon)
    assert post(alice, f"/postalada/{entry}/keres", {"vissza": "/postalada"}).headers["location"] == "/postalada"
    finish(anon, token)
    done = alice.get("/postalada?allapot=kesz").text
    assert "Metszés" in done and "unread-dot" in done and "A videó a kertről szól" in done and "Elolvastam" in done
    item_id = rows("SELECT item_id FROM inbox_entries WHERE id = :e", e=entry)[0][0]
    r = post(alice, f"/elem/{item_id}/olvasott", {"olvasott": "1", "vissza": "/postalada?allapot=kesz"})
    assert r.headers["location"] == "/postalada?allapot=kesz"
    done = alice.get("/postalada?allapot=kesz").text
    assert "Metszés" not in done and "1 elolvasott nem látszik" in done
    shown = alice.get("/postalada?allapot=kesz&mind=1").text
    assert "Metszés" in shown and "Olvasatlannak jelölés" in shown and "Kész (1)" in shown


def test_skip_returns_to_the_page_it_came_from(alice, emails, fake_youtube):
    fake_youtube.feeds[GARDEN] = [("vid-new-001", "Metszés", iso(-1))]
    follow(alice, GARDEN)
    poll()
    entry = rows("SELECT id FROM inbox_entries")[0][0]
    r = post(alice, f"/postalada/{entry}/kihagy", {"vissza": "/postalada?oldal=1"})
    assert r.headers["location"] == "/postalada?oldal=1"
    assert rows("SELECT state FROM inbox_entries")[0][0] == "skipped"


def test_existing_feed_rows_get_their_description(alice, emails, fake_youtube):
    fake_youtube.feeds[GARDEN] = [("vid-new-001", "Metszés", iso(-1))]
    follow(alice, GARDEN)
    poll()
    assert rows("SELECT description FROM channel_videos")[0][0] is None
    fake_youtube.feeds[GARDEN] = [("vid-new-001", "Metszés", iso(-1), "Most már van leírás.")]
    run_sql("UPDATE channels SET last_checked_at = NULL")
    poll()
    assert rows("SELECT description FROM channel_videos")[0][0] == "Most már van leírás."


# ---- 19.2 sharing from the phone ---------------------------------------------------------------------------------

def test_share_page_choices_and_submit(alice, anon):
    r = alice.get("/megosztas", params={"title": "Kerti videó", "text": f"Nézd meg: https://youtu.be/{VIDEO}?si=x1"})
    assert r.status_code == 200
    page = r.text
    assert page.index("<h1>Összefoglaló kérése</h1>") < page.index('class="card share"')  # stacked, not side by side
    assert f'value="https://www.youtube.com/watch?v={VIDEO}"' in page and "Kerti videó" in page
    assert 'name="megnezendo"' in page
    r = post(alice, "/megosztas", {"url": f"https://www.youtube.com/watch?v={VIDEO}", "mode": "captions",
                                   "summarize": "on"})
    assert r.status_code == 303 and r.headers["location"].startswith("/elem/")
    item_id = int(r.headers["location"].rsplit("/", 1)[1])
    token = pair_runner(alice, anon)
    assert claim(anon, token).json()["input"]["options"]["mode"] == "captions"
    r = alice.get("/megosztas", params={"url": f"https://www.youtube.com/watch?v={VIDEO}"})  # already there
    assert r.status_code == 303 and r.headers["location"] == f"/elem/{item_id}"
    web = alice.get("/megosztas", params={"text": "https://example.com/cikk"}).text
    assert 'value="captions"' not in web and "Megnézendő is" not in web  # a web page: no captions, no playlist
    assert alice.get("/megosztas", params={"text": "csak szöveg"}).status_code == 422


def test_share_with_watch_later_goes_through_the_consent(alice, emails, fake_google):
    r = post(alice, "/megosztas", {"url": f"https://www.youtube.com/watch?v={VIDEO}", "mode": "captions",
                                   "summarize": "on", "megnezendo": "on", "cim": "Kerti videó"})
    item_id = rows("SELECT id FROM items")[0][0]
    r = give_consent(alice, fake_google, r)
    assert r.headers["location"] == f"/elem/{item_id}"
    assert rows("SELECT video_id FROM watch_later") == [(VIDEO,)]


def test_share_mode_start_runs_at_once_only_for_a_real_share(alice, anon):
    r = post(alice, "/beallitasok", {"share_mode": "start", "captions_policy": "manual"})
    assert r.status_code == 200 and rows("SELECT share_mode FROM users WHERE email = 'alice@example.com'") == [
        ("start",)]
    assert 'value="start" checked' in alice.get("/beallitasok").text
    link = {"text": f"https://www.youtube.com/watch?v={VIDEO}"}
    r = alice.get("/megosztas", params=link, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200 and rows("SELECT count(*) FROM items")[0][0] == 0  # a link elsewhere: the page
    r = alice.get("/megosztas", params=link, headers={"Sec-Fetch-Site": "none"})
    assert r.status_code == 303 and r.headers["location"].startswith("/elem/")
    token = pair_runner(alice, anon)
    job = claim(anon, token).json()
    assert job["input"]["options"]["mode"] == "captions" and job["kind"] == "transcribe"


def test_a_share_while_signed_out_continues_after_the_sign_in(alice, client_factory, fake_google):
    client = client_factory()
    r = client.get("/megosztas", params={"text": f"https://youtu.be/{VIDEO}"})
    assert r.status_code == 303 and r.headers["location"] == "/belepes"
    r = google_login(client, fake_google, "alice@example.com", "sub-alice")
    assert r.status_code == 303 and r.headers["location"].startswith("/megosztas?"), r.headers
    assert "Összefoglaló kérése" in client.get(r.headers["location"]).text
    r = google_login(client, fake_google, "alice@example.com", "sub-alice")  # used once
    assert r.headers["location"] == "/"


def test_installable_web_app_with_a_share_target(anon):
    r = anon.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    data = r.json()
    assert data["share_target"]["action"] == "/megosztas" and data["share_target"]["method"] == "GET"
    assert {"192x192", "512x512"} <= {i["sizes"] for i in data["icons"]}
    for icon in data["icons"]:
        assert anon.get(icon["src"]).status_code == 200
    sw = anon.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert 'rel="manifest"' in anon.get("/belepes").text
