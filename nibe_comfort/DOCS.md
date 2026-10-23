# NIBE Comfort – användarguide

## Vad tillägget gör

Du väljer en eller flera rumsgivare och önskad inomhustemperatur i °C.
Tillägget läser Home Assistant varje minut. Medelvärdet av givarna jämförs
med ditt mål. Valfri påverkan från aktuell utetemperatur och aktuellt elpris
läggs till. Resultatet blir ett förslag till **kurvförskjutning**, inte en
direkt inställning av rumstemperaturen på pumpen.

Automatik är avstängd från början. Övervakning fungerar även utan vald
kurvförskjutningsentitet. Tillägget försöker hitta rätt pumpanslutning automatiskt. Endast entiteter
från Home Assistants myUplink-integration används för upptäckten. Det kräver
exakt en `number`-entitet med suffixet `_heating_offset_climate_system_1`.
Utegivaren identifieras med suffixet `_current_outd_temp_bt1` och måste tillhöra
samma HA-enhet som kurvförskjutningen. Smart Price Adaption och andra
pumpinställningar väljs inte som ersättning.

Vid flera pumpar, omdöpta entitets-ID:n eller saknade entiteter behöver du
välja anslutning under **Ändra pumpanslutning manuellt** respektive
**Ändra utegivare manuellt**. Redan sparade val ändras inte automatiskt.
Upptäckten fyller bara tomma anslutningsfält i formuläret; tryck **Spara
inställningar** för att behålla förslaget. Den aktiverar inte styrningen.
Rumsgivare väljer du alltid själv.

### Golvvärme och climate-entiteter

Välj exempelvis `climate.lk_allrum_thermostat` i samma lista som dina andra
rumsgivare. Tillägget läser **`current_temperature`**, alltså uppmätt temperatur,
inte termostatens börvärde eller läge. Termostatens egna inställningar ändras
inte. Temperaturenheten hämtas från Home Assistants enhetsinställningar och
Fahrenheit räknas om till Celsius.

En termostat utan uppmätt temperatur kan visas i listan men pausar regleringen
om den väljs; börvärdet används aldrig som reservvärde. Välj inte både en
termostat och dess separata givare om du vill undvika dubbel vikt för samma rum.
En avstängd termostat kan fortfarande lämna en giltig temperatur.

### Temperaturgrafer och väderprognos

Temperaturgrafen visar de **sparade** rumsgivarnas senaste 24 timmar via
Home Assistants historik-API. Givarna behöver ingå i Recorder; data som
aldrig sparats kan inte återskapas. Välj **Alla rum** eller ett enskilt rum.
För golvvärme och andra termostater (`climate`) visas både uppmätt temperatur
och vad termostaten är inställd att värma till (streckad), hämtat från
termostatens historiska börvärde. Givare rapporterar bara när värdet ändras,
så varje värde ritas som giltigt tills nästa rapport (steglinje). Luckor
betyder att givaren var otillgänglig. Grafen glesas ut till första/sista
värdet i femminutersintervall. Mätpunkterna finns också som tabell.
Historik hämtas högst var femte minut.

### Värmeplan · väder och elpris

Ett eget kort visar senaste dygnet och kommande cirka 36 timmar på samma
tidsaxel, med en linje för *nu*:

- **Utetemperatur**: NIBE-givarens historik och väderprognosen (streckad).
- **Elpris**: Tibbers kvartspriser via `tibber.get_prices`, med dina gränser
  för billigt och dyrt. Kräver exakt ett Tibber-hem; annars visas ett fel.
- **Kurvförskjutning**: faktisk förskjutning, planerad förskjutning
  (streckad lila) och beräknat mål (tunn streckad). Planen simulerar samma
  regler som automatiken: hela steg, största steg och minsta ändringsintervall,
  så markeringarna visar när pumpen faktiskt kommer att ändras.
  - Lodräta markeringar med etikett, t.ex. `13:45 ▲ 2→3`:
    **streckad lila** = kommande justering, **grön** = ändrad av automatiken,
    **orange** = ändrad manuellt eller av annan automation. En gjord ändring
    räknas som automatikens när loggen har ett skrivförsök med samma värde.
  - Listan *Justeringar* visar kommande ändringar med orsak och gjorda
    ändringar senaste dygnet med källa.

