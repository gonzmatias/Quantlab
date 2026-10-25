# Gherkin Implementation Plan

**Goal:** Implementar el flujo aprobado de especificación a evaluación sin IA.
**Architecture:** Parser oficial + contrato estricto; traductor de RuleProgram;
evaluador independiente reutilizando backtest y registros del proyecto.
**Tech Stack:** Python, Pydantic, gherkin-official, NumPy/pandas, panel HTTP local.
**Spec:** docs/superpowers/specs/2026-10-24-gherkin-design.md

## Restricciones
- Nunca ejecutar Python generado por el modelo.
- Ninguna llamada a IA desde evaluación.
- No modificar reglas, contratos, costos o criterios para obtener aprobación.
- No ignorar pasos desconocidos ni escenarios fallidos.
- Preservar .gitignore modificado por el usuario.

## Tareas
- [x] Crear `gherkin_spec.py`: parse_markdown(text), validate_translation(document,
  program), run_scenarios(document, program). Rechazar pasos desconocidos, resultados
  esperados incompletos, escenarios vacíos y ejecución incompatible.
- [x] Crear `gherkin_pipeline.py`: translate(text, model, api_key), load_bundle(bundle),
  evaluate(bundle, manifest, output). Persistir hashes, código declarativo y escenarios.
  Evaluar con datos separados temporalmente y registrar consumo antes del holdout.
- [x] Crear `test_gherkin.py`: fixtures conocidas, traducción equivocada, cambios de
  hashes, contrato incompatible, aislamiento de IA, evaluación reproducible.
- [x] Integrar panel: cargar .md, traducir, descargar artefacto, cargar artefacto y
  evaluar sin clave. Usar el mismo bloqueo de trabajo y cancelación existentes.
- [x] Añadir ejemplo ejecutable y documentación de pasos, semántica, CLI y límites.
- [x] Ejecutar pruebas nuevas y regresiones existentes; revisión independiente y
  corrección de hallazgos antes de entregar.

Se ejecuta en esta sesión conforme a la autorización explícita de implementación.

Validación final: 149 pruebas de unittest correctas; node --check web/app.js correcto.
Revisión independiente completada; corregidos registros reiniciables, períodos observados,
comparación de zonas horarias y contradicciones en el prompt. Integración de IA probada
con cliente simulado; no se consumió una llamada real al proveedor.
