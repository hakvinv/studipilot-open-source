# StudIPilot

StudIPilot verbindet Studienunterlagen, Uni-Mail, Kalender und Aufgaben über einen MCP-Zugang mit KI-Assistenten. Der Code stammt aus einem persönlichen Projekt für Stud.IP an der Universität Bremen und wird als Ausgangspunkt für eigene Installationen veröffentlicht.

## Bestandteile

- `railway-studip-sync/`: inkrementeller Stud.IP-Dateiabruf und Dokumentindex.
- `remote-rag/`: Suche in Dokumenten und Abruf von Originalseiten.
- `uni-gateway/`: gemeinsamer MCP-Endpunkt mit OAuth und Scopes.
- `uni-mail/`: Mailzugriff, Entwürfe und Aufgabenstatus.
- `uni-pilot/`: Zusammenstellung von Briefing-Daten.
- `uni-rag/`: lokale Oberfläche und Embedding-Adapter.
- `skills/uni-kontext/`: lokaler Skill für die quellenbasierte Suche.

Das Projekt ist eine Entwicklungsversion, kein vollständig automatisiertes Installationspaket. Einige Komponenten setzen Stud.IP, OneDrive/rclone, Railway, einen Mailserver oder externe Modellanbieter voraus. Der Mailtransport ist auf die Universität Bremen zugeschnitten. Dienstadressen, Zugangsdaten und Nutzerdateien müssen für eine eigene Installation eingerichtet werden. Die private App-Bindung ist nicht enthalten.

## Tests

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r remote-rag/requirements.txt -r uni-gateway/requirements.txt
python scripts/check_repository.py
python scripts/check.py
```

Die Tests verwenden künstliche Daten. Für die PDF-Tests ist `pdftotext` aus Poppler erforderlich. `uni-pilot/collect.py` benötigt für den Remote-Zugriff die HTTPS-Ursprünge `UNI_MAIL_MCP_ORIGIN` und `UNI_RAG_MCP_ORIGIN`; Zugangsdaten gehören ausschließlich in die lokale Konfiguration oder einen Secret Store.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
