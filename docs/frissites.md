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
