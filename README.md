# dwyt-central

A **dwyt** központja: webes felület, belépés (Google + meghívás), felhasználónként külön könyvtár, munkasor és a futtatók protokollja. A számítást nem maga végzi: a munkákat a felhasználók saját futtatói ([dwyt-runner](https://github.com/kzkz22/dwyt-runner)) kérik el és adják le. Kulcsot (AssemblyAI, LLM) nem tárol.

- Terv és döntések: [dwyt-docs/docs/terv.md](https://github.com/kzkz22/dwyt-docs/blob/main/docs/terv.md)
- Központ–futtató protokoll: [dwyt-docs/docs/protokoll.md](https://github.com/kzkz22/dwyt-docs/blob/main/docs/protokoll.md)
- [Telepítés](docs/telepites.md) · [Frissítés](docs/frissites.md) · [MCP-kliensek](docs/mcp.md)

## Állapot

Az 1–6. kör kész:

- belépés Google-lel, meghívással (vagy telepítéskor választva nyitottan); admin tartalékbelépés jelszóval és TOTP-vel;
- felhasználónként külön könyvtár; a szétválasztást az adatbázis sorvédelme (row-level security) is őrzi;
- futtatók párosítása kóddal, munkakiosztás a futtató képességei szerint, lízing és szívverés, megszakítás, újrapróbálás;
- átírás YouTube-felirattal, felhőben (AssemblyAI) vagy saját GPU-n (Whisper); saját felvétel böngészős feltöltéssel vagy a futtató figyelt mappájából;
- magyar összefoglaló és fordítás (a futtató modelljével, a saját felvételek csak a helyi modellel); keresés szövegre és jelentésre;
- kész eredmények átvétele (ha valaki már átíratta ugyanazt), kvóták;
- elemoldal: összefoglaló-változatok (választható aktuális, Markdown-letöltés), több átirat (fő átirat választása, törlés, újraírás más beállítással), letöltés TXT-ben és SRT-ben eredetiben és magyarul, választható modell, a hosszú munkák haladása;
- könyvtár szűréssel és összefoglaló-előnézettel; a hiányzó összefoglalók, fordítások és az index pótlása egy gombbal;
- YouTube-feliratkozások (a YouTube-fiókból, Takeout-fájlból vagy kézzel), csatornánkénti szabály, postaláda; a feliratkozásos videók láncolata: felirat → helyi Whisper → felhő, ha engedélyezett (4a).;
- értesítés ntfy-jal vagy Home Assistanttel: kész munka, hiba, a feliratkozásokról napi összesítő vagy csatornánként azonnal (4b);
- MCP a `/mcp` címen, felhasználónkénti, visszavonható tokenekkel (4c): [docs/mcp.md](docs/mcp.md);
- címkék, jegyzet és beszélőnevek az elemeken; a könyvtár exportja (Markdown vagy JSON); fióktörlés a felhasználó által; kétlépcsős belépés (hitelesítő alkalmazás a Google után, kötelezővé is tehető); meghívó e-mailben (5. kör);
- kérdezés a felületen belül (6. kör): beszélgetés egy videóról az elem oldalán, és az egész könyvtárról a Keresés oldalon (a saját felvételek nélkül); a választ a futtató modellje írja hivatkozásokkal, a beszélgetés Markdownként letölthető;
- felület a StockMaster mintájára: bal oldali, becsukható menü, mobilon becsúszó menü, világos és sötét téma, akadálymentes kezelés (billentyűzet, képernyőolvasó, látható fókusz).

## Fejlesztés

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest -q
```

A tesztekhez PostgreSQL kell: a tesztek a telepített szerverprogramokból saját, ideiglenes fürtöt indítanak, vagy a `TEST_DATABASE_URL` (superuser) szerverét használják. A Google-t egy helyi utánzat helyettesíti.

## Licenc

GNU AGPL-3.0, lásd [LICENSE](LICENSE). Aki módosított változatát szolgáltatásként üzemelteti, a módosításokat is köteles közzétenni.
