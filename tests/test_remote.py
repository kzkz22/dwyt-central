"""Round 7d: one-line install codes and remote settings (protocol: "Telepítés egy paranccsal",
"Távoli beállítás")."""
import base64
import re

from conftest import CAPS, PROTO, claim, pair_runner, post, runner_headers
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import text

KEY = rsa.generate_private_key(public_exponent=65537, key_size=3072)
PEM = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                    serialization.PublicFormat.SubjectPublicKeyInfo).decode()

DESCRIBE = {"groups": [
    {"id": "connection", "title": "Kapcsolat a dwyt-vel", "settings": [
        {"key": "server", "kind": "url", "label": "A dwyt címe", "help": "", "remote": False, "readonly": True,
         "advanced": False, "choices": [], "show_if": [], "unit": ""},
        {"key": "token", "kind": "secret", "label": "Párosítási token", "help": "", "remote": False,
         "readonly": True, "advanced": False, "choices": [], "show_if": [], "unit": ""},
        {"key": "name", "kind": "text", "label": "A futtató neve", "help": "", "remote": True, "readonly": False,
         "advanced": False, "choices": [], "show_if": [], "unit": ""}]},
    {"id": "local", "title": "Helyi átírás ezen a gépen", "settings": [
        {"key": "polite_mode", "kind": "bool", "label": "Udvarias mód", "help": "", "remote": True,
         "readonly": False, "advanced": False, "choices": [], "show_if": [], "unit": ""},
        {"key": "polite_wait_min", "kind": "int", "label": "Udvarias mód: leghosszabb várakozás", "help": "",
         "remote": True, "readonly": False, "advanced": False, "choices": [], "unit": "perc",
         "show_if": [{"key": "polite_mode", "value": True}]},
        {"key": "whisper_cli", "kind": "path", "label": "A whisper.cpp helye", "help": "", "remote": False,
         "readonly": False, "advanced": True, "choices": [], "show_if": [], "unit": ""}]},
    {"id": "llm", "title": "Összefoglaló", "settings": [
        {"key": "llm_key", "kind": "secret", "label": "Az LLM-szerver kulcsa", "help": "", "remote": True,
         "readonly": False, "advanced": False, "choices": [], "show_if": [], "unit": ""},
        {"key": "llm_reasoning", "kind": "choice", "label": "Gondolkodás", "help": "", "remote": True,
         "readonly": False, "advanced": False, "show_if": [], "unit": "",
         "choices": [{"value": "auto", "label": "a modell alapértelmezése"}, {"value": "on", "label": "be"},
                     {"value": "off", "label": "ki"}]},
        {"key": "allowed_domains", "kind": "list", "label": "Engedélyezett oldalak", "help": "", "remote": True,
         "readonly": False, "advanced": False, "choices": [], "show_if": [], "unit": ""}]}]}
VALUES = {"server": "http://testserver", "name": "Otthoni LXC", "polite_mode": "igen", "polite_wait_min": "10",
          "whisper_cli": "", "llm_reasoning": "auto", "allowed_domains": "youtube.com, youtu.be"}


def report(anon, token, **over):
    body = {"runner_version": "0.10.0", "latest_version": "0.10.0", "install": "service", "remote": True,
            "public_key": PEM, "describe": DESCRIBE, "mode": "egyeni", "values": dict(VALUES),
            "secrets_set": ["llm_key"],
            "modes": [{"id": "felho", "title": "Felhő", "text": "átírás felhőben", "values": {"polite_mode": "nem"}}]}
    body.update(over)
    r = anon.put("/runner/v1/settings", json=body, headers=runner_headers(token))
    assert r.status_code == 200, r.text
    return r.json()


def rows(sql, **params):
    from central import db
    with db.system_engine().begin() as conn:
        result = conn.execute(text(sql), params)
        return result.all() if result.returns_rows else []


def install_code(client) -> str:
    r = post(client, "/futtatok/telepitokod")
    assert r.status_code == 200, r.text
    m = re.search(r"futtato\.sh \| sh -s -- ([A-Z0-9-]+)", r.text)
    assert m and "5 percig érvényes" in r.text
    return m.group(1)


def test_install_command_redeems_once_without_approval(alice, anon):
    code = install_code(alice)
    script = anon.get("/futtato.sh")
    assert script.status_code == 200 and 'CENTRAL="http://testserver"' in script.text
    assert "@CENTRAL_URL@" not in script.text and code not in script.text
    r = anon.post("/runner/v1/pair/redeem?format=env", json={"code": code.lower(), "name": "lxc"}, headers=PROTO)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    env = dict(line.split("=", 1) for line in r.text.strip().splitlines())
    token = env["DWYT_TOKEN"]
    assert claim(anon, token).status_code == 204  # a working runner token
    assert "lxc" in alice.get("/futtatok").text
    again = anon.post("/runner/v1/pair/redeem?format=env", json={"code": code, "name": "x"}, headers=PROTO)
    assert again.status_code == 410 and again.text.startswith("ERROR=")
    # an expired code is refused as well
    code2 = install_code(alice)
    rows("UPDATE runner_install_codes SET expires_at = now() - interval '1 second' WHERE used_at IS NULL")
    r = anon.post("/runner/v1/pair/redeem", json={"code": code2, "name": "y"}, headers=PROTO)
    assert r.status_code == 410 and "lejárt" in r.json()["error"]


