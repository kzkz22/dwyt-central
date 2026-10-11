# A központ telepítése

Ez a leírás a dwyt központ első telepítését írja le egy Proxmox LXC-be, Debian 12-re. Más Debian/Ubuntu gépen is ugyanígy megy; csak az 1. lépés Proxmox-specifikus. A frissítés: [frissites.md](frissites.md). A terv és a döntések: [dwyt-docs](https://github.com/kzkz22/dwyt-docs/blob/main/docs/terv.md).

Mi kerül az LXC-be:

| Elem | Hol | Mire |
| --- | --- | --- |
| PostgreSQL 17 (+ `pgvector` a 3. körhöz) | rendszercsomag | a felhasználók könyvtárai, sorvédelemmel |
| `dwyt-central` szolgáltatás | `/opt/dwyt-central`, 8000-es port | a webes felület és a futtatók protokollja; indításkor lefuttatja az adatbázis-migrációkat |
| `dwyt-central-cli` | `/usr/local/bin` | admin létrehozása, meghívó, ellenőrzés |

A központ nem számol: a munkát a felhasználók saját futtatói végzik ([dwyt-runner](https://github.com/kzkz22/dwyt-runner)), amelyek kifelé kapcsolódnak ide. Kulcsot (AssemblyAI, LLM) a központ nem tárol.

A nagybetűs részeket (`DWYT-IP`, `APACHE-IP`, `VPN-SUBNET`) cseréld a saját értékeidre. Minden lépés elején ott áll, hol kell futtatni.

## 1. Az LXC

```bash
# Proxmox gépen; a sablon neve a saját tárolódon eltérhet (pveam list local), a 150-es azonosító szabadon választható
pct create 150 local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst \
  --hostname dwyt --cores 2 --memory 4096 --swap 1024 \
  --rootfs local-lvm:20 --unprivileged 1 --features nesting=1 \
  --net0 name=eth0,bridge=vmbr0,ip=DWYT-IP/24,gw=ÁTJÁRÓ-IP \
  --onboot 1
pct start 150
```

A 4 GB memória a 3. körben érkező keresés miatt kell (a `bge-m3` modell kb. 2,2 GB); az 1. körnek 1 GB is elég.

## 2. Alapcsomagok és PostgreSQL 17

```bash
# LXC-ben (pct enter 150)
apt update && apt full-upgrade -y
apt install -y git rsync curl ca-certificates python3-venv python3-pip postgresql-common
/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
apt install -y postgresql-17 postgresql-17-pgvector
systemctl is-active postgresql          # active
```

## 3. Szolgáltatásfelhasználó, adatbázis és a két adatbázis-szerepkör

A központ két szerepkörrel dolgozik:

- `dwyt`: a táblák tulajdonosa. Ezzel futnak a migrációk, a belépés, a futtatók protokollja és az admin oldal.
- `dwyt_app`: a felhasználók oldalai ezzel olvasnak és írnak. Erre vonatkozik a sorvédelem: minden lekérdezés csak a bejelentkezett felhasználó sorait látja, akkor is, ha a kód elfelejt szűrni.

Mindkettő jelszó nélkül, Unix-socketen csatlakozik, a `dwyt` rendszerfelhasználó nevében (peer hitelesítés, egy névleképezéssel).

```bash
# LXC-ben
adduser --system --group --home /var/lib/dwyt dwyt
runuser -u postgres -- createuser dwyt
runuser -u postgres -- createuser dwyt_app
runuser -u postgres -- createdb -O dwyt -E UTF8 -T template0 --locale=C.UTF-8 dwyt

# a dwyt rendszerfelhasználó mindkét szerepkörként csatlakozhat
PGCONF=/etc/postgresql/17/main
cat >> $PGCONF/pg_ident.conf <<'EOF'
dwytmap  dwyt  dwyt
dwytmap  dwyt  dwyt_app
EOF
sed -i '0,/^local/s//local   dwyt            dwyt,dwyt_app                           peer map=dwytmap\nlocal/' $PGCONF/pg_hba.conf
systemctl reload postgresql
```

A `sed` sor a meglévő `local` szabályok elé teszi az újat (a PostgreSQL az első illeszkedő sort használja). Ellenőrzés:

```bash
grep -n "dwytmap" /etc/postgresql/17/main/pg_hba.conf      # a többi "local" sor előtt
runuser -u dwyt -- psql -U dwyt_app -d dwyt -c "select current_user"   # dwyt_app
runuser -u postgres -- psql -l | grep dwyt                 # a kódolás UTF8
```

Fontos: a `dwyt_app` ne legyen superuser és ne kapjon `BYPASSRLS` jogot (a `dwyt-central-cli check` ezt is ellenőrzi).

## 4. A program

```bash
# LXC-ben
mkdir -p /root/src && git clone https://github.com/kzkz22/dwyt-central /root/src/dwyt-central
mkdir -p /opt/dwyt-central
rsync -a --delete --exclude .git --exclude tests /root/src/dwyt-central/ /opt/dwyt-central/app/
python3 -m venv /opt/dwyt-central/venv
/opt/dwyt-central/venv/bin/pip install /opt/dwyt-central/app
```

Ellenőrzés: `/opt/dwyt-central/venv/bin/dwyt-central --help` kiírja a parancsokat.

## 5. Beállítások

```bash
# LXC-ben
mkdir -p /etc/dwyt-central
install -m 600 /opt/dwyt-central/app/deploy/dwyt-central/central.env.example /etc/dwyt-central/central.env
nano /etc/dwyt-central/central.env
```

Az első indításhoz elég a `BASE_URL` (`https://dwyt.iodate.date`) és a `FORWARDED_ALLOW_IPS` (az Apache-gép címe). A Google-adatok a 7. lépésben kerülnek bele.

## 6. Szolgáltatás és admin

```bash
# LXC-ben
cp /opt/dwyt-central/app/deploy/dwyt-central/dwyt-central.service /etc/systemd/system/
install -m 755 /opt/dwyt-central/app/deploy/dwyt-central/dwyt-central-cli /usr/local/bin/dwyt-central-cli
systemctl daemon-reload
systemctl enable --now dwyt-central
systemctl is-active dwyt-central          # active
dwyt-central-cli create-admin SAJÁT-EMAIL@gmail.com
dwyt-central-cli check
```

A `create-admin` először visszaírja az e-mail-címet, és megerősítést kér (`i` = igen; egy véletlen leütés vagy beillesztési szemét így nem kerül a címbe). Utána jelszót kér (legalább 12 karakter), és kiír egy QR-kódot a hitelesítő alkalmazáshoz. Ez a tartalékbelépés (`/admin-belepes`), ha a Google-belépés nem működne. Ugyanez az e-mail-cím a Google-lel is beléphet, és első alkalommal hozzáköti a Google-fiókját.

A `check` elvárt kimenete: „sorvédelem: bekapcsolva”, „alkalmazás-szerepkör (dwyt_app): rendben”.

Próba a belső hálózatról, az Apache nélkül: `http://DWYT-IP:8000/admin-belepes` (jelszó + kód). Ehhez a `.env`-ben átmenetileg `COOKIE_SECURE=false` kell, mert HTTP-n a böngésző nem küldi vissza a `Secure` sütit.

## 7. Google-belépés

Saját Google Cloud-projekt kell; a régi `yt-webui` projektet nem érdemes használni, mert a belépési képernyőn annak a neve jelenne meg.

1. [console.cloud.google.com](https://console.cloud.google.com): új projekt, `dwyt`.
2. **Google Auth Platform → Branding:**
   - App name: `dwyt`; User support email és Developer contact: a saját címed.
   - Logót ne tölts fel (logóval a Google ellenőrzést kér).
   - Application home page: `https://github.com/kzkz22/dwyt-docs`
   - Application privacy policy link: `https://github.com/kzkz22/dwyt-docs/blob/main/docs/adatvedelem.md`
   - Authorized domains: `iodate.date` és `github.com`.
3. **Audience:** External, majd **Publish app**. A belépéshez csak alap jogosultság kell (név, e-mail), ezért nincs „unverified app” figyelmeztetés és felhasználói korlát.
4. **Clients → Create client:** típus **Web application**, név `dwyt`.
   - Authorized redirect URIs: `https://dwyt.iodate.date/auth/google/callback`
   - Helyi fejlesztéshez még: `http://localhost:8000/auth/google/callback`
5. A kapott Client ID és Client secret a `.env`-be: `GOOGLE_CLIENT_ID=…`, `GOOGLE_CLIENT_SECRET=…`, majd `systemctl restart dwyt-central`.

## 8. Apache, tanúsítvány, DNS

1. DNS: `dwyt.iodate.date` A-rekordja a nyilvános IP-címedre mutasson (a pfSense a 80-as és a 443-as portot már az Apache-gépre továbbítja).
2. Az Apache-gépen:

```bash
# Apache-gépen
a2enmod ssl proxy proxy_http headers
cp dwyt.conf /etc/apache2/sites-available/dwyt.conf     # a repó deploy/apache/dwyt.conf fájlja, kitöltve
certbot --apache -d dwyt.iodate.date                   # vagy: certbot certonly --webroot ...
a2ensite dwyt && apache2ctl configtest && systemctl reload apache2
```

A minta nyilvános elérést ír le. Ha átmenetileg csak a belső hálózatot és a VPN-t akarod engedni, vedd ki a megjegyzésből a végén lévő `Require ip` blokkot, de valódi címtartományokkal. Ha helyőrző marad benne (például `VPN-SUBNET`), az Apache az egész beállítást elutasítja.

### Védelem a nyilvános eléréshez

**Tűzfal a központ előtt.** A 8000-es portot csak az Apache-gép érje el, hogy senki ne kerülhesse meg az Apache-ot. A Proxmox felületén:

1. **CT 150 → Firewall → Options:** Firewall: Yes, Input Policy: DROP.
2. **CT 150 → Firewall → Add**, két szabály:
   - IN, ACCEPT, Protocol tcp, Dest. port 8000, Source `APACHE-IP`;
   - IN, ACCEPT, Protocol tcp, Dest. port 22, Source `192.168.1.0/24`.
3. A konténer hálókártyáján (**Network → net0 → Edit**) legyen bepipálva a **Firewall**.
4. A **Datacenter → Firewall → Options** alatt is legyen bekapcsolva.

Ellenőrzés egy másik belső gépről: `curl -m 5 http://DWYT-IP:8000/healthz` → időtúllépés. Az Apache-gépről ugyanez → `{"ok":true}`.

**A valódi kliens-cím.** A `central.env`-ben a `FORWARDED_ALLOW_IPS` az Apache-gép címe legyen, `*` soha. A kéréskorlátok (admin-belépés, Google-belépés indítása, meghívólink, futtató-párosítás) erre a címre vonatkoznak. A `dwyt-central-cli check` szól, ha nincs beállítva.

**fail2ban az Apache-gépen (ajánlott).** A hibás admin-belépés `401`-et, a kéréskorlát `429`-et ad. A fail2ban ezekből tiltja ki a próbálkozó címet a tűzfalon.

Az Apache-gépen kell futnia (nem a központ LXC-jében), mert az Apache naplóját olvassa, és ott tilt.

```bash
# Apache-gépen
apt install -y fail2ban python3-systemd
R=https://raw.githubusercontent.com/kzkz22/dwyt-central/main/deploy/fail2ban
curl -sSfL -o /etc/fail2ban/filter.d/dwyt.conf $R/filter-dwyt.conf
curl -sSfL -o /etc/fail2ban/jail.d/dwyt.conf  $R/jail-dwyt.conf      # az ignoreip-ben a saját hálózatod
ls -l /var/log/apache2/dwyt-access.log        # léteznie kell (CustomLog a vhostban, majd reload)
systemctl restart fail2ban
fail2ban-client status dwyt
```

Debian 12-n a fail2ban magától bekapcsolt `sshd` szabálya a `/var/log/auth.log` fájlt keresi, ami ott nincs, és emiatt az egész fail2ban el sem indul. Megoldás: `printf '[sshd]\nbackend = systemd\n' > /etc/fail2ban/jail.d/sshd-systemd.conf`. Ha nem indul, az ok a `journalctl -u fail2ban -n 30` kimenetében látszik.

A `dwyt.conf` vhost ehhez saját naplót ír: `/var/log/apache2/dwyt-access.log`. Ha a vhostot korábban a régi minta alapján írtad, add hozzá az `ErrorLog` és a `CustomLog` sort, és töltsd újra az Apache-ot.

Ellenőrzés: `curl -sI https://dwyt.iodate.date/healthz` → `200`, és a válaszban ott a `Strict-Transport-Security` fejléc.

## 9. Első belépés, meghívó, futtató

1. `https://dwyt.iodate.date` → **Belépés Google-lel** a 6. lépésben megadott címmel. Az Admin menüben látszol adminként.
2. **Admin → Meghívó:** e-mail-cím → a kapott linket küldd el a meghívottnak (7 napig érvényes, egyszer használható).
3. **Futtatók:** a saját gépeden telepítsd a futtatót ([dwyt-runner](https://github.com/kzkz22/dwyt-runner)), `dwyt-runner pair https://dwyt.iodate.date`, a kiírt kódot írd be itt.
4. **Új átirat:** egy YouTube-link „YouTube-felirat” móddal; a futtató pár másodpercen belül elkezdi.

## Hibakeresés

| Jelenség | Teendő |
| --- | --- |
| `dwyt-central` nem indul, a naplóban `alembic` hiba | `journalctl -u dwyt-central -n 50`. „The database encoding is …”: az adatbázis nem UTF8 (3. lépés, `createdb -E UTF8 -T template0`). |
| `role "dwyt_app" does not exist` vagy `Peer authentication failed` | A 3. lépés `createuser` parancsa vagy a `pg_ident.conf`/`pg_hba.conf` sor hiányzik; a `pg_hba.conf` sorának a többi `local` sor előtt kell lennie. |
| A `check` szerint az alkalmazás-szerepkör megkerülheti a sorvédelmet | `runuser -u postgres -- psql -c "ALTER ROLE dwyt_app NOSUPERUSER NOBYPASSRLS"`. |
| Google-belépéskor `redirect_uri_mismatch` | A 7. lépés redirect URI-ja betűre egyezzen a `BASE_URL` + `/auth/google/callback` címmel (https, perjel nélkül a végén). |
| „Ide csak meghívással lehet regisztrálni” | A meghívó linkjét kell először megnyitni, és onnan belépni; vagy a `REGISTRATION` értéke `open`. |
| Belépés után visszadob a belépő oldalra | HTTP-n `COOKIE_SECURE=true` mellett a böngésző nem küldi a sütit; az Apache-on át (HTTPS) próbáld, vagy belső próbához átmenetileg `false`. |
| A futtatók oldalán „nem jelentkezik” | A futtató gépén `dwyt-runner check`; a futtatónak el kell érnie a `BASE_URL`-t. Egy `Require ip` blokk a kívülről jövő futtatót is kizárja. |
| „Túl sok próbálkozás” mindenkinek egyszerre | A `FORWARDED_ALLOW_IPS` hiányzik vagy rossz: minden kérés az Apache címéről jön, és a korlát közös. |
| Az Apache `configtest` hibát ad a `Require ip` sorra | Helyőrző maradt benne (például `VPN-SUBNET`); valódi tartományt írj be, vagy töröld a blokkot. |
