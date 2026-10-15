#!/bin/sh
# dwyt runner installer (round 7d, decision 18.17). Usage, with the command from the dwyt Runners page:
#
#   curl -fsSL @CENTRAL_URL@/futtato.sh | sh -s -- KÓD
#
# What it does, in this order:
#   1. trades the install code (5 minutes, single use) for this runner's token at the dwyt service,
#      before any download, so a slow download cannot outlive the code;
#   2. as root: creates the system user "dwyt-runner" (home /var/lib/dwyt-runner) and installs everything
#      for that user; otherwise installs for you, without admin rights;
#   3. installs uv (https://astral.sh/uv) if it is missing, then the runner with uv from GitHub;
#   4. stores the token in the runner's settings (passed on stdin, never on a command line);
#   5. writes a systemd service (a system service as root, else your user service) and starts it.
# Settings are then made on the dwyt Runners page (Beállítás). Nothing here is secret: the code is the
# only credential, and it is given on the command line by you.
set -eu

CENTRAL="@CENTRAL_URL@"
SOURCE="https://github.com/kzkz22/dwyt-runner/archive/refs/heads/main.zip"

say() { printf '%s\n' "$*"; }
die() { printf 'Hiba: %s\n' "$*" >&2; exit 1; }

CODE=$(printf '%s' "${1:-}" | tr -cd 'A-Za-z0-9-')
[ -n "$CODE" ] || die "add meg a telepítőkódot: curl -fsSL $CENTRAL/futtato.sh | sh -s -- KÓD"
[ "$(uname -s)" = "Linux" ] || die "ez a telepítő Linuxra készült; más rendszeren az asztali alkalmazást használd."
command -v curl >/dev/null 2>&1 || die "a curl program kell hozzá."
NAME=$(printf '%s' "${DWYT_RUNNER_NAME:-$(hostname 2>/dev/null || echo szerver)}" | tr -cd 'A-Za-z0-9._ -' | cut -c1-80)
[ -n "$NAME" ] || NAME="szerver"

# ---- 1. the code first ------------------------------------------------------------------------------
umask 077
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
say "1/5 Párosítás a dwyt-vel ($CENTRAL) …"
STATUS=$(curl -sS -o "$TMP/pair" -w '%{http_code}' -X POST \
  -H 'Dwyt-Protocol: 1' -H 'Content-Type: application/json' \
  --data "{\"code\": \"$CODE\", \"name\": \"$NAME\"}" \
  "$CENTRAL/runner/v1/pair/redeem?format=env") || die "a dwyt nem érhető el innen: $CENTRAL"
if [ "$STATUS" != "200" ]; then
  MSG=$(sed -n 's/^ERROR=//p' "$TMP/pair")
  die "${MSG:-a dwyt HTTP $STATUS választ adott.}"
fi
RUNNER_ID=$(sed -n 's/^DWYT_RUNNER_ID=//p' "$TMP/pair" | tr -cd '0-9')
TOKEN=$(sed -n 's/^DWYT_TOKEN=//p' "$TMP/pair" | tr -cd 'A-Za-z0-9_-')
rm -f "$TMP/pair"
[ -n "$RUNNER_ID" ] && [ -n "$TOKEN" ] || die "a dwyt válasza hiányos."
DONE=0
trap 'rm -rf "$TMP"; if [ "$DONE" = 0 ]; then printf "\nA telepítés félbemaradt. A dwyt-ben a(z) %s futtató már létrejött: vond vissza a Futtatók oldalon, és kérj új parancsot.\n" "$NAME" >&2; fi' EXIT

# ---- 2. who runs it ------------------------------------------------------------------------------------
if [ "$(id -u)" = "0" ]; then
  SYSTEM=1
  RUN_USER="dwyt-runner"
  HOME_DIR="/var/lib/dwyt-runner"
  command -v runuser >/dev/null 2>&1 || die "a runuser program kell hozzá (util-linux)."
  if ! id "$RUN_USER" >/dev/null 2>&1; then
    say "2/5 A dwyt-runner rendszerfelhasználó létrehozása ($HOME_DIR) …"
    useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin "$RUN_USER" \
      || die "nem tudtam létrehozni a $RUN_USER felhasználót."
  else
    say "2/5 A dwyt-runner rendszerfelhasználó már megvan."
  fi
  # a clean environment for the runner's user (root's variables stay with root), only proxy and CA settings kept
  PASS=""
  for v in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy SSL_CERT_FILE SSL_CERT_DIR; do
    eval "val=\${$v:-}"
    [ -n "$val" ] && PASS="$PASS $v=$val"
  done
  as_user() {
    # shellcheck disable=SC2086
    runuser -u "$RUN_USER" -- env -i HOME="$HOME_DIR" LANG=C.UTF-8 \
      PATH="$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin" $PASS "$@"
  }
