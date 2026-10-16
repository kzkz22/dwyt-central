"""Decision 19.10: a page with a video or podcast on it (Telex, 444, Index…). The runner finds the recording; the
central keeps the page's words and recordings, shows them on the item page and lets the user transcribe another one."""
from conftest import CAPS, pair_runner, post, runner_headers, transcript_result
from test_subscriptions import rows

PAGE = "https://telex.hu/video/2026/09/27/interju"
YT = "I3eE-GoA57M"
MP3 = "https://audio-assets.telex.hu/podcast/interju_20260927.mp3"
PAGE_CAPS = {**CAPS, "transcribe_modes": ["captions", "cloud", "local"], "allowed_domains": ["youtube.com", "telex.hu"],
             "whisper_models": ["large-v3", "large-v3-turbo"]}


def claim(client, token):
    from conftest import claim as plain
    return plain(client, token, PAGE_CAPS)


def page_result(chosen="youtube"):
    youtube = {"kind": "youtube", "url": f"https://www.youtube.com/watch?v={YT}", "label": "YouTube-videó",
               "video_id": YT}
    audio = {"kind": "dayfile", "url": MP3, "label": "Hangfájl a cikk napjáról (audio-assets.telex.hu)"}
    result = transcript_result(title="Interjú", channel="Telex")
    result["page"] = {"url": PAGE, "title": "Interjú", "site": "Telex", "published": "2026-09-27T08:00:00+02:00",
                      "text": "A beszélgetés kérdései: üzlet, cipők, Gen-EU.",
                      "chosen": youtube if chosen == "youtube" else audio, "candidates": [youtube, audio]}
    return result


def submit_page(client, mode="captions"):
    r = post(client, "/uj", {"source": "url", "url": PAGE, "mode": mode, "summarize": "on"})
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_a_page_result_is_kept_and_shown_and_feeds_the_summary(alice, anon):
    token = pair_runner(alice, anon)
    item_id = submit_page(alice)  # captions on a page are fine: the runner decides whether there is a YouTube video
    job = claim(anon, token).json()
    assert job["input"]["source"]["url"] == PAGE and job["input"]["options"]["mode"] == "captions"
    assert anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=page_result(),
                    headers=runner_headers(token)).status_code == 200
    summary_job = claim(anon, token).json()
    assert summary_job["kind"] == "summarize"
    assert summary_job["input"]["meta"]["page_text"].startswith("A beszélgetés kérdései")
    page = alice.get(f"/elem/{item_id}").text
    assert "Az oldalról" in page and "ezt használtuk" in page and "Telex (videós oldal)" in page
    assert "Más is van az oldalon (1)" in page and f'name="pick" value="{MP3}"' in page
    assert 'name="mode" value="local"' in page  # captions only where there is a YouTube video
    assert f"https://i.ytimg.com/vi/{YT}/" in page
    library = alice.get("/").text
    assert f"https://i.ytimg.com/vi/{YT}/mqdefault.jpg" in library and ", az oldalról" in library
    meta = rows("SELECT meta FROM sources WHERE kind = 'url'")[0][0]
    assert meta["page"]["site"] == "Telex" and meta["video_id"] == YT


def test_another_recording_of_the_page_is_a_new_transcript(alice, anon):
    token = pair_runner(alice, anon)
    item_id = submit_page(alice)
    job = claim(anon, token).json()
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=page_result(), headers=runner_headers(token))
    claim(anon, token)  # the summary
    r = post(alice, f"/elem/{item_id}/ujrairas", {"pick": "https://example.com/mas.mp3", "mode": "local"})
    assert r.status_code == 422 and "nincs az oldal felvételei között" in r.text
    r = post(alice, f"/elem/{item_id}/ujrairas", {"pick": MP3, "mode": "captions"})
    assert r.status_code == 422 and "helyi vagy felhős" in r.text
    r = post(alice, f"/elem/{item_id}/ujrairas", {"pick": MP3, "mode": "local"})
    assert r.status_code == 303, r.text
    job = claim(anon, token).json()
    assert job["kind"] == "transcribe" and job["input"]["options"]["pick"] == MP3
    result = page_result(chosen="audio")
    result["transcript"] = transcript_result(mode="local")["transcript"]
    assert anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=result,
                    headers=runner_headers(token)).status_code == 200
    assert rows("SELECT count(*) FROM transcripts")[0][0] == 2
    used = rows("SELECT meta->'page'->'used' FROM sources WHERE kind = 'url'")[0][0]
    assert sorted(c["kind"] for c in used.values()) == ["dayfile", "youtube"]
    page = alice.get(f"/elem/{item_id}").text  # the main transcript is still the YouTube one
    assert page.index("ezt használtuk") > page.index("YouTube-videó")


def test_youtube_items_have_no_page_card(alice, anon):
    token = pair_runner(alice, anon)
    r = post(alice, "/uj", {"source": "url", "url": f"https://www.youtube.com/watch?v={YT}", "mode": "captions"})
    item_id = int(r.headers["location"].rsplit("/", 1)[1])
    job = claim(anon, token).json()
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=transcript_result(), headers=runner_headers(token))
    assert "Az oldalról" not in alice.get(f"/elem/{item_id}").text
    assert "Videó vagy oldal címe" in alice.get("/uj").text
