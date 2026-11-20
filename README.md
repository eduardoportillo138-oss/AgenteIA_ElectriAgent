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
3. **Revisar la propuesta**: acciones concretas, métricas frente a la operación actual, justificación regla por regla (1–4) y la gráfica de 24 horas con tarifas, renovables y límite de suministro.
4. **Aprobar o rechazar** (Validación humana). El rechazo pide un motivo y el agente de meta replantea una alternativa menos invasiva; tras 3 rechazos escala el caso.
5. **Consultar la evidencia**: consumo por carga, anomalías detectadas, riesgos ISO/IEC 23894, impactos ISO/IEC 42005, monitoreo ISO/IEC 5338, escenarios comparados y la bitácora de mensajes. El reporte y la bitácora se descargan en JSON.

Es una **PWA**: en el celular se puede instalar desde el navegador ("Agregar a pantalla de inicio") y se adapta a pantalla angosta (grafo vertical y botones de decisión fijos abajo). Sin conexión abre la interfaz, pero los análisis siempre se piden al servidor. El modo sin conexión requiere HTTPS o `localhost`.

### API REST

| Método | Ruta | Uso |
|---|---|---|
| GET | `/api/config` | Periodos, condiciones, grafo y modelo de la red |
| POST | `/api/sesiones` | `{periodo, condicion, falla?, semilla?}`: analiza y deja la propuesta en espera |
| GET | `/api/sesiones/<id>` | Estado de la sesión |
| POST | `/api/sesiones/<id>/aprobacion` | `{aprobado, motivo}`: decisión del operador (el rechazo exige motivo) |
| GET | `/api/sesiones/<id>/reporte` | Reporte final (cuando ya se decidió) |
| GET | `/api/sesiones/<id>/bitacora` | Bitácora de trazabilidad |

Estados de una sesión: `en_espera`, `aprobada`, `escalada`. La API está pensada para que una app nativa (Flutter, React Native) la consuma sin cambios.

## Consola

```bash
pip install numpy matplotlib
python main.py                         # 1 mes, aprobación interactiva por consola
python main.py --periodo 1a --modo auto
python main.py --condicion ola_calor --modo auto
python main.py --condicion falla --modo auto --mostrar
python main.py --modo auto --rechazos 2          # ciclo de rechazo y replanteo
python main.py --modo auto --falla aprendizaje   # manejo de fallas
python pruebas.py                                # pruebas de coordinación
```

| Opción | Valores | Descripción |
|---|---|---|
| `--periodo` | `1d`, `1s`, `1m`, `6m`, `1a` | Periodo de datos IoT que se analiza |
| `--condicion` | `normal`, `ola_calor`, `falla` | Condición prevista de la red para el día que se recomienda |
| `--modo` | `interactivo`, `auto` | Aprobación humana por consola o automática (para demostraciones) |
| `--rechazos` | número | En modo `auto`, cuántas veces se rechaza antes de aprobar |
| `--falla` | `ninguna`, `aprendizaje`, `deliberativo`, `iso` | Simula una falla transitoria en un agente |
| `--mostrar` | — | Abre la gráfica en una ventana emergente |

En la carpeta `salida/` se generan cuatro archivos:
- `reporte_electriagent.json`: la recomendación, sus acciones, métricas, evaluación ISO y aprobación.
- `bitacora_trazabilidad.json`: el registro de cada mensaje y cada escritura en la pizarra (ISO/IEC 5338).
- `grafica_recomendacion.png`: la demanda, las renovables y el límite de suministro, con la recomendación encima.
- `memoria_aprendizaje.json`: la experiencia acumulada por el agente de aprendizaje.

## Estructura

| Archivo | Contenido |
|---|---|
| `modelo_red.py` | Red de distribución (media y baja tensión, cargas, renovables, tarifas) y simulador IoT |
| `coordinacion.py` | Grafo A–H, mensajes, bus, pizarra, bitácora y planificador |
| `agentes.py` | Los 8 agentes y las reglas de decisión. G admite aprobación por consola, automática o web (asíncrona) |
| `motor.py` | Arma el sistema completo; lo comparten la consola y el servidor |
| `main.py` | Punto de entrada por consola, resumen y gráfica |
| `servidor.py` | API REST y servidor de la app (solo biblioteca estándar) |
| `web/` | App web / móvil (PWA): `index.html`, `estilos.css`, `app.js`, manifiesto, service worker e íconos del logotipo |
| `pruebas.py` | Pruebas de jerarquía, permisos, flujos, contingencia, fallas y API |

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
- **Carga desplazable:** cada carga tiene una fracción que puede cambiar de horario (de 0 % a 50 %) y una prioridad de suspensión.
- **Tarifa horaria:** base de 0 a 6 h, punta de 18 a 22 h e intermedia el resto del día.
- **Demanda dinámica:** perfiles diarios por tipo de usuario, efecto de fin de semana, estacionalidad (más consumo en meses cálidos) y ruido. Además se inyectan anomalías y lecturas perdidas, para probar la detección.

