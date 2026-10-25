# Ändringslogg

## 0.2.23

- Temperaturmålet märks nu tydligt som mål för prioriterade rum när sådana
  valts. Texten visar att målet gäller deras snitt; utan prioritering gäller
  det snittet av alla valda rum.
- Förtydligar måltexten direkt i rumsväljaren och uppdaterar förklaringen
  högst upp. Styrningen är oförändrad: prioriterade rum väger lika och
  kurvförskjutningen påverkar hela huset.

## 0.2.22

- Ingen extra väntetid enbart för att tillägget startas om eller uppdateras.
  Pump och termostater använder tidpunkten för senaste sparade styrförsök
  och återläser giltiga HA-värden innan de kan ändra något.
- Faktiskt minsta intervall mellan försök behålls, även efter timeout och
  bekräftad återläsning. Pågående begäranden och termostatens spärr per varm
  period överlever omstart. Ingen omstart används som genväg runt dessa skydd.
- Termostatstatus skiljer på återstående intervall och redan begränsad varm
  period. Inga aktiveringsval eller sparade börvärden ändras av uppdateringen.

## 0.2.21

- Lugnare vardagsvy med genvägar till översikt, varmvatten, rum/historik,
  inställningar och ändringslogg. Begripliga introduktioner och en utfällbar
  guide för måltemperatur, kurvförskjutning och bekräftad återläsning.
- Fördjupning kan fällas ut för väder-/prisunderlag, teknisk värmekurva,
  husmodell, varmvattenhistorik, beslutsförklaringar och elförbrukning.
  Alla funktioner finns kvar. Aktuellt läge, larm, övertemperaturvarningar,
  huvudgrafer och kritisk termostatinformation döljs inte.
- Diagram ritas om till rätt bredd när informationen öppnas, utan nytt
  styranrop. Rensade diagram efter ett fel återupplivas inte av utfällning.
  Ogiltiga fält i stängda inställningsdelar öppnas inför rättning.
- Samma inställningar, samtycken, sparrad och reglering som tidigare.
  Ingen automation aktiveras och inga sparade värden ändras av designen.

## 0.2.20

- Egen dygnsgraf per vald termostat med uppmätt temperatur och historiskt
  börvärde. Rumsfiltret styr både grafer och relevant ändringslista.
- Kurv- och termostatbegäranden markeras på samma tidsaxel. Listan skiljer
  begäran, accepterat serviceanrop, återläsning, fel och utebliven bekräftelse.
  Kurvobservationer visas separat utan att tillskrivas en obekräftad orsak.
- Vid pumpbeslut visas loggade bidrag från rum, ute och elpris (även noll),
  mätvärden vid beslutet samt förvärmning/sparläge. Termostatbeslut visar
  övertemperatur och mål, inte en påhittad direkt elprispåverkan.
- Historiken hämtar periodens beständiga logg inklusive länkade begäranden
  utanför perioden, inte bara de senaste 100 posterna. Loggfel visas tydligt
  utan att kasta bort tillgänglig temperaturhistorik. Inga styrregler ändras.

## 0.2.19

- Stöd för climate-termostater som inte rapporterar den valfria
  `target_temp_step`, exempelvis Liljebagge. Målet skickas exakt inom
  rapporterade gränser utan gissad steglängd. Saknad steglängd visas i status.
  Rapporterad steglängd valideras som tidigare; återläsning och alla övriga
  skydd behålls, inklusive utebliven bekräftelse om målet inte återläses.
- Sparrad högst upp i inställningarna följer med när du rullar. Visar vilka
  inställningar som ändrats, pågående sparande, resultat och fel nära knappen.
  Samma validering och aktiveringsbekräftelser gäller för båda sparknapparna.

## 0.2.18

- Rumsvalet visar separat antal termostater och givare samt namn och ID för
  valda separata givare, även när sökningen döljer deras kort.
- Knapp för att uttryckligen avmarkera separata givare och behålla termostater.
  Inget tas bort automatiskt; ändringen sparas först med Spara inställningar.
