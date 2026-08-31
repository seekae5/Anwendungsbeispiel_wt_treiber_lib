# WT3000 Verbindungsfehler: `TmcInitialize` schlägt fehl (Code 0x1)

## Symptom

Beim Start von `main.py` (mit `SIMULATION = False`):

```
Fehler: TmcInitialize fehlgeschlagen, TMCTL-Fehlercode 0x00000001 (Adresse=192.168.10.20)

Process finished with exit code 1
```

## Ursache

Es ist **kein** Netzwerk- oder Treiberfehler. Das Gerät weist die **Anmeldung**
ab, weil kein Benutzername/Passwort übergeben wird.

Schrittweise nachgewiesen:

| Verdacht | Prüfung | Ergebnis |
|---|---|---|
| Netzwerk nicht erreichbar | `ping 192.168.10.20`, Port 10001 | erreichbar, Port offen |
| Falsche DLL / Bitness | PE-Header von `tmctl64.dll` | 64-Bit (x64), passt zu 64-Bit-Python; alle Funktionen exportiert |
| Treiberfehler in `wt_treiber_lib` | Handshake mit rohem TCP-Socket nachgestellt | **identisches Verhalten** → nicht der Treiber |
| Anmeldung wird abgewiesen | Gerät sendet `username:`, schließt die Verbindung **direkt nach Empfang des Logins** | **Ursache bestätigt** |

Das Gerät spricht auf Port 10001 ein längengerahmtes Protokoll
(`0x80` + 3 Byte Länge + Nutzlast), schickt zuerst `username:` und trennt die
Verbindung sofort, sobald ein nicht registrierter Login ankommt. TMCTL meldet
diese Abweisung als Rückgabecode `0x1`.

### Warum der Login leer war

`main.py` ruft auf:

```python
WT3000.connect(ip=IP, dll_path=DLL_PFAD, read_only=False, allow_changes=True)
```

`connect()` besitzt **keine** Parameter für `user`/`password`. Diese beiden Felder
holt der Treiber ausschließlich aus:

1. Umgebungsvariablen `WT3000_USER` / `WT3000_PASSWORD`, **oder**
2. einer Datei `wt3000.json` im Arbeitsverzeichnis.

Beides fehlte im Projekt → `user` und `password` gingen als leere Zeichenketten
raus → das Gerät weist die Anmeldung ab.

Die mitgelieferte Vorlage
`.venv/Lib/site-packages/wt_treiber_lib/resources/wt3000.json` dokumentiert das
Konto dieses Geräts:

```json
{ "user": "TEST", "password": "1", ... }
```

Mit diesen Zugangsdaten verbindet das Gerät sofort und antwortet auf `*IDN?`:

```
YOKOGAWA,760304-40-MV,0,F5.01
```

## Fix

Eine Datei `wt3000.json` im Projektwurzelverzeichnis (neben `main.py`) anlegen:

```json
{
  "ip": "192.168.10.20",
  "user": "TEST",
  "password": "1",
  "dll_path": "tools/tmctl64.dll",
  "use_remote": true
}
```

Der Treiber liest sie über `WTConfig.from_environment()`. Damit werden `IP` und
`DLL_PFAD` in `main.py` überflüssig (die Datei liefert sie); explizite
`connect(ip=...)`-Argumente haben aber weiterhin Vorrang und schaden nicht.

### Alternative ohne Datei

Statt der Datei die Zugangsdaten als Umgebungsvariablen setzen (haben Vorrang vor
der Datei):

```powershell
$env:WT3000_USER = "TEST"
$env:WT3000_PASSWORD = "1"
```

## Sicherheitshinweis

`wt3000.json` enthält das Gerätepasswort im Klartext. Steht das Projekt unter
Versionskontrolle, die Datei in `.gitignore` aufnehmen. Wer kein Passwort in eine
Datei schreiben will, nutzt die Umgebungsvariablen-Variante.
