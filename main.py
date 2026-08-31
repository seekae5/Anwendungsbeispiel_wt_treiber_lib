
from __future__ import annotations

import logging
from pathlib import Path

from wt_treiber_lib import (
    WT3000,
    WTConfig,
    WTError,
    SampleMark,
    IntegrationMode,
    ThdFormula,
    Quantity,
    RangePlan,
    RangeSpec,
    AutoRangeSpec,
)

from simulation import SimuliertesWT3000

# --- hier anpassen -----------------------------------------------------------

#: True = nachgebildetes Geraet, False = echtes WT3000 ueber die tmctl-DLL.
SIMULATION = False

#: Verbindungsdaten. Steht schon alles in 'wt3000.json', koennen beide auf
#: None bleiben - der Treiber sucht dann in dieser Reihenfolge: Argument,
#: Umgebungsvariable WT3000_IP, Datei 'wt3000.json', Vorgabe.
IP: str | None = "192.168.10.20"
DLL_PFAD: str | None = "tools/tmctl64.dll"

#: Takt der Messschleife in Sekunden. Ist er kleiner als die Update-Rate des
#: Geraets (:RATE), liefert das Geraet denselben Datensatz mehrfach - die
#: Schleife kennzeichnet ihn dann als DUPLICATE.
INTERVALL_S = 1.0

#: Wie viele Datensaetze je Schritt. Ohne Limit laeuft eine Messung ewig.
ANZAHL_KONSOLE = 10
ANZAHL_CSV = 20

#: Schritt 4 - der Zielzustand der Messbereiche. 'scope' ist eine
#: Elementnummer, "SIGMA", "SIGMB" oder "ALL". Welche Stufen das Geraet
#: annimmt, steht in VOLTAGE_RANGES, CURRENT_RANGES und SENSOR_RANGES - je
#: nach Crest-Faktor und Elementtyp; ein Zwischenwert gilt als Fehler.
BEREICHS_PLAN = RangePlan.of(
    RangeSpec(Quantity.VOLTAGE, "ALL", 600.0),      # 600 V fest, alle Elemente
    RangeSpec(Quantity.CURRENT, "SIGMA", 10.0),     # 10 A fest, Element 1-3
    AutoRangeSpec(Quantity.CURRENT, 4, True),       # Element 4 sucht selbst
)

#: Schritt 7 - wie lange der Energiezaehler laufen soll, in Sekunden.
INTEGRATION_S = 10

#: Schritt 8 - welche Ordnungen betrachtet werden und woran die PLL haengt.
#: Ungerade Ordnungen sind die interessanten; die Grundschwingung (1) gehoert
#: als Bezug dazu.
ORDNUNGEN = (1, 3, 5, 7)
PLL_QUELLE = "U1"

#: Der Treiber legt kein Verzeichnis an - das ist Sache des Skripts.
AUSGABE = Path("messungen")

#: True zeigt, was tatsaechlich ueber die Leitung geht (jedes SET, jede
#: Rueckleseprobe) - lehrreich, aber viel Text. Die Zusammenfassung am Ende
#: einer Messreihe ('stats.log_summary') erscheint ebenfalls nur dann.
PROTOKOLL = False


# --- Verbindung --------------------------------------------------------------


def verbindung_oeffnen() -> WT3000:
    """Die Sitzung aufbauen. Rueckgabe gehoert in ein 'with'.

    Beide Schloesser werden geoeffnet, weil Schritt 3 etwas einstellt:

        read_only=False      laesst die Sitzung Set-Kommandos durch
        allow_changes=True   laesst die Fachobjekte (wt.input, ...) schreiben

    Wer nur misst, laesst beide zu - dann kann das Skript am Geraet
    nachweislich nichts verstellen.
    """
    if SIMULATION:
        print("*** SIMULATION - es ist kein WT3000 angeschlossen ***\n")
        return WT3000.from_transport(
            SimuliertesWT3000(),
            WTConfig(use_remote=False),      # kein REMOTE ohne echtes Bedienfeld
            read_only=False,
            allow_changes=True,
            owns_transport=True,             # die Fassade schliesst ihn wieder
        )

    return WT3000.connect(
        ip=IP,
        dll_path=DLL_PFAD,
        read_only=False,
        allow_changes=True,
    )


