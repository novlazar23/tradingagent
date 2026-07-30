# tradingagent

Eigenständiger BTC/USDT-Dienst für reproduzierbare Backtests und Paper-Trading
mit Chartmustern und technischen Indikatoren. Das System kann keine echten
Börsenorders senden.

## Betrieb mit Docker Compose

Voraussetzungen: Docker Engine mit Compose v2, erreichbarer OctoBot-History-
Service und ein lokales Secret. Die Anwendungs-API wird ausschließlich an
`127.0.0.1` veröffentlicht; PostgreSQL besitzt keinen Host-Port.

```bash
sudo install -d -m 0700 /opt/docker/stacks/tradingagent/secrets
sudo install -m 0400 /dev/null /opt/docker/stacks/tradingagent/secrets/postgres_password
sudo sh -c "printf '%s' 'ein-langes-lokales-passwort' > /opt/docker/stacks/tradingagent/secrets/postgres_password"
export POSTGRES_PASSWORD_FILE=/opt/docker/stacks/tradingagent/secrets/postgres_password
export OCTOBOT_HISTORY_API_KEY_FILE=/opt/docker/stacks/octobot/secrets/history_api_key
docker compose up --build -d
docker compose ps
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

API, Worker und Scheduler besitzen kein direktes Egress-Netz. Historienabrufe
laufen ausschließlich über einen Read-only-Proxy, der nur die drei benötigten
GET-Routen (`/health`, Datensätze und Kerzen) an den festen Upstream
`192.168.178.20:5002` weiterleitet. Diese
Grenze verhindert beliebige ausgehende Verbindungen der Anwendung, setzt aber
weiterhin voraus, dass der konfigurierte OctoBot-Dienst und das lokale LAN
vertrauenswürdig sind. Für eine nicht vertrauenswürdige Netzstrecke muss der
History-Dienst zusätzlich TLS mit verifizierter Serveridentität anbieten.

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
tradingagent bootstrap --external-dataset-id OCTOBOT_DATASET \
  --start 2026-01-01T00:00:00Z --end 2026-07-01T00:00:00Z
tradingagent config validate config.yaml
tradingagent data source-datasets
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

Im Compose-Betrieb wird die CLI über den Worker ausgeführt, weil nur dieser
Dienst Zugriff auf das OctoBot-Secret besitzt:

```bash
docker compose run --rm worker data source-datasets
docker compose run --rm worker bootstrap \
  --external-dataset-id ExchangeHistoryDataCollector_<id>.data \
  --start 2026-01-01T00:00:00Z \
  --end 2026-07-01T00:00:00Z
```

Die JSON-Ausgabe enthält `dataset_id` und `configuration_version`. Beide Werte
werden anschließend explizit an Datenimport, Backtest und Paper-Session
übergeben:

```bash
docker compose run --rm worker data sync --dataset-id DATASET_ID
docker compose run --rm worker backtest run \
  --dataset-id DATASET_ID --configuration-version CONFIGURATION_VERSION
docker compose run --rm worker backtest report BACKTEST_ID
docker compose run --rm worker paper create \
  --dataset-id DATASET_ID --configuration-version CONFIGURATION_VERSION
docker compose run --rm worker paper start PAPER_SESSION_ID
docker compose run --rm worker paper status PAPER_SESSION_ID
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

Idempotente PostgreSQL-Job- und Ledger-Persistenz ermöglicht Recovery ohne
doppelte Buchung. SQLite wird ausschließlich ohne konfigurierte Datenbank für
lokale CLI- und Unit-Test-Ausführung verwendet; Compose nutzt PostgreSQL.

## Sicherheitsgrenzen

Der einzige externe Fachzugriff ist read-only auf den konfigurierten
OctoBot-History-Host und dessen dokumentierte GET-Routen. Dataset-IDs oder
Requests können keinen Zielhost wählen. Es gibt weder Börsen-Zugangsdaten noch
Orderendpunkte oder einen Schalter von Paper- auf Echtgeldhandel.

Eine Freigabe über localhost hinaus ist nicht vorgesehen. Dafür wären eine
separate Entscheidung und mindestens TLS, Authentifizierung, Autorisierung und
ein gehärteter Reverse Proxy erforderlich. Vor Releases müssen Dependency- und
Container-Scans sowie die vollständige Test-Suite erfolgreich sein.

## Video-Strategien mit Whisper und Freqtrade

Video-Strategien werden nicht direkt in ausführbaren Python-Code übersetzt.
Zuerst entsteht ein Transkript mit dem lokalen Whisper-Dienst, danach ein
versioniertes Strategie-DSL. Nur freigegebene DSL-Dokumente dürfen in eine
Freqtrade-Strategie kompiliert werden.

Whisper und Freqtrade werden als optionale Compose-Profile betrieben:

```bash
docker compose -f compose.yaml -f compose.strategy.yaml --profile transcription up -d whisper
docker compose -f compose.yaml -f compose.strategy.yaml run --rm worker strategy transcribe \
  --url 'https://www.youtube.com/watch?v=VIDEO_ID' \
  --output artifacts/transcripts/video.json
docker compose -f compose.yaml -f compose.strategy.yaml run --rm worker strategy extract \
  --transcript artifacts/transcripts/video.json \
  --name rsi_video \
  --output artifacts/strategies/rsi_video.strategy.json
```

Das Whisper-Modell ist standardmäßig `small` und kann über `--model` oder
`WHISPER_DEVICE`/`WHISPER_COMPUTE_TYPE` angepasst werden. Der Dienst akzeptiert
ausschließlich HTTPS-URLs von YouTube.

Eine freigegebene DSL-Datei wird kompiliert:

```bash
docker compose -f compose.yaml -f compose.strategy.yaml run --rm worker strategy compile \
  --spec artifacts/strategies/rsi_breakout.json \
  --output artifacts/strategies/RsiBreakoutStrategy.py
```

Die Extraktion erzeugt absichtlich den Status `draft`. Vor dem Kompilieren muss
die Datei geprüft und auf `"status": "approved"` gesetzt werden.

OctoBot-Daten werden im Freqtrade-JSON-Format unter
`artifacts/freqtrade_data/BTC_USDT-<timeframe>.json` abgelegt. Der Backtest läuft
isoliert und ohne Börsenschlüssel:

```bash
docker compose -f compose.yaml -f compose.strategy.yaml --profile backtesting run --rm \
  -e FREQTRADE_STRATEGY=RsiBreakoutStrategy freqtrade-backtest
```

Vor der Paper-Freigabe müssen Backtest, Out-of-sample-Test, Lookahead-Analysis
und Recursive-Analysis geprüft werden. Ein erfolgreiches Backtest-Ergebnis ist
keine Zusage für zukünftige Rendite.

## Entwicklung

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy src
```
