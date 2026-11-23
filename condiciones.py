"""
Catálogo de condiciones de la red (escenarios de demanda) para el día que se recomienda.

Cada condición tiene una descripción y sus parámetros. Los aplica el agente
deliberativo (E) sobre los pronósticos y bandas que entrega el agente de
aprendizaje (D):

  demanda          factor por tipo de carga (residencial, comercial, industrial,
                   servicio, critica)
  horas            si existe, los factores de demanda solo aplican en esas horas
  crecimiento_pct  crecimiento uniforme de toda la demanda
  renovable        factor por tipo de fuente (solar, eolica)
  capacidad        factor de capacidad del transformador de la subestación
  alimentador      factor de capacidad del alimentador en falla (A1 o A2)

Los valores son ilustrativos.
"""
from __future__ import annotations

import copy

import numpy as np

_VESPERTINO = list(range(17, 23))

CONDICIONES = {
    "normal": {
        "desc": "Operación normal",
        "descripcion": "Día típico con el pronóstico del agente de aprendizaje, sin ajustes.",
        "demanda": {}, "capacidad": 1.0,
    },
    "ola_calor": {
        "desc": "Ola de calor (pico de demanda)",
        "descripcion": "Aire acondicionado y refrigeración elevan la demanda residencial y comercial "
                       "todo el día; las fuentes renovables operan normalmente.",
        "demanda": {"residencial": 1.80, "comercial": 1.60, "critica": 1.10}, "capacidad": 1.0,
    },
    "frente_frio": {
        "desc": "Frente frío",
        "descripcion": "Calefacción eléctrica en hogares, cielo cubierto (menos solar) y viento "
                       "más intenso.",
        "demanda": {"residencial": 1.30, "comercial": 0.95, "critica": 1.05}, "capacidad": 1.0,
        "renovable": {"solar": 0.60, "eolica": 1.35},
    },
    "baja_renovable": {
        "desc": "Baja generación renovable (nublado y sin viento)",
        "descripcion": "Día nublado y en calma: la solar cae al 25 % y la mini-eólica al 20 % del "
                       "pronóstico; la demanda no cambia.",
        "demanda": {}, "capacidad": 1.0,
        "renovable": {"solar": 0.25, "eolica": 0.20},
    },
    "pico_vespertino": {
        "desc": "Pico vespertino extraordinario",
        "descripcion": "Entre las 17:00 y las 23:00 la demanda residencial sube 45 % y la comercial "
                       "25 % (evento masivo, regreso simultáneo a casa).",
        "demanda": {"residencial": 1.45, "comercial": 1.25}, "horas": _VESPERTINO, "capacidad": 1.0,
    },
    "crecimiento_demanda": {
        "desc": "Crecimiento de la demanda",
        "descripcion": "Toda la demanda crece un porcentaje uniforme (nuevos usuarios o "
                       "electrificación). Por omisión 15 %.",
        "demanda": {}, "capacidad": 1.0, "crecimiento_pct": 15.0,
    },
    "falla_transformador": {
        "desc": "Falla parcial del transformador de la subestación",
        "descripcion": "El transformador de SE-01 opera al 55 % de su capacidad (un banco fuera "
                       "de servicio).",
        "demanda": {}, "capacidad": 0.55,
    },
    "falla_alimentador": {
        "desc": "Falla de alimentador",
        "descripcion": "Un tramo del alimentador elegido (A1 o A2) queda aislado; la carga se "
                       "atiende por un enlace de respaldo con el 45 % de la capacidad.",
        "demanda": {}, "capacidad": 1.0, "alimentador": "A1", "factor_alimentador": 0.45,
    },
    "contingencia_natural": {
        "desc": "Contingencia natural (falla y pico combinados)",
        "descripcion": "Tormenta: el transformador de la subestación queda al 70 %, la solar cae a la "
                       "mitad y coincide un pico vespertino (+45 % residencial, +25 % comercial).",
        "demanda": {"residencial": 1.45, "comercial": 1.25}, "horas": _VESPERTINO, "capacidad": 0.70,
        "renovable": {"solar": 0.50, "eolica": 1.10},
    },
    # Alias de compatibilidad con versiones anteriores (`--condicion falla`)
    "falla": {
        "desc": "Falla parcial del transformador de la subestación",
        "descripcion": "Alias de 'falla_transformador'.",
        "demanda": {}, "capacidad": 0.55, "alias_de": "falla_transformador",
    },
}
ALIMENTADORES_FALLA = ("A1", "A2")


def resolver(condicion: str, alimentador: str | None = None,
             crecimiento_pct: float | None = None) -> dict:
    """Copia de la condición con sus parámetros resueltos."""
    if condicion not in CONDICIONES:
        raise ValueError(f"Condición no válida: {condicion}")
    c = copy.deepcopy(CONDICIONES[condicion])
    c["id"] = condicion
    if condicion == "falla_alimentador":
        if alimentador is not None:
            if alimentador not in ALIMENTADORES_FALLA:
                raise ValueError(f"Alimentador no válido: {alimentador}")
            c["alimentador"] = alimentador
        c["desc"] = f"Falla del alimentador {c['alimentador']}"
    if condicion == "crecimiento_demanda":
        if crecimiento_pct is not None:
            if not -50 <= crecimiento_pct <= 200:
                raise ValueError("El crecimiento debe estar entre -50 % y 200 %")
            c["crecimiento_pct"] = float(crecimiento_pct)
        c["desc"] = f"Crecimiento de la demanda ({c['crecimiento_pct']:+g} %)"
    return c


