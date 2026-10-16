![MemeBot 3 banner](assets/memebot3img.jpg)

# MemeBot 3

MemeBot 3 es un bot y workstation de investigacion para meme-coins de Solana.
Combina discovery, enriquecimiento de mercado, filtros por regimen, paper/live
execution, gestion de posiciones, analitica local, modelos ML en sombra y un
loop AutoResearch para proponer y validar cambios de estrategia sin activar
live de forma automatica.

El proyecto esta pensado para operar primero en paper, medir edge con reports y
replay, y solo despues considerar canaries live manuales y pequenos.

## Estado Actual

### Monitor: precio vigente tras esperar SQL (2026-10-16)

Se corrige el uso de precio LIVE caducado tras persistir peak/politica, antes
de decidir parciales o la proteccion por liquidez. Se conserva la validacion
de caja PAPER y el limite existente de 30 segundos del recibo HTTP; no se
certifica el instante de mercado del proveedor ni una ejecucion financiera.

Pasan 2.188 regresiones por carpeta, incluidas las 1.914 anteriores; 112 casos
dirigidos y 7.369 recogidos. CI completa de esta revision pendiente.
Sin nueva PAPER/LIVE, llamadas a proveedores, cambios de 0,1 SOL, umbrales de
salida, modelos o configuracion. Operador y AutoResearch apagados.
La rentabilidad y la auditoria global siguen sin estar completadas.
[Evidencia y limites](docs/audits/live_monitor_freshness_20261016.json).

### Riesgo ML: evidencia independiente y probabilidades correctas (2026-10-16)

Un riesgo alto validado ya no depende de que exista el modelo principal de
entrada para aplicar un veto habilitado. Los lectores de riesgo rechazan las
regresiones aunque devuelvan un numero entre 0 y 1: requieren probabilidades
binarias calibradas con los controles financieros/temporales existentes.
Shadow/off conserva su autoridad de observacion; no se activa ningun modelo.

Pasan 1.914 regresiones por carpeta, incluidas las 1.560 anteriores; 194 casos
dirigidos y 7.351 recogidos. La CI de esta revision sigue pendiente.
Son correcciones de software, no prueba de ganancias ni causa demostrada de
las cero compras anteriores. Sin nueva PAPER/LIVE, proveedores, cambios de
0,1 SOL, datos, pesos, configuracion o salidas. Operador y AutoResearch OFF.
[Evidencia y limites](docs/audits/risk_veto_evidence_20261016.json).

### Politica ML: shadow/off sin autoridad de veto (2026-10-16)

Se reproduce y corrige un veto ML indebido por flags de riesgo/green en modo
global shadow/off. El permiso del guardia nativo en modos activos no cambia;
green ahora informa el rechazo de riesgo que ese guardia ya aplicaba.
Pasan 1.560 regresiones por carpeta, incluidas las 1.460 anteriores, y se
recogen 7.257 casos oficiales. La CI de esta revision sigue pendiente.

Las particiones canonicas contienen 7.961 observaciones shadow/rechazos y
ninguna muestra de cierre financiero neto validado. No son trades rentables ni
permiten activar un modelo antiguo. No se atribuyen a este defecto las cero
compras de la ultima PAPER, donde no habia modelo principal aplicado.
El cuaderno se ejecuta; la revision visual completa queda limitada por la vista
disponible. Operador, entrenamiento y AutoResearch apagados; sin otra PAPER,
LIVE, cambios de 0,1 SOL ni de salidas. Rentabilidad futura no demostrada.
[Evidencia y limites](docs/audits/ml_observation_authority_20261016.json).

### Ensayo nativo Windows: clasificar intentos bloqueados (2026-10-16)

El fallo previo se reproduce antes de importar el bot: la dependencia intenta
tres consultas de version de Windows y el sondeo CPU de NumPy, todos denegados.
Ahora se informan por separado solo las consultas nativas exactas durante ese
bootstrap. Ningun subproceso queda permitido; las acciones desconocidas,
incompletas, repetidas, desordenadas o sin propietario siguen fallando.

Pasan 54 pruebas dirigidas y 1.460 regresiones por carpeta, conservando las
1.435 anteriores. Se recogen 7.219 casos oficiales; la nueva CI sigue pendiente.
No cambian produccion, estrategia, 0,1 SOL ni salidas. No se ha arrancado el
operador ni otra PAPER; bot, entrenamiento y AutoResearch quedan apagados.
Se conservan los fallos CI y el aviso SQLite anteriores. Rentabilidad no probada.
[Evidencia y alcance del guardia](docs/audits/native_bootstrap_guard_20261016.json).

### Birdeye: medir la admision, no una observacion posterior (2026-10-16)

Cuatro pruebas podian fallar aun con admisiones estrictamente espaciadas: tres
median el HTTP despues de preparar la sesion y otra leia un valor global que ya
habia actualizado otro hilo. Se registran ahora los recibos bajo el lock nativo,
sin tolerancia, cambio de RPM ni cambios de produccion. Se conservan todas las
aserciones originales y se comprueba que una admision realmente temprana falla.

Los cuatro contraejemplos originales fallan, mientras sus cuatro cohortes de
admision pasan la comprobacion estricta. Pasan 1.435 regresiones por carpeta,
incluidas las 1.393 anteriores, y se recogen 7.194 casos oficiales. La nueva CI
completa sigue pendiente. El fallo historico y el aviso SQLite previo no se
ocultan: sin el trace original no se acredita la causa exacta de aquella CI.
No se ha arrancado otra PAPER ni operado; 0,1 SOL, salidas y modelos no cambian.
El operador queda apagado y la rentabilidad sigue sin demostrar.
[Pruebas y limites de la medicion](docs/audits/birdeye_pacing_measurement_20261016.json).

La CI anterior `dee4bca` tambien registra un fallo separado en Windows: el
bootstrap del ensayo aislado rechaza intentos de sondeo de `platform` bloqueados
por su guardia de subprocesos. El job no llego a ejecutar la suite completa.
Se conserva ese registro; esta correccion de Birdeye no resuelve ese fallo.

### Aceptacion PAPER original antes de preparar LIVE (2026-10-16)

El preflight, el escritor de perfiles y el arranque reconstruyen ahora la caja
PAPER neta original, con quotes, FX y costes; un resumen positivo o un flag
guardado no bastan. La aprobacion manual queda ligada a su cohorte original y se
revalida antes de escribir y arrancar. Los relojes de proceso y saldo son distintos.
No se anaden filtros a las compras PAPER ni se cambian los 0,1 SOL o las salidas.

Pasan 1.393 regresiones por carpeta, con 45 casos nuevos y 7.188 recogidos.
La comprobacion local actual sigue bloqueando LIVE, sin crear perfiles ni operar.
Se conserva una excepcion: SQLite cambio de bytes durante la lectura nativa.
El esquema y las tablas no operativas coinciden con el backup anterior a PAPER2;
se reproduce un checkpoint por lectura, pero la causa historica exacta no queda
probada. No se restaura ni se oculta el aviso. La CI anterior 7b4ac1f termina con
Windows aprobado y un fallo Linux de espaciado Birdeye, aun sin resolver.
La nueva CI completa y la rentabilidad siguen pendientes; el operador queda OFF.
[Pruebas, excepcion SQLite y limites](docs/audits/live_paper_acceptance_20261016.json).

### Replay: diagnostico sin falsa aprobacion financiera (2026-10-16)

El replay conserva los ceros originales y los retornos desconocidos, calcula la
mediana par correctamente y no fabrica captura sin un pico observado. Sus sumas
son puntos porcentuales de observaciones historicas, no SOL, dolares ni retorno
de cartera. La API y la interfaz separan la comparacion heuristica de cualquier
aceptacion financiera; vacios y datos malformados no aprueban el replay.

El original falla 44 de 46 casos iniciales; pasan 1.258 regresiones por carpeta,
sin retirar las 948 anteriores. Hay 59 casos nuevos y se recogen 7.143 oficiales.
Ambos builds UI pasan; esto no equivale a aceptacion visual completa. El cuaderno
ejecuta cinco celdas, con revision visual limitada por el viewport disponible.
La nueva CI completa sigue pendiente. No se regenera el informe del operador,
no se oculta su aviso de cambios ni se toca el supervisor original. Todos los
servicios quedan apagados, con 0,1 SOL y las colas extremas sin techo conservados.
No se ha probado rentabilidad ni autorizado otra PAPER.
[Evidencia y limites](docs/audits/policy_replay_evidence_20261016.json).

### Relojes de nacimiento y discovery sin redondear recibos (2026-10-16)

La fusion de nacimiento, la referencia local de PumpPortal y la admision de
HotQueue conservan ahora segundos numericos originales. La conversion UTC podia
perder un recibo valido o aceptar uno ligeramente futuro; tambien podia descartar
una referencia antes de consultar RPC. No se inventan edades ni se anade tolerancia.

Las tres funciones originales fijadas a Git fallan 19 de 39 casos nuevos.
La reparacion pasa 948 regresiones por carpeta, incluidas las 696 anteriores;
el test nativo repetido pasa 200 de 200 frente a 197 de 200 antes. Se recogen
7.084 casos oficiales; la CI completa de la nueva revision sigue pendiente.
La medicion local de 20.000 pares de reloj no reprodujo discrepancias: no se
atribuye retrospectivamente una causa exacta al fallo de CI o a la PAPER sin compras.
El operador queda apagado, con datos/modelos y tamano de 0,1 SOL conservados.
No se ha iniciado otra PAPER ni demostrado rentabilidad.
[Pruebas, procedencia y limites](docs/audits/birth_clock_sources_20261016.json).

### Edad en cache: conservar la evidencia ya comprobada (2026-10-16)

Se ha reproducido y corregido una perdida de la edad original: un token visto
por otro feed sin repetir su referencia de creacion no aprovechaba la cache RPC
ya verificada. Ahora se reutiliza ese recibo sin renovarlo ni consultar otra vez.
Los desconocidos, caducados o invalidos siguen sin autorizar compras. Los rechazos
futuros incorporan diagnosticos acotados, ajenos a las senales del modelo.

La PAPER original sigue teniendo cero compras: 159 de 291 rechazos registrados
fueron por edad ausente, pero no guardaron detalle suficiente para atribuirlos
al defecto de cache. Las 84 observaciones nuevas no fueron operaciones financieras.
Pasan 696 regresiones por carpeta, incluidas las 406 anteriores y una compra
nativa aislada de 0,1 SOL con cierre y exportacion neta. Se recogen 7.045 pruebas;
la CI completa de esta nueva fuente queda pendiente. No se ha lanzado otra PAPER
ni entrenado/activado modelos. El operador permanece apagado y la rentabilidad
no esta demostrada.
[Evidencia, analisis y limites](docs/audits/birth_cache_coverage_20261016.json).

### Parada: una cancelacion interna no confirma limpieza (2026-10-16)

La revision posterior detecto seis casos en los que una cancelacion dentro de
la limpieza podia confundirse con la parada normal. Ahora se comunica como
fallo conservando su causa, sin alterar la cancelacion legitima del usuario.
Las 406 regresiones por carpeta pasan, incluidas las 400 anteriores sin retirar
aserciones. Se recogen 7.007 pruebas oficiales; la nueva CI completa sigue
pendiente. No se ha arrancado otra PAPER ni cambiado datos/modelos/configuracion.
El operador permanece apagado; no hay aceptacion de rentabilidad ni garantia
de ausencia universal de errores.
[Evidencia del limite de cancelacion](docs/audits/http_cleanup_cancellation_20261016.json).

### Cierre HTTP y logger: defectos reproducidos y corregidos (2026-10-16)

El runner cierra ahora la sesion persistente de Jupiter despues de drenar los
productores. Si falla una limpieza, intenta las demas y no confirma parada
completa con recursos sin cerrar. El cierre conserva la identidad del recurso;
no pierde una sesion fallida o reemplazada. El logger serializa escritura/cierre
y no reabre ni escribe en un archivo ya cerrado; los errores activos y los
diagnosticos en otras salidas siguen visibles.

El original congelado falla 15 de 20 pruebas estrictas; la correccion pasa
400 regresiones por carpeta, incluida una conexion persistente real a localhost
y el ciclo PAPER nativo aislado. Se recogen 7.001 pruebas oficiales y coinciden
los 856 archivos funcionales. La nueva CI completa esta pendiente; no se ha
arrancado otra PAPER ni contactado proveedores. Datos, modelos y configuracion
se conservan; todos los servicios del operador permanecen apagados.
Esta reparacion acotada no demuestra rentabilidad ni ausencia universal de fallos.
[Validacion y limites](docs/audits/http_shutdown_ownership_20261016.json).

### Segunda PAPER: cero compras y fallo de limpieza al salir (2026-10-16)

La revision funcional `4799aa1` aprobo la CI completa antes del arranque:
6.981 pruebas Windows; 6.972 Linux y 9 omitidas por plataforma; 41 de estrategia.
La nueva autorizacion se consumio en una unica PAPER del 15/10 23:40 al
16/10 00:10, hora de Madrid. Se solicito la parada al cumplir treinta minutos.
Compras configuradas en 0,1 SOL, pero hubo cero intentos y cero compras:
el tamano no quedo ejercitado en fills. No hay rentabilidad financiera demostrada.

Se registraron 291 bloqueos: 159 por edad no verificable, 48 por datos de entrada
ausentes y 84 por politica/riesgo. Birdeye devolvio 401 en creacion, pero 200
en overview y pair overview; RPC resolvio 98 consultas de origen en su contador
local. No son facturacion, cobertura total ni oportunidades ganadoras demostradas.
Las 84 nuevas filas de aprendizaje son 83 sombras y un rechazo, no trades.

El propietario salio cooperativamente, sin terminacion forzada, y procesos,
locks y SQLite confirman OFF. Sin embargo, al salir quedaron errores de sesion
HTTP/conector sin cerrar y del logger sobre un archivo cerrado. La limpieza
no esta aceptada como libre de errores. El supervisor conserva su salida 1
por cambios sustantivos en el replay diagnostico historico, no solo en su fecha;
no se restauraron datos ni se activaron politicas o modelos para ocultarlo.
No hubo LIVE, entrenamiento, AutoResearch ni otra prueba posterior.
[Resultados y pendientes](docs/audits/paper_acceptance_20261016.json).

### Repeticion de CI y confirmacion del reloj causal (2026-10-15)

La repeticion del commit documental `8c104c3`, con el mismo codigo funcional
anterior, termino con 2 fallos de Windows: reloj causal de una entrada sintetica
y plazo global de arranque de una prueba. Linux paso. El resultado verde anterior
se conserva como evidencia historica, no como garantia de repetibilidad.

Se corrige un rechazo reproducible entre las conversiones numerica y UTC del
mismo tick: solo se admite una nueva lectura UTC confirmada, sin tolerancia,
timestamp fabricado ni ampliar los limites del reloj. Los fallos no confirmados
aportan diagnostico y conservan el aplazamiento de la entrada.
La prueba de arranque distingue inicio del host y respuesta de la API, aislada
sin servicios operativos. Pasan 230 comprobaciones distintas por carpeta y se
recogen 6.981 casos oficiales. La CI completa de esta correccion aprobo antes
de consumir la nueva autorizacion PAPER de 30 minutos, 0,1 SOL, sin LIVE,
entrenamiento ni AutoResearch. El resultado de esa prueba figura arriba.
[Evidencia y limites](docs/audits/causal_clock_confirmation_20261015.json).

### Software aprobado en ambos sistemas; rentabilidad pendiente (2026-10-15)

