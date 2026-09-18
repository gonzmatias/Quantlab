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
   El investigador recibe sólo estadísticas y resultados de entrenamiento, no
   métricas OOS ni logs del validador. SQLite conserva investigaciones y rechazos.
   En modo real se rechaza repetir familia + régimen + confirmación, aunque cambien
   parámetros. No se sustituyen duplicados por variantes automáticas; esa conducta
   permanece únicamente en la demo.
2. Prototipar: reglas y parámetros congelados para los cinco activos. Costos y
   tamaños mínimos dependen del instrumento. El LLM no ejecuta código arbitrario.
3. Validar: prueba TODOS los activos antes de avanzar. Misma fecha de división
   temporal 70/30, con IS/OOS, DSR y tres ventanas progresivas.
4. Estresar: $100 con costos x1, x2 y x3 en TODOS los activos que aprobaron quant.
5. Generar: entre los aprobados en ambos filtros, selecciona mayor Sharpe OOS,
   luego menor drawdown y mayor retorno. Exporta el simulador del activo elegido.
6. Reportar: conserva resultados de los cinco. Sin aprobación vuelve a investigar.

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

La compatibilidad exige OHLC diario, activos disponibles, long-only y ausencia de
apalancamiento. Ideas que declaren necesitar volumen, noticias, libro de órdenes,
frecuencia intradía u otros campos se rechazan antes del backtest. La ficha declara
activos objetivo; se siguen probando los cinco para evaluar transferencia.

El motor conserva ocho familias y añade combinaciones acotadas: régimen de tendencia
(cierre sobre SMA lenta), baja volatilidad (desviación de retornos rápida inferior
a lenta, ambas hasta la barra anterior), y confirmación de cierre en el cuarto superior
del rango diario. Se aplican tanto al entrar como al mantener la posición. Las reglas
no representables deben declararse incompatibles, no traducirse silenciosamente.
Cambiar el texto de una explicación no evita la deduplicación estructural.

Se compara cada propuesta con su familia sin filtros, mismos parámetros y costos,
en entrenamiento. Las diferencias de retorno y Sharpe son diagnósticas, no una prueba
causal ni un nuevo filtro de aprobación. Si no se usan filtros, la diferencia será cero.
La predicción y la refutación quedan documentadas; no hay un evaluador automático
general de afirmaciones científicas. No se promete que cada propuesta sea original.

Dossiers: `outputs/<ejecución>/research/attempt_XX.json`; también se incluyen en
reportes HTML/JSON/Markdown y en el panel, con fuentes enlazadas. La memoria pertenece
a cada ejecución. Se mantiene el esquema 70/30 y el OOS adaptativo existente:
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
  Ventanas con reglas congeladas, sin reentrenamiento por ventana.
- Se reutiliza OOS adaptativamente. Una búsqueda ilimitada no garantiza hallar
  rentabilidad; los ajustes no reemplazan datos posteriores independientes.
- Long-only, sin apalancamiento. Señales y stops al cierre, ejecución en apertura
  siguiente. No fills intrabar. Drawdown medido al cierre.
- Los tres escenarios de $100 deben ganar, respetar DD, tener operaciones
  suficientes y evitar quiebra. El mínimo de operaciones no garantiza significancia.
