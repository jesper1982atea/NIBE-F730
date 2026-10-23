# NIBE Comfort

Ställ önskad rumstemperatur i ett svenskt webbgränssnitt, välj temperaturgivare
från Home Assistant och se hur rum, väder och elpris påverkar värmen.

Tillägget hittar automatiskt kurvförskjutning och utegivare BT1 när en entydig
myUplink-pump med standardiserade entitetsnamn finns. Rumsgivare väljer du själv.
Det börjar i övervakningsläge och har begränsningar för
ändringstakt, värdeintervall och mätvärdenas ålder.

Även golvvärmetermostater (`climate`, exempelvis LK) kan användas som rumsgivare.
Temperaturgrafer per rum (uppmätt och termostatens börvärde), en värmeplan med
väder, Tibber-priser och planerad förskjutning, en uppskattad värmekurva,
väderprognos, automatisk Tibber-prisupptäckt och pumpstatus samlas i samma gränssnitt. Alla tilläggets framtida inställningsändringar
och automatiska styrförsök loggas beständigt, med separat återläsningsbekräftelse.

**Experimentell första version:** rätt pumpentitet och säkerhetsgränser måste
verifieras på din anläggning innan automatiken aktiveras. Grundkurva,
varmvatten och ventilation ändras inte.

Se dokumentationsfliken för installation, reglering och det valfria lokala API:t.
