# SSLv3/RC4 terminator for the Nintendo DS (stunnel + patched OpenSSL).
#
# The DS only speaks SSLv3 with RC4-SHA/RC4-MD5, which modern OpenSSL (and
# therefore Python) refuses. This image terminates those connections and
# forwards plain traffic to PokeGateway:
#   443   (nas, dls1, pkvldtprod)     -> 127.0.0.1:80
#   12401 (Gen 5 Global Terminal)     -> 127.0.0.1:12411
#
# Approach (OpenSSL 1.1.1 with SSLv3 forced through the version/security
# checks, stunnel patched so it doesn't wait for close_notify, which the DS
# never sends) follows the hardware-tested setup in
# https://github.com/FreakedOutGamer/NDS-pokemon-mystery-gift-distribution
FROM debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates wget perl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
ARG OPENSSL_VERSION=1.1.1w
ARG STUNNEL_VERSION=5.72

RUN wget -q https://www.openssl.org/source/openssl-${OPENSSL_VERSION}.tar.gz \
    && tar xzf openssl-${OPENSSL_VERSION}.tar.gz \
    && wget -q https://www.stunnel.org/archive/5.x/stunnel-${STUNNEL_VERSION}.tar.gz \
    && tar xzf stunnel-${STUNNEL_VERSION}.tar.gz

# OpenSSL: enable SSLv3 + weak ciphers, security level 0, and skip the
# checks that would still refuse an SSLv3 handshake.
RUN cd openssl-${OPENSSL_VERSION} \
    && ./config enable-ssl3 enable-ssl3-method enable-weak-ssl-ciphers \
        -DOPENSSL_TLS_SECURITY_LEVEL=0 --prefix=/opt/openssl --openssldir=/opt/openssl \
    && sed -i 's/if (ssl_get_min_max_version(s, \&ver_min, \&ver_max, NULL) != 0)/if (0)/' ssl/statem/statem_lib.c \
    && sed -i 's/} else if (ver_max >= c->min_tls \&\& ver_max <= c->max_tls) {/} else if (1) {/' ssl/statem/statem_lib.c \
    && sed -i 's/if (client_version <= SSL3_VERSION)/if (0)/' ssl/statem/statem_lib.c \
    && sed -i 's/if ((s->options \& method->mask) != 0)/if (method->version != SSL3_VERSION \&\& (s->options \& method->mask) != 0)/' ssl/statem/statem_lib.c \
    && sed -i 's/ssl_security(s, SSL_SECOP_VERSION, 0, version, NULL) == 0/ssl_security(s, SSL_SECOP_VERSION, 0, version, NULL) == 0 \&\& version != SSL3_VERSION/' ssl/statem/statem_lib.c \
    && make -j"$(nproc)" && make install_sw

# stunnel: security level 0, and don't wait for the DS's (never sent)
# close_notify before tearing the connection down.
RUN cd stunnel-${STUNNEL_VERSION} \
    && sed -i 's/#define DEFAULT_SECURITY_LEVEL 2/#define DEFAULT_SECURITY_LEVEL 0/' src/common.h \
    && sed -i 's/section->timeout_close=60;/section->timeout_close=0;/' src/options.c \
    && sed -i 's/            timeout=c->opt->timeout_close;/            timeout=c->opt->timeout_idle;/' src/client.c \
    && ./configure --with-ssl=/opt/openssl --prefix=/opt/stunnel \
        --disable-libwrap --disable-systemd \
    && make -j"$(nproc)" && make install

FROM debian:bookworm-slim
COPY --from=builder /opt/openssl /opt/openssl
COPY --from=builder /opt/stunnel /opt/stunnel
RUN echo "/opt/openssl/lib" > /etc/ld.so.conf.d/openssl.conf && ldconfig
COPY stunnel.conf /etc/stunnel/stunnel.conf
CMD ["/opt/stunnel/bin/stunnel", "/etc/stunnel/stunnel.conf"]
