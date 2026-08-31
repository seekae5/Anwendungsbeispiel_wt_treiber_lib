# =============================================================================
# Simuliertes WT3000 - damit main.py auch ohne Geraet laeuft
#
# Auf macOS gibt es keine 'tmctl64.dll', und im Labor steht das WT3000 nicht
# immer bereit. Der Treiber sieht dafuer 'FakeTransport' vor: eine Tabelle
# 'Kommando -> Antwort', die sich wie ein Geraet verhaelt. Eine nicht
# hinterlegte Abfrage wird BEWUSST nicht erfunden, sondern meldet einen
# KeyError - so faellt auf, was das Skript wirklich abfragt.
#
# Diese Datei ist reines Beiwerk zum Ausprobieren. Wer am realen Geraet
# arbeitet, braucht sie nicht: dort steht in main.py SIMULATION = False.
# =============================================================================

from __future__ import annotations

import math
import struct

from wt_treiber_lib import FakeTransport

# --- so tut das simulierte Geraet ---------------------------------------------

IDENTITAET = "YOKOGAWA,WT3000,C1B234567,F2.11"
OPTIONEN = "G6,B5,DT,C7,C5,CC"     # /G6 = Oberschwingungen, /DT = Delta-Rechnung
VERDRAHTUNG = "V3A3,P1W2"          # Element 1-3 als Drehstrom, Element 4 einzeln
MODULE = "30,30,30,30"             # vier 30-A-Elemente
ELEMENTE = (1, 2, 3, 4)

#: Welche Elemente hinter einem Sammelziel stehen. Folge der Verdrahtung
#: oben: Element 1-3 bilden die Unit SIGMA, Element 4 die Unit SIGMB.
#: Ein Kommando an ':...:SIGMA' wirkt also auf drei Elemente, zurueckgelesen
#: wird aber immer einzeln - ':...:SIGMA?' gibt es nicht.
SAMMELZIELE = {
    ":ALL": ELEMENTE,
    ":SIGMA": (1, 2, 3),
    ":SIGMB": (4,),
}

#: Item-Tabelle, die das simulierte Geraet beim Start "eingestellt" hat.
#: Format wie das Geraet: 'FUNKTION,ELEMENT[,ORDNUNG]'.
START_ITEMS = {
    1: "U,1",
    2: "I,1",
    3: "P,1",
    4: "U,2",
    5: "I,2",
    6: "P,2",
    7: "P,SIGMA",
    8: "LAMBDA,SIGMA",
}

#: Ungefaehre Messwerte je Messfunktion - nur damit die Ausgabe plausibel
#: aussieht. Um sie schwankt der simulierte Wert ein wenig.
TYPISCHE_WERTE = {
    "U": 230.0,
    "I": 4.98,
    "P": 1145.0,
    "S": 1150.0,
    "Q": 105.0,
    "LAMBDA": 0.97,
    "PHI": 5.2,
    "FU": 50.0,
    "FI": 50.0,
}

#: Integrationsgroessen - Zaehlerstaende, die im Lauf anwachsen. WH =
#: Wattstunden, AH = Amperestunden, WS = Scheinleistungs-, WQ =
#: Blindleistungsstunden; das nachgestellte P/M trennt Bezug und Rueckspeisung.
#: Angegeben ist der Zuwachs je Sekunde Laufzeit.
INTEGRATIONS_WERTE = {
    "WH": 1145.0 / 3600.0,
    "WHP": 1145.0 / 3600.0,
    "WHM": 0.0,
    "AH": 4.98 / 3600.0,
    "AHP": 4.98 / 3600.0,
    "AHM": 0.0,
    "WS": 1150.0 / 3600.0,
    "WQ": 105.0 / 3600.0,
}

#: Summengroessen der Oberschwingungsanalyse - ein Antrieb am Umrichter mit
#: maessig verzerrtem Strom.
OBERSCHWINGUNGS_WERTE = {
    "UTHD": 2.8,
    "ITHD": 14.5,
    "PTHD": 1.2,
    "UTHF": 0.9,
    "ITHF": 3.4,
    "UTIF": 55.0,
    "ITIF": 120.0,
    "HVF": 0.6,
    "HCF": 2.1,
}

