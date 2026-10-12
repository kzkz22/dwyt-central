"""Paging of the lists (page size per user), the Inbox counter in the menu, the favicon."""
from conftest import pair_runner, post
from sqlalchemy import text
from test_library import deliver, submit

CHANNEL = "UC" + "a" * 22


def rows(sql: str, **params):
    from central import db
    with db.system_session() as s:
        return s.execute(text(sql), params).all()


def test_library_pages_and_the_remembered_page_size(alice, bob, anon):
    token = pair_runner(alice, anon)
    for i in range(17):
        submit(alice, video=f"vid{i:08d}")
        deliver(anon, token, title=f"Videó {i}")
    page = alice.get("/").text
    assert page.count('class="item media"') == 17  # default 25 per page
    assert "1. oldal / 1" in page and 'name="meret"' in page and "data-autosubmit" in page
    r = post(alice, "/lapmeret", {"meret": "15", "vissza": "/?q=Vide"})
    assert r.status_code == 303 and r.headers["location"] == "/?q=Vide"
    assert rows("SELECT page_size FROM users WHERE email = 'alice@example.com'")[0][0] == 15
    page = alice.get("/").text
    assert page.count('class="item media"') == 15 and "1. oldal / 2" in page and 'href="/?oldal=2"' in page
    second = alice.get("/", params={"oldal": 2, "q": "Vide"}).text
    assert second.count('class="item media"') == 2 and 'href="/?q=Vide"' in second  # previous keeps the filter
    assert alice.get("/", params={"oldal": 99}).text.count('class="item media"') == 2  # clamped to the last page
    # only 15 / 25 / 50, and never back to another site
    post(alice, "/lapmeret", {"meret": "1000", "vissza": "//evil.example/x"})
    assert rows("SELECT page_size FROM users WHERE email = 'alice@example.com'")[0][0] == 15
    assert post(alice, "/lapmeret", {"meret": "50", "vissza": "https://evil.example"}).headers["location"] == "/"
    assert rows("SELECT page_size FROM users WHERE email = 'bob@example.com'")[0][0] == 25  # per user


def test_other_lists_are_paged(alice, anon):
    from central import db
    from central.models import Conversation
    post(alice, "/lapmeret", {"meret": "15", "vissza": "/"})
    for i in range(17):  # no runner: the jobs wait
        submit(alice, video=f"job{i:08d}")
    jobs_page = alice.get("/munkak").text
    assert jobs_page.count("Megszakítás") == 15 and 'href="/munkak?oldal=2"' in jobs_page
    assert alice.get("/munkak/lista", params={"oldal": 2}).text.count("Megszakítás") == 2
    assert 'hx-get="/munkak/lista?oldal=2"' in alice.get("/munkak", params={"oldal": 2}).text  # polling keeps the page
    user_id = rows("SELECT id FROM users WHERE email = 'alice@example.com'")[0][0]
    with db.user_session(user_id) as s:
        s.add_all(Conversation(user_id=user_id, title=f"Kérdés {i}") for i in range(16))
    convs = alice.get("/beszelgetesek").text
    assert "1. oldal / 2" in convs and alice.get("/beszelgetesek?oldal=2").text.count('class="item conv"') == 1


def test_inbox_counter_and_paging(alice):
    assert "sb-count" not in alice.get("/").text
    user_id = rows("SELECT id FROM users WHERE email = 'alice@example.com'")[0][0]
    rows("INSERT INTO channels (id, title) VALUES (:c, 'Csatorna') RETURNING id", c=CHANNEL)
    for i in range(18):
        rows("INSERT INTO inbox_entries (user_id, channel_id, video_id, title, state) "
             "VALUES (:u, :c, :v, :t, :s) RETURNING id", u=user_id, c=CHANNEL, v=f"inb{i:08d}", t=f"Új {i}",
             s="new" if i < 17 else "skipped")
    page = alice.get("/").text
    assert '<span class="sb-count" aria-label="17 új videó vár">17</span>' in page
    assert 'class="dot"' in page  # on phones the menu button shows there is something
    post(alice, "/lapmeret", {"meret": "15", "vissza": "/postalada"})
    inbox = alice.get("/postalada").text
    assert inbox.count('class="item media"') == 15 and "17 videó" in inbox
    assert alice.get("/postalada?oldal=2").text.count('class="item media"') == 2


def test_favicon_and_layout_rules(alice, anon):
    page = alice.get("/").text
    assert 'rel="icon" href="/static/favicon.svg' in page and 'rel="apple-touch-icon"' in page
    r = anon.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert anon.get("/static/favicon.svg").status_code == 200
    css = anon.get("/static/style.css").text
    # the search field and the select next to it on one line (the general input rule's margin overridden)
    assert ".filters input:not([type=radio]):not([type=checkbox]):not([type=file]) {" in css
    assert ".mobile-top .mobile-spacer { flex: 1; }" in css  # phone header: logo next to the menu button