# --------------------------------------------- Gancho de pruebas (no expuesto)
# Solo lo usa crear_sistema(condicion_personalizada=...) desde las pruebas adversas.
# No está en la API, la web ni la app. Los límites salen del catálogo:
#   demanda: 0 .. 1 + 200 % (tope de crecimiento_pct); 0 permite probar demanda nula
#   renovable: 0 .. 2 (fuente fuera de servicio hasta el doble del pronóstico)
#   capacidad y factor_alimentador: (0, 1] (fallas; nunca más capacidad de la nominal)
TIPOS_CARGA = ("residencial", "comercial", "industrial", "servicio", "critica")
TIPOS_RENOVABLE = ("solar", "eolica")
FACTOR_DEMANDA_MAX = 1 + 200 / 100
FACTOR_RENOVABLE_MAX = 2.0


def _factor(valor, minimo: float, maximo: float, nombre: str):
    """Escalar o lista de 24 factores horarios dentro de [minimo, maximo]."""
    arr = np.asarray(valor, dtype=float)
    if arr.ndim not in (0, 1) or (arr.ndim == 1 and arr.shape != (24,)):
        raise ValueError(f"{nombre}: debe ser un número o 24 valores horarios")
    if not np.all(np.isfinite(arr)) or arr.min() < minimo or arr.max() > maximo:
        raise ValueError(f"{nombre}: los factores deben estar entre {minimo:g} y {maximo:g}")
    return float(arr) if arr.ndim == 0 else arr


def validar_personalizada(datos: dict) -> dict:
    """Condición de prueba con factores de demanda por tipo de carga y de renovables
    por fuente, escalares o por hora. Devuelve una condición resuelta como las del catálogo."""
    if not isinstance(datos, dict):
        raise ValueError("La condición personalizada debe ser un diccionario")
    permitidas = {"desc", "demanda", "renovable", "capacidad", "alimentador", "factor_alimentador",
                  "crecimiento_pct", "horas"}
    sobran = set(datos) - permitidas
    if sobran:
        raise ValueError(f"Claves no válidas en la condición personalizada: {sorted(sobran)}")
    c = {"id": "personalizada", "desc": str(datos.get("desc", "Condición personalizada (prueba)")),
         "descripcion": "Condición de prueba (gancho no expuesto en la API).",
         "demanda": {}, "capacidad": 1.0}
    for tipo, v in (datos.get("demanda") or {}).items():
        if tipo not in TIPOS_CARGA:
            raise ValueError(f"Tipo de carga no válido: {tipo}")
        c["demanda"][tipo] = _factor(v, 0.0, FACTOR_DEMANDA_MAX, f"demanda[{tipo}]")
    if datos.get("renovable"):
        c["renovable"] = {}
        for tipo, v in datos["renovable"].items():
            if tipo not in TIPOS_RENOVABLE:
                raise ValueError(f"Fuente renovable no válida: {tipo}")
            c["renovable"][tipo] = _factor(v, 0.0, FACTOR_RENOVABLE_MAX, f"renovable[{tipo}]")
    if "horas" in datos:
        horas = sorted({int(h) for h in datos["horas"]})
        if not horas or horas[0] < 0 or horas[-1] > 23:
            raise ValueError("Las horas deben estar entre 0 y 23")
        if any(np.ndim(v) for v in c["demanda"].values()):
            raise ValueError("Con 'horas' los factores de demanda deben ser escalares")
        c["horas"] = horas
    if "crecimiento_pct" in datos:
        crec = float(datos["crecimiento_pct"])
        if not -50 <= crec <= 200:
            raise ValueError("El crecimiento debe estar entre -50 % y 200 %")
        c["crecimiento_pct"] = crec
    c["capacidad"] = float(_factor(datos.get("capacidad", 1.0), 1e-6, 1.0, "capacidad"))
    if "alimentador" in datos or "factor_alimentador" in datos:
        alim = datos.get("alimentador", "A1")
        if alim not in ALIMENTADORES_FALLA:
            raise ValueError(f"Alimentador no válido: {alim}")
        c["alimentador"] = alim
        c["factor_alimentador"] = float(_factor(datos.get("factor_alimentador", 0.45), 1e-6, 1.0,
                                                "factor_alimentador"))
    return c


def parametros(c: dict) -> dict:
    """Parámetros numéricos de la condición (para mostrar y para la bitácora)."""
    p = {"factor_capacidad_subestacion": c.get("capacidad", 1.0)}
    if c.get("demanda"):
        p["factores_demanda"] = dict(c["demanda"])
    if c.get("horas"):
        p["horas"] = f"{c['horas'][0]:02d}:00-{c['horas'][-1] + 1:02d}:00"
    if c.get("renovable"):
        p["factores_renovable"] = dict(c["renovable"])
    if "crecimiento_pct" in c:
        p["crecimiento_pct"] = c["crecimiento_pct"]
    if "alimentador" in c:
        p["alimentador"] = c["alimentador"]
        p["factor_capacidad_alimentador"] = c["factor_alimentador"]
    return p


def factor_demanda(c: dict, tipo: str) -> np.ndarray:
    """Factor horario (24 valores) para una carga del tipo dado."""
    f = np.ones(24)
    v = c.get("demanda", {}).get(tipo, 1.0)
    if c.get("horas"):
        f[c["horas"]] = v
    else:
        f[:] = v
    return f * (1 + c.get("crecimiento_pct", 0.0) / 100)


def factor_renovable(c: dict, tipo: str) -> float:
    return c.get("renovable", {}).get(tipo, 1.0)


def factores_capacidad(c: dict) -> tuple[float, dict[str, float]]:
    """(factor de la subestación, {alimentador: factor})."""
    fa = {c["alimentador"]: c["factor_alimentador"]} if "alimentador" in c else {}
    return c.get("capacidad", 1.0), fa
