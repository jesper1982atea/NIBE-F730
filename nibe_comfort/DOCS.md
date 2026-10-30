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

Gränssnittet skiljer på **vardag och fördjupning**. Temperatur, mål och nästa
åtgärd visas först. Genvägar högst upp går till varmvatten, rum/historik,
inställningar och ändringslogg; de två sistnämnda öppnas när du följer länken.
**Så fungerar värmen** förklarar skillnaden mellan rumsmål, termostatbörvärde
och kurvförskjutning.

Väder-/prisgrafer, pumpens tekniska värmekurva, husmodellens detaljer,
varmvattenhistorik, beslutsförklaringar och elförbrukning ligger bakom tydliga
utfällbara rubriker. Klicka eller fokusera rubriken och tryck Enter för att
öppna. Underlagen fortsätter uppdateras i bakgrunden. Utfällning ändrar aldrig
inställningar eller styrning; delarna börjar stängda när sidan laddas om.
Inga funktioner har tagits bort. Aktuella temperaturer, nästa åtgärd,
varmvattenbedömning, larm och viktiga varningar ligger kvar synligt.
Sparraden och samtyckena gäller precis som tidigare.

Överst finns fyra rutor: **Inne nu** (medel av dina valda rum och hur långt
från ditt mål det är), **Du vill ha** (ändra med − och +, tryck sedan *Spara
ny temperatur*), **Ute nu** (med prognosens spann närmaste dygnet) och **Nästa
justering** (när värmekurvan ändras, åt vilket håll och varför). Är automatiken
av visas justeringen som ett förslag. I samma ruta visas **Kurvförskjutning
nu** från senaste avläsningen och ditt **grundvärde**, även när ingen
justering behövs. Detta är förskjutningen, inte numret på pumpens grundkurva.
Därunder visas elpriset just nu.

Status och aktuella temperaturer hämtas automatiskt var 15:e sekund.
Sparade inställningar hämtas samtidigt, men ett långsamt inställningsanrop
blockerar inte temperaturerna. Ändras målet i en annan flik följer visningen
med. Egna osparade ändringar ligger kvar och markeras som osparade; de används
inte av automatiken förrän du sparar. Ändrar du rum, prioritering eller
markeringen närmast pumpen hålls dessa osparade rumsval ihop.
Styrloopen läser Home Assistant ungefär varje minut. När du återvänder till
appen eller får tillbaka nätverket hämtas status och översikt direkt.
Giltiga temperaturer visas även om ett annat mätvärde pausar styrningen;
saknade eller gamla värden visas som **—**, inte som aktuella. Kontaktfel
och en avläsning som är äldre än tre minuter markeras tydligt.
Status och sparade inställningar kan läsas även medan ett styranrop väntar
på Home Assistant. Kontaktfel återhämtas automatiskt vid kommande hämtningar.

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

Måltemperaturen högst upp gäller snittet av prioriterade rum; gränssnittet
visar detta bredvid temperaturinställningen. Med två prioriterade givare
väger de lika. Utan prioritering styr snittet
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

**Förvärmning före dyra timmar** är av från början och kräver kommande
priser från vald Tibber- eller Nord Pool-givare.
Inställningen **Lagra lite värme före dyra timmar** finns vid husmodellen;
elpriskortet länkar direkt dit. Där visas ett exempel med ditt mål: med
21 °C och måländring 0,5 °C blir det tillfälliga målet 21,5 °C före
dyrperioden och 20,5 °C under den, sedan 21 °C igen. Exemplet följer
osparade värden men ändrar inget förrän du sparar. Detta är mål, inte en
garanti att huset håller sig inom dessa temperaturer eller att du sparar
en viss summa. Termostatbegränsningen följer det vanliga målet och kan
bromsa förvärmningen. Huvudautomatiken måste vara på för att styra pumpen.

Timmar minst 25 % över dygnets mittpris räknas som dyra. Före dem höjs
måltemperaturen tillfälligt med vald mängd (0,1–1,5 °C, standard 0,5), om
timmarna innan är minst 20 % billigare. Under de dyra timmarna får det bli
lika mycket svalare, även när ingen föregående förvärmning kunde planeras.
Förvärmningen börjar 3 timmar före tills modellen *känner
huset väl*; därefter räknas tiden ut från modellen (1–6 timmar). Ändringen går
genom vanliga rumsregeln, så alla gränser, steglängd och minsta tid mellan
ändringar gäller. Saknas priser eller blir något fel används vanlig
måltemperatur och orsaken visas i statusförklaringen.

