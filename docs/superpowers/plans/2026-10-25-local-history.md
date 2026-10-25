# Implementación de históricos locales

**Goal:** Dukascopy 2016–2025 persistente, Parquet y DuckDB, evaluación offline.
**Architecture:** Ingesta explícita, catálogo de particiones inmutables y lector sin red.
**Tech Stack:** Python, DuckDB, PyArrow, pandas, unittest.
**Spec:** ../specs/2026-10-25-local-history-design.md

## Contratos

- `HistoryStore(root).put_day(asset, day, frame, provenance, issues)` escribe Parquet y calidad.
- `HistoryStore(root).read(asset,start,end)` verifica y devuelve ticks + metadatos locales.
- `download_history(root,assets,start,end,fetch,checkpoint,progress)` es la única ruta de red nueva.
- `load_local_contract(contract,root)` adapta el catálogo al contrato Gherkin.
- `history_cli.py download|status|verify` permite iniciar/reanudar y auditar sin IA.

## Secuencia

- [ ] Escribir tests BI5, calidad, cobertura, tampering y segunda descarga sin fetch.
- [ ] Ejecutar `python -m unittest test_local_history -v`: rojo sin módulos.
- [ ] Implementar catálogo DuckDB y Parquet diarios, hashes y rechazo de faltantes.
- [ ] Implementar BI5 horario y caché raw reanudable; HTTP 429 aborta, 404 no equivale a cierre.
- [ ] Conectar evaluación Gherkin, snapshot local y controles del panel. Bloquear red legacy.
- [ ] Probar el panel con catálogo real de fixtures y sockets externos bloqueados.
- [ ] Ejecutar cobertura, regresión y revisión de código; corregir hallazgos.
- [ ] Iniciar descarga solicitada, verificar estado real e informar cualquier bloqueo del proveedor.

Ejemplos de aserciones obligatorias:

```python
assert store.read('EURUSD',start,end)[0].bid.iloc[0] == 1.1
with patch('socket.socket.connect',side_effect=AssertionError('offline')):
    store.read('EURUSD',start,end)
with self.assertRaisesRegex(ValueError,'hash'):
    store.read('EURUSD',start,end)  # después de alterar una partición
download_history(root,['EURUSD'],start,end,fetch=fail_if_called)  # ya descargado
```

No commits automáticos: hay cambios del usuario y de trabajo anterior en el árbol compartido.