- Om termostatbegränsningen blockeras visas exakt vilka givare som behöver
  avmarkeras. Tomt rumsval får ett separat fel. En termostats sensor-entitet
  förväxlas inte med dess styrbara climate-entitet.

## 0.2.17

- Nytt rumsval med sökning, tydliga temperaturkort och termostater först.
  Befintliga givare och prioriterade rum behålls. Separata givare kan visas.
- Markera vilken vald termostat som är närmast värmepumpen. Markeringen är
  enbart information, inte prioritering eller undantag från begränsningen.
- Valfri **Begränsa övertemperatur med termostater**, av från början.
  Vid minst 0,5 °C över rumsmålet sänks ett högre börvärde till målet.
  Lägre börvärden höjs aldrig och driftlägen ändras inte. Kräver valda
  climate-termostater i värmeläge, giltiga färska värden och skrivbart mål.
- Huvudstopp, pumptest, 30 minuters intervall/omstartsskydd och högst ett
  försök per varm period skyddar styrningen. Försök sparas före skrivning;
  återläsning, fel och utebliven bekräftelse loggas per rum.
  Avstängning återställer inte tidigare ändrade börvärden.

## 0.2.16

- Nytt komfortkort **Varför blir inte rummet varmt?** under startsidan.
  Visar kalla styrande rum och om en kopplad termostat är avstängd eller
  inställd under ditt mål. Saknade eller flertydiga börvärden anges tydligt.
  Koppling sker bara direkt eller till en entydig termostat på samma HA-enhet.
- Efter en bekräftad höjning jämförs rumstemperaturerna vid höjningen med
  aktuella värden. Tidiga observationer markeras; historikluckor, sänkning
  efter höjning och loggfel hanteras utan påhittade resultat.
- Råd att kontrollera kalla rums termostater när andra rum redan är varma,
  samt förklaring om säkerhetsgränsen begränsar mer värme. Kortet är
  rådgivande och ändrar inte styrning, ventiler eller inställningar.

## 0.2.15

- Varning högst upp om ett annat valt rum når 2 °C över målet vid prioriterad
  styrning. Rummets namn, temperatur, avvikelse och uppmätt timförändring
  visas, även när automatiken planerar att höja för prioriterade rum.
  Detta är en varning, inte ett automatiskt stopp.
- När mer eller mindre värme begärs men säkerhetsgränsen stoppar nästa
  steg visas **Övre gränsen nådd** eller **Nedre gränsen nådd**, i stället
  för det missvisande **Ingen behövs**. Beräknad förskjutning före
  begränsning och aktuell gräns visas. Gränser ändras aldrig automatiskt.

## 0.2.14

- Välj valfritt 1–2 **prioriterade rumsgivare** i inställningarna. Deras
  snittemperatur styr mot ditt mål. Övriga valda rum visas för uppföljning,
  men påverkar inte styrningen. Utan prioritering fortsätter allt som tidigare.
- Startsidan visar både styrande snitt och snittet för alla valda givare.
  Rumslistan visar varje temperatur, avvikelse från målet och uppmätt
  förändring senaste timmen. Förändringen är ingen prognos eller garanti
  för vad pumpen har orsakat; förskjutningen påverkar hela huset.
- Husmodellen, förvärmningen och värmeplanen följer samma styrande rum.
  Ogiltiga prioriterade givare pausar styrningen. Ett fel i ett rum som
  bara övervakas markeras men stoppar inte prioriterad styrning.
- Befintliga inställningar behålls utan automatisk prioritering.

## 0.2.13

- Oförändrade rumstermostater kan nu verifieras via färska rapporter från
  samma Home Assistant-enhet, även utanför myUplink-integrationen.
  Exempelvis kan LK-termostatens temperatur vara oförändrad medan dess
  separata temperaturgivare fortsätter rapportera. Snittemperaturen
  försvinner då inte på grund av termostatens gamla tidsstämpel.
  Otillgängliga termostater och enheter som helt slutat rapportera
  blockerar fortfarande styrningen.