# --- Schritt 1: Geraet vorstellen --------------------------------------------


def schritt_1_geraet_vorstellen(wt: WT3000) -> None:
    """Steckbrief des Geraets ausgeben. Veraendert nichts."""
    print("=== 1. Geraet ===")
    # Den Steckbrief erhebt der Treiber schon beim Verbinden; describe()
    # gibt ihn Zeile fuer Zeile heraus.
    for zeile in wt.device.describe():
        print(zeile)

    # Die nuetzlichste Frage vor jedem Messaufbau: kann dieses Geraet das
    # ueberhaupt? Fehlt die Option, laeuft das Kommando spaeter in einen
    # Timeout, der wie ein Verbindungsabbruch aussieht.
    print(f"Oberschwingungen moeglich: {wt.device.supports(':HARMonics')}")
    print()


# --- Schritt 2: Eingang ansehen ----------------------------------------------


def schritt_2_eingang_ansehen(wt: WT3000) -> None:
    """Aktuelle Eingangskonfiguration lesen. Veraendert nichts."""
    print("=== 2. Eingang (Ist-Zustand) ===")
    print(f"Verdrahtung:  {wt.input.get_wiring()}")
    print(f"Crest-Faktor: {wt.input.get_crest_factor()}")
    print(f"Update-Rate:  {wt.input.get_update_rate()} s")
    print(f"SIGMA umfasst die Elemente: {wt.ranges.expand_scope('SIGMA')}")
    bereiche_zeigen(wt)
    print()


def bereiche_zeigen(wt: WT3000) -> None:
    """Messbereiche und Auto-Range je Element ausgeben. Reine Leseoperation.

    Wird von Schritt 2 und Schritt 4 benutzt - dort ist es die Probe darauf,
    dass der Plan angekommen und hinterher wieder zurueckgestellt ist.
    """
    spannung = wt.ranges.get_ranges(Quantity.VOLTAGE)
    strom = wt.ranges.get_ranges(Quantity.CURRENT)
    for element in wt.device.elements:
        print(
            f"  Element {element}: "
            f"U {spannung[element].describe(Quantity.VOLTAGE):>12}"
            f"  (auto {str(wt.input.get_voltage_auto(element)):>5})"
            f"   I {strom[element].describe(Quantity.CURRENT):>12}"
            f"  (auto {str(wt.input.get_current_auto(element)):>5})"
        )


# --- Schritt 3: Eingang einstellen -------------------------------------------


def schritt_3_auto_range_einschalten(wt: WT3000) -> dict[int, bool]:
    """Auto-Range fuer die Spannung einschalten - das erste Set-Kommando.

    Der Treiber liest jedes Set-Kommando zurueck und prueft es; kommt der
    Wert nicht an, gibt es eine VerificationError statt einer stillen
    Falschmessung.

    Eine Freigabe braucht es hier NICHT: geschuetzt sind nur die vier
    eingemessenen Gruppen WIRING, RANGE, SCALING und CFACTOR. Auto-Range
    gehoert zur Gruppe AUTO. Waere es eine der vier, stuende hier

        with wt.input.unlocked(GROUP_RANGE):
            ...

    Rueckgabe ist der Ausgangszustand je Element - Schritt 9 stellt ihn
    wieder her.
    """
    print("=== 3. Auto-Range einschalten ===")

    # Erst merken, dann stellen. Wer etwas am Geraet veraendert, sollte es
    # hinterher zuruecknehmen koennen - sonst findet der Naechste einen
    # Aufbau vor, den niemand so eingemessen hat.
    vorher = {element: wt.input.get_voltage_auto(element) for element in wt.device.elements}

    wt.input.set_voltage_auto_range(True, scope="ALL")

    for element in wt.device.elements:
        print(
            f"  Element {element}: Auto-Range {vorher[element]}"
            f" -> {wt.input.get_voltage_auto(element)}"
        )
    print()
    return vorher


# --- Schritt 4: Messbereiche setzen ------------------------------------------


