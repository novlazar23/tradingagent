# tradingagent

Eigenständiger BTC/USDT-Dienst für reproduzierbare Backtests und Paper-Trading
mit Chartmustern und technischen Indikatoren. Das System kann keine echten
Börsenorders senden.

## Betrieb mit Docker Compose

Voraussetzungen: Docker Engine mit Compose v2, erreichbarer OctoBot-History-
Service und ein lokales Secret. Die Anwendungs-API wird ausschließlich an
`127.0.0.1` veröffentlicht; PostgreSQL besitzt keinen Host-Port.

```bash
export POSTGRES_PASSWORD='ein-langes-lokales-passwort'
export OCTOBOT_HISTORY_API_KEY_FILE=/opt/docker/stacks/octobot/secrets/history_api_key
export OCTOBOT_HISTORY_BASE_URL=http://192.168.178.20:5002
docker compose up --build -d
docker compose ps
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

Der History-Schlüssel wird nur als read-only Docker Secret unter
`/run/secrets/octobot_history_api_key` eingebunden. Er darf nie in `.env`,
Compose-Environment, Images, Logs oder Supportausgaben kopiert werden.

Die Rollen sind getrennt:

- `api`: internes REST/OpenAPI-Control-Plane auf Port 8000
- `worker`: Hintergrundjobs
- `scheduler`: zeitbasierte Paper- und Datenjobs
- `postgres`: persistente Lauf-, Job- und Ledgerdaten im benannten Volume

Alle Anwendungscontainer laufen ohne root, ohne Linux-Capabilities, mit
read-only Root-Dateisystem und begrenztem `/tmp`. Logs rotieren lokal.

## Schnittstellen und Bedienung

OpenAPI ist lokal unter `http://127.0.0.1:8000/docs` und
`/openapi.json` verfügbar. Alle mutierenden REST-Aufrufe benötigen
`Idempotency-Key`; Antworten führen `X-Correlation-ID`. Wiederverwendung eines
Keys mit anderer Nutzlast ergibt `409 idempotency_conflict`.

```bash
tradingagent config validate config.yaml
tradingagent data datasets
tradingagent data sync --dataset-id DATASET
tradingagent data gaps --dataset-id DATASET
tradingagent backtest run --dataset-id DATASET --configuration-version VERSION
tradingagent backtest report ID
tradingagent paper create --dataset-id DATASET --configuration-version VERSION
tradingagent paper start ID
tradingagent paper pause ID
tradingagent paper resume ID
tradingagent paper stop ID
tradingagent paper status ID
tradingagent db migrate
```

Strategie-, Risiko- und Kostenwerte besitzen bewusst keine fachlichen
Produktionsdefaults. Unbekannte Konfigurationsfelder werden abgelehnt. Ein Lauf
referenziert eine unveränderliche Konfigurationsversion.

## Monitoring und Fehlerbehandlung

`/health/live` zeigt reine Prozesslebendigkeit. `/health/ready` wird nur grün,
wenn Datenbank, Migration und Konfiguration bereit sind; ein ausgefallener
History-Dienst wird als `degraded` ausgewiesen. `/metrics` liefert Prometheus-
Metriken für HTTP-Latenz/-Fehler und Jobs. Fehlerantworten haben das versionierte
Schema `code`, `message`, `correlation_id` und nur sichere Details.

Für Diagnose und Recovery:

```bash
docker compose logs --since=15m api worker scheduler
docker compose restart worker scheduler
docker compose run --rm api db migrate
```

Idempotente Job- und Ledger-Persistenz ist die Voraussetzung für Recovery ohne
doppelte Buchung. Die aktuelle In-Memory-Registry dient nur als
Schnittstellenadapter und ist nicht für produktive Job-Recovery geeignet; vor
Paper-Dauerbetrieb muss der PostgreSQL-Jobadapter aktiviert sein.

## Sicherheitsgrenzen

Der einzige externe Fachzugriff ist read-only auf den konfigurierten
OctoBot-History-Host und dessen dokumentierte GET-Routen. Dataset-IDs oder
Requests können keinen Zielhost wählen. Es gibt weder Börsen-Zugangsdaten noch
Orderendpunkte oder einen Schalter von Paper- auf Echtgeldhandel.

Eine Freigabe über localhost hinaus ist nicht vorgesehen. Dafür wären eine
separate Entscheidung und mindestens TLS, Authentifizierung, Autorisierung und
ein gehärteter Reverse Proxy erforderlich. Vor Releases müssen Dependency- und
Container-Scans sowie die vollständige Test-Suite erfolgreich sein.

## Entwicklung

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy src
```