def test_settings_page_change_is_delivered_and_logged(alice, anon):
    token = pair_runner(alice, anon)
    runner_id = rows("SELECT id FROM runners")[0][0]
    page = alice.get(f"/futtatok/{runner_id}/beallitas").text
    assert "nem engedi" in page
    assert report(anon, token) == {"pending": 0}
    page = alice.get("/futtatok").text
    assert "a dwyt-ből állítható" in page and f"/futtatok/{runner_id}/beallitas" in page
    page = alice.get(f"/futtatok/{runner_id}/beallitas").text
    assert "Udvarias mód: leghosszabb várakozás" in page and 'name="v_polite_wait_min"' in page
    assert 'name="v_whisper_cli"' in page and "csak a futtató gépén állítható" in page
    assert 'data-public-key="-----BEGIN PUBLIC KEY-----' in page and 'name="enc_llm_key"' in page
    assert "Párosítási token" not in page and 'data-secret="llm_key"' in page
    oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    secret = base64.b64encode(KEY.public_key().encrypt(b"sk-or-titkos", oaep)).decode()
    r = post(alice, f"/futtatok/{runner_id}/beallitas", {
        "v_name": "Otthoni LXC", "v_polite_mode": "on", "v_polite_wait_min": "15", "v_llm_reasoning": "auto",
        "v_allowed_domains": "youtube.com,youtu.be", "enc_llm_key": secret})
    assert r.status_code == 303, r.text
    page = alice.get(f"/futtatok/{runner_id}/beallitas").text
    assert "változás vár átadásra" in page and "Udvarias mód: leghosszabb várakozás: 15 perc" in page
    assert "sk-or-titkos" not in page
    assert alice.get(f"/futtatok/{runner_id}/beallitas/allapot").status_code == 204
    # the long poll answers at once and says so
    r = claim(anon, token)
    assert r.status_code == 204 and r.headers.get("dwyt-settings") == "pending"
    changes = anon.get("/runner/v1/settings/changes", headers=runner_headers(token)).json()["changes"]
    assert len(changes) == 1 and changes[0]["values"] == {"polite_wait_min": "15"}
    assert changes[0]["secrets"] == {"llm_key": secret} and changes[0]["action"] is None
    plain = KEY.decrypt(base64.b64decode(changes[0]["secrets"]["llm_key"]),
                        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    assert plain == b"sk-or-titkos"
    assert "dwyt-settings" not in claim(anon, token).headers  # handed out: no longer pending
    r = anon.post(f"/runner/v1/settings/changes/{changes[0]['id']}/result", headers=runner_headers(token),
                  json={"applied": ["polite_wait_min", "llm_key"], "errors": {},
                        "checks": {"llm_key": [True, "rendben"]}})
    assert r.status_code == 200
    assert rows("SELECT secrets FROM runner_setting_changes")[0][0] is None  # the ciphertext is gone
    assert alice.get(f"/futtatok/{runner_id}/beallitas/allapot").headers["hx-redirect"].endswith("/beallitas")
    page = alice.get(f"/futtatok/{runner_id}/beallitas").text
    assert "Átvette" in page and "Kulcspróba: rendben" in page and "a tartalma nem kerül a naplóba" in page
    # the next report carries the new value: nothing logged as changed on the machine
    report(anon, token, values={**VALUES, "polite_wait_min": "15"})
    assert rows("SELECT count(*) FROM runner_setting_changes WHERE origin = 'runner'")[0][0] == 0
    # a change made on the runner's machine is logged
    report(anon, token, values={**VALUES, "polite_wait_min": "15", "llm_reasoning": "on"}, secrets_set=[])
    page = alice.get(f"/futtatok/{runner_id}/beallitas").text
    assert "a futtató gépén" in page and "Gondolkodás: a modell alapértelmezése → be" in page
    assert "helyben állították" in page and "Az LLM-szerver kulcsa: törölve" in page


def test_refusals_rename_and_what_cannot_be_set(alice, bob, anon):
    token = pair_runner(alice, anon)
    runner_id = rows("SELECT id FROM runners")[0][0]
    report(anon, token)
    base = {"v_name": "Otthoni LXC", "v_polite_mode": "on", "v_polite_wait_min": "10", "v_llm_reasoning": "auto",
            "v_allowed_domains": "youtube.com, youtu.be"}
    r = post(alice, f"/futtatok/{runner_id}/beallitas", base)
    assert "Nem változott semmi." in r.text
    # a field the runner keeps for itself is ignored, even if posted
    r = post(alice, f"/futtatok/{runner_id}/beallitas", {**base, "v_whisper_cli": "/tmp/x"})
    assert "Nem változott semmi." in r.text
    # bob can neither see nor change alice's runner
    assert bob.get(f"/futtatok/{runner_id}/beallitas").status_code == 404
    assert post(bob, f"/futtatok/{runner_id}/beallitas", {**base, "v_polite_wait_min": "1"}).status_code == 404
    r = post(alice, f"/futtatok/{runner_id}/beallitas", {**base, "v_polite_wait_min": "999", "v_name": "Pince"})
    assert r.status_code == 303
    change = anon.get("/runner/v1/settings/changes", headers=runner_headers(token)).json()["changes"][0]
    anon.post(f"/runner/v1/settings/changes/{change['id']}/result", headers=runner_headers(token),
              json={"applied": ["name"], "errors": {"polite_wait_min": "0 és 120 között"}})
    page = alice.get(f"/futtatok/{runner_id}/beallitas").text
    assert "Nem vette át" in page and "0 és 120 között" in page and "A többi változást átvette." in page
    assert "Pince" in alice.get("/futtatok").text  # the name in dwyt follows the runner's
    # a runner that switches remote settings off: the page says so, saving is refused
    report(anon, token, remote=False)
    page = alice.get(f"/futtatok/{runner_id}/beallitas").text
    assert "nem engedi" in page and "v_polite_mode" not in page
    r = post(alice, f"/futtatok/{runner_id}/beallitas", base)
    assert r.status_code == 404 or "nem engedi" in r.text


def test_update_now_and_an_unanswered_change(alice, anon, fake_notify):
    from test_notify import use_ntfy
    use_ntfy(alice, fake_notify.url)
    token = pair_runner(alice, anon)
    runner_id = rows("SELECT id FROM runners")[0][0]
    claim(anon, token)  # the runner version is known from the claim
    report(anon, token, runner_version="0.10.0", latest_version="0.10.1")
    page = alice.get("/futtatok").text
    assert "újabb van: 0.10.1" in page and f"/futtatok/{runner_id}/frissites" in page
    r = post(alice, f"/futtatok/{runner_id}/frissites")
    assert r.status_code == 303
    assert "már folyamatban" in post(alice, f"/futtatok/{runner_id}/frissites").text
    change = anon.get("/runner/v1/settings/changes", headers=runner_headers(token)).json()["changes"][0]
    assert change["action"] == "update"
    rows("UPDATE runner_setting_changes SET delivered_at = now() - interval '11 minutes'")
    report(anon, token)
    page = alice.get(f"/futtatok/{runner_id}/beallitas").text
    assert "nem jelzett vissza" in page and "nem sikerült" in page
    titles = [r[0] for r in rows("SELECT title FROM notifications WHERE kind = 'runner' ORDER BY id")]
    assert titles == ["otthoni gép: frissítés kérve"]
    # an app runner never gets the update button
    report(anon, token, install="app", latest_version="0.10.2")
    assert f"/futtatok/{runner_id}/frissites" not in alice.get("/futtatok").text
    assert post(alice, f"/futtatok/{runner_id}/frissites").status_code == 422


def test_report_validation(alice, anon):
    token = pair_runner(alice, anon)
    r = anon.put("/runner/v1/settings", json={"remote": True, "public_key": "not a key"},
                 headers=runner_headers(token))
    assert r.status_code == 422
    assert anon.put("/runner/v1/settings", json={"remote": False}, headers=PROTO).status_code == 401
    assert claim(anon, token, CAPS).status_code == 204


def test_new_runner_tabs(alice):
    page = alice.get("/futtatok?ful=asztali").text
    assert "dwyt-runner[desktop,local]" in page and "dwyt-runner app" in page
    page = alice.get("/futtatok?ful=kod").text
    assert "Párosítás kóddal" in page and "Jóváhagyás" in page and "Telepítőparancs kérése" not in page
    page = alice.get("/futtatok").text  # no runner yet: the install command first
    assert "Telepítőparancs kérése" in page and "/futtato.sh" in page


def test_mode_cards_wrap_their_text():
    """Buttons do not wrap by default (white-space: nowrap); the mode cards' long texts must."""
    from pathlib import Path
    css = (Path(__file__).parent.parent / "central" / "static" / "style.css").read_text(encoding="utf-8")
    rule = css[css.index(".modes button {"):]
    rule = rule[:rule.index("}")]
    assert "white-space: normal" in rule and "min-width: 0" in rule
