# Ejemplo de contrato ejecutable

Solo el bloque Gherkin es normativo. Los precios y costos de este ejemplo son
didácticos, no datos ni tarifas reales. Sustituye contrato, costos, período y reglas.
`expression`, cuando existe, es normativa; `text` la explica. Sin `expression`,
la fidelidad de la traducción se comprueba únicamente contra los escenarios.

```gherkin
# language: es
Característica: Entrada sobre un nivel y salida bajo el nivel
  Antecedentes:
    Dado el contrato
      """
      {
        "version": 1,
        "asset": "EURUSD",
        "instrument_type": "fx",
        "quote_currency": "USD",
        "source": "CSV del usuario",
        "contract": "EURUSD FX sin apalancamiento",
        "data_timeframe": "1d",
        "timeframe": "1d",
        "direction": "long",
        "allocation": 1,
        "stop_loss": 0.1,
        "take_profit": 0.5,
        "max_holding": 10,
        "execution_model": "bar_open_market; stops_at_signal_close; estimated_fills",
        "costs": {
          "bars_per_year": 252,
          "quote_side": 0,
          "commission_rate": 0,
          "fixed_commission": 0,
          "spread_bps": 0,
          "slippage_bps": 0,
          "quantity_step": 1,
          "min_notional": 0,
          "holding_cost_bps": 0,
          "allow_short": true
        },
        "evaluation": {
          "start": "2018-01-01T00:00:00Z",
          "end": "2026-01-01T00:00:00Z",
          "protocol": "fixed-holdout-v1",
          "settings": {"capital": 1040}
        }
      }
      """
    Y la regla de entrada
      """
      {"text": "Entrar cuando el cierre sea estrictamente mayor que 1 USD.", "expression": "col('close') > 1"}
      """
    Y la regla de salida
      """
      {"text": "Salir cuando el cierre sea estrictamente menor que 1 USD.", "expression": "col('close') < 1"}
      """

  Escenario: Igualdad no activa ninguna señal
    Dadas las barras
      """
      [
        {"timestamp":"2020-01-01T00:00:00Z","open":1,"high":1,"low":1,"close":1},
        {"timestamp":"2020-01-02T00:00:00Z","open":1.02,"high":1.02,"low":1.02,"close":1.02},
        {"timestamp":"2020-01-03T00:00:00Z","open":1.04,"high":1.04,"low":1.04,"close":1.04},
        {"timestamp":"2020-01-04T00:00:00Z","open":0.99,"high":0.99,"low":0.99,"close":0.99},
        {"timestamp":"2020-01-05T00:00:00Z","open":0.98,"high":0.98,"low":0.98,"close":0.98}
      ]
      """
    Cuando calculo las señales
    Entonces el resultado exacto
      """
      {"entry":[false,true,true,false,false],"exit":[false,false,false,true,true]}
      """

  Escenario: Ejecución en la apertura posterior y salida por condición
    Dadas las barras
      """
      [
        {"timestamp":"2020-01-01T00:00:00Z","open":1,"high":1,"low":1,"close":1},
        {"timestamp":"2020-01-02T00:00:00Z","open":1.02,"high":1.02,"low":1.02,"close":1.02},
        {"timestamp":"2020-01-03T00:00:00Z","open":1.04,"high":1.04,"low":1.04,"close":1.04},
        {"timestamp":"2020-01-04T00:00:00Z","open":0.99,"high":0.99,"low":0.99,"close":0.99},
        {"timestamp":"2020-01-05T00:00:00Z","open":0.98,"high":0.98,"low":0.98,"close":0.98}
      ]
      """
    Cuando ejecuto la estrategia
    Entonces el resultado exacto
      """
      {"trades":[{"entry_timestamp":"2020-01-03 00:00:00+00:00","exit_timestamp":"2020-01-05 00:00:00+00:00","direction":"long","quantity":1000,"entry_price":1.04,"exit_price":0.98,"pnl":-60}],"final_equity":980}
      """
```