La revision funcional `f0f969d` supera la matriz completa: 6.972 pruebas en
Windows; 6.963 en Linux y 9 omitidas por plataforma. Tambien pasan las 41 de
estrategia. Hay un aviso conocido de deprecacion por suite, no fallos.
Los 855 archivos funcionales coinciden con Git y ambas carpetas locales.

Esta aprobacion no demuestra beneficio neto ni captura exhaustiva de oportunidades.
La PAPER anterior no produjo compras y todavia no hay cierres financieros netos
verificables en la poblacion auditada para acreditar un bot ganador.
Otra PAPER requiere nueva autorizacion; no se ha
iniciado. Bot, LIVE, entrenamiento, AutoResearch, API y UI permanecen apagados.
Las entradas cronologicas inferiores conservan sus capturas historicas; este
resumen y la [aceptacion actual](docs/audits/software_ci_acceptance_20261015.json)
aclaran el estado y los requisitos pendientes sin cambiar el codigo funcional.

### Parada: tiempos de arranque y entrega separados en QA (2026-10-15)

La CI anterior conserva dos fallos de Windows al esperar la solicitud de parada.
Una reproduccion con arranque frio controlado de 35 s agota la espera unica de
30 s; no demuestra por si sola la causa exacta de CI. La prueba separa ahora
arranque del controlador y entrega de la solicitud, conserva todos los requisitos
de limpieza real y anade diagnosticos por fase. Los limites de produccion de
15/20 s, la verificacion del proceso y el respaldo forzado no cambian.
Pasan 125 pruebas focalizadas en cada carpeta, incluido el ciclo nativo aislado
de compra, monitor, cierre y exportacion neta. Se recogen 6.972 casos oficiales
en ambos checkouts; la matriz completa de esa revision funcional ya esta aprobada.
[Evidencia y alcance](docs/audits/stop_controller_startup_20261015.json).

### Familias ML: la entrada directa tambien verifica el original (2026-10-15)

El DataFrame pasado directamente a una familia o al aprendizaje del ranking ya
verifica todas las copias financieras antes de generar etiquetas y filtrar fechas.
Asi, una copia invalida no puede desaparecer antes de invalidar su identidad.
Se restauran los predictores originales sin imponer al llamador los filtros de
cohorte de la carga por defecto. Los datos del operador permanecen intactos.

Las diez regresiones reproducidas fallan en el cargador anterior. Pasan 1.037
pruebas focalizadas en cada carpeta, incluidos los consumidores del cargador.
Los informes conservan los rechazos iniciales y la poblacion final por separado.
Se recogen 6.967 casos oficiales en ambos checkouts; la nueva CI sigue pendiente.
Las pruebas usan fuentes sinteticas y destinos temporales; no se entrenan modelos
del operador ni se acredita rentabilidad.
[Contrato y limites](docs/audits/explicit_family_sources_20261015.json).

### Aprendizaje: fuente financiera original antes de filtrar (2026-10-15)

EV, riesgo y la carga auxiliar comun verifican ahora los trades declarados antes
de eliminar etiquetas o seleccionar cohortes. Se recuperan sus entradas originales;
una copia alterada invalida esa identidad completa, incluso entre particiones.
Los alias de diagnostico no rellenan predictores originales ausentes y los IDs
CSV conservan sus ceros iniciales. Los datos del operador no se reescriben.

La matriz de `61451b5` fallo la misma asercion de prueba en ambos sistemas:
comparaba `None` con el valor ausente inferido por pandas. Las pruebas de reloj
y parada pasaron. Se conserva esa ejecucion y se compara ahora el DataFrame
completo antes/despues, sin cambiar la politica de entrada. Pasan 644 comprobaciones
focalizadas en cada checkout y se recogen 6.936 casos oficiales en ambos.
Son dos invocaciones por checkout, no la suite completa; la nueva matriz sigue pendiente.
Sin entrenamiento, otra PAPER ni activacion; no acredita rentabilidad.
[Contrato, reproducciones y limites](docs/audits/financial_cohort_sources_20261015.json).

### Entrada: T0 causal observado, sin tolerar recibos futuros (2026-10-15)

La CI de `107eae2` termina con Linux aprobado y dos fallos en Windows.
Uno reproduce un recibo numerico posterior al T0 truncado a microsegundos;
el otro no observa una solicitud de parada dentro del plazo de la prueba.
Los logs originales se conservan: no se ha reiniciado esa ejecucion.

La entrada ahora espera, como maximo 100 ms de espera admitida, una lectura UTC
real posterior al recibo ya observado. No adelanta T0, altera recibos ni anade
epsilon. Recibos futuros, relojes invalidos y ausencia de confirmacion siguen
aplazando la entrada. La prueba de parada separa su presupuesto de arranque de
los plazos de produccion y conserva la exigencia de cierre sin taskkill.
Pasan 310 comprobaciones focalizadas de runtime y otras 569 de modelos/cohortes
en cada checkout; son varias ejecuciones, no la suite completa. La nueva matriz
completa aun no se ha aprobado.
[Reproduccion, reparacion y limites](docs/audits/causal_entry_clock_20261015.json).

### Aprendizaje: cohortes por estrategia y edad original (2026-10-15)

La reconstruccion configurada de filas sin estrategia ya reconoce el marcador
`unknown` que produce la carga nativa. Cada familia respeta su allowlist: no se
mezclan filas standard, meteor y breakout por compartir un fallback general.
Una edad original declarada desconocida o invalida no se sustituye por un alias.
Las filas explicitas y sus pruebas originales no se reescriben; esta elegibilidad
de entrenamiento no recupera una decision historica ni da permiso para comprar.

La suite por defecto se limita a `tests/`; los simuladores privados de `work/`
siguen disponibles al indicarlos expresamente. La nueva validacion completa
todavia esta pendiente. No cambia el tamano de 0,1 SOL, los filtros de compra,
las salidas ni la activacion de modelos. No se acredita rentabilidad.
[Contrato y evidencia](docs/audits/primary_lane_cohort_20261015.json).

### Parada normal: solicitud propia, drenaje y confirmacion (2026-10-15)

El arranque nativo publica una generacion de parada propia junto a su PID real.
El script `scripts/stop_stack.ps1` sin `-Force` solicita primero la parada y
espera a que termine la limpieza del runtime. Solo confirma esa limpieza si
coinciden el recibo de cierre y la salida del proceso original; ni un archivo
de solicitud ni la desaparicion del proceso bastan por separado.

La espera cooperativa es de 15 segundos, con limite externo del solicitante de
20 segundos para conservar el respaldo ante IO atascado. Se mantienen la
verificacion del proceso, el respaldo forzado y `-Force` como emergencia directa.
Esto cubre `_runner`, incluido su inicio, no las importaciones previas de Python.
Los botones/API que pidan `force=true` conservan su comportamiento de emergencia.
La prueba aislada de Windows no usa proveedores ni el bot operativo; el
finalizador SQLite se valida aparte. Pasan 191 pruebas focalizadas en cada
checkout; la CI posterior fallo en Windows y su reparacion se documenta arriba.
[Contrato, pruebas y limites](docs/audits/cooperative_stop_20261015.json).

### Scanner: esperar al cupo local sin reevaluar cada 5 segundos (2026-10-15)

La admision local de intentos Pump.fun distingue cooldown por token, ventana
de intentos y reloj invalido. El siguiente intento se programa segun la espera
restante, sin cambiar la edad original, los filtros ni el tamano de 0,1 SOL.
El estado retenido queda acotado y una ventana mayor de 64 intentos ya no puede
perder su limite por truncar el historial. Se conservan los valores del operador:
4 intentos por 10 segundos y 60 segundos de cooldown. Estas cifras no son CUs.

Cuatro regresiones nativas reproducidas en la revision publicada anterior;
58 casos nuevos y 745 pruebas focalizadas pasan en cada checkout. La revision
`572f4d5` supera 6.711 pruebas completas en Linux y otras 6.711 en Windows.
Esa matriz no valida por anticipado la posterior integracion de parada. No se arranca el bot.
Evidencia: [admision local](docs/audits/pumpfun_local_admission_20261015.json).

### PAPER autorizada del 15-10: mas datos de origen, todavia cero compras

La prueba nativa de 30 minutos registra 477 evaluaciones, cero intentos de compra,
cero posiciones nuevas y 103 resoluciones de creacion por RPC. Los 345 bloqueos
se reparten en edad no verificada (144), snapshot ausente (46), espera local
Pump.fun (70) y otras reglas/riesgos (85). Son eventos, no tokens unicos ni
oportunidades rentables perdidas demostradas. Birdeye creacion sigue en HTTP401.
Se guardan 82 cierres shadow y 3 rechazos, no 85 trades financieros.

La parada al plazo requirio terminar el arbol verificado; procesos y bloqueos
quedan a cero, con SQL stopped/idle/idle. Sin LIVE, entrenamiento, AutoResearch,
activacion ni cambios de clave/plan. No hay rentabilidad demostrada. La correccion
del scanner anterior es posterior a este run y aun no tiene otra PAPER autorizada.
Evidencia: [aceptacion operativa](docs/audits/paper_acceptance_20261015.json).

### ML Center: diagnosticos de eventos, no permiso LIVE (2026-10-15)

El checklist historico de filas, AUC y porcentajes de eventos no acredita
rendimiento neto ni autoriza una estrategia LIVE. La API mantiene `ml_live_ready`
y sus permisos en falso y separa el resultado diagnostico del checklist. Los
recuentos son eventos de una cola acotada, no operaciones financieras unicas;
el panel muestra esa limitacion expresamente.

Los porcentajes booleanos/no finitos ya no contaminan la media o la tasa de
acierto. La envoltura de la API ya protegia JSON convirtiendo no finitos a null;
no se ha demostrado un HTTP500 previo. La media conserva valores finitos
extremos sin recorte ni desbordamiento. No cambian compras, salidas, entrenamiento
ni activacion. [Contrato y evidencia](docs/audits/ml_api_evidence_20261015.json).

### Apagado: el exportador conserva su hilo hasta terminar (2026-10-15)

Una cancelacion repetida ya no abandona el hilo del exportador de cierres.
El supervisor cancela cada propietario una vez y espera su limpieza sin que
otra cancelacion del padre la interrumpa. Si falla la creacion de tareas o el
productor de corutinas, se cierran las no admitidas y se drenan las ya creadas.
La excepcion o cancelacion se comunica despues de completar esa limpieza.

La reproduccion nativa anterior podia publicar `stopped` antes de terminar una
escritura aislada; ahora el estado detenido espera al propietario. Esto no
impone un tiempo maximo al sistema de archivos ni permite matar hilos nativos.
No cambia filtros, compras de 0.1 SOL, salidas ni activacion de modelos.
[Evidencia y limites](docs/audits/export_owner_shutdown_20261015.json).

### Evidencia de aprendizaje: antes y despues de la PAPER del 15-10

La comprobacion posterior conserva 7.877 observaciones: 5.438 rechazos y 2.439
sombras. Las 85 nuevas coinciden con la ventana PAPER y 83 tienen recibos de
entrada actuales. El T0 mas reciente es del 15 de octubre; siguen siendo cero
las filas con retorno financiero neto verificable. No hay principal, metadatos
ni registro principal configurados. No se han entrenado o activado para rellenarlos.

La captura anterior de las 07:14 UTC, previa a la PAPER, contenia 7.792 registros:
5.435 rechazos de politica y 2.357 cierres de sombras, no operaciones financieras
independientes. Su T0 mas reciente era del 4 de octubre. Ninguna fila acreditaba
retorno neto con el contrato financiero actual ni el recibo actual de entradas.
El principal configurado, sus metadatos y su registro no estan presentes; los
29 metadatos historicos inspeccionados no acreditan el contrato financiero actual.

Es una captura del operador local, no un requisito de instalacion ni prueba de
que no existan otros modelos auxiliares. El modo shadow y la autopromocion
apagada no demuestran una causa de cero compras. No se han reconstruido datos,
entrenado pesos o activado procesos para rellenar estas carencias. La evidencia
prospectiva requiere una nueva validacion autorizada y conservada.
[Fuentes, alcance y limites](docs/audits/learning_readiness_20261015.json).

### Salidas: contexto original y colas extremas en sombras diagnosticas (2026-10-15)

Las nuevas sombras fijan carril, contexto de decision, ranking recibido, modo
original y politica de caida de precio antes de esperar al proveedor. El
subperfil solo se recupera de su recibo T0 v4 valido. Las salidas nativas reciben
este contexto, el retorno bruto del prefijo parcial y el estado observado;
sus cambios de fase y excursion adversa se guardan tambien para reinicios.

En modo PAPER se conserva la politica de runner configurada al abrir, incluso
tras reiniciar o cambiar la configuracion. Se prueban subidas sinteticas hasta
+1.000.000% y la diferencia entre retroceso de precio y puntos de beneficio.
Una sombra sigue siendo una simulacion no financiera de un unico parcial
fraccional: no inventa cantidades, fills, comisiones, net cash ni varias ventas.
Los estados antiguos no reciben contexto reconstruido a posteriori.

Tambien se corrige una excepcion real de las salidas al convertir un dato
ausente con default `None`: permanece desconocido, sin `float(None)` ni cero
fabricado. Se conserva el comportamiento de los defaults numericos existentes.
No se cambian umbrales de compra, importe de 0.1 SOL ni se activa el bot.
[Evidencia y limites](docs/audits/shadow_exit_context_20261015.json).

### Aprendizaje: el diagnostico posterior no sustituye al vector original (2026-10-15)

Las sombras de investigacion conservan los 77 valores originales y sus tres
proof attrs. Los datos posteriores de filtros y rechazos viajan por separado
como `decision_context`, sin sobrescribir ni rellenar predictores historicos.
El perfil de salida de la sombra sigue eligiendose con el contexto de decision
existente y queda fijado antes de encolar; no cambia la politica de compra.

Se comprueba la cadena nativa hasta checkpoint, reinicio, cierre, Parquet y
lector de modelos. Los ceros originales de edad y liquidez proxy ya no se
reemplazan por un alias. Un fallo al congelar la muestra no entrega un objeto
mutable como alternativa. No se reescriben datos previos ni se entrena o activa
ningun modelo. Es una reparacion de integridad, no evidencia de rentabilidad ni
una causa demostrada de las cero compras.
[Evidencia y limites](docs/audits/research_shadow_t0_20261015.json).

### Entrada: las sombras no esperan precios ni disco en el evaluador (2026-10-15)

La apertura auxiliar de sombras se admite en una cola propia de un consumidor,
limitada a 64 trabajos pendientes incluido el activo. Vector, proof attrs,
contexto, perfil y metadatos originales se congelan antes de encolar. El
evaluador sigue atendiendo candidatos sin esperar el precio o la persistencia
de esa sombra; el monitor y la compra real conservan sus propietarios propios.

Las reservas pendientes cuentan en los limites de investigacion existentes sin
duplicar las posiciones ya abiertas. Saturacion, caducidad y errores se cuentan
como tales: admision en memoria no significa apertura durable ni compra. El
ejecutor supervisa y drena este propietario antes de publicar `stopped`.
La ruta de investigacion conserva tambien los tres proof attrs de la Series.
No se modifican filtros de compra, importe de 0.1 SOL ni politica de salidas.
[Evidencia y limites](docs/audits/shadow_open_admission_20261015.json).

### Reinicios: sombras abiertas y parciales con estado durable (2026-10-14)

Las nuevas sombras guardan su vector original y sus proof attrs, referencia de
entrada, perfil de salida, contexto, pico y prefijo del parcial. Cada direccion
tiene un slot con identidad de instancia, checksum y generacion; una escritura
vieja no puede reemplazar una nueva ni cambiar la entrada o un cierre congelado.

