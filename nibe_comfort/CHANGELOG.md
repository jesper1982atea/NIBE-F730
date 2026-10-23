# Ändringslogg

## 0.2.5

- Enklare startsida. Överst visas bara det viktigaste: **Inne nu**, **Du vill
  ha** (ändra med − och + och tryck *Spara ny temperatur*), **Ute nu** med
  prognos för närmaste dygnet och **Nästa justering** – när och varför
  värmekurvan ändras. Under rutorna visas elpriset just nu och värmekurvans
  förskjutning.
- Värmeplanen börjar med listan över kommande och gjorda justeringar, följd av
  graferna. Därefter temperaturgrafer och pumpens drift.
- Inställningar (rum, pump, väder, elpris, automatik) och ändringsloggen ligger
  längst ner i utfällbara delar. Inställningarna fälls ut automatiskt så länge
  rum eller pumpanslutning saknas.

## 0.2.4

- Mörkt läge. Knappen **Tema** uppe till höger växlar mellan *auto*, *ljust*
  och *mörkt*. *Auto* följer Home Assistants tema (även när du byter tema medan
  sidan är öppen) och annars enhetens inställning. Valet sparas i webbläsaren.

## 0.2.3

- Kurvförskjutningsgrafen visar tydligt **när** värmekurvan kommer att
  justeras (streckad lila markering, t.ex. `13:45 ▲ 2→3`) och när den har
  ändrats – grön om automatiken gjorde det, orange om det var manuellt eller
  en annan automation.
- Planen simulerar automatikens regler: hela steg, största steg och minsta
  ändringsintervall.
- Ny lista *Justeringar*: kommande ändringar med orsak och gjorda ändringar
  senaste dygnet med källa.
- Mobilanpassat: ingen sidoscroll i Home Assistant-appen, graferna ritas i
  skärmens bredd.

## 0.2.2

- Nytt kort *Värmeplan · väder och elpris* med utetemperatur och prognos,
  Tibbers kvartspriser, faktisk och planerad kurvförskjutning.
- Uppskattad värmekurva från pumpens beräknade framledning.
- Temperaturgrafen kan visa ett rum i taget med termostatens börvärde.

## 0.2.1

- Kurvförskjutningen pausas inte längre felaktigt för att myUplink bara
  rapporterar ändrade värden.

## 0.2.0

- Golvvärmetermostater (`climate`) som rumsgivare, temperaturgrafer,
  SMHI-prognos, automatisk Tibber-upptäckt, pumpöversikt med larm, kompressor
  och el samt beständig ändringslogg.

## 0.1.0

- Första version: måltemperatur, rumsgivare, väder- och elprispåverkan på
  kurvförskjutningen via myUplink.
