# Quant Lab · cinco activos y datos públicos

## Ejecutar

```powershell
.\.venv\Scripts\python.exe dashboard.py
```

Abre http://127.0.0.1:8765. Selecciona **Mercados públicos**, configura modelo y
clave de IA en **Configurar pruebas**, y pulsa **Iniciar investigación**.
También se admiten OPENAI_API_KEY y OPENAI_MODEL en el entorno.
Los precios no requieren clave, Excel, CSV ni cargas manuales.
La demo usa cinco series sintéticas, no consume IA y nunca aprueba producción.

La búsqueda continúa hasta superar todos los filtros o pulsar **Detener**.
Un error operativo detiene con diagnóstico. Recargar la página conserva el
trabajo; reiniciar el servidor no reanuda investigaciones anteriores.
La cancelación es cooperativa; una descarga pendiente tiene timeout de 10s.
Cancelar una solicitud de IA no revierte consumo ya recibido por el proveedor.

## Activos y proveedores

| Activo | Fuente | Serie |
|---|---|---|
| EUR/USD | Dukascopy | EUR-USD BID |
| XAU/USD · oro | Dukascopy | XAU-USD BID |
| GBP/USD | Dukascopy | GBP-USD BID |
| CAD/USD | Dukascopy | USD-CAD invertido, referencia ASK |
| BTC/USD | Coinbase Exchange | BTC-USD spot; no BTC/USDT |

Velas diarias desde 2018 hasta el último día completo UTC. Se iguala el rango
por calendario; BTC conserva fines de semana. Las sesiones cortas del domingo
FX/oro se agregan al lunes. CAD/USD usa open=1/open, close=1/close, high=1/low,
low=1/high. No se fabrican OHLC a partir de tipos de cambio de referencia.

Los snapshots JSON se guardan en outputs/market_cache. Cada rango se descarga
una vez por día, no por estrategia. El reporte registra fuente, URLs, fechas,
barras, hash y costos. Se rechazan precios inválidos, duplicados, datos con más
de siete días de retraso/huecos y menos de 600 barras. Si falta un activo no
se inicia la investigación. Nunca se sustituyen datos reales por sintéticos.

Fuentes:

- https://www.dukascopy.com/wiki/en/development/data-export/
- https://github.com/Leo4815162342/dukascopy-node/blob/master/src/url-generator/index.ts
- https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles

Los endpoints públicos pueden cambiar o limitar solicitudes. Se muestra un error
si dejan de responder; no se eluden verificaciones de navegador.

## Ciclo por hipótesis

1. Investigar: búsqueda web real de fuentes primarias, análisis de compatibilidad
   y ficha de hipótesis estructurada. Guarda URLs trazables, hallazgos, limitaciones,
   mecanismo, predicción, criterio de descarte y justificación de la adaptación.
   El investigador recibe estadísticas y resultados de entrenamiento, más motivos
   de rechazo por activo; no recibe las series ni métricas numéricas OOS.
   El feedback de rechazo también hace adaptativa la búsqueda. SQLite conserva
   investigaciones y rechazos.
   En modo real se proponen reglas compuestas con nombre libre, entrada y salida
   independientes. Se deduplican reglas ejecutables y cada variante consume otra
   prueba estadística; no hay un cupo de tres propuestas por familia en este motor.
   Las familias anteriores se conservan para compatibilidad y demo.
2. Prototipar: reglas y parámetros congelados para los activos objetivo. Costos y
   tamaños mínimos dependen del instrumento. El LLM no ejecuta código arbitrario.
3. Validar por activo: backtest IS/OOS, DSR, regresión alfa/beta y walk-forward.
   Misma fecha de división temporal 70/30. Si falla un filtro, omite las pruebas
   posteriores y cambia de activo, sin modificar las reglas.
4. Estresar ese mismo activo: $100 con costos x1/x2/x3, perturbaciones de parámetros,
   Monte Carlo, retraso de señales y concentración de beneficios. Después pasa al
   siguiente activo. Recorre los cinco antes de reformular (marca como omitidos los
   no seleccionados o sin datos necesarios); sólo acepta activos que
   superaron todos los filtros. Las pruebas omitidas se distinguen de las fallidas.
5. Generar: entre los aprobados en ambos filtros, selecciona mayor Sharpe OOS,
   luego menor drawdown y mayor retorno. Exporta el simulador del activo elegido.
6. Exportar MQL5: un nodo dedicado genera el Expert Advisor con las mismas reglas,
   sus dependencias de datos y el paquete de descarga. La ejecución sólo termina
   como aprobada después de guardar los artefactos. Demo y rechazos no exportan EAs.
