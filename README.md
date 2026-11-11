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
| Wi-Fi Mystery Gift downloads (DLS1), B2W2 PWT tournament downloads | dls1.nintendowifi.net | 443 → 80 | all | ✅ (see [Wi-Fi Mystery Gift](#wi-fi-mystery-gift)) |
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

### Docker (one command)

Put your Wii NWC certificate material in `certs/` first (either
`WII_NWC_1_CERT.p12`, or `NWC.crt` + `NWC.key` — see
[SSL certificates](#ssl-certificates)), then:

```bash
./start.sh
```

`start.sh` detects your LAN IP, writes it into `config.ini`, generates the
TLS certificates (via `deploy/sslproxy/gen-certs.sh`) if they're missing,
and launches both containers with `docker compose`. It prints the IP to
enter as your DS's DNS.

The compose file runs PokeGateway and the SSL proxy with host networking,
which is required so the matchmaking and NAT negotiation servers see real
client addresses. To run the steps yourself: `cp config.example.ini
config.ini`, `bash deploy/sslproxy/gen-certs.sh`, `docker compose up -d
--build`.

## SSL certificates

The DS logs in over HTTPS (`nas.nintendowifi.net`), downloads Mystery
Gifts over HTTPS (`dls1.nintendowifi.net`), and Gen 5 opens an SSL
connection to `pkgdsprod.nintendo.co.jp:12401`. The DS only supports
**SSLv3 with RC4-SHA/RC4-MD5**. Modern OpenSSL, and therefore Python,
refuses to use that. `deploy/sslproxy` builds stunnel against an OpenSSL
1.1.1 that is patched to allow it, and it handles two DS quirks: the DS
never sends `close_notify`, and it can't handle the empty-fragment
countermeasure. This follows the setup tested on real hardware by
[NDS-pokemon-mystery-gift-distribution](https://github.com/FreakedOutGamer/NDS-pokemon-mystery-gift-distribution).
The proxy forwards:

* `443` to `127.0.0.1:80` (NAS, DLS1, pkvldtprod)
* `12401` to `127.0.0.1:12411` (Gen 5 Global Terminal)

The build, the forwarding and the generated certificates have been tested
with an SSLv3/RC4-SHA client (not a DS): NAS login, a Gen 5 Mystery Gift
download and a Gen 5 Global Terminal request all complete, and the
certificate presented chains `*.*.*` → `Wii NWC Prod 1` → `Nintendo CA`,
the path a DS trusts.

The DS has a known flaw, **nds-constrain't**: it doesn't check whether an
intermediate certificate is allowed to sign others. So a certificate signed
with the **Wii NWC** client certificate — which Nintendo's CA signed — is
accepted as if Nintendo issued it. You supply that Wii certificate, and
`deploy/sslproxy/gen-certs.sh` builds the rest.

1. Put the Wii NWC material in `certs/`, as **either**:
   * `WII_NWC_1_CERT.p12` (the PKCS#12 bundle; password `alpine`), **or**
   * `NWC.crt` and `NWC.key` (PEM).

   Dump it from your own Wii, or use the public bundle the DS preservation
   community circulates (e.g. in
   [NDS-pokemon-mystery-gift-distribution](https://github.com/FreakedOutGamer/NDS-pokemon-mystery-gift-distribution/tree/main)).
   This file is **not** included here and is gitignored so it is never
   committed.

2. Generate the certificates:
   ```bash
   bash deploy/sslproxy/gen-certs.sh
   ```

This writes `server-chain.crt` + `server.key` (the NAS/DLS1 certificate,
1024-bit RSA / SHA-1, signed by the Wii NWC cert) and a self-signed
`gt5.crt` + `gt5.key` for the Gen 5 Global Terminal (which the game does
not validate). `start.sh` runs this step for you.

The proxy forwards:

* `443` to `127.0.0.1:80` (NAS, DLS1, pkvldtprod)
* `12401` to `127.0.0.1:12411` (Gen 5 Global Terminal)

If you only want to test without the DS, NAS and DLS1 also answer over
plain HTTP on port 80.

## Wi-Fi Mystery Gift

The `dlc/` folder uses the same layout as
[InternalLoss/DLS1-Files](https://github.com/InternalLoss/DLS1-Files), the
collection Wiimmfi serves. You can copy its game-code folders straight in,
`_list.txt` files included:

* **Gen 4** (`ADA*` Diamond/Pearl, `CPU*` Platinum, `IPG*`
  HeartGold/SoulSilver): 936-byte `.myg` files. The game takes one gift
  per connection. Which one depends on the **DS's date**: day of year,
  modulo the number of gifts. To choose a gift, change the DS clock. Pearl
  (`APA*`) and HeartGold (`IPK*`) fall back to the Diamond and SoulSilver
  folders.
* **Gen 5**: every BW/B2W2 cartridge sends the shared code `IRAO`
  (international), `IRAJ` or `IRAK`. Gifts are 720-byte `.bin` files
  listed in `_list.txt` as
  `name<TAB><TAB>MYSTERY_<lang><TAB><game mask><TAB><TAB>size`. The game
  mask is hex: 100000 Black, 200000 White, 400000 White 2, 800000 Black 2.
  Each game is only offered gifts with its bit set, picked by date as in
  Gen 4. The same folders also hold B2W2 downloadable PWT tournaments
  (`WBT_<lang>` entries), which are listed normally.

Without a `_list.txt`, every file in the folder is offered with blank
attributes. That is fine for Gen 4, but Gen 5 needs the attributes.

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
* [NDS-pokemon-mystery-gift-distribution](https://github.com/FreakedOutGamer/NDS-pokemon-mystery-gift-distribution): SSLv3/RC4 stunnel setup tested on real hardware, the HTTP/1.0 requirement, and the certificate recipe
* [DLS1-Files](https://github.com/InternalLoss/DLS1-Files): Mystery Gift file and listing format

Pokémon and Nintendo DS are trademarks of Nintendo, Creatures Inc. and GAME FREAK.
This project is not affiliated with them.
