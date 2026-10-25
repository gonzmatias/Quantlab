# Estrategias definidas en Markdown con Gherkin

El panel incluye **Estrategia desde un documento Gherkin**. Carga el `.md`, configura
modelo y clave y pulsa **Traducir y verificar escenarios**. Descarga `strategy.json`.
Después indica un manifiesto local y pulsa **Evaluar sin IA**. Puedes cargar ese
JSON en otra sesión: no necesita modelo ni clave. El motor y las versiones de sus
dependencias deben coincidir con los hashes guardados.

La traducción llama al modelo una vez, sin búsqueda web ni datos de mercado. Produce
un `RuleProgram` (código declarativo ejecutable por el intérprete restringido del
proyecto); no se ejecuta Python arbitrario generado por el modelo. No se envían las
barras, ticks ni las respuestas de los escenarios al modelo. Un fallo no inicia un ciclo
de reparación/optimización automática.

## Formato del documento

Usa [la plantilla completa](examples/strategy-gherkin.md). Solo el bloque cercado
`gherkin` es normativo; el Markdown exterior sirve para comentarios. El parser
oficial reconoce Gherkin; las definiciones de pasos de este adaptador son:

| Sección | Paso | Argumento DocString |
|---|---|---|
| Antecedentes | `Dado el contrato` | JSON del contrato y protocolo |
| Antecedentes | `Y la regla de entrada` | JSON con `text`, opcional `expression` |
| Antecedentes | `Y la regla de salida` | JSON con `text`, opcional `expression` |
| Escenario | `Dadas las barras` | Array JSON de barras OHLC UTC |
| Escenario v2 | `Dados los ticks` | Array JSON de timestamp, sequence, bid, ask |
| Escenario | `Cuando calculo las señales` | Ninguno |
| Escenario alternativo | `Cuando ejecuto la estrategia` | Ninguno |
| Escenario | `Entonces el resultado exacto` | Objeto JSON con resultados esperados |

Las palabras clave pueden localizarse con `# language: es`. Los textos de pasos
de la tabla son exactos. Pasos adicionales, Rules, Scenario Outlines, tablas,
etiquetas o descripciones libres dentro del bloque no se ignoran: quedan bloqueados.
Estos elementos requieren una definición ejecutable antes de poder evaluarse.

Las reglas `text` admiten lenguaje natural traducible a las expresiones del motor.
Con `expression`, esa expresión es normativa y su árbol sintáctico se compara sin
IA contra la traducción. Sin ella, pasar los escenarios verifica los ejemplos,
**no demuestra equivalencia universal entre lenguaje natural y código**. Escribe
ejemplos de límites, ausencia de señal, entradas, salidas, stops y objetivos.

Los resultados esperados los proporciona el autor, no el traductor. Se exige
entrada y salida verdaderas/falsas, y al menos una operación comprobada. La
comparación exige todas las señales, todas las operaciones y capital final; no
se admiten aserciones vacías ni un subconjunto de operaciones. Tolerancia numérica
absoluta `1e-8`, sin tolerancia relativa. Timestamps según el formato de la plantilla.

## Contrato y capacidades

El documento fija símbolo exacto, tipo de instrumento, moneda, fuente, contrato,
resolución, temporalidad, dirección, asignación, stop, objetivo y tenencia máxima.
No se cambia un símbolo por otro: JPYUSD no es USDJPY; USDCAD no es CADUSD.
`costs` requiere todos los campos de la plantilla y debe coincidir con el manifiesto.
`evaluation.start` y `evaluation.end` son fechas con zona horaria, fin exclusivo.
Los datos deben cubrir ese intervalo, sin inventar precios para rellenarlo.

El contrato v1 utiliza exactamente el modelo existente:
`bar_open_market; stops_at_signal_close; estimated_fills`. Señales de velas
cerradas, ejecución en la apertura posterior disponible, stop/objetivo porcentuales
respecto al precio de entrada, una posición, sin apalancamiento. El motor liquida
al cierre de cada segmento, cobra costos, redondea cantidad al paso del contrato,
no reabre en la barra de salida y no abre si coinciden señales de entrada y salida.
La tenencia se cuenta por barras de señal. Los escenarios empiezan en efectivo.

Admite M1/M5/M15/M30/H1/H4/D1 si los datos permiten construirlas, long/short según
contrato, instrumentos spot/FX/CFD cotizados en USD. El **estilo** no se deduce del
nombre de la estrategia: la compatibilidad depende de sus reglas y datos.

