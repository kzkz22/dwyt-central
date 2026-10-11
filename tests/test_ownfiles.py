"""Own recordings (browser uploads, the runner's watched folder) and quotas (round 3b)."""
import hashlib
from pathlib import Path

from conftest import CAPS, PROTO, claim, csrf_of, google_login, make_admin, pair_runner, post, runner_headers
from conftest import transcript_result
from sqlalchemy import select, text

FILE_CAPS = {**CAPS, "transcribe_modes": ["captions", "cloud", "local"], "whisper_models": ["large-v3"],
             "source_kinds": ["upload", "local"]}
AUDIO = b"RIFF" + b"\x01" * 5000


def upload(client, data=AUDIO, name="../Interjú 2026.wav"):
    return client.put("/feltoltes", params={"name": name}, content=data,
                      headers={"X-CSRF-Token": client.csrf, "content-type": "application/octet-stream"})


def staging() -> Path:
    from central.config import get_settings
    return Path(get_settings().staging_dir)


def submit_upload(client, staging_id, mode="local", **extra):
    return post(client, "/uj", {"source": "upload", "staging_id": str(staging_id), "mode": mode,
                                "model_local": "large-v3", "summarize": "on", **extra})


def deliver(anon, token, job, **kw):
    r = anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=transcript_result(mode="local", **kw),
                 headers=runner_headers(token))
    assert r.status_code == 200, r.text