Los valores son ilustrativos y se reemplazan con los datos reales del caso de estudio.

## Agentes (nodos del grafo)

| Nodo | Agente | Responsabilidad |
|---|---|---|
| A | Usuario | Elige el periodo y arranca la conversación |
| B | Captura IoT | Lee los medidores e inversores e interpola las lecturas perdidas. Solo captura datos agregados por nodo, sin datos personales |
| C | Agente de meta | Supervisor: mantiene los objetivos, enruta, aplica las reglas 1–4, reintenta tareas y escala casos |
| D | Agente de aprendizaje | Detecta anomalías, identifica horas pico y variación estacional, pronostica el día siguiente, mide su error (MAPE) y guarda experiencia entre ejecuciones |
| E | Agente deliberativo | Simula escenarios: S0 operación actual, S1 prioridad renovable, S2 desplazamiento por tarifa, S3 combinado, variantes conservadoras y contingencia |
| F | Evaluación ISO | Riesgo con probabilidad × impacto (ISO/IEC 23894), impacto por grupo de interés (ISO/IEC 42005) y monitoreo de deriva (ISO/IEC 5338) |
| G | Aprobación humana | Acepta o rechaza la propuesta y registra el motivo |
| H | Recomendación final | Entrega el reporte, la bitácora y la gráfica |

## Reglas de decisión del agente de meta

1. **Prioridad de suministro y contingencias.** Primero se descartan los escenarios con déficit. Si ninguno queda sin déficit, se propone una contingencia: suspensiones controladas de cargas no críticas según su prioridad, siempre protegiendo las cargas críticas.
2. **Renovables.** Entre los escenarios viables, se prefiere el de mayor cobertura renovable.
3. **Costo.** Si el suministro está garantizado, se elige el de menor costo operativo.
4. **Validación humana.** Toda recomendación se justifica regla por regla y se envía a aprobación.

Si el operador rechaza la propuesta, el agente de meta excluye ese escenario y vuelve a pedir la deliberación. En la nueva ronda, el agente deliberativo agrega variantes conservadoras (50 %, 25 %), y el agente de meta busca una alternativa **menos invasiva**, es decir, con menos energía desplazada que la rechazada. Después de 3 rechazos, el caso se escala al operador.

## Mecanismos de coordinación

1. **Jerarquía validada por el grafo.** El bus solo acepta mensajes por las aristas del grafo A–H. Un mensaje directo entre trabajadores, como D → E, lanza `ViolacionJerarquia`.
2. **Protocolo de mensajes** inspirado en FIPA-ACL, con las performativas `REQUEST`, `INFORM`, `PROPOSE`, `ACCEPT_PROPOSAL`, `REJECT_PROPOSAL` y `FAILURE`. Cada mensaje lleva un id y el identificador de la conversación.
3. **Pizarra compartida (blackboard).** Los mensajes transportan solo la clave de la sección; los datos viven en la pizarra. Cada sección la escribe únicamente su agente dueño; si otro lo intenta, se lanza `PermisoDenegado`.
4. **Planificador con buzones.** La entrega es FIFO y hay un límite de 200 mensajes, para evitar ciclos infinitos.
5. **Canal de excepciones.** Si un agente falla, el planificador envía un `FAILURE` al supervisor. El supervisor reintenta la tarea una vez y, si vuelve a fallar, escala el caso al operador.
6. **Bitácora trazable.** Registra cada mensaje con su peso en el grafo y cada escritura en la pizarra, y genera una huella hash de los datos usados.
7. **Costo de coordinación.** Es la suma de los pesos de las aristas recorridas: 36 en el flujo aprobado y 24 más por cada rechazo.

## Limitaciones del prototipo

- Los datos IoT son simulados. Para usar datos reales, hay que sustituir `SimuladorIoT.capturar` por la lectura de los medidores (MQTT, Modbus o una API).
- El pronóstico es estadístico: promedio ponderado con tendencia. Puede reemplazarse por un modelo de aprendizaje automático sin cambiar la coordinación.
- El balance de energía es horario y agregado en la subestación; no se modelan flujos de potencia, pérdidas ni tensiones por alimentador.
- Las suspensiones son por carga completa (circuito), así que pueden recortar más energía de la estrictamente necesaria.
- El servidor guarda las sesiones en memoria (máximo 100) y no tiene inicio de sesión: es para uso local o en una red de confianza. Para publicarlo hace falta autenticación del operador y HTTPS.