### Braskamin och automatisk observation av extravärme

I kortet **Värme från kaminen** väljer du **Jag har braskamin** och
temperaturkällan i kaminens rum. Välj bland dina redan valda rumsgivare;
Home Assistants rumsnamn visas när tilldelning finns. Välj inte både en
termostat och dess separata givare för samma rum. Minst ett annat valt rum
utan kamin behövs som jämförelse. Spara valet; uppdateringen aktiverar
ingenting automatiskt. Observation kan användas i övervakningsläge utan
att huvudautomatiken aktiveras.

**Ditt prioriterade snitt styr fortfarande även under misstänkt eldning.**
Utan prioritering används snittet av alla valda rum, precis som tidigare.
Är kaminrummet med i det styrande snittet kan dess högre temperatur
sänka kurvförskjutningen för hela huset och göra andra rum kallare. Ett
kaminrum som bara visas men inte är prioriterat påverkar inte det prioriterade
snittet. **Bestående misstänkt extravärme ger dessutom ett extra bidrag på
−1 steg i beräknad kurvförskjutning**, även om kaminrummet inte är
prioriterat. Bidraget tillåts bara när det styrande snittet är minst ditt
vanliga mål minus toleransen, oberoende av tillfälligt sänkt elprismål.
Det läggs till tillsammans med rum, väder och elpris innan säkerhetsgränser
och steglängd tillämpas. Det betyder inte ett nytt steg ned varje minut.
Faktiskt begärt värde och väntetid visas som vanligt.

Bidraget tas bort när perioden avslutas, avläsningar saknas, funktionen
stängs av eller det styrande snittet blir för kallt. Då räknas kurvan om
utifrån det aktuella behovet; ingen gammal förskjutning återställs blint.
Minsta ändringsintervall gäller även när bidraget tas bort. Ingen
prioritering ändras och kaminen styrs inte. Alla pumpgränser,
termostatregler och stopp gäller fortfarande. Huvudautomatiken måste vara
på för att skicka ändringar; annars visas bara förslag. Aktivering och
ändrade rumsval kräver bekräftelse i gränssnittet. Ändringsloggen visar
kaminbidraget vid beslutet, inte att värmen säkert kom från eldning.

**Automatisk temperaturanalys är osäker.** Den kan misstänka lokal
extravärme men inte bekräfta eldning. Sol, matlagning eller annan lokal
värme kan ge samma mönster. Långsam eldning eller värme som sprids jämnt
till alla rum kan missas. Det är inget brand- eller säkerhetslarm.

Analysen tar en observation högst var femte minut, med giltiga och färska
temperaturer (högst 15 minuter, eller din kortare mätvärdesgräns) i
samtliga valda rum. En oförändrad givare kan bekräftas genom färska
rapporter från samma HA-enhet, som i övrig styrning. En temperaturökning
i kaminrummens snitt måste vara minst 0,5 °C under ungefär 45–75 minuter,
motsvara minst 0,7 °C per timme och vara minst 0,5 °C större än ökningen
i övriga rum. Mönstret ska bestå minst tio minuter innan en misstänkt
period registreras. Analysen behöver först minst 45 minuter med
sammanhängande observationer.

Perioden avslutas när kaminrummens relativa ökning har sjunkit till högst
0,2 °C över jämförelserummen i minst 30 minuter. Förloppet ska ha varat
minst 45 minuter. Detta mäter hur länge den **lokala temperaturskillnaden**
syns, inte glödens varaktighet eller tiden tills hela huset svalnat.
Avbrott över 15 minuter, ändrade rumsval, avstängning eller ett oavslutat
förlopp efter 18 timmar markeras som osäkra/avbrutna och används inte
för typisk varaktighet.

Efter minst **tre avslutade förlopp med samma rumsval** visas medianen
av observerad varaktighet samt rummens temperaturökning och när minst
0,3 °C ökning först observerades. Det är samtidiga mätningar, inte bevis
för att kaminen värmde ett visst rum. Siffrorna används inte för att
automatiskt schemalägga framtida kurvkommandon eller garantera återstående
värmetid.

