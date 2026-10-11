"""Round 5: tags, note, speaker names, export, TOTP after Google, account deletion, invite e-mail."""
import io
import re
import json
import time
import zipfile

import pyotp
from conftest import claim, csrf_of, google_login, make_admin, pair_runner, post, runner_headers, transcript_result
from sqlalchemy import text
from test_items import summarize_next
from test_library import submit, translate_next


def rows(sql: str):
    from central import db
    with db.system_session() as s:
        return s.execute(text(sql)).all()


def cloud_item(client, anon, summarize=True):
    token = pair_runner(client, anon)
    r = post(client, "/uj", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "cloud",
                             "model_cloud": "auto", **({"summarize": "on"} if summarize else {})})
    item_id = int(r.headers["location"].rsplit("/", 1)[1])
    job = claim(anon, token).json()
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", headers=runner_headers(token),
             json=transcript_result(mode="cloud", language="en", texts=("Hello everyone.", "Tulips today.")))
    return item_id, token


def test_tags_and_note_filter_the_library(alice, bob, anon):
    token = pair_runner(alice, anon)
    garden = submit(alice)
    claim(anon, token)
    other = submit(alice, video="aaaaaaaaaaa")
    assert post(alice, f"/elem/{garden}/cimkek", {"tags": "kert, #Tavasz , kert,  ősz   eleje"}).status_code == 303
    assert {r[0] for r in rows("SELECT tag FROM item_tags")} == {"kert", "Tavasz", "ősz eleje"}
    assert post(alice, f"/elem/{garden}/jegyzet", {"note": "Megnézni a metszést is.\r\n"}).status_code == 303
    page = alice.get(f"/elem/{garden}").text
    assert "Megnézni a metszést is." in page
    shown = re.search(r'name="tags" value="([^"]*)"', page).group(1)  # order follows the database's collation
    assert sorted(shown.split(", ")) == ["Tavasz", "kert", "ősz eleje"]
    library = alice.get("/").text
    assert "kert (1)" in library and "Tavasz (1)" in library
    only = alice.get("/", params={"cimke": "TAVASZ"}).text
    assert f"/elem/{garden}" in only and f"/elem/{other}" not in only
    assert f"/elem/{garden}" in alice.get("/", params={"q": "metszés"}).text
    assert "kert (1)" not in bob.get("/").text
    assert post(bob, f"/elem/{garden}/cimkek", {"tags": "betörő"}).status_code == 303  # not his: nothing happens
    assert rows("SELECT count(*) FROM item_tags WHERE tag = 'betörő'")[0][0] == 0
    post(alice, f"/elem/{garden}/cimkek", {"tags": ""})
    assert rows("SELECT count(*) FROM item_tags")[0][0] == 0


def test_speaker_names_are_shown_everywhere_but_only_for_the_user(alice, anon):
    item_id, token = cloud_item(alice, anon)
    summarize_next(anon, token, body="## Összefoglaló\nSzereplő 1 a tulipánokról beszél.")
    tid = rows("SELECT id FROM transcripts")[0][0]
    r = post(alice, f"/elem/{item_id}/atirat/{tid}/beszelok",
             {"label_0": "Szereplő 1", "name_0": "Kovács  Anna", "label_1": "Szereplő 9", "name_1": "Senki"})
    assert r.status_code == 303
    assert rows("SELECT speaker_names FROM item_transcripts")[0][0] == {"Szereplő 1": "Kovács Anna"}
    page = alice.get(f"/elem/{item_id}").text
    assert "<strong>Kovács Anna:</strong>" in page and "Kovács Anna a tulipánokról beszél." in page
    txt = alice.get(f"/elem/{item_id}/atirat/{tid}.txt").text
    assert "Kovács Anna: Hello everyone." in txt and "Beszélők: Kovács Anna" in txt
    assert "[Kovács Anna] Hello everyone." in alice.get(f"/elem/{item_id}/atirat/{tid}.srt").text
    assert rows("SELECT count(*) FROM segments WHERE speaker = 'Szereplő 1'")[0][0] == 2  # shared result unchanged


