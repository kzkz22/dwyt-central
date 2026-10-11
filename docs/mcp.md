# MCP: a könyvtár MCP-kliensekből

A központ a `/mcp` címen MCP-szerverként is működik (Streamable HTTP). Így egy MCP-kliens (Hermes, OpenCode, Claude Code és mások) kereshet a könyvtáradban, átiratot és összefoglalót olvashat, és ha engeded, átírást is indíthat. „Kérdezz a videóról”: a kliens modellje a `kereses`, `atirat` és `osszefoglalo` eszközzel válaszol.

## Token

A **Beállítások → MCP-hozzáférés** részen hozz létre egy tokent, és adj neki nevet (például `Hermes`). A token csak egyszer látszik; a központ csak a lenyomatát tárolja. Kliensenként külön tokent érdemes csinálni, mert egyenként visszavonható.

- **Átírást is indíthat:** e nélkül csak az olvasó eszközök látszanak.
- **A saját felvételeimet is láthatja:** csak akkor kapcsold be, ha a kliens modellje helyi, mert a feltöltött és a figyelt mappás felvételek szövege a kliens modelljéhez kerül.

## Kliensek

A cím: `https://dwyt.iodate.date/mcp`, a fejléc: `Authorization: Bearer TOKEN`.

**Hermes** (`%USERPROFILE%\.hermes\config.yaml`; a tokent a `%USERPROFILE%\.hermes\.env`-be érdemes tenni `DWYT_MCP_TOKEN=...` sorként):

```yaml
mcp_servers:
  dwyt:
    url: "https://dwyt.iodate.date/mcp"
    headers:
      Authorization: "Bearer ${DWYT_MCP_TOKEN}"
```

Utána egy futó Hermesben `/reload-mcp` (vagy új munkamenet). Az eszközök `mcp_dwyt_kereses`, `mcp_dwyt_atirat` stb. néven látszanak.

**OpenCode** (`opencode.json`; a token a `DWYT_MCP_TOKEN` környezeti változóból):

```json
"mcp": {
  "dwyt": {
    "type": "remote",
    "url": "https://dwyt.iodate.date/mcp",
    "headers": { "Authorization": "Bearer {env:DWYT_MCP_TOKEN}" },
    "enabled": true
  }
}
```

**Claude Code:** `claude mcp add --transport http dwyt https://dwyt.iodate.date/mcp --header "Authorization: Bearer TOKEN"`

**Ellenőrzés kézzel:**

```bash
curl -s https://dwyt.iodate.date/mcp -H "Authorization: Bearer TOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-06-18' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Rossz vagy visszavont tokennel `401`, túl sok kéréssel `429` jön.

## Eszközök

| Eszköz | Mit tud |
| --- | --- |
| `kereses` | Keresés szóra, szórészletre és jelentésre, mint a felületen; szűrés csatornára, nyelvre, dátumra. Elemenként legfeljebb 3 találat időponttal és YouTube-linkkel. |
| `atirat` | Egy elem átirata eredetiben, magyarul vagy mindkettőben, időszakra szűkíthetően; hosszú átiratnál részletekben (a válasz végén a folytatás helye). |
| `osszefoglalo` | Az elem aktuális magyar összefoglalója. |
| `legutobbiak` | A legutóbb bekerült elemek, csatornára szűrhetően. |
| `atiras_inditasa` | Csak az indításra jogosult tokennel. Új videó URL-je vagy meglévő elem újraírása; alapból YouTube-felirat, helyi Whisper kérésre, felhő csak kifejezett kérésre. A munkát a futtatód végzi. |
| `munka_allapota` | Csak az indításra jogosult tokennel. Egy munka állapota, a hosszú munkáknál a haladás is. |

Az eszközök, a paraméterek és a válaszok magyarul vannak. Minden elemnél ott az azonosító és a felület linkje.