Observationerna sparas från aktivering i `/data/fireplace-state.json` och
överlever omstart: högst ett dygn med femminutersvärden och de senaste
100 förloppen inom 180 dygn. Äldre historik analyseras inte retroaktivt.
Misstänkta perioder markeras i husmodellens temperaturgraf och i
ändringsloggen. Pumpens modell undantar registrerade perioder från en
timme före jämförelsens början till sex timmar efter avslut, även om
kaminobservation senare stängs av. Därmed tillskrivs inte dessa
temperaturförändringar pumpen vid inlärningen. Oupptäckt extravärme kan
fortfarande påverka modellen. Historiska temperaturer visas oförändrade;
den framtida temperaturprognosen pausas under en pågående observation
eftersom den fortsatta extravärmen är okänd.

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
bara om du uttryckligen aktiverar begränsningen nedan. Temperaturenheten hämtas från Home Assistants enhetsinställningar och
Fahrenheit räknas om till Celsius.

En termostat utan uppmätt temperatur kan visas i listan men pausar regleringen
om den väljs; börvärdet används aldrig som reservvärde. Välj inte både en
termostat och dess separata givare om du vill undvika dubbel vikt för samma rum.
En avstängd termostat kan fortfarande lämna en giltig temperatur.

### Rumsval och termostaten närmast pumpen

Temperaturkorten visar namn, uppmätt temperatur, driftläge och termostatens
börvärde. Sök på namn eller entitets-ID. Termostater visas först; kryssa i
**Visa även separata temperaturgivare** för övriga givare. Redan valda givare
visas även utan den kryssrutan och tas inte bort vid uppdateringen.
Ett sökfilter ändrar inte vilka rum som är valda.
Antalet valda termostater och separata givare visas var för sig. Valda separata
givare listas med namn och ID även om sökningen döljer korten. En termostats
`sensor.…` är inte samma sak som dess styrbara `climate.…`.
**Avmarkera separata givare · behåll termostater** tar bort givarna och deras
eventuella prioriteringar från formuläret, inte från Home Assistant. Kontrollera
rumsvalet och spara; inga bortval sparas automatiskt.

Välj **Termostat närmast värmepumpen** bland de valda termostaterna.
Markeringen visas i rumsöversikten och är enbart information: den ändrar
inte prioritering, pumpreglering eller termostatbegränsning och innebär inte
att detta rum undantas. Tas termostaten bort från rumsvalet rensas markeringen.

### Begränsa övertemperatur med termostater

Funktionen är **av från början**, även vid uppdatering. Välj enbart
`climate`-termostater, aktivera **Begränsa övertemperatur med termostater**
och spara med bekräftelsen. Även huvudautomatiken måste vara på och pumpens
vanliga kontroll måste lyckas. Under pumptest skickas inga nya termostatändringar.
Befintliga rumsval eller termostatinställningar byts aldrig automatiskt.

När ett valt rum är minst **0,5 °C över ditt sparade rumsmål** sänks ett
högre termostatbörvärde till målet via `climate.set_temperature`.
Alla valda rum omfattas, även rum som inte är prioriterade och det närmast
pumpen. Funktionen sänker inte stegvis, höjer aldrig ett lägre börvärde
och ändrar inte driftläge eller slår av/på värmen. Den använder det sparade
målet även under förvärmning och kan därför begränsa den extra uppvärmningen.

Termostaten måste vara i **värmeläge (`heat`)**, rapportera giltig temperatur,
stöd för ett enskilt temperaturbörvärde och min/max. Om steglängd rapporteras
måste målet passa den exakt. `target_temp_step` är valfri i Home Assistant:
saknas den skickas målet utan avrundning inom rapporterade gränser, utan att
gissa steglängd. Detta anges i status och loggens försök visar rapporterat
steg (null om det saknas). Återläst börvärde måste fortfarande matcha begäran;
avrundar integrationen målet räknas ändringen inte som bekräftad.
Automatikläge, kyla, dubbla temperaturintervall, andra saknade uppgifter och gamla
mätvärden avvisas. Vid aktivering kontrolleras samtliga valda termostater.
Senare fel visas per rum och stoppar det rummets nya skrivningar; andra
giltiga rum kan fortsätta om pumpens övergripande kontroll fortfarande lyckas.
Fahrenheit räknas om vid avläsning och serviceanropet använder HA:s ursprungsenhet.

