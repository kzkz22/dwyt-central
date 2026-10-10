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
