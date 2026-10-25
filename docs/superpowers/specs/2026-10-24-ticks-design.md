# Motor de ticks aprobado

El usuario aprobó datos bid/ask, señales por tick o vela cerrada, ejecución por
eventos, órdenes de mercado/limitadas, stops intrabar, objetivos mercado/limitados,
latencia, deslizamiento y métricas en intervalos de tiempo. IA solo traduce reglas.

Contrato Gherkin v2, execution_model=tick_bid_ask_v1, data_timeframe=tick y bloque
tick_execution obligatorio: signal_price, distance_unit, pip_size, entry_order,
entry_limit_offset, order_ttl_ms, target_order, latency_ms, holding_unit,
max_quote_gap_ms, return_interval y liquidate_at_end. No valores implícitos.
Una posición, nocional sin apalancamiento, contrato lineal cotizado en USD. Otros
modelos se identifican como NEEDS_CAPABILITY. No se simula prioridad de cola.

Ticks: timestamp UTC explícito, sequence entero, bid, ask. Orden por tiempo y
secuencia; un timestamp repetido requiere sequence mayor. Sin ordenar/rellenar
precios silenciosamente. Manifiesto declara cobertura y contrato. CSV local.

Las señales crean órdenes ejecutables a partir del siguiente evento y después
de latency_ms. Las órdenes limitadas usan precio cotizado después de slippage
y solo llenan si ese precio satisface el límite. TTL desde la creación, exclusivo.
Stops mercado y objetivos mercado conservan el disparo aunque el precio vuelva.
Brackets activos desde el fill de entrada. Stop precede a objetivo; órdenes de
salida pendientes se conservan. Señal de salida cancela entrada pendiente.
Sin reentrada en el evento de salida. Liquidación final explícita en último tick.

Precios de señal por tick o agregación de velas completas no vacías; una vela se
conoce al llegar el primer tick de la siguiente. No señales de velas incompletas.
Tenencia en milisegundos o eventos de señal según contrato. Datos anteriores al
segmento solo calientan indicadores; cada segmento empieza sin posiciones.

Curva mark-to-market al bid para largos y ask para cortos, descontando fricciones
de cierre. Retornos en rejilla UTC fija 1h/4h/1d con última valoración conocida;
no se interpreta cada tick como muestra independiente. Drawdown también intratick.
Protocolo fijo 60/20/20, diagnóstico causal, costos x1/x2/x3, DSR aproximado,
walk-forward, benchmark y Monte Carlo. Auditoría estable y reserva global
consumida antes del cálculo. Falta de muestra es insuficiencia, no rentabilidad.

Impacto: nuevos módulos tick_data.py, tick_engine.py, tick_evaluation.py;
adaptadores en gherkin_spec.py/gherkin_pipeline.py; plantilla y guía; tests.
No cambia la semántica de barras. La versión/hash del runtime evita evaluar
artefactos congelados con código diferente. Se conservan fuentes para traducir
de nuevo con una revisión distinta; no se migran resultados históricos.