El contrato **v2** incorpora `tick_bid_ask_v1`: cotizaciones bid/ask, stops intrabar,
entradas market/limit y objetivos market/limit, latencia y caducidad explícitas.
Usa [la plantilla de ticks](examples/strategy-ticks-gherkin.md) y consulta
[el contrato de ejecución y datos](TICKS.md). Las señales pueden evaluarse en cada
tick o con velas cerradas construidas a partir de esas cotizaciones.

Futuros, perpetuos, varias posiciones, apalancamiento, divisas de cotización distintas
de USD, profundidad de mercado y costos variables requieren nuevos adaptadores.
El flujo devuelve `NEEDS_CAPABILITY`; nunca cambia esas reglas para hacerlas pasar.
Por ejemplo, USDCAD cotizado en CAD no puede evaluarse todavía con este adaptador.
La ampliación de capacidades debe preservar escenarios, trazabilidad y aislamiento
de IA. No basta con quitar la comprobación para declarar soporte.

## Protocolo estadístico fixed-holdout-v1

Nombrarlo en el documento selecciona explícitamente este protocolo versionado:

- División calendario 60% entrenamiento, 20% validación, 20% reserva histórica.
- Causalidad antes de estadísticas; reglas idénticas en todos los segmentos.
- Backtest neto y criterios `viable` (ganancias, Sharpe, drawdown, solvencia,
  mínimo de operaciones), DSR aproximado y umbral secuencial con conteo persistente.
- Walk-forward de reglas congeladas, benchmark pasivo, Monte Carlo por bloques
  y costos x1/x2/x3. Sin selección ni perturbación automática de parámetros.
- Intervalo bootstrap de la media neta por barra (v1) o intervalo temporal (v2) como diagnóstico; no es una
  probabilidad de rentabilidad futura ni está corregido por selección.
- Solo tras superar descubrimiento se consume la reserva global compartida con
  el modo anterior. La reserva exige 120 barras de señal (v1) o intervalos temporales (v2) y el mínimo calendario,
  y evalúa su primer prefijo elegible con costos, benchmark, Monte Carlo y retraso.
  Se consume todo el intervalo reservado incluso si ese prefijo es más corto.
  Un fallo o interrupción no restaura la reserva.

`evaluation.settings` permite declarar los campos de `Settings`, excepto los
costos, que solo aparecen en `costs`. Los valores no sobrescritos corresponden a
la versión del protocolo/motor y se guardan completos en el artefacto y reporte.
Los controles generales del panel no sustituyen esta configuración.

Estados: `CONFORMANCE_FAILED`, `NEEDS_CLARIFICATION`, `NEEDS_CAPABILITY`,
`NEEDS_DATA`, `CONTRACT_MISMATCH`, `INSUFFICIENT_DATA`, `INSUFFICIENT_EVIDENCE`,
`CRITERIA_NOT_MET`, `HOLDOUT_ALREADY_USED` y `HISTORICAL_EVIDENCE`.
El último significa evidencia histórica favorable bajo ese protocolo, con
validación prospectiva pendiente. No equivale a rentabilidad futura demostrada.
Los escenarios y las métricas se presentan por separado.

## Terminal

```powershell
.\.venv\Scripts\python.exe gherkin_pipeline.py translate examples\strategy-gherkin.md --model MODELO --output outputs\mi_traduccion
.\.venv\Scripts\python.exe gherkin_pipeline.py evaluate outputs\mi_traduccion\strategy.json --data-manifest C:\datos\manifest.json --output outputs\mi_evaluacion
```

La primera orden usa `OPENAI_API_KEY`; la segunda no necesita credenciales de IA.
Cada carpeta de salida debe ser nueva. El registro se guarda en `<proyecto>/outputs` independientemente
de la carpeta de resultados. Se registran también los intervalos de descubrimiento
observados para impedir reutilizarlos como reserva. Los comandos anteriores y el
panel comparten ese directorio. Ejecuciones antiguas guardadas fuera de él no se
importan automáticamente y requieren conciliación; esta versión no puede reconstruir
conocimiento previo no registrado. Borrar o mover los SQLite rompe la continuidad
de la auditoría. Los hashes detectan cambios, no autentican
al autor frente a alguien que pueda modificar y recalcular todos los archivos.

La traducción guarda Markdown, artefacto, programa y conformidad. La evaluación
guarda además snapshot CSV, hash de datos, operaciones de validación, auditoría y
reportes JSON/Markdown/HTML. Los fallos del panel se guardan en `failure.json`.
La cancelación es cooperativa; una petición al modelo ya enviada puede completar
y generar consumo antes de que se atienda la cancelación.
