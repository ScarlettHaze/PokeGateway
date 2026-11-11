# PokeGateway

A self-hosted replacement for Nintendo Wi-Fi Connection (WFC) for the
**Generation 4 and 5 Pokémon games**: Diamond, Pearl, Platinum, HeartGold,
SoulSilver, Black, White, Black 2 and White 2.

You point your DS at PokeGateway's **DNS server**. It answers every
Nintendo/GameSpy hostname with its own IP and runs all of the services
those games talk to. Other lookups are forwarded to a normal upstream
resolver. It is pure Python 3.10+ with no dependencies.

## Features

| Service | Hostname(s) | Port | Games | Status |
|---|---|---|---|---|
| DNS redirect + forwarding | — | 53 udp/tcp | all | ✅ |
| Connection test | conntest.nintendowifi.net | 80 | all | ✅ |
| NAS login / account creation / profanity check | nas.nintendowifi.net | 443 → 80 | all | ✅ (needs [SSL proxy](#ssl-certificates)) |
| Friends, presence, buddy messages (GPCM) | gpcm.gs.nintendowifi.net | 29900 | all | ✅ |
| Player search (GPSP) | gpsp.gs.nintendowifi.net | 29901 | all | ✅ |
| Matchmaking host reporting (QR2) | *.master.gs.nintendowifi.net | 27900 udp | all | ✅ |
| Server browser (Wi-Fi Club, friend battles/trades, Random Matchup) | *.ms*.gs.nintendowifi.net | 28910 | all | ✅ |
| NAT negotiation (peer-to-peer connections) | natneg*.gs.nintendowifi.net | 27901 udp | all | ✅ |
| **GTS**: deposit, search, trade, take back, receive | gamestats2.gs.nintendowifi.net | 80 | Gen 4 & 5 | ✅ |
| Trainer profile upload (setProfile) | gamestats2 | 80 | Gen 4 & 5 | ✅ |
| Wi-Fi Battle Tower / Battle Subway (opponents + leaders) | gamestats2 | 80 | Gen 4 & 5 | ✅ (filler trainers until players upload) |
| Pokémon validation (pkvldtprod) | pkvldtprod.nintendo.co.jp | 443 → 80 | Gen 5 | ✅ (accepts everything) |
| Battle Videos: upload, search, watch, save | pkgdsprod.nintendo.co.jp | 12400 | Pt / HGSS | ✅ |
| Dress-up photos, Box uploads | pkgdsprod.nintendo.co.jp | 12400 | Pt / HGSS | ✅ |
| Battle Videos, Musical photos | pkgdsprod.nintendo.co.jp | 12401 → 12411 | BW / B2W2 | ✅ (needs SSL proxy) |
| Wi-Fi Mystery Gift downloads (DLS1) | dls1.nintendowifi.net | 80 | all | ✅ (put files in `dlc/<GAMECODE>/`) |
| Wi-Fi Plaza web calls (schedule, questionnaire, VIPs, profile) | gamestats2 | 80 | Pt / HGSS | ⚠️ partial: the Plaza's IRC chat server (peerchat) is not implemented |
| Trainer Rankings | pkgdsprod | 12400 | Pt / HGSS | ❌ not implemented (shows an error in-game) |
| Game Sync / Pokémon Global Link / Dream World | pokemon-gl.com | 443 | Gen 5 | ❌ not implemented (redirected so it fails cleanly) |

There is also a status page at `http://<server-ip>/` showing online
players, GTS listings and upload counts.

## Quick start

```bash
cp config.example.ini config.ini
```

```bash
sudo python3 -m pokegateway -c config.ini
```

It needs root, or `CAP_NET_BIND_SERVICE`, to bind ports 53 and 80. When it
starts, it logs the IP your DS should use as its DNS server. On the DS
(for Gen 5, use the DS/DSi settings menu, not the in-game one):

1. Open **Nintendo WFC Settings → Connection Settings → your connection**.
2. Set **Auto-obtain DNS** to *No*, and set both the **Primary** and
   **Secondary DNS** to the server's IP.
3. Run the connection test, then connect in-game.

The DS only supports open or WEP Wi-Fi networks. A DSi or 3DS running a
DS game has the same limit.

### Docker

```bash
cp config.example.ini config.ini
```

```bash
mkdir -p certs data dlc
```

```bash
docker compose up -d --build
```

The compose file runs PokeGateway and the legacy SSL proxy, both with host
networking. Host networking is required so that the matchmaking and NAT
negotiation servers see real client addresses.

## SSL certificates

The DS logs in over HTTPS to `nas.nintendowifi.net`, and Gen 5 opens an
SSL connection to `pkgdsprod.nintendo.co.jp:12401`. The DS only supports
**SSLv3 with RC4**. Modern OpenSSL, and therefore Python, refuses to use
that, so `deploy/sslproxy` builds nginx against OpenSSL 1.0.2 with those
ciphers enabled. The proxy forwards:

* `443` to `127.0.0.1:80` (NAS and pkvldtprod)
* `12401` to `127.0.0.1:12411` (Gen 5 Global Terminal)

Put these files in `./certs`:

* `gt5.crt` / `gt5.key`: any certificate. The game does not validate it:
  ```bash
  openssl req -x509 -newkey rsa:1024 -nodes -days 3650 -subj "/CN=pkgdsprod.nintendo.co.jp" -keyout certs/gt5.key -out certs/gt5.crt
  ```
* `nas.crt` / `nas.key`: a certificate for `nas.nintendowifi.net` that the
  DS accepts. The DS checks that the chain ends at Nintendo's CA, but it
  has a known flaw: it accepts a leaf certificate signed by the Wii's NWC
  client certificate. The usual approach, used by the AltWFC/Wiimmfi
  projects, is to sign your own `nas.nintendowifi.net` certificate with
  the NWC certificate and key dumped from **your own** Wii. Then put
  `leaf + NWC cert` in `nas.crt`. That certificate can't be shipped here.

If you only want to test without the DS, NAS also answers over plain HTTP
on port 80.

## Wi-Fi Mystery Gift

Put the event files in `dlc/<GAMECODE>/`, for example
`dlc/IPKE/myevent.myg` for HeartGold (US). Every file in that folder is
offered. To control attribute filtering, add a `_list.txt` file with
`name<TAB><TAB>attr1<TAB>attr2<TAB>attr3<TAB>size` lines.

## Matchmaking game keys

The QR and server-browser servers need each game's GameSpy secret key.
The defaults come from the game code (see the decompilation projects
linked below):

| Game | GameSpy name | Key |
|---|---|---|
| Diamond / Pearl / HeartGold / SoulSilver | `pokemondpds` | `1vTlwb` |
| Platinum (and the Pt/HGSS Wi-Fi Plaza) | `pokemonplatds` | `IIup73` |
| Black / White / Black 2 / White 2 | `syachi2ds` | `tXH2sN` |

To add or override keys, use the `[gamekeys]` section of `config.ini`. If
a game sends a name with no known key, a warning is logged.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests act as DS clients. They cover DNS, the NAS login, the GPCM
login hash and friend flow, a full Gen 4 and Gen 5 GTS trade, Battle
Tower, QR registration, the encrypted server-browser list and message
forwarding, NAT negotiation pairing, and Global Terminal uploads,
searches and downloads.

```
pokegateway/
  dns_server.py       DNS redirect + upstream forwarding
  http_server.py      tiny asyncio HTTP server
  app.py              HTTP routing + status page
  nas.py              nas.nintendowifi.net (/ac, /pr) + conntest
  gamestats.py        GTS, Battle Tower/Subway, setProfile, Plaza, pkvldtprod
  pokemon.py          GTS record / Battle Tower structures
  gpcm.py, gpsp.py    GameSpy presence (friends) and player search
  matchmaking.py      QR2 + server browser
  enctypex.py         server-browser list cipher
  sqlfilter.py        safe evaluator for server-browser filters
  natneg.py           NAT negotiation
  global_terminal.py  battle videos, dress-ups, boxes, musicals
  dls1.py             Wi-Fi Mystery Gift downloads
  db.py               SQLite storage
```

## Credits

Protocol knowledge comes from these community reverse-engineering projects:

* [dwc_network_server_emulator](https://github.com/barronwaffles/dwc_network_server_emulator) (AltWFC): NAS, GameSpy, QR, server browser, natneg, EncTypeX
* [pkmn-classic-framework](https://github.com/mm201/pkmn-classic-framework): GTS, Battle Tower, Global Terminal formats
* [pokeheartgold](https://github.com/ctrl-cheeb-del/pokeheartgold), [pokebw2](https://github.com/fuddlesworth/pokebw2), [aletheia](https://github.com/Project-Holon/aletheia) decompilations: game names, keys, URLs and the gamestats cipher, confirmed against the game code
* Luigi Auriemma: EncTypeX / GameSpy documentation

Pokémon and Nintendo DS are trademarks of Nintendo, Creatures Inc. and GAME FREAK.
This project is not affiliated with them.
