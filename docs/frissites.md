# A központ frissítése

Frissítés előtt érdemes kézi mentést készíteni:

```bash
# Proxmox gépen
vzdump 150 --storage pbs --mode snapshot
```

## Új verzió a repóból

```bash
# LXC-ben
git -C /root/src/dwyt-central pull
rsync -a --delete --exclude .git --exclude tests /root/src/dwyt-central/ /opt/dwyt-central/app/
/opt/dwyt-central/venv/bin/pip install /opt/dwyt-central/app
cp /opt/dwyt-central/app/deploy/dwyt-central/dwyt-central.service /etc/systemd/system/
install -m 755 /opt/dwyt-central/app/deploy/dwyt-central/dwyt-central-cli /usr/local/bin/dwyt-central-cli
systemctl daemon-reload
systemctl restart dwyt-central
dwyt-central-cli check
```

A szolgáltatás indításkor lefuttatja az új adatbázis-migrációkat. A futó munkák nem vesznek el: a futtatók szívverése megújítja a lízinget, a leadott eredményt a központ az újraindulás után is elfogadja.

Ha a `central.env.example` új beállítást kapott, azt kézzel kell átvenni: `diff /opt/dwyt-central/app/deploy/dwyt-central/central.env.example /etc/dwyt-central/central.env`.

## Régebbi telepítés frissítése körönként

Az alábbi részek csak annak kellenek, aki egy korábbi kör után telepítette a központot. Mindegyik a fenti frissítés mellé ír egyszeri lépéseket; a kihagyott köröket sorban kell pótolni. Új telepítésnél ezek nem kellenek: a [telepites.md](telepites.md) mindent tartalmaz.

## Frissítés a 3. körre (fordítás, keresés, újrahasznosítás)

Egyszeri lépések, a fenti frissítés **előtt**:

1. Memória: a Proxmoxban a CT 150 kapjon 6,5 GB-ot (`pct set 150 --memory 6500`).
2. A bővítmények (superuser kell hozzájuk, a migráció csak ellenőrzi őket):

   ```bash
   runuser -u postgres -- psql -d dwyt -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS unaccent; CREATE EXTENSION IF NOT EXISTS pg_trgm;"
   ```
3. Utána jöhet a fenti frissítés, majd a beágyazó szolgáltatás telepítése a [telepites.md](telepites.md) „A beágyazó szolgáltatás” része szerint, és a `central.env`-be az `EMBED_URL=http://127.0.0.1:8001` sor.
4. A már meglévő elemeket a migráció magától indexelésre teszi; a `dwyt-central-cli check` mutatja, hány vár még.
5. A futtatókat is frissíteni kell (a fordítás új munkafajta): `uv tool install --force git+https://github.com/kzkz22/dwyt-runner`, majd a futtató újraindítása. A régebbi futtató nem kap fordítást; a Munkák oldal jelzi, ha egy munkát egyik futtató sem tud elvégezni.

## Frissítés a 3b részre (saját felvételek, kvóták)

1. A fenti frissítés; a szolgáltatásfájlt is másold át (a feltöltések miatt írhatja a `/var/lib/dwyt` mappát), majd `systemctl daemon-reload`.
2. Az Apache-gépen a `dwyt.conf` vhostba kerüljön be a feltöltések korlátja (a repó `deploy/apache/dwyt.conf` mintája szerint), majd `apache2ctl configtest && systemctl reload apache2`:

   ```
   <Location /feltoltes>
       LimitRequestBody 2147483647
   </Location>
   ```
3. A futtatókat frissítsd a 0.4.0-s változatra (`uv tool install --force git+https://github.com/kzkz22/dwyt-runner`), és indítsd újra őket. A régebbi futtató nem kap feltöltött fájlt.
4. Ha kell, a figyelt mappát a futtatón: `dwyt-runner config set watch_dirs "MAPPA"`, majd újraindítás.

## Frissítés a 4a részre (YouTube-feliratkozások, postaláda)

