import time

import pyotp
from conftest import csrf_of, google_login, make_admin, post
from sqlalchemy import select


def _invite(email="new@example.com"):
    from central import accounts, db
    from central.config import get_settings
    with db.system_session() as s:
        return accounts.create_invite(s, get_settings(), email, None).rsplit("/", 1)[1]


def test_pages_need_login(anon):
    for url in ("/", "/uj", "/munkak", "/futtatok", "/admin"):
        r = anon.get(url)
        assert r.status_code == 303 and r.headers["location"] == "/belepes", url
    assert "Belépés Google-lel" in anon.get("/belepes").text


def test_invitation_sign_in_binds_the_google_account(anon, fake_google):
    token = _invite()
    page = anon.get(f"/meghivo/{token}")
    assert "new@example.com" in page.text
    r = google_login(anon, fake_google, "New@Example.com", "sub-1", name="Új Ember", invite=token)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "Új Ember" in anon.get("/").text
    from central import db
    from central.models import Invite, User
    with db.system_session() as s:
        user = s.scalar(select(User))
        assert (user.google_sub, user.role) == ("sub-1", "user")
        assert s.scalar(select(Invite)).used_by == user.id
    # the invitation cannot be used twice
    assert anon.get(f"/meghivo/{token}").status_code == 404


def test_sign_in_is_refused_without_invitation_or_with_wrong_address(client_factory, fake_google, monkeypatch):
    c = client_factory()
    r = google_login(c, fake_google, "stranger@example.com", "sub-x")
    assert r.status_code == 403 and "meghívással" in r.text
    token = _invite("someone@example.com")
    r = google_login(c, fake_google, "other@example.com", "sub-y", invite=token)
    assert r.status_code == 403 and "másik e-mail-címre" in r.text
    r = google_login(c, fake_google, "someone@example.com", "sub-z", invite=token, verified=False)
    assert r.status_code == 403 and "nincs megerősítve" in r.text
    # open registration
    monkeypatch.setenv("REGISTRATION", "open")
    from central.config import get_settings
    get_settings.cache_clear()
    r = google_login(c, fake_google, "stranger@example.com", "sub-x")
    assert r.status_code == 303


def test_replayed_or_forged_state_is_refused(anon, fake_google):
    r = anon.get("/auth/google/callback?state=forged&code=abc")
    assert r.status_code == 403 and "lejárt vagy érvénytelen" in r.text
    token = _invite()
    first = google_login(anon, fake_google, "new@example.com", "sub-1", invite=token)
    assert first.status_code == 303
    # the same state again
    from urllib.parse import parse_qs, urlsplit
    start = anon.get("/auth/google/start")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    fake_google.codes["c"] = {"challenge": "wrong", "nonce": "n", "claims": {}}
    assert anon.get(f"/auth/google/callback?state={state}&code=c").status_code == 403
    assert anon.get(f"/auth/google/callback?state={state}&code=c").status_code == 403  # state consumed


def test_disabled_user_cannot_sign_in(alice, client_factory, fake_google):
    from central import db
    from central.models import User
    with db.system_session() as s:
        s.scalar(select(User)).disabled = True
    assert alice.get("/").status_code == 303  # existing session no longer valid
    r = google_login(client_factory(), fake_google, "alice@example.com", "sub-alice")
    assert r.status_code == 403 and "le van tiltva" in r.text


def test_admin_password_and_totp_and_google_binding(anon, client_factory, fake_google):
    make_admin()
    r = anon.post("/admin-belepes", data={"email": "admin@example.com", "password": "rossz"})
    assert r.status_code == 401
    r = anon.post("/admin-belepes", data={"email": "admin@example.com", "password": "nagyon-titkos-jelszo"})
    assert r.headers["location"] == "/admin-belepes/kod"
    page = anon.get("/admin-belepes/kod").text
    from central import db, security
    with db.system_session() as s:
        admin = security.user_by_email(s, "admin@example.com")
        secret = admin.totp_secret
    code = pyotp.TOTP(secret).at(time.time())
    r = anon.post("/admin-belepes/kod", data={"code": code, "csrf_token": csrf_of(page)})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "Meghívó készítése" in anon.get("/admin").text
    # the same address signs in with Google and is bound to the admin account
    g = client_factory()
    assert google_login(g, fake_google, "admin@example.com", "sub-admin").status_code == 303
    assert g.get("/admin").status_code == 200


def test_admin_invites_and_disables(anon, alice, client_factory, fake_google):
    make_admin()
    admin = client_factory()
    google_login(admin, fake_google, "admin@example.com", "sub-admin")
    admin.csrf = csrf_of(admin.get("/").text)
    r = post(admin, "/admin/meghivo", {"email": "carol@example.com"})
    assert "http://testserver/meghivo/" in r.text
    link = r.text.split("http://testserver/meghivo/")[1].split("<")[0]
    carol = client_factory()
    assert google_login(carol, fake_google, "carol@example.com", "sub-carol", invite=link).status_code == 303
    assert alice.get("/admin").status_code == 403
    from central import db
    from central.models import User
    with db.system_session() as s:
        alice_id = s.scalar(select(User.id).where(User.email == "alice@example.com"))
    post(admin, f"/admin/felhasznalo/{alice_id}/tiltas")
    assert alice.get("/").status_code == 303


def test_csrf_is_required(alice):
    r = alice.post("/uj", data={"url": "https://youtu.be/dQw4w9WgXcQ", "mode": "captions"})
    assert r.status_code == 403
