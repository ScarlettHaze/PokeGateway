"""In-process SSLv3/RC4 TLS terminator (pure Python, via tlslite-ng).

The Nintendo DS only speaks SSLv3 with RC4, which OpenSSL — and therefore
Python's built-in ssl module — refuses. tlslite-ng implements it in pure
Python, so PokeGateway can terminate the DS's HTTPS/SSL connections itself
and forward the plaintext to the local HTTP and Global Terminal servers.
No external stunnel/nginx proxy is needed.

This is what makes the whole stack run as one process, which matters on
macOS where Docker's host networking can't reach a DS on the LAN.

Approach (same as the Entralinked Game Sync server): terminate TLS with
tlslite, then pipe the decrypted bytes to a plain local backend socket.
The DS never sends a TLS close_notify, so an abrupt close is treated as a
normal end of stream.
"""

import logging
import socket
import threading

log = logging.getLogger("tls")

try:
    from tlslite import HandshakeSettings, TLSConnection, X509, X509CertChain
    from tlslite.errors import TLSAbruptCloseError, TLSError
    from tlslite.utils.keyfactory import parsePEMKey
    HAVE_TLSLITE = True
except ImportError:  # pragma: no cover
    HAVE_TLSLITE = False


def _load_chain(cert_path, key_path):
    with open(key_path) as fh:
        key = parsePEMKey(fh.read(), private=True)
    certs = []
    with open(cert_path) as fh:
        blob = fh.read()
    for part in blob.split("-----END CERTIFICATE-----"):
        part = part.strip()
        if part:
            x = X509()
            x.parse(part + "\n-----END CERTIFICATE-----\n")
            certs.append(x)
    if not certs:
        raise ValueError("no certificates in %s" % cert_path)
    return key, X509CertChain(certs)


def _settings():
    s = HandshakeSettings()
    s.minVersion = (3, 0)      # SSLv3
    s.maxVersion = (3, 1)      # TLS 1.0
    s.cipherNames = ["rc4"]
    s.macNames = ["md5", "sha"]
    s.keyExchangeNames = ["rsa"]
    return s


class Terminator:
    def __init__(self, name, bind, listen_port, target, key, chain):
        self.name = name
        self.bind = bind
        self.listen_port = listen_port
        self.target = target          # (host, port) plain backend
        self.key = key
        self.chain = chain
        self.settings = _settings()
        self.sock = None

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.bind, self.listen_port))
        self.sock.listen(64)
        threading.Thread(target=self._accept_loop, name="tls-%s" % self.name,
                         daemon=True).start()
        log.info("TLS terminator (%s) on %s:%d -> %s:%d", self.name, self.bind,
                 self.listen_port, self.target[0], self.target[1])

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def _accept_loop(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        client.settimeout(30)
        conn = TLSConnection(client)
        try:
            conn.handshakeServer(privateKey=self.key, certChain=self.chain,
                                 settings=self.settings)
        except (TLSError, OSError, SyntaxError) as exc:
            log.debug("%s: handshake failed: %r", self.name, exc)
            try:
                client.close()
            except OSError:
                pass
            return
        try:
            backend = socket.create_connection(self.target, timeout=30)
        except OSError as exc:
            log.debug("%s: backend connect failed: %r", self.name, exc)
            self._quiet_close(conn)
            return
        # Pipe backend -> TLS in a second thread; TLS -> backend here.
        up = threading.Thread(target=self._backend_to_tls, args=(backend, conn),
                              daemon=True)
        up.start()
        try:
            while True:
                try:
                    data = conn.read(max=16384)
                except TLSAbruptCloseError:
                    break          # DS closed without close_notify: normal
                except (TLSError, OSError):
                    break
                if not data:
                    break
                backend.sendall(bytes(data))
        except OSError:
            pass
        finally:
            try:
                backend.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        up.join(timeout=10)
        try:
            backend.close()
        except OSError:
            pass
        self._quiet_close(conn)

    @staticmethod
    def _backend_to_tls(backend, conn):
        try:
            while True:
                data = backend.recv(16384)
                if not data:
                    break
                conn.write(data)
        except (OSError, TLSError):
            pass

    @staticmethod
    def _quiet_close(conn):
        try:
            conn.close()
        except (TLSError, OSError):
            pass


def start(config, public_ip):
    """Start TLS terminators for the DS's HTTPS/SSL endpoints. Returns a list
    of objects with .close(); empty if TLS can't be enabled."""
    if not config.tls_enabled:
        return []
    if not HAVE_TLSLITE:
        log.warning("tlslite-ng is not installed, so HTTPS (login, GTS validation, "
                    "Mystery Gift, Gen 5 Global Terminal) is disabled. "
                    "Install it with: pip install tlslite-ng")
        return []

    backend_host = "127.0.0.1" if config.bind in ("0.0.0.0", "") else config.bind
    terminators = []
    try:
        key, chain = _load_chain(config.tls_cert, config.tls_key)
    except (OSError, ValueError) as exc:
        log.warning("Could not load HTTPS certificate (%s / %s): %s. HTTPS is "
                    "disabled; run deploy/sslproxy/gen-certs.sh.", config.tls_cert,
                    config.tls_key, exc)
        return []

    terminators.append(Terminator("https", config.bind, config.https_port,
                                  (backend_host, config.http_port), key, chain))
    # Gen 5 Global Terminal: DS connects to 12401 over SSL -> plain gt5 port.
    gt5_key, gt5_chain = key, chain
    try:
        gt5_key, gt5_chain = _load_chain(config.gt5_cert, config.gt5_key)
    except (OSError, ValueError):
        pass          # fall back to the main cert; the game doesn't validate it
    terminators.append(Terminator("gt5", config.bind, config.gt5_tls_port,
                                  (backend_host, config.gt5_port), gt5_key, gt5_chain))

    started = []
    for t in terminators:
        try:
            t.start()
            started.append(t)
        except OSError as exc:
            log.error("Could not start TLS terminator %s on port %d: %s", t.name,
                      t.listen_port, exc)
    return started