## 0.2.12

- Startsidan uppdateras direkt när appen blir synlig igen eller nätverket
  återkommer, utöver den löpande statusuppdateringen var 15:e sekund.
  Överlappande statusanrop undviks och gamla avläsningar markeras tydligt.
- Giltiga temperaturer och kurvförskjutning visas oberoende av andra
  mätfel. Ett fel på pumpanslutningen tömmer inte längre rumstemperaturen.
  Automatiken pausar fortfarande vid fel; ogiltiga mätvärden visas inte.
- Utetemperaturen högst upp kommer från aktuell avläsning, inte prognosen.

## 0.2.11

- Aktuell kurvförskjutning och grundvärde visas tydligt högst upp i rutan
  **Nästa justering**, även när ingen justering behövs. Värdet uppdateras
  med senaste statusavläsningen.

## 0.2.10

- **Testa styrningen** är enklare att följa: en stor ruta säger vilket värde
  pumpen ska visa just nu, med *Före testet*, *Under testet* och *Pumpen
  rapporterar nu* sida vid sida. Om automatiken redan hade justerat bort från
  ditt grundvärde förklaras varför testet utgår från ett annat värde.
  Menyvägarna på pumpen ligger under en utfällbar rad.

## 0.2.9

- Ny utfällbar sektion **Testa styrningen** (KONTROLL). Knappen *Starta test*
  ändrar kurvförskjutningen ett steg i 10 minuter, visar steg för steg att
  Home Assistant skickat ändringen och att pumpen rapporterar tillbaka det nya
  värdet, och ställer sedan automatiskt tillbaka ursprungsvärdet. Automatiken
  väntar under testet. Instruktioner visar var du ser värdet på pumpens
  display och i myUplink-appen. Testet överlever omstart och skriver aldrig
  över ett värde som någon annan har ändrat under tiden.

## 0.2.8

- Versionsnumret visas längst ned i gränssnittet, så att det syns att en
  uppdatering från Home Assistant har installerats.

## 0.2.7

- Nytt kort **Ditt hus lär sig** efter värmeplanen. Tillägget sparar timvisa
  medelvärden för rum, ute och kurvförskjutning i `/data/learning.sqlite` (180
  dygn) och lär sig hur huset svarar: hur många grader ett kurvsteg ger på
  sikt, hur snabbt det märks och hur träffsäker modellen är. Status visas
  ärligt som *Lär sig*, *Grov bild* eller *Känner huset väl*.
- Graf över innetemperaturen senaste dygnet och modellens förutsägelse för
  nästa dygn längs värmeplanen och väderprognosen.
- **Förvärmning före dyra timmar** (av från början). Före tydligt dyra timmar
  enligt Tibber höjs måltemperaturen tillfälligt och under de dyra timmarna
  får det bli lika mycket svalare (0,1–1,5 °C, standard 0,5). Tills modellen
  är säker börjar förvärmningen 3 timmar före. Saknas priser eller blir något
  fel styrs huset som vanligt mot måltemperaturen.
- Förvärmning och sparläge syns i värmeplanen, i statusförklaringen och i
  ändringsloggen.

## 0.2.6

- Nytt kort **Varmvatten** direkt efter värmeplanen. En tank visar ungefär hur
  mycket varmvatten som finns kvar (pumpens egen uppskattning i minuter, annars
  laddgivaren BT6), med temperatur i toppen (BT7) och BT6, inställt behov och
  om pumpen laddar just nu eller när den laddade senast.
- Statistik över senaste veckan: hur många tappningar, vilka tider på dygnet de
  brukar ske (stapeldiagram per timme), stora tappningar som dusch eller bad
  och hur lång tid det tar innan tanken är laddad igen.
- Graf över tankens temperatur senaste två dygnen med markerade tappningar och
  skuggade laddningar, samt en lista över de senaste tappningarna.
- Varmvattennivån visas även på startsidan under de fyra rutorna.

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
