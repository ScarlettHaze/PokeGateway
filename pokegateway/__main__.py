"""Entry point: python -m pokegateway [-c config.ini]"""

import argparse
import errno
import asyncio
import logging
import signal

from . import __version__, dns_server, global_terminal, gpcm, gpsp, http_server, \
    matchmaking, natneg
from .app import WebApp
from .config import load_config
from .db import Database

log = logging.getLogger("pokegateway")


async def run(config):
    public_ip = config.resolved_public_ip()
    db = Database(config.db_path)
    state = {}
    servers = []
    failures = []

    async def launch(name, port, coro):
        """Start one service; on a bind error log it and keep the rest up."""
        try:
            result = await coro
            if result is not None:
                servers.append(result)
            return result
        except OSError as exc:
            failures.append((name, port, exc))
            log.error("Could not start %s on port %s: %s", name, port, exc)
            if exc.errno == errno.EADDRINUSE:
                log.error("  Port %s is already in use. Another program (often a system "
                          "DNS/HTTP service) holds it. Free it, or change the port in "
                          "config.ini.", port)
            return None

    if config.dns_enabled:
        await launch("DNS", config.dns_port, dns_server.start(config, public_ip))
    await launch("HTTP", config.http_port,
                 http_server.start(WebApp(config, db, state), config.bind, config.http_port))

    async def start_gpcm():
        gpcm_server, srv = await gpcm.start(db, config.bind, config.gpcm_port)
        state["gpcm"] = gpcm_server
        return srv
    await launch("GPCM", config.gpcm_port, start_gpcm())
    await launch("GPSP", config.gpsp_port, gpsp.start(db, config.bind, config.gpsp_port))

    async def start_mm():
        registry, srv = await matchmaking.start(config)
        state["registry"] = registry
        return srv
    await launch("matchmaking", "%s/%s" % (config.qr_port, config.sb_port), start_mm())
    await launch("NATNEG", config.natneg_port, natneg.start(config.bind, config.natneg_port))
    await launch("Global Terminal (Gen 4)", config.gt4_port,
                 global_terminal.start(db, config.bind, config.gt4_port, 4))
    await launch("Global Terminal (Gen 5)", config.gt5_port,
                 global_terminal.start(db, config.bind, config.gt5_port, 5))

    if failures and len(failures) >= 7:
        # Nothing meaningful came up (almost every port taken). Exit so the
        # container doesn't masquerade as healthy.
        raise SystemExit("No services could start; check the port errors above.")
    if failures:
        log.warning("%d of the services could not start (see errors above); the rest are "
                    "running.", len(failures))
    log.info("PokeGateway %s ready. Set your DS's primary DNS to %s", __version__, public_ip)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    log.info("Shutting down")
    for s in servers:
        s.close()


def main():
    parser = argparse.ArgumentParser(description="Nintendo WFC replacement for Pokemon Gen 4/5")
    parser.add_argument("-c", "--config", default="config.ini", help="config file (INI)")
    parser.add_argument("--public-ip", help="IP address the DNS server answers with")
    parser.add_argument("--no-dns", action="store_true", help="don't run the DNS server")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.public_ip:
        config.public_ip = args.public_ip
    if args.no_dns:
        config.dns_enabled = False
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s")
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
