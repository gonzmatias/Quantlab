# Implementación ticks y stops intrabar

**Goal:** Ejecutar estrategias Gherkin v2 con ticks, sin IA en evaluación.
**Architecture:** Cargador estricto, motor de eventos y evaluador temporal separados.
**Tech Stack:** Python, pandas, NumPy, Pydantic, unittest.
**Spec:** docs/superpowers/specs/2026-10-24-ticks-design.md

- [x] Casos de prueba con precios esperados a mano antes del motor.
- [x] tick_data.py: contrato de ejecución, validate_ticks y manifiesto con cobertura.
- [x] tick_engine.py: tick_signals y replay_ticks; trazas, costos y muestreo temporal.
- [x] Integración Gherkin v2 y escenarios `los ticks`, sin llamadas adicionales IA.
- [x] tick_evaluation.py: protocolo temporal, auditoría y reserva, métricas sin ticks como muestras.
- [x] Plantilla, documentación y enlace del panel.
- [x] Tests, cobertura de módulos nuevos, regresiones y revisión independiente.

Se implementa en esta sesión con autorización explícita. El entorno usa unittest.

Revisión: benchmark sin entrada bloquea la comparación; exposición por duración,
no cantidad de ticks. Prueba de navegador detectó y corrigió pérdida de notación
numérica del artefacto al pasar por JavaScript: transporte como JSON textual intacto.
Evidencia y comandos: `docs/ticks-verification.md`.
