# ElectriAgent: prototipo del sistema multi-agente

Prototipo funcional del sistema multi-agente **ElectriAgent** para optimizar y gestionar una red de distribución eléctrica. Usa una arquitectura **vertical (jerárquica)** basada en el grafo etiquetado A–H del documento del proyecto.

El sistema es **informativo**: analiza, simula y recomienda. No controla dispositivos ni la red, y toda recomendación necesita la aprobación de una persona (Regla 4).

## Aplicación web y móvil

```bash
pip install -r requirements.txt
python servidor.py                     # abre http://localhost:8000
python servidor.py --host 0.0.0.0      # para abrirla desde el celular en la misma red Wi-Fi
```

La app (carpeta `web/`) lleva el flujo del documento a una interfaz para el usuario final:

1. **Elegir el periodo** (1 día, 1 semana, 1 mes, 6 meses, 1 año) y la condición prevista de la red.
2. **Ver el recorrido de los agentes** sobre el grafo etiquetado A–H: cada arista se ilumina cuando pasa un mensaje, con su peso y el costo de coordinación.
3. **Revisar la propuesta**: acciones concretas, métricas frente a la operación actual, justificación regla por regla (1–4), si la estrategia es **robusta** ante la banda P90, el **recorte parcial por carga** (kWh, kW máximos y horas), el uso de cada alimentador y la gráfica de 24 horas con tarifas, renovables, límite de suministro y la **banda P10–P90**. Un selector cambia la gráfica entre la subestación, el alimentador A1 y el A2.
4. **Aprobar o rechazar** (Validación humana). El rechazo pide un motivo y el agente de meta replantea una alternativa menos invasiva; tras 3 rechazos escala el caso.
5. **Consultar la evidencia**: consumo por carga, anomalías detectadas, riesgos ISO/IEC 23894, impactos ISO/IEC 42005, monitoreo ISO/IEC 5338, escenarios comparados y la bitácora de mensajes. El reporte y la bitácora se descargan en JSON.

En `docs/capturas/` hay capturas de la app con una falla del alimentador A1 (propuesta y gráfica del A1, evaluación ISO y escenarios comparados).

Es una **PWA**: en el celular se puede instalar desde el navegador ("Agregar a pantalla de inicio") y se adapta a pantalla angosta (grafo vertical y botones de decisión fijos abajo). Sin conexión abre la interfaz, pero los análisis siempre se piden al servidor. El modo sin conexión requiere HTTPS o `localhost`.

### API REST

| Método | Ruta | Uso |
|---|---|---|
| GET | `/api/config` | Periodos, catálogo de condiciones (con descripción y parámetros), grafo y modelo de la red con alimentadores |
| POST | `/api/sesiones` | `{periodo, condicion, alimentador?, crecimiento_pct?, falla?, semilla?}`: analiza y deja la propuesta en espera |
| GET | `/api/sesiones/<id>` | Estado de la sesión |
| POST | `/api/sesiones/<id>/aprobacion` | `{aprobado, motivo}`: decisión del operador (el rechazo exige motivo) |
| GET | `/api/sesiones/<id>/reporte` | Reporte final (cuando ya se decidió) |
| GET | `/api/sesiones/<id>/bitacora` | Bitácora de trazabilidad |

Estados de una sesión: `en_espera`, `aprobada`, `escalada`. La API está pensada para que una app nativa (Flutter, React Native) la consuma sin cambios.

Campos nuevos (los anteriores se conservan igual):
- `propuesta.robusto`, `propuesta.metricas_p90`, `propuesta.base_planificacion` (`P50` o `P90`), `propuesta.recorte_por_carga` y `propuesta.alimentadores` (totales y uso por alimentador).
- `propuesta.serie.bandas` (P10/P90 de demanda y renovables) y `propuesta.serie.por_alimentador` con las series de `SE`, `A1` y `A2`.
- `alternativas[].recorte_kwh`, `robusto`, `deficit_p90_kwh` y `utilizacion_max_pct`.
- `condicion.descripcion` y `condicion.parametros`; `analisis.cobertura_bandas`; en `trazabilidad`, la cobertura de las bandas.

## Consola

```bash
pip install numpy matplotlib
python main.py                         # 1 mes, aprobación interactiva por consola
python main.py --periodo 1a --modo auto
python main.py --condicion ola_calor --modo auto
python main.py --condicion falla_transformador --modo auto --mostrar
python main.py --condicion falla_alimentador --alimentador A2 --modo auto
python main.py --condicion crecimiento_demanda --crecimiento 25 --modo auto
python main.py --modo auto --rechazos 2          # ciclo de rechazo y replanteo
python main.py --modo auto --falla aprendizaje   # manejo de fallas
python pruebas.py                                # pruebas de coordinación
```