def test_browser_upload_goes_to_the_runner_and_is_deleted_after(alice, anon):
    token = pair_runner(alice, anon)
    assert alice.put("/feltoltes", content=AUDIO).status_code == 403  # no CSRF header
    r = upload(alice)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Interjú 2026.wav" and body["size"] == len(AUDIO)
    sha = hashlib.sha256(AUDIO).hexdigest()
    assert (staging() / sha).read_bytes() == AUDIO

    r = submit_upload(alice, body["staging_id"], mode="captions")
    assert r.status_code == 422 and "YouTube-felirat nem kérhető" in r.text
    r = submit_upload(alice, body["staging_id"])
    assert r.status_code == 303, r.text
    item_id = int(r.headers["location"].rsplit("/", 1)[1])

    assert claim(anon, token).status_code == 204  # a runner that does not take files gets nothing
    job = claim(anon, token, FILE_CAPS).json()
    src = job["input"]["source"]
    assert src == {"kind": "upload", "sha256": sha, "name": "Interjú 2026.wav", "size": len(AUDIO),
                   "media": f"/runner/v1/jobs/{job['job_id']}/media"}
    media = anon.get(src["media"], headers=runner_headers(token))
    assert media.status_code == 200 and media.content == AUDIO
    part = anon.get(src["media"], headers={**runner_headers(token), "Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == AUDIO[10:20]
    assert anon.get(src["media"], headers=PROTO).status_code == 401

    deliver(anon, token, job)
    assert not (staging() / sha).exists()  # deleted once the runner delivered
    summary_job = claim(anon, token, FILE_CAPS).json()
    assert summary_job["kind"] == "summarize" and summary_job["input"]["role"] == "private"
    page = alice.get(f"/elem/{item_id}").text
    assert "Interjú 2026.wav" in page
    from central import db
    with db.system_session() as s:
        shareable = s.execute(text("SELECT shareable, private FROM transcripts")).one()
        meta = s.scalar(text("SELECT meta FROM sources WHERE kind = 'upload'"))
    assert shareable == (False, True) and "title" not in meta  # the shared source keeps no file name


def test_the_same_file_of_another_user_is_not_taken_over(alice, bob, anon):
    a = pair_runner(alice, anon)
    b = pair_runner(bob, anon)
    submit_upload(alice, upload(alice).json()["staging_id"])
    deliver(anon, a, claim(anon, a, FILE_CAPS).json())
    r = submit_upload(bob, upload(bob, name="bob.wav").json()["staging_id"])
    assert r.status_code == 303
    job = claim(anon, b, FILE_CAPS).json()
    assert job["kind"] == "transcribe"  # bob's runner does the work: own recordings are never shared
    assert anon.get(job["input"]["source"]["media"], headers=runner_headers(b)).content == AUDIO


def test_upload_limits(alice, anon, client_factory, fake_google):
    from central import db
    with db.system_session() as s:
        s.execute(text("UPDATE users SET quota_upload_mb = 1 WHERE email = 'alice@example.com'"))
    r = upload(alice, data=b"x" * (1024 ** 2 + 1))
    assert r.status_code == 413 and "nagyobb a megengedettnél" in r.json()["error"]
    assert upload(alice, data=b"a").status_code == 200
    assert upload(alice, data=b"b").status_code == 200
    r = upload(alice, data=b"c")
    assert r.status_code == 413 and "legfeljebb 2 feltöltés" in r.json()["error"]
    assert "feldolgozásra vár 2 / 2" in alice.get("/beallitasok").text


def test_storage_quota_blocks_new_work(alice, anon):
    from central import db
    with db.system_session() as s:
        s.execute(text("UPDATE users SET quota_storage_mb = 0 WHERE email = 'alice@example.com'"))
    r = post(alice, "/uj", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "captions"})
    assert r.status_code == 422 and "Elfogyott a tárhelyed" in r.text


def test_admin_sets_quotas(anon, alice, client_factory, fake_google):
    make_admin()
    admin = client_factory()
    google_login(admin, fake_google, "admin@example.com", "sub-admin")
    admin.csrf = csrf_of(admin.get("/").text)
    from central import db
    from central.models import User
    with db.system_session() as s:
        alice_id = s.scalar(select(User.id).where(User.email == "alice@example.com"))
    r = post(admin, f"/admin/felhasznalo/{alice_id}/kvota", {"storage_mb": "5000", "upload_mb": "x"})
    assert r.status_code == 303
    with db.system_session() as s:
        u = s.get(User, alice_id)
        assert (u.quota_storage_mb, u.quota_upload_mb) == (5000, None)
    assert "/ 5000 MB" in admin.get("/admin").text
    assert post(alice, f"/admin/felhasznalo/{alice_id}/kvota", {"storage_mb": "99999"}).status_code == 403


def test_watched_folder_item_belongs_to_the_runner_that_has_the_file(alice, anon):
    first = pair_runner(alice, anon, name="laptop")
    second = pair_runner(alice, anon, name="vps")
    sha = "a" * 64
    body = {"sha256": sha, "name": "C:\\Felvételek\\megbeszélés.m4a", "size": 1234,
            "options": {"mode": "local", "model": "large-v3", "language": "hu"}, "summarize": True}
    r = anon.post("/runner/v1/local-items", json=body, headers=runner_headers(first))
    assert r.status_code == 200, r.text
    reg = r.json()
    assert reg["existing"] is False and reg["job_id"]
    again = anon.post("/runner/v1/local-items", json=body, headers=runner_headers(first)).json()
    assert again["job_id"] == reg["job_id"]  # registering twice does not double the work
    assert claim(anon, second, FILE_CAPS).status_code == 204  # the other runner has no such file
    job = claim(anon, first, FILE_CAPS).json()
    assert job["input"]["source"] == {"kind": "local", "sha256": sha, "name": "megbeszélés.m4a", "size": 1234}
    assert job["input"]["options"]["language"] == "hu"
    deliver(anon, first, job)
    done = anon.post("/runner/v1/local-items", json=body, headers=runner_headers(first)).json()
    assert done == {"item_id": reg["item_id"], "job_id": None, "existing": True}
    assert "megbeszélés.m4a" in alice.get(f"/elem/{reg['item_id']}").text

    bad = {**body, "sha256": "b" * 64, "options": {"mode": "captions"}}
    r = anon.post("/runner/v1/local-items", json=bad, headers=runner_headers(first))
    assert r.status_code == 422 and "YouTube-felirat" in r.json()["error"]


def test_expired_uploads_are_cleaned_up(alice):
    from central import db, ownfiles
    from central.config import get_settings
    staging_id = upload(alice, data=b"old file").json()["staging_id"]
    sha = hashlib.sha256(b"old file").hexdigest()
    with db.system_session() as s:
        s.execute(text("UPDATE uploads SET created_at = now() - interval '30 days' WHERE id = :i"), {"i": staging_id})
    ownfiles.housekeeping(get_settings())
    assert not (staging() / sha).exists()
    r = submit_upload(alice, staging_id)
    assert r.status_code == 422 and "már nem használható" in r.text


def test_static_files_have_versioned_urls(alice):
    import re
    page = alice.get("/uj")
    assert 'id="upload-file"' in page.text
    url = re.search(r'src="(/static/app\.js\?v=[0-9a-f]{12})"', page.text).group(1)
    js = alice.get(url)
    assert js.status_code == 200 and "uploadFile" in js.text
    assert "immutable" in js.headers["cache-control"]  # a changed file gets a new URL instead
    assert alice.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert page.headers["cache-control"] == "no-store"
