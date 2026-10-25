# Evaluación con ticks y stops intrabar

Parte de [strategy-ticks-gherkin.md](examples/strategy-ticks-gherkin.md). La plantilla
usa precios sintéticos para verificar el motor; no representa una estrategia validada.
El flujo del panel y de [la terminal](GHERKIN.md#terminal) es el mismo: traducir una
vez, guardar `strategy.json` y evaluar con datos locales sin modelo ni credenciales.
La evaluación comprueba hashes, contrato y todos los escenarios antes de simular.

## Contrato v2

Declara `version: 2`, `execution_model: "tick_bid_ask_v1"`, `data_timeframe: "tick"`.
`timeframe: "tick"` evalúa reglas sobre `bid`, `ask` y `mid` por evento. Las otras
temporalidades admitidas construyen OHLC desde `signal_price`; la señal de una vela
cerrada aparece en el primer tick posterior a su cierre. Se descarta la primera
vela parcial y no se utiliza la última vela sin cerrar. No hay ticks sintéticos.

Todos los campos de `tick_execution` son obligatorios:

| Campo | Semántica |
|---|---|
| `signal_price` | `bid`, `ask` o `mid` para construir velas |
| `distance_unit` | `price`, `percent` (fracción: 0.01 = 1%) o `pips` |
| `pip_size` | Tamaño explícito con pips; `null` en los otros casos |
| `entry_order` | `market` o `limit` |
| `entry_limit_offset` | Distancia desde ask de compra o bid de venta al enviar; cero para market |
| `order_ttl_ms` | Caducidad exclusiva desde el envío; debe superar la latencia |
| `target_order` | `market` o `limit` |
| `latency_ms` | Retraso de entrada y salida market |
| `holding_unit` | `milliseconds` o `signal_events` para `max_holding` |
| `max_quote_gap_ms` | Máximo hueco permitido, incluidos bordes de segmentos |
| `return_interval` | `1h`, `4h` o `1d` para estadísticas |
| `liquidate_at_end` | Debe ser `true`: cierre en la última cotización del segmento |

Compra al ask y vende al bid, con slippage adverso. Las órdenes market solo se
llenan en un evento posterior al envío/disparo y una vez cumplida la latencia,
incluso si esta es cero. Los stops miran bid para largos y ask para cortos;
un salto de precio se ejecuta al precio disponible, no al precio teórico del stop.
Un stop disparado persiste aunque el precio se recupere. El objetivo limit exige
que el precio, después del slippage, satisfaga el límite; puede mejorar el precio.
Las protecciones se activan al llenar la entrada. No se reabre en el evento de salida.

El spread ya está en bid/ask: `spread_bps` y `quote_side` deben ser cero.
Se cobran comisión fija/proporcional y financiación por tiempo calendario, con
redondeo de cantidad y mínimo nocional. Estrés x2/x3 multiplica spread observado,
slippage, comisiones y financiación. La exposición es tiempo en posición dividido
por duración del segmento: añadir cotizaciones repetidas no la cambia.

## Datos y escenarios

CSV local, exactamente cuatro columnas:

```csv
timestamp,sequence,bid,ask
2018-01-01T00:00:00Z,0,1.0999,1.1000
2018-01-01T00:00:01Z,1,1.0999,1.1000
```

Los timestamps necesitan zona horaria; se normalizan a UTC. Se conserva el orden
de entrada: tiempo creciente, o `sequence` estrictamente creciente entre ticks
con el mismo timestamp. Precios finitos y positivos, bid ≤ ask. No se ordenan ni
rellenan datos defectuosos. Los escenarios usan `Dados los ticks`; las operaciones
esperadas incluyen secuencia de entrada/salida y motivo de salida además de precios,
cantidad, resultado y capital final. Las respuestas esperadas las escribe el autor.

El manifiesto tiene esta forma (sustituye `settings` por el objeto `costs` completo
de tu documento, sin omitir campos):

```json
{
  "datasets": [{
    "asset": "EURUSD", "source": "local", "contract": "EURUSD-spot",
    "instrument_type": "fx", "quote_currency": "USD", "timeframe": "tick",
    "file": "eurusd-ticks.csv", "settings": {},
    "coverage_start": "2018-01-01T00:00:00Z",
    "coverage_end": "2020-01-01T00:00:00Z"
  }]
}
```

Identidad, fuente, contrato y costos deben coincidir exactamente con el Gherkin.
La cobertura declarada debe abarcar la evaluación; ningún tick puede quedar fuera.
`max_quote_gap_ms` debe contemplar los cierres de mercado que acepte tu experimento.
El motor rechaza huecos mayores; no interpreta automáticamente calendarios de mercado.

## Evidencia y trazabilidad

El protocolo divide el calendario 60/20/20 y exige 60/120/120 intervalos temporales
como mínimo. Inicio y fin se alinean a la rejilla UTC de `return_interval`.
Los retornos se muestrean en esa rejilla, usando el último valor anterior a cada
frontera. `bars_per_year` debe ser 365 × 86400 / segundos del intervalo. Los ticks
no cuentan como muestras estadísticas independientes. Walk-forward utiliza ventanas
temporales disjuntas y purga según la tenencia definida.

El benchmark compra una vez con los mismos costos, capital, latencia y caducidad.
Si su entrada no llega a ejecutarse, la comparación devuelve `INSUFFICIENT_DATA` y
la evaluación `INSUFFICIENT_EVIDENCE`; no se considera una estrategia pasiva en efectivo.
La reserva histórica se consume antes de examinarla, también si falla o se interrumpe.

Además del informe y el snapshot CSV se guardan `validation_trades.json`,
`validation_orders.json`, `validation_equity.json` y, si se llega a reserva,
sus equivalentes `holdout_*`. El panel permite descargarlos junto con la auditoría.
Las marcas de tiempo del archivo equity corresponden a los retornos; el array equity
incluye un primer elemento adicional con el capital inicial.

Este modelo simula cruces de cotizaciones, sin cola, profundidad ni fills parciales.
Admite una posición en instrumentos lineales spot/FX/CFD cotizados en USD, sin
apalancamiento. Una capacidad ausente se bloquea: no se transforma la estrategia.
Pasar escenarios prueba esos casos; no demuestra equivalencia universal de texto
libre. `HISTORICAL_EVIDENCE` describe evidencia histórica bajo el protocolo,
con validación prospectiva pendiente, y no garantiza rentabilidad futura.
