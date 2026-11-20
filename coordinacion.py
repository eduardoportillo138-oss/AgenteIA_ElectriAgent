"""
Mecanismos de coordinación del sistema multi-agente ElectriAgent.

1. Jerarquía validada por el grafo: el bus solo permite los enlaces del grafo
   etiquetado A-H. Los trabajadores no pueden hablar entre sí fuera de él.
2. Protocolo de mensajes inspirado en FIPA-ACL (performativas).
3. Pizarra compartida (blackboard) con secciones que solo escribe su dueño.
4. Planificador de eventos con buzones (cola FIFO) y límite de mensajes.
5. Canal de excepciones: cualquier agente puede reportar FAILURE al supervisor.
6. Bitácora trazable (ISO/IEC 5338): cada mensaje queda registrado con su costo.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

# ----------------------------------------------- Grafo etiquetado y ponderado
NODOS = {
    "A": "Usuario",
    "B": "Captura de datos IoT",
    "C": "Agente de meta (supervisor)",
    "D": "Agente de aprendizaje",
    "E": "Agente deliberativo",
    "F": "Evaluación normativa ISO",
    "G": "Aprobación humana",
    "H": "Recomendación final",
}
ARISTAS = {   # (origen, destino): peso (costo relativo 1-10)
    ("A", "B"): 1, ("B", "C"): 4, ("C", "D"): 8, ("D", "C"): 2,
    ("C", "E"): 6, ("E", "F"): 5, ("F", "C"): 2, ("C", "G"): 7,
    ("G", "H"): 1, ("G", "C"): 4,
}
SUPERVISOR = "C"
MAX_MENSAJES = 200


class Performativa(str, Enum):
    REQUEST = "REQUEST"          # solicitar una tarea
    INFORM = "INFORM"            # entregar un resultado
    PROPOSE = "PROPOSE"          # proponer (escenarios / recomendación)
    ACCEPT = "ACCEPT_PROPOSAL"   # aprobación
    REJECT = "REJECT_PROPOSAL"   # rechazo con motivo
    FAILURE = "FAILURE"          # error en la tarea


@dataclass
class Mensaje:
    emisor: str
    receptor: str
    performativa: Performativa
    contenido: dict[str, Any]
    conversacion: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    fecha: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class ViolacionJerarquia(Exception):
    """Mensaje por un enlace que no existe en el grafo (p. ej. D -> E)."""


class PermisoDenegado(Exception):
    """Un agente intentó escribir en una sección de la pizarra que no le pertenece."""


# ----------------------------------------------------------------- Bitácora
class Bitacora:
    def __init__(self):
        self.eventos: list[dict] = []

    def registrar(self, tipo: str, **datos):
        self.eventos.append({"fecha": datetime.now().isoformat(timespec="seconds"),
                             "tipo": tipo, **datos})

    def guardar(self, ruta: str):
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(self.eventos, f, ensure_ascii=False, indent=2, default=str)


# ------------------------------------------------------------------ Pizarra
class Pizarra:
    """Estado compartido. Los mensajes llevan la clave de la sección, no los datos."""
    DUENOS = {"datos": "B", "analisis": "D", "escenarios": "E",
              "evaluacion": "F", "decision": "C", "aprobacion": "G", "reporte": "H"}

    def __init__(self, bitacora: Bitacora):
        self._datos: dict[str, Any] = {}
        self.bitacora = bitacora

    def escribir(self, nodo: str, clave: str, valor: Any):
        if self.DUENOS.get(clave) != nodo:
            raise PermisoDenegado(f"{nodo} no puede escribir '{clave}'")
        self._datos[clave] = valor
        self.bitacora.registrar("pizarra", nodo=nodo, clave=clave)

    def leer(self, clave: str) -> Any:
        return self._datos.get(clave)


def huella(obj: Any) -> str:
    """Hash corto para trazabilidad de los datos usados en cada decisión."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:12]


# ----------------------------------------------------- Bus + planificador
class BusCoordinacion:
<<<<<<< HEAD
    def __init__(self, bitacora: Bitacora, verbose: bool = True):
