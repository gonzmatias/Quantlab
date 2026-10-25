# Evaluación de estrategias definidas en Gherkin

Diseño aprobado por el usuario: Markdown como fuente de verdad; IA únicamente
para traducir reglas; comprobación y evaluación mediante herramientas deterministas.
No investigar, optimizar ni sustituir estrategias. Capacidades ausentes bloquean
la evaluación y se detallan. Instrumentos definidos por el documento.

El documento contiene un bloque Gherkin oficial, un contrato JSON en Antecedentes,
reglas de entrada/salida y escenarios con datos y resultados esperados escritos por
el autor. Los pasos disponibles tienen semántica versionada. Pasos desconocidos,
contradicciones estructurales y campos adicionales fallan cerrados.

La traducción produce RuleProgram (AST restringido), sin Python arbitrario. Se
conservan documento, reglas, trazabilidad, escenarios, versión y hashes del motor.
Los escenarios se ejecutan antes de congelar y al cargar el artefacto. Pasarlos
demuestra conformidad con esos ejemplos, no equivalencia universal de lenguaje
natural. Las reglas pueden incluir una expresión normativa que se compara sin IA.

La evaluación recibe únicamente el artefacto y el manifiesto de datos. Valida
contrato, costos, resolución, período y hashes, conserva snapshots, registra cada
ensayo y consume la reserva final global antes de leer sus resultados. Herramientas:
backtest, causalidad, DSR, walk-forward, costos, Monte Carlo y benchmark. Los
parámetros de la estrategia no se optimizan ni perturban automáticamente.

El primer adaptador reutiliza la semántica del motor de barras existente, declarada
explícitamente en el contrato. Intrabar, ticks, órdenes limitadas, otros contratos
o reglas no expresables deben producir NEEDS_CAPABILITY; nunca simularse como
equivalentes. Esto es un flujo extensible, no soporte universal de ejecución.

CLI con translate/evaluate y panel con carga de Markdown/artefacto, resultados y
descargas. No se requiere modelo ni clave en evaluación. La conclusión separa
conformidad, evidencia histórica, insuficiencia y capacidades ausentes.
