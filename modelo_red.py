"""
Modelo de la red de distribución eléctrica y simulador de dispositivos IoT.

Red de distribución (según el documento):
  - Media tensión (MT): subestación y alimentadores hacia zonas comerciales,
    industriales y centros de transformación.
  - Baja tensión (BT): transformadores que entregan a usuarios residenciales
    y centros de consumo locales.
  - Fuentes renovables descentralizadas conectadas a la red de distribución.

Todos los valores numéricos (potencias, tarifas, perfiles) son ilustrativos
para el prototipo; se sustituyen por los datos reales del caso de estudio.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

# Periodos de análisis seleccionables (días)
PERIODOS = {"1d": 1, "1s": 7, "1m": 30, "6m": 182, "1a": 365}
NOMBRE_PERIODO = {"1d": "1 día", "1s": "1 semana", "1m": "1 mes",
                  "6m": "6 meses", "1a": "1 año"}

COSTO_RENOVABLE = 0.30   # MXN/kWh (operación y mantenimiento, ilustrativo)


# ------------------------------------------------------------------ Elementos
@dataclass
class Carga:
    nombre: str
    tipo: str               # residencial | comercial | industrial | servicio | critica
    nivel: str              # MT | BT
    alimentador: str
    potencia_kw: float      # potencia de referencia
    desplazable: float      # fracción de la carga que puede moverse de horario
    prioridad_corte: int    # 1 = se suspende primero; 99 = nunca (crítica)

    @property
    def critica(self) -> bool:
        return self.tipo == "critica"


@dataclass
class FuenteRenovable:
    nombre: str
    tipo: str               # solar | eolica
    capacidad_kw: float
    alimentador: str


@dataclass
class Alimentador:
    """Alimentador de media tensión que sale de la subestación.

    capacidad_kw: límite térmico del troncal medido en la cabecera.
    transformador_bt_kw: capacidad agregada de los transformadores MT/BT que
    alimentan a las cargas de baja tensión (0 si el alimentador atiende en MT).
    Valores ilustrativos: se sustituyen por los del estudio de la red real."""
    clave: str              # A1 | A2
    nombre: str             # como aparece en Carga.alimentador
    nivel: str              # nivel de las cargas que atiende: MT | BT
    capacidad_kw: float
    transformador_bt_kw: float = 0.0


@dataclass
class RedDistribucion:
    subestacion: str
    tension_mt_kv: float
    tension_bt: str
    capacidad_convencional_kw: float    # límite del transformador de la subestación
    cargas: list[Carga] = field(default_factory=list)
    renovables: list[FuenteRenovable] = field(default_factory=list)
    alimentadores: list[Alimentador] = field(default_factory=list)
    perdidas_mt_pct: float = 2.5        # pérdidas técnicas aproximadas en la red de MT
    perdidas_bt_pct: float = 4.0        # pérdidas adicionales en transformadores y red de BT
    limite_inyeccion_kw: float = 800.0  # excedente renovable que puede subir a la subestación

    def alimentador(self, nombre_o_clave: str) -> Alimentador:
        for a in self.alimentadores:
            if nombre_o_clave in (a.clave, a.nombre):
                return a
        raise KeyError(f"Alimentador desconocido: {nombre_o_clave}")

    def cargas_de(self, clave: str) -> list[Carga]:
        nombre = self.alimentador(clave).nombre
        return [c for c in self.cargas if c.alimentador == nombre]

    def renovables_de(self, clave: str) -> list[FuenteRenovable]:
        nombre = self.alimentador(clave).nombre
        return [r for r in self.renovables if r.alimentador == nombre]

    # Tarifa horaria (estructura base / intermedia / punta, ilustrativa)
    TARIFAS = {"base": 1.10, "intermedia": 1.80, "punta": 3.20}

    @staticmethod
    def periodo_tarifario(hora: int) -> str:
        if 0 <= hora <= 5:
            return "base"
        if 18 <= hora <= 21:
            return "punta"
        return "intermedia"

    def tarifa(self, hora: int) -> float:
        return self.TARIFAS[self.periodo_tarifario(hora)]

    def vector_tarifas(self) -> np.ndarray:
        return np.array([self.tarifa(h) for h in range(24)])


def red_ejemplo() -> RedDistribucion:
    """Red de distribución de ejemplo: 1 subestación, 2 alimentadores de MT.

    Capacidades de alimentadores y transformadores de BT, pérdidas y límite de
    inyección son ILUSTRATIVOS (orden de magnitud razonable para 13.2 kV)."""
    red = RedDistribucion(subestacion="Subestación Reductora SE-01",
                          tension_mt_kv=13.2, tension_bt="127/220 V",
                          capacidad_convencional_kw=4_750)   # 5 MVA, fp 0.95
    red.cargas = [
        Carga("Hospital General", "critica", "MT", "Alimentador 1", 400, 0.00, 99),
        Carga("Parque industrial", "industrial", "MT", "Alimentador 1", 1_500, 0.25, 1),
        Carga("Bombeo de agua municipal", "servicio", "MT", "Alimentador 1", 350, 0.50, 2),
        Carga("Zona comercial centro", "comercial", "BT", "Alimentador 2", 900, 0.15, 3),
        Carga("Fraccionamiento norte", "residencial", "BT", "Alimentador 2", 800, 0.10, 4),
        Carga("Fraccionamiento sur", "residencial", "BT", "Alimentador 2", 700, 0.10, 4),
    ]
    red.renovables = [
        FuenteRenovable("Solar distribuida (techos y estacionamientos)", "solar", 1_200, "Alimentador 2"),
        FuenteRenovable("Mini-eólica parque industrial", "eolica", 600, "Alimentador 1"),
    ]
    red.alimentadores = [
        Alimentador("A1", "Alimentador 1", "MT", capacidad_kw=2_800),
        Alimentador("A2", "Alimentador 2", "BT", capacidad_kw=3_600, transformador_bt_kw=3_400),
    ]
    return red


# ------------------------------------------------------- Perfiles de consumo
_PUNTOS = {   # (hora, factor) -> se interpolan a 24 h
    "residencial": [(0, .40), (5, .32), (7, .60), (9, .50), (13, .55), (17, .65), (20, 1.0), (22, .85), (23, .55)],
    "comercial":   [(0, .20), (7, .30), (9, .90), (13, 1.0), (19, .95), (21, .45), (23, .22)],
    "industrial":  [(0, .55), (6, .60), (7, .95), (15, 1.0), (22, .90), (23, .60)],
    "servicio":    [(0, .70), (4, .95), (8, 1.0), (10, .70), (23, .70)],
    "critica":     [(0, .85), (8, 1.0), (18, 1.0), (23, .88)],
}
_FIN_SEMANA = {"residencial": 1.10, "comercial": 0.70, "industrial": 0.60,
               "servicio": 1.0, "critica": 1.0}
_ESTACIONAL = {"residencial": 0.22, "comercial": 0.18, "industrial": 0.05,
               "servicio": 0.10, "critica": 0.08}   # amplitud por clima cálido (may-ago)


def perfil_diario(tipo: str) -> np.ndarray:
    h, f = zip(*_PUNTOS[tipo])
    return np.interp(np.arange(24), h, f)


def factor_estacional(tipo: str, fecha: datetime) -> float:
    # Máximo a mediados de junio-julio (calor), mínimo en diciembre-enero
    return 1 + _ESTACIONAL[tipo] * np.cos(2 * np.pi * (fecha.month - 6.5) / 12)


# ------------------------------------------------------------- Simulador IoT
class SimuladorIoT:
    """Genera lecturas horarias de medidores inteligentes (consumo por nodo)
    y de los inversores de las fuentes renovables.

    Captura limitada a lo necesario: lecturas agregadas por nodo de la red,
    sin datos personales de usuarios finales."""

    TASA_ANOMALIAS = 0.003     # picos o caídas anómalas
    TASA_PERDIDAS = 0.002      # lecturas perdidas (NaN)

    def __init__(self, red: RedDistribucion, semilla: int = 7):
        self.red = red
        self.rng = np.random.default_rng(semilla)

    def capturar(self, periodo: str, fin: datetime) -> dict:
        dias = PERIODOS[periodo]
        n = dias * 24
        inicio = fin - timedelta(hours=n)
        tiempos = [inicio + timedelta(hours=i) for i in range(n)]
        horas = np.array([t.hour for t in tiempos])

        consumo, anomalias = {}, []
        for c in self.red.cargas:
            perfil = perfil_diario(c.tipo)[horas]
            estac = np.array([factor_estacional(c.tipo, t) for t in tiempos])
            semana = np.array([_FIN_SEMANA[c.tipo] if t.weekday() >= 5 else 1.0 for t in tiempos])
            ruido = self.rng.normal(1, 0.04, n)
            serie = c.potencia_kw * perfil * estac * semana * ruido

            idx = np.where(self.rng.random(n) < self.TASA_ANOMALIAS)[0]
            for i in idx:
                f = self.rng.choice([self.rng.uniform(1.6, 2.2), self.rng.uniform(0.1, 0.3)])
                serie[i] *= f
                anomalias.append((c.nombre, tiempos[i]))
            serie[self.rng.random(n) < self.TASA_PERDIDAS] = np.nan
            consumo[c.nombre] = serie

        generacion = {}
        for r in self.red.renovables:
            if r.tipo == "solar":
                hd = horas + 0.5
                campana = np.clip(np.sin(np.pi * (hd - 6.5) / 12), 0, None)
                estac = np.array([0.85 + 0.15 * np.cos(2 * np.pi * (t.month - 5) / 12) for t in tiempos])
                nubes = np.repeat(self.rng.uniform(0.55, 1.0, dias), 24)
                serie = r.capacidad_kw * 0.85 * campana * estac * nubes
            else:
                v = np.empty(n)
                v[0] = 0.35
                for i in range(1, n):   # viento con autocorrelación AR(1)
                    v[i] = 0.9 * v[i - 1] + 0.1 * self.rng.weibull(2) * 0.4
                serie = r.capacidad_kw * np.clip(v, 0, 1)
            generacion[r.nombre] = serie

        return {"periodo": periodo, "tiempos": tiempos, "consumo": consumo,
                "generacion": generacion, "anomalias_inyectadas": anomalias}