El monitor recupera estas muestras con presupuesto y cursor antes de seguirlas.
Los eventos pendientes conservan su identidad y hora original, y un cierre ya
completado se retira sin consultar otro precio ni dejar abierto su contexto.
El portfolio historico incompleto no se transforma en un estado inventado.
La durabilidad empieza despues de la escritura atomica confirmada, no antes;
archivos corruptos quedan pendientes para diagnostico. Es seguimiento de sombras
no financieras, no recuperacion de fills LIVE ni prueba de rentabilidad.
[Evidencia y limites](docs/audits/shadow_state_recovery_20261014.json).

### Aprendizaje: captura desconocida no significa captura del 0% (2026-10-14)

`capture_ratio` conserva un valor desconocido cuando falta el retorno, falta
un pico positivo finito o la division no es finita. Un retorno realmente
observado de cero con pico positivo sigue dando cero; los retornos con signo
y las magnitudes extremas no se recortan en esta etiqueta. La validacion neta
financiera existente sigue teniendo prioridad sobre aliases brutos.

Se verifican el constructor de etiquetas, la carga de familias sin fit, los
roundtrips CSV/Parquet y la cadena nativa de cierre de sombras. No se corrigen
datos historicos ni se activan modelos. No hay evidencia de que esta etiqueta
fuera la causa de las cero compras ni de rentabilidad real.
[Evidencia y limites](docs/audits/capture_ratio_observation_20261014.json).

### Aprendizaje: ausencia de precio no es un pico observado de cero (2026-10-14)

Una sombra sin referencia de entrada positiva y finita conserva su pico como
desconocido, no como 0%. Precios cero, negativos, infinitos y booleanos no se
usan para calcular retornos. Si llega una referencia positiva por el fallback
existente, se conserva el cero inicial real; no se rejuvenece una entrada al
cerrarla con un precio posterior.

Una referencia valida conserva sus picos conocidos aunque el precio final
falte, incluidos picos superiores al 10.000%. Se verifica apertura, cierre,
outbox, Parquet y etiquetas de runner: la ausencia sigue siendo ausencia.
No se reescriben datasets, modelos ni observaciones cerradas historicas.
[Evidencia y limites](docs/audits/shadow_peak_observation_20261014.json).

### Entrada: stages y decisiones con escritor propio (2026-10-14)

Los registros de investigacion de candidatos ya se preparan en la entrada y
se escriben desde una cola propia, limitada a 256 trabajos incluido el activo,
con un solo hilo. Hora, contexto y valores originales se congelan al admitir
la muestra; la espera por el journal no bloquea estas llamadas de entrada.

Admision, persistencia, fallo, descarte y pendiente se distinguen. Saturacion
o fallo no dejan una deduplicacion falsa; un fallo antiguo no borra una
admision nueva. El contexto de una compra real ya confirmada queda disponible
antes de la escritura, tambien si su telemetria se descarta o deduplica.
Los cierres financieros no entran en esta cola diagnostica en memoria.

La parada drena el escritor antes de `stopped` y conserva los errores incluso
si deben terminar otras limpiezas primero. Todavia hay otros sinks sincronos,
y no se promete conservar diagnosticos ante saturacion o caida del proceso.
La CI anterior fallo en Windows en coherencia de edad/RPC; el motivo exacto
sigue sin estar demostrado. Se conservan los asserts y se amplian diagnosticos.
[Evidencia y limites](docs/audits/candidate_event_persistence_20261014.json).

### Cierres shadow: observacion congelada y recuperacion (2026-10-14)

El seguimiento de sombras tiene un propietario serial independiente de la
entrada. Al cerrar, congela features, resultado y hora de disponibilidad tras
la consulta de precio. El IO se ejecuta en un hilo cuya terminacion sigue
siendo propiedad del bot, incluso ante cancelaciones repetidas.

Un outbox atomico conserva el cierre antes de publicarlo. Los reintentos y el
reinicio no vuelven a calcular el precio; Parquet y journal usan una identidad
de observacion no financiera. Solo se retira la sombra tras confirmar ambos.
Un cierre antiguo no cierra una apertura nueva, tampoco tras reiniciar.

Se verifican ventanas de fallo y recuperacion con archivos temporales y un
proceso nuevo. Esta etapa no certifica beneficio, precio ejecutable ni todo el
rendimiento del scanner: otros journals sincronos pueden contender por sus
locks, y las aperturas/parciales aun no tienen recuperacion durable completa.
[Auditoria y limites](docs/audits/shadow_close_recovery_20261014.json).

### Cierres simulados y picos conocidos sin retorno final (2026-10-14)

Un cierre sin precio/retorno observado conserva `label=null`: no se convierte
en una perdida. Los valores del cierre prevalecen sobre contexto antiguo y el
contexto de apertura se conserva si falla el journal o el portfolio; un cierre
antiguo tampoco elimina una apertura nueva del mismo token.

El lector de familias conserva filas sin etiqueta final. Un pico conocido
puede aportar una etiqueta de runner, incluso por encima del 10.000%, sin
inventar retorno realizado. Siguen vigentes los filtros de muestra, lane, DEX,
tiempo y prueba financiera. El modelo primario sigue exigiendo su etiqueta.

Pasan 251 pruebas enfocadas por carpeta, incluidos 20 casos nuevos; se recogen
6.347 casos de repositorio. La CI completa de esta revision queda pendiente.
No se ha ejecutado entrenamiento operativo ni trading. Aun quedan por cerrar
la escritura sincrona y la recuperacion/idempotencia completa de estos cierres:
un evento escrito antes de un fallo del portfolio puede repetirse al reintentar.
[Evidencia y limites](docs/audits/shadow_close_observation_integrity_20261014.json).

### Scanner: rechazos sin escritura de disco en la entrada (2026-10-14)

Las muestras de rechazo de politica pasan ahora por una cola propia limitada
a 128 trabajos pendientes, incluido el activo, y un unico escritor fuera del
bucle de eventos. Se congela la feature original antes de devolver el control.
Los cierres financieros y sus etiquetas netas no pasan por esta cola.

Admision, persistencia confirmada, fallo, pendiente y muestra descartada tienen
contadores separados. La saturacion descarta muestras diagnosticas nuevas, no
las presenta como guardadas; tampoco bloquea su reintento en la deduplicacion.
La parada cierra admisiones, drena los trabajos y el executor antes de publicar
`stopped`, incluso ante cancelaciones repetidas. Es una cola en memoria, no un
outbox durable contra una caida completa del proceso.

Pasan 357 pruebas enfocadas por carpeta, con 37 casos nuevos; se recogen
6.327 casos de repositorio. La revision anterior pasa 6.290 pruebas completas
en Windows y Linux; la CI completa de este nuevo codigo sigue pendiente.
Todo queda apagado y los datos/modelos/env intactos. Otros IO sincronos,
latencia bajo carga real y rentabilidad siguen pendientes.
[Auditoria y limites](docs/audits/policy_feature_persistence_20261014.json).

### Datos de aprendizaje: escritura con propiedad del SO (2026-10-14)

El almacen Parquet ya no decide que un escritor ha terminado por la antiguedad
de su archivo de bloqueo. Un guard estable del sistema operativo cubre lectura,
modificacion, publicacion y limpieza; su existencia no significa que siga activo.
Los marcadores antiguos con propietario vivo o desconocido se conservan. Una
caida libera la propiedad del SO y permite recuperar el marcador nativo; no se
borran recibos ajenos durante la liberacion. No se cambia la politica de trading.

Tambien se desacopla la importacion de utilidades: el guard y el bootstrap del
entorno no cargan mercado/ML antes de necesitarlos; se conservan sus exportaciones.
Pasan 232 pruebas enfocadas por carpeta, con 38 casos nuevos, y se recogen
6.290 casos de repositorio. La CI completa de esta fuente sigue pendiente.
La revision anterior de diagnosticos RPC `e872777` pasa 6.237 pruebas completas
en ambos sistemas; no explica ni borra el fallo historico de Windows.
Bot, entrenamiento y servicios quedan apagados, con datos/modelos/env intactos.
La latencia de escritura en la ruta de entrada y la rentabilidad siguen pendientes.
[Auditoria y limites](docs/audits/feature_writer_ownership_20261014.json).

### Aprendizaje: particiones CSV/Parquet completas (2026-10-14)

La existencia de un Parquet ya no oculta los CSV de otros meses. Entrenamiento,
estado de frescura, informes de segmentos, preflight y recibos de auditoria
comparten una seleccion por particion: Parquet prevalece sobre su replica CSV
del mismo nombre, sin duplicar filas ni mezclar versiones de una misma particion.
No se inventan horas de decision ni se aceptan etiquetas financieras nuevas.

El fallo se reproduce con seis pruebas fallidas antes de corregirlo. Pasan
181 comprobaciones enfocadas por carpeta, con 15 casos nuevos; se recogen
6.252 casos de repositorio (mas una fixture privada solo en la carpeta de trabajo).
Tambien pasan las 26 pruebas del ciclo PAPER nativo aislado en ambas carpetas;
son datos sinteticos, sin proveedores ni operaciones reales.
La CI completa de esta nueva fuente sigue pendiente. En los datos actuales no
hay una particion CSV distinta perdida; este fallo no demuestra la causa del
anterior run sin compras. El modelo primario sigue ausente y la rentabilidad
no esta acreditada. Todo permanece apagado y los datos/modelos/env intactos.
[Auditoria y limites](docs/audits/feature_partition_selection_20261014.json).

### PAPER nativo: fallo Windows conservado y diagnosticos (2026-10-14)

La CI `fb9a14d` fallo en una prueba de nacimiento RPC/entrada de 0,1 SOL en
Windows (6.223 correctas y una fallida); Ubuntu paso 6.224. La causa no esta
demostrada: ocho repeticiones locales pasan y no se consideran una reparacion.
El fallo ahora conserva cantidades, llamadas, estado RPC y motivos de decision.
Una inyeccion de RPC no disponible exige fallo real, cero posiciones y
`entry_observation:missing_age`, sin aflojar la asercion positiva ni los filtros.

Pasan 291 comprobaciones enfocadas por carpeta y se recogen 6.237 casos; la
CI completa de esta nueva revision sigue pendiente. El cambio es de
diagnosticos/pruebas, no reparacion de la causa ni evidencia de beneficios.
Todo permanece apagado y los datos/modelos/env estan preservados.
[Auditoria y limites](docs/audits/native_rpc_paper_diagnostics_20261014.json).

### Preflight: alcance explicito y resultados conservados (2026-10-14)

`ok` acredita comprobaciones auxiliares de software/perfiles/informes,
no arranque operativo, acceso a proveedores ni rentabilidad. Pytest distingue
ejecucion real, fallo de lanzamiento, ausencia de ejecucion y la afirmacion
del lanzador: `--external-pytest-passed` ya no inventa un comando o exit0.
`--status-path ruta.json` permite resultados atomicos de auditoria sin
sustituir el estado operativo; el destino habitual no cambia.

Pasan 55 pruebas enfocadas por carpeta, con 12 casos nuevos, y cuatro
comprobaciones de CLI auxiliar real (sin pytest ni runtime). Se recogen
6.236 casos. La revision anterior `eb5e5a2` pasa sus 6.209 casos completos
en Windows y Ubuntu; la CI de esta nueva fuente sigue pendiente. Todo queda
apagado, con datos/modelos/env y estado de preflight operativo preservados.
[Auditoria y limites](docs/audits/preflight_acceptance_scope_20261014.json).

### Aprendizaje: un ciclo compartido y trabajadores supervisados (2026-10-14)

El control del bot y el daemon comparten ahora el ciclo de entrenamiento.
Las entradas publicas del modelo principal y del ranking respetan el mismo
bloqueo del sistema operativo, incluido un propietario ajeno. Una mejora
del ranking no se anuncia como promocion del modelo principal. Al cancelar
se espera al trabajador admitido antes de liberar el ciclo; no se mata el
hilo ni se promete una latencia de parada. Las comprobaciones financieras
y de activacion permanecen intactas, y no se habilita entrenamiento.

Pasan 338 pruebas enfocadas por carpeta, con 15 casos nuevos; se recogen
6.224 casos. La revision anterior `52f7560` pasa sus 6.113 casos completos
en Windows y Ubuntu; la CI completa de esta nueva fuente sigue pendiente.
Todo permanece apagado y datos/modelos/env intactos. No acredita beneficios.
[Auditoria y limites](docs/audits/training_cycle_integration_20261014.json).

### Validacion Windows: recuperacion SQL sin SLA artificial (2026-10-14)

La CI anterior `d78b2fe` fallo en Windows porque una entrada prevista como
correcta agoto los 100ms de una prueba SQLite. Ese fallo sigue conservado.
La prueba usa ahora plazos aislados para la transaccion suspendida y el
trabajo correcto, y exige inserciones reales, cancelacion, rollback,
sesiones independientes y que solo persista la entrada posterior. Se añade
una variante con pausa de 200ms; no se modifica el timeout del bot.

Pasan 409 comprobaciones enfocadas en ambas carpetas y se recogen 6.209
casos. La CI completa de esta nueva version sigue pendiente. Todo permanece
apagado, con datos/modelos/env intactos. No es prueba de latencia productiva,
fills reales ni rentabilidad.
[Auditoria y limites](docs/audits/native_sql_timeout_validation_20261014.json).

### Scanner: nacimiento nuevo y caducidad coherente (2026-10-14)

La cola prioritaria reconoce ahora los mismos alias y unidades de nacimiento
que la entrada comun, incluido `token_age_min`. Un nacimiento o referencia
de creacion nuevo puede volver a evaluarse tras el intervalo existente,
aunque el precio no cambie. La recepcion tardia de la misma referencia no
basta para saltarse la deduplicacion. Se conservan la espera original, los
limites y los filtros finales; una referencia no se convierte en nacimiento
ni permiso de compra, y un nacimiento conocido no se rejuvenece.

Pasan 604 pruebas enfocadas en ambas carpetas, con 95 casos nuevos. Se
recogen 6.208 casos; la CI completa de esta version sigue pendiente. Los
procesos permanecen apagados y los datos/modelos/env intactos. No se ha
demostrado que este defecto causara las compras ausentes ni rentabilidad.
[Auditoria y limites](docs/audits/hot_queue_birth_reconsideration_20261014.json).

### Autoentrenamiento: propiedad del ciclo y estado historico (2026-10-13)

El ciclo del daemon usa ahora un bloqueo del sistema operativo: el TTL no
puede desplazar un entrenamiento activo y un llamante ajeno no puede liberarlo.
Una caida libera el bloqueo; el archivo `.training.lock.guard` se conserva como
ancla estable, no como prueba de un proceso activo. Los recibos antiguos
desconocidos se preservan. Un intento bloqueado escribe su diagnostico en
`training_daemon_status.json`, sin sustituir el estado del entrenador activo.

Se reproducen cuatro fallos antes de reparar; pasan 198 pruebas enfocadas
en ambas carpetas, incluidos 28 casos nuevos. Se recogen 6.113 casos; la CI
completa de esta nueva version sigue pendiente. No se ha entrenado ni activado
ningun modelo operativo, ni consultado proveedores. Esta reparacion no
demuestra rentabilidad ni completa el objetivo financiero.
[Auditoria y limites](docs/audits/training_cycle_ownership_20261013.json).

### Gecko: consultas sincronas compartidas y acotadas (2026-10-13)