#: Summengroessen (Element 'SIGMA') sind bei drei Phasen etwa dreimal so
#: gross - ausser den Verhaeltnisgroessen, die bleiben, wie sie sind.
SUMMEN_FAKTOR = 3.0
OHNE_SUMMEN_FAKTOR = {"LAMBDA", "PHI", "FU", "FI", "U", "I"}


def _grundwert(funktion: str, element: str, ordnung: str, laufzeit_s: int) -> float:
    """Plausibler Wert fuer eine Messgroesse - reine Kulisse, keine Physik."""
    # TIME und die Zaehlerstaende der Integration wachsen mit der Laufzeit des
    # Zaehlvorgangs; ohne laufende Integration stehen sie auf null.
    if funktion == "TIME":
        return float(laufzeit_s)
    if funktion in INTEGRATIONS_WERTE:
        wert = INTEGRATIONS_WERTE[funktion] * laufzeit_s
        return wert * SUMMEN_FAKTOR if element.startswith("SIGM") else wert

    # Die Summengroessen der Oberschwingungsanalyse tragen keine Ordnung.
    if funktion in OBERSCHWINGUNGS_WERTE:
        return OBERSCHWINGUNGS_WERTE[funktion]

    grundwert = TYPISCHE_WERTE.get(funktion, 0.0)
    if element.startswith("SIGM") and funktion not in OHNE_SUMMEN_FAKTOR:
        grundwert *= SUMMEN_FAKTOR
    # Einzelordnungen fallen mit steigender Ordnung ab - die 5. Oberschwingung
    # liegt hier bei rund 4 % der Grundschwingung.
    if ordnung.isdigit() and int(ordnung) > 1:
        grundwert /= float(ordnung) ** 2
    return grundwert


def _messwert_block(
    items: dict[int, str], anzahl: int, zyklus: int, laufzeit_s: int
) -> bytes:
    """Die Antwort auf ':NUMeric:NORMal:VALue?' bauen.

    Das Geraet liefert die Werte als Binaerblock '#4NNNN' mit je vier Byte
    IEEE-Single, MSB zuerst - und zwar POSITIONSBEZOGEN in der Reihenfolge
    der Item-Tabelle. Genau deshalb ist diese Reihenfolge die wichtigste
    Information der Tabelle.
    """
    werte = []
    for index in range(1, anzahl + 1):
        teile = items.get(index, "NONE").split(",")
        funktion = teile[0].upper()
        element = teile[1].upper() if len(teile) > 1 else ""
        ordnung = teile[2].upper() if len(teile) > 2 else ""

        grundwert = _grundwert(funktion, element, ordnung, laufzeit_s)
        # Eine langsame Schwingung von rund +/- 2 %, damit aufeinander
        # folgende Zyklen sich unterscheiden (sonst meldet die Messschleife
        # zu Recht lauter DUPLICATE).
        werte.append(grundwert * (1.0 + 0.02 * math.sin(zyklus / 3.0 + index)))

    nutzlast = b"".join(struct.pack(">f", wert) for wert in werte)
    return f"#4{len(nutzlast):04d}".encode("ascii") + nutzlast