7. Reportar: conserva resultados de los cinco. Sin aprobación vuelve a investigar.

### Exportación a MetaTrader 5 mediante LangGraph

El grafo ejecuta `production_coder_node → mql5_export_node → reporter_node` después
de superar todos los filtros obligatorios. `mql5_export_node` verifica de nuevo las
condiciones y traduce el AST de las reglas probadas, sin pedir al LLM que invente
otra implementación. El simulador Python se conserva para reproducibilidad.

Cada estrategia aprobada se guarda en:

```text
outputs/mql5/<ejecución>/attempt_<número>_<activo>/
  strategy.mq5
  strategy_bundle.zip
  manifest.json
  reference_signals.csv
  README.md
  quantlab_<firma>_features.csv  # cuando hay datos auxiliares
```

El panel permite descargar el MQL5 o el ZIP con todos sus archivos. El manifest
registra la hipótesis, firma, hashes, datos requeridos y estado de compilación.
Los nombres de carpeta dependen del intento y activo, nunca del texto del modelo.

El EA evalúa reglas compuestas y las ocho familias heredadas. Calcula señales con
velas D1 cerradas y actúa al primer tick de la siguiente vela; mantiene entradas y
salidas independientes, stops/objetivos al cierre y límite de tenencia. Usa el mismo
tratamiento de ventanas y NaN, incluida la semántica EMA de la versión de pandas
con la que se exportó. No sustituye una media por un indicador del bróker con otra
inicialización. Se incluyen señales Python de referencia para comparar en MT5.

OHLC y calendario se calculan con las velas del símbolo configurado. Volumen, series
externas y columnas cruzadas se exportan como CSV, ya alineadas de forma causal.
Copiar ese CSV a `Terminal/Common/Files`; una fecha ausente impide nuevas entradas,
manteniendo los cierres por stop, objetivo o tenencia cuando hay precios disponibles.
El archivo sólo contiene el snapshot existente: para fechas nuevas debe actualizarse
con el mismo proceso y fuentes. No hay sustitución automática por tick volume.

`EnableTrading=false` por defecto permite observar señales. Cambiarlo habilita órdenes
en el entorno donde se ejecute el EA. `StrategyCapitalUSD=100` fija el capital inicial,
actualizado con resultados y costos del historial del símbolo/magic. El nocional se
limita a ese saldo sin apalancamiento; se respetan contrato, lote mínimo, paso,
margen disponible y moneda USD. No reutilizar el magic para operaciones ajenas.
El símbolo y su orientación deben corresponder al activo validado: CADUSD no se
convierte silenciosamente en una compra de USDCAD. Sólo se gestiona una posición
propia por símbolo y magic; posiciones ajenas y órdenes pendientes bloquean acciones.

La generación del `.mq5` no certifica su compilación ni equivalencia con un bróker.
Sin MetaEditor/MT5, los estados son `PENDING_METAEDITOR` y `PENDING_STRATEGY_TESTER`.
Compilar en MetaEditor y comparar en Strategy Tester con los datos de referencia.
Revisar horarios de sesiones, DST, OHLC, divisas, spread, swaps y contratos; el ajuste
de fecha UTC no convierte sesiones distintas. El código comprueba los resultados
de las solicitudes de trading. No se inicia MetaTrader ni se colocan órdenes al exportar.

Las pruebas locales ejecutan los cuerpos numéricos MQL con una adaptación C++ para
compararlos con Python, además de verificar el grafo, los filtros y las descargas.
Esta comprobación no sustituye al compilador MetaEditor ni al Strategy Tester.

