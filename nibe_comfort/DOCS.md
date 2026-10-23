# NIBE Comfort – användarguide

## Vad tillägget gör

Du väljer en eller flera rumsgivare och önskad inomhustemperatur i °C.
Tillägget läser Home Assistant varje minut. Medelvärdet av de styrande givarna jämförs
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

### Startsidan

Överst finns fyra rutor: **Inne nu** (medel av dina valda rum och hur långt
från ditt mål det är), **Du vill ha** (ändra med − och +, tryck sedan *Spara
ny temperatur*), **Ute nu** (med prognosens spann närmaste dygnet) och **Nästa
justering** (när värmekurvan ändras, åt vilket håll och varför). Är automatiken
av visas justeringen som ett förslag. I samma ruta visas **Kurvförskjutning
nu** från senaste avläsningen och ditt **grundvärde**, även när ingen
justering behövs. Detta är förskjutningen, inte numret på pumpens grundkurva.
Därunder visas elpriset just nu.

Status och aktuella temperaturer hämtas automatiskt var 15:e sekund.
Styrloopen läser Home Assistant ungefär varje minut. När du återvänder till
appen eller får tillbaka nätverket hämtas status och översikt direkt.
Giltiga temperaturer visas även om ett annat mätvärde pausar styrningen;
saknade eller gamla värden visas som **—**, inte som aktuella. Kontaktfel
och en avläsning som är äldre än tre minuter markeras tydligt.

Längre ner finns värmeplanen med grafer, husmodellen (*Ditt hus lär sig*),
varmvatten, temperaturhistorik och pumpens drift.
**Inställningar** och **Ändringslogg** ligger längst ner och fälls ut med ett
tryck. Inställningarna är utfällda från början tills rum och pumpanslutning
är valda.

### Prioritera ett eller två rum

Under **Inställningar** väljer du först **Rumsgivare** för de rum du vill
följa. Välj sedan valfritt 1–2 av dessa under **Prioriterade rum** och
spara. Exempel: välj endast vardagsrummets Liljebagge-givare för att styra
dess uppmätta temperatur mot 22 °C, även om snittet för alla rum redan är
högre. Välj inte både termostaten och dess separata temperaturgivare för
samma rum: det skulle ge rummet dubbel vikt i snittet.

Med två prioriterade givare väger de lika. Utan prioritering styr snittet
av alla valda rum som tidigare. Inga prioriterade givare väljs vid uppdatering.

Överst visas **Prioriterade rum nu** och **Alla valda givare**. Under dem
öppnas rumslistan med varje temperatur, avvikelse från målet och uppmätt
förändring senaste timmen. Förändringen jämför aktuell giltig temperatur
med Recorder-historiken en timme tidigare. Saknas historik visas ingen
förändring. Det är inte en prognos och säger inte vad som orsakat ändringen.

**Pumpens förskjutning påverkar hela huset.** Varmare rum kan därför bli
ännu varmare när vardagsrummet behöver värme. Rumslistan gör detta synligt,
men reglerar inte enskilda golvvärmeslingor eller radiatorventiler.
Husmodellen, förvärmningen och värmeplanen följer samma prioriterade snitt.

När ett annat valt rum når **2 °C över målet** visas en varning högst upp:
exempelvis 24 °C vid mål 22 °C. Varningen namnger rummen och visar deras
temperatur och uppmätta timförändring. En pågående höjningsplan visas
uttryckligen. Varningen stoppar inte automatiken och utger sig inte för
att bevisa att pumpen orsakat temperaturökningen. Saknade mätvärden
är aldrig bevis för att ett rum ligger under varningsgränsen.

Om en prioriterad givare är ogiltig pausas styrningen, utan att ersätta den
med något annat rum. Övriga rum kan sakna mätvärde utan att stoppa
prioriterad styrning. Då visas inte ett missvisande delsnitt för hela huset:
**Alla valda givare** visar **—** och felet anges på berört rum.

### Komfortkort: varför blir inte rummet varmt?

Under startsidan visas ett rådgivande kort för de rum som styr värmen
(prioriterade rum om sådana valts, annars alla valda rum). Ett rum räknas
som kallt när det ligger under målet med mer än toleransbandet.

För termostater läses driftläge och börvärde. För separata temperaturgivare
kopplas endast en entydig termostat på exakt samma HA-enhet. En avstängd
termostat eller ett börvärde under målet markeras som något att kontrollera
innan mer värme tillåts i hela huset. Otillgängliga, ogiltiga, saknade eller
flertydiga inställningar anges uttryckligen. Ett börvärde på 22 °C bevisar
inte att en ventil är öppen eller att flödet räcker.

