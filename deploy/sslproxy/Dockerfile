# nginx linked against OpenSSL 1.0.2 with SSLv3 + RC4 enabled.
#
# The Nintendo DS only speaks SSLv3 with RC4 ciphers, which modern OpenSSL
# (and therefore Python's ssl module) no longer supports. This proxy
# terminates those connections and forwards plain traffic to PokeGateway:
#   443   (nas.nintendowifi.net, pkvldtprod.nintendo.co.jp) -> http :80
#   12401 (Gen 5 Global Terminal)                             -> tcp  :12411
FROM debian:bookworm-slim AS build

ARG NGINX_VERSION=1.22.1
ARG OPENSSL_VERSION=1.0.2u

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl libpcre3-dev zlib1g-dev perl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN curl -fsSLO https://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz \
    && curl -fsSLO https://www.openssl.org/source/old/1.0.2/openssl-${OPENSSL_VERSION}.tar.gz \
    && tar xzf nginx-${NGINX_VERSION}.tar.gz && tar xzf openssl-${OPENSSL_VERSION}.tar.gz

WORKDIR /src/nginx-${NGINX_VERSION}
RUN ./configure \
        --prefix=/etc/nginx --sbin-path=/usr/sbin/nginx \
        --conf-path=/etc/nginx/nginx.conf \
        --error-log-path=/dev/stderr --http-log-path=/dev/stdout \
        --pid-path=/run/nginx.pid \
        --with-http_ssl_module --with-stream --with-stream_ssl_module \
        --with-openssl=/src/openssl-${OPENSSL_VERSION} \
        --with-openssl-opt="enable-ssl3 enable-ssl3-method enable-weak-ssl-ciphers no-shared" \
    && make -j"$(nproc)" && make install

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libpcre3 zlib1g \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/sbin/nginx /usr/sbin/nginx
COPY --from=build /etc/nginx /etc/nginx
COPY nginx.conf /etc/nginx/nginx.conf
CMD ["nginx", "-g", "daemon off;"]