=======
    def __init__(self, bitacora: Bitacora):
>>>>>>> 9be51136663ac36fd03ce90c555d1e9a012a9e1e
        self.agentes: dict[str, Any] = {}
        self.buzones: dict[str, deque[Mensaje]] = {}
        self.bitacora = bitacora
        self.costo_total = 0
        self.recorrido: list[str] = []
        self.enviados = 0
<<<<<<< HEAD
        self.verbose = verbose            # False: no imprime en consola (modo servidor)
        self.consola: list[dict] = []     # registro legible de lo que dice cada agente
=======
>>>>>>> 9be51136663ac36fd03ce90c555d1e9a012a9e1e

    def registrar(self, agente):
        self.agentes[agente.nodo] = agente
        self.buzones[agente.nodo] = deque()

    def enviar(self, msg: Mensaje):
        enlace = (msg.emisor, msg.receptor)
        excepcion = msg.performativa == Performativa.FAILURE and msg.receptor == SUPERVISOR
        if enlace not in ARISTAS and not excepcion:
            self.bitacora.registrar("violacion", emisor=msg.emisor, receptor=msg.receptor)
            raise ViolacionJerarquia(f"Enlace no permitido {msg.emisor} -> {msg.receptor}")
        peso = ARISTAS.get(enlace, 0)
        self.costo_total += peso
        self.recorrido.append(f"{msg.emisor}->{msg.receptor}")
        self.enviados += 1
        self.bitacora.registrar("mensaje", id=msg.id, conversacion=msg.conversacion,
                                emisor=msg.emisor, receptor=msg.receptor,
                                performativa=msg.performativa.value, peso=peso,
                                contenido={k: v for k, v in msg.contenido.items()
                                           if isinstance(v, (str, int, float, bool, list))})
        self.buzones[msg.receptor].append(msg)

    def ejecutar(self):
        """Entrega los mensajes en orden FIFO hasta que no quede trabajo."""
        procesados = 0
        while any(self.buzones.values()):
            for nodo, buzon in self.buzones.items():
                if not buzon:
                    continue
                msg = buzon.popleft()
                procesados += 1
                if procesados > MAX_MENSAJES:
                    self.bitacora.registrar("corte", motivo="límite de mensajes")
                    raise RuntimeError("Se alcanzó el límite de mensajes (posible ciclo)")
                agente = self.agentes[nodo]
                try:
                    agente.procesar(msg)
                except (ViolacionJerarquia, PermisoDenegado):
                    raise
                except Exception as e:   # falla del agente -> canal de excepciones
                    self.bitacora.registrar("falla", nodo=nodo, error=str(e))
                    if nodo != SUPERVISOR:
                        self.enviar(Mensaje(nodo, SUPERVISOR, Performativa.FAILURE,
                                            {"error": str(e), "tarea": msg.performativa.value,
                                             "solicitud": msg.contenido},
                                            msg.conversacion))
                    else:
                        raise
                break   # vuelve a revisar desde el primer buzón (prioridad jerárquica)


class Agente:
    """Clase base: cada agente ocupa un nodo del grafo."""
    def __init__(self, nodo: str, bus: BusCoordinacion, pizarra: Pizarra):
        self.nodo = nodo
        self.nombre = NODOS[nodo]
        self.bus = bus
        self.pizarra = pizarra
        bus.registrar(self)

    def enviar(self, receptor: str, performativa: Performativa, contenido: dict, conversacion: str):
        self.bus.enviar(Mensaje(self.nodo, receptor, performativa, contenido, conversacion))

    def log(self, texto: str):
<<<<<<< HEAD
        self.bus.consola.append({"fecha": datetime.now().isoformat(timespec="seconds"),
                                 "nodo": self.nodo, "agente": self.nombre, "texto": texto})
        if self.bus.verbose:
            print(f"  [{self.nodo}] {self.nombre}: {texto}")
=======
        print(f"  [{self.nodo}] {self.nombre}: {texto}")
>>>>>>> 9be51136663ac36fd03ce90c555d1e9a012a9e1e

    def procesar(self, msg: Mensaje):
        raise NotImplementedError