Gränssnittet anpassar sig till smala skärmar (även Home Assistant-appen) utan
sidoscroll, och graferna ritas om i skärmens bredd så att texten går att läsa.
- **Värmekurvan som den är inställd nu**: myUplink exponerar inte kurvans
  inställning, så den uppskattas från pumpens egen *beräknade framledning*
  mot utetemperaturen under 7 dygn. Linjen gäller nuvarande förskjutning;
  punkter med andra förskjutningar visas gråa. Prognosens utetemperatur visas
  som ett skuggat fält.

Planen använder samma regler som styrningen: nuvarande rumsavvikelse,
väderpåverkan på prognosen och elprisregeln på kommande priser, inom dina
gränser. Den antar att rumstemperaturen ligger kvar. **Planen är en
förhandsvisning och ändrar inte pumpen**; styrningen räknar om varje minut
och ändrar högst ett steg i taget. Är väder- och elpris­påverkan 0 följer
planen bara rummen, vilket kortet visar.

Väderprognosen hämtas från en HA-`weather`-entitet med tjänsten
`weather.get_forecasts`. En entydig SMHI-källa prioriteras automatiskt.
Annars används den enda prognosstödda väderkällan; finns flera väljer du källa
i formuläret och sparar. Timprognos prioriteras, annars visas dygnsprognos.
Prognosen mellanlagras i 30 minuter och har egen felindikering.

**Prognosen är beslutsstöd, inte ny automatisk styrning.** Befintlig
väderpåverkan fortsätter använda aktuell NIBE-utomhustemperatur. Ingen
prognosbaserad förvärmning aktiveras vid uppdatering.

### Pumpens drift, larm och elförbrukning

Översikten läser entiteter från samma myUplink-enhet som den valda
kurvförskjutningen: kompressorstatus, aktuell kompressorfrekvens, larm,
driftläge, eltillsats, framledning, retur, varmvatten och NIBEs utegivare.
Det kräver de vanliga entitets-ID-suffixen från integrationen; omdöpta eller
saknade entiteter visas som okända. Råa driftlägen som inte är översatta
visas som integrationen rapporterar dem. Inget antas vara ”av” eller
”inget larm” om källan saknas eller är otillgänglig, eller om hela pumpen
har slutat rapportera.

Pumpens elförbrukning visas bara om samma enhet har en tydlig elektrisk
effekt-/förbrukningsgivare. Ström i ampere eller producerad värme omvandlas
inte till en påhittad elförbrukning. Saknas sådan givare behöver du en
separat mätare/integration för att mäta pumpen specifikt.

Entydiga Tibber-givare för **hela husets** aktuella effekt och dagens
ackumulerade förbrukning visas separat. De är inte pumpens förbrukning.
Vid flera hem/mätare väljs ingen godtyckligt. Värdena visas i källans enhet,
med rapporteringstid och synliga fel. Översikten uppdateras varje minut.

## Börja här

1. Anslut NIBE via Home Assistants myUplink-integration och säkerställ att
   du kan ändra kurvförskjutningen manuellt där. Möjligheten att skriva
   beror på integration, modell och myUplink-behörigheter.
2. Öppna NIBE Comfort från sidopanelen. Välj tillförlitliga rumsgivare,
   helst i representativa rum utan direkt sol eller tillfälliga värmekällor.
   Alla valda givare måste vara tillgängliga; ingen givare ignoreras tyst.
3. Kontrollera den automatiskt hittade kurvförskjutningen och utegivaren.
   När en ny pumpanslutning fylls i automatiskt föreslås även dess aktuella
   förskjutning som bas, om värdet ryms inom dina säkerhetsgränser.
   Kontrollera alltid basförskjutningen; befintliga val ändras inte.
4. Sätt säkerhetsgränser inom entitetens rapporterade min/max. Det aktuella
   värdet måste ligga inom dina gränser innan automatiken kan köras.
5. Spara med automatiken avstängd. Läs status och förslag. Ställ in
   elpris och väder först när grundinställningen känns rimlig.
6. Aktivera automatiken när du har verifierat entitet och gränser.

**Stoppa automatiken** stoppar framtida skrivningar men återställer inte
tidigare värden. En redan skickad begäran kan inte tas tillbaka.
Pumpen fortsätter reglera på sin senaste inställning. Använd pumpens egna
reglage om Home Assistant, nätverket eller tillägget inte är tillgängligt.