Referencias oficiales: [CopyRates](https://www.mql5.com/en/docs/series/copyrates),
[CTrade.Buy](https://www.mql5.com/en/docs/standardlibrary/tradeclasses/ctrade/ctradebuy),
[FileOpen y FILE_COMMON](https://www.mql5.com/en/docs/files/fileopen).

### Pruebas por activo

Los valores siguientes son criterios iniciales configurables, no garantías de éxito.
No se reducen automáticamente durante la búsqueda. El capital de estrés sigue siendo
$100, sin apalancamiento y respetando los mínimos de cada instrumento.

| Prueba | Método y aprobación |
|---|---|
| Backtest | 70/30 temporal, costos y mínimos reales del modelo; mantiene filtros IS/OOS, DSR, PF, operaciones y drawdown |
| Regresión histórica | OLS de retornos netos OOS de estrategia contra retornos diarios del propio activo; alfa/beta, errores Newey–West (HAC), mínimo 60 observaciones; alfa − 1,645 × error estándar > 0; diagnóstico, no bloquea aprobación |
| Walk-forward | 3 ventanas forward sin solapamiento; entrenamiento previo creciente, separación de max_holding barras; reglas congeladas, sin reoptimización; al menos 2/3 ventanas válidas, ganancias agregadas y DD dentro del límite |
| Costos | $100 con costos x1/x2/x3; todos deben superar criterios de retorno, operaciones, DD y solvencia |
| Parámetros | Cada parámetro ejecutable varía ±20%, uno por vez, dentro del esquema; mínimo 4 variantes y ≥80% con retorno/Sharpe positivos, operaciones suficientes, sin quiebra y DD aceptable |
| Monte Carlo | 1.000 trayectorias por bloque de 5, 10 y 20 barras (3.000 total); en cada grupo P(ganancia) ≥90%, DD percentil 95 ≤límite y ruina operativa ≤1% |
| Retraso | Señales de entrada y salida por condición retrasadas una barra adicional; stops y límites de tenencia mantienen su lógica; exige retorno/Sharpe positivos, operaciones y DD válidos |
| Concentración | Anula los retornos de los cinco mejores días positivos; el rendimiento compuesto restante debe seguir positivo; diagnóstico, no bloquea aprobación |

La regresión es explicativa, no un predictor de precios; supone tasa libre de riesgo
cero. El umbral z es asintótico, unilateral, y no corrige por sí solo la selección
adaptativa. Muestras degeneradas o sin varianza no se aprueban.

Cada ventana walk-forward arranca en efectivo y liquida al final con costos.
Entrenamiento requiere el mínimo general de operaciones; cada test exige al menos
ceil(min_trades / ventanas). Se exige el mínimo general en total, DD aceptable en
todas las ventanas de test y en la curva concatenada. Se prueban exactamente las
reglas que se exportarían: no se selecciona retrospectivamente la variante ganadora.

Monte Carlo remuestrea bloques circulares de los retornos netos diarios de la cuenta
de $100, con semilla reproducible. Ruina operativa significa perder ≥95% del capital
inicial. No reconstruye órdenes, tamaños mínimos o costos dependientes de otra
trayectoria, ni modela eventos que no existen en la muestra. La concentración también
es una sensibilidad de retornos, no un segundo simulador de ejecución.

El motor continúa hasta encontrar un aprobado, pulsar Detener, un error operativo o
alcanzar un límite opcional configurado. No garantiza encontrarlo. Una ganancia sola
no habilita la exportación: se comprueba explícitamente que estén aprobadas las pruebas obligatorias de walk-forward, parámetros,
Monte Carlo y retraso. Regresión y concentración son diagnósticas. Los filtros usan el histórico disponible y no sustituyen datos
posteriores independientes.

Referencias metodológicas: [covarianza HAC/Newey–West](https://www.statsmodels.org/dev/generated/statsmodels.stats.sandwich_covariance.cov_hac.html)
y [divisiones cronológicas con separación](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html).

### Investigación con fuentes

El modelo configurado debe admitir salida estructurada y `web_search` mediante
Responses API. Cada intento real realiza una búsqueda y una formulación estructurada;
consume tokens y uso de la herramienta web. Se usa la misma clave y modelo del panel.
Si la herramienta falla o no devuelve fuentes trazables, la ejecución se detiene
con error operativo, sin simular investigación ni recurrir a estrategias de respaldo.
Integración: https://developers.openai.com/api/docs/guides/tools-web-search

Se comprueban las URLs contra los metadatos de la búsqueda. Esto verifica procedencia,
no la veracidad de las conclusiones: el análisis de calidad, compatibilidad semántica
y lectura de fuentes lo realiza el modelo y requiere revisión humana. Las fuentes
actuales también pueden incorporar conocimiento posterior al período histórico.

La investigación puede explorar mecanismos y fuentes fuera de los indicadores de
precios. La ejecución disponible sigue siendo diaria, sobre los activos cargados,
long-only y sin apalancamiento. El catálogo muestra cobertura y estadísticas sólo
sobre entrenamiento. Incluye OHLC, volumen real cuando el proveedor lo entrega,
calendario, columnas cruzadas `market_<activo>_<campo>` y series externas.
Los activos fuera del objetivo o sin los campos necesarios se marcan omitidos.
Una idea que necesita otro tipo de ejecución queda documentada como incompatible,
con sus necesidades de datos; no se transforma silenciosamente en una regla distinta.

El motor real admite `program` con expresiones de entrada y salida independientes,
parámetros nombrados y un nombre de mecanismo libre. No hay una lista cerrada de
familias: pueden combinarse relaciones entre series, ventanas, rangos, retornos,
correlaciones, cuantiles, calendario y condiciones. Las ocho familias anteriores
siguen funcionando como formato heredado. La referencia IS de un programa compuesto
mantiene la gestión de riesgo y elimina las señales; es diagnóstica, no causal.

Las expresiones se interpretan como datos mediante un AST validado; no se ejecuta
Python generado. Se admiten operaciones aritméticas, comparaciones, and/or/not y
`col`, `lag`, `change`, `pct`, `sma`, `ema`, `std`, `lowest`, `highest`, `total`, `rank`,
`quantile`, `corr`, `abs`, `log`, `sqrt`, `where`. Todas las ventanas miran hacia atrás.
Esto amplía las hipótesis representables, pero no implementa cualquier algoritmo,
entrenamiento ML, microestructura intradía ni cortos. Los límites de tamaño de
expresión y ventanas son controles de recursos del intérprete.

Cada investigación debe justificar el enfoque elegido, qué conserva o cambia
respecto de la memoria y la evidencia contraria. No se fuerza una rotación aleatoria.
El contexto incorpora diagnósticos de entrenamiento: asociaciones con retornos desde
la próxima apertura a 1/5/20 barras, cuartiles de características y calendario.
Se etiquetan como exploratorios, brutos y con ventanas solapadas; no son pruebas de
significancia ni evidencia independiente y no incluyen valores posteriores al corte IS.
El objetivo es evaluar mecanismos con fundamento; variar la narrativa no constituye
otra estrategia. La predicción y refutación siguen requiriendo revisión científica:
los filtros automáticos verifican rendimiento y robustez simulados.

### Incorporación de datos de investigación

El volumen BTC ya no se descarta al validar las velas; los snapshots pasan a v2.
Las columnas de otro mercado se incorporan desde el día siguiente a su vela y caducan
tras siete días, evitando asumir cierres simultáneos entre mercados. Calendario y
campos adicionales numéricos se conservan sin inventar OHLC ni volumen.

El investigador puede solicitar `data_acquisition` con CSV públicos HTTPS cuya URL
esté en las fuentes de la búsqueda. Debe identificar la columna numérica, la fecha
real de publicación/disponibilidad y la tolerancia de antigüedad. Se descargan hasta
8 MB por fuente, con timeout, comprobación de destino público y de redirecciones.
Se guarda un snapshot con hash y procedencia dentro de la ejecución. Si el archivo,
esquema o disponibilidad no son válidos, se rechaza el intento con diagnóstico.
No se infiere una fecha de publicación a partir del período económico de una serie.
La semántica temporal declarada por la fuente/modelo todavía requiere revisión.

También se cargan automáticamente archivos `outputs/research_data/*.json` con este
contrato para fuentes macro, eventos, on-chain u otros valores numéricos:

```json
{
  "name": "example_release",
  "source_url": "https://example.org/data",
  "availability_description": "Fecha real en que se publicó cada valor, con revisiones fechadas por separado.",
  "max_age_days": 40,
  "observations": [{"available_at": "2020-01-15T13:30:00Z", "value": 1.2}]
}
```

El campo será `external_example_release`. Sólo entra después de su disponibilidad,
nunca retroactivamente ni más allá de su antigüedad declarada. Los archivos locales
se copian a la ejecución para reproducibilidad. Los simuladores exportados aceptan
`--research-data` y descargan los mercados cruzados necesarios; las series externas
se leen del snapshot, sin refrescar valores retrospectivamente. Para datos nuevos
hay que aportar observaciones nuevas y respetar sus fechas de disponibilidad.
Fuentes con credenciales, formatos distintos de CSV, datos sin historial de publicación
y modalidades de ejecución no implementadas quedan como necesidades documentadas.

Dossiers: `outputs/<ejecución>/research/attempt_XX.json`; también se incluyen en
reportes HTML/JSON/Markdown y en el panel, con fuentes enlazadas. La memoria SQLite persiste
entre ejecuciones y separa demo de investigación real. Se mantiene el esquema 70/30 y el OOS adaptativo existente:
**todavía se necesitan datos posteriores independientes antes de usar una estrategia**.
La nueva etapa mejora la formulación, no convierte la búsqueda continua en una prueba final.

Un retorno OOS positivo se destaca aunque falle otros filtros. «Mejor activo»
significa mejor entre estos cinco, en este período y con estos supuestos.
No se cambia de hipótesis después del primer activo rechazado.
El contador aumenta por hipótesis. Cada combinación estrategia-activo cuenta
para las múltiples pruebas. Un supervisor reinvoca el grafo; recursion_limit=16
protege cada intento, sin poner un tope al número de intentos.

## Costos y capital reducido

Supuestos iniciales editables por activo, NO tarifas verificadas de un bróker:

| Condición | Forex | Oro | BTC |
|---|---:|---:|---:|
| Comisión por lado | 0,0035% | 0,0035% | 0,6% |
| Spread completo | 2 bps | 3 bps | 5 bps |
| Deslizamiento por lado | 1 bp | 1 bp | 5 bps |
| Paso de cantidad | 1.000 unidades | 1 onza | 0,00000001 BTC |
| Mínimo nocional adicional | $0 | $0 | $10 |
| Débito diario de tenencia | 1 bp | 1 bp | 0 |
| Barras anuales | 252 | 252 | 365 |

1 bp = 0,01%. Comisión fija inicial $0. Sin apalancamiento, $100 puede no alcanzar
el mínimo FX/oro; esto debe rechazar la prueba por falta de operaciones. No se
reducen mínimos automáticamente para aprobar. El débito diario se cobra por días
de calendario, incluidos fines de semana. No se modelan créditos de swap,
impuestos ni el calendario exacto de rollover del intermediario.

En BID se añade spread completo al comprar; en ASK inverso se resta al vender.
En BTC se aplica medio spread por lado al precio de operaciones como aproximación.
El estrés multiplica comisión, spread, deslizamiento y tenencia por 1, 2 y 3.
El modelo no reconstruye el libro de órdenes histórico. Los mínimos se aplican
al abrir; se supone que se puede liquidar íntegramente una posición pequeña.

## Reportes e interfaz

El panel muestra etapa, estrategia, activo en curso, progreso sobre cinco activos,
comparación, capital, drawdown, motivos de rechazo e historial. Los reportes
HTML/JSON/Markdown incluyen IS/OOS, DSR, ventanas, estrés y procedencia por activo.
La aprobación abre el reporte. El HTML es autónomo e imprimible.

Archivos: outputs/<ejecución>/reports/attempt_XX.{html,json,md}.
Todos permanecen en disco. El panel guarda 100 recientes y permite abrir un
intento antiguo por número; 300 eventos recientes en memoria, todos en JSONL.
La clave de IA no se guarda en estado ni reportes. El servidor escucha localhost,
valida sesión para iniciar/detener y admite un solo trabajo.

## Instalación y terminal

Python 3.11+ estable. Si falta el entorno:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```powershell
# Datos públicos; Ctrl+C para detener
.\.venv\Scripts\python.exe trading_agent.py --model NOMBRE_DEL_MODELO
# Demo sin IA
.\.venv\Scripts\python.exe trading_agent.py --demo
# Prueba acotada opcional
.\.venv\Scripts\python.exe trading_agent.py --demo --max-iterations 2
# Tests
.\.venv\Scripts\python.exe -m unittest discover -v
```

--settings acepta JSON de criterios generales de Settings. Los perfiles de mercado
fijan costos y frecuencia por activo; edita costos desde el panel. El simulador
exportado descarga precios con --start y --end, no requiere IA y queda vinculado
al activo aprobado. No envía órdenes reales. Ejecutarlo sobre datos nuevos no
constituye automáticamente una nueva validación.

## Alcance estadístico

- DSR probabilístico en [0,1]: se exige >=0,95 y z>1,2. Bootstrap por bloques,
  aproximado; no certifica alfa. La referencia de múltiples pruebas considera
  presupuesto DSR y combinaciones estrategia-activo. Umbral secuencial creciente.
- IS/OOS positivos, Sharpe positivo, degradación <=20%, DD <=25%, 30 operaciones
  por segmento, PF OOS >=1,2 (o ninguna pérdida), y 2/3 ventanas positivas.
  Ventanas con reglas congeladas, sin reentrenamiento por ventana. Además se exigen
  walk-forward con separación, parámetros, Monte Carlo y retraso de señales;
  regresión y concentración son diagnósticas.
- Se reutiliza OOS adaptativamente. Una búsqueda ilimitada no garantiza hallar
  rentabilidad; los ajustes no reemplazan datos posteriores independientes.
- Long-only, sin apalancamiento. Señales y stops al cierre, ejecución en apertura
  siguiente. No fills intrabar. Drawdown medido al cierre.
- Los tres escenarios de $100 deben ganar, respetar DD, tener operaciones
  suficientes y evitar quiebra. El mínimo de operaciones no garantiza significancia.