def test_export_markdown_and_json(alice, bob, anon):
    item_id, token = cloud_item(alice, anon)
    summarize_next(anon, token, body="## Összefoglaló\nTulipánok.")
    translate_next(anon, token)
    post(alice, f"/elem/{item_id}/cimkek", {"tags": "kert"})
    post(alice, f"/elem/{item_id}/jegyzet", {"note": "fontos"})
    r = alice.get("/export", params={"formatum": "json"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    data = json.loads(zipfile.ZipFile(io.BytesIO(r.content)).read("dwyt/konyvtar.json"))
    entry = data["elemek"][0]
    assert entry["cimkek"] == ["kert"] and entry["jegyzet"] == "fontos"
    assert entry["osszefoglalo"]["szoveg"].endswith("Tulipánok.")
    assert entry["atirat"]["szegmensek"][0] == {"kezdet": 0.0, "veg": 4.0, "beszelo": "Szereplő 1",
                                                "szoveg": "Hello everyone.", "magyar": "HU Hello everyone."}
    md = zipfile.ZipFile(io.BytesIO(alice.get("/export").content))
    name = md.namelist()[0]
    assert name.startswith("dwyt/0001-") and name.endswith(".md")
    body = md.read(name).decode()
    assert "## Jegyzet\n\nfontos" in body and "**Szereplő 1:** Hello everyone. Tulips today." in body
    assert "> HU Hello everyone. HU Tulips today." in body
    empty = json.loads(zipfile.ZipFile(io.BytesIO(bob.get("/export?formatum=json").content)).read("dwyt/konyvtar.json"))
    assert empty["elemek"] == []


def enable_totp(client) -> str:
    page = post(client, "/beallitasok/kod/bekapcsolas").text
    secret = page.split('<code class="token">')[1].split("<")[0]
    assert "<svg" in page
    r = post(client, "/beallitasok/kod/megerosites", {"code": "000000"})
    assert r.status_code == 422
    r = post(client, "/beallitasok/kod/megerosites", {"code": pyotp.TOTP(secret).now()})
    assert "bekapcsolva" in r.text
    return secret


def test_own_totp_after_google(alice, client_factory, fake_google):
    secret = enable_totp(alice)
    browser = client_factory()
    r = google_login(browser, fake_google, "alice@example.com", "sub-alice")
    assert r.headers["location"] == "/belepes/kod"
    assert browser.get("/").status_code == 303  # not signed in yet
    page = browser.get("/belepes/kod").text
    assert "Admin" not in page.split("<h1>")[1][:20]
    r = browser.post("/belepes/kod", data={"code": "123456", "csrf_token": csrf_of(page)})
    assert r.status_code == 401
    r = browser.post("/belepes/kod", data={"code": pyotp.TOTP(secret).at(time.time() + 30),
                                           "csrf_token": csrf_of(page)})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert browser.get("/").status_code == 200
    browser.csrf = csrf_of(browser.get("/").text)
    from test_subscriptions import run_sql
    run_sql("UPDATE users SET totp_last_step = 0")  # the next 30 s code (used just now) would be refused
    r = post(browser, "/beallitasok/kod/kikapcsolas", {"code": pyotp.TOTP(secret).now()})
    assert "kikapcsolva" in r.text
    assert rows("SELECT totp_secret IS NULL, totp_after_google FROM users WHERE email = 'alice@example.com'")[0] \
        == (True, False)


def test_required_totp_is_set_up_at_sign_in(client_factory, fake_google, monkeypatch):
    from central import accounts, db
    from central.config import get_settings
    monkeypatch.setenv("REQUIRE_TOTP", "true")
    get_settings.cache_clear()
    with db.system_session() as s:
        link = accounts.create_invite(s, get_settings(), "dave@example.com", None)
    dave = client_factory()
    r = google_login(dave, fake_google, "dave@example.com", "sub-dave", invite=link.rsplit("/", 1)[1])
    assert r.headers["location"] == "/belepes/kod-beallitas"
    page = dave.get("/belepes/kod-beallitas").text
    secret = rows("SELECT totp_pending_secret FROM users WHERE email = 'dave@example.com'")[0][0]
    assert secret in page and "<svg" in page
    r = dave.post("/belepes/kod-beallitas", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf_of(page)})
    assert r.status_code == 303 and r.headers["location"] == "/"
    dave.csrf = csrf_of(dave.get("/").text)
    assert "Ezen a központon kötelező" in dave.get("/beallitasok").text
    r = post(dave, "/beallitasok/kod/kikapcsolas", {"code": pyotp.TOTP(secret).at(time.time() + 30)})
    assert r.status_code == 422


def test_account_deletion_keeps_what_others_took_over(alice, bob, anon):
    token = pair_runner(alice, anon)
    item_id = submit(alice)
    job = claim(anon, token).json()
    anon.put(f"/runner/v1/jobs/{job['job_id']}/result", json=transcript_result(), headers=runner_headers(token))
    submit(bob)  # reuses alice's transcript
    r = post(alice, "/beallitasok/fiok-torlese", {"email": "valaki@example.com"})
    assert r.status_code == 422
    r = post(alice, "/beallitasok/fiok-torlese", {"email": "ALICE@example.com"})
    assert r.status_code == 200 and "A fiókod törölve" in r.text
    assert alice.get("/").status_code == 303
    assert rows("SELECT count(*) FROM users WHERE email = 'alice@example.com'")[0][0] == 0
    assert rows(f"SELECT count(*) FROM items WHERE id = {item_id}")[0][0] == 0
    assert rows("SELECT count(*) FROM runners")[0][0] == 0
    assert "Kerti videó" in bob.get("/").text and rows("SELECT origin_user_id FROM transcripts")[0][0] is None


def test_the_last_admin_cannot_delete_himself(client_factory, fake_google):
    make_admin()
    admin = client_factory()
    google_login(admin, fake_google, "admin@example.com", "sub-admin")
    admin.csrf = csrf_of(admin.get("/").text)
    r = post(admin, "/beallitasok/fiok-torlese", {"email": "admin@example.com"})
    assert r.status_code == 422 and "utolsó admin" in r.text


def test_invite_by_email(client_factory, fake_google, monkeypatch):
    sent = []

    class FakeSmtp:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            sent.append(("starttls",))

        def login(self, user, password):
            sent.append(("login", user))

        def send_message(self, msg):
            sent.append(("message", msg["To"], msg["Subject"], msg.get_content()))

    from central import mailer
    from central.config import get_settings
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "dwyt@example.com")
    monkeypatch.setenv("SMTP_USER", "dwyt")
    get_settings.cache_clear()
    make_admin()
    admin = client_factory()
    google_login(admin, fake_google, "admin@example.com", "sub-admin")
    admin.csrf = csrf_of(admin.get("/").text)
    r = post(admin, "/admin/meghivo", {"email": "carol@example.com"})
    assert "Meghívó elküldve e-mailben" in r.text
    message = next(x for x in sent if x[0] == "message")
    assert message[1] == "carol@example.com" and "http://testserver/meghivo/" in message[3]
    assert ("starttls",) in sent and ("login", "dwyt") in sent
