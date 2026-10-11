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