`dafbbf8` repara la duplicacion de consultas sincronas compatibles. El
propietario trabaja en el hilo del llamante, con limites de 16 propietarios
y 256 llamadas en espera; una espera agotada no cancela al propietario.
La capacidad conjunta, la generacion y la invalidacion de cache se reservan
solo tras admitir la peticion, manteniendo los recibos originales y copias
independientes. No cambian filtros, importe PAPER ni contratos financieros.

Pasan 305 pruebas enfocadas en ambas carpetas y los tres casos del diagnostico
original. La version asincrona anterior pasa sus 6.047 pruebas completas;
la suite actual de 6.085 casos tambien pasa en ambos sistemas sobre `dafbbf8`
(Ubuntu: 1.571,73 s; Windows: 1.822,98 s; un aviso Starlette por sistema).
Los comandos de modelo ausente y replay sin datos son diagnosticos permitidos,
no aceptacion de un modelo operativo ni rentabilidad. Todo el bot,
entrenamiento, AutoResearch y API/UI permanece apagado, con datos/modelos/env
intactos. No es evidencia de rentabilidad ni de latencia operativa del proveedor.
[Auditoria y limites](docs/audits/gecko_sync_ownership_20261013.json).

### Antecedente: primera reparacion asincrona de Gecko (2026-10-13)

Una respuesta antigua podia sustituir la cache nueva si su tarea arrancaba
mas tarde; ademas, un refresco rechazado por capacidad borraba un recibo
valido. `4aed559` vincula generacion, capacidad e invalidacion a la admision
del propietario, antes de arrancar la tarea, y libera su reserva tras el
cleanup incluso cuando la tarea nunca llega a iniciarse.

Las 206 pruebas enfocadas pasan en ambas carpetas, con 11 casos nuevos.
En esa etapa la suite de 6.047 casos y la duplicacion sincrona estaban
pendientes; sus avances posteriores constan en la seccion anterior.
No se han relajado filtros ni contratos financieros, ni arrancado
el bot. [Evidencia y limites](docs/audits/gecko_admission_20261013.json).

### Antecedente: instalacion limpia Windows/Linux y fallos de CI (2026-10-13)

Se conservan los fallos reales de CI y sus reparaciones: ABI PyArrow/NumPy,
imports de paquetes del proyecto, SDK Solana sincronico, identidad escalar de
cohortes, nulos de Pandas y comprobaciones portables de API/PowerShell. El
bootstrap de las utilidades de pruebas de NumPy registra por separado su
sonda CPU bloqueada; no permite ejecutar subprocesos ni acceder a red externa.
Los filtros de entrada/riesgo, las compras PAPER exactas de 0,1 SOL y los
contratos financieros no se han relajado.

Una instalacion limpia real de Python 3.12.15 pasa 6.031 pruebas en ambos
sistemas (commit `9f59015`: Linux 1.574,08 s; Windows 1.800,59 s; un aviso
Starlette por sistema). La UI instala y compila
en Windows 2022 y Ubuntu 24.04 sobre `574ac59`: el lockfile incorpora solo tres
entradas faltantes y el modulo estatico de rutas ya pertenece a Git. Antes
existia localmente pero estaba ignorado, por lo que el build local no probaba
que un clon nuevo funcionara. El sincronizador ahora avisa de fuentes sin
tracking, sin auto-staging ni sobrescritura de cambios del propietario.

La matriz posterior de 6.036 casos detecto dos pruebas antiguas con una
simulacion incompleta de Git (ambos sistemas: 6.034 aprobadas, dos fallidas). La
correccion `5fcb8e5` conserva los controles reales de secretos y tracking;
sus 30 pruebas afectadas pasan en ambas carpetas y la regresion de estrategia
pasa 41. La nueva matriz completa Windows/Linux sigue ejecutandose y no se
presenta como aprobada. El build conserva
un aviso de chunk JavaScript de aproximadamente 524,83 KB; no acredita
validacion visual, acceso a proveedores, modelo operativo ni rentabilidad.
Bot, entrenamiento operativo, AutoResearch, API/UI y QA local permanecen
apagados; datos, modelos y `.env` siguen intactos frente al baseline de esta
etapa. El objetivo completo y la aceptacion financiera siguen abiertos.
[Auditoria de primer arranque y fallos preservados](docs/audits/first_launch_clean_ci_20261013.json).

### Creacion de mint por RPC: validacion nativa completa (2026-10-13)

La referencia original de creacion PumpPortal ya llega a la entrada comun y
puede resolverse por una consulta RPC de la transaccion exacta, antes del
fallback a Birdeye. La firma sola no basta: se comprueban la estructura nativa,
firmas, identidad y escritura del mint, inicializacion Token/Token2022 y los
datos de instrucciones internas/cuentas cargadas reportados por el nodo.
Los recibos se reconstruyen al aceptarlos y mantienen presupuesto/cache
acotados, cancelacion con limpieza y nacimiento original hasta la exportacion
T0/neto, sin anadir predictores, relajar filtros ni modificar el `.env` personal.

154 casos nuevos y450 pruebas focalizadas pasan en cada copia. La primera
bateria completa paso6.009 casos en C y fallo una comprobacion de paridad de
claves opcionales de `.env` en D; se conserva ese resultado fallido. La
comprobacion ahora reconoce exactamente los ajustes RPC documentados con
defaults nativos, sin cambiar secretos, omitir duplicados, flags obligatorios
o diferencias ajenas a RPC. La nueva bateria nativa pasa los mismos6.012 casos
del repositorio por copia (D suma una prueba privada), en1.462,89 s en D y
1.442,31 s en C, con los dos avisos de pandas conocidos. Compilacion,
configuracion,27 imports base mas6 modulos RPC verificados y siete informes
vacios por copia pasan sobre805 fuentes congeladas identicas. El entorno
instalado Python3.12.3 pasa `pip check`. Los archivos protegidos siguen sin
cambios frente al baseline de esta etapa; no se borra la alerta historica de
bytes de SQLite. Bot y QA quedan apagados. No se declara terminado el objetivo.
La ruta PAPER
completa aislada compra
0,1 SOL y conserva el runner y el recibo hasta el cierre/exportacion; las rutas
con nacimiento ausente, fallido o futuro no compran. Son datos sinteticos, sin
red externa ni operaciones reales. El bot, LIVE, entrenamiento operativo y
AutoResearch y QA quedan apagados.
Acceso real, cobertura de firmas/oportunidades, modelo primario y rendimiento
neto siguen sin quedar aceptados.
[Auditoria RPC y estado verificable](docs/audits/rpc_mint_birth_20261013.json).

### Gecko y limites compartidos: reloj y admision atomica (2026-10-13)

El falso retroceso reproducido en la etapa anterior esta corregido: Gecko y el
limitador de ventana leen el reloj dentro del mismo bloqueo que decide el
permiso. Los callers sync/async e hilos mantienen los limites existentes,
recomprueban permiso al despertar y no reservan turnos futuros al cancelarse.
La ventana valida tambien el reloj inicial; un NaN ya no puede dejar un
presupuesto roto despues de recuperar el reloj. No se relajan tasas, filtros,
recibos de nacimiento, politicas ni compras PAPER exactas de 0,1 SOL.

32 casos nuevos y 482 pruebas focalizadas pasan en ambas copias. El preflight
completo nativo pasa las mismas 5.858 pruebas del repositorio por copia
(D incluye una privada): 1.430,70 s en D y 1.405,12 s en C, con dos avisos de
pandas ya conocidos. Compilacion, configuracion, 27 imports y siete informes
vacios por copia se verifican sobre 798 fuentes congeladas identicas.
Los archivos protegidos siguen sin cambios frente al baseline de esta etapa,
sin borrar la alerta historica de bytes de SQLite. Bot y QA quedan apagados.

Al cerrar esta etapa anterior, el parser perdia las posibles referencias de
transaccion y el enriquecimiento tipado dependia de Birdeye. Una prueba
sintetica confirmo ese contrato, no la disponibilidad real de firmas en el
feed. Ese limite se aborda en la implementacion RPC posterior indicada arriba:
una fecha de bloque o cuenta asociada no basta y no implica permiso de compra.
Acceso real, modelo primario, cobertura de oportunidades y rendimiento neto
siguen sin quedar aceptados.
[Auditoria de relojes y siguiente limite de datos](docs/audits/shared_pacing_clock_20261013.json).

### Birdeye: limitador entre bucles/hilos y orden de respuestas (2026-10-13)

El fallo del limitador async de la etapa anterior esta corregido. Los tres
recursos comparten admision monotonic protegida entre hilos y bucles, sin
mantener el bloqueo durante una espera ni reservar turnos futuros. Cada
despertar comprueba de nuevo el permiso; una cancelacion en espera no consume
otro turno. Un reloj local invalido falla cerrado y un retroceso espera un
intervalo completo. Un RPM opcional desmesurado ya no impide importar el modulo.

Un 401/403 recibido ahora bloquea las consultas nuevas que todavia esperan,
aunque ya hayan sido admitidas. El estado sigue la respuesta completada mas
reciente, no solo la admision: un resultado anterior no pisa otro posterior
completado. Se mantienen coalescencia, cache actual, recuperacion por recurso,
recibos originales, controles de riesgo y compras PAPER exactas de 0,1 SOL.

36 casos nuevos y 440 pruebas focalizadas pasan en ambas copias. El preflight
completo nativo pasa las mismas 5.826 pruebas del repositorio por copia
(D suma una prueba privada): 1.699,29 s en D y 1.671,30 s en C, con dos avisos
existentes de pandas. Compilacion, configuracion, 27 imports y siete informes
vacios por copia quedan verificados sobre las mismas 797 fuentes congeladas.
Los archivos protegidos permanecen sin cambios respecto al nuevo baseline;
la alerta historica de bytes de SQLite de la etapa anterior no se borra.

El fallo independiente de Gecko/ventana reproducido en esta etapa se corrige
en el bloque posterior indicado arriba. El acceso real a creacion de Birdeye
y la aceptacion financiera siguen pendientes.
Estas pruebas no certifican cuota real, latencia de red, ocurrencia historica
ni beneficios. Bot, QA, LIVE, entrenamiento y AutoResearch quedan apagados.
[Auditoria del limitador y limites pendientes](docs/audits/birdeye_throttle_20261013.json).

### Birdeye: acceso por recurso y precision de recibos (2026-10-13)

Los HTTP 401/403 ahora se distinguen de la ausencia de datos de un token.
El recurso denegado entra en espera y admite una sola prueba de recuperacion;
el refresh no salta ese limite y los recursos sanos siguen independientes.
El estado y el plazo aparecen en las metricas nativas, sin claves ni cuerpos de
respuesta. El nuevo ajuste opcional `BIRDEYE_ACCESS_RETRY_S` vale 300 segundos
por defecto, limitado a 1-3.600. No se modifica tu `.env` ni el plan de la cuenta.

El reloj numerico original en segundos ya no pierde precision al validar un
recibo de nacimiento: igualdad exacta pasa y una fecha futura sigue fallando,
sin tolerancia anadida ni edades fabricadas. Los recibos positivos originales,
presupuestos de creacion, controles de compra y PAPER exacto de 0,1 SOL permanecen.

Tras corregir incompatibilidades detectadas en la primera regresion completa,
el preflight nativo pasa las mismas 5.790 pruebas del repositorio por copia
(D incluye ademas una prueba privada), con dos avisos existentes de pandas.
Las fuentes finales, 27 imports y siete informes vacios por copia se verifican.
La alerta anterior de bytes de SQLite queda documentada: solo se observa una
columna adicional y retencion de registros en la comparacion disponible; no se
declara igualdad historica de bytes ni se restaura la base.

El fallo del limitador reproducido en esta etapa se corrige en el bloque
posterior indicado arriba. El acceso real y la aceptacion financiera siguen pendientes.
No se ha arrancado otro PAPER, LIVE, entrenamiento ni AutoResearch. Todo sigue
apagado y no se presenta este bloque como prueba de rentabilidad.
[Auditoria de acceso y limites pendientes](docs/audits/birdeye_resource_access_20261013.json).

### PAPER autorizado: aceptacion operativa fallida (2026-10-13)

La prueba nativa de 30 minutos, con compras simuladas exactas de 0,1 SOL, termino
sin intentos de compra ni posiciones nuevas. De 242 eventos deduplicados de
bloqueo, 221 fueron por edad desconocida y 21 por snapshot ausente. Estos eventos
no cuentan tokens unicos ni oportunidades rentables demostradas.

Una consulta nativa muestreada a `/defi/token_creation_info` devolvio HTTP 401
por permisos insuficientes. No se ha verificado el plan de la cuenta ni creado
otra clave. El adaptador todavia agrupa acceso denegado con otros fallos como
`unavailable`; distinguirlos y comprobar acceso real sigue pendiente. El primario
esta en sombra sin modelo cargado, pero las entradas se bloquearon antes de ML.

El cierre automatico requirio parada forzada del arbol de procesos verificado;
el finalizador SQL y el censo posterior confirman todo apagado. El watchdog
conservo una alerta de hash y salida 1: solo se anadio la fecha de generacion a
un informe de replay; sus valores previos, `.env`, pesos y registros no cambiaron.
No hubo LIVE, entrenamiento ni AutoResearch. No se reiniciara PAPER sin otra
autorizacion. Esta prueba no demuestra rentabilidad ni completa el objetivo.
[Aceptacion PAPER y limites](docs/audits/paper_acceptance_20261013.json).

### Preflight nativo: plazo y fallos verificables (2026-10-13)

El comando independiente `tools.preflight --run-tests` ya dispone de una hora
para la suite completa, configurable con `--pytest-timeout-seconds`. Un timeout o
un error al lanzar un subproceso queda registrado como validacion fallida,
en lugar de impedir la escritura del estado. No se relajan los controles del bot.

Veintiun casos nuevos y 39 pruebas focalizadas por copia pasan. El comando
nativo completo pasa 5.739 pruebas del repositorio en ambas carpetas: D incluye
ademas una prueba privada del timeout, con 5.740 aprobadas en total. La
coleccion independiente confirma identicos casos del repositorio. Hay dos
avisos existentes de pandas por suite; compilacion, siete informes vacios y
26 imports nativos por copia pasan sobre fuentes verificadas identicas.

El launcher de stack ya ejecutaba pytest directamente y no tenia este limite
de cuatro minutos. No se atribuye esta correccion al run sin compras ni se
presenta como rentabilidad. En esta verificacion todo el runtime permanecio
apagado; la prueba PAPER de 30 minutos autorizada por el usuario es una
aceptacion operativa independiente, sin LIVE, entrenamiento ni AutoResearch.
[Auditoria de preflight](docs/audits/preflight_runtime_20261013.json).

### T0 original y relojes nulos del aprendizaje (2026-10-13)

El cargador acepta solo la hora original de decision `timestamp`: ni cierre,
persistencia `ts`, nacimiento, listado ni hora de carga pueden sustituirla.
Las filas sin T0 conservan sus etiquetas historicas, pero no entran en folds o
poblaciones temporales validadas. Los relojes pandas `NaT` ahora permanecen
desconocidos sin romper la generacion nativa de etiquetas.

Veintisiete casos nuevos, el escritor Parquet nativo, CSV, particiones mixtas,
etiquetas de picos >10.000% y folds quedan cubiertos. Las 387 pruebas focalizadas
por copia pasan, y ambas suites completas pasan 5.718 pruebas con dos avisos
existentes de pandas cada una. Los informes JUnit persistentes contienen todas
las pruebas, sin errores, fallos ni omisiones. Ambos preflight y 26 imports
nativos por copia pasan; las fuentes verificadas son identicas.