Sparraden högst upp i inställningarna ligger kvar när du rullar och visar
osparade fält, sparresultat och fel. **Spara ändringar** använder samma
validering och bekräftelser som knappen längst ner. Ändringar sparas inte
automatiskt.

En bekräftad begränsning görs högst en gång per varm period, med minst
**30 minuter** mellan försök. Efter omstart återläses värden och återstående tid räknas från
senaste sparade försök för just termostaten; ingen extra startväntan läggs på.
Finns inget tidigare försök kan första giltiga avläsningen leda till ändring.
En ny varm period tillåts först efter att
rummet svalnat till högst **0,2 °C över målet** och intervallet passerat.
Om du manuellt höjer börvärdet under samma varma period sänks det inte igen.
Egna scheman kan ändra börvärdet; undvik konkurrerande automationer.

Försöket sparas i `/data/thermostat-state.json` före serviceanropet. En
timeout kan ändå ha nått termostaten, så värdet återläses först. Om ett
försök misslyckas eller inte bekräftas kan det upprepas efter 30 minuter,
men bara om rummet fortfarande behöver begränsas, börvärdet är samma som
före försöket och rumsmålet är oförändrat. Ändrade börvärden eller mål
spelas inte över av ett återförsök. Äldre sparade försök utan känt tidigare
börvärde upprepas inte inom samma varma period; detta visas i rumsstatus.
En sen återläsning kan fortfarande bekräfta ändringen och stoppar återförsök.
Alla vanliga kontroller, avstängning och pumptest gäller även vid återförsök.
Status och ändringslogg visar begäran, accepterat anrop,
återläst börvärde och utebliven bekräftelse efter 15 minuter. En HA-återläsning
bekräftar bara integrationens rapport, inte ventilens fysiska läge eller flöde.

**Avstängning stoppar nya ändringar men återställer inte tidigare börvärden.**
Återställ dem själv i Home Assistant vid behov. Säkerställ att värmesystemet
har tillräckligt flöde när flera termostater stryper; markeringen närmast
pumpen är inget flödesskydd. Rådgör med installatör vid osäkerhet.

### Temperaturgrafer och väderprognos

Temperaturgrafen visar de **sparade** rumsgivarnas senaste 24 timmar via
Home Assistants historik-API. Givarna behöver ingå i Recorder; data som
aldrig sparats kan inte återskapas. Välj **Alla rum** eller ett enskilt rum.
Rumsöversikten och dygnskorten grupperas efter det **område** som enheten
eller entiteten tillhör i Home Assistant. Ett kort visar senaste temperatur,
förändring under historikperioden och loggade termostatändringar i det området.
Om Home Assistant inte har ett område tilldelat visas givaren under
**Inget område i Home Assistant**. Tilldela området i Home Assistant för att
få önskad rumsgruppering. Ett rum med flera valda entiteter kan visa fler än
en temperaturkurva.
För golvvärme och andra termostater (`climate`) visas både uppmätt temperatur
och vad termostaten är inställd att värma till (streckad), hämtat från
termostatens historiska börvärde. Givare rapporterar bara när värdet ändras,
så varje värde ritas som giltigt tills nästa rapport (steglinje). Luckor
betyder att givaren var otillgänglig. Grafen glesas ut till första/sista
värdet i femminutersintervall. Mätpunkterna finns också som tabell.
Historik hämtas högst var femte minut.

Under översikten finns kort **per Home Assistant-område**. Kortet visar
uppmätt temperatur för entiteterna i rummet och, för climate-termostater,
dess historiska börvärde. Rumsfiltret kan visa en entitet eller alla.
Rumskortets markeringar och ändringslista visar termostatbegäranden i det
rummet. Pumpens kurvförskjutning är gemensam för hela huset och visas i
översiktens styrhistorik, inte som en lokal rumsändring.