| Opción | Valores | Descripción |
|---|---|---|
| `--periodo` | `1d`, `1s`, `1m`, `6m`, `1a` | Periodo de datos IoT que se analiza |
| `--condicion` | ver [catálogo](#catálogo-de-escenarios-de-demanda) | Condición prevista de la red para el día que se recomienda (`falla` se acepta como alias de `falla_transformador`) |
| `--alimentador` | `A1`, `A2` | Alimentador en falla con `falla_alimentador` (por omisión `A1`) |
| `--crecimiento` | porcentaje | Crecimiento con `crecimiento_demanda` (por omisión 15) |
| `--modo` | `interactivo`, `auto` | Aprobación humana por consola o automática (para demostraciones) |
| `--rechazos` | número | En modo `auto`, cuántas veces se rechaza antes de aprobar |
| `--falla` | `ninguna`, `aprendizaje`, `deliberativo`, `iso` | Simula una falla transitoria en un agente |
| `--mostrar` | — | Abre la gráfica en una ventana emergente |

En la carpeta `salida/` se generan cuatro archivos:
- `reporte_electriagent.json`: la recomendación, sus acciones, métricas, evaluación ISO y aprobación.
- `bitacora_trazabilidad.json`: el registro de cada mensaje y cada escritura en la pizarra (ISO/IEC 5338).
- `grafica_recomendacion.png`: la demanda con su banda P10–P90, las renovables y el límite de suministro, con la recomendación encima.
- `memoria_aprendizaje.json`: la experiencia acumulada por el agente de aprendizaje.

## Estructura

| Archivo | Contenido |
|---|---|
| `modelo_red.py` | Red de distribución (alimentadores, transformadores de BT, pérdidas, cargas, renovables, tarifas) y simulador IoT |
| `distribucion.py` | Asignación horaria de energía por alimentador y recorte parcial (la usa el agente E) |
| `condiciones.py` | Catálogo de escenarios de demanda y su aplicación a pronósticos y capacidades |
| `coordinacion.py` | Grafo A–H, mensajes, bus, pizarra, bitácora y planificador |
| `agentes.py` | Los 8 agentes y las reglas de decisión. G admite aprobación por consola, automática o web (asíncrona) |
| `motor.py` | Arma el sistema completo; lo comparten la consola y el servidor |
| `main.py` | Punto de entrada por consola, resumen y gráfica |
| `servidor.py` | API REST y servidor de la app (solo biblioteca estándar) |
| `web/` | App web / móvil (PWA): `index.html`, `estilos.css`, `app.js`, manifiesto, service worker e íconos del logotipo |
| `pruebas.py` | Pruebas de jerarquía, permisos, flujos, contingencia, fallas, API y los criterios de distribución y escenarios |
| `docs/capturas/` | Capturas de la app con una falla del alimentador A1 |

## Modelo de la red de distribución

- **Subestación SE-01**, de 13.2 kV en media tensión a 127/220 V en baja tensión. Su capacidad convencional es de 4,750 kW (5 MVA, factor de potencia 0.95).
- **Alimentador 1 (media tensión):**
  - Hospital General: carga crítica, nunca se desplaza ni se suspende.
  - Parque industrial.
  - Bombeo de agua municipal.
  - Mini-eólica de 600 kW.
- **Alimentador 2 (baja tensión):**
  - Zona comercial centro.
  - Fraccionamientos norte y sur.
  - Solar distribuida de 1,200 kWp.
- **Carga desplazable:** cada carga tiene una fracción que puede cambiar de horario (de 0 % a 50 %) y una prioridad de recorte.
- **Tarifa horaria:** base de 0 a 6 h, punta de 18 a 22 h e intermedia el resto del día.
- **Demanda dinámica:** perfiles diarios por tipo de usuario, efecto de fin de semana, estacionalidad (más consumo en meses cálidos) y ruido. Además se inyectan anomalías y lecturas perdidas, para probar la detección.

### Modelo por alimentador

| Elemento | Valor ilustrativo | Uso |
|---|---|---|
| Transformador de SE-01 | 4,750 kW | Límite de la energía convencional (medida en la barra, con pérdidas) |
| Alimentador 1 (MT) | 2,800 kW en cabecera | Hospital, parque industrial, bombeo y mini-eólica |
| Alimentador 2 (MT hacia BT) | 3,600 kW en cabecera | Comercial y fraccionamientos, solar distribuida |
| Transformadores MT/BT del A2 | 3,400 kW agregados | Límite de lo que llega a las cargas de baja tensión |
| Pérdidas técnicas | 2.5 % en MT, 4.0 % adicional en BT | Un kW entregado en BT cuesta (1.025 × 1.04) kW en la barra |
| Límite de inyección | 800 kW | Excedente renovable de un alimentador que puede subir a la subestación |

Todos estos valores son **ilustrativos** (orden de magnitud razonable para 13.2 kV) y se configuran en `red_ejemplo()`. Se reemplazan con los datos reales del caso de estudio.

## Asignación de energía (`distribucion.py`)

El agente deliberativo (E) asigna la energía **hora por hora y alimentador por alimentador**:

1. **Renovables primero (Regla 2).** Cada alimentador usa su generación local; el excedente sube a la subestación (hasta el límite de inyección, con pérdidas) y se reparte entre los alimentadores que lo necesitan.
2. **Convencional después.** El resto se cubre con el transformador de la subestación, respetando su capacidad, la de cada cabecera y la de los transformadores de BT.
3. **Recorte parcial (Regla 1).** Si falta capacidad, solo se recortan los kW necesarios, en orden de `prioridad_corte` (a igual prioridad, en proporción a la demanda). Una saturación de alimentador se resuelve con cargas de ese mismo alimentador; una del transformador de la subestación, con cualquier alimentador. Las cargas críticas nunca se recortan.
4. **Reporte.** Por hora y alimentador: demanda, renovable usada y desperdiciada, convencional, pérdidas, déficit, recorte por carga, margen (cabecera y transformador) y costo. Siempre se cumple `demanda = renovable usada + convencional + recorte + no servido`.

Es determinista y está vectorizado con numpy (las 24 horas a la vez). Es un balance de energía, no un flujo de potencia.

## Catálogo de escenarios de demanda

| Condición | Parámetros (ilustrativos) |
|---|---|
| `normal` | Pronóstico sin ajustes |
| `ola_calor` | Demanda residencial ×1.8, comercial ×1.6, crítica ×1.1 |
| `frente_frio` | Residencial ×1.3, comercial ×0.95, crítica ×1.05; solar ×0.6, eólica ×1.35 |
| `baja_renovable` | Nublado y sin viento: solar ×0.25, eólica ×0.20 |
| `pico_vespertino` | De 17:00 a 23:00: residencial ×1.45, comercial ×1.25 |
| `crecimiento_demanda` | Toda la demanda +15 % (configurable con `--crecimiento` o `crecimiento_pct`) |
| `falla_transformador` | Transformador de SE-01 al 55 % |
| `falla_alimentador` | Alimentador A1 o A2 (`--alimentador`) al 45 % por enlace de respaldo |
| `contingencia_natural` | Transformador al 70 %, pico vespertino, solar ×0.5 y eólica ×1.1 |

Cada condición tiene una descripción que la app muestra al elegirla.

## Bandas de pronóstico y robustez

- **Bandas P10/P50/P90.** El agente de aprendizaje (D) pronostica cada día de los últimos 60 con los días previos y guarda los errores relativos. Sus cuantiles 10, 50 y 90 dan la banda de cada carga; para las renovables se usa el error de la energía diaria (nubes y viento afectan el día completo). La banda se centra en el pronóstico puntual: P50 es el valor central de siempre y P10 ≤ P50 ≤ P90 por construcción.
- **Cobertura (ISO/IEC 5338).** Las bandas se vuelven a construir sin el último día y se cuenta qué porcentaje de sus 144 lecturas reales cae dentro de P10–P90 (nominal: 80 %). Con menos de 3 días no hay validación. Por debajo de 60 %, F pide recalibrar.
- **Robustez.** E evalúa cada estrategia en P50 y en un escenario desfavorable: **demanda P90 y renovables P10**. Una estrategia es **robusta** si en ese escenario no deja déficit.
- **Contingencias.** Si ninguna estrategia evita el déficit, E propone dos recortes: uno planificado en P50 (exactamente el déficit esperado) y uno **robusto** planificado en P90. C prefiere el robusto (Regla 1); si el operador lo rechaza, propone el de P50, que es menos invasivo.

## Agentes (nodos del grafo)

| Nodo | Agente | Responsabilidad |
|---|---|---|
| A | Usuario | Elige el periodo y arranca la conversación |
| B | Captura IoT | Lee los medidores e inversores e interpola las lecturas perdidas. Solo captura datos agregados por nodo, sin datos personales |
| C | Agente de meta | Supervisor: mantiene los objetivos, enruta, aplica las reglas 1–4, reintenta tareas y escala casos |
| D | Agente de aprendizaje | Detecta anomalías, identifica horas pico y variación estacional, pronostica el día siguiente con bandas P10/P50/P90 por carga y fuente, mide su error (MAPE) y la cobertura de las bandas, y guarda experiencia entre ejecuciones |
| E | Agente deliberativo | Aplica la condición, simula S0 operación actual, S1 prioridad renovable, S2 desplazamiento por tarifa, S3 combinado, variantes conservadoras y contingencias (P50 y P90), y asigna la energía por alimentador en P50 y P90 |
| F | Evaluación ISO | Riesgos con probabilidad × impacto (ISO/IEC 23894), incluida la saturación de alimentador y la insuficiencia estimada con la banda P90; impacto por grupo con los kWh recortados reales (ISO/IEC 42005); monitoreo de deriva y cobertura de bandas (ISO/IEC 5338) |
| G | Aprobación humana | Acepta o rechaza la propuesta y registra el motivo |
| H | Recomendación final | Entrega el reporte, la bitácora y la gráfica |

## Reglas de decisión del agente de meta

1. **Prioridad de suministro y contingencias.** Primero se descartan los escenarios con déficit en P50. Si ninguno queda sin déficit, se propone una contingencia: recorte parcial de cargas no críticas según su prioridad, siempre protegiendo las cargas críticas. Entre los viables se prefieren los **robustos** (sin déficit en P90); si ninguno lo es, la justificación lo dice y F lo trata como riesgo.
2. **Renovables.** Entre los escenarios viables, se prefiere el de mayor cobertura renovable.
3. **Costo.** Si el suministro está garantizado, se elige el de menor costo operativo.
4. **Validación humana.** Toda recomendación se justifica regla por regla y se envía a aprobación.

Si el operador rechaza la propuesta, el agente de meta excluye ese escenario y vuelve a pedir la deliberación. En la nueva ronda, el agente deliberativo agrega variantes conservadoras (50 %, 25 %), y el agente de meta busca una alternativa **menos invasiva**: con menos energía desplazada que la rechazada o, en contingencia, con menos energía recortada. Después de 3 rechazos, el caso se escala al operador.

La justificación incluye además la robustez ante P90 y el margen, el uso y el recorte de cada alimentador.

## Mecanismos de coordinación

1. **Jerarquía validada por el grafo.** El bus solo acepta mensajes por las aristas del grafo A–H. Un mensaje directo entre trabajadores, como D → E, lanza `ViolacionJerarquia`.
2. **Protocolo de mensajes** inspirado en FIPA-ACL, con las performativas `REQUEST`, `INFORM`, `PROPOSE`, `ACCEPT_PROPOSAL`, `REJECT_PROPOSAL` y `FAILURE`. Cada mensaje lleva un id y el identificador de la conversación.
3. **Pizarra compartida (blackboard).** Los mensajes transportan solo la clave de la sección; los datos viven en la pizarra. Cada sección la escribe únicamente su agente dueño; si otro lo intenta, se lanza `PermisoDenegado`.
4. **Planificador con buzones.** La entrega es FIFO y hay un límite de 200 mensajes, para evitar ciclos infinitos.
5. **Canal de excepciones.** Si un agente falla, el planificador envía un `FAILURE` al supervisor. El supervisor reintenta la tarea una vez y, si vuelve a fallar, escala el caso al operador.
6. **Bitácora trazable.** Registra cada mensaje con su peso en el grafo y cada escritura en la pizarra, y genera una huella hash de los datos usados.
7. **Costo de coordinación.** Es la suma de los pesos de las aristas recorridas: 36 en el flujo aprobado y 24 más por cada rechazo.

La distribución por alimentador y los escenarios no agregan aristas ni comunicación directa entre trabajadores: D publica las bandas en la pizarra y E las lee desde ahí, como antes el pronóstico central.

## Limitaciones del prototipo

- Los datos IoT son simulados. Para usar datos reales, hay que sustituir `SimuladorIoT.capturar` por la lectura de los medidores (MQTT, Modbus o una API).
- El pronóstico es estadístico: promedio ponderado con tendencia. Puede reemplazarse por un modelo de aprendizaje automático sin cambiar la coordinación.
- El balance es de energía por hora y alimentador, no un flujo de potencia: no se calculan tensiones, potencia reactiva ni pérdidas por tramo. Las pérdidas son un porcentaje fijo por nivel y la renovable consumida en su alimentador se considera sin pérdidas.
- Las capacidades de alimentadores y transformadores, las pérdidas, el límite de inyección y los factores del catálogo son ilustrativos.
- El recorte parcial supone que cada carga puede reducirse en cualquier fracción. En la práctica se hace por circuitos o bloques de carga, así que el recorte real puede ser algo mayor.
- La banda P90 aplica el cuantil 90 en todas las horas a la vez (y P10 a las renovables). Es más pesimista que un P90 conjunto, así que la contingencia robusta puede recortar bastante más que el déficit esperado en P50. La cobertura medida (≈ 90 % frente a 80 % nominal) indica bandas algo anchas.
- La cobertura de las bandas se valida con un solo día fuera de muestra y solo para las cargas.
- El servidor guarda las sesiones en memoria (máximo 100) y no tiene inicio de sesión: es para uso local o en una red de confianza. Para publicarlo hace falta autenticación del operador y HTTPS.
