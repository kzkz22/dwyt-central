"""The page frame (design round): left sidebar, mobile menu, theme, vendored assets."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_signed_in_pages_have_the_sidebar_with_the_current_page_marked(alice):
    page = alice.get("/csatornak").text
    assert 'class="skip-link" href="#main"' in page and '<main id="main"' in page
    assert 'id="sidebar"' in page and 'aria-label="Fő navigáció"' in page
    current = re.findall(r'<a href="([^"]+)" class="sb-link" aria-current="page"', page)
    assert current == ["/csatornak"]
    assert 'href="/admin"' not in page  # not an admin
    assert "data-sidebar-open" in page and "data-sidebar-collapse" in page and "data-theme-toggle" in page
    # the library itself
    assert re.findall(r'<a href="([^"]+)" class="sb-link" aria-current="page"', alice.get("/").text) == ["/"]


def test_signed_out_pages_have_no_sidebar(anon):
    page = anon.get("/belepes").text
    assert 'id="sidebar"' not in page and 'class="bare-main"' in page and "Belépés Google-lel" in page


def test_frame_assets_are_served_versioned_and_packaged(alice, anon):
    page = alice.get("/").text
    for name in ("theme.js", "app.js", "style.css", "fonts/inter-latin-wght-normal.woff2"):
        url = re.search(rf'"(/static/{re.escape(name)}\?v=[0-9a-f]{{12}})"', page)
        assert url, name
        r = anon.get(url.group(1))
        assert r.status_code == 200 and "immutable" in r.headers.get("cache-control", ""), name
    # theme.js runs in <head> before the first paint: not deferred
    assert re.search(r'<script src="/static/theme\.js\?v=[0-9a-f]+"></script>', page)
    css = anon.get("/static/style.css").text
    assert 'url("fonts/inter-latin-ext-wght-normal.woff2")' in css  # Hungarian ő, ű
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "static/fonts/*" in data["tool"]["setuptools"]["package-data"]["central"]