La ejecucion de QA anterior a la pausa no se da por aprobada: se repitio de forma
controlada al comprobar que sus identificadores ya no existian y no habia
pytest activo. No se cargan modelos del operador ni se activa entrenamiento,
AutoResearch o LIVE. Bot/API/UI permanecen apagados, con estado SQL detenido;
PAPER sigue configurado en compras exactas de 0,1 SOL.

No se ha observado este defecto en los T0 poblados del historico conservado,
ni queda demostrado como causa del run sin compras. Esta es una correccion de
integridad, no una prueba de bot ganador, captura real o maxima rentabilidad.
El objetivo global sigue incompleto. [Auditoria T0](docs/audits/training_decision_clock_20261013.json).

### Dex: propiedad de peticiones y calidad del aprendizaje (2026-10-11)

Las consultas normales compatibles de Dexscreener comparten una peticion en el
mismo loop; los refresh forzados mantienen propietarios independientes y limites
globales. La ultima admision controla cache/backoff incluso entre loops e hilos:
una respuesta antigua ya no pisa el resultado reciente. Cancelaciones y rechazos
de capacidad conservan la propiedad y liberan la sesion sin dejar generaciones.

Veinticinco casos nuevos y 221 pruebas focalizadas por copia pasan. Las suites
completas pasan 5.691 pruebas por copia, con dos avisos existentes de pandas;
ambos preflight y 26 imports nativos por copia pasan. PAPER sigue en 0,1 SOL.
Bot/API/UI/entrenamiento/AutoResearch permanecen apagados; no hubo ordenes LIVE.

La revision de solo lectura encuentra 2.147 filas elegibles de julio: cinco
tokens con pico >=500%, cuatro >=1.000% y ninguno >=2.000%. Los umbrales se
solapan. Ninguna fila acredita neto bajo el contrato actual; los picos no son
beneficios ni cobertura del mercado actual. No se activa un modelo por ellos.

El defecto T0 que esta etapa dejo reproducido se corrige y valida en la etapa
posterior documentada arriba; no se ha observado en los Parquet retenidos.
El objetivo global sigue incompleto. [Auditoria Dex y calidad](docs/audits/dex_request_ownership_20261011.json).

### Ciclo nativo PAPER completo y aprendizaje sin logging (2026-10-11)

El ciclo probado admite senales sinteticas de +500%, +25.000% y +1.000.000%,
compra exactamente 0,1 SOL y mantiene una cola parcial en una trayectoria de
precio hasta 10.001x, sin cerrar por un techo fijo del 100/200%. Los parciales
reducen la cantidad: queda aproximadamente el 3% original antes de la caida.
Son pruebas sinteticas, no captura ni rentabilidad reales.

Se ha corregido un defecto independiente: sin `--log` ni identidad del launcher,
una compra y su cierre podian quedarse sin identidad causal para aprender.
Ahora se conserva la identidad explicita o se genera una por run. No se aflojan
riesgo, ruta, antiguedad, politica, whitelist, capacidad ni controles de compra.

Catorce casos nuevos ejecutan entrada guardada, buyer, SQL, journals, monitor,
parciales, cierre y exportacion Parquet neta e idempotente nativos en copias
temporales sin credenciales ni datos del operador. Tambien verifican rechazos.
Ambas suites completas pasan 5.666 pruebas, con dos avisos de pandas cada una;
ambos preflight y 26 imports por copia pasan. Todo el runtime sigue apagado.
La carrera Dex que esta etapa dejo reproducida se corrige en la etapa posterior
documentada arriba; la aceptacion operativa y financiera sigue independiente.
El objetivo global sigue incompleto. [Auditoria del ciclo nativo](docs/audits/native_paper_lifecycle_20261011.json).

### Bootstrap PAPER por fases y oportunidades extremas (2026-10-11)

La admision preliminar permite recoger riesgo, ruta e impacto todavia ausentes;
no autoriza una compra. Datos malformados o riesgos observados siguen bloqueando.
Tras el enriquecimiento y antes de ejecutar, los controles nativos exigen datos
completos, ademas de capacidad, politica y coherencia de entrada.

Dos casos reproducen el fallo de orden original. La correccion pasa 50 casos
nuevos, 308 pruebas focalizadas y 5.652 pruebas completas en cada copia (dos
avisos de pandas cada una), ambos preflight y 26 imports nativos comprobados.
Otras 14 regresiones por copia conservan senales/cash sinteticos hasta
+1.000.000% y una cola parcial hasta +100.000%, sin un techo al 100/200%.
Esto no prueba captura ni beneficio real, ni conservar todo el capital.

El ultimo run conservado es una prueba corta del 4 de octubre: 19 evaluaciones
bootstrap rechazadas por motivos de calidad solapados, bajo otra configuracion,
sin intentos de compra. No queda demostrado que el nuevo defecto fuera su causa.
Data Analytics ayuda a distinguir datos pendientes de datos invalidos o negativos.
Bot/API/UI/entrenamiento/AutoResearch quedan apagados; PAPER mantiene compras
exactas de 0,1 SOL. Rentabilidad, cobertura y primer arranque operativo no estan
certificados. Detalles: [auditoria por fases](docs/audits/paper_bootstrap_phases_20261011.json).

### Relevo primario verificado y cola extrema (2026-10-11)

El relevo de la generacion cap anterior ya funciona con evidencia sintetica
nativa: se conserva el ajuste y la seleccion originales y solo se permite un
sucesor aprobado con una cohorte posterior, independiente y del mismo objetivo
financiero. La integridad, el selector y la identidad se comprueban de nuevo
bajo bloqueo. El modelo retirado no vuelve a inferencia ni a rollback.
Otras generaciones antiguas o mixtas siguen necesitando validacion especifica.

Ambas copias pasan 5.602 pruebas completas (dos avisos de pandas cada una),
preflight y 25 imports nativos comprobados. Ademas, 12 regresiones por copia
conservan senales y cash sinteticos hasta +1.000.000%, sin un cierre obligatorio
al 100/200%. La escalera parcial probada retiene un 3% de la cantidad original
para seguir la subida antes de una salida por caida de cash; no retiene todo
el capital ni demuestra captura o beneficio real.

El primario configurado en la copia de trabajo no tiene actualmente selector
administrado ni metadata: el defecto de relevo reparado no esta establecido
como causa del run sin compras. No se carga ni activa ningun modelo del operador.
Bot/API/UI/entrenamiento/AutoResearch permanecen apagados; compras PAPER exactas
de 0,1 SOL. Cobertura, primer arranque operativo y rentabilidad siguen pendientes.
Detalles: [auditoria del relevo](docs/audits/primary_valuation_retirement_20261011.json).

### Capitalizacion/FDV y aprendizaje original (2026-10-11)

La capitalizacion reportada y FDV son independientes: cero/desconocido no se
sustituyen por el otro campo. Los predictores de cap exigen evidencia original
T0 por objetivo; no se reetiqueta el historico ni se activa ningun modelo.
Las pruebas sinteticas conservan senales superiores al 100/200%, hasta
+1.000.000%, sin presentarlas como rentabilidad obtenida.

Ambas copias pasan 5.567 pruebas completas (dos avisos de pandas cada una),
preflight y 22 imports nativos comprobados. Bot/API/UI/entrenamiento/AutoResearch
quedan apagados; PAPER mantiene compras exactas de 0,1 SOL.
El relevo de la generacion cap anterior se verifica en el apartado superior;
otras generaciones, cobertura completa y beneficio neto real siguen pendientes.
Detalles y limites: [auditoria de valoracion](docs/audits/market_valuation_20261011.json).

### Valoracion SOL/USD y evidencia historica

La conversion compartida exige un precio SOL/USD numerico positivo, el
`last_updated_at` de CoinGecko y el recibo HTTP original. La edad del recibo se
limita a `COINGECKO_SOL_TTL` (1..60 s) y la del dato de mercado a
`COINGECKO_SOL_MAX_MARKET_AGE_S` (1..300 s; defecto 120 s). Son limites de
aceptacion del cliente, no garantias del proveedor ni de ejecucion de swaps.
Un fallo de refresco deja la conversion desconocida: no se reutiliza
indefinidamente el ultimo precio bueno. Las consultas concurrentes comparten
el recibo original y los errores tienen un cooldown de 10 s.

`COINGECKO_DEMO_API_KEY` es opcional para configurar el acceso al endpoint
publico; la documentacion Demo lo requiere. Sin acceso aceptado no se inventa
precio y no se garantiza que se puedan valorar operaciones. La clave se envia
solo a `api.coingecko.com`, sin redirecciones. No se ha probado el acceso real
del operador en esta revision.

`SOL_USD_OVERRIDE` queda identificado como supuesto de escenario, no como
evidencia de mercado. `get_sol_usd()` y las conversiones financieras normales
lo rechazan; solo una consulta diagnostica explicita con
`allow_assumed=True` puede consumirlo. Los parametros opcionales invalidos
utilizan valores acotados por defecto y no rompen el primer import.

PAPER y las dos investigaciones prospectivas revisan SOL/USD de nuevo tras
esperar una cotizacion; si no existe, no registran un fill ni una ganancia.
Una compra historica sin su importe USD original no se reconstruye con el
cambio actual. El canary live no convierte con un SOL ficticio de 1 USD ni
reinicia una racha de perdidas ante un resultado desconocido: bloquea nuevas
admisiones. La via green LIVE reconstruye ahora un registro duradero desde
los buy/sell journals originales y las identidades SQL, antes del arranque y
de cada admision. Reserva el presupuesto antes de ejecutar, sin sobrepasar
el tamano LIVE aprobado ni el principal restante del limite diario.
Duplicados no incrementan contadores; fuentes perdidas, cambiadas, cierres
sin cobertura y finalidades pendientes no se convierten en riesgo cero.

El presupuesto usa lamports enteros y caja nativa conservadora de los recibos
independientes originales: no usa PnL USD convertido con el cambio actual.
Incluye las comisiones de red de la wallet y no acredita mas que el principal
neto comprobado cuando hay rent/depositos o saldos wrapped. Es una medida de
riesgo, no un certificado de beneficio; las comisiones futuras pueden hacer
que una perdida efectiva supere el principal reservado. El registro requiere
el lock de instancia del bot, conserva las fuentes y no activa LIVE. PAPER
no crea ni escribe este registro. Los otros limites de estrategia/capital y
la aceptacion financiera completa siguen siendo fronteras independientes.
Vease `docs/audits/green_canary_risk_20261008.json` y sus pruebas aisladas.

La ejecucion PAPER principal conserva ahora `SolUsdObservation` y el reloj
original de cada compra y de cada venta, incluidos los parciales. Estos
recibos pasan por buy/sell recovery, close outbox, archivo, fuente neta del
modelo y los dos bancos prospectivos. Cada conversion se valida en su reloj
original; un scalar, supuesto o recibo perdido no se convierte en beneficio.
Los bancos solo reutilizan la cotizacion tras persistir el fill principal,
sin solicitudes adicionales ni nuevos intents. Un fallo de un banco no
bloquea el otro ni deshace la venta. El replay PAPER comprueba el modo SQL
sin cambiarlo. Los historicos sin estos recibos siguen sin certificarse y
no se reescriben. Vease `docs/audits/paper_execution_fx_20261009.json`.

La investigacion de runners reconstruye tambien la caja del primer parcial
desde su recibo original: cantidad exacta, cotizacion inversa, slippage y las
comisiones de compra/venta valoradas con el cambio original de cada fill.
Conserva esa prueba publica en los casos y la comprueba de nuevo al comparar
salidas o consumir una politica seleccionada. Un hash recalculado no valida
totales financieros alterados. Las fuentes antiguas sin prueba completa se
conservan, pero no certifican ni financian esta investigacion. Vease
`docs/audits/paper_first_partial_cash_20261009.json`.

El cierre PAPER cotizado reconstruye ahora cada venta desde su cotizacion
original, cantidad, reloj y FX. Suma los cobros SOL/USD y las comisiones de
cada fill, y contrasta el beneficio neto guardado antes de archivarlo, usarlo
como evidencia prospectiva o convertirlo en etiqueta de aprendizaje. Los
parciales se conservan separados de la venta final; ni un hash recalculado
ni cifras coherentes entre si sustituyen esa prueba de caja. No se consultan
precios actuales ni se reparan historicos. Vease
`docs/audits/paper_closed_cash_20261009.json`.

Cada compra PAPER conserva ahora una copia separada y versionada de sus
supuestos originales de slippage/comision y de su reloj. La respuesta y el
journal retienen esa misma base, tambien al recuperar una respuesta perdida.
Ventas, archivos, investigacion de parciales y etiquetas financieras rechazan
costes declarados que cambian despues de la compra. Las fuentes o modelos
financieros antiguos sin esa procedencia no se recertifican ni se reescriben;
siguen siendo diagnosticos. Esto evita convertir una perdida en una etiqueta
de ganancia eliminando comisiones de forma coherente, pero no autentica
recibos contra un proveedor ni demuestra beneficios LIVE. Vease
`docs/audits/original_paper_cost_basis_20261009.json`.

La evidencia financiera tampoco puede degradarse a compatibilidad antigua
eliminando los recibos FX originales y recalculando el hash. Aprendizaje y
evidencia prospectiva exigen la caja cotizada completa; los metadatos de
modelos financieros declaran esa base. Los historicos con cifras coherentes
pero sin prueba completa pueden conservarse como diagnostico, nunca como
certificacion financiera. El ranking de oportunidades sigue separado y los
retornos extremos no se recortan por este control. Vease
`docs/audits/financial_provenance_downgrade_20261009.json`.

La cola del scanner distingue ahora una senal entregada para evaluacion de
una candidatura expulsada sin evaluar. Una expulsion por capacidad no impone
un TTL de evaluacion ficticio; se conserva solo el ultimo historial realmente
entregado. Los empates mantienen a quienes ya esperaban, los cambios de
ranking pueden actualizar la cola y las candidaturas caducadas liberan sitio
antes de expulsar a otras frescas. Edades invalidas no rejuvenecen una
observacion previa; un cero medido o un booleano no se convierten en ranking
alto. Estas correcciones no autorizan compras ni relajan sus guardas. Vease
`docs/audits/scanner_queue_reconsideration_20261009.json`.

El FIFO de PumpPortal separa tambien las senales pendientes del historial
entregado: una expulsion previa a la entrega no crea un bloqueo ficticio y
las copias quedan desligadas del payload mutable original. El recibo local,
el reloj generico del evento y la creacion declarada son datos distintos.
Una creacion ausente/invalida no se sustituye por ahora ni por tiempo en cola.
La edad comun permanece desconocida en features y no recibe el bonus green
de recien nacido. La via green espera su observacion antes del bootstrap,
con el reintento acotado existente; no relaja guardas ni etiqueta una espera
como perdida financiera. La documentacion publica no especifica la semantica
exacta de timestamps; estas comprobaciones no autentican la creacion on-chain
ni prueban un feed completo. Datos/modelos historicos se conservan y necesitan
validacion fresca. Vease `docs/audits/discovery_birth_age_integrity_20261009.json`.

El filtro basico, reintento, prioridad, sizing y helpers del orquestador usan
tambien la edad nullable comun. Una edad desconocida espera la observacion
valida tras el snapshot fresco, antes de proxies, bootstrap y decisiones
financieras; no consume intentos de "too_young" ni fabrica una etiqueta de
perdida. El tiempo en cola no concede prioridad de recien nacido. El cero
medido sigue siendo valido y la creacion original prevalece sobre una edad
medida antigua. Las vias de investigacion standalone y los historicos no se
recertifican por este cambio. Vease
`docs/audits/entry_age_admission_integrity_20261009.json`.