def schritt_4_messbereiche_setzen(wt: WT3000) -> None:
    """Feste Messbereiche nach Plan setzen - und garantiert zurueckstellen.

    Messbereiche gehoeren zum EINGEMESSENEN Zustand des Geraets; die Gruppe
    RANGE liegt deshalb hinter einer eigenen Sperre. 'wt.applied_ranges()'
    braucht diese Freigabe trotzdem nicht, weil es den Rueckweg selbst
    mitbringt: sichern, Schreibprobe, setzen, verifizieren - und im 'finally'
    zurueckstellen, auch bei einem Fehler oder Strg+C mitten in der Messung.

    Der rohe Weg OHNE Rueckweg waere

        with wt.input.unlocked(GROUP_RANGE):
            wt.input.set_voltage_range(600.0, scope="ALL")

    Ein fester Bereich schaltet den Auto-Range derselben Groesse von selbst
    aus - das aus Schritt 3 eingeschaltete Auto also auch. Am Blockende steht
    beides wieder wie vorgefunden.
    """
    print("=== 4. Messbereiche ===")
    print("  Vorgabe:")
    for zeile in BEREICHS_PLAN.describe():
        print(f"    {zeile}")

    with wt.applied_ranges(
        BEREICHS_PLAN, backup_file=AUSGABE / "bereiche_backup.json"
    ) as bericht:
        print(f"  {bericht.commands_written} Kommandos geschrieben - jetzt gilt:")
        bereiche_zeigen(wt)

        # Eine kurze Messreihe mit genau diesen Bereichen. Ohne 'table' wird
        # die Item-Tabelle des Geraets uebernommen.
        wt.measure.record_csv(
            AUSGABE / "mit_bereichen.csv",
            interval_s=INTERVALL_S,
            max_samples=ANZAHL_CSV,
            sidecar=True,
        )


    print("  Nach dem Blockende:")
    bereiche_zeigen(wt)
    print(f"  Abweichungen nach dem Ruecksetzen: {bericht.restore_problems or 'keine'}")
    print(f"  Sicherung: {AUSGABE / 'bereiche_backup.json'}")
    print()





def schritt_5_messwerte_auf_konsole(wt: WT3000) -> None:

    print("=== 5. Messwerte (stream) ===")

    # Die Item-Tabelle sagt, WAS gemessen wird und in welcher Reihenfolge die
    # Werte kommen - der Messwertblock des Geraets traegt keine Namen.
    tabelle = wt.items.read()
    spalten = [item.key for item in tabelle.items]
    print("Spalten:", ", ".join(spalten))

    kopf = f"{'Nr':>3} {'Zeit':>7}  " + "  ".join(f"{name:>10}" for name in spalten)
    print(kopf)

    for sample in wt.measure.stream(
        tabelle,
        interval_s=INTERVALL_S,
        max_samples=ANZAHL_KONSOLE,
        use_hold=True,          # Datensatz im Geraet einfrieren -> in sich stimmig
    ):
        werte = "  ".join(f"{str(wert):>10}" for wert in sample.values)
        zeile = f"{sample.number:>3} {sample.elapsed_s:>7.2f}  {werte}"

        # 'mark' bewertet den ganzen Zyklus (DUPLICATE, MISSING),
        # 'wert.status' den einzelnen Messwert (OVERRANGE, NO_DATA).
        if sample.mark is not SampleMark.OK:
            zeile += f"   [{sample.mark.value}]"
        print(zeile)
    print()


# --- Schritt 6: Messreihe in eine CSV ----------------------------------------


def schritt_6_messreihe_in_csv(wt: WT3000) -> None:
    """Aufzeichnen mit record_csv() - der uebliche Weg fuer eine Messreihe."""
    print("=== 6. Messreihe in CSV ===")
    ziel = AUSGABE / "messreihe.csv"

    stats = wt.measure.record_csv(
        ziel,
        interval_s=INTERVALL_S,     # Takt DIESER Schleife, nicht die Geraeterate
        max_samples=ANZAHL_CSV,     # ohne Limit laeuft die Schleife bis Strg+C
        sidecar=True,               # legt messreihe.csv.meta.json daneben
    )

    # 'sidecar=True' ist die Zeile, die man nicht vergessen sollte: erst damit
    # ist die CSV Wochen spaeter noch interpretierbar - Geraet, Verdrahtung,
    # Item-Tabelle und Laufparameter stehen in der Datei daneben.
    stats.log_summary(INTERVALL_S)      # ausfuehrlich, nur mit PROTOKOLL = True
    print(f"Datei: {ziel}")
    print(f"{stats.measured_samples} echte Messpunkte, {stats.duplicates} Wiederholungen")
    print()