Ändringslistan visar före/eftervärden, enhet och senaste status: begärd,
skickad utan bekräftelse, återläst, misslyckat anrop eller saknad återläsning.
En begäran markeras vid försökstidpunkten, inte när rummet blev varmare.
Återläsningens tid visas separat. Började begäran före dygnet men fick
besked under perioden visas den vid beskedstiden. En separat återläst
kurvobservation kan avse samma händelse; orsaken antas inte vara tillägget.
Även pumptest eller manuell/annan styrning kan ge en kurvobservation.
Börvärdeslinjen kan ändras av egna scheman; avsaknad av tilläggsmarkering
betyder inte att termostaten varit oförändrad.

För pumpens automatiska beslut visas loggade bidrag i kurvsteg från rum,
utetemperatur och elpris, inklusive noll. Positivt bidrag begär mer värme,
negativt mindre. Mätvärden och mål vid beslutet samt eventuell förvärmning
visas när de finns i loggen. Prisets enhet sparas för nya beslut; äldre
poster utan enhet markeras i stället för att ges dagens enhet.
Termostatbegränsning grundas på övertemperatur mot det sparade målet,
inte direkt på elpris. Temperaturutvecklingen efteråt bevisar inte orsak:
sol, ventilation, användning och annan styrning kan också påverka.

Markeringarna använder periodens beständiga ändringslogg och länkade
begäranden, även om fler än 100 händelser har loggats. Historik innan
tillägget började logga kan inte återskapas. Om loggen inte kan läsas
visas ett uttryckligt fel; temperaturhistoriken behålls utan markeringar.

### Värmeplan · väder och elpris

**Tillåten kurvförskjutning** finns direkt i inställningarna. Du kan ändra
lägsta och högsta förskjutning; varningen om nådd gräns länkar direkt dit.
Värdet 4 är inte ett fast tak. Befintliga gränser behålls tills du själv
ändrar och sparar dem. Pumpens tillåtna intervall, maximal ändring per gång
och väntetid gäller fortfarande. Högre förskjutning påverkar hela huset,
även redan varma rum, och ett kurvsteg är inte en grads rumstemperatur.

Den sparade elpriskällan styr prisunderlaget och förbrukningsöversiktens
leverantörstexter. Med Nord Pool visas inte husmätare eller feltexter från
Tibber, även om integrationen också är installerad. Nord Pool är en
priskälla, inte en förbrukningsmätare; värden från exempelvis Enegic hämtas
inte automatiskt till förbrukningsöversikten.

Ett eget kort visar senaste dygnet och kommande cirka 36 timmar på samma
tidsaxel, med en linje för *nu*:

- **Utetemperatur**: NIBE-givarens historik och väderprognosen (streckad).
- **Elpris**: priser från vald Tibber- eller Nord Pool-givare, med dina gränser
  för billigt och dyrt. Tibber hämtas via `tibber.get_prices` och kräver exakt
  ett hem; annars visas ett fel. Nord Pool beskrivs under *Elpris* nedan.
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

### Historikhämtning och minne

Styrloopens förvärmningsplan hämtas av en ensam bakgrundsarbetare.
Pris- eller historikhämtning för denna valfria optimering blockerar inte
nya temperaturavläsningar. Saknas aktuell plan används vanligt mål med en
synlig förklaring. En gammal plan används inte med andra inställningar,
och planer äldre än 15 minuter används inte. En bakgrundshämtning kan
uppdatera planen tidigast efter en minut, även efter ett fel.

Ingress, lokalt API och förvärmning delar hämtat underlag. Samtidiga
förfrågningar om samma underlag gör inte flera identiska hämtningar,
och Recorder-historik hämtas ett anrop i taget. Cachen behåller högst
24 varianter av underlagen.

Husmodellens första inläsning delas upp i högst ett dygn per anrop.
Senare inläsningar kompletterar de sparade timvärdena med två dygns
överlapp. Klimatentiteternas temperaturattribut behålls.

Enskilda historiksvar över 8 MiB stoppas före JSON-avkodning och visas
som ett fel, inte som en tom eller lyckad hämtning. Väntan på en redan
pågående hämtning eller ledig historikanslutning avbryts efter 15 sekunder;
ett senare försök kan lyckas. Historikfel ändrar inte dina styrinställningar.
Detta begränsar tilläggets belastning men är ingen garanti mot minnesbrist
orsakad av andra integrationer eller en Docker-bilduppdatering.