Bootstrap, confirmacion de momentum y rank-canary separan ahora nacimiento
y permanencia en cola. La edad del token no sustituye una cola observada;
cero medido es valido y valores booleanos/futuros permanecen desconocidos.
La investigacion congela los tres perfiles al instante original de cada
caso y los revalida en ese mismo instante, no con el reloj actual. El replay
de bootstrap usa tambien su evento historico. Umbrales, controles de riesgo,
importe y salidas no cambian. Esto no recertifica datos/modelos previos ni
todas las rutas standalone. Vease
`docs/audits/research_temporal_integrity_20261009.json`.

Moonshot y seguimiento separan tambien nacimiento, cola y primera observacion;
la compra no sustituye el comienzo del seguimiento. Los tokens antiguos con
cola recien observada pueden conservar sus oportunidades extremas, sin un
veto general por nacimiento ni un techo fijo de beneficio. Las sondas
especiales mantienen su politica independiente. Investigacion y replay usan
el instante original; informes y etiquetas historicas conservan desconocida
una edad derivada de timestamps cuando falta ese instante, sin reescribir
datos ni modelos. Esto no prueba disponibilidad original de confirmaciones,
profundidad ejecutable o rentabilidad. Importe de 0.1 SOL PAPER y controles de
riesgo/salida permanecen. Vease
`docs/audits/moonshot_temporal_integrity_20261009.json`.

La investigacion de entradas registra ahora una huella original de fuentes
para cada componente y la conserva en plan, caso, journal, caja, evaluacion,
manifest y procedencia de la decision. Cambiar las fuentes invalida nuevas
entradas virtuales y aprobaciones incompatibles, sin abandonar salidas de
casos ya financiados. La cache revisa contenido original, no solo fecha y
tamano. Los artefactos historicos no se rellenan ni se recertifican; las
decisiones diagnosticas anteriores siguen siendo legibles. Esto identifica
fuentes declaradas, no acredita el Python realmente ejecutado ni demuestra
rentabilidad. Vease `docs/audits/entry_gate_code_integrity_20261009.json`.

La HotQueue actualiza tambien los pendientes al cambiar la generacion comprobada
de modelos de ranking, incluso sin un nuevo evento del feed. Conserva la edad y
el turno original, retira bonos de modelos invalidados y admite cambios de los
inputs de aprendizaje sin esperar todo el TTL, respetando el cooldown. El ranking
agrupado sigue siendo orientativo: no concede permiso de compra ni cambia los
controles de importe/riesgo/salida. Las mediciones locales con modelos sinteticos
no prueban cobertura del feed, latencia con artefactos reales ni rentabilidad.
Vease `docs/audits/hot_queue_model_refresh_20261009.json`.

La ruta real de discovery incorpora ahora snapshots en lotes acotados, con
una generacion comprobada por lote y cesion del loop entre bloques de 128.
Conserva fuentes, orden de admision, capacidad, caducidad, cooldown y updates
parciales; una fila invalida no descarta sus vecinos. El ranking preparado
solo se reutiliza si coincide todo el input final. El fallback escalar usa
el mismo reloj de features. No convierte avisos de nacimiento en mediciones
de mercado ni permite compras. Mantiene 0.1 SOL PAPER y remanentes sin techo
fijo de subida, sujetos a riesgo y salidas. Las pruebas sinteticas no prueban
cobertura ni beneficios. Vease `docs/audits/discovery_burst_20261009.json`.

El modo HotQueue tiene ahora un propietario serial supervisado por fuente:
DexScreener y el drenado/admisión de PumpPortal esperan la reconciliacion de
arranque, pero no esperan entre si ni a una evaluacion lenta. Las compras
conservan un unico propietario y los controles actuales. El modo sin HotQueue
retiene su ruta Pump guardada dentro de ese propietario; la seleccion de modo
se fija para cada arranque. Una pausa no inicia nuevos pulls, aunque uno ya
iniciado puede completar su admision. El shutdown drena tambien el socket
PumpPortal antes de publicar stopped, sin vaciar su historial original.
Son contratos de concurrencia y apagado, no una garantia de latencia del
proveedor, cobertura completa ni beneficios. Mantiene 0.1 SOL PAPER y los
remanentes elegibles sin techo porcentual. Vease
`docs/audits/discovery_loop_ownership_20261009.json`.

El descubridor Dex consulta los feeds documentados de perfiles recientes y
perfiles actualizados, y combina sus filas en orden intercalado. Una fuente
no-Solana, vacia o fallida no oculta los candidatos validos de la otra.
Solo admite identidades `chainId=solana` y `tokenAddress` canonicas; aplica
`MAX_CANDIDATES` despues de validar y deduplicar. Un perfil no acredita la
fecha de nacimiento del mint: sus campos de listado/par/edad no filtran esta
recogida ni se convierten en edad cero. La preparacion de mercado y los
controles finales de edad, entrada, ejecucion y riesgo siguen vigentes.
Un fallo de ambas fuentes conserva el anterior reloj de salud del propietario;
una coleccion valida vacia es distinta de un fallo. El diagnostico privado de
relojes legacy usa UTC y ausencia nullable, y no decide compras. Ver
`docs/audits/discovery_profile_inputs_20261009.json`; cobertura productiva y
rentabilidad no quedan demostradas por esta integracion sintetica.

La creacion/listado de un par o pool ya no representa el nacimiento del mint.
Dex usa `token-pairs/v1/solana` y conserva su evento nullable junto a la identidad
del par; Gecko y el normalizador legacy de pools Birdeye separan asimismo sus
relojes de venue. Una observacion de mercado nueva no rejuvenece un nacimiento
conocido ni el tiempo original en cola. Los modelos que usan edad requieren la
generacion `original_mint_birth_not_venue_v2`; la etapa inicial empleaba pipeline
advisory 12, sin reescribir ni activar modelos/politicas del operador.
Ver `docs/audits/venue_birth_semantics_20261011.json`. Esto no aporta un nacimiento
autoritativo cuando falta, ni certifica acceso real a proveedores, cobertura de
oportunidades o beneficios. El bot permanece apagado y la medida es PAPER 0.1 SOL.

El aprendizaje temporal exige ademas procedencia original de los inputs
normalizados en T0: mint, reloj, edad y ausencia se conservan como metadata,
fuera de las 77 columnas. Una etiqueta de generacion no basta para certificar
edades historicas. Si faltan soporte o tokens distintos suficientes, se
mantienen los inputs historicos no temporales sin renombrar ni reescribir datos.
La pipeline advisory actual es 13: conserva artefactos obsoletos y exige un
sucesor validado de forma independiente, sin activacion operativa por esta QA.
El ranking por lotes conserva asimismo la procedencia temporal de cada Series
y aisla los comprobantes invalidos sin convertir en desconocidos sus pares validos.
Ver `docs/audits/token_clock_learning_20261011.json`. Esta procedencia no autentica
el nacimiento on-chain; la reconstruccion legacy de cohortes por lane/edad,
acceso real a proveedores y rentabilidad prospectiva siguen pendientes.

Birdeye usa ahora las rutas documentadas de token overview y pair overview,
con identidad Solana exacta, respuesta `success=true`, cabecera de acceso
documentada y redirecciones desactivadas. Acepta liquidez escalar y conserva
actividad de 5 minutos, ceros reales, ausencia nullable y porcentajes extremos
sin recortar. El recibo HTTP original permanece ligado al valor y a su cache;
no garantiza la fecha efectiva del dato de mercado.
Los relojes/edades no tipados del overview no son nacimiento del mint, y la
creacion del par queda separada. `get_token_creation_info` es una consulta
explicita con contexto de creacion cacheado: no se agrega automaticamente
a cada precio ni autentica la cadena de forma independiente.
Ver `docs/audits/birdeye_contract_20261011.json`; el acceso, coste/cuota reales,
entitlement y beneficios futuros no se certifican.

La entrada comun resuelve ahora el nacimiento original desconocido antes de
recabar su snapshot de mercado. La consulta es opcional si Birdeye no esta
configurado o esta degradado, tiene timeout y presupuesto propios, cache
acotada y coalescencia por mint; nunca fabrica edad cero ni permiso de compra.
Los valores por defecto reservan como maximo 12 consultas de creacion/minuto
(hasta420 CU documentados, aparte de precios), separadas5 segundos, con timeout
de3 segundos y2048 entradas de cache. Los cinco ajustes `BIRTH_ENRICHMENT_*`
estan descritos en `.env.example`; no se modifica el `.env` personal.
Se conserva el nacimiento original y su recibo en reintentos/HotQueue,
snapshots y metadata T0 del aprendizaje, sin agregar un predictor ni reescribir
modelos o historicos. El recibo es reportado por el proveedor, no una prueba
independiente de cadena. No reemplaza un nacimiento conocido ni rejuvenece
la residencia original en cola. Los porcentajes extremos siguen sin recorte;
esto no certifica oportunidades ejecutadas ni beneficio neto.
Ver `docs/audits/token_birth_enrichment_20261011.json`.

La ruta de creacion tambien puede usar la firma original del evento PumpPortal
para consultar una sola transaccion confirmada por RPC, antes del fallback a
Birdeye. La referencia del feed no se convierte por si sola en nacimiento: se
reconstruyen el mensaje binario, su estructura/firma, las cuentas cargadas y la
inicializacion exacta `InitializeMint`/`InitializeMint2` de Token o Token2022,
incluidas las instrucciones internas registradas. Se conserva evidencia
acotada a64 KiB y su recibo original fuera de los predictores del modelo.
El RPC sigue siendo la fuente de fecha/slot, cuentas cargadas e instrucciones
internas; no es una verificacion independiente de inclusion/finalidad ni del
primer ciclo de vida historico de una direccion. No se rejuvenece un nacimiento
ya conocido ni se convierte una transaccion ausente/fallida en edad cero.
Los siete ajustes `BIRTH_RPC_ENRICHMENT_*` de `.env.example` acotan esta ruta
por proceso a12 consultas/minuto por defecto,2 propietarios y256 seguidores,
timeout de3 segundos y2048 entradas de cache, con negativos de5 segundos.
El presupuesto no se reinicia por mint, cambio de nodo, hilo o event loop.
`USE_BIRDEYE=false` no desactiva RPC; `BIRTH_ENRICHMENT_ENABLED=false` desactiva
ambas rutas. No se modifica el `.env` personal. No firma ni envia transacciones
y no activa entrenamiento, modelos ni LIVE. Ver
`docs/audits/rpc_mint_birth_20261013.json` para el estado real de la validacion;
no certifica acceso/cuotas reales, oportunidades ejecutadas ni rentabilidad.

La cache compartida en memoria usa ahora TTL/LRU con8192 entradas por defecto,
barrido de expirados no consultados e indice de expiracion acotado. Los
contadores recientes de Birdeye/Dex/Gecko tambien tienen8192 claves por
proveedor, retencion de1 hora y nivel de backoff limitado a4; no son contadores
de todas las peticiones o costes. Los valores/recibos originales no se
rejuvenecen; un reloj local invalido o que retrocede invalida la cache.
Birdeye comparte consultas normales identicas dentro del mismo event loop,
con limites globales de16 operaciones propias y256 seguidores. Un refresco
forzado siempre inicia otra consulta; una respuesta anterior ya no puede
sobrescribir la cache ni el backoff de un refresco mas nuevo. La cancelacion
del propietario drena su trabajo, incluso con senales repetidas; el timeout
no garantiza la latencia real de esa limpieza. El estado operativo publica
solo presion agregada, no claves privadas o direcciones.
Los cinco ajustes opcionales de capacidad aparecen en `.env.example`, sin
modificar el `.env` personal. Esto no certifica acceso/cuotas, throughput,
cobertura de todos los proveedores, oportunidades ejecutables ni rentabilidad.
Ver `docs/audits/provider_capacity_20261011.json`.

El adaptador publico GeckoTerminal exige ahora HTTP200 y recurso token con
`id`, `type` y `attributes.address` correspondientes al mint solicitado.
Solicita `include=top_pools` y utiliza solo el primer pool enlazado, unico y
con relacion base/quote comprobada. Su reserva no se sustituye por la suma
de reservas del token: sin pool comprobado, esa suma es solo un proxy.
Capitalizacion desconocida y FDV permanecen separados; cero y desconocido
no se confunden. Precio y cambios extremos no se recortan, y la actividad
direccional del token base no se atribuye al token quote. El nacimiento del
pool nunca acredita el nacimiento del mint.

El acceso sigue siendo al gateway publico, con version Accept20230203 y un
maximo local de10 llamadas/minuto compartido entre rutas sync/async, conforme
al Swagger publico actual; la cuota upstream puede fluctuar. Sus endpoints
tienen cache upstream de1 minuto: un recibo HTTP nuevo no acredita mercado
instantaneo. No se migra a CoinGecko Demo/Pro ni se agregan credenciales.
Consultas async compatibles comparten trabajo; force-refresh conserva su
propia generacion, y respuestas antiguas no reemplazan cache/backoff nuevos.
Como maximo hay16 operaciones Gecko sync+async admitidas; cancelaciones
repetidas conservan la propiedad hasta completar su limpieza. La presion y
los resultados agregados no son contadores de peticiones facturadas.
Ver `docs/audits/gecko_contract_20261011.json`. La cobertura real, la latencia
de limpieza y la rentabilidad neta siguen sin acreditarse; bot y servicios
permanecen apagados, con compras configuradas de0.1 SOL exclusivamente PAPER.

La procedencia de las entradas aprendidas incluye ahora una comprobacion
acotada del codigo Python cargado y sus namespaces, aliases y cierres de
funcion frente a las fuentes originales. Una funcion sustituida, un alias
antiguo o un comprobante incompleto no pueden prestar esa evidencia a una
nueva seleccion. Las fuentes v1 siguen siendo diagnosticas, sin rellenar
artefactos historicos ni aprobar nuevas politicas con ellas. El collector v2
guarda en el journal referencias verificadas al original completo del plan;
evita duplicaciones que excedan el limite de lectura en 128 casos. Mantiene
las identidades completas en los casos, salidas financiadas, 0.1 SOL PAPER
y remanentes sin techo fijo de beneficio. Esta comprobacion no autentica
todo el proceso, sus inputs o el caller realmente ejecutado, ni acredita
rentabilidad. Vease `docs/audits/loaded_entry_gate_integrity_20261009.json`.

La evaluacion del ranking ya no elige ganadores entre puntuaciones iguales
por el orden de las filas. Precision y recall comparten los puestos del corte
proporcionalmente entre todos los empates exactos, sin ampliar la capacidad.
La generacion de la metrica y la fraccion top-k quedan declaradas; evidencia
antigua no puede aprobar ranking nuevo ni saltarse la reevaluacion por cache.
Los artefactos originales se conservan y los filtros de compra no cambian.
Los targets extremos siguen siendo elegibles cuando existe evidencia; esta
correccion no demuestra rentabilidad ni generalizacion de eventos raros.
Vease `docs/audits/ranking_tie_integrity_20261009.json`.

La validacion del ranking tambien distingue decisiones repetidas de tokens:
la utilidad de capturar una etiqueta positiva en el top-k se promedia por mint
y se compara con el mismo presupuesto uniforme mediante bootstrap de tokens.
La precision por fila sigue siendo diagnostica, no la prueba de promocion.
Los sucesores usan el lift de captura por token y una comparacion emparejada
en el mismo cohort, reutilizando exactamente las predicciones evaluadas.
La generacion v9 invalida aprobaciones anteriores sin reescribir sus modelos;
un sucesor debe validarse por separado. No cambia compras, importes ni salidas,
y el bootstrap historico no acredita independencia temporal ni rentabilidad.
Vease `docs/audits/ranking_token_skill_20261009.json`.