1. A fenti frissítés (a `pip install` hozza az új `cryptography` csomagot; az adatbázis-migráció magától lefut).
2. Ha a felhasználók a YouTube-fiókjukból is behúzhatják a feliratkozásaikat: a [telepites.md](telepites.md) „YouTube-feliratkozások” része (YouTube Data API bekapcsolása, a `youtube.readonly` jogosultság, `TOKEN_KEY`), majd `systemctl restart dwyt-central`.
3. A futtatókat nem kell frissíteni. A feliratkozásos videók láncolata (felirat → helyi Whisper → felhő, ha a csatornán engedélyezett) a meglévő futtatókkal működik.

## Frissítés a 4b részre (értesítések)

1. A fenti frissítés (az adatbázis-migráció magától lefut). A tokenek titkosításához itt is a `TOKEN_KEY` kell (a 4a résznél került be).
2. Az értesítést mindenki maga állítja be a Beállítások oldalon: **ntfy** (telefonos alkalmazás, alapból az `ntfy.sh` szerverrel) vagy **Home Assistant** (cím, `notify` szolgáltatás, hosszú élettartamú token).
3. A felhasználók által megadott címek nem mutathatnak belső hálózatra (így a központ nem használható a belső gépek elérésére). Az admin a saját belső Home Assistantjét enélkül is megadhatja; ha más felhasználónak is engedni kell egy belső hálózatot, a `central.env`-be: `NOTIFY_PRIVATE_NETS=192.168.1.0/24`, majd `systemctl restart dwyt-central`.

## Frissítés a 4c részre (MCP)

1. A fenti frissítés (a `pip install` hozza az `mcp` csomagot; az adatbázis-migráció magától lefut).
2. Az Apache-gépen a fail2ban szűrője kapjon egy sort a hibás MCP-tokenekre (a repó `deploy/fail2ban/filter-dwyt.conf` fájlja szerint), majd `systemctl reload fail2ban`:

   ```bash
   # Apache-gépen
   curl -sSfL -o /etc/fail2ban/filter.d/dwyt.conf https://raw.githubusercontent.com/kzkz22/dwyt-central/main/deploy/fail2ban/filter-dwyt.conf
   systemctl reload fail2ban
   ```
3. A tokeneket mindenki maga hozza létre a Beállítások oldalon; a kliensek beállítása: [mcp.md](mcp.md).

## Frissítés az 5. körre (címkék, jegyzet, beszélőnevek, export, fióktörlés, kétlépcsős belépés, meghívó e-mailben)

1. A fenti frissítés (az adatbázis-migráció magától lefut).
2. Ha a meghívók e-mailben menjenek: a `central.env`-be az `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` sorok (minta: `deploy/dwyt-central/central.env.example`), majd `systemctl restart dwyt-central`. Nélkülük minden marad a régiben: az admin kimásolja a linket.
3. Ha mindenkinek kötelező legyen a Google után a hitelesítő alkalmazás kódja: `REQUIRE_TOTP=true`. Aki még nem állította be, a következő belépéskor beállíttatja vele. Alapból mindenki maga kapcsolhatja be a Beállítások oldalon.

## Frissítés az új felületre (0.10)

1. A fenti frissítés; adatbázis-migráció és új beállítás nincs.
2. A böngészőben semmit nem kell törölni: a stílus és a szkriptek címe a tartalmukkal együtt változik, így a régi példány nem ragad be.
3. A téma (világos / sötét) és a becsukott menü böngészőnként tárolódik; először a rendszer témáját követi.
4. Ha az Apache-on saját `Content-Security-Policy` fejlécet állítottál be, abban is maradjon `default-src 'self'` (a betűtípus és az ikonok a központról jönnek, külső címről semmi).

## Frissítés a 0.17.2-re (gombok a Könyvtár soraiban, Csatornák elrendezése)

