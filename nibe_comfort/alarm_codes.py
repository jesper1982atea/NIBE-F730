"""Plain-language names for NIBE alarm numbers (F series), so a bare code is never all the user sees.

Compiled from NIBE's alarm lists as summarised by varmepumpsdoktorn.se/felkoder/nibe. Codes that mean
different things in the S series use the F-series meaning. The pump display has the final word.
"""

# code: (name, meaning, what to do, severity låg/medel/hög)
ALARMS = {
    40: ("Kompressorfas 1 saknas", "En av kompressorns faser saknas eller har för låg spänning. Kompressorn är blockerad.",
         "Kontrollera husets säkringar och jordfelsbrytare. Återställ först när matningen är kontrollerad.", "hög"),
    41: ("Kompressorfas 2 saknas", "En av kompressorns faser saknas eller har för låg spänning. Kompressorn är blockerad.",
         "Kontrollera husets säkringar och jordfelsbrytare. Återställ först när matningen är kontrollerad.", "hög"),
    42: ("Kompressorfas 3 saknas", "En av kompressorns faser saknas eller har för låg spänning. Kompressorn är blockerad.",
         "Kontrollera husets säkringar och jordfelsbrytare. Återställ först när matningen är kontrollerad.", "hög"),
    43: ("Fel fasföljd", "Faserna är inkopplade i fel ordning.", "Låt en behörig elektriker rätta det.", "medel"),
    50: ("Högtryckslarm", "Trycket i köldmediekretsen blev för högt och kompressorn stoppades.",
         "Oftast ett flödesproblem: kontrollera att radiatorventiler är öppna och systemtrycket. Kontakta service om det återkommer.", "hög"),
    51: ("Lågtryckslarm", "Trycket i köldmediekretsen blev för lågt och kompressorn stoppades.",
         "Kontakta service om larmet återkommer efter en återställning.", "hög"),
    52: ("Temperaturbegränsare", "Elpatronens temperaturbegränsare har löst ut.",
         "Begränsaren återställs med en knapp inne i pumpen; att kvittera larmet räcker inte. Kontakta service vid osäkerhet.", "medel"),
    55: ("Hetgaslarm", "Hetgasen från kompressorn blev för varm och kompressorn stoppades.",
         "Återställ en gång och notera om det hände vid värme eller varmvatten. Återkommer det behövs kyltekniker.", "hög"),
    60: ("Låg köldbärartemperatur", "Köldbäraren ut är kallare än tillåtet och kompressorn är stoppad.",
         "Kontakta service om larmet återkommer.", "medel"),
    63: ("Lågt luftflöde", "Luftflödet genom pumpen är för lågt och kompressorn är blockerad.",
         "Rengör luftfiltret, kontrollera frånluftsdonen i kök och våtrum och återställ sedan.", "medel"),
    64: ("Låg frånluftstemperatur", "Frånluften var under 16 °C för länge, så kompressorn stoppas tillfälligt.",
         "Höj innetemperaturen och kontrollera att inga kanaler eller don drar in kall uteluft.", "medel"),
    71: ("Kommunikationsfel med grundkortet", "Styrningen har tappat kontakten med grundkortet och kompressorn är blockerad.",
         "Kontakta service om det inte går över av sig självt.", "medel"),
    83: ("Misslyckad avfrostning", "Avfrostningen av förångaren blev inte klar i tid.",
         "Kontrollera om förångaren är kraftigt nedisad och återställ en gång. Hacka inte bort is för hand.", "medel"),
    162: ("Hög kondensor ut-temperatur", "Vattnet från kondensorn är varmare än tillåtet. Larmet återställs vid nästa kompressorstart.",
          "Vanliga orsaker är för hög värmekurva eller förskjutning, för lågt flöde eller för hög stopptemperatur för varmvatten.", "medel"),
    182: ("Effektvakt aktiverad", "Effektvakten begränsar elpatronen tillfälligt eftersom strömmen är nära huvudsäkringen.",
          "Ingen åtgärd behövs. Undvik att köra flera stora förbrukare samtidigt.", "låg"),
    183: ("Avfrostning pågår", "Pumpen avfrostar förångaren.", "Ingen åtgärd behövs; det tar normalt några minuter.", "låg"),
    184: ("Filterlarm", "Påminnelse om att luftfiltret behöver rengöras.",
          "Rengör frånluftsfiltret enligt handboken och kvittera larmet på pumpen.", "låg"),
    220: ("Högtryckslarm från utedelen", "Utedelen rapporterar för högt tryck och kompressorn är blockerad.",
          "Kontrollera öppna ventiler, systemtryck och smutsfilter, återställ en gång och håll koll.", "hög"),
    221: ("Lågtryckslarm från utedelen", "Utedelen rapporterar för lågt köldmedietryck.",
          "Kontrollera att utedelen är fri från snö, is och löv, återställ en gång och håll koll.", "hög"),
    223: ("Kommunikationsfel med utedelen", "Innedelen har tappat kontakten med utedelen och kompressorn stannar.",
          "Bryt strömmen till hela systemet en minut och starta igen.", "medel"),
    224: ("Fläktfel i utedelen", "Utedelens fläkt står still eller rapporterar fel.",
          "Kontrollera med strömmen bruten om fläkten är blockerad av is eller skräp och återställ en gång.", "medel"),
    229: ("Korta drifttider på kompressorn", "Kompressorn har stannat kort efter start tre gånger i rad och är blockerad.",
          "Kontrollera flödet (öppna ventiler, systemtryck, smutsfilter), återställ en gång och notera hur länge kompressorn går.", "medel"),
    233: ("Avfrostning pågår i frånluftsmodulen", "Driftmeddelande: frånluftsbatteriet i en dockad modul avfrostas.",
          "Ingen åtgärd behövs; det går över av sig självt.", "låg"),
    237: ("Givarfel avluftsgivare AZ2-BT21", "Fel på avluftsgivaren i en andra dockad frånluftsmodul. Styrningen går i larmläge.",
          "Kontakta service.", "medel"),
    271: ("Kall uteluft, värmedrift", "Uteluften är för kall för kompressordrift. Elpatronen tar över värmen.",
          "Ingen åtgärd behövs; kompressorn startar igen när det blir varmare.", "medel"),
    290: ("Fläktlarm", "Fläkten står still. Kompressor, elpatron och avfrostning stoppas.", "Kontakta service.", "medel"),
    291: ("Laddpumpslarm", "Laddpumpen står still. Kompressor och elpatron stoppas.",
          "Bryt strömmen en minut och starta igen, kontrollera systemtrycket och ring service om det återkommer.", "medel"),
    313: ("Torrkörning", "Cirkulationspumpen har ingen vätska att pumpa och systemet stoppas.",
          "Oftast läckage eller luft i systemet efter påfyllning. Kontakta service.", "hög"),
}


def alarm_info(code):
    """Name, meaning, action and severity for a NIBE alarm number, or None when it is not in the list."""
    entry = ALARMS.get(code) if isinstance(code, int) else None
    if entry is None:
        return None
    name, meaning, action, severity = entry
    return {"name": name, "meaning": meaning, "action": action, "severity": severity}


def alarm_label(code):
    """'184 Filterlarm', or just the number when its meaning is unknown."""
    info = alarm_info(code)
    return f"{code} {info['name']}" if info else f"{code}"
