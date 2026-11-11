FROM python:3.12-slim
WORKDIR /app
COPY pokegateway ./pokegateway
COPY config.example.ini ./config.ini
RUN mkdir -p data dlc
EXPOSE 53/udp 53/tcp 80 12400 12411 27900/udp 27901/udp 28910 29900 29901
CMD ["python", "-m", "pokegateway", "-c", "config.ini"]