1. A fenti frissítés; migráció nincs.
2. A Könyvtár minden során három kis ikon: Megnézendő (YouTube-videónál), olvasott / olvasatlan, törlés (megerősítéssel); utána ugyanoda tér vissza a lista.
3. A Csatornák oldalon a beállítások 1400 képpontnál keskenyebb ablakban (vagy nagyított böngészőben) a csatorna neve alá kerülnek, így a név nem nyomódik össze.

## Frissítés a 0.17.1-re (a postaláda régebbi videóinak leírása)

1. A fenti frissítés; a 0018-as migráció magától lefut.
2. A videó leírását a központ a hírcsatornából veszi. Ami a 0.17 előtt (vagy a frissítés közben) került a postaládába, annak még nincs leírása; a migráció ezért minden olyan csatornát azonnali újraellenőrzésre jelöl, és a háttér pár percen belül pótolja. Kézzel ugyanezt a Csatornák oldal „Ellenőrzés most” gombja teszi.

## Frissítés a 0.17-re (8. kör: Megnézendő, megosztás telefonról, olvasott jelölés, gyors döntés a Postaládában)

1. A fenti frissítés; az adatbázis-migráció (0017) magától lefut. A meglévő elemek olvasottként indulnak, így a Könyvtárban csak az ezután elkészülők kapnak „olvasatlan” pöttyöt.
2. **Megnézendő-lista** (a YouTube-fiókban egy „dwyt – megnézendő” lejátszási lista): a Google Cloud Console-ban **Google Auth Platform → Data Access → Add or remove scopes:** add hozzá a `https://www.googleapis.com/auth/youtube` jogot (a `youtube.readonly` mellé) → Update → Save. Enélkül a Google az engedélykérésnél hibát ad. Ez is „érzékeny” jogosultság, ugyanazzal az „unverified app” figyelmeztetéssel. A `TOKEN_KEY` és a YouTube Data API ugyanaz, mint a feliratkozásoknál. A felhasználók az első Megnézendő gombnál egyszer engedélyezik; aki eddig nem kapcsolta össze a fiókját, annak a feliratkozásait ez nem veszi át.
3. **Megosztás telefonról:** HTTPS kell hozzá (a meglévő Apache-beállítás jó). A telefon böngészőjében a dwyt menüjéből: „Telepítés” vagy „Hozzáadás a kezdőképernyőhöz”; ezután a YouTube-alkalmazás Megosztás menüjében megjelenik a dwyt. Androidon a Chrome így működik; iPhone-on a Safari webalkalmazása nem lehet megosztási cél, ott a link a dwyt Új átirat oldalára másolható. Hogy a megosztás egy oldalt nyisson vagy rögtön induljon, a Beállításokban választható. Ha az Apache-on saját `Content-Security-Policy` fejléc van, a `manifest.webmanifest` és a `sw.js` a központról jön (`default-src 'self'` elég).
4. A futtatókat nem kell frissíteni.

## Frissítés a 0.16.4-re (futó munka közben is elérhető futtató, újraindult futtató munkái)

1. A fenti frissítés; migráció nincs.
2. Hosszú munka közben (például egy összefoglalónál) a Munkák oldal eddig 90 másodperc után azt írta, hogy „Egyik futtatód sem jelentkezett”, mert a szívverés nem frissítette a futtató utolsó jelentkezését. Most igen.
3. Ha a futtató munka közben újraindul (dwyt-runner 0.11.1-től), a munkáját a központ azonnal visszateszi a sorba, a megszakítottat lezárja; eddig a lízing lejártáig (15 perc) „folyamatban” maradt, és nem lehetett megszakítani.

## Frissítés a 0.16.3-ra (letöltés a Futtatók oldalon)

Csak a felület: az „Új futtató” → *Asztali gépre* fülön letöltőgombok a Windows-telepítőhöz és a Linux AppImage-hez (a dwyt-runner legújabb GitHub-kiadásából), és linkek az útmutatókhoz. A fenti frissítés elég.