def _grundantworten() -> dict[str, str]:
    """Alles, was Fassade, Geraetesteckbrief und Eingangsabfragen brauchen."""
    antworten = {
        # Identitaet und Ausstattung - liest 'wt.device' beim Verbinden.
        "*IDN": IDENTITAET,
        "*OPT": OPTIONEN,
        # Protokollzustand - ohne diese drei scheitert das Auslesen.
        ":COMMUNICATE": "0,0,0",
        ":COMMUNICATE:HEADER": "0",
        ":COMMUNICATE:VERBOSE": "0",
        ":NUMERIC:FORMAT": "FLOat",
        # Aufbau und Zustand des Eingangs.
        ":INPUT:WIRING": VERDRAHTUNG,
        ":INPUT:MODULE": MODULE,
        ":INPUT:INDEPENDENT": "1",
        ":INPUT:CFACTOR": "3",
        ":INPUT:SCALING": "0,0,0,0",
        ":INPUT:FILTER": "OFF,OFF,OFF,OFF",
        ":INPUT": "ELEMENT1,1000V;ELEMENT2,1000V;ELEMENT3,1000V;ELEMENT4,1000V",
        ":RATE": "1.000E+00",
        ":MEASURE": "NORMAL",
        # Zustand waehrend der Messung.
        ":STATUS:CONDITION": "0",
        ":NUMERIC:HOLD": "0",
        # Integration (Wh/Ah). 'RES' ist die Kurzform von RESET, mit der das
        # reale Geraet antwortet - der Treiber kennt beide.
        ":INTEGRATE:MODE": "NORM",
        ":INTEGRATE:STATE": "RES",
        ":INTEGRATE:TIMER": "0,0,0",
        ":INTEGRATE:ACAL": "0",
        ":INTEGRATE:RTIME:START": "2006,1,1,0,0,0",
        ":INTEGRATE:RTIME:END": "2006,1,1,1,0,0",
        # Oberschwingungsanalyse (Option /G6). Werte des Handbuchbeispiels.
        ":HARMONICS:FBAND": "NORMAL",
        ":HARMONICS:ORDER": "1,100",
        ":HARMONICS:PLLSOURCE": "U1",
        ":HARMONICS:PLLWARNING:STATE": "1",
        ":HARMONICS:THD": "TOTAL",
        ":HARMONICS:IEC:OBJECT": "ELEMENT1",
        ":HARMONICS:IEC:UGROUPING": "OFF",
        ":HARMONICS:IEC:IGROUPING": "OFF",
    }
    for element in ELEMENTE:
        antworten[f":INPUT:VOLTAGE:RANGE:ELEMENT{element}"] = "1.000E+03"
        antworten[f":INPUT:VOLTAGE:AUTO:ELEMENT{element}"] = "0"
        antworten[f":INPUT:VOLTAGE:MODE:ELEMENT{element}"] = "RMS"
        antworten[f":INPUT:CURRENT:RANGE:ELEMENT{element}"] = "5.00E+00"
        antworten[f":INPUT:CURRENT:AUTO:ELEMENT{element}"] = "0"
        antworten[f":INPUT:CURRENT:MODE:ELEMENT{element}"] = "RMS"
        antworten[f":INPUT:FILTER:LINE:ELEMENT{element}"] = "OFF"
        antworten[f":INPUT:FILTER:FREQUENCY:ELEMENT{element}"] = "0"
        antworten[f":INPUT:SCALING:STATE:ELEMENT{element}"] = "0"
        antworten[f":INPUT:SCALING:VT:ELEMENT{element}"] = "1.0000E+00"
        antworten[f":INPUT:SCALING:CT:ELEMENT{element}"] = "1.0000E+00"
        antworten[f":INPUT:SCALING:SFACTOR:ELEMENT{element}"] = "1.0000E+00"
        antworten[f":INPUT:SCALING:SRATIO:ELEMENT{element}"] = "1.0000E+00"
        antworten[f":INPUT:SYNCHRONIZE:ELEMENT{element}"] = "U{0}".format(element)
    return antworten