Enhetsvärdena i inställningarna läses om varje minut medan sidan är aktiv,
utan att osparade val försvinner. Status hämtas var 15:e sekund. När
webbsidan återupptas avbryts en fastnad statusläsning så att en ny kan
starta. Den underliggande styrloopen läser normalt HA varje minut;
visningen gör inte en ny fysisk pumpavläsning vid varje webbuppdatering.

Tilläggets lokala Docker-bygge använder den publika ECR-spegeln av
officiella Python-imagen. Uppdateringen behöver fortfarande fungerande
nätverk, Docker och tillräckligt med värdminne. En nätverkstimeout till
ett bildregister är inte i sig bevis för att tillägget har en minnesläcka.

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

Pumpens elförbrukning i driftöversikten visas bara om samma enhet har en tydlig elektrisk
effekt-/förbrukningsgivare. Ström i ampere eller producerad värme omvandlas
inte till en påhittad elförbrukning. Saknas sådan givare behöver du en
separat mätare/integration för att mäta pumpen specifikt.

Entydiga Tibber-givare för **hela husets** aktuella effekt och dagens
ackumulerade förbrukning visas separat. De är inte pumpens förbrukning.
Vid flera hem/mätare väljs ingen godtyckligt. Värdena visas i källans enhet,
med rapporteringstid och synliga fel. Översikten uppdateras varje minut.

### Pumpens el och kostnad över tid

**Pumpens ackumulerade elanvändning** under **Inställningar → Din
värmepump** använder automatiskt en entydig räknare för **använd el**
på samma myUplink-enhet som din sparade kurvförskjutningsentitet.
Givaren måste både uppfylla mätarkraven nedan och ha ett namn/ID som
tydligt anger förbrukad el, exempelvis `energy_spent` eller `energy_consumed`.
Producerad värme, andra pumpar och husmätare väljs inte automatiskt.
Flera möjliga räknare kan överlappa eller avse olika delar: då visas
ett tydligt meddelande och du får välja manuellt. Ingen automatisk
summering eller växling till en annan räknare vid otillgänglighet görs.

Du kan välja och spara en specifik elmätare manuellt, även från en separat
integration; sådana val går alltid före automatisk upptäckt. Återgå till
**Automatiskt · elmätare på vald myUplink-pump** och spara för att åter
använda upptäckten. Statuskortet visar vilket läge och vilken entitet som
används. Upptäckten sparar inga inställningar och ändrar aldrig styrningen.
Om mätaren bara avser värme ingår inte varmvatten eller annan el som den
inte mäter. Om myUplink bara har periodräknare eller producerad värme
behövs fortfarande en separat ackumulerad elmätare.

Kortet **Pumpens el och kostnad** visar de senaste 24 avslutade timmarna,
inte ett kalenderdygn. Datatäckning, timvärden, kostnad och styrmarkeringar
visas tillsammans. Mätaren ska ha `device_class: energy`, `state_class:
total` eller `total_increasing` och enheten Wh, kWh eller MWh. Använd en
ackumulerad räknare, inte `energy_produced_for_heating_current_hour`:
den mäter producerad värme under en period, inte använd el. Givare för
producerad energi och identifierade tim-/dygnsräknare erbjuds inte som
elmätare. Funktionen integrerar inte effekt (W) och summerar inte flera
överlappande räknare.

Ökningen mellan två giltiga mätarrapporter fördelas jämnt över intervallet,
som får vara högst två timmar. Därför är timfördelning och kostnad
**uppskattade utifrån mätarintervall**, inte exakt uppmätt momentan
elanvändning. Negativa räknarhopp, otillgängliga värden och längre luckor
utelämnas. Efter sista rapporten extrapoleras ingen förbrukning. En timme
utan full täckning visas som okänd; totalsiffror märks uttryckligen som
enbart täckt/prissatt del när underlaget inte räcker till hela perioden.
Noll förbrukning från en giltig oförändrad räknare är däremot ett giltigt
nollvärde.