La validacion walk-forward conserva ahora el cohort de cada ajuste del modelo:
las puntuaciones brutas de modelos distintos no compiten por un unico top-k.
Cada ventana reparte su propio presupuesto con empates exactos; precision,
recall y captura por token agregan esas selecciones originales. La referencia
uniforme y la comparacion emparejada usan la misma capacidad de cada ventana,
incluido el redondeo. La generacion v10 requiere esta evidencia y mantiene
neutrales las aprobaciones de captura agrupada de la generacion anterior,
sin reescribir modelos ni invalidar probabilidades calibradas independientes.
Esto no es un bootstrap temporal ni prueba independencia entre creadores,
beneficios futuros o permiso de compra. Vease
`docs/audits/ranking_fit_cohort_20261009.json`.

La generacion v11 anade soporte temporal original al ranking: cada media por
mint se ancla a su primera decision OOS y se remuestrean bloques UTC de una
hora completos con dos origenes fijos, sin convertir una rafaga de tokens en
muchas unidades independientes. Se conserva la capacidad original de cada
ajuste y se exige soporte positivo repartido entre bloques; relojes ausentes
o parciales no se inventan. La comparacion con el incumbente reutiliza los
mismos tiempos y predicciones. La v3 de captura neutraliza aprobaciones solo
por token, sin reescribir modelos ni desactivar probabilidades independientes.
Es una sensibilidad historica a rafagas: no demuestra independencia entre
creadores, entre regimenes mas largos, cobertura futura ni beneficio neto.
No cambia filtros de compra, 0.1 SOL PAPER ni salidas. Vease
`docs/audits/ranking_temporal_support_20261009.json`.

Estas correcciones no certifican rentabilidad: la alineacion de las marcas
de runners y de la investigacion de entradas con su propia caja cotizada ya
esta integrada. La procedencia de la estrategia completa, el cambio original
de los fills LIVE y la aceptacion financiera prospectiva siguen
pendientes. Las compras de 0.1 SOL PAPER y los runners sin techo de beneficio
no se han cambiado. Vease `docs/audits/sol_usd_quality_20261008.ipynb` para las
comprobaciones reproducibles con datos sinteticos, no trades del operador.

El checkout actual incluye:

- Bot async principal en `run_bot.py`.
- Backend FastAPI con API operacional protegida por login local.
- UI React/Vite como cockpit de operador.
- Feature store parquet, SQLite runtime DB, JSONL de eventos y reports locales.
- ML tabular con validaciones, entrenamiento, threshold tuning y modo sombra por defecto.
- AutoResearch end-to-end: safety, schema, objectives, report bundle, API budget,
  sandbox, replay, evaluator, scoreboard, search spaces, batch, checkpoint,
  bandit, paper-forward, rollback, scheduler, LLM adapter deshabilitado,
  API/UI read-only y smoke/gates finales.

## Aviso De Riesgo

Este proyecto puede ejecutar swaps reales si `DRY_RUN=0` y los perfiles live lo
permiten. Las meme-coins pueden perder liquidez de forma inmediata. Usa una
hot wallet dedicada, empieza con paper, valida la UI y no deposites fondos que
no puedas perder.

AutoResearch no activa live. Genera y valida artefactos de research/paper; la
promocion live queda fuera del loop automatico.

## Arquitectura

```text
Fetchers
  DexScreener, Pump.fun/PumpPortal, GeckoTerminal, Jupiter,
  Helius/RPC, RugCheck, Birdeye, GMGN
      |
      v
Discovery queue
  filtros iniciales, backoff, retry, route/liquidity checks
      |
      v
Feature builder
  SQLite + parquet + runtime_events/candidate_outcomes/decision_ledger
      |
      v
Strategy runtime
  regimenes, lanes, gates, health, bucket blocks, ML shadow, policy state
      |
      v
Execution
  paper trading o swaps reales via rutas configuradas
      |
      v
Position monitor
  partial TP, runner floors, adverse tick, no-pump, liquidity crush, stops
      |
      v
Analytics, API, UI, AutoResearch
  reports locales, replay, objective score, scoreboard, API budget y gates
```

## Componentes Principales

| Ruta | Funcion |
| --- | --- |
| `run_bot.py` | Loop principal: discovery, scoring, gating, buy/sell, monitor, retrain, telemetry. |
| `config/config.py` | Carga `.env` y expone configuracion runtime. |
| `analytics/` | Filtros, runtime strategy, reports, scorecards, exits, sniper, runner capture. |
| `runtime/` | Estado runtime, command bus, process manager, policy modes, hot queue, paper-forward helpers. |
| `features/` | Construccion y escritura del feature store parquet. |
| `ml/` | Entrenamiento, modelos, registry, policy, risk/EV/runner/continuation models. |
| `trader/` | Paper trading y ejecucion real de buyer/seller. |
| `api/` | Backend FastAPI para UI y endpoints operacionales. |
| `ui/` | UI React/Vite de operador. |
| `research_loop/` | AutoResearch paper/replay-only. |
| `tools/` | Herramientas de reports, replay, AutoResearch y auditoria. |
| `scripts/` | Arranque local, smoke tests, quality gates, backup/restore. |
| `strategy_proposals/` | Schemas y candidate policies. |
| `data/` | DB, metrics, features, research runs. No deberia subirse a GitHub. |
| `logs/` | Logs runtime locales. No deberia subirse a GitHub. |

## Requisitos

| Requisito | Notas |
| --- | --- |
| Python | Usar 3.12, rama de CI y del entorno validado. 3.10 no soportado; el codigo requiere APIs async disponibles desde 3.11. Otras ramas no quedan certificadas aqui. |
| Node.js + npm | Necesario para la UI. |
| PowerShell | Los scripts de arranque son Windows-first. |
| Solana RPC | Helius u otro RPC fiable si se usa live o enriquecimiento avanzado. |
| Credenciales externas | Opcionales segun provider y modo. Paper puede arrancar con menos. |

Instalar dependencias Python. Comprueba que `python --version` selecciona
Python 3.12 antes de crear el entorno; no reutilices un `.venv` de otra rama.
Los controles async utilizan `Task.cancelling()` y `asyncio.timeout()`,
[introducidos en Python 3.11](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout).

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

El entorno instalado actual (Python 3.12.3) pasa `pip check`; esto no acredita
una instalacion limpia con futuras versiones de todos los paquetes ni acceso
efectivo a proveedores. No arranques LIVE para comprobar dependencias.

Instalar UI:

```powershell
cd ui
node --version
npm ci
npm run build
cd ..
```

La instalacion limpia se verifica con Node 24 en CI (Windows/Linux); la prueba
local aislada usa Node 24.19.0 y npm 10.9.8. No reutilices `node_modules` de
otra rama como prueba de que el lockfile instala correctamente. `npm run build`
no arranca la UI ni sustituye una comprobacion visual/autenticada del sistema.

## Configuracion Y Secretos

Copia `.env.example` a `.env` y rellena solo lo que uses:

```powershell
Copy-Item .env.example .env
notepad .env
```

Variables relevantes:

| Variable | Uso |
| --- | --- |
| `DRY_RUN` | `1` paper, `0` live. |
| `SOL_PUBLIC_KEY` | Wallet publica. |
| `SOL_PRIVATE_KEY` | Necesaria solo para swaps reales. Usa hot wallet dedicada. |
| `SOL_RPC_URL`, `RPC_URL`, `HELIUS_RPC_URL` | RPC Solana. |
| `HELIUS_API_KEY`, `BIRDEYE_API_KEY`, `RUGCHECK_API_KEY` | Enriquecimiento de datos. |
| `PUMPPORTAL_API_KEY` | PumpPortal websocket discovery si esta habilitado. |
| `JUP_API_KEY` | Opcional: gateway actual con o sin clave; una clave inválida no se descarta para reintentar. |
| `JUP_API_RPS` | Vacío: 0.5 sin clave / 1 con clave. Con clave, ajustar solo al cupo real del plan contratado. |
| `UI_AUTH_MODE`, `UI_LOCAL_USERS`, `UI_SESSION_SECRET` | Login local de la UI/API. |

No subas `.env`, claves privadas, backups con secretos, `data/` ni `logs/`.

## Modos Runtime

| Modo | Como | Efecto |
| --- | --- | --- |
| Paper | `DRY_RUN=1` o `.\scripts\start_bot.ps1` | No envia swaps reales; usa `trader.papertrading`. |
| Real | `DRY_RUN=0` o `.\scripts\start_bot.ps1 -RealMode` | Puede enviar swaps reales. |
| Shadow | ML/lane en sombra | Registra decision/outcome sin afectar gating productivo. |
| Live canary | Flags live + caps + aprobacion operacional | Rollout live controlado y manual. |
| AutoResearch | `research_loop/` + `tools/run_autoresearch_loop.py` | Research local/paper; live auto prohibido. |

Base recomendada para desarrollo:

```env
DRY_RUN=1
STRATEGY_OPTIMIZATION_LOCK=true
ML_GATE_MODE=shadow
AUTO_PROMOTE_LIVE=false
MODEL_AUTO_PROMOTE=false
```

## Arranque Rapido

API + UI:

```powershell
.\scripts\start_stack.ps1
```

API + UI + bot en paper:

```powershell
.\scripts\start_stack.ps1 -IncludeBot
```

Ese comando arranca solo el bot en paper. AutoResearch es explicito y se
incluye con:

```powershell
.\scripts\start_stack.ps1 -IncludeBot -IncludeAutoResearch
```

AutoResearch arranca en modo daemon paper/replay seguro. Por defecto usa
`AutoResearchMaxCandidates=3`, `MaxParallel=1` e intervalo de 360 minutos. El
stack limpia locks muertos del bot, regenera core reports antes de levantar
AutoResearch, refresca reports antes de los ciclos de investigacion, mantiene
live promotion apagado y bloquea LLM live-touch.

Para una sola iteracion:

```powershell
.\scripts\start_stack.ps1 -IncludeBot -IncludeAutoResearch -AutoResearchOnce
```

Opciones utiles:

```powershell
.\scripts\start_stack.ps1 -IncludeBot -IncludeAutoResearch -AutoResearchSpace moonshot_micro -AutoResearchMaxCandidates 10
.\scripts\start_stack.ps1 -IncludeBot -IncludeAutoResearch -AutoResearchOnce -AutoResearchNoPaperPromote
.\scripts\start_stack.ps1 -IncludeBot -IncludeAutoResearch -AutoResearchSkipRegenerateReports
```

API + UI + bot en real mode:

```powershell
.\scripts\start_stack.ps1 -IncludeBot -BotRealMode
```

Servicios:

| Servicio | URL |
| --- | --- |
| UI | `http://127.0.0.1:5173` |
| API docs | `http://127.0.0.1:8000/docs` |
| API health | `http://127.0.0.1:8000/api/v1/health` |

Componentes por separado:

```powershell
.\scripts\start_api.ps1
.\scripts\start_ui.ps1
.\scripts\start_bot.ps1
.\scripts\start_autoresearch.ps1
```

Bot directo:

```powershell
.\.venv\Scripts\python.exe run_bot.py --dry-run --log
```

## UI Operacional

La UI es un cockpit, no una landing page. Rutas principales:

| Pagina | Objetivo |
| --- | --- |
| `Overview` | Estado diario: runtime, queue, wallet, ML, source truth, posiciones. |
| `Runtime` | Heartbeat, flags de pausa, strategy health, buy limiter. |
| `Sniper` | Green sniper, hot queue, missed pumps, postura live-canary. |
| `Discovery` | Funnel de rejects/waits/shadows/buys y motivos. |
| `Queue` | Backlog, retries, oldest candidate y presion de cola. |
| `Positions` | Posiciones abiertas y riesgo. |
| `Trades` | Ledger cerrado y filtros. |
| `Trade Replay` | Reconstruccion de una operacion desde T0 a cierre. |
| `Analytics` | Edge por exits, lanes, regimenes y cobertura. |
| `ML Center` | Modelo, dataset quality, thresholds, readiness y blockers. |
| `Policy Center` | Safety gates, replay, decision ledger, funnel attribution y proposals. |
| `AutoResearch` | Scoreboard, current best, API budget, moonshot progress, paper-forward. |
| `Config Center` | Config efectiva y policies derivadas. |
| `Logs and Events` | Logs, runtime events y research events. |
| `Control Center` | Command bus, pause/resume, refresh reports, retrain, process control. |

## API

La API usa envelopes normalizados:

```json
{
  "data": {},
  "meta": {
    "generated_at": "2026-06-04T00:00:00+00:00",
    "degraded": false,
    "empty": false,
    "stale": false,
    "source_status": []
  }
}
```

`/api/v1/health` y auth son publicos. El resto requiere sesion local.

Endpoints principales:

| Endpoint | Uso |
| --- | --- |
| `GET /api/v1/health` | Health API. |
| `GET /api/v1/auth/session` / `POST /api/v1/auth/login` | Sesion local. |
| `GET /api/v1/sources/status` | Estado de SQLite, JSONL, parquet y metrics. |
| `GET /api/v1/overview` | Cockpit principal. |
| `GET /api/v1/runtime/state` | Runtime snapshot. |
| `GET /api/v1/runtime/strategy-health` | Salud de regimen/lane/buckets. |
| `GET /api/v1/discovery/feed` / `summary` | Funnel discovery. |
| `GET /api/v1/queue/summary` / `items` | Cola runtime. |
| `GET /api/v1/positions/open` | Posiciones abiertas. |
| `GET /api/v1/trades/closed` | Trades cerrados. |
| `GET /api/v1/trades/{trade_id}` / `replay` | Trade factsheet y replay. |
| `GET /api/v1/analytics/edge` / `baseline` | Reports de edge. |
| `GET /api/v1/config/effective` / `policies` | Config y policies efectivas. |
| `GET /api/v1/ml/status` / `research` | Estado ML y research scorecard. |
| `GET /api/v1/policy/*` | Learned-policy safety/replay/ledger/funnel/proposals. |
| `GET /api/v1/sniper/*` | Sniper status, missed pumps y hot queue. |
| `GET /api/v1/socials/*` | Social enrichment. |
| `GET /api/v1/research/*` | AutoResearch read-only. |
| `GET /api/research/*` | Alias read-only para AutoResearch. |
| `GET /api/v1/logs/tail` | Log tail permitido. |
| `GET /api/v1/events/runtime` / `research` | JSONL event rails. |
| `GET/POST /api/v1/control/*` | Command bus y process manager. |
| `GET/POST/PATCH/DELETE /api/v1/saved-views` | Views locales UI. |

AutoResearch endpoints:

```text
/api/v1/research/scoreboard
/api/v1/research/runs
/api/v1/research/current-best
/api/v1/research/api-budget
/api/v1/research/moonshot-progress
/api/v1/research/paper-forward
```

## Estrategia, Lanes Y Exits

El bot separa PnL productivo de investigacion:

| Lane / familia | Funcion |
| --- | --- |
| `pump_early_pumpswap_profit` | Lane productiva principal con gates estrictos. |
| `pump_early_pumpswap_prime` / meteor-prime | Tags internos de mayor edge dentro de pumpswap. |
| Green sniper / birth probe | Captura temprana y auditoria de newborn pumps. |
| Rank canary | Paper/research basado en ranking. |
| Shadow follow-up micro | Follow-up paper sobre señales shadow. |
| Moonshot micro | Micro-lottery paper para captura de tail events. |
| Late momentum micro | Paper/research sobre momentum tardio. |
| Dex mature / revival shadow | Investigacion y scorecards sin contaminar PnL productivo. |

