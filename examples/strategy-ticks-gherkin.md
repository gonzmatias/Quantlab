# Ejemplo didáctico: ticks y stop intrabar

Los precios y tarifas son fixtures, no un histórico ni condiciones de un bróker.
El único bloque Gherkin es normativo. Reemplaza contrato, reglas, costos y fechas
antes de evaluar datos reales. Distancias en precio, sin porcentaje implícito.

```gherkin
# language: es
Característica: Entrada por tick y protección intrabar
  Antecedentes:
    Dado el contrato
      """
      {
        "version": 2,
        "asset": "EURUSD",
        "instrument_type": "fx",
        "quote_currency": "USD",
        "source": "CSV de ticks del usuario",
        "contract": "EURUSD FX sin apalancamiento",
        "data_timeframe": "tick",
        "timeframe": "tick",
        "direction": "long",
        "allocation": 0.5,
        "stop_loss": 0.0005,
        "take_profit": 0.001,
        "max_holding": 60000,
        "execution_model": "tick_bid_ask_v1",
        "tick_execution": {
          "signal_price": "mid",
          "distance_unit": "price",
          "pip_size": null,
          "entry_order": "market",
          "entry_limit_offset": 0,
          "order_ttl_ms": 10000,
          "target_order": "limit",
          "latency_ms": 0,
          "holding_unit": "milliseconds",
          "max_quote_gap_ms": 86400000,
          "return_interval": "1d",
          "liquidate_at_end": true
        },
        "costs": {
          "bars_per_year": 365,
          "quote_side": 0,
          "commission_rate": 0,
          "fixed_commission": 0,
          "spread_bps": 0,
          "slippage_bps": 0,
          "quantity_step": 1000,
          "min_notional": 0,
          "holding_cost_bps": 0,
          "allow_short": true
        },
        "evaluation": {
          "start": "2018-01-01T00:00:00Z",
          "end": "2020-01-01T00:00:00Z",
          "protocol": "fixed-holdout-v1",
          "settings": {"capital": 10000}
        }
      }
      """
    Y la regla de entrada
      """
      {"text":"Entrar cuando bid sea igual a 1.0999.","expression":"col('bid') == 1.0999"}
      """
    Y la regla de salida
      """
      {"text":"Salir cuando bid sea menor que 1.0991.","expression":"col('bid') < 1.0991"}
      """

  Escenario: Señales con entradas y salidas verdaderas y falsas
    Dados los ticks
      """
      [
        {"timestamp":"2020-01-01T00:00:00Z","sequence":0,"bid":1.0999,"ask":1.1000},
        {"timestamp":"2020-01-01T00:00:01Z","sequence":1,"bid":1.0999,"ask":1.1000},
        {"timestamp":"2020-01-01T00:00:02Z","sequence":2,"bid":1.0994,"ask":1.0995},
        {"timestamp":"2020-01-01T00:00:03Z","sequence":3,"bid":1.0990,"ask":1.0991}
      ]
      """
    Cuando calculo las señales
    Entonces el resultado exacto
      """
      {"entry":[true,true,false,false],"exit":[false,false,false,true]}
      """

  Escenario: Stop atravesado y ejecutado al siguiente bid disponible
    Dados los ticks
      """
      [
        {"timestamp":"2020-01-01T00:00:00Z","sequence":0,"bid":1.0999,"ask":1.1000},
        {"timestamp":"2020-01-01T00:00:01Z","sequence":1,"bid":1.0999,"ask":1.1000},
        {"timestamp":"2020-01-01T00:00:02Z","sequence":2,"bid":1.0994,"ask":1.0995},
        {"timestamp":"2020-01-01T00:00:03Z","sequence":3,"bid":1.0990,"ask":1.0991}
      ]
      """
    Cuando ejecuto la estrategia
    Entonces el resultado exacto
      """
      {"trades":[{"entry_timestamp":"2020-01-01 00:00:01+00:00","exit_timestamp":"2020-01-01 00:00:03+00:00","direction":"long","quantity":4000,"entry_price":1.1,"exit_price":1.099,"pnl":-4,"entry_sequence":1,"exit_sequence":3,"exit_reason":"stop"}],"final_equity":9996}
      """

  Escenario: Objetivo limitado con precio favorable antes de un descenso
    Dados los ticks
      """
      [
        {"timestamp":"2020-01-01T00:00:00Z","sequence":0,"bid":1.0999,"ask":1.1000},
        {"timestamp":"2020-01-01T00:00:01Z","sequence":1,"bid":1.0999,"ask":1.1000},
        {"timestamp":"2020-01-01T00:00:02Z","sequence":2,"bid":1.1012,"ask":1.1013},
        {"timestamp":"2020-01-01T00:00:03Z","sequence":3,"bid":1.0990,"ask":1.0991}
      ]
      """
    Cuando ejecuto la estrategia
    Entonces el resultado exacto
      """
      {"trades":[{"entry_timestamp":"2020-01-01 00:00:01+00:00","exit_timestamp":"2020-01-01 00:00:02+00:00","direction":"long","quantity":4000,"entry_price":1.1,"exit_price":1.1012,"pnl":4.8,"entry_sequence":1,"exit_sequence":2,"exit_reason":"target_limit"}],"final_equity":10004.8}
      """
```