## Hur regleringen räknar

```text
rum = (måltemperatur − medeltemperatur) × rumspåverkan
väder = (referenstemperatur − utetemperatur) × väderpåverkan
önskad förskjutning = basförskjutning + rum + väder + elpris
```

Inom toleransbandet sätts rumspåverkan till 0. Förslaget begränsas till dina
säkerhetsgränser. Nästa skrivning begränsas dessutom av maximal ändring per
gång och kvantiseras till entitetens giltiga steg, räknat från dess minimum.
Små skillnader kan därför ge oförändrad förskjutning.

Grundinställningen tillåter högst 1 förskjutningssteg per 30 minuter.
Värmesystem är tröga; detta är en enkel begränsad proportionell reglering,
inte en självlärande modell eller en garanti för exakt rumstemperatur.
Låt pumpens grundkurva vara rimligt inställd även utan tillägget.

Väderpåverkan är 0 från början för att undvika dubbel kompensation med
pumpens egen utomhusreglering. Välj en temperaturgivare eller `weather`-entitet
om du vill lägga till extra påverkan. Prognoser visas separat och påverkar inte regleringen.

## Elpris

Välj en numerisk givare för **aktuellt** elpris, exempelvis från en
Nord Pool-integration. Kontrollera enheten i gränssnittet: gränserna använder
exakt samma enhet. Tillägget räknar inte om öre till kronor och lägger inte
automatiskt till skatt, moms eller nätavgifter.

Tillägget försöker fylla i prisgivaren automatiskt från Tibber-integrationen,
om exakt en prisgivare med enheten valuta/kWh eller valuta/MWh finns.
Månadspris, energiförbrukning och kostnad väljs inte som aktuellt pris.
Flera Tibber-hem kräver manuellt val. Redan sparat val ersätts aldrig.
Tryck **Spara inställningar** för att behålla ett nytt förslag; att hitta
en givare aktiverar inte prisstyrning eller ändrar prisgränserna.

Vid eller under lågprisgränsen läggs vald höjning till. Vid eller över
högprisgränsen dras vald sänkning av. Mellan gränserna är påverkan 0.
Prisoptimering får inte sänka när medeltemperaturen ligger under
komfortbandet, eller höja när den ligger över. Säkerhetsgränserna gäller alltid.
Påverkan 0 inaktiverar elprisstyrningen. Framtida priser, optimerad
dygnsplanering, varmvattenstyrning och faktisk besparingsberäkning ingår inte.

## Skydd och fel

- Saknade, otillgängliga, icke-numeriska eller gamla värden pausar nya
  skrivningar och ger ett synligt fel. Temperaturer måste anges i °C eller °F;
  Fahrenheit räknas om. Rumsvärden utanför 5–40 °C avvisas.
- Åldern räknas från Home Assistants `last_reported`, eller `last_updated`
  om det första saknas. myUplink skriver bara värden som har ändrats, så en
  oförändrad kurvförskjutning, BT1 eller pumpstatus behåller sin gamla
  tidsstämpel. Ett sådant värde godtas ändå så länge **något annat värde från
  samma pump** har rapporterats inom åldersgränsen. Det visar att
  integrationen fortfarande levererar data. Tystnar hela pumpen, eller blir
  värdet otillgängligt, pausas styrningen som tidigare.
- Start och omstart innebär minst ett helt inställt intervall utan skrivning.
  Tidpunkten för skrivförsök sparas **innan** anropet till Home Assistant:
  även ett timeoutfel ger väntetid eftersom pumpen kan ha tagit emot ändringen.
- Ett lyckat serviceanrop betyder bara att Home Assistant accepterat anropet.
  Nästa avläsning visar vad integrationen faktiskt rapporterar; UI:t låtsas
  aldrig att pumpen har ändrats innan återläsning.
- Manuell ändring på pumpen kan senare skrivas över när automatiken är på.
  Stäng av automatiken innan du ändrar manuellt.
- Konfiguration och senaste skrivförsök ligger i `/data` och överlever omstart.
  Ogiltiga sparade filer stoppar starten i stället för att ersättas med standarder.
- Det finns ingen styrning när tillägget inte körs. Pumpens eget frostskydd,
  kompressorlogik och säkerhetsfunktioner ersätts aldrig av detta tillägg.

## Lokalt REST API