## Frissítés a 0.16.2-re (beragadt „Folyamatban” videók a postaládában)

Ha egy postaládás videó elemét törölted, amíg a munkája futott vagy várt, a munka is törlődött, a videó viszont a Postaláda *Folyamatban* fülén maradt (a Munkák között nem látszott, újraindítani sem lehetett). A 0.16.2 az ilyen bejegyzést a postaláda következő megnyitásakor (és a háttérben is) rendbe teszi: ha az elemnek van átirata, *Kész*, különben *Kihagyott* „A munkája megszűnt” megjegyzéssel és **Újra** gombbal. A fenti frissítés elég.

## Frissítés a 0.16.1-re (a működési mód dobozai)

Csak a felület: a futtató Beállítás oldalán a működési módok dobozaiban a leírás tördelődik, nem lóg ki és nem csúszik egymásra. A fenti frissítés elég; a böngészőben semmit nem kell törölni.

## Frissítés a 0.16-ra (egysoros telepítő, távoli beállítás)

1. A fenti frissítés; a migráció (0016) magától lefut. Új beállítás nincs.
2. **Új futtató** a Futtatók oldalon, három füllel: *Szerverre (Linux)* egy paranccsal telepít és párosít (a parancs telepítőkódja 5 percig él, egyszer használható, jóváhagyni nem kell; a szkript a `/futtato.sh` címen olvasható), *Asztali gépre*, *Párosítás kóddal* (ahogy eddig). Ha az Apache csak bizonyos útvonalakat enged ki, a `/futtato.sh` is legyen elérhető onnan, ahonnan telepítesz.
3. **Beállítás** gomb a futtató kártyáján: a 0.10-es vagy újabb futtató beállításai innen állíthatók, ha a futtató engedi (szerveren alapból igen, az asztali alkalmazásban a Működés lapon kapcsolható be). A kulcsokat a böngésző a futtató nyilvános kulcsával titkosítja; ehhez https kell (a WebCrypto csak biztonságos kapcsolaton működik), http-n csak a többi beállítás menthető. Minden változás a futtató Napló részébe kerül, és értesítés megy róla, ha van értesítési csatorna.
4. **Frissítés most**: ha a szerveres futtató újabb változatot lát a GitHubon, a kártyán megjelenik a gomb; a futtató két munka között frissít, és újraindul (aláírás-ellenőrzés még nincs, terv 18.16).

## Frissítés a 0.15.1-re (visszavont futtatók elrejtve)

Csak a felület: a Futtatók oldal nem listázza a visszavont futtatókat. Az adatbázisban megmaradnak, mert a régi munkák hivatkoznak rájuk. A fenti frissítés elég.

## Frissítés a 0.15-re (futtató kikapcsolása)

1. A fenti frissítés; a migráció (0015) magától lefut.
2. A Futtatók oldalon minden futtató mellett **Kikapcsolás** / **Bekapcsolás** gomb van: a kikapcsolt futtató nem kap tőled új munkát, de párosítva marad (a megosztott is). A futtatón nincs teendő.

## Frissítés a 0.14.3-ra (a Futtatók oldal magától frissül párosítás után)

Csak a felület: a Jóváhagyás után a Futtatók oldal pár másodpercenként megnézi, megkapta-e már a futtató a tokenjét, és ha igen, magától újratölt; így az új futtató kézi frissítés nélkül megjelenik. A fenti frissítés elég.

## Frissítés a 0.14.2-re (párosítás a futtató ablakából)

Csak a felület: a Futtatók oldal `?kod=…&nev=…` címmel kitöltött párosítókóddal nyílik meg. Ezt a futtató telepítő ablaka nyitja meg (dwyt-runner 0.9). Jóváhagyni továbbra is a gombbal kell; az oldal kéri, hogy a kódot vesd össze a futtató ablakában látható kóddal. A fenti frissítés elég.

## Frissítés a 0.14.1-re (új munkalépés felirata)

