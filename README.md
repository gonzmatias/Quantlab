# Agente cuantitativo cíclico en LangGraph

Archivo principal completo: `trading_agent.py`. Requiere Python 3.11 o posterior
estable y las dependencias de `requirements.txt`.

## Ejecución

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe trading_agent.py --demo
.\.venv\Scripts\python.exe -m unittest -v
```

Para investigación con el LLM, configura `OPENAI_API_KEY` en tu entorno y usa un
modelo de tu cuenta que admita salida estructurada:

```powershell
.\.venv\Scripts\python.exe trading_agent.py --csv bars.csv --model NOMBRE_DEL_MODELO --bars-per-year 252
```

`bars.csv` debe contener al menos 600 filas cronológicas únicas con las columnas
`timestamp,open,high,low,close`. Un único activo cotizado en USD, sin posiciones
cortas ni apalancamiento. Usa una serie con tratamiento consistente de splits;
el motor no añade dividendos, intereses ni impuestos. La frecuencia de las barras
debe corresponder a `--bars-per-year` (252 sesiones diarias o 365 días cripto).
No se descargan datos ni se envían órdenes al bróker.

Personaliza costos reales con `--settings broker_settings.json`. Ejemplo de JSON:

```json
{
  "commission_rate": 0.001,
  "fixed_commission": 0.0,
  "spread_bps": 5.0,
  "slippage_bps": 5.0,
  "min_notional": 5.0,
  "quantity_step": 0.00001,
  "min_trades": 30,
  "max_iterations": 10
}
```

Estos valores son supuestos ilustrativos, no tarifas verificadas de un intermediario.
Spread es el diferencial completo en puntos básicos; se cobra la mitad por lado.
La comisión porcentual y fija se cobran tanto en compra como en venta. El estrés
multiplica todas las fricciones por 1, 2 y 3. Los mínimos se aplican al abrir;
se supone que el intermediario permite cerrar íntegramente una posición pequeña.

## Las cinco etapas

1. `researcher_node`: LLM con JSON validado por Pydantic; usa feedback de rechazos.
   Cinco familias implementadas con fundamentos que debe justificar el LLM.
   Repetir una familia se rechaza; el catálogo puede agotarse antes del límite.
2. `prototyper_node`: compila un script autónomo desde funciones deterministas.
   El LLM no ejecuta código arbitrario ni cambia el motor contable.
3. `quant_validator_node`: división temporal 70/30, Sharpe, DSR aproximado,
   drawdown, Profit Factor >= 1.2, mínimo de operaciones y degradación <= 20%.
   Tres ventanas progresivas con reglas congeladas; no reentrenamiento por ventana.
4. `stress_test_node`: $100 en el segmento OOS, pasos de cantidad, costos,
   retorno positivo, drawdown <= 25%, sin quiebra y operaciones suficientes
   en cada escenario. Treinta operaciones es un mínimo operativo, no garantía
   de significancia estadística.
5. `production_coder_node`: usa el LLM para documentar el módulo generado y
   exporta exactamente el motor probado mediante una plantilla. El artefacto
   es ejecutable para simulación histórica; no es una integración live.

El contador aumenta al comenzar cada hipótesis, no por paso del grafo. Las dos
rutas condicionales vuelven a investigación al fallar, o terminan al décimo
intento. Una aprobación en el décimo intento aún puede completar las etapas.
Errores operativos de infraestructura detienen la ejecución con estado guardado.

## Interpretación estadística

El **DSR estándar está entre 0 y 1**, por lo que `DSR > 1.2` es imposible.
Aquí se exigen **DSR >= 0.95 y estadístico z > 1.2**. No son métricas idénticas:
el primer umbral es más estricto. El benchmark de Sharpe se calcula con el
presupuesto completo de intentos desde el principio, usando dispersión bajo
hipótesis nula estimada mediante bootstrap circular por bloques. Es una
aproximación, no un DSR exacto de toda la búsqueda adaptativa de un LLM.

El OOS se reutiliza para dar feedback y por tanto se convierte en validación
adaptativa. `APPROVED` significa que pasó los filtros programados, **no** que
demostró alfa independiente ni aptitud para dinero real. Es necesario evaluar
en datos nuevos sin reutilizarlos para ajustar hipótesis. Repetir ejecuciones
tampoco reinicia legítimamente el presupuesto estadístico de búsqueda.

Se decide al cierre y se opera en la apertura siguiente. Los stops/takes son
condiciones al cierre, no órdenes intrabar; un gap puede exceder el stop nominal.
El drawdown se mide al cierre de las barras incluyendo el capital inicial y la
liquidación final. No estima extremos intrabar ni impacto de mercado dependiente
del volumen. El R/R indicado es nominal, no una promesa de fills.

## Resultados

Cada ejecución crea una carpeta nueva en `outputs/` con `manifest.json`
(configuración y hash de datos), `state.json` (estado, métricas, historial y logs),
prototipos y, exclusivamente si aprueba con CSV, `production_strategy.py`.
La demo offline usa un generador reproducible de precios sin alfa supuesto y
bloquea la exportación de producción incluso si algún filtro pasara por azar.

El módulo exportado se ejecuta así:

```powershell
.\.venv\Scripts\python.exe outputs/RUN/production_strategy.py --csv new_bars.csv --capital 100
```

## Referencias

- [LangGraph StateGraph y flujo](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [ChatOpenAI y salida estructurada](https://docs.langchain.com/oss/python/integrations/chat/openai)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Bailey y López de Prado: Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)