Detta är tilläggets lokala API, **inte** ett direkt REST API på NIBE-pumpen.
Styrningen går fortfarande via Home Assistant. En framtida lokal
pumpintegration kan användas om den exponerar motsvarande `number`-entitet.

Ingress används normalt utan separat token; Home Assistant hanterar
inloggningen. Ingressport 8098 ska inte publiceras separat.

För ett valfritt lokalt API:

1. Ange en slumpmässig `api_token` på minst 32 tecken i tilläggets alternativ.
2. Aktivera en värdport för `8099/tcp` under nätverk och starta om tillägget.
3. Skicka `Authorization: Bearer <din token>` till varje begäran.

Porten är avstängd som standard. Token krävs även om porten endast är nåbar
på ditt lokala nätverk. API:t använder HTTP: exponera det inte mot internet
eller osäkra nät. Använd HTTPS-proxy/VPN för trafik över ej betrodda nät.
Token ger behörighet att både läsa och ändra alla styrinställningar.

| Metod | Sökväg | Innehåll |
| --- | --- | --- |
| GET | `/api/status` | Senaste avläsning, förslag och eventuellt fel |
| GET | `/api/entities` | Valbara HA-entiteter med namn, värde och enhet |
| GET | `/api/discovery` | Entydigt identifierad myUplink-pump och utegivare, utan att ändra inställningar |
| GET | `/api/overview` | Pumpdrift, larm och åtskilda pump-/huselmätvärden |
| GET | `/api/history` | Senaste dygnets temperaturhistorik för sparade givare |
| GET | `/api/forecast` | Prognos från vald eller entydigt upptäckt HA-väderkälla |
| GET | `/api/plan` | Utetemperatur, Tibber-priser, faktisk/planerad förskjutning och uppskattad värmekurva (endast läsning) |
| GET | `/api/events` | Senaste 100 logghändelser; `?before=<id>` hämtar äldre |
| GET | `/api/settings` | Komplett sparad styrkonfiguration |
| PUT | `/api/settings` | Ersätt komplett konfiguration med JSON |

Hämta inställningarna först, ändra önskade fält och skicka tillbaka hela
objektet med `Content-Type: application/json`. Okända eller saknade fält
avvisas. Aktivering kräver en aktuell, giltig avläsning innan sparandet
accepteras. Det finns inget API för godtyckliga HA-serviceanrop.

## Beständig ändringslogg

Från version 0.2.0 sparas loggen i `/data/events.sqlite` och överlever omstart
och uppdatering. Historiska ändringar före denna version kan inte återskapas.
Inga gamla värden eller aktiveringsval återställs vid uppdateringen.
SQLite använder full synkronisering. Om loggen inte kan skrivas stoppas
automatiken i stället för att göra nya ospårbara ändringar.

Loggen skiljer på:

- Start med de då sparade inställningarna.
- Begäran att spara inställningar och slutfört sparande, med före-/eftervärden.
- Automatiskt skrivförsök, med entitet, gammalt/begärt värde, rumstemperatur,
  måltemperatur och bidrag från rum, väder och elpris.
- Accepterat HA-serviceanrop, eller fel där pumpen ändå kan ha tagit emot det.
- Separat återläsning av det begärda värdet. Detta bekräftar HA-integrationens
  rapport, inte en oberoende fysisk mätning av pumpens inställning.
- Utebliven bekräftelse när minsta ändringsintervallet passerat, ändrade
  återlästa förskjutningar, paus, återhämtning och interna fel.

Även ett pågående skrivförsök sparas så att det kan följas efter omstart.
En ändrad förskjutning kan komma från pumpens reglage eller annan automation;
tillägget påstår inte att varje sådan ändring orsakats av det.

Loggen innehåller inga åtkomsttoken men innehåller valda entiteter och
styrinställningar. Den har ingen automatisk gallring: äldre poster kan läsas
med paginering i UI/API. Övervaka tilläggets diskutrymme vid långvarig användning.

## Begränsningar

Första versionen stöder ett klimatsystem och ett gemensamt rumsmål.
Den styr inte zonventiler, varmvatten, ventilation, semesterläge eller
kompressorn direkt. Den är inte verifierad mot just din F730 förrän du
har provat den med din installation. Kontrollera NIBEs rekommendationer
för ditt hus och värmesystem innan automatisk styrning aktiveras.