Kortet visar också hur varje valt rums uppmätta temperatur förändrats sedan
senaste bekräftade höjningen av den valda pumpens förskjutning. Jämförelsen
använder `offset_observed` i den hämtade ändringsloggen, inte enbart ett
skickat kommando, och Recorder-värdet vid återläsningen. Om senaste ändringen
var en sänkning inväntas nästa höjning. Saknas historik eller aktuellt värde
visas ingen beräknad förändring. Under de första två timmarna anges att det
är tidigt att bedöma effekten.

Temperaturförändringen är **inte ett säkert orsakssamband**. Väder, sol,
ventilation och användning påverkar också. När andra rum redan är minst
2 °C över målet rekommenderas lokal kontroll före ytterligare höjning.
Kortet kan inte diagnostisera flöde, injustering eller ventilfel och ändrar
inte pumpen, termostaterna eller dina säkerhetsgränser.

### Ditt hus lär sig och förvärmning

Tillägget lär sig hur ditt hus reagerar på värmekurvan. Varje timme sparas
medelvärdet för de styrande rummen, utegivaren och kurvförskjutningen i
`/data/learning.sqlite` (högst 180 dygn), så inlärningen fortsätter även när
Home Assistants Recorder bara sparar en vecka. En enkel värmebalans per timme
anpassas till mätvärdena:

- **Ett steg på kurvan ger** – hur mycket varmare det blir inne på sikt per
  steg förskjutning.
- **Halva effekten märks efter** – hur trögt huset är.
- **Status**: *Lär sig* (under 3 dygn eller för lite variation), *Grov bild*
  och *Känner huset väl* (minst 7 dygn, 6 kurvändringar och att modellen
  förutsäger 6 timmar framåt bättre än att anta oförändrat).

Grafen visar innetemperaturen senaste dygnet och vad modellen tror händer
nästa dygn med den planerade värmekurvan och väderprognosen.

**Förvärmning före dyra timmar** är av från början och kräver Tibber-priser.
Timmar minst 25 % över dygnets mittpris räknas som dyra. Före dem höjs
måltemperaturen tillfälligt med vald mängd (0,1–1,5 °C, standard 0,5), om
timmarna innan är minst 20 % billigare. Under de dyra timmarna får det bli
lika mycket svalare. Förvärmningen börjar 3 timmar före tills modellen *känner
huset väl*; därefter räknas tiden ut från modellen (1–6 timmar). Ändringen går
genom vanliga rumsregeln, så alla gränser, steglängd och minsta tid mellan
ändringar gäller. Saknas priser eller blir något fel används vanlig
måltemperatur och orsaken visas i statusförklaringen.

### Varmvatten

Kortet **Varmvatten** läser givarna från samma myUplink-enhet som den valda
kurvförskjutningen (`…_hot_water_top_bt7`, `…_hot_water_charging_bt6`,
`…_hot_water_amount`, `select.…_hot_water_amount` och `…_status`). Inget styrs
härifrån; kortet är bara information.

- **Tanken** visar en uppskattad nivå. Finns pumpens givare för varmvattenmängd
  (minuter kvar) jämförs värdet med det högsta under senaste veckan. Annars
  bedöms nivån från laddgivaren BT6. Det är en uppskattning, inte en mätning av
  liter.
- **Tappningar** hittas när laddgivaren BT6 längre ner i tanken sjunker snabbt
  (minst 1 °C mellan två rapporter och totalt minst 1,5 °C) när kallt vatten
  strömmar in. Långsam avsvalning i halva grader över flera timmar räknas inte.
  Minst 10 °C räknas som *stor* (dusch eller bad), 4–10 °C som *mellan* och
  mindre som *liten*. BT7 i toppen ändras ofta bara lite vid en tappning.
- **När används varmvattnet?** visar antal tappningar per timme på dygnet för
  senaste 7 dygnen, vanligaste tider och hur snabbt tanken brukar vara laddad
  igen efter en stor tappning. Laddningar räknas när pumpens status är
  *Hot water*.
- Grafen visar BT7 och BT6 senaste två dygnen med tappningar markerade och
  laddningar skuggade. Kortet uppdateras varannan minut.

Saknas givarna, eller har pumpens entiteter döpts om, visas ett tydligt
meddelande i stället för gissningar. Värden som slutat rapporteras visas inte.

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