Csak a felület: a Munkák oldal „Modell letöltése (első használat)” felirattal mutatja, amikor a futtató a beépített helyi Whisperhez először tölti le a modelleket (dwyt-runner 0.8). A fenti frissítés elég.

## Frissítés a 0.14-re (felirat használata, hallgató futtató)

1. A fenti frissítés; a migráció (0014) magától lefut.
2. **Felirat használata** (Beállítások): alapból csak a videó készítőjének saját felirata kerül be; ha csak gépi (automatikus) felirat van, a feliratkozott csatornák videóinál a lánc a helyi Whisperrel (majd, ha engedélyezted, a felhővel) folytatódik, egy kézzel beküldött videónál pedig a munka ezt jelzi, és az elem oldalán kérhetsz átírást. Aki eddig a gépi feliratot is elfogadta, a Beállításokban válassza a „Gépi felirat is jó” lehetőséget, vagy adja meg a nyelveket, ahol a gépi felirat elég (például `en`). Csatornánként a Csatornák oldalon, videónként az Új átirat oldalon eltérhetsz.
3. Ha a futtatód egy óránál régebben nem jelentkezett, és munka vár rá, a Könyvtár és a Munkák oldal szól.
4. A futtatón nincs teendő.

## Frissítés a 0.13-ra (lapozás, Postaláda-számláló, ikon)

1. A fenti frissítés; a migráció (0013) magától lefut (a felhasználó lapmérete).
2. Újdonságok: lapozás 15 / 25 / 50 elemenként a Könyvtár, Keresés, Beszélgetések, Postaláda és Munkák oldalon (a választást fiókonként megjegyzi, minden eszközön); a menüben a Postaláda mellett az új videók száma; böngészőfül-ikon; mobilon a logó a menügomb mellett balra.
3. Ha az Apache-ban a `/favicon.ico` máshová mutatna, azt vedd ki: a központ maga szolgálja ki.

## Frissítés a 0.12.3-ra (hosszabb összefoglaló-előnézet)

Csak a felület: a Könyvtár sorában az összefoglaló előnézete álló telefonon 6 sor (eddig 2), szélesebb képernyőn továbbra is 2. Az előnézet a címsorokat kihagyja, a felsorolás pontjait viszont már tartalmazza. A fenti frissítés elég.

## Frissítés a 0.12.2-re (mobil álló nézet)

Csak a felület: a Könyvtár és a Postaláda sorai álló telefonon a kép mellett csak a címet mutatják, a többi (csatorna, idő, címkék, összefoglaló) alattuk teljes szélességben. A fenti frissítés elég; a böngészőben egy újratöltés kell, ha a régi stíluslap maradt meg.

## Frissítés a 0.12.1-re (könyvtár-kérdések a teljes könyvtárral)

A könyvtárról szóló kérdés eddig csak a keresés legjobb néhány videóját látta, ezért az áttekintő kérdések („csoportosítsd az összes videót”) hiányos választ adtak. Mostantól a teljes könyvtár rövid listáját is megkapja (cím, csatorna, dátum, címkék, az összefoglaló eleje).

1. A fenti frissítés; migráció nincs.
2. A futtatót is frissíteni kell (0.7.1 vagy újabb), mert az utasítást ő írja: `uv tool install --force git+https://github.com/kzkz22/dwyt-runner`, majd újraindítás. A régebbi futtató a listát nem adja tovább a modellnek, és ha a kérdéshez nincs keresési találat, hibát ad.
3. A korábbi beszélgetésekben a régi válaszok maradnak; új kérdés (akár ugyanabban a beszélgetésben) már a teljes listával megy.

## Frissítés a 0.12-re (futtató megosztása)

1. A fenti frissítés; a migráció (0012) magától lefut (megosztások és havi felhasználás táblái).
2. A futtatón és az AI-routeren nincs teendő.
3. Megosztás: Futtatók oldal → a futtató kártyáján „Megosztás valakivel” (a másik felhasználónak már dwyt-fiókkal kell rendelkeznie). Ő a Futtatók oldalon fogadja el; amíg nem fogadta el, a futtató nem dolgozik neki. A Könyvtár oldal szól neki a felajánlásról.

