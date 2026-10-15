"""Reading a summary aloud with the browser's voice (decision 19.7)."""
import json
import re

from conftest import pair_runner
from test_round8 import finish, submit

from central.render import speech_chunks


def test_speech_chunks_are_plain_short_sentences():
    text = ("## Fő gondolatok\n- **MiniCPM-V 4.7** egy 1B modell [03:12], amely telefonon is fut\n"
            "- Olvasta a kézírást ([1:12:05]) és [a táblázatot](https://example.com/t) `CSV`-be alakította.\n"
            "1. Első pont\n| Feladat | Eredmény |\n|---|---|\n| kézírás | jó |\n\n"
            "Hosszú mondat " + "szó, " * 80 + "vége.")
    chunks = speech_chunks(text)
    assert chunks[:6] == ["Fő gondolatok.", "MiniCPM-V 4.7 egy 1B modell, amely telefonon is fut.",
                          "Olvasta a kézírást és a táblázatot CSV-be alakította.", "Első pont.",
                          "Feladat, Eredmény.", "kézírás, jó."]
    assert all(len(c) <= 160 for c in chunks) and len(chunks) > 7
    assert "".join(chunks[6:]).replace(" ", "").startswith("Hosszúmondatszó,")
    assert not any(re.search(r"[#*\[\]`|]|\d:\d\d|https?:", c) for c in chunks)


def test_item_page_offers_reading_aloud_only_with_a_summary(alice, anon):
    token = pair_runner(alice, anon)
    item_id = submit(alice, summarize=False)
    finish(anon, token, summary=False)
    page = alice.get(f"/elem/{item_id}").text
    assert "data-speak" not in page and "speak.js" not in page
    other = submit(alice, video="abcdefghijk")
    finish(anon, token)
    page = alice.get(f"/elem/{other}").text
    # the script shows the player where the browser can speak
    assert "data-speak data-speak-key" in page and "data-speak-seek" in page and "speak.js" in page
    data = re.search(r'<script type="application/json" id="speech-text">(.*?)</script>', page, re.S).group(1)
    assert json.loads(data) == ["A videó a kertről szól, és a metszésről."]
    assert alice.get("/static/speak.js").status_code == 200