class SimuliertesWT3000(FakeTransport):
    """FakeTransport, der gesetzte Werte uebernimmt statt sie zu ignorieren.

    Der Treiber liest jedes Set-Kommando zurueck und prueft es ('Rueckleseprobe').
    Ein Transport, der nur eine feste Tabelle beantwortet, laesst deshalb jedes
    'set_...' scheitern. Hier wandert das gesendete Argument in die
    Antworttabelle - das simulierte Geraet 'merkt' sich also, was man ihm sagt.
    """

    def __init__(self) -> None:
        self.items = dict(START_ITEMS)
        self.anzahl_items = len(START_ITEMS)
        #: Zaehlt die Messzyklen und dient als grobe Uhr (ein Zyklus ~ 1 s).
        self.zyklus = 0
        #: Zyklus, in dem die Integration gestartet bzw. gestoppt wurde.
        self.integration_start: int | None = None
        self.integration_ende: int | None = None

        antworten = _grundantworten()
        # Item-Tabelle und Messwerte werden bei jeder Abfrage neu berechnet,
        # stehen also als Funktion und nicht als fester Text in der Tabelle.
        antworten[":NUMERIC:NORMAL"] = lambda _cmd: self._tabellen_antwort()
        antworten[":NUMERIC:NORMAL:NUMBER"] = lambda _cmd: str(self.anzahl_items)
        antworten[":NUMERIC:NORMAL:VALUE"] = lambda _cmd: self._werte_antwort()
        for index in range(1, 256):     # das Geraet fuehrt bis zu 255 Items
            antworten[f":NUMERIC:NORMAL:ITEM{index}"] = self._item_antwort(index)
        super().__init__(antworten)

    # -- die drei berechneten Antworten ------------------------------------

    def _item_antwort(self, index: int):
        return lambda _cmd: self.items.get(index, "NONE")

    def _tabellen_antwort(self) -> str:
        """':NUMeric:NORMal?' -> '<Anzahl>;<Item1>;<Item2>;...'"""
        teile = [str(self.anzahl_items)]
        teile += [self.items.get(i, "NONE") for i in range(1, self.anzahl_items + 1)]
        return ";".join(teile)

    def _werte_antwort(self) -> bytes:
        self.zyklus += 1
        return _messwert_block(
            self.items, self.anzahl_items, self.zyklus, self._laufzeit_s()
        )

    def _laufzeit_s(self) -> int:
        """Wie lange die Integration gezaehlt hat - nach dem Stopp eingefroren."""
        if self.integration_start is None:
            return 0
        ende = self.zyklus if self.integration_ende is None else self.integration_ende
        return max(0, ende - self.integration_start)

    # -- Set-Kommandos uebernehmen -----------------------------------------

    #: Kommandos ohne Argument, die den Integrationszustand umschalten.
    ZUSTANDS_KOMMANDOS = {
        ":INTEGRATE:START": "START",
        ":INTEGRATE:STOP": "STOP",
        ":INTEGRATE:RESET": "RES",
    }

    def write(self, command: str) -> None:
        super().write(command)
        knoten, _, argument = command.strip().partition(" ")
        knoten = knoten.upper()

        if knoten in self.ZUSTANDS_KOMMANDOS:
            self.responses[":INTEGRATE:STATE"] = self.ZUSTANDS_KOMMANDOS[knoten]
            if knoten == ":INTEGRATE:START":
                self.integration_start = self.zyklus
                self.integration_ende = None
            elif knoten == ":INTEGRATE:STOP":
                self.integration_ende = self.zyklus
            else:                                   # RESET verwirft den Stand
                self.integration_start = None
                self.integration_ende = None
            return

        if not argument:
            return                                  # sonstiges Kommando ohne Wert
        argument = argument.strip()

        if knoten == ":NUMERIC:NORMAL:NUMBER":
            self.anzahl_items = int(argument)
            return
        if knoten.startswith(":NUMERIC:NORMAL:ITEM"):
            self.items[int(knoten.removeprefix(":NUMERIC:NORMAL:ITEM"))] = argument
            return

        # Ein Sammelziel spricht mehrere Elemente an, wird aber einzeln
        # zurueckgelesen - also auch einzeln ablegen.
        for ziel, elemente in SAMMELZIELE.items():
            if knoten.endswith(ziel):
                stamm = knoten.removesuffix(ziel)
                for element in elemente:
                    self.responses[f"{stamm}:ELEMENT{element}"] = argument
                return

        self.responses[knoten] = argument