else
  SYSTEM=0
  RUN_USER=$(id -un)
  HOME_DIR="$HOME"
  say "2/5 Telepítés a saját felhasználódnak ($RUN_USER), rendszergazdai jog nélkül."
  as_user() { env PATH="$HOME_DIR/.local/bin:$PATH" "$@"; }
fi

# ---- 3. uv and the runner -------------------------------------------------------------------------------
if as_user sh -c 'command -v uv' >/dev/null 2>&1; then
  say "3/5 Az uv már telepítve van."
else
  say "3/5 Az uv telepítése (https://astral.sh/uv) …"
  curl -LsSf https://astral.sh/uv/install.sh -o "$TMP/uv-install.sh" || die "az uv telepítője nem tölthető le."
  chmod 755 "$TMP" "$TMP/uv-install.sh"
  as_user env UV_NO_MODIFY_PATH=1 sh "$TMP/uv-install.sh" >/dev/null || die "az uv telepítése nem sikerült."
fi
say "4/5 A futtató telepítése (Python 3.13-mal, ez pár perc) …"
if ! as_user uv tool install --force --python 3.13 "dwyt-runner @ $SOURCE" > "$TMP/uv.log" 2>&1; then
  tail -n 15 "$TMP/uv.log" >&2
  die "a futtató telepítése nem sikerült (uv tool install)."
fi
RUNNER="$HOME_DIR/.local/bin/dwyt-runner"
[ -x "$RUNNER" ] || die "a futtató nem került ide: $RUNNER"
printf '%s\n' "$TOKEN" | as_user "$RUNNER" adopt --server "$CENTRAL" --runner-id "$RUNNER_ID" --name "$NAME" \
  --token-stdin || die "a futtató nem tudta elmenteni a párosítást."
TOKEN=""

# ---- 4. the service ------------------------------------------------------------------------------------
say "5/5 Szolgáltatás beállítása …"
if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
  DONE=1
  say "Ezen a gépen nem a systemd fut. A futtató telepítve és párosítva; indítsd így:"
  if [ "$SYSTEM" = "1" ]; then say "  runuser -u $RUN_USER -- $RUNNER run"; else say "  $RUNNER run"; fi
  exit 0
fi
if [ "$SYSTEM" = "1" ]; then
  cat > /etc/systemd/system/dwyt-runner.service <<EOF
# written by $CENTRAL/futtato.sh
[Unit]
Description=dwyt runner
Wants=network-online.target
After=network-online.target

[Service]
User=$RUN_USER
Environment=HOME=$HOME_DIR
Environment=PATH=$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$RUNNER run
Restart=on-failure
RestartSec=10
# 2: the token was revoked, 3: the dwyt service wants a newer runner; restarting would not help
RestartPreventExitStatus=2 3

[Install]
WantedBy=multi-user.target
EOF
  chmod 644 /etc/systemd/system/dwyt-runner.service
  systemctl daemon-reload
  systemctl enable --now dwyt-runner >/dev/null 2>&1 || die "a szolgáltatás nem indult el: systemctl status dwyt-runner"
  systemctl restart dwyt-runner
  DONE=1
  say ""
  say "Kész: a(z) $NAME futtató fut (systemctl status dwyt-runner, napló: journalctl -u dwyt-runner)."
else
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME_DIR/.config}/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/dwyt-runner.service" <<EOF
# written by $CENTRAL/futtato.sh
[Unit]
Description=dwyt runner
Wants=network-online.target
After=network-online.target

[Service]
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/bin/dwyt-runner run
Restart=on-failure
RestartSec=10
RestartPreventExitStatus=2 3

[Install]
WantedBy=default.target
EOF
  DONE=1
  if systemctl --user daemon-reload 2>/dev/null && systemctl --user enable --now dwyt-runner >/dev/null 2>&1; then
    systemctl --user restart dwyt-runner
    if ! loginctl enable-linger "$RUN_USER" 2>/dev/null; then
      say "Figyelem: a futtató csak addig fut, amíg be vagy jelentkezve. Hogy kijelentkezés után is fusson,"
      say "egy rendszergazda futtassa: loginctl enable-linger $RUN_USER"
    fi
    say ""
    say "Kész: a(z) $NAME futtató fut (systemctl --user status dwyt-runner, napló: journalctl --user -u dwyt-runner)."
  else
    say "A felhasználói szolgáltatás nem indítható ebből a munkamenetből. A futtató telepítve és párosítva;"
    say "indítsd így: systemctl --user enable --now dwyt-runner (vagy: $RUNNER run)"
  fi
fi
say "A beállításokat a dwyt Futtatók oldalán, a Beállítás gombbal adhatod meg: $CENTRAL/futtatok"
