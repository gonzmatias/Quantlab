# Contratos, temporalidades y ejecución

La fuente pública mantiene barras diarias Dukascopy/Coinbase. Un short de FX/oro
está admitido en el modelo; un short de BTC spot no. Un contrato diferente requiere
otro identificador y condiciones explícitas. No hay conversiones silenciosas entre
BTC spot/CFD ni entre CADUSD/USDCAD.

## Datos locales

Crear un manifiesto como este junto a `eurusd_m1.csv`:

```json
{
  "datasets": [{
    "asset": "EURUSD",
    "file": "eurusd_m1.csv",
    "source": "Exportación del intermediario elegido",
    "contract": "Especificación del contrato y cuenta a verificar",
    "quote_currency": "USD",
    "instrument_type": "fx",
    "timeframe": "1m",
    "settings": {
      "bars_per_year": 362880,
      "quote_side": 1,
      "commission_rate": 0.000035,
      "fixed_commission": 0,
      "spread_bps": 2,
      "slippage_bps": 1,
      "quantity_step": 1000,
      "min_notional": 0,
      "holding_cost_bps": 1,
      "allow_short": true
    }
  }]
}
```

Los números son ejemplos, no tarifas verificadas. `bars_per_year` expresa barras
de ejecución anuales del contrato; no número de señales. El manifiesto debe declarar
costos y mínimos; no puede cambiar filtros estadísticos. Tipos admitidos: `spot`,
`fx`, `cfd`, con precios cotizados en USD. Futuros, préstamo spot, funding de perpetuos
y apalancamiento necesitan modelos adicionales y no se simulan como contratos equivalentes.

El CSV tiene `timestamp,open,high,low,close`, al menos 600 barras; investigación real
necesita además historia suficiente para descubrimiento y reserva. `timestamp` es
la APERTURA de cada barra UTC. No se aceptan barras futuras/incompletas, repetidas,
precios inválidos ni desalineación con la grilla declarada. Se conservan los huecos.

Opcionalmente:

- `volume`: volumen real del proveedor.
- `bid_open,ask_open,bid_close,ask_close`: cotizaciones observadas; deben venir las cuatro.
- `spread_bps,slippage_bps,holding_cost_bps`: costos de esa barra; valores faltantes o negativos fallan.

Cuando hay bid/ask se usa el lado correspondiente y no se añade otra vez el spread
configurado. El estrés amplía la distancia bid/ask alrededor del punto medio.
Sin bid/ask se conserva la aproximación BID/ASK/mid indicada por `quote_side`.
La financiación es un débito por tiempo calendario; no reproduce créditos ni todo
el calendario de swaps del intermediario. El modelo limita el nocional inicial al
capital asignado; una posición corta aún puede generar pérdidas mayores al capital.

```powershell
.\.venv\Scripts\python.exe trading_agent.py --data-manifest C:\datos\manifest.json --model NOMBRE_MODELO
```

También está disponible **Datos del intermediario** en el panel. Para un simulador
Python exportado se utiliza `--csv` con el contrato y resolución originales.

## Señal y ejecución

`Hypothesis.direction` admite `long` o `short`; cada hipótesis tiene una dirección.
Las dos direcciones son pruebas distintas, no una inversión automática de reglas.
No se implementan posiciones largas/cortas simultáneas en una misma hipótesis.

`timeframe` admite `1m,5m,15m,30m,1h,4h,1d`. M1 puede construir M5; D1 no puede
inventar M5. Se requieren todas las barras constituyentes para cerrar una señal
agregada. Los indicadores reciben sólo esas velas cerradas; la señal se ejecuta
en una apertura posterior disponible. Stops y tenencia siguen la cadencia de señal.
Las columnas de otros mercados se incorporan después del cierre de su propia barra.

Actualmente son órdenes de mercado y stops detectados al cierre de señal. No hay
órdenes limitadas, prioridad de cola, fills parciales simulados, stops intrabar ni
replay de ticks/libro. Una hipótesis que necesita estas capacidades queda pendiente,
aunque su temporalidad figure en el catálogo. Scalping con OHLC no se certifica como
ejecución de alta fidelidad. `execution_delay_bars` permite investigar retrasos de
ejecución pero bloquea aprobación/exportación hasta disponer de paridad EA de ese modo.

`max_drawdown` sigue siendo al cierre. `intrabar_drawdown_lower_bound` mide la caída
desde máximos de cierres anteriores hasta el extremo adverso de barras con posición.
No conoce la secuencia de extremos, spreads intrabar ni precio liquidable real.
Si cualquiera de las dos medidas supera el límite se rechaza el riesgo observado.

## MT5, shadow y comparación

El EA usa la temporalidad y dirección congeladas; `EnableTrading=false` es el valor
inicial. En ese modo guarda señales y bid/ask en `Terminal/Common/Files/quantlab_shadow_<firma>.csv`.
Son cotizaciones observadas y decisiones, no fills de órdenes que nunca se enviaron.

El paquete incluye `reference_signals.csv` y `reference_trades.csv`. La referencia
es un replay histórico desde el calentamiento del EA; no es la curva de la reserva.
Hay que usar el mismo período, datos, zona horaria, contratos e inicialización en MT5.

```powershell
.\.venv\Scripts\python.exe verify_execution.py C:\ruta\ea --signals C:\datos\mt5_signals.csv --trades C:\datos\mt5_trades.csv
```

Se escribe `parity_report.json`. Señales: timestamp, entry, exit, direction. Operaciones:
entry_timestamp, exit_timestamp, direction, quantity, entry_price, exit_price, pnl.
`quantity` usa unidades del contrato, no lotes sin convertir. Los timestamps son UTC.
La referencia de señales excluye calentamiento y la última vela sin apertura posterior.
Para shadow iniciado hoy, una referencia de otro intervalo será rechazada: hay que
comparar un replay del mismo intervalo, no interpretar la falta de histórico como paridad.

`PARTIAL` sólo compara señales; `MATCHED` requiere además operaciones coincidentes.
Tolerancias explícitas: precios/cantidad 1e-8, PnL 0.01 USD por defecto; no se ajustan
automáticamente. Se comprueban hashes y se rechazan faltantes/duplicados. El archivo
de operaciones se obtiene de MT5/intermediario y se normaliza a ese esquema.
El comparador no autentica al proveedor de la traza ni autoriza operativa.

No se ejecuta MetaEditor ni se simula su certificación. Compilación, ticks reales,
sesiones del bróker y microcapital siguen pendientes hasta disponer de esos entornos.