### Mörkt läge

Knappen **Tema** uppe till höger växlar mellan *auto*, *ljust* och *mörkt*.
*Auto* följer Home Assistants tema, även om du byter tema medan sidan är öppen.
Öppnas sidan utanför Home Assistant följer den enhetens ljus/mörk-inställning.
Valet sparas per webbläsare.

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

### Testa styrningen

Under **Testa styrningen** längst ned kan du kontrollera att pumpen verkligen
tar emot ändringar. *Starta test* ändrar kurvförskjutningen ett steg (uppåt,
eller nedåt om din övre gräns är nådd) i 10 minuter och ställer sedan
tillbaka ursprungsvärdet automatiskt. Under tiden gör automatiken inga egna
ändringar. Testet fungerar även när automatiken är avstängd, men kräver att
en kurvförskjutning är vald, att värdet är aktuellt och att dina min/max
lämnar plats för ett steg.

Stegen i kortet visar att ändringen skickades, att Home Assistant läser
tillbaka det nya värdet från pumpen (brukar ta 1–5 minuter via myUplink) och
att ursprungsvärdet är återställt. Kontrollera samtidigt på pumpen:

- **Pumpens display:** meny 1.1 Temperatur (Inomhusklimat), värme för
  klimatsystem 1, eller meny 1.9.1.1 Värmekurva.
- **myUplink-appen:** *Förskjutning värmekurva* för klimatsystem 1.
- Efter några minuter ändras pumpens beräknade framledningstemperatur.

Menynamnen kan skilja mellan programversioner. Läser Home Assistant aldrig
tillbaka testvärdet når ändringen inte pumpen; kontrollera myUplink-
integrationens skrivrättigheter (kan kräva myUplinks Premium-abonnemang).
*Avbryt och återställ nu* ställer tillbaka direkt. Om förskjutningen ändras
av något annat under testet lämnas den orörd. Testet loggas i ändringsloggen.

## Börja här

1. Anslut NIBE via Home Assistants myUplink-integration och säkerställ att
   du kan ändra kurvförskjutningen manuellt där. Möjligheten att skriva
   beror på integration, modell och myUplink-behörigheter.
2. Öppna NIBE Comfort från sidopanelen. Välj tillförlitliga rumsgivare,
   helst i representativa rum utan direkt sol eller tillfälliga värmekällor.
   Alla styrande givare måste vara tillgängliga; ingen ersätts tyst.
   Valfria prioriterade givare väljs bland dina rumsgivare.
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

Om exempelvis grundvärdet är 3 och rumspåverkan +1,35 begär regleringen
4,35. Med en maxgräns på 3 tillåts ingen höjning från 3. Startsidan visar
då **Övre gränsen nådd**, inte att rummet redan nått målet. Gränsen måste
ändras medvetet i inställningarna; den höjs aldrig automatiskt.
En höjning påverkar även redan varma rum och ger ingen garanti för en
viss inomhustemperatur.

Medeltemperaturen avser prioriterade givare om sådana valts, annars alla
valda rumsgivare. Varje styrande givare väger lika.

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
Påverkan 0 inaktiverar elprisstyrningen. Framtida priser används bara av
förvärmningen (se *Ditt hus lär sig och förvärmning*). Varmvattenstyrning och
faktisk besparingsberäkning ingår inte.

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
- Samma kontroll gäller rumstermostater (`climate`), till exempel LK:
  en färsk rapport från en annan entitet på exakt samma HA-enhet kan
  bekräfta en oförändrad termostat. Givare från andra enheter används
  inte som bevis. Termostaten måste fortfarande ha en giltig temperatur
  och får inte vara otillgänglig.
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
| GET | `/api/hotwater` | Varmvattnets nivå, temperaturer, tappningar och laddningar senaste 7 dygnen (endast läsning) |
| GET | `/api/plan` | Utetemperatur, Tibber-priser, faktisk/planerad förskjutning och uppskattad värmekurva (endast läsning) |
| GET | `/api/events` | Senaste 100 logghändelser; `?before=<id>` hämtar äldre |
| GET | `/api/settings` | Komplett sparad styrkonfiguration |
| PUT | `/api/settings` | Ersätt komplett konfiguration med JSON |
| GET | `/api/test` | Status för testet av styrningen |
| PUT | `/api/test` | `{"action": "start"}` startar testet, `{"action": "stop"}` återställer direkt |

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
