# Histórico local aprobado

El usuario autoriza Dukascopy para EURUSD, GBPUSD, USDJPY, USDCAD y XAUUSD,
2016-01-01 inclusive a 2026-01-01 exclusivo. Almacenamiento Parquet comprimido,
catálogo DuckDB y evaluación sin descargas. La descarga inicial y las actualizaciones
son acciones explícitas, reanudables, separadas de cualquier llamada de evaluación.

Se preservan originales BI5 y hashes, orden de eventos y escala por instrumento.
Particiones diarias inmutables y registros de calidad: precios, spread, gaps,
duplicados, cobertura horaria y procedencia. Faltantes, errores HTTP o datos anómalos
no se convierten en precios ni en cierres de mercado. Un 429 detiene la tarea;
404 queda como tramo faltante. Horas de fin de semana inequívocamente cerradas
se documentan; otras horas vacías exigen revisión. No se certifica equivalencia
con fills del bróker a partir de cotizaciones de otro proveedor.

Las consultas comprueban cobertura, estados y hashes antes de usar DuckDB para
seleccionar fecha/instrumento de Parquet. No ejecutan el descargador ni instalan
extensiones remotas. Conservan soporte del manifiesto CSV existente. Gherkin
sin manifiesto usa data/catalog.duckdb; acepta una carpeta/catálogo alternativos.
v1 agrega velas desde bid (quote_side=0) o ask (quote_side=2), v2 usa bid/ask.
USDJPY/USDCAD se almacenan sin invertir; evaluación sigue bloqueada donde falte
conversión JPY/CAD al capital USD. No se añade silenciosamente ese modelo.

Impacto: local_history.py (catálogo/consulta/calidad), dukascopy_history.py
(BI5/red/reanudación), history_cli.py; tick_data.py, gherkin_pipeline.py,
dashboard/web, requisitos y documentación. El modo público anterior solo leerá
caché local durante evaluación; no se permitirá fallback de red. No se migran
ni borran históricos anteriores. La reversión de código conserva los archivos.
El runtime hash cambia y exige regenerar artefactos Gherkin anteriores.

Pruebas: BI5 conocidos, orden y escala, ausencia de datos, límites HTTP,
reanudación sin red, rechazo por calidad/hash, consultas por rango y fechas,
Gherkin offline, panel, regresión completa y revisión independiente.