Kostnaden använder **historik från den sparade prisgivaren och elpriskällan**,
inte dagens pris eller framtidsprognoser. Pris i valuta/MWh omvandlas till
valuta/kWh; öre och kr tolkas som SEK. Negativa priser stöds. Pris får gälla
högst din inställda mätvärdesålder, dock aldrig längre än två timmar utan
ny rapport. Saknade priser, okänd enhet eller ändrad valuta ger okänd
kostnad, inte gratis el. Endast moms och avgifter som redan ingår i
prisgivaren räknas med; fast avgift ingår inte. Pumpens elmätare och
prisgivare behöver ingå i Home Assistants Recorder.

Före/efter-listan jämför hela timmen före och efter ändringens timme;
själva ändringstimmen hoppas över. Bekräftade pump-/termostatändringar
placeras vid återläsningen från HA, inte vid en tidigare skrivbegäran.
Även sparade inställningar markeras. Väder, varmvatten, kamin och
värmesystemets tröghet kontrolleras inte bort: detta visar samtidig
förbrukning/kostnad, **inte orsak eller bevisad besparing**. Ingen
besparingsmodell eller automatisk optimering läggs till.

Detta är enbart läsning och påverkar inte pumpstyrningen. Underlaget
uppdateras var femte minut, med en enda pågående energihämtning per
konfiguration. Endast de två valda entiteternas historik hämtas, för
högst ett dygn plus en timmes startunderlag och pågående timme.
Anropen använder samma serialisering och 8 MiB-svarsgräns som övrig
historik; ingen separat full historikkopia sparas i tillägget.

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
En hysteres gör att befintligt steg behålls när önskat värde ligger nära
gränsen mellan två steg; först när nästa steg är tydligt närmare ändras
förskjutningen. Säkerhetsgränserna gäller även då.

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

Välj först **Vilken elpriskälla vill du använda?**: **Tibber** eller
**Nord Pool**. Tibber är standard, även när en äldre installation uppdateras.
Din sparade prisgivare och dina styrinställningar behålls; att upptäcka
Nord Pool byter inte källa.

Ett källbyte är ett osparat val tills du trycker **Spara inställningar**.
Gränssnittet föreslår en entydig givare för den nya källan eller ber dig
välja manuellt; föregående källas givare förs inte över. Kontrollera
prisgränserna eftersom givarens enhet och innehåll kan skilja sig.
Kommande priser hämtas bara från vald källa, utan automatiskt reservbyte.

Välj en numerisk givare för **aktuellt** elpris, exempelvis från en
Nord Pool-integration. Kontrollera enheten i gränssnittet: gränserna använder
exakt samma enhet. Tillägget räknar inte om öre till kronor och lägger inte
automatiskt till skatt, moms eller nätavgifter.

Tillägget försöker fylla i prisgivaren automatiskt från den valda källan,
om exakt en aktuell prisgivare med enheten valuta/kWh eller valuta/MWh finns.
Månadspris, energiförbrukning och kostnad väljs inte som aktuellt pris.
Flera hem eller elområden inom vald källa kräver manuellt val.
Redan sparat val ersätts aldrig.
Tryck **Spara inställningar** för att behålla ett nytt förslag; att hitta
en givare aktiverar inte prisstyrning eller ändrar prisgränserna.

De fasta prisgränserna finns under **Avancerat · extra kurvpåverkan från
priset just nu**. Detta är en separat regel, inte förvärmningsplanen, och
kan läggas ovanpå den. Förvärmningen behöver inga fasta prisgränser.
En givare med priset just nu bekräftar inte att kommande priser finns.

### Enegic och Nord Pool utan Tibber

Du kan använda Enegic för att mäta husets förbrukning och Nord Pool för
elpriset utan att ha Tibber. Välj **Nord Pool** som elpriskälla och exempelvis
`sensor.nord_pool_se3_aktuellt_pris` under **Ändra elprisgivare manuellt**,
kontrollera enheten och spara. En förbrukningsgivare i W eller kWh ska inte
väljas som prisgivare. Den befintliga förbrukningsöversikten är fortfarande
Tibber-specifik; detta stöd lägger inte till Enegics API eller en separat
Enegic-mätaröversikt.

Priset just nu fungerar med den numeriska givarens värde och enhet.
För **förvärmning och kommande prisgraf** stöds två varianter:

- **HACS Nord Pool**: vald givare har `raw_today` och eventuellt
  `raw_tomorrow`, med `start`, `end` och `value` per period.
  Morgondagen används bara när `tomorrow_valid` är sann. Värdena behålls i
  givarens enhet, inklusive öre/kWh. Tidsstämplarna måste ha tidszon.