# --- Schritt 7: Energie zaehlen (Integration) --------------------------------


def schritt_7_energie_zaehlen(wt: WT3000) -> None:
    """Wh und Ah ueber eine feste Dauer aufzaehlen.

    Zwei Dinge gehoeren dazu, und sie sind bewusst getrennt:

        wt.integration            STEUERT den Zaehlvorgang - Modus, Timer,
                                  Start, Stopp, Reset
        items.integration_profile()  macht die aufgelaufenen Werte LESBAR;
                                  sie kommen wie alle Messwerte ueber die
                                  Item-Tabelle (TIME, WH, AH, WS, WQ ...)
    """
    print("=== 7. Energie zaehlen (Integration) ===")
    print(f"  Zustand vorher: {wt.integration.state().value}")

    # 'applied' setzt die Tabelle, prueft sie zurueck und stellt am Blockende
    # in JEDEM Fall die vorherige wieder her - auch bei Strg+C.
    #
    # Die Warnung "... Items benutzen Funktionen, die am Original-WT3000 nicht
    # bestaetigt sind" gehoert zu diesem Profil und ist kein Fehler: der
    # Treiber kennzeichnet damit die Integrationsitems, die noch niemand am
    # echten Geraet gegengeprueft hat.
    with wt.items.applied(wt.items.integration_profile()) as tabelle:
        wt.integration.set_mode(IntegrationMode.NORMAL)
        wt.integration.set_timer(hours=0, minutes=0, seconds=INTEGRATION_S)

        # 'running()' startet und stoppt garantiert. Ohne diese Klammer zaehlt
        # das Geraet nach einem Abbruch weiter, ganz ohne PC.
        with wt.integration.running():
            print(f"  Integration laeuft ({INTEGRATION_S} s) ...")
            wt.measure.record_csv(
                AUSGABE / "integration.csv",
                tabelle,
                interval_s=INTERVALL_S,
                max_duration_s=INTEGRATION_S,
                sidecar=True,
            )

        # Der Endstand: ein einzelner Lesevorgang derselben Tabelle.
        # 'read_mapped' liefert die Werte unter denselben Namen, die auch in
        # der CSV-Kopfzeile stehen.
        werte = wt.measure.read_mapped(tabelle)
        for name in ("TIME1", "WH1", "WHSIGMA", "AHSIGMA"):
            print(f"  {name:>10} = {werte[name]}")

    print(f"  Zustand danach: {wt.integration.state().value}")
    # ':INTEGrate:RESet' verwirft den Zaehlerstand unwiderruflich und ist
    # deshalb zusaetzlich gesperrt:
    #     with wt.integration.unlocked(GROUP_RESET):
    #         wt.integration.reset()
    print("  Der Zaehlerstand bleibt am Geraet stehen (reset() ist gesperrt).")
    print(f"  Datei: {AUSGABE / 'integration.csv'}")
    print()


# --- Schritt 8: Oberschwingungen ---------------------------------------------


