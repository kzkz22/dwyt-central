# dwyt-central

A **dwyt** központja: webes felület, belépés (Google + meghívás), felhasználónként külön könyvtár, munkasor és a futtatók protokollja. A számítást nem maga végzi: a munkákat a felhasználók saját futtatói ([dwyt-runner](https://github.com/kzkz22/dwyt-runner)) kérik el és adják le. Kulcsot (AssemblyAI, LLM) nem tárol.

- Terv és döntések: [dwyt-docs/docs/terv.md](https://github.com/kzkz22/dwyt-docs/blob/main/docs/terv.md)
- Központ–futtató protokoll: [dwyt-docs/docs/protokoll.md](https://github.com/kzkz22/dwyt-docs/blob/main/docs/protokoll.md)
- [Telepítés](docs/telepites.md) · [Frissítés](docs/frissites.md)

## Állapot

Az 1. kör kész:

- belépés Google-lel, meghívással (vagy telepítéskor választva nyitottan); admin tartalékbelépés jelszóval és TOTP-vel;
- felhasználónként külön könyvtár; a szétválasztást az adatbázis sorvédelme (row-level security) is őrzi;
- futtatók párosítása kóddal, munkakiosztás a futtató képességei és engedélyezett tartományai szerint, lízing és szívverés, megszakítás, újrapróbálás;
- beküldés YouTube-felirat vagy felhős átírás módban, magyar összefoglalóval;
- könyvtár, elemoldal (átiratok, összefoglaló, időbélyeg-linkek), munkák, futtatók, admin (meghívók, felhasználók letiltása).

Következik a 2. kör: saját GPU (dwyt-airouter, dwyt-gpu), helyi Whisper.

## Fejlesztés

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest -q
```

A tesztekhez PostgreSQL kell: a tesztek a telepített szerverprogramokból saját, ideiglenes fürtöt indítanak, vagy a `TEST_DATABASE_URL` (superuser) szerverét használják. A Google-t egy helyi utánzat helyettesíti.

## Licenc

GNU AGPL-3.0, lásd [LICENSE](LICENSE). Aki módosított változatát szolgáltatásként üzemelteti, a módosításokat is köteles közzétenni.