## Frissítés a 0.11.1-re (mi fut a modellnevek mögött)

A Futtatók oldal futtatónként mutatja, mit tud, és táblázatban, mi fut az egyes nyelvi modellnevek mögött; a modellválasztók opciói és az alattuk lévő sor ugyanezt mondják röviden. Migráció nincs.

1. AI-router: dwyt-airouter 0.3, `dwyt-airouter generate`, `systemctl restart dwyt-litellm` (dwyt-airouter, frissites.md).
2. Futtató: `uv tool install --force git+https://github.com/kzkz22/dwyt-runner` (0.7 vagy újabb), majd újraindítás. A régebbi futtató továbbra is dolgozik, csak a leírások hiányoznak; az oldal ezt jelzi.
3. A központ: a fenti frissítés.

## Frissítés a 0.11-re (kérdezés a könyvtárról)

1. A fenti frissítés; a migráció (0011) magától lefut (beszélgetések táblái, új munkafajta).
2. A futtatót is frissíteni kell, mert a választ ő írja: `uv tool upgrade dwyt-runner` (0.6 vagy újabb), majd `dwyt-runner config set llm_ask_model kerdes` (vagy más modell; ha nincs megadva, az `llm_public_model` válaszol), és a futtató újraindítása.
3. Az AI-routeren a `kerdes` név felvétele a `models.yaml`-ba (dwyt-airouter, frissites.md), `dwyt-airouter generate`, `systemctl restart dwyt-litellm`.
4. Amíg egyik futtató sem jelenti, hogy tud kérdésre válaszolni, a kérdés-panel ezt kiírja, és a kérdések várnak.

## Frissítés a 0.10.1-re (YouTube-hírcsatorna kiesése)

A YouTube hírcsatornái (`/feeds/videos.xml`) 2025 vége óta naponta órákra minden csatornára 404-et vagy 500-at adnak, főleg éjjel. A korábbi verzió ezt „a csatorna nem található” hibaként írta ki minden csatornánál.

1. A fenti frissítés; a migráció (0010) magától lefut, és a régi, félrevezető hibaüzeneteket törli.
2. Mostantól egy kiesés a Csatornák oldal tetején egy sárga jelzésként látszik. A központ 15 percenként újrapróbálja; amíg a YouTube nem válaszol, nem kérdez sorban minden csatornát, hanem 5 percenként csak egyet. A közben megjelent videók nem vesznek el, mert a hírcsatorna a legutóbbi kb. 15 videót adja.
3. Pirosan csak az a csatorna jelenik meg, amelyik egy napnál régebben nem válaszol (ilyenkor lehet, hogy tényleg törölték vagy rejtett).

## Protokollváltás

Ha egy frissítés új protokollverziót hoz, a központ az előzőt is elfogadja egy ideig; a túl régi futtató a Futtatók oldalon „régi protokoll” jelzést kap, és a futtató maga is kiírja: `uv tool upgrade dwyt-runner`.

## Visszaállás

```bash
# LXC-ben
cd /root/src/dwyt-central && git log --oneline -5
```

Ha a két verzió között új migráció került be (`migrations/versions/` új fájl), előbb az adatbázist kell visszaléptetni, még az új kóddal:

```bash
cd /opt/dwyt-central/app && set -a && . /etc/dwyt-central/central.env && set +a
runuser -u dwyt --preserve-environment -- /opt/dwyt-central/venv/bin/alembic downgrade ELŐZŐ-REVÍZIÓ
```

Utána a régi kód: `git -C /root/src/dwyt-central checkout <azonosító>`, majd a fenti frissítés `rsync` sorától. Ha a migráció visszaléptetése nem megy, a frissítés előtti mentés visszaállítása a biztos út.
