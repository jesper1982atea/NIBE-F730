# NIBE Comfort

Ställ önskad rumstemperatur i ett svenskt webbgränssnitt, välj temperaturgivare
från Home Assistant och se hur rum, väder och elpris påverkar värmen.

Tillägget hittar automatiskt kurvförskjutning och utegivare BT1 när en entydig
myUplink-pump med standardiserade entitetsnamn finns. Rumsgivare väljer du själv.
Det börjar i övervakningsläge och har begränsningar för
ändringstakt, värdeintervall och mätvärdenas ålder.

Även golvvärmetermostater (`climate`, exempelvis LK) kan användas som rumsgivare.
Rummen väljs med sökbara temperaturkort. Markera vilken termostat som är
närmast pumpen och aktivera vid behov valfri övertemperaturbegränsning:
vid minst 0,5 °C över målet kan ett högre termostatbörvärde sänkas till målet.
Funktionen är av från början och kräver huvudautomatiken; lägre börvärden
höjs aldrig och driftlägen ändras inte. Tillräckligt flöde måste säkerställas.
Temperaturgrafer per rum (uppmätt och termostatens börvärde), en värmeplan med
väder, Tibber-priser och planerad förskjutning, en uppskattad värmekurva,
väderprognos, automatisk Tibber-prisupptäckt och pumpstatus samlas i samma gränssnitt. Alla tilläggets framtida inställningsändringar
och automatiska styrförsök loggas beständigt, med separat återläsningsbekräftelse.

Med en ackumulerad elmätare för just pumpen kan du följa elanvändning
och beräknad kostnad per timme tillsammans med loggade styrändringar.
En entydig förbrukningsräknare på vald myUplink-pump används automatiskt;
du kan välja manuellt om flera räknare finns eller en separat mätare används.
Producerad värme räknas inte som använd el. Kostnaden använder historiska
priser från vald elpriskälla; skillnader före/efter är observationer, inte
bevisad besparing. Saknat underlag visas som luckor, inte som noll.

**Experimentell första version:** rätt pumpentitet och säkerhetsgränser måste
verifieras på din anläggning innan automatiken aktiveras. Grundkurva,
varmvatten och ventilation ändras inte.

Se dokumentationsfliken för installation, reglering och det valfria lokala API:t.
