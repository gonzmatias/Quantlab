# Verificación del motor de ticks — 2026-10-25

Se ejecutaron pruebas unitarias, integración con manifiestos y reserva SQLite,
HTTP y un recorrido real del panel en Edge sin ventana. Datos y reglas sintéticos:
estas pruebas verifican implementación, no rentabilidad de ninguna estrategia.

- Suite completa: 179 pruebas de Python pasaron antes del último ajuste de
  transporte del artefacto en el panel. Después del ajuste pasaron 33 regresiones
  de dashboard, Gherkin e integración tick, más una nueva prueba específica del
  transporte de la traducción (34 comprobaciones posteriores).
- Cobertura con ramas de los tres módulos tick: 91% global; tick_data 88%,
  tick_engine 94%, tick_evaluation 88%.
- E2E: cargar JSON, evaluar sin modelo/clave, mostrar estado y descargar trazas.
  `ask_model` y `socket.socket.connect` fallan deliberadamente si se invocan en
  el servidor de prueba. El navegador solo permite solicitudes al servidor local.
- Revisión independiente: corregidos benchmark sin entrada y exposición por ticks;
  segunda revisión sin otros defectos importantes.

Regresiones específicas cubren stops long/short por bid/ask, gaps, secuencia entre
ticks simultáneos, límites y slippage, latencia, caducidad, comisión y financiación,
insolvencia, exposición temporal, causalidad de velas, reserva consumida tras fallos,
insuficiencia del benchmark y artefactos con decimales. Las pruebas de orquestación
fuerzan algunos criterios estadísticos para alcanzar las ramas finales; no se usan
como evidencia financiera. La evaluación real conserva sus criterios sin mocks.

## Reproducir

```powershell
.\.venv\Scripts\python.exe -m unittest discover
.\.venv\Scripts\python.exe -m coverage run --branch --data-file=outputs/.coverage-ticks --source=tick_data,tick_engine,tick_evaluation -m unittest test_ticks test_tick_pipeline test_gherkin
.\.venv\Scripts\python.exe -m coverage report --data-file=outputs/.coverage-ticks -m
node --check web/app.js
node tests/ticks_browser.cjs
```

Cobertura requiere `coverage` en el entorno de desarrollo. El E2E requiere Node,
Playwright resoluble por `require('playwright')` y Edge instalado. En esta sesión
se usó Playwright del runtime de Codex mediante `NODE_PATH`, sin añadir dependencias
de navegador a producción. `TEST_PYTHON` permite elegir Python y `BROWSER_CHANNEL`
el navegador. El servidor utiliza una carpeta temporal y una reserva aislada.

Los contratos son extensibles, pero no universales: no se simulan cola, profundidad,
fills parciales, apalancamiento ni conversión de monedas de cotización distintas
de USD. Ver `TICKS.md` y `GHERKIN.md` para límites y semántica exacta.