El motor de exits incluye partial TP, post-partial protection, runner floors,
adverse tick, no-pump exit, liquidity crush, stop loss, time stop y total PnL
protection. Los reports de runner capture y missed pumps alimentan AutoResearch.

El objetivo de runners no se limita al +100% o +200%: incluye +500%, +1.000%,
+5.000% y superiores. Los umbrales de aprendizaje no son techos de venta.
Las posiciones PAPER de 0,1 SOL conservan la politica congelada de parciales y
proteccion de precio, sin prometer capturar el maximo ni eliminar el riesgo.

La investigacion prospectiva de entradas valora su propia cantidad restante,
no el precio de pantalla. Conserva el recibo original SOL/USD de la entrada y
los fills nuevos, y separa la cotizacion de valoracion de la posterior venta
virtual. Los casos sin prueba financiera completa no certifican el autoajuste.
El presupuesto compartido sigue acotado; una falta de cobertura se rechaza,
no se convierte en una ganancia o perdida ficticia. El hook de la salida PAPER
principal aun entrega solo un cambio escalar y no puede certificar estos fills
nuevos; la sonda de investigacion con recibo original funciona por separado.
Evidencia de software: `docs/audits/entry_gate_cash_forward_20261008.json`.

Las valoraciones PAPER principales ya comparten su cotizacion exacta y el
recibo SOL/USD original con los bancos de entrada y runner compatibles.
Cada brazo conserva su propia base financiera y solo acepta el mismo token,
cantidad raw y cronologia. La reutilizacion no consume una sonda adicional
ni ejecuta una intencion de venta recien creada. El polling pareado deja de
consultar por defecto un precio spot que no certifica ese efectivo.
La cuota no garantiza cobertura exhaustiva: una simulacion con la reserva
original obtiene huecos de 360 segundos con 3 grupos de entrada y 1 runner,
frente al limite financiero de 300 segundos. Son escenarios sinteticos,
no trades ni rentabilidad observada. La aceptacion sigue rechazando huecos.
Evidencia: `docs/audits/research_cash_reuse_20261008.json` y el notebook
`docs/audits/research_quote_capacity_20261008.ipynb`.

Una cotizacion no disponible no equivale a ausencia de ruta. Las sondas
revalidan el payload, el importe y el recibo original (maximo 10 segundos);
red, cuota, auth, payload invalido o caducidad quedan desconocidos. Si la ruta
es obligatoria, se aplaza la decision completa, sin permiso de compra ni
penalizacion negativa de aprendizaje. La cola conserva limites de edad y
capacidad. Solo un rechazo previo al envio confirmado en el diario original
puede volver a esa cola; un envio incierto sigue en recuperacion, sin reintento.
Los limites de impacto, capital y configuracion no se relajan. Evidencia:
`docs/audits/route_observation_admission_20261008.json` (software, no rentabilidad).

Con `JUP_MANAGED_ENABLED=true`, el scanner, las entradas/salidas PAPER y la
investigacion prospectiva consultan Swap V2 sin `taker`: Metis, JupiterZ,
DFlow y OKX. No se solicita wallet ni transaccion para firmar. Una cotizacion
RFQ/externa puede tener cero pasos publicados; solo se admite con su contrato
completo validado, sin inventar pasos Metis. Los recibos publicos conservan
payload permitido, importe, router y hora originales a traves del archivo y
las etiquetas de aprendizaje. No son prueba de ejecucion ni de liquidez actual.
La orden real sigue su validacion y reconciliacion independientes. Desactivar
explicitamente el modo managed conserva Metis V1, sin fallback silencioso entre
protocolos. Evidencia: `docs/audits/jupiter_all_router_observations_20261008.json`.

Price V3 es evidencia de precio, no de ruta: `has_route` y `routes_count`
permanecen desconocidos. Solo una omision en un mapa V3 valido entra en la
cache negativa; HTTP, JSON, campos duplicados, unidades invalidas o payloads
mal formados quedan como ERR y se pueden consultar de nuevo. Se limita el
cuerpo a 1 MiB y se conserva el recibo HTTP original tambien en cache, junto
con `blockId` y `decimals` cuando existen. El recibo no demuestra la recencia
del ultimo swap; los atajos fijos de estables son supuestos, no observaciones.
La policy de precio fiable y las cotizaciones independientes no se relajan.
Evidencia reproducible: `docs/audits/jupiter_price_quality_20261008.ipynb`.

## Machine Learning

ML es opcional y seguro por defecto:

```env
ML_GATE_MODE=shadow
AI_SIZING_ENABLED=false
MODEL_AUTO_PROMOTE=false
ML_AUTO_PROMOTE_LANES=false
```

Fuentes:

| Fuente | Uso |
| --- | --- |
| `data/features/features_YYYYMM.parquet` | Snapshots de features. |
| SQLite `positions` | Resultados cerrados. |
| `data/metrics/candidate_outcomes.jsonl` | Research/shadow outcomes. |
| `data/metrics/decision_ledger.jsonl` | Ledger canonico de decisiones. |
| `ml/model.pkl`, `ml/model.meta.json` | Modelo activo. |
| `data/metrics/train_status.json` | Estado ultimo entrenamiento. |
| `data/metrics/dataset_quality.json` | Calidad dataset. |

Comandos:

```powershell
.\.venv\Scripts\python.exe -m ml.retrain
.\.venv\Scripts\python.exe scripts\ml_report.py
.\.venv\Scripts\python.exe tools\ml_status.py
```

## AutoResearch

AutoResearch adapta el patron de experimentos tipo `autoresearch` a trading
sin permitir que el agente edite runtime live ni ejecute swaps reales.

Flujo:

```text
reports locales
-> report_bundle
-> api_budget
-> candidate_policy
-> safety/schema
-> sandbox candidate.env
-> replay local
-> objective_score
-> evaluator
-> scoreboard
-> paper-forward opcional
-> accepted/rejected
```

Superficie permitida:

- Candidate policies.
- Perfiles sandbox/paper sin secretos.
- Reports bajo `data/research_runs/`.
- Search spaces de thresholds, sizing paper y exits.
- Batch, checkpoint, bandit y scheduler.

Superficie prohibida:

- Activar live.
- Cambiar `.env` real.
- Tocar buyer/seller/wallet/signer.
- Tocar claves, RPC secrets o API keys.
- Aumentar rate limits o frecuencia de discovery.
- Desactivar risk guards.
- Usar LLM como trader.

Modulos:

| Archivo | Funcion |
| --- | --- |
| `research_loop/safety.py` / `safety.yaml` | Contrato de seguridad. |
| `research_loop/experiment_schema.py` | Schema candidate policy. |
| `research_loop/objectives.py` / `objectives.yaml` | Objective score y hard gates. |
| `research_loop/report_bundle.py` | Bundle local de reports. |
| `research_loop/api_budget.py` | Budget local, 429s, degraded providers. |
| `research_loop/sandbox.py` | `candidate.env` aislado sin secretos. |
| `research_loop/replay_runner.py` | Replay local y snapshot de reports. |
| `research_loop/evaluator.py` | `accepted_replay`, `needs_paper`, `rejected`, `failed`, `inconclusive`. |
| `research_loop/scoreboard.py` | `scoreboard.json` y `scoreboard.md`. |
| `research_loop/search_space.py` + `spaces/` | Registry y optimizadores especializados. |
| `research_loop/candidate_generator.py` | Grid/random/local/bandit candidates. |
| `research_loop/batch_runner.py` / `checkpoint.py` | Batches y resume/duplicates. |
| `research_loop/bandit.py` | Seleccion de espacios por reward. |
| `research_loop/paper_forward.py` | Controller paper-forward. |
| `research_loop/policy_promoter.py` | Perfil paper seguro. |
| `research_loop/rollback.py` | Rollback de paper candidate degradado. |
| `research_loop/scheduler.py` | Loop continuo, idle trigger y demotion. |
| `research_loop/llm_adapter.py` | Adapter opcional, disabled por defecto. |
| `research_loop/smoke.py` | Smoke end-to-end. |

Comandos AutoResearch:

```powershell
.\.venv\Scripts\python.exe tools\api_budget_report.py
.\.venv\Scripts\python.exe tools\generate_research_candidates.py --space moonshot_micro --n 25 --seed 42
.\.venv\Scripts\python.exe tools\run_research_replay.py strategy_proposals\candidates\PROPOSAL_ID.json
.\.venv\Scripts\python.exe tools\run_research_batch.py --space moonshot_micro --n 50 --seed 42
.\.venv\Scripts\python.exe tools\research_scoreboard.py
.\.venv\Scripts\python.exe tools\start_research_paper.py strategy_proposals\candidates\PROPOSAL_ID.json
.\.venv\Scripts\python.exe tools\finalize_research_paper.py PAPER_RUN_ID
.\.venv\Scripts\python.exe tools\run_autoresearch_loop.py --once --space moonshot_micro --max-candidates 3
.\.venv\Scripts\python.exe tools\autoresearch_smoke.py
```

Search spaces actuales:

```text
rank_canary
shadow_followup / shadow_followup_micro
moonshot_micro
runner_exit / runner_ladder
entry_quality
late_momentum
lane_sizing
sniper_momentum
paper_exploration
```

Docs:

- `docs/AUTORESEARCH_MEMEBOT.md`
- `docs/AUTORESEARCH_RUNBOOK.md`
- `research_loop/program.md`
- `research_loop/runbook.md`

## Datos Y Artefactos

| Ruta | Descripcion |
| --- | --- |
| `data/memebotdatabase.db` | SQLite runtime: tokens, positions, state, commands, views. |
| `data/features/features_YYYYMM.parquet` | Feature store. |
| `data/metrics/*.json` | Reports generados. |
| `data/metrics/*.jsonl` | Runtime/research/social/decision event rails. |
| `data/research_runs/runs/` | Sandboxes y replay runs AutoResearch. |
| `data/research_runs/batches/` | Batches AutoResearch. |
| `data/research_runs/paper_forward/` | Estados paper-forward. |
| `data/research_runs/scoreboard.json` | Scoreboard AutoResearch. |
| `data/research_runs/api_budget.json` | Budget API AutoResearch. |
| `config/profiles/*.env` | Perfiles paper/live/research. No contienen secrets si son generados por AutoResearch. |
| `logs/*.txt` | Logs runtime. |

## Reports

Regenerar reports core:

```powershell
.\.venv\Scripts\python.exe tools\regenerate_core_reports.py
```

Reports habituales:

```powershell
.\.venv\Scripts\python.exe scripts\baseline_report.py
.\.venv\Scripts\python.exe scripts\edge_report.py
.\.venv\Scripts\python.exe scripts\ml_report.py
.\.venv\Scripts\python.exe scripts\pnl_rollout_report.py
.\.venv\Scripts\python.exe tools\trade_diagnostics.py
.\.venv\Scripts\python.exe tools\missed_pumps_report.py
.\.venv\Scripts\python.exe tools\runner_capture_ladder_report.py
```

## Quality Gates Y Tests

Suite completa:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Gate local completo:

```powershell
.\.venv\Scripts\python.exe scripts\quality_gate.py
```

Gate backend/API sin build UI:

```powershell
.\.venv\Scripts\python.exe scripts\quality_gate.py --skip-ui-build
```

Strategy gate:

```powershell
.\.venv\Scripts\python.exe scripts\strategy_quality_gate.py --warn-only
```

AutoResearch final smoke/gate:

```powershell
.\.venv\Scripts\python.exe tools\autoresearch_smoke.py
.\.venv\Scripts\python.exe scripts\strategy_quality_gate.py --warn-only
```

UI build:

```powershell
cd ui
npm run build
cd ..
```

Ultima validacion conocida de este README:

```text
pytest: 472 passed
strategy_quality_gate: ok
autoresearch_smoke: ok
ui build: ok
```

## Backup Y Restore

Crear backup runtime:

```powershell
.\scripts\backup_runtime.ps1
```

Restaurar:

```powershell
.\scripts\restore_runtime.ps1 .\backups\memebot3-backup-YYYYMMDD-HHMMSS.zip --force
```

Incluye `.env` solo si entiendes que el backup contendra secretos:

```powershell
.\scripts\backup_runtime.ps1 --with-env --with-logs
.\scripts\restore_runtime.ps1 .\backups\memebot3-backup-YYYYMMDD-HHMMSS.zip --force --with-env
```

## Operacion Diaria

1. Abrir `http://127.0.0.1:5173`.
2. Revisar `Overview`: sources, runtime, queue, positions, ML.
3. Revisar `Runtime`: heartbeat, pause flags, strategy health y cooldowns.
4. Revisar `Discovery`: principales motivos de reject/wait/shadow.
5. Revisar `Sniper`: hot queue, missed pumps y postura de canary.
6. Revisar `Analytics`: closed trades, median PnL, exits y runner capture.
7. Revisar `AutoResearch`: scoreboard, current best, API budget y paper-forward.
8. Usar `Trade Replay` para perdidas grandes o runners importantes.

Si no hay buys:

1. Confirmar `buys_paused=false` y `discovery_paused=false`.
2. Revisar `Runtime -> Strategy Health`.
3. Revisar `Discovery -> Summary`.
4. Verificar que ML esta en shadow o en el modo esperado.
5. Mirar `logs/*`, `runtime_events.jsonl`, `candidate_outcomes.jsonl` y API budget.

## Checklist Antes De Live

1. `DRY_RUN=0` solo si es intencional.
2. Hot wallet dedicada y con fondos minimos.
3. `AUTO_PROMOTE_LIVE=false` y `MODEL_AUTO_PROMOTE=false`.
4. AutoResearch live promotion deshabilitado.
5. UI sin sources stale.
6. Strategy health sin cooldown critico.
7. API budget sin degradacion nueva.
8. Tamano pequeno y caps live revisados.
9. ML no enforce salvo validacion explicita.
10. Monitorizar primeras operaciones desde UI y logs.

## GitHub Hygiene

Subir:

```text
README.md
.env.example
source code
tests
docs publicos
```

No subir:

```text
.env
data/
logs/
backups con .env
wallet keys
API keys
modelos privados si no quieres publicarlos
```

`.gitignore` recomendado:

```gitignore
.env
.venv/
__pycache__/
.pytest_cache/
data/
logs/
backups/
ml/*.pkl
ml/*.meta.json
ui/node_modules/
ui/dist/
*.bkup.*
```

## Documentacion

| Documento | Uso |
| --- | --- |
| `docs/API_UI_SPEC.md` | Contrato API/UI. |
| `docs/UI_OPERATIONS.md` | Runbook UI/API/bot. |
| `docs/UI_SITEMAP.md` | Mapa UI. |
| `docs/UI_STATE_CONTRACT.md` | Source/state contract. |
| `docs/AUTORESEARCH_MEMEBOT.md` | Vision AutoResearch. |
| `docs/AUTORESEARCH_RUNBOOK.md` | Smoke y gate AutoResearch. |
| `docs/SNIPER_RUNBOOK.md` | Sniper runbook. |
| `docs/ML_REPORT.md` | Report ML generado. |
| `docs/EDGE_REPORT.md` | Edge report generado. |
| `docs/ROLLOUT_REPORT.md` | Rollout report generado. |

## Licencia

MIT. Ver detalles del repositorio original de `mudanzasalegre`.
