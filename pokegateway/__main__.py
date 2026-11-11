"""Entry point: python -m pokegateway [-c config.ini]"""

import argparse
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

    if config.dns_enabled:
        servers.append(await dns_server.start(config, public_ip))
    servers.append(await http_server.start(WebApp(config, db, state), config.bind,
                                           config.http_port))
    gpcm_server, srv = await gpcm.start(db, config.bind, config.gpcm_port)
    state["gpcm"] = gpcm_server
    servers.append(srv)
    servers.append(await gpsp.start(db, config.bind, config.gpsp_port))
    registry, srv = await matchmaking.start(config)
    state["registry"] = registry
    servers.append(srv)
    await natneg.start(config.bind, config.natneg_port)
    servers.append(await global_terminal.start(db, config.bind, config.gt4_port, 4))
    servers.append(await global_terminal.start(db, config.bind, config.gt5_port, 5))

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