def schritt_8_oberschwingungen(wt: WT3000) -> None:
    """THD und Einzelordnungen - dasselbe Muster wie bei der Integration.

    'wt.harmonics' stellt die Analyse ein, 'items.harmonics_profile()' macht
    ihr Ergebnis lesbar. Einzelordnungen sind ganz normale Items: 'U,1,5' ist
    die 5. Spannungsoberschwingung an Element 1 und heisst als Spalte 'U1_5'.
    """
    print("=== 8. Oberschwingungen ===")

    # Die Gruppe antwortet nur mit Option /G5 oder /G6. Vorher fragen ist
    # besser als spaeter ein Timeout, der wie ein Verbindungsabbruch aussieht.
    if not wt.device.supports(":HARMonics"):
        print("  Option /G5 oder /G6 fehlt - an diesem Geraet nicht messbar.\n")
        return

    # Momentaufnahme der ganzen Gruppe; ganz unten wird sie zurueckgeschrieben.
    vorher = wt.harmonics.capture()
    for zeile in vorher.describe():
        print(f"  {zeile}")

    try:
        wt.harmonics.set_pll_source(PLL_QUELLE)      # woran haengt die PLL?
        wt.harmonics.set_order_range(1, max(ORDNUNGEN))
        wt.harmonics.set_thd_formula(ThdFormula.TOTAL)

        profil = wt.items.harmonics_profile(orders=ORDNUNGEN, elements=("1",))
        with wt.items.applied(profil) as tabelle:
            werte = wt.measure.read_mapped(tabelle)

            print(f"  Klirrfaktor Element 1:  UTHD={werte['UTHD1']}  ITHD={werte['ITHD1']}")
            print(f"  {'Ordnung':>8} {'U [V]':>12} {'I [A]':>12} {'P [W]':>12}")
            for ordnung in ORDNUNGEN:
                print(
                    f"  {ordnung:>8}"
                    f" {str(werte[f'U1_{ordnung}']):>12}"
                    f" {str(werte[f'I1_{ordnung}']):>12}"
                    f" {str(werte[f'P1_{ordnung}']):>12}"
                )
    finally:
        # Auch hier gilt: hinterlassen, wie vorgefunden.
        wt.harmonics.restore(vorher)
    print()


# --- Schritt 9: zuruecksetzen ------------------------------------------------


def schritt_9_zuruecksetzen(wt: WT3000, vorher: dict[int, bool]) -> None:
    """Den in Schritt 3 veraenderten Zustand wiederherstellen.

    Fuer den GANZEN Geraetezustand gibt es den bequemeren Weg
    'wt.backup(pfad)' / 'wt.restore_backup(pfad)'. Hier ist genau eine
    Einstellung veraendert worden, also wird auch genau die zurueckgestellt.
    """
    print("=== 9. Zuruecksetzen ===")
    for element, zustand in vorher.items():
        wt.input.set_voltage_auto_range(zustand, scope=element)
    print(f"  Auto-Range wieder: {vorher}")
    print()


# --- Ablauf ------------------------------------------------------------------


def main() -> int:
    """Rueckgabe 0 = erfolgreich."""
    logging.basicConfig(
        level=logging.INFO if PROTOKOLL else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    AUSGABE.mkdir(parents=True, exist_ok=True)

    try:
        with verbindung_oeffnen() as wt:
            # Stellt ':COMMunicate:HEADer 0', ':VERBose 0' und
            # ':NUMeric:FORMat FLOat' her - ohne die scheitert das Auslesen -
            # und nimmt das am Blockende zurueck. Der Treiber macht das
            # absichtlich nicht von selbst: ein Messaufruf, der unangekuendigt
            # am Geraetezustand dreht, waere das Gegenteil der zwei Schloesser.
            with wt.ensured_protocol_state():
                schritt_1_geraet_vorstellen(wt)
                schritt_2_eingang_ansehen(wt)

                vorher = schritt_3_auto_range_einschalten(wt)
                try:
                    schritt_4_messbereiche_setzen(wt)
                    schritt_5_messwerte_auf_konsole(wt)
                    schritt_6_messreihe_in_csv(wt)
                    schritt_7_energie_zaehlen(wt)
                    schritt_8_oberschwingungen(wt)
                finally:
                    # 'finally', damit auch ein Fehler oder Strg+C mitten in
                    # der Messung das Geraet nicht veraendert zuruecklaesst.
                    schritt_9_zuruecksetzen(wt, vorher)

    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        return 1
    except WTError as fehler:
        # Die Meldungen dieses Treibers nennen den Ausweg, nicht nur das
        # Problem - deshalb ausgeben statt verschlucken.
        print(f"Fehler: {fehler}")
        return 1

    print("Fertig.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
