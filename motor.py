"""
Armado del sistema multi-agente ElectriAgent.

Un solo lugar donde se crean el bus, la pizarra, la bitácora y los 8 agentes
(nodos A-H). Lo usan la consola (main.py) y el servidor web (servidor.py).
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from agentes import (CONDICIONES, AgenteAprendizaje, AgenteDeliberativo, AgenteMeta,
                     AprobacionHumana, CapturaIoT, EvaluadorISO, RecomendacionFinal, Usuario)
from coordinacion import BusCoordinacion, Bitacora, Pizarra
from modelo_red import PERIODOS, RedDistribucion, SimuladorIoT, red_ejemplo

FALLAS = ("ninguna", "aprendizaje", "deliberativo", "iso")


@dataclass
class Sistema:
    periodo: str
    condicion: str
    red: RedDistribucion
    bitacora: Bitacora
    pizarra: Pizarra
    bus: BusCoordinacion
    usuario: Usuario
    aprobacion: AprobacionHumana
    salida: RecomendacionFinal
    dir_salida: str
    fin: datetime
    conversacion: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def iniciar(self):
        """Arranca la conversación A -> B y procesa hasta terminar o hasta que
        la propuesta quede en espera de aprobación humana (modo web)."""
        self.usuario.iniciar(self.periodo, self.conversacion)
        self.bus.ejecutar()

    def responder(self, aprobado: bool, motivo: str = ""):
        """Decisión del operador (modo web): reanuda el flujo."""
        self.aprobacion.responder(aprobado, motivo)
        self.bus.ejecutar()

    @property
    def terminado(self) -> bool:
        return self.salida.reporte is not None

    @property
    def en_espera(self) -> bool:
        return self.aprobacion.pendiente is not None

    def guardar_bitacora(self):
        self.bitacora.guardar(os.path.join(self.dir_salida, "bitacora_trazabilidad.json"))


def crear_sistema(periodo: str = "1m", condicion: str = "normal", modo: str = "interactivo",
                  rechazos: int = 0, falla: str = "ninguna", semilla: int = 7,
                  salida: str = "salida", ruta_memoria: str | None = None,
                  verbose: bool = True, fin: datetime | None = None) -> Sistema:
    if periodo not in PERIODOS:
        raise ValueError(f"Periodo no válido: {periodo}")
    if condicion not in CONDICIONES:
        raise ValueError(f"Condición no válida: {condicion}")
    if falla not in FALLAS:
        raise ValueError(f"Falla no válida: {falla}")

    os.makedirs(salida, exist_ok=True)
    red = red_ejemplo()
    fin = fin or datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    bitacora = Bitacora()
    pizarra = Pizarra(bitacora)
    bus = BusCoordinacion(bitacora, verbose=verbose)

    usuario = Usuario("A", bus, pizarra)
    CapturaIoT("B", bus, pizarra, simulador=SimuladorIoT(red, semilla), fin=fin)
    AgenteMeta("C", bus, pizarra, condicion=condicion)
    AgenteAprendizaje("D", bus, pizarra,
                      ruta_memoria=ruta_memoria or os.path.join(salida, "memoria_aprendizaje.json"),
                      fallar=falla == "aprendizaje")
    AgenteDeliberativo("E", bus, pizarra, red=red, fallar=falla == "deliberativo")
    EvaluadorISO("F", bus, pizarra, fallar=falla == "iso")
    aprobacion = AprobacionHumana("G", bus, pizarra, modo=modo, rechazos_auto=rechazos)
    final = RecomendacionFinal("H", bus, pizarra, red=red, periodo=periodo, salida=salida)

    return Sistema(periodo, condicion, red, bitacora, pizarra, bus, usuario, aprobacion,
                   final, salida, fin)


def a_json(obj):
    """Convierte tipos de numpy y fechas para json.dumps(default=...)."""
    if isinstance(obj, np.ndarray):
        return obj.round(2).tolist()
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return round(float(obj), 3)
    if isinstance(obj, datetime):
        return obj.isoformat(timespec="minutes")
    return str(obj)
