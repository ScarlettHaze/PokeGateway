FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY pokegateway ./pokegateway
COPY config.example.ini ./config.ini
RUN mkdir -p data dlc certs
# 443 + 12401: the DS's SSLv3/RC4 endpoints, terminated in-process (tlslite).
EXPOSE 53/udp 53/tcp 80 443 12400 12401 12411 27900/udp 27901/udp 28910 29900 29901
CMD ["python", "-m", "pokegateway", "-c", "config.ini"]
