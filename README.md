# Unstoppable Relay Server (Python)

Relay WebSocket compatibile con il client Android attuale:
- riceve `announce`
- riceve `punch_register`
- invia `punch_now`

## 1) Prerequisiti

- VPS con IP statico
- DNS A record: `relay1.tuodominio.com -> IP_VPS`
- Docker + Docker Compose installati
- Porte aperte: `80/tcp`, `443/tcp`

## 2) Deploy

```bash
cd relay-server
cp .env.example .env
# modifica RELAY_HOST e ACME_EMAIL
docker compose up -d --build
```

Verifica:

```bash
curl https://relay1.tuodominio.com/healthz
```

Output atteso: `{"ok":true,"connectedPeers":0}`

## 3) Configurazione app Android

Nel file `app/build.gradle.kts`:

```kotlin
buildConfigField(
    "String",
    "RELAY_URLS",
    "\"wss://relay1.tuodominio.com/ws,wss://relay2.tuodominio.com/ws\""
)
```

Poi build/install dell'app.

## 4) Alta disponibilita` consigliata

- Replica su una seconda VPS (`relay2.tuodominio.com`)
- Metti entrambi in `RELAY_URLS`
- Il client ruota automaticamente sugli URL successivi in caso di problemi.

## 5) Note operative

- Questo relay non trasporta i chunk media: coordina solo discovery/punch NAT.
- Per i certificati TLS usa automaticamente Let's Encrypt via Caddy.
- Log:

```bash
docker compose logs -f relay
docker compose logs -f caddy
```
