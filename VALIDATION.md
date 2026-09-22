# Investigación y confirmación

## Qué se conserva

AST seguro, LangGraph, investigación adaptativa en descubrimiento, 60/20/20 inicial,
DSR aproximado con conteo persistente, benchmark long pasivo, walk-forward de reglas
congeladas, Monte Carlo por bloques, estrés x1/x2/x3 y exportación determinista.
El candidato se congela ANTES de reservar/consultar el holdout global. Un fallo,
reinicio o cambio de activo no devuelve el intervalo.

## Qué se amplía

- La ficha distingue replicación, adaptación y conjetura. Una conjetura puede no
  citar publicaciones; la búsqueda web sigue siendo real y obligatoria para contexto
  y evidencia contraria. Un fallo de búsqueda no se convierte en fuentes inventadas.
- Fuentes: pasaje de respaldo, fecha declarada y alcance. Supuestos y correspondencia
  mecanismo-reglas quedan separados. `evidence_review` identifica documentación ausente;
  no certifica que un pasaje sea auténtico o que una interpretación científica sea correcta.
- Auditoría de señales: invariancia al truncar el futuro y al alterar sus valores.
  Se ejecuta antes de evaluar estadísticamente una hipótesis. No elimina conocimiento
  retrospectivo del investigador ni errores de vintage en la fuente.
- Experimento preliminar IS: retornos brutos a 1/5/20 barras de señal después de la
  próxima apertura. Labels solapados, sin significancia; es diagnóstico para investigar.
- Fingerprint de señales en entrenamiento: permite comparar comportamiento además
  de la firma canónica del AST. Se registra; no se presume identidad económica sólo por
  coincidir en un tramo ni se cambia automáticamente el conteo DSR.
- Perturbaciones individuales y conjuntas: al menos 80% en cada grupo por separado.
  Los rangos y la semilla se fijan antes; no se elige el parámetro ganador.
- Regímenes: volatilidad/tendencia causal, umbral de volatilidad calculado en entrenamiento.
  Los retornos agrupados no constituyen una nueva curva operable.
- Placebos: desplazamientos aleatorios de eventos con costos y gestión de riesgo;
  se reportan exposición y operaciones efectivamente resultantes. No son un p-value
  ni evidencia independiente. Sólo están disponibles cuando señal y ejecución tienen
  la misma resolución; el caso multirresolución se declara no disponible.

## Registro y reproducibilidad

SQLite versión 2 agrega `audit_events`, triggers append-only y encadenamiento SHA256,
sin borrar tablas anteriores. Cada reserva estadística y versión de resultados añade
un evento. La copia `research_audit.json` permite custodia y conciliación externas.
Esta migración conserva un punto de partida del historial existente; no recupera
experimentos borrados y no impide que el propietario elimine todas sus copias.

El conteo estadístico acumula combinaciones estrategia-activo entre ejecuciones.
Dirección y temporalidad forman parte de la firma de estrategia. DSR sigue siendo
aproximado: una cifra nominal de trials no certifica independencia ni corrige toda
la búsqueda adaptativa, literatura seleccionada o conocimiento del modelo.

El candidato nuevo registra hashes de código, datos y configuración. La comprobación
prospectiva exige el motor original y observaciones posteriores al instante de congelación.
Espera 120 barras de señal y un mínimo de 90 días calendario por defecto; ambos criterios
se fijan en la configuración. Evalúa el primer horizonte elegible una vez, no una ventana
que se alarga hasta ganar. `--data-manifest` acepta datos nuevos del contrato local.

## Evaluar al investigador

`research_scorecard.py` inventaría todos los candidatos congelados presentes y sus
resultados posteriores, incluidos fallos y pendientes. No considera independientes
varios candidatos expuestos al mismo mercado. Hay que reconciliarlo con auditoría
externa para detectar carpetas eliminadas.

`calibrate_research.py` ejecuta selección y filtros de descubrimiento en escenarios
sintéticos nulos y con efecto bruto conocido. Acepta candidatos predeclarados por JSON
y conserva configuración, semillas y motivos de rechazo. El efecto bruto puede no
superar costos o los requisitos de robustez. No ajusta umbrales para forzar aceptación.
No calibra al LLM, la búsqueda web ni el protocolo completo de producción; eso requiere
experimentos prospectivos y suficientes repeticiones independientes.

PBO/CSCV no se añadió como filtro universal. No hay promesa de 95% de probabilidad de
ganar, ni activación automática de microcapital. Una versión nueva del investigador
debe evaluarse por evidencia posterior y por errores conocidos, no por su mejor backtest.