- **Officiella Nord Pool-integrationen i Home Assistant**: SE1–SE4 stöds.
  Givarens entitets-id ska innehålla elområdet, som i exemplet ovan
  (alternativt ha attributet `region`). Tillägget hittar givarens konfiguration
  och hämtar priser med `nordpool.get_prices_for_date` i samma valuta.
  Åtgärdens valuta/MWh räknas om till valuta/kWh om givaren använder kWh.
  Datum hämtas från Home Assistants lokala klocka. Morgondagens lista
  hämtas bara när samma elområdes diagnostiska binärgivare anger att
  priserna är tillgängliga. Saknas den givaren används endast dagens lista.

Prisgrafen slutar vid sista kända prisperioden. Om prislistan inte täcker
hela förhandsplanen anges det i planens förklaring; efter sista perioden
antas ingen extra kurvpåverkan från den separata prisregeln.

Tim- och kvartsperioder stöds, inklusive sommar-/vintertid.
Luckor, överlappningar, felaktiga priser eller okänd konfiguration ger
ett uttryckligt fel. Inga priser gissas och ingen annan Tibber-givare används
som reserv för en vald Nord Pool-givare. Moms, påslag och nätavgifter läggs
inte till. Den officiella integrationen visar normalt spotpris utan moms,
medan en HACS-givare kan vara inställd med moms eller tillägg.

Saknas kommande prisdata, eller finns för få kommande perioder, visas orsaken
och förvärmningen använder vanligt temperaturmål. Den separata regeln för
priset just nu kan fortfarande användas när dess givare är giltig.
Nord Pool-stödet är verifierat med testdata mot dokumenterade dataformat,
inte mot en fysisk installation.
Formatreferenser: [Home Assistants Nord Pool-åtgärd](https://www.home-assistant.io/actions/nordpool.get_prices_for_date/)
och [HACS Nord Pool](https://github.com/custom-components/nordpool).

Vid eller under lågprisgränsen läggs vald höjning till. Vid eller över
högprisgränsen dras vald sänkning av. Mellan gränserna är påverkan 0.
Prisoptimering får inte sänka när medeltemperaturen ligger under
komfortbandet, eller höja när den ligger över. Säkerhetsgränserna gäller alltid.
Sätt båda stegvärdena till 0 för att stänga av enbart denna extra
kurvpåverkan; förvärmningen stängs av med sin egen kryssruta.
Framtida priser används bara av
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
- Vid start och omstart återläses HA-värden och pågående begäranden följs upp.
  Väntetiden för pumpen räknas från senaste sparade styrförsök, inte omstarten.
  Har intervallet passerat, eller finns inget tidigare försök, kan en giltig
  första avläsning leda till ändring. Huvudstopp, pumptest och alla vanliga
  kontroller gäller fortfarande. Pump- och termostatskrivningar får upp till
  60 sekunder på svar från Home Assistant; vanliga läsningar har en kortare
  tidsgräns. Tidpunkten sparas **innan** HA-anropet:
  även timeout ger väntetid eftersom pumpen kan ha tagit emot ändringen.
  Misslyckade eller obekräftade pumpförsök följs upp genom återläsning.
  När intervallet gått ut räknas behovet om från aktuella värden och
  inställningar; bara en fortfarande nödvändig ändring skickas igen.
  Återläst börvärde eller förskjutning tar inte bort intervallet: det visar
  integrationens rapport, inte att huset redan hunnit reagera på värmen.
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
| GET | `/api/energy` | Senaste 24 avslutade timmarnas pumpel, beräknad kostnad, datatäckning och styrmarkeringar (endast läsning) |
| GET | `/api/history` | Senaste dygnets temperaturhistorik för sparade givare |
| GET | `/api/forecast` | Prognos från vald eller entydigt upptäckt HA-väderkälla |
| GET | `/api/hotwater` | Varmvattnets nivå, temperaturer, tappningar och laddningar senaste 7 dygnen (endast läsning) |
| GET | `/api/plan` | Utetemperatur, Tibber-/Nord Pool-priser, faktisk/planerad förskjutning och uppskattad värmekurva (endast läsning) |
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
