"""
Campaña de EVALUACIÓN DE RESULTADOS de ElectriAgent: continuidad, eficiencia y costo.

    python evaluar.py                      # campaña completa: 1d, 1s, 1m, 6m, 1a × 5 semillas (< 15 min)
    python evaluar.py --rapido             # solo 1m (< 2 min); escribe en docs/evaluacion/rapido/
    python evaluar.py --corrida CAT.falla_transformador --periodo 1m --semilla 7 --dia laboral \
                      --perfil aprueba [--mejora M-PRON]       # reproduce una corrida e imprime sus KPI
    python evaluar.py --supuestos mis_supuestos.json           # cambia los supuestos (CENS, precios...)
    python evaluar.py --listar                                 # escenarios y mejoras disponibles

Regla de la campaña: EVALUAR, NO CORREGIR. No se modifica el motor, la API, la web ni la app.
Las mejoras se evalúan como escenarios hipotéticos que viven SOLO en este script:
  - componentes alternativos que se enganchan a la instancia de un agente de una corrida
    (p. ej. un pronosticador por tipo de día que reescribe el análisis de D antes de que lo lea E);
  - cálculos posteriores sobre las Distribuciones que entrega E (exportación, tope de recorte);
  - procesos independientes para medir concurrencia (sin tocar servidor.py).
Nada de esto queda activo en producción: los ganchos se aplican a objetos de la corrida.

"Realidad": para medir lo que pasa el día recomendado se simula ese día con el mismo simulador
IoT (semilla distinta, sin anomalías ni lecturas perdidas) y se le aplica la condición de la red.
El plan aprobado (desplazamientos + recorte fijo) se aplica a esa realidad con distribucion.asignar.

Solo biblioteca estándar + numpy + matplotlib. Indicadores en evaluacion.py; pruebas en
pruebas_evaluacion.py.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import csv
import io
import json
import os
import platform
import shutil
import sys
import tempfile
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import numpy as np

AQUI = os.path.dirname(os.path.abspath(__file__))
if AQUI not in sys.path:
    sys.path.insert(0, AQUI)

import evaluacion as ev  # noqa: E402
from agentes import AgenteAprendizaje  # noqa: E402
from condiciones import (CONDICIONES, factor_demanda, factor_renovable, resolver,  # noqa: E402
                         validar_personalizada)
from distribucion import asignar  # noqa: E402
from modelo_red import (_FIN_SEMANA, COSTO_RENOVABLE, PERIODOS, RedDistribucion, SimuladorIoT,  # noqa: E402
                        factor_estacional, perfil_diario, red_ejemplo)
from motor import crear_sistema  # noqa: E402

DIR_INFORME = os.path.join(AQUI, "docs", "evaluacion")
OPERADOR = "Evaluación"

# ============================================================== SUPUESTOS
# Valores ILUSTRATIVOS. Se pueden cambiar con --supuestos archivo.json (mismas claves).
SUPUESTOS = {
    # Costo de la energía no suministrada por tipo de carga [MXN/kWh] (CENS, "value of lost load").
    "cens_mxn_kwh": {"residencial": 40.0, "comercial": 120.0, "industrial": 90.0, "servicio": 150.0,
                     "critica": 3000.0},
    # El déficit no planificado (no servido) cuesta más que el recorte avisado: × factor.
    "factor_sin_aviso": 1.5,
    # Probabilidad del escenario desfavorable (demanda P90 con renovable P10) para el costo esperado.
    "prob_desfavorable": 0.10,
    # Precio de la energía exportada aguas arriba (M-EXPORT) [MXN/kWh].
    "precio_exportacion_mxn_kwh": 0.90,
    # M-ROBUSTEZ: el recorte del plan con tope no supera tope × déficit central (P50).
    "tope_recorte": 2.0,
    # M-ROBUSTEZ (costo mínimo): mundos ex ante (α de la banda: 0 = P50, 1 = P90/P10) y sus pesos.
    "mundos_exante": [[0.0, 0.5], [0.5, 0.4], [1.0, 0.1]],
    # M-BANDA: semiancho mínimo de la banda de demanda con poco historial y días que cuentan como "poco".
    "semiancho_minimo": 0.15,
    "dias_historial_corto": 7,
    # Día recomendado fijo: laboral (jueves) y fin de semana (domingo), misma estacionalidad.
    "dias": {"laboral": "2026-11-19", "finde": "2026-11-22"},
    "semillas": [7, 101, 202, 303, 404],
    # La realidad del día recomendado se simula con semilla + desfase (ruido independiente del historial).
    "desfase_semilla_realidad": 50_000,
    # Si el caso se escala o el flujo se rompe no hay plan: la red opera como S0 (sin cambios).
    "operacion_sin_plan": "S0",
}
PERFILES = {
    "aprueba": None,
    "recortar_menos": "Recortar menos: el recorte propuesto afecta demasiado a los usuarios",
    "mas_seguridad": "Pido más seguridad: plan robusto ante P90",
}


def dias() -> dict[str, datetime]:
    return {k: datetime.fromisoformat(v) for k, v in SUPUESTOS["dias"].items()}


# ============================================================== escenarios
@dataclass
class Esc:
    id: str
    familia: str
    desc: str
    condicion: str = "normal"
    alimentador: str | None = None
    personalizada: dict | Callable | None = None
    datos: Callable | None = None           # (datos IoT, rng, registro) -> None
    agentes: Callable | None = None         # (sistema) -> None
    realidad: str = "simulada"              # simulada | optimista (renovable real 0.5 × P10) | renovable_x0.3
    referencia: str | None = None           # escenario sano para S0 y la realidad si el flujo se rompe


def _catalogo() -> list[Esc]:
    out = []
    for cid, c in CONDICIONES.items():
        if "alias_de" in c:
            continue
        if cid == "falla_alimentador":
            out += [Esc(f"CAT.falla_alimentador_{a}", "CAT", f"Falla del alimentador {a}", cid, alimentador=a)
                    for a in ("A1", "A2")]
        else:
            out.append(Esc(f"CAT.{cid}", "CAT", c["desc"], cid))
    return out


def _adversos() -> tuple[list[Esc], list[Esc], list[Esc]]:
    """Casos tomados de pruebas_adversas.py: representativos F1–F6, de fallas y de datos."""
    import pruebas_adversas as pa
    T = pa.TODOS

    def de(cid, nuevo=None, familia=None, **k):
        c = T[cid]
        return Esc(nuevo or cid, familia or c.familia, c.desc, k.pop("condicion", c.condicion),
                   personalizada=c.personalizada, datos=c.datos, agentes=c.agentes, **k)
    reps = [de("F1.crec+60"), de("F2.dem-60"), de("F3.renov150"),
            de("F4.optimista-trafo55", realidad="optimista"), de("F5.dem+60-renov0-trafo55"), de("F6.negativas")]
    trafo = "CAT.falla_transformador"
    fallas = [
        Esc("A1.falla-B-trafo55", "A1", "Falla única en B con el transformador al 55 %", "falla_transformador",
            agentes=pa.falla_una_vez("B"), referencia=trafo),
        Esc("A1.falla-G-trafo55", "A1", "Falla única en G con el transformador al 55 %", "falla_transformador",
            agentes=pa.falla_una_vez("G"), referencia=trafo),
        Esc("A1.falla-H-trafo55", "A1", "Falla única en H con el transformador al 55 %", "falla_transformador",
            agentes=pa.falla_una_vez("H"), referencia=trafo),
        Esc("A2.sensor-fuera-trafo55", "A2", "Medidor comercial sin lecturas con el transformador al 55 %",
            "falla_transformador", datos=pa.sensor_fuera, referencia=trafo),
        de("F2.cero-total", familia="A3"),
        de("F1.hospital-imposible", familia="A4"),
        Esc("A5.cobertura-trafo55", "A5", "La renovable cae al 30 % desde el último día del historial y sigue "
            "así el día recomendado (transformador al 55 %)", "falla_transformador",
            datos=pa.renovable_ultimo_dia, realidad="renovable_x0.3"),
    ]
    datos = [de("F6.congelado"), de("F6.sensor-fuera", referencia="CAT.normal")] + [
        Esc(f"{c}-trafo55", "F6", T[c].desc + " con el transformador al 55 %", "falla_transformador",
            datos=T[c].datos, referencia=trafo if c == "F6.sensor-fuera" else None)
        for c in ("F6.negativas", "F6.congelado", "F6.sensor-fuera")]
    # Excedente renovable (M-EXPORT): los casos de la campaña adversa que desperdician renovable
    exporta = [de("F3.renov150-dem-60"), de("F3.renov150-dem-90"), de("F2.dem-90"),
               de("F5.baja-renov150-fallaA2")]
    return reps, fallas, datos, exporta


CATALOGO = _catalogo()
REPRESENTATIVOS, FALLAS_ESC, DATOS_ESC, EXPORTA_ESC = _adversos()
ESCENARIOS = {e.id: e for e in CATALOGO + REPRESENTATIVOS + FALLAS_ESC + DATOS_ESC + EXPORTA_ESC}
CAMPANA_PRINCIPAL = [e.id for e in CATALOGO + REPRESENTATIVOS]


def condicion_de(esc: Esc, rng) -> tuple[dict | None, dict]:
    """(dict personalizado para el motor o None, condición resuelta para la realidad)."""
    pers = esc.personalizada(rng) if callable(esc.personalizada) else esc.personalizada
    if pers is not None:
        return pers, validar_personalizada(pers)
    params = {"alimentador": esc.alimentador} if esc.alimentador else {}
    return None, resolver(esc.condicion, **params)


# ====================================================== mejoras (ganchos)
def limpias_de(datos: dict, analisis: dict) -> dict[str, np.ndarray]:
    """Series que D usó para aprender: consumo (ya interpolado por B) con las anomalías detectadas
    reemplazadas por su valor esperado (igual que AgenteAprendizaje.procesar)."""
    out = {n: np.array(x, float) for n, x in datos["consumo"].items()}
    idx = {t.isoformat(timespec="minutes"): i for i, t in enumerate(datos["tiempos"])}
    for a in analisis["anomalias"]:
        out[a["carga"]][idx[a["fecha"]]] = a["esperado_kw"]
    return out


def pronostico_tipo_dia(m: np.ndarray, tipos: np.ndarray, objetivo: bool) -> np.ndarray:
    """M-PRON: promedio ponderado de los últimos días DEL MISMO TIPO (4 de fin de semana o 5
    laborales) con la misma tendencia semanal que AgenteAprendizaje._pronostico."""
    mismos = np.where(tipos == objetivo)[0]
    if len(mismos) == 0:
        return AgenteAprendizaje._pronostico(m)
    k = min(4 if objetivo else 5, len(mismos))
    pesos = np.arange(1, k + 1)
    base = (m[mismos[-k:]] * pesos[:, None]).sum(0) / pesos.sum()
    if m.shape[0] >= 14:
        base = base * np.clip(m[-7:].mean() / max(m[-14:-7].mean(), 1e-6), 0.9, 1.1)
    return base


def _residuales_tipo_dia(m: np.ndarray, tipos: np.ndarray) -> np.ndarray:
    dias_ = m.shape[0]
    if dias_ < 2:
        return AgenteAprendizaje._residuales(m)
    res = []
    for d in range(max(1, dias_ - 60), dias_):
        p = pronostico_tipo_dia(m[:d], tipos[:d], tipos[d])
        res.append((m[d] - p) / np.maximum(p, 1e-6))
    return np.concatenate(res)


def gancho_pron(s, an: dict, datos: dict):
    """M-PRON: reescribe pronóstico, bandas, MAPE y cobertura de D con el pronosticador por tipo de día."""
    dias_ = an["dias"]
    tiempos = datos["tiempos"]
    tipos = np.array([tiempos[d * 24].weekday() >= 5 for d in range(dias_)])
    objetivo = s.fin.weekday() >= 5
    limpias = limpias_de(datos, an)
    for n, x in limpias.items():
        m = x.reshape(dias_, 24)
        p = pronostico_tipo_dia(m, tipos, objetivo)
        an["pronostico_cargas"][n] = p
        an["bandas_cargas"][n] = AgenteAprendizaje._banda(p, _residuales_tipo_dia(m, tipos))
    if dias_ >= 2:
        pred = sum(pronostico_tipo_dia(x.reshape(dias_, 24)[:-1], tipos[:-1], tipos[-1]) for x in limpias.values())
        real = sum(limpias.values())[-24:]
        an["mape_pct"] = round(float(np.mean(np.abs(pred - real) / real) * 100), 2)
    if dias_ >= 3:
        dentro = total = 0
        for n, x in limpias.items():
            m = x.reshape(dias_, 24)[:-1]
            b = AgenteAprendizaje._banda(pronostico_tipo_dia(m, tipos[:-1], tipos[-1]),
                                         _residuales_tipo_dia(m, tipos[:-1]))
            real = datos["consumo"][n][-24:]
            dentro += int(((real >= b["p10"] - 1e-6) & (real <= b["p90"] + 1e-6)).sum())
            total += 24
        an["cobertura_bandas"] = dict(an["cobertura_bandas"], pct=round(dentro / total * 100, 1),
                                      dentro=dentro, lecturas=total)


def gancho_banda(s, an: dict, datos: dict):
    """M-BANDA: (A6) piso de incertidumbre con poco historial: P10 ≤ (1−w)·P50 y P90 ≥ (1+w)·P50;
    (A5) validación fuera de muestra de la banda renovable con el último día del historial: si la
    energía real cae bajo P10, P10 baja a (real/pronóstico)·P50."""
    w = SUPUESTOS["semiancho_minimo"]
    dias_ = an["dias"]
    if dias_ < SUPUESTOS["dias_historial_corto"]:
        for b in an["bandas_cargas"].values():
            b["p10"] = np.minimum(b["p10"], b["p50"] * (1 - w))
            b["p90"] = np.maximum(b["p90"], b["p50"] * (1 + w))
    if dias_ >= 3:
        alertas = []
        for n, g in datos["generacion"].items():
            m = g.reshape(dias_, 24)
            hist = m[:-1]
            p = hist[-min(3, len(hist)):].mean(0)
            b = AgenteAprendizaje._banda(p, AgenteAprendizaje._residuales_renovable(hist))
            real = float(m[-1].sum())
            if real < float(b["p10"].sum()) - 1e-6:
                alertas.append(n)
                razon = real / max(float(p.sum()), 1e-6)
                bn = an["bandas_renovables"][n]
                bn["p10"] = np.minimum(bn["p10"], razon * bn["p50"])
        an["alerta_banda_renovable"] = alertas


def gancho_datos(datos: dict, red: RedDistribucion, registro: dict):
    """M-DATOS: datos limpios en B antes de interpolar. Negativas → faltantes; corridas de ≥ 6
    lecturas idénticas (sensor congelado) → faltantes; huecos largos → valor de la misma hora 7 días
    antes; medidor sin lecturas válidas → fuera de servicio, se estima con su perfil típico."""
    tiempos = datos["tiempos"]
    tipo = {c.nombre: c for c in red.cargas}
    registro["fuera_servicio"], registro["marcadas"] = [], 0
    for n, x in datos["consumo"].items():
        malo = np.zeros(len(x), bool)
        malo |= np.isfinite(x) & (x < 0)
        iguales = np.r_[False, np.diff(x) == 0]
        i = 0
        while i < len(x):            # corridas de valores idénticos
            if iguales[i]:
                j = i
                while j < len(x) and iguales[j]:
                    j += 1
                if j - i + 1 >= 6:
                    malo[i - 1:j] = True
                i = j
            else:
                i += 1
        registro["marcadas"] += int(malo.sum())
        x[malo] = np.nan
        nan = np.isnan(x)
        if nan.all():
            c = tipo[n]
            registro["fuera_servicio"].append(n)
            x[:] = [c.potencia_kw * perfil_diario(c.tipo)[t.hour] * factor_estacional(c.tipo, t)
                    * (_FIN_SEMANA[c.tipo] if t.weekday() >= 5 else 1.0) for t in tiempos]
            continue
        for i in np.where(nan)[0]:   # hueco: misma hora de la semana anterior
            if i >= 168 and np.isfinite(x[i - 168]):
                x[i] = x[i - 168]


MEJORAS_GANCHO = {"M-PRON": gancho_pron, "M-BANDA": gancho_banda}


def _enganchar_d(s, fn):
    d = s.bus.agentes["D"]
    original = d.procesar

    def procesar(msg):
        original(msg)
        fn(s, s.pizarra.leer("analisis"), s.pizarra.leer("datos"))
    d.procesar = procesar


def _alterar_datos(s, fns: list):
    b = s.bus.agentes["B"]
    original = b.simulador.capturar

    def capturar(periodo, fin):
        datos = original(periodo, fin)
        for fn in fns:
            fn(datos)
        return datos
    b.simulador.capturar = capturar


def _espiar(s, propuestas: list):
    g = s.aprobacion
    original = g.procesar

    def procesar(msg):
        dec = s.pizarra.leer("decision")
        if dec and dec.get("escenario"):
            paquete = s.pizarra.leer("escenarios")
            evals = {e["id"]: e for e in s.pizarra.leer("evaluacion")["escenarios"]}
            propuestas.append({"esc": next(e for e in paquete["lista"] if e.id == dec["escenario"]),
                               "decision": dict(dec), "evals": evals, "paquete": paquete})
        return original(msg)
    g.procesar = procesar


# ================================================================= corrida
@dataclass
class Corrida:
    esc: Esc
    periodo: str
    semilla: int
    dia: str
    perfil: str
    mejora: str | None
    excepcion: str | None
    tiempo_s: float
    resultado: str
    mensajes: int
    costo_coordinacion: int
    decisiones_operador: int
    lista: list                    # escenarios de la primera deliberación
    ctx: object
    analisis: dict
    evals: dict
    inicial: object                # primera propuesta (Escenario) o None
    final: object                  # propuesta aprobada o None
    decision: dict
    s0: object
    fin: datetime
    extra: dict = field(default_factory=dict)

    @property
    def operacion(self):
        """Lo que la red opera: la propuesta aprobada o, sin plan, S0 (supuesto)."""
        return self.final if self.final is not None else self.s0


def comando(esc_id, periodo, semilla, dia, perfil="aprueba", mejora=None) -> str:
    return (f"python evaluar.py --corrida {esc_id} --periodo {periodo} --semilla {semilla} --dia {dia} "
            f"--perfil {perfil}" + (f" --mejora {mejora}" if mejora else ""))


def correr(esc: Esc, periodo: str, semilla: int, dia: str, perfil: str = "aprueba",
           mejora: str | None = None) -> Corrida:
    rng = np.random.default_rng(semilla)
    pers, _ = condicion_de(esc, rng)
    fin = dias()[dia]
    d = tempfile.mkdtemp(prefix="ea_eval_")
    try:
        s = crear_sistema(periodo=periodo, condicion=esc.condicion, modo="web", semilla=semilla, salida=d,
                          ruta_memoria=os.path.join(d, "memoria.json"), verbose=False, fin=fin,
                          alimentador=esc.alimentador, condicion_personalizada=pers)
        propuestas, registro = [], {}
        _espiar(s, propuestas)
        alteraciones = []
        if esc.datos:
            alteraciones.append(lambda datos: esc.datos(datos, rng, registro))
        if mejora == "M-DATOS":
            alteraciones.append(lambda datos: gancho_datos(datos, s.red, registro))
        if alteraciones:
            _alterar_datos(s, alteraciones)
        if mejora in MEJORAS_GANCHO:
            _enganchar_d(s, MEJORAS_GANCHO[mejora])
        if esc.agentes:
            esc.agentes(s)
        excepcion, decisiones = None, 0
        t0 = time.perf_counter()
        with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
            warnings.simplefilter("ignore")
            try:
                s.iniciar()
                if s.en_espera and PERFILES[perfil]:
                    decisiones += 1
                    s.responder(False, PERFILES[perfil], OPERADOR)
                if s.en_espera:
                    decisiones += 1
                    s.responder(True, "", OPERADOR)
            except Exception as e:      # el flujo se rompió (A1, A2...)
                excepcion = f"{type(e).__name__}: {e}"
        seg = time.perf_counter() - t0
    finally:
        shutil.rmtree(d, ignore_errors=True)

    paquete = propuestas[0]["paquete"] if propuestas else s.pizarra.leer("escenarios")
    lista = list(paquete["lista"]) if paquete else []
    final = None
    resultado = ev.resultado_decision(s, excepcion)
    if resultado == "propuesta":
        fid = s.salida.reporte["recomendacion"]["escenario"]
        final = next(e for e in s.pizarra.leer("escenarios")["lista"] if e.id == fid)
    evaluacion = s.pizarra.leer("evaluacion")
    evals = propuestas[0]["evals"] if propuestas else ({e["id"]: e for e in evaluacion["escenarios"]}
                                                        if evaluacion else {})
    if final is not None and evaluacion:
        evals = dict(evals, **{e["id"]: e for e in evaluacion["escenarios"] if e["id"] == final.id})
    an = s.pizarra.leer("analisis") or {}
    analisis = {k: an.get(k) for k in ("dias", "pronostico_cargas", "bandas_cargas", "pronostico_renovables",
                                       "bandas_renovables", "mape_pct", "cobertura_bandas", "anomalias",
                                       "alerta_banda_renovable")}
    analisis["alerta_deriva"] = bool(evaluacion and evaluacion["trazabilidad"]["alerta_deriva"])
    analisis["alerta_calibracion"] = bool(evaluacion and evaluacion["trazabilidad"]["alerta_calibracion_bandas"])
    return Corrida(esc, periodo, semilla, dia, perfil, mejora, excepcion, seg, resultado, s.bus.enviados,
                   s.bus.costo_total, decisiones, lista, paquete["contexto"] if paquete else None, analisis, evals,
                   propuestas[0]["esc"] if propuestas else None, final, s.pizarra.leer("decision") or {},
                   next((e for e in lista if e.id == "S0"), None), fin, {"registro": registro})


# ================================================================= realidad
@dataclass
class Realidad:
    cargas: dict           # carga -> 24 h (kW) con la condición aplicada
    renovables: dict       # fuente -> 24 h (kW)


def simular_realidad(esc: Esc, semilla: int, fin: datetime, ctx, red: RedDistribucion) -> Realidad:
    """Día recomendado "real": el simulador IoT con semilla + desfase, sin anomalías ni pérdidas,
    con la condición de la red aplicada. optimista: renovable real = 0.5 × P10 (como F4)."""
    rng = np.random.default_rng(semilla)
    _, cond = condicion_de(esc, rng)
    sim = SimuladorIoT(red, semilla + SUPUESTOS["desfase_semilla_realidad"])
    sim.TASA_ANOMALIAS = 0.0
    sim.TASA_PERDIDAS = 0.0
    datos = sim.capturar("1d", fin + timedelta(days=1))
    cargas = {c.nombre: datos["consumo"][c.nombre] * factor_demanda(cond, c.tipo) for c in red.cargas}
    ren = {r.nombre: np.clip(datos["generacion"][r.nombre] * factor_renovable(cond, r.tipo), 0, r.capacidad_kw)
           for r in red.renovables}
    if esc.realidad == "optimista":
        ren = {n: 0.5 * v for n, v in ctx.renovables_p10.items()}
    elif esc.realidad == "renovable_x0.3":
        ren = {n: 0.3 * v for n, v in ren.items()}
    return Realidad(cargas, ren)


def aplicar(red, ctx, esc, cargas: dict, renov: dict, plan: dict | None = None, recortar: bool = False):
    """El plan de `esc` (desplazamientos = esc.cargas − ctx.cargas, y su recorte fijo) en otro mundo."""
    c = {n: np.clip(cargas[n] + esc.cargas[n] - ctx.cargas[n], 0, None) for n in cargas}
    if recortar:
        return asignar(red, c, renov, ctx.limites, ctx.tarifas, recortar=True)
    plan = esc.recorte if plan is None else plan
    return asignar(red, c, renov, ctx.limites, ctx.tarifas, recorte_fijo=plan or None)


# ============================================================== indicadores
def _cens():
    return SUPUESTOS["cens_mxn_kwh"], SUPUESTOS["factor_sin_aviso"]


def kpis(c: Corrida, real: Realidad, red: RedDistribucion, ref: Corrida | None = None) -> dict:
    """Fila de kpis.csv para una corrida."""
    t = red.vector_tarifas()
    cens, fsa = _cens()
    s0 = c.s0 if c.s0 is not None else (ref.s0 if ref else None)
    ctx = c.ctx if c.ctx is not None else (ref.ctx if ref else None)
    op = c.final if c.final is not None else s0
    f = {"escenario": c.esc.id, "familia": c.esc.familia, "periodo": c.periodo, "semilla": c.semilla,
         "dia": c.dia, "perfil": c.perfil, "mejora": c.mejora or "", "resultado": c.resultado,
         "excepcion": (c.excepcion or "")[:80],
         "propuesta_inicial": c.inicial.id if c.inicial is not None else "-",
         "propuesta_final": c.final.id if c.final is not None else "-",
         "modo": c.decision.get("modo", "-"), "robusta": bool(c.final.robusto) if c.final is not None else "",
         "mensajes": c.mensajes, "costo_coordinacion": c.costo_coordinacion, "tiempo_s": round(c.tiempo_s, 4),
         "decisiones_operador": c.decisiones_operador,
         "motivo_escalamiento": " ".join(c.decision.get("justificacion") or [])[:90] if c.resultado == "escalamiento" else ""}
    if s0 is None or op is None or ctx is None:
        f["comando"] = comando(c.esc.id, c.periodo, c.semilla, c.dia, c.perfil, c.mejora)
        return f
    d0, d = s0.distribucion, op.distribucion
    # --- continuidad (planificado, banda P50)
    g = ev.ens_por_grupo(d, red)
    a = ev.afectacion(d, red)
    pa = ev.pct_servida_por_alimentador(d)
    ea = ev.ens_por_alimentador(d)
    just = " ".join(c.decision.get("justificacion") or [])
    f.update({
        "ens_s0_kwh": ev.ens_kwh(d0), "ens_kwh": ev.ens_kwh(d),
        "recorte_kwh": float(d.total("recorte").sum()), "no_servido_kwh": float(d.total("no_servido").sum()),
        "ens_A1_kwh": ea["A1"], "ens_A2_kwh": ea["A2"],
        "ens_res_com_kwh": g["Usuarios residenciales y comerciales"],
        "ens_ind_serv_kwh": g["Industria y servicios públicos"], "ens_critica_kwh": g["Cargas críticas (hospital)"],
        "ens_critica_s0_kwh": ev.ens_critica_kwh(d0, red),
        "pct_servida_s0": ev.pct_servida(d0), "pct_servida": ev.pct_servida(d),
        "pct_servida_A1": pa["A1"], "pct_servida_A2": pa["A2"],
        "saidi_h": a["saidi_h"], "saifi": a["saifi"], "caidi_h": a["caidi_h"], "cargas_afectadas": a["cargas_afectadas"],
        "saidi_s0_h": ev.afectacion(d0, red)["saidi_h"],
        "horas_deficit_p90": ev.horas_deficit(op.distribucion_p90),
        "ens_p90_kwh": ev.ens_kwh(op.distribucion_p90),
        "escalamiento_menciona_critica": ("crític" in just.lower() or "hospital" in just.lower())
        if c.resultado == "escalamiento" else "",
    })
    # --- continuidad real (el plan en el día que ocurre)
    dr0 = aplicar(red, ctx, s0, real.cargas, real.renovables)
    dr = aplicar(red, ctx, op, real.cargas, real.renovables)
    dmin = aplicar(red, ctx, op, real.cargas, real.renovables, recortar=True)
    f.update({
        "ens_real_s0_kwh": ev.ens_kwh(dr0), "ens_real_kwh": ev.ens_kwh(dr),
        "ens_no_prevista_kwh": ev.ens_no_prevista_kwh(dr, d),
        "ens_minima_real_kwh": ev.ens_kwh(dmin),
        "sobre_recorte_kwh": max(0.0, float(dr.total("recorte").sum()) - ev.ens_kwh(dmin)),
        "ens_critica_real_kwh": ev.ens_critica_kwh(dr, red),
    })
    # --- eficiencia
    r, r0 = ev.renovable(d), ev.renovable(d0)
    p, p0 = ev.perdidas(d), ev.perdidas(d0)
    u = ev.utilizacion(d)
    desplazado = float(sum(op.desplazamientos.values()))
    f.update({
        "renovable_pct_s0": r0["pct_demanda"], "renovable_pct": r["pct_demanda"],
        "renov_disponible_kwh": r["disponible_kwh"], "renov_usada_kwh": r["usada_kwh"],
        "renov_desperdiciada_kwh": r["desperdiciada_kwh"], "renov_desperdiciada_s0_kwh": r0["desperdiciada_kwh"],
        "pct_renov_aprovechada": r["pct_aprovechada"],
        "perdidas_kwh": p["kwh"], "perdidas_pct": p["pct"], "perdidas_s0_kwh": p0["kwh"], "perdidas_s0_pct": p0["pct"],
        "util_max_A1": u["A1"]["max_pct"], "util_media_A1": u["A1"]["media_pct"],
        "util_max_A2": u["A2"]["max_pct"], "util_media_A2": u["A2"]["media_pct"],
        "util_max_trafoBT_A2": u["A2-trafoBT"]["max_pct"], "util_media_trafoBT_A2": u["A2-trafoBT"]["media_pct"],
        "util_max_SE": u["SE"]["max_pct"], "util_media_SE": u["SE"]["media_pct"],
        "factor_carga_s0": ev.factor_carga(d0.importacion_subestacion),
        "factor_carga": ev.factor_carga(d.importacion_subestacion),
        "reduccion_pico_pct": ev.reduccion_pico_pct(d0, d),
        "desplazado_kwh": desplazado,
        "desplazado_por_deficit_evitado": ev.desplazado_por_deficit_evitado(desplazado, ev.ens_kwh(d0), ev.ens_kwh(d)),
        "acciones": ev.n_acciones(c.final),
    })
    # --- pronóstico
    an = c.analisis
    tot = lambda dd: sum(dd.values())   # noqa: E731
    dentro_d = tot_d = dentro_r = tot_r = 0
    for n, v in real.cargas.items():
        a_, b_ = ev.cobertura(ctx.cargas_p10[n], ctx.cargas_p90[n], v)
        dentro_d, tot_d = dentro_d + a_, tot_d + b_
    for n, v in real.renovables.items():
        a_, b_ = ev.cobertura(ctx.renovables_p10[n], ctx.renovables_p90[n], v)
        dentro_r, tot_r = dentro_r + a_, tot_r + b_
    ev_op = c.evals.get(op.id) or c.evals.get("S0") or {}
    ren50, ren10, ren90 = tot(ctx.renovables).sum(), tot(ctx.renovables_p10).sum(), tot(ctx.renovables_p90).sum()
    f.update({
        "mape_validacion_pct": an.get("mape_pct"), "mape_real_pct": ev.mape_pct(tot(ctx.cargas), tot(real.cargas)),
        "sesgo_real_pct": ev.sesgo_pct(tot(ctx.cargas), tot(real.cargas)),
        "pronostico_min_kw": float(min(np.min(v) for v in c.analisis["pronostico_cargas"].values()))
        if c.analisis.get("pronostico_cargas") else None,
        "cobertura_dem_real_pct": 100 * dentro_d / tot_d if tot_d else None,
        "cobertura_ren_real_pct": 100 * dentro_r / tot_r if tot_r else None,
        "cobertura_validacion_pct": (an.get("cobertura_bandas") or {}).get("pct"),
        "ancho_dem_pct": ev.ancho_relativo_pct(tot(ctx.cargas_p10), tot(ctx.cargas), tot(ctx.cargas_p90)),
        "ancho_ren_pct": 100 * (ren90 - ren10) / ren50 if ren50 > 1e-6 else 0.0,
        "riesgo_max": ev_op.get("riesgo_max"), "riesgo_max_s0": (c.evals.get("S0") or {}).get("riesgo_max"),
        "alerta_deriva": an.get("alerta_deriva"),
        "impacto_operador": next((i["efecto"] for i in ev_op.get("impactos", []) if i["grupo"] == "Operador de la red"),
                                 ""),
        "alerta_calibracion": an.get("alerta_calibracion"),
    })
    # --- costo
    ah = ev.descomponer_ahorro(d0, d, t)
    f.update({
        "costo_s0_mxn": ah["costo_s0_mxn"], "costo_mxn": ah["costo_mxn"],
        "costo_kwh_s0": ev.costo_por_kwh_servido(d0, t), "costo_kwh": ev.costo_por_kwh_servido(d, t),
        "ahorro_mxn": ah["ahorro_mxn"], "ahorro_pct": ah["ahorro_pct"],
        "efecto_tarifa_mxn": ah["efecto_tarifa_mxn"], "efecto_renovable_mxn": ah["efecto_renovable_mxn"],
        "efecto_perdidas_mxn": ah["efecto_perdidas_mxn"], "efecto_servicio_mxn": ah["efecto_servicio_mxn"],
        "costo_social_s0_mxn": ev.costo_social_ens_mxn(d0, red, cens, fsa),
        "costo_social_mxn": ev.costo_social_ens_mxn(d, red, cens, fsa),
        "costo_total_s0_mxn": ev.costo_total_mxn(d0, red, t, cens, fsa),
        "costo_total_mxn": ev.costo_total_mxn(d, red, t, cens, fsa),
        "costo_total_real_s0_mxn": ev.costo_total_mxn(dr0, red, t, cens, fsa),
        "costo_total_real_mxn": ev.costo_total_mxn(dr, red, t, cens, fsa),
        "costo_social_real_mxn": ev.costo_social_ens_mxn(dr, red, cens, fsa),
    })
    m = op.metricas
    if m.get("recorte_adicional_robustez_kwh") is not None:
        mins = next((e for e in c.lista if e.estrategia == "contingencia" and e.base_planificacion == "P50"), None)
        robs = next((e for e in c.lista if e.estrategia == "contingencia" and e.base_planificacion == "P90"), None)
        if mins is not None and robs is not None:
            cr = ev.costo_robustez(mins.distribucion, robs.distribucion, red, cens, m["deficit_p90_evitado_kwh"],
                                   SUPUESTOS["prob_desfavorable"], fsa)
            f.update({"recorte_adicional_robustez_kwh": m["recorte_adicional_robustez_kwh"],
                      "deficit_p90_evitado_kwh": m["deficit_p90_evitado_kwh"],
                      "costo_robustez_mxn": cr["costo_mxn"], "beneficio_robustez_mxn": cr["beneficio_esperado_mxn"]})
    # --- consistencia (criterio 2): ENS = recorte + no servido, Σ por carga = total, costo desde series
    res = d.resumen()
    ens_res = sum(x["recorte_kwh"] + x["no_servido_kwh"] for x in res.values())
    f["consistencia_ok"] = bool(abs(f["ens_kwh"] - ens_res) <= 0.1
                                and abs(sum(ev.ens_por_carga(d, red).values()) - f["ens_kwh"]) <= 0.1
                                and abs(f["costo_mxn"] - float(d.total("costo").sum())) <= 0.01)
    f["comando"] = comando(c.esc.id, c.periodo, c.semilla, c.dia, c.perfil, c.mejora)
    return f


# ======================================================= mejoras posteriores
def exportacion(d, red: RedDistribucion) -> dict:
    """M-EXPORT: renovable desperdiciada que podría subir a la subestación y exportarse aguas arriba,
    limitada por la cabecera, el trafo BT y limite_inyeccion_kw (por alimentador y en total)."""
    lim = d.limites
    barra, fuente = np.zeros(24), np.zeros(24)
    for k in d.claves:
        servida = d.demanda[k] - d.recorte[k] - d.no_servido[k]
        R = d.renovable_disponible[k]
        exced = R - np.minimum(R, servida)
        desp = d.renovable_desperdiciada[k]
        env = exced - desp
        cap = min(lim.inyeccion_kw, lim.alimentador_kw[k], lim.transformador_bt_kw[k])
        extra = np.clip(np.minimum(desp, cap - env), 0, None)
        fuente += extra
        barra += extra / (1 + lim.perdidas[k])
    exp = np.minimum(barra, lim.inyeccion_kw)
    usada_fuente = np.where(barra > 1e-9, fuente * exp / np.maximum(barra, 1e-9), 0.0)
    ingreso = float(exp.sum()) * SUPUESTOS["precio_exportacion_mxn_kwh"]
    om = COSTO_RENOVABLE * float(usada_fuente.sum())
    disp = float(d.total("renovable_disponible").sum())
    usada = float(d.total("renovable_usada").sum())
    desp_total = float(d.total("renovable_desperdiciada").sum())
    return {"exportada_kwh": float(exp.sum()), "aprovechada_extra_kwh": float(usada_fuente.sum()),
            "desperdiciada_kwh": desp_total - float(usada_fuente.sum()), "desperdiciada_actual_kwh": desp_total,
            "pct_aprovechada": 100 * (usada + float(usada_fuente.sum())) / disp if disp > 1e-9 else 0.0,
            "pct_aprovechada_actual": 100 * usada / disp if disp > 1e-9 else 0.0,
            "ingreso_mxn": ingreso, "ahorro_neto_mxn": ingreso - om,
            "horas_en_limite": int((barra > lim.inyeccion_kw + 1e-6).sum())}


def plan_alfa(red, ctx, base, alfa: float) -> dict:
    """Plan de recorte calculado sobre la banda P50 + α (P90 − P50) de demanda y P50 + α (P10 − P50)
    de renovables. α = 0 reproduce *-CONT y α = 1 reproduce *-CONT-P90."""
    c = {n: np.clip(ctx.cargas[n] + alfa * (ctx.cargas_p90[n] - ctx.cargas[n]) + base.cargas[n] - ctx.cargas[n],
                    0, None) for n in ctx.cargas}
    r = {n: ctx.renovables[n] + alfa * (ctx.renovables_p10[n] - ctx.renovables[n]) for n in ctx.renovables}
    return asignar(red, c, r, ctx.limites, ctx.tarifas, recortar=True).recorte_por_carga


def robustez(c: Corrida, real: Realidad, red: RedDistribucion) -> dict | None:
    """M-ROBUSTEZ: políticas de recorte en contingencia, evaluadas ex post en la realidad simulada y
    en el mundo desfavorable (P90/P10), con costo esperado = (1 − p) · realidad + p · desfavorable.
      P50     recorte mínimo (política vigente)
      P90     plan robusto (alternativa vigente)
      TOPE    banda α máxima con recorte ≤ tope × déficit central
      MINCOSTO α ∈ {0, 1/8, ..., 1} con menor costo total esperado EX ANTE: mundos de la banda
               (SUPUESTOS["mundos_exante"]: P50 con peso 0.5, mitad de la banda 0.4, P90 0.1)."""
    base = next((e for e in c.lista if e.estrategia == "contingencia" and e.base_planificacion == "P50"), None)
    if base is None or c.ctx is None:
        return None
    ctx, t = c.ctx, red.vector_tarifas()
    cens, fsa = _cens()
    p = SUPUESTOS["prob_desfavorable"]
    deficit_central = base.metricas["deficit_planificado_kwh"]

    def total_rec(plan):
        return float(sum(np.sum(v) for v in plan.values()))

    def evaluar_plan(plan, mundos):
        out = []
        for cargas, ren in mundos:
            dd = aplicar(red, ctx, base, cargas, ren, plan=plan)
            out.append((ev.ens_kwh(dd), ev.costo_energia_mxn(dd, t), ev.costo_social_ens_mxn(dd, red, cens, fsa)))
        return out
    def mundo(a):
        return ({n: ctx.cargas[n] + a * (ctx.cargas_p90[n] - ctx.cargas[n]) for n in ctx.cargas},
                {n: ctx.renovables[n] + a * (ctx.renovables_p10[n] - ctx.renovables[n]) for n in ctx.renovables})
    exante = [mundo(a) for a, _ in SUPUESTOS["mundos_exante"]]
    pesos = [w for _, w in SUPUESTOS["mundos_exante"]]
    expost = [(real.cargas, real.renovables), (ctx.cargas_p90, ctx.renovables_p10)]
    planes = {a: plan_alfa(red, ctx, base, a) for a in np.linspace(0, 1, 9)}
    # TOPE: mayor α con recorte ≤ tope × déficit central (los recortes crecen con α)
    tope = SUPUESTOS["tope_recorte"] * deficit_central
    a_tope = max([a for a, pl in planes.items() if total_rec(pl) <= tope + 0.1] or [0.0])

    def esperado(res):
        return {"ens": (1 - p) * res[0][0] + p * res[1][0], "energia": (1 - p) * res[0][1] + p * res[1][1],
                "social": (1 - p) * res[0][2] + p * res[1][2]}
    costo_exante = {a: sum(w * (x[1] + x[2]) for w, x in zip(pesos, evaluar_plan(pl, exante)))
                    for a, pl in planes.items()}
    a_min = min(costo_exante, key=costo_exante.get)
    out = {"deficit_central_kwh": deficit_central, "alfa_tope": float(a_tope), "alfa_mincosto": float(a_min)}
    for nombre, a in (("P50", 0.0), ("P90", 1.0), ("TOPE", a_tope), ("MINCOSTO", a_min)):
        pl = planes[a]
        res = evaluar_plan(pl, expost)
        e = esperado(res)
        out[nombre] = {"recorte_kwh": total_rec(pl), "ens_real_kwh": res[0][0], "ens_desfavorable_kwh": res[1][0],
                       "ens_no_prevista_real_kwh": max(0.0, res[0][0] - total_rec(pl)),
                       "ens_esperada_kwh": e["ens"], "energia_esperada_mxn": e["energia"],
                       "social_esperado_mxn": e["social"], "total_esperado_mxn": e["energia"] + e["social"]}
    return out


# ============================================================ tareas (procesos)
def _variantes_principales() -> list[tuple[str, str | None]]:
    return [(p, None) for p in PERFILES]


def tarea(args) -> dict:
    """Una clave (escenario, periodo, semilla, día): corre la línea base y sus variantes. Devuelve
    filas de kpis.csv y registros de mejoras (todo serializable)."""
    esc_id, periodo, semilla, dia, variantes, supuestos = args
    SUPUESTOS.update(supuestos)
    esc = ESCENARIOS[esc_id]
    red = red_ejemplo()
    ref = correr(ESCENARIOS[esc.referencia], periodo, semilla, dia) if esc.referencia else None
    base = correr(esc, periodo, semilla, dia)
    fuente = base if base.ctx is not None else ref
    real = simular_realidad(esc, semilla, dias()[dia], fuente.ctx if fuente else None, red)
    filas, mejoras = [], []
    corridas = {("aprueba", None): base}
    for perfil, mejora in variantes:
        if (perfil, mejora) not in corridas:
            corridas[(perfil, mejora)] = correr(esc, periodo, semilla, dia, perfil, mejora)
    clave = {"escenario": esc_id, "familia": esc.familia, "periodo": periodo, "semilla": semilla, "dia": dia}
    for (perfil, mejora), c in corridas.items():
        if (perfil, mejora) not in variantes and (perfil, mejora) != ("aprueba", None):
            continue
        filas.append(kpis(c, real, red, ref))
    # Mejoras posteriores sobre la línea base (perfil aprueba)
    op = base.operacion if base.operacion is not None else (ref.s0 if ref else None)
    if op is not None:
        mejoras.append({**clave, "mejora": "M-EXPORT", **exportacion(op.distribucion, red)})
    rb = robustez(base, real, red)
    if rb is not None:
        mejoras.append({**clave, "mejora": "M-ROBUSTEZ", **rb})
    # Extras para las fallas: ancho de banda y mensajes del escalamiento
    return {"filas": filas, "mejoras": mejoras}


def plan_campana(rapido: bool) -> list[tuple]:
    periodos = ["1m"] if rapido else list(PERIODOS)
    semillas = SUPUESTOS["semillas"]
    tareas = []
    principales = _variantes_principales()
    for esc_id in CAMPANA_PRINCIPAL:
        for periodo in periodos:
            for semilla in semillas:
                for dia in dias():
                    var = principales + [("aprueba", "M-PRON"), ("mas_seguridad", "M-PRON"),
                                         ("mas_seguridad", "M-BANDA"), ("aprueba", "M-BANDA")]
                    if esc_id.startswith("F6"):
                        var.append(("aprueba", "M-DATOS"))
                    tareas.append((esc_id, periodo, semilla, dia, var))
    for e in FALLAS_ESC:
        for periodo in periodos:
            for semilla in semillas:
                for dia in dias():
                    var = [("aprueba", None), ("mas_seguridad", None), ("aprueba", "M-DATOS"),
                           ("aprueba", "M-BANDA"), ("mas_seguridad", "M-BANDA")]
                    tareas.append((e.id, periodo, semilla, dia, var))
    for e in DATOS_ESC:
        for periodo in periodos:
            for semilla in semillas:
                for dia in dias():
                    tareas.append((e.id, periodo, semilla, dia, [("aprueba", None), ("aprueba", "M-DATOS")]))
    for e in EXPORTA_ESC:
        for periodo in periodos:
            for semilla in semillas:
                for dia in dias():
                    tareas.append((e.id, periodo, semilla, dia, [("aprueba", None)]))
    return [t + (dict(SUPUESTOS),) for t in tareas]


def ejecutar_campana(rapido: bool, procesos: int) -> tuple[list[dict], list[dict]]:
    tareas = plan_campana(rapido)
    filas, mejoras = [], []
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=procesos) as ex:
        for i, r in enumerate(ex.map(tarea, tareas, chunksize=4), 1):
            filas += r["filas"]
            mejoras += r["mejoras"]
            if i % 200 == 0 or i == len(tareas):
                print(f"  {i}/{len(tareas)} claves, {len(filas)} corridas, {time.perf_counter() - t0:.0f} s", flush=True)
    return filas, mejoras


# ============================================================ concurrencia
def _analisis_aislado(args) -> float:
    """Un análisis completo con su propia memoria de aprendizaje (proceso independiente)."""
    periodo, semilla = args
    d = tempfile.mkdtemp(prefix="ea_conc_")
    try:
        t0 = time.perf_counter()
        s = crear_sistema(periodo=periodo, condicion="normal", modo="web", semilla=semilla, salida=d,
                          ruta_memoria=os.path.join(d, "memoria.json"), verbose=False)
        s.iniciar()
        return time.perf_counter() - t0
    finally:
        shutil.rmtree(d, ignore_errors=True)


def medir_concurrencia(n: int = 16) -> dict:
    """M-CONCURRENCIA: ráfaga de n análisis (periodos 1m, 6m y 1a) que llegan a la vez.
    servidor: servidor.py tal cual (un análisis a la vez por _motor_lock, memoria compartida).
    P procesos: P análisis en paralelo, cada uno con su memoria (hipotético, sin cambiar servidor.py).
    Latencia de cada pedido = espera en cola + análisis. Se reporta p50, p95 y máximo [s]."""
    import pruebas_adversas as pa
    pedidos = [(("1m", "6m", "1a")[i % 3], 1000 + i) for i in range(n)]
    out = {"pedidos": n, "periodos": "1m, 6m, 1a"}
    with pa.servidor_prueba() as (srv, _):
        pa.pedir(srv, "POST", "/sesiones", {"periodo": "1d"})          # calentamiento

        def pedir(p):
            t0 = time.perf_counter()
            st, _ = pa.pedir(srv, "POST", "/sesiones", {"periodo": p[0], "semilla": p[1]}, timeout=300)
            return st, time.perf_counter() - t0
        with ThreadPoolExecutor(max_workers=n) as ex:
            res = list(ex.map(pedir, pedidos))
        lat = [x for _, x in res]
        out["servidor"] = {"p50_s": ev.percentil(lat, 50), "p95_s": ev.percentil(lat, 95), "max_s": max(lat),
                           "errores": sum(1 for st, _ in res if st != 201)}
        # A7: entradas mal tipadas que hoy dan 500
        api = pa.F7API("test_A7_api_500_con_tipos_inesperados")._adversas(pa.ENTRADAS_DEFECTO)
        out["a7"] = {"entradas": len(api), "respuestas_500": sum(1 for _, st, _, _ in api if st == 500),
                     "detalle": [f"{c[1]} → {st}" for c, st, _, _ in api]}
    for procesos in (1, 2, 4):
        with ProcessPoolExecutor(max_workers=procesos) as ex:
            list(ex.map(_analisis_aislado, [("1d", 1)] * procesos))     # calentamiento de los procesos
            fines: dict[int, float] = {}
            t0 = time.perf_counter()
            futuros = []
            for i, p in enumerate(pedidos):
                fu = ex.submit(_analisis_aislado, p)
                fu.add_done_callback(lambda _f, i=i: fines.__setitem__(i, time.perf_counter()))
                futuros.append(fu)
            for fu in futuros:
                fu.result()
            time.sleep(0.05)               # los callbacks corren en otro hilo
            lat = [fines[i] - t0 for i in range(n)]
        out[f"procesos_{procesos}"] = {"p50_s": ev.percentil(lat, 50), "p95_s": ev.percentil(lat, 95),
                                       "max_s": max(lat)}
    return out


# ================================================================== salida
def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x is not None and np.isfinite(x)


def escribir_csv(ruta: str, filas: list[dict]):
    columnas = []
    for f in filas:
        for k in f:
            if k not in columnas:
                columnas.append(k)
    if "comando" in columnas:
        columnas.remove("comando")
        columnas.append("comando")
    with open(ruta, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columnas)
        w.writeheader()
        for f in filas:
            w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in f.items()})


def aplanar_mejoras(mejoras: list[dict]) -> list[dict]:
    """Registros de M-EXPORT y M-ROBUSTEZ por corrida, con los dicts anidados aplanados."""
    out = []
    for m in mejoras:
        f = {}
        for k, v in m.items():
            if isinstance(v, dict):
                f.update({f"{k}_{kk}": vv for kk, vv in v.items()})
            else:
                f[k] = v
        out.append(f)
    return out


def reproducir(esc_id: str, periodo: str, semilla: int, dia: str, perfil: str, mejora: str | None):
    var = [(perfil, mejora)]
    r = tarea((esc_id, periodo, semilla, dia, var, dict(SUPUESTOS)))
    fila = next(f for f in r["filas"] if f["perfil"] == perfil and (f["mejora"] or None) == mejora)
    print(f"[{esc_id} | {periodo} | semilla={semilla} | día {dia} ({SUPUESTOS['dias'][dia]}) | perfil {perfil}"
          + (f" | mejora {mejora}" if mejora else "") + "]")
    for k, v in fila.items():
        print(f"  {k:<34} {round(v, 3) if isinstance(v, float) else v}")
    if mejora is None and perfil == "aprueba":
        for m in aplanar_mejoras(r["mejoras"]):
            print(f"  -- {m['mejora']}")
            for k, v in m.items():
                if k not in ("escenario", "familia", "periodo", "semilla", "dia", "mejora"):
                    print(f"     {k:<34} {round(v, 3) if isinstance(v, float) else v}")
    return 0


# ================================================================== informe
# Paleta categórica validada (dataviz: validate_palette.js, modo claro). Cada gráfica va acompañada
# de una tabla con los mismos números en INFORME.md (los tonos 3 y 4 tienen contraste < 3:1).
COLORES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
TINTA, TINTA2, MUDO, REJILLA, EJE, SUPERFICIE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
PERIODO_TABLAS = "1m"

FALLAS_INFO = {
    "A1": dict(sev="alta", titulo="Una falla en B, G o H tumba al supervisor", ejes="continuidad, costo",
               regla="Regla 1 (sin plan la red opera como S0 con déficit sin aviso) y Regla 4; mecanismo 5 del "
                     "bus (canal de excepciones); ISO/IEC 5338 (registro) e ISO/IEC 23894 (riesgo operativo)",
               causa="agentes.py · AgenteMeta._manejar_falla reenvía self.ultima_solicitud[destino], que solo "
                     "tiene D y E → KeyError en el supervisor",
               comando="A1.falla-B-trafo55"),
    "A2": dict(sev="media", titulo="Un medidor sin lecturas rompe el flujo", ejes="continuidad, costo",
               regla="Regla 1; ISO/IEC 5338 (monitoreo de datos de entrada)",
               causa="agentes.py · CapturaIoT.procesar interpola con np.interp sin comprobar que haya lecturas "
                     "válidas; la FAILURE de B termina en A1", comando="A2.sensor-fuera-trafo55"),
    "A3": dict(sev="media", titulo="Demanda nula: división entre cero y diagnóstico falso", ejes="eficiencia (servicio de decisión)",
               regla="ISO/IEC 5338 (trazabilidad: motivo registrado incorrecto); Regla 4",
               causa="agentes.py · AgenteDeliberativo._metricas divide desplazado / demanda_original_kwh sin "
                     "protección", comando="F2.cero-total"),
    "A4": dict(sev="media", titulo="El escalamiento no dice que falta energía para el hospital", ejes="continuidad, costo",
               regla="Regla 1 (prioridad de cargas críticas); ISO/IEC 42005 (grupo de interés más sensible)",
               causa="agentes.py · AgenteMeta._decidir escala con MOTIVO_SIN_MEDIDAS sin consultar "
                     "no_servido_por_tipo_kwh['critica']", comando="F1.hospital-imposible"),
    "A5": dict(sev="media", titulo="La cobertura de bandas ignora las renovables", ejes="continuidad, costo",
               regla="Regla 1 y 2; ISO/IEC 5338 (monitoreo) e ISO/IEC 23894 (riesgo de pronóstico renovable)",
               causa="agentes.py · AgenteAprendizaje._cobertura recorre solo el consumo; las bandas de "
                     "_residuales_renovable nunca se validan", comando="A5.cobertura-trafo55"),
    "A6": dict(sev="media", titulo="Con 1 día de historial la banda es demasiado estrecha", ejes="continuidad, eficiencia",
               regla="ISO/IEC 23894 (estimación de la incertidumbre), ISO/IEC 5338",
               causa="agentes.py · AgenteAprendizaje._residuales con un día solo mide ruido intradía (±2 h)",
               comando="CAT.falla_transformador --periodo 1d --perfil mas_seguridad"),
    "A7": dict(sev="baja", titulo="La API responde 500 ante tipos inválidos", ejes="continuidad (servicio de decisión)",
               regla="ISO/IEC 5338 (operación); robustez de la interfaz", causa="servidor.py · crear_sesion y "
               "Manejador._leer_json no validan tipos ni Content-Length", comando=None),
    "A8": dict(sev="alta", titulo="Lecturas negativas → pronóstico negativo (historial corto)", ejes="continuidad, eficiencia, costo",
               regla="Regla 1 (subestima la demanda); ISO/IEC 23894 (calidad de datos)",
               causa="agentes.py · AgenteAprendizaje._referencia (mediana ±2 h negativa con < 3 días) y la "
                     "limpieza copia la referencia negativa", comando="F6.negativas-trafo55 --periodo 1d"),
    "A9": dict(sev="media", titulo="Sin detector de sensor congelado", ejes="continuidad, eficiencia, costo",
               regla="Regla 1; ISO/IEC 5338 (calidad de los datos de entrada)",
               causa="agentes.py · AgenteAprendizaje.procesar solo detecta desviaciones (z robusto)",
               comando="F6.congelado-trafo55 --periodo 1s"),
    "A10": dict(sev="media", titulo="El pronóstico ignora el tipo de día (fin de semana)", ejes="continuidad, eficiencia, costo",
                regla="Regla 1 y política de recorte mínimo (recorte sobre demanda inflada); Regla 3; "
                      "ISO/IEC 5338 (alerta de deriva falsa), ISO/IEC 23894",
                causa="agentes.py · AgenteAprendizaje._pronostico: promedio ponderado de 7 días sin tipo de día",
                comando="CAT.falla_transformador --dia finde"),
    "B1": dict(sev="alta", titulo="Sesgo negativo del pronóstico en día laboral sin monitoreo (nueva)",
               ejes="continuidad, eficiencia, costo",
               regla="Regla 1 (el plan 'sin déficit' deja ENS no prevista y sin aviso); ISO/IEC 23894; ISO/IEC 5338 "
                     "(el MAPE de validación no mide sesgo)",
               causa="agentes.py · AgenteAprendizaje._pronostico (la ventana de 7 días incluye sábado y domingo con "
                     "pesos 3 y 4 de 28) y la validación del MAPE en procesar(), que no separa sesgo de ruido",
               comando="CAT.falla_transformador --dia laboral"),
    "B2": dict(sev="media", titulo="Escalamiento engañoso tras rechazar el recorte mínimo (nueva)",
               ejes="continuidad, costo",
               regla="Regla 4 (validación humana informada); Regla 1; ISO/IEC 42005 (impacto no comunicado), "
                     "ISO/IEC 23894",
               causa="agentes.py · AgenteMeta._decidir: tras un rechazo sin pedir seguridad filtra a recortes "
                     "menores que el rechazado (ninguno, ya era el mínimo) y escala con MOTIVO_SIN_MEDIDAS",
               comando="CAT.falla_transformador --perfil recortar_menos"),
    "B3": dict(sev="baja", titulo="Impacto al operador sin igualar la energía servida (nueva)", ejes="costo",
               regla="Regla 3 (comparación de costos); ISO/IEC 42005 (impacto al operador mal etiquetado)",
               causa="agentes.py · EvaluadorISO._impactos compara costo_mxn contra S0 sin dividir entre la energía "
                     "servida", comando="CAT.falla_transformador"),
}

MEJORAS_INFO = {
    "M-PRON": dict(desc="Pronóstico por tipo de día (laboral / fin de semana) con residuales y validación del mismo tipo",
                   horas=6, riesgo="bajo: cambia solo _pronostico/_residuales; requiere ≥ 1 día del mismo tipo",
                   archivos="agentes.py (AgenteAprendizaje._pronostico, _residuales, MAPE y _cobertura), pruebas.py",
                   reglas="Reglas 1 y 3; ISO/IEC 23894, ISO/IEC 5338", cierra="A10, B1"),
    "M-EXPORT": dict(desc="Exportar el excedente renovable aguas arriba hasta limite_inyeccion_kw",
                     horas=12, riesgo="medio: cambia el balance de distribucion._paso, el costo y las métricas; "
                     "requiere precio y acuerdo de exportación",
                     archivos="distribucion.py (_paso, asignar, costo), agentes.py (_metricas), modelo_red.py, web/app",
                     reglas="Reglas 2 y 3; ISO/IEC 42005 (medio ambiente)", cierra="hallazgo de desperdicio renovable"),
    "M-BANDA": dict(desc="Piso de incertidumbre con < 7 días de historial y validación fuera de muestra de las bandas renovables",
                    horas=4, riesgo="bajo en código; medio en operación (más recorte en el plan P90)",
                    archivos="agentes.py (AgenteAprendizaje._banda/_residuales, _cobertura)",
                    reglas="Regla 1; ISO/IEC 23894, ISO/IEC 5338", cierra="A5, A6"),
    "M-ROBUSTEZ": dict(desc="Tope al recorte robusto (≤ 2 × déficit central) o elección por costo total mínimo "
                            "(energía + costo social de la ENS)",
                       horas=8, riesgo="medio: cambia la política de recorte; el CENS debe validarlo el operador",
                       archivos="agentes.py (AgenteDeliberativo._contingencia, AgenteMeta._decidir)",
                       reglas="Reglas 1, 3 y 4; ISO/IEC 23894, ISO/IEC 42005", cierra="hallazgo de recorte robusto excesivo"),
    "M-DATOS": dict(desc="Datos limpios en B y D: negativas como faltantes, sensor congelado, medidor vacío fuera de servicio "
                         "con perfil típico",
                    horas=6, riesgo="bajo: preprocesamiento aislado; umbrales a calibrar con datos reales",
                    archivos="agentes.py (CapturaIoT.procesar, AgenteAprendizaje.procesar)",
                    reglas="Regla 1; ISO/IEC 5338, ISO/IEC 23894", cierra="A2, A8, A9"),
    "M-CONCURRENCIA": dict(desc="Análisis en paralelo (2 y 4 procesos) con memoria de aprendizaje por sesión",
                           horas=16, riesgo="alto: concurrencia, aislamiento de sesiones y memoria compartida",
                           archivos="servidor.py (_motor_lock, crear_sesion), motor.py (ruta de memoria)",
                           reglas="ISO/IEC 5338 (operación)", cierra="hallazgo: el servidor procesa un análisis a la vez"),
    "M-SUPERVISOR": dict(desc="Corrección de A1: si no hay solicitud que repetir, el supervisor escala en lugar de "
                              "lanzar KeyError (medido como el flujo sano del mismo escenario)",
                         horas=1, riesgo="bajo: una rama en _manejar_falla", archivos="agentes.py (AgenteMeta._manejar_falla)",
                         reglas="Reglas 1 y 4; ISO/IEC 5338, ISO/IEC 23894", cierra="A1"),
}

INDICADORES = [
    # (eje, indicador, fórmula, unidades, fuente)
    ("Continuidad", "ENS", "Σ_h Σ_k (recorte + no_servido)", "kWh/día", "evaluacion.ens_kwh · Distribucion.recorte, no_servido"),
    ("Continuidad", "ENS por alimentador / carga / grupo", "Σ_h (recorte + no_servido) del elemento; grupos de ISO/IEC 42005",
     "kWh/día", "ens_por_alimentador, ens_por_carga, ens_por_grupo"),
    ("Continuidad", "% de la demanda servida", "100 × (1 − ENS / demanda)", "%", "pct_servida, pct_servida_por_alimentador"),
    ("Continuidad", "ENS crítica (hospital)", "ENS de las cargas tipo crítica (debe ser 0)", "kWh/día", "ens_critica_kwh"),
    ("Continuidad", "SAIDI / SAIFI / CAIDI diarios", "Σ horas afectadas / N cargas; Σ eventos / N cargas; horas / eventos "
     "(afectada: ENS > 0.05 kW)", "h/carga·día; eventos/carga·día; h/evento", "afectacion"),
    ("Continuidad", "Horas con déficit en P90", "#h con no_servido > 0.05 kW en Escenario.distribucion_p90", "h/día", "horas_deficit"),
    ("Continuidad", "ENS no prevista", "no_servido(plan aplicado a la realidad) − no_servido(plan en P50), ≥ 0", "kWh/día",
     "ens_no_prevista_kwh · evaluar.aplicar"),
    ("Continuidad", "Sobre-recorte", "recorte aplicado en la realidad − ENS mínima con información perfecta, ≥ 0", "kWh/día",
     "evaluar.kpis (asignar con recortar=True sobre la realidad)"),
    ("Continuidad", "Servicio de decisión", "% de corridas que terminan en propuesta, escalamiento o flujo roto; mensajes",
     "%, mensajes/análisis", "resultado_decision, coordinacion"),
    ("Eficiencia", "% renovable", "100 × renovable usada / demanda", "%", "renovable()['pct_demanda']"),
    ("Eficiencia", "Renovable aprovechada / desperdiciada", "Σ renovable_usada; Σ renovable_desperdiciada; % sobre la disponible",
     "kWh/día, %", "renovable"),
    ("Eficiencia", "Pérdidas técnicas", "Σ pérdidas; 100 × pérdidas / (renovable usada + convencional)", "kWh/día, %", "perdidas"),
    ("Eficiencia", "Utilización", "flujo / capacidad de cabecera, trafo BT y subestación (máx y media de 24 h)", "%", "utilizacion"),
    ("Eficiencia", "Factor de carga y reducción del pico", "promedio / pico de la importación; 100 × (pico S0 − pico) / pico S0",
     "adim., %", "factor_carga, reduccion_pico_pct"),
    ("Eficiencia", "Desplazado por déficit evitado", "kWh desplazados / (ENS S0 − ENS)", "kWh/kWh", "desplazado_por_deficit_evitado"),
    ("Eficiencia", "Acciones pedidas al operador", "bloques de movimiento + cargas recortadas", "acciones", "n_acciones"),
    ("Eficiencia", "MAPE (validación y real)", "100 × promedio |pronóstico − real| / real", "%", "mape_pct"),
    ("Eficiencia", "Sesgo del pronóstico", "100 × (Σ pronóstico − Σ real) / Σ real", "%", "sesgo_pct"),
    ("Eficiencia", "Cobertura de bandas (demanda y renovable)", "lecturas reales dentro de [P10, P90] / lecturas (sin horas < 1 kW)",
     "%", "cobertura"),
    ("Eficiencia", "Ancho relativo de banda", "100 × promedio (P90 − P10) / P50", "%", "ancho_relativo_pct"),
    ("Eficiencia", "Costo de coordinación y mensajes", "Σ pesos de aristas del grafo A–H; mensajes enviados", "puntos, mensajes",
     "coordinacion · BusCoordinacion"),
    ("Eficiencia", "Tiempo por análisis y p95 de la API", "segundos de s.iniciar() + respuestas; percentil 95 de las latencias", "s",
     "evaluar.correr, medir_concurrencia, percentil"),
    ("Costo", "Costo de la energía", "Σ importación_h × tarifa_h + 0.30 × Σ renovable usada", "MXN/día", "costo_energia_mxn"),
    ("Costo", "Costo por kWh servido", "costo de la energía / (demanda − ENS)", "MXN/kWh", "costo_por_kwh_servido"),
    ("Costo", "Ahorro y su descomposición", "C_S0 − C = efecto tarifa + renovable + pérdidas + servicio (exacta)", "MXN/día, %",
     "descomponer_ahorro"),
    ("Costo", "Costo social de la ENS", "Σ_c (recorte_c + factor_sin_aviso × no_servido_c) × CENS(tipo_c)", "MXN/día",
     "costo_social_ens_mxn"),
    ("Costo", "Costo total", "costo de la energía + costo social de la ENS", "MXN/día", "costo_total_mxn"),
    ("Costo", "Costo de la robustez", "(recorte P90 − recorte P50) × CENS contra p × déficit P90 evitado × CENS", "MXN/día",
     "costo_robustez"),
]


def _vals(rows, k):
    out = []
    for f in rows:
        v = f.get(k)
        if isinstance(v, bool):
            out.append(float(v))
        elif _num(v):
            out.append(float(v))
    return out


def media(rows, k):
    v = _vals(rows, k)
    return float(np.mean(v)) if v else None


def fm(x, nd=0, signo=False):
    if x is None:
        return "—"
    x = 0.0 if abs(x) < 0.5 * 10 ** -nd else x
    return f"{x:+,.{nd}f}" if signo else f"{x:,.{nd}f}"


def mi(rows, k, nd=0):
    """Media [P10–P90] entre las corridas (entre semillas cuando la selección fija lo demás)."""
    v = _vals(rows, k)
    if not v:
        return "—"
    m, a, b = float(np.mean(v)), float(np.percentile(v, 10)), float(np.percentile(v, 90))
    return fm(m, nd) if b - a < 10 ** -nd else f"{fm(m, nd)} [{fm(a, nd)}–{fm(b, nd)}]"


def sel(rows, **kw):
    return [f for f in rows if all(f.get(k) == v for k, v in kw.items())]


def tabla(cab, filas_) -> str:
    out = ["| " + " | ".join(cab) + " |", "|" + "|".join("---" for _ in cab) + "|"]
    out += ["| " + " | ".join(str(c) for c in f) + " |" for f in filas_]
    return "\n".join(out)


def _pares(F, mejora, perfil, filtro=lambda f: True):
    """(línea base, variante) con la misma clave y perfil."""
    clave = lambda f: (f["escenario"], f["periodo"], f["semilla"], f["dia"], f["perfil"])   # noqa: E731
    base = {clave(f): f for f in F if not f["mejora"] and f["perfil"] == perfil and filtro(f)}
    return [(base[clave(f)], f) for f in F if f["mejora"] == mejora and f["perfil"] == perfil and clave(f) in base]


def _delta(pares, k):
    v = [m[k] - b[k] for b, m in pares if _num(b.get(k)) and _num(m.get(k))]
    return float(np.mean(v)) if v else None


def _media_par(pares, k, lado):
    return media([p[lado] for p in pares], k)


def _con_deficit(f):
    return _num(f.get("ens_s0_kwh")) and f["ens_s0_kwh"] > 0.05


# ------------------------------------------------------------------ gráficas
def _estilo(ax, titulo, xlab, ylab):
    ax.set_facecolor(SUPERFICIE)
    ax.set_title(titulo, color=TINTA, fontsize=11, loc="left")
    ax.set_xlabel(xlab, color=TINTA2, fontsize=9)
    ax.set_ylabel(ylab, color=TINTA2, fontsize=9)
    ax.tick_params(colors=TINTA2, labelsize=8)
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    for lado in ("left", "bottom"):
        ax.spines[lado].set_color(EJE)
    ax.grid(axis="x", color=REJILLA, linewidth=0.6)
    ax.set_axisbelow(True)


def _barras_h(ax, etiquetas, series, nombres, log=False, nd=0):
    n = len(series)
    alto = 0.8 / n
    y = np.arange(len(etiquetas))
    for i, (vals, nom) in enumerate(zip(series, nombres)):
        pos = y + (i - (n - 1) / 2) * alto
        v = [0 if x is None else x for x in vals]
        ax.barh(pos, [(x if x >= 1 else np.nan) if log else x for x in v], height=alto * 0.86, color=COLORES[i],
                label=nom, edgecolor=SUPERFICIE, linewidth=1)
        for p_, x in zip(pos, v):
            if (log and x < 1) or x == 0:
                ax.text(1.05 if log else 0, p_, f" {x:.0f}", va="center", fontsize=6, color=MUDO)
    ax.set_yticks(y)
    ax.set_yticklabels(etiquetas)
    ax.invert_yaxis()
    if log:
        ax.set_xscale("log")
        ax.set_xlim(left=1)
    ax.legend(fontsize=8, frameon=False, loc="upper center", bbox_to_anchor=(0.4, -0.07), ncol=n)


def graficas(destino, F, MJ) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.facecolor": SUPERFICIE})
    out = {}
    B = [f for f in F if f["perfil"] == "aprueba" and not f["mejora"]]
    esc = CAMPANA_PRINCIPAL
    et = [e.replace("CAT.", "") for e in esc]
    lab = [sel(B, escenario=e, periodo=PERIODO_TABLAS, dia="laboral") for e in esc]

    fig, ax = plt.subplots(figsize=(9, 7))
    _barras_h(ax, et, [[media(r, "ens_s0_kwh") for r in lab], [media(r, "ens_kwh") for r in lab],
                       [media(r, "ens_real_kwh") for r in lab]],
              ["S0 (sin cambios), planificado", "Propuesta aprobada, planificado", "Propuesta en la realidad simulada"],
              log=True)
    _estilo(ax, f"ENS por condición: S0 contra propuesta ({PERIODO_TABLAS}, día laboral, media de 5 semillas)",
            "Energía no suministrada (kWh/día, escala logarítmica)", "")
    fig.tight_layout()
    out["ens"] = "ens_por_condicion.png"
    fig.savefig(os.path.join(destino, out["ens"]), dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 7))
    _barras_h(ax, et, [[media(r, "costo_kwh_s0") for r in lab], [media(r, "costo_kwh") for r in lab]],
              ["S0 (sin cambios)", "Propuesta aprobada"])
    _estilo(ax, f"Costo por kWh servido por condición ({PERIODO_TABLAS}, día laboral)", "MXN por kWh servido", "")
    fig.tight_layout()
    out["costo"] = "costo_kwh_servido.png"
    fig.savefig(os.path.join(destino, out["costo"]), dpi=130)
    plt.close(fig)

    # Renovable aprovechada contra desperdiciada (incluye los casos de excedente de M-EXPORT)
    esc_r = esc + [e.id for e in EXPORTA_ESC]
    ex = [m for m in MJ if m["mejora"] == "M-EXPORT" and m["periodo"] == PERIODO_TABLAS and m["dia"] == "laboral"]
    usada, recup, perdida = [], [], []
    for e in esc_r:
        r = sel(B, escenario=e, periodo=PERIODO_TABLAS, dia="laboral")
        x = [m for m in ex if m["escenario"] == e]
        usada.append(media(r, "renov_usada_kwh") or 0)
        desp = media(r, "renov_desperdiciada_kwh") or 0
        rec = min(media(x, "aprovechada_extra_kwh") or 0, desp)
        recup.append(rec)
        perdida.append(max(desp - rec, 0))
    fig, ax = plt.subplots(figsize=(9, 7.5))
    y = np.arange(len(esc_r))
    izq = np.zeros(len(esc_r))
    for i, (vals, nom) in enumerate(zip([usada, recup, perdida], ["Aprovechada (hoy)", "Desperdiciada, recuperable "
                                         "exportando (M-EXPORT)", "Desperdiciada sin recuperar"])):
        ax.barh(y, vals, left=izq, height=0.7, color=COLORES[i], label=nom, edgecolor=SUPERFICIE, linewidth=1.5)
        izq += np.array(vals)
    ax.set_yticks(y)
    ax.set_yticklabels([e.replace("CAT.", "") for e in esc_r])
    ax.invert_yaxis()
    ax.legend(fontsize=8, frameon=False, loc="upper center", bbox_to_anchor=(0.4, -0.07), ncol=3)
    _estilo(ax, f"Renovable aprovechada contra desperdiciada ({PERIODO_TABLAS}, día laboral)", "kWh/día", "")
    fig.tight_layout()
    out["renovable"] = "renovable_aprovechada.png"
    fig.savefig(os.path.join(destino, out["renovable"]), dpi=130)
    plt.close(fig)

    # MAPE laboral contra fin de semana, actual y con M-PRON
    per = [p for p in PERIODOS if sel(B, periodo=p)]
    cat = [f for f in F if f["familia"] == "CAT" and f["perfil"] == "aprueba"]
    series, nombres = [], []
    for mej, et_m in (("", "actual"), ("M-PRON", "con M-PRON")):
        for dia, et_d in (("laboral", "laboral"), ("finde", "fin de semana")):
            series.append([media(sel(cat, periodo=p, dia=dia, mejora=mej), "mape_real_pct") for p in per])
            nombres.append(f"{et_d}, {et_m}")
    fig, ax = plt.subplots(figsize=(8, 4.5))
    x = np.arange(len(per))
    for i, (vals, nom) in enumerate(zip(series, nombres)):
        ax.bar(x + (i - 1.5) * 0.2, [v or 0 for v in vals], width=0.18, color=COLORES[i], label=nom,
               edgecolor=SUPERFICIE, linewidth=1)
    ax.set_xticks(x)
    ax.set_xticklabels([NOMBRE_PERIODO_CORTO.get(p, p) for p in per])
    ax.axhline(15, color=MUDO, linewidth=1, linestyle="--")
    ax.text(len(per) - 0.5, 15.4, "umbral de reentrenamiento (15 %)", fontsize=7, color=MUDO, ha="right", va="bottom")
    _estilo(ax, "MAPE del pronóstico contra el día real: laboral y fin de semana", "Periodo de historial",
            "MAPE (%)")
    ax.grid(axis="y", color=REJILLA, linewidth=0.6)
    ax.grid(axis="x", visible=False)
    ax.set_ylim(0, 18)
    ax.legend(fontsize=8, frameon=False, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.14))
    fig.tight_layout()
    out["mape"] = "mape_tipo_dia.png"
    fig.savefig(os.path.join(destino, out["mape"]), dpi=130)
    plt.close(fig)

    # Frontera costo-continuidad (M-ROBUSTEZ)
    rb = [m for m in MJ if m["mejora"] == "M-ROBUSTEZ" and m["escenario"] in esc and m["periodo"] == PERIODO_TABLAS]
    fig, ax = plt.subplots(figsize=(8, 5.5))
    pol = ["P50", "TOPE", "MINCOSTO", "P90"]
    nom_pol = {"P50": "P50 (recorte mínimo, vigente)", "TOPE": "Tope 2 × déficit central", "MINCOSTO": "Costo total mínimo",
               "P90": "P90 (robusto, alternativa vigente)"}
    escs = sorted({m["escenario"] for m in rb})
    for e in escs:
        r = [m for m in rb if m["escenario"] == e]
        xs = [media(r, f"{p}_ens_esperada_kwh") for p in pol]
        ys = [media(r, f"{p}_total_esperado_mxn") for p in pol]
        ax.plot(xs, ys, color=EJE, linewidth=1, zorder=1)
        ax.text(xs[-1], ys[-1], "  " + e.replace("CAT.", ""), fontsize=6.5, color=TINTA2, va="center")
    for i, p in enumerate(pol):
        xs = [media([m for m in rb if m["escenario"] == e], f"{p}_ens_esperada_kwh") for e in escs]
        ys = [media([m for m in rb if m["escenario"] == e], f"{p}_total_esperado_mxn") for e in escs]
        if p == "P50":     # anillo grande: suele coincidir con el costo mínimo y no debe quedar oculto
            ax.scatter(xs, ys, s=110, facecolors="none", edgecolors=COLORES[i], linewidths=2, label=nom_pol[p], zorder=3)
        else:
            ax.scatter(xs, ys, s=36, color=COLORES[i], label=nom_pol[p], zorder=2, edgecolors=SUPERFICIE,
                       linewidths=1.5, marker={"TOPE": "s", "MINCOSTO": "o", "P90": "D"}[p])
    if escs:
        ax.set_xscale("log")
        ax.set_yscale("log")
    _estilo(ax, f"Frontera costo-continuidad en contingencia ({PERIODO_TABLAS}, ambos días)",
            "ENS esperada (kWh/día, log)", "Costo total esperado: energía + costo social (MXN/día, log)")
    ax.grid(axis="y", color=REJILLA, linewidth=0.6)
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    fig.tight_layout()
    out["frontera"] = "frontera_costo_continuidad.png"
    fig.savefig(os.path.join(destino, out["frontera"]), dpi=130)
    plt.close(fig)
    return out


def grafica_matriz(destino, filas_m) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 5))
    puntos: dict = {}
    for m in filas_m:          # mejoras con el mismo esfuerzo y puntaje comparten punto y etiqueta
        puntos.setdefault((m["horas"], m["puntaje"]), []).append(m["id"])
    for i, ((h, pt), ids) in enumerate(sorted(puntos.items())):
        ax.scatter(h, pt, s=60, color=COLORES[0], edgecolors=SUPERFICIE, linewidths=1.5, zorder=2)
        ax.annotate(" / ".join(ids), (h, pt), xytext=(6, 4 if i % 2 else -10), textcoords="offset points",
                    fontsize=8, color=TINTA)
    ax.axvline(8, color=EJE, linewidth=1, linestyle="--")
    ax.axhline(3, color=EJE, linewidth=1, linestyle="--")
    ax.text(0.5, 5.25, "ganancia rápida", fontsize=8, color=MUDO)
    ax.text(17.5, 5.25, "proyecto estructural", fontsize=8, color=MUDO, ha="right")
    ax.set_xlim(0, 18)
    ax.set_ylim(0.5, 5.5)
    _estilo(ax, "Matriz impacto × esfuerzo de las mejoras", "Esfuerzo estimado (horas)",
            "Impacto (puntaje 1–5, ver tabla)")
    ax.grid(axis="y", color=REJILLA, linewidth=0.6)
    fig.tight_layout()
    fig.savefig(os.path.join(destino, "matriz_impacto_esfuerzo.png"), dpi=130)
    plt.close(fig)
    return "matriz_impacto_esfuerzo.png"


NOMBRE_PERIODO_CORTO = {"1d": "1 día", "1s": "1 semana", "1m": "1 mes", "6m": "6 meses", "1a": "1 año"}


# ------------------------------------------------------------ cuantificación
def cuantificar_fallas(F, conc) -> dict:
    B = [f for f in F if f["perfil"] == "aprueba" and not f["mejora"]]
    out = {}
    cens = SUPUESTOS["cens_mxn_kwh"]
    clave = lambda f: (f["periodo"], f["semilla"], f["dia"])   # noqa: E731
    ref_trafo = {clave(f): f for f in sel(B, escenario="CAT.falla_transformador")}

    a1 = [f for f in B if f["familia"] == "A1"]
    ref = [ref_trafo[clave(f)] for f in a1 if clave(f) in ref_trafo]
    out["A1"] = {
        "roto_pct": 100 * len(sel(a1, resultado="roto")) / max(len(a1), 1), "n": len(a1),
        "ens_sin_plan": media(a1, "ens_kwh"), "ens_con_plan": media(ref, "ens_kwh"),
        "ens_real_sin_plan": media(a1, "ens_real_kwh"), "ens_real_con_plan": media(ref, "ens_real_kwh"),
        "costo_total_sin": media(a1, "costo_total_real_mxn"), "costo_total_con": media(ref, "costo_total_real_mxn"),
        "mensajes": media(a1, "mensajes"), "mensajes_ref": media(ref, "mensajes"),
        "costo_coord": media(a1, "costo_coordinacion")}
    a2 = [f for f in B if f["familia"] == "A2"]
    a2m = _pares(F, "M-DATOS", "aprueba", lambda f: f["familia"] == "A2")
    out["A2"] = {"roto_pct": 100 * len(sel(a2, resultado="roto")) / max(len(a2), 1), "n": len(a2),
                 "propuesta_con_datos_pct": 100 * sum(m["resultado"] == "propuesta" for _, m in a2m) / max(len(a2m), 1),
                 "ens_sin_plan": media(a2, "ens_kwh"), "costo_total_sin": media(a2, "costo_total_real_mxn"),
                 "costo_total_con": _media_par(a2m, "costo_total_real_mxn", 1),
                 "ens_real_sin": media(a2, "ens_real_kwh"), "ens_real_con": _media_par(a2m, "ens_real_kwh", 1)}
    a3 = sel(B, escenario="F2.cero-total")
    norm = sel(B, escenario="CAT.normal")
    out["A3"] = {"escalamiento_pct": 100 * len(sel(a3, resultado="escalamiento")) / max(len(a3), 1), "n": len(a3),
                 "motivo": next((f["motivo_escalamiento"] for f in a3 if f.get("motivo_escalamiento")), ""),
                 "mensajes": media(a3, "mensajes"), "mensajes_normal": media(norm, "mensajes"),
                 "costo_coord": media(a3, "costo_coordinacion"), "costo_coord_normal": media(norm, "costo_coordinacion"),
                 "acciones_pedidas": 1}
    a4 = sel(B, escenario="F1.hospital-imposible")
    out["A4"] = {"n": len(a4), "escalamiento_pct": 100 * len(sel(a4, resultado="escalamiento")) / max(len(a4), 1),
                 "ens_critica": media(a4, "ens_critica_kwh"), "ens_critica_real": media(a4, "ens_critica_real_kwh"),
                 "menciona_pct": 100 * (media(a4, "escalamiento_menciona_critica") or 0),
                 "costo_social_hospital": (media(a4, "ens_critica_kwh") or 0) * cens["critica"] * SUPUESTOS["factor_sin_aviso"],
                 "costo_social_total": media(a4, "costo_social_mxn"), "ens": media(a4, "ens_kwh")}
    a5 = sel(B, escenario="A5.cobertura-trafo55")
    a5s = sel([f for f in F if not f["mejora"]], escenario="A5.cobertura-trafo55", perfil="mas_seguridad")
    a5b = _pares(F, "M-BANDA", "mas_seguridad", lambda f: f["escenario"] == "A5.cobertura-trafo55" and f["periodo"] != "1d")
    f4 = sel(B, escenario="F4.optimista-trafo55")
    out["A5"] = {"n": len(a5),
                 "ens_np_lab": media(sel(a5, dia="laboral"), "ens_no_prevista_kwh"),
                 "ens_np_fin": media(sel(a5, dia="finde"), "ens_no_prevista_kwh"),
                 "ens_np_p90": media(a5s, "ens_no_prevista_kwh"),
                 "cob_valid": media([f for f in a5 if f["periodo"] != "1d"], "cobertura_validacion_pct"),
                 "cob_ren_real": media(a5, "cobertura_ren_real_pct"), "cob_dem_real": media(a5, "cobertura_dem_real_pct"),
                 "alerta_cal_pct": 100 * (media([f for f in a5 if f["periodo"] != "1d"], "alerta_calibracion") or 0),
                 "costo_social_np": (media(a5, "costo_social_real_mxn") or 0) - (media(a5, "costo_social_mxn") or 0),
                 "f4_np_lab": media(sel(f4, dia="laboral"), "ens_no_prevista_kwh"),
                 "f4_np_fin": media(sel(f4, dia="finde"), "ens_no_prevista_kwh"),
                 "banda_np_p90": _media_par(a5b, "ens_no_prevista_kwh", 1), "banda_np_p90_base": _media_par(a5b, "ens_no_prevista_kwh", 0),
                 "banda_recorte": _delta(a5b, "ens_kwh")}
    cont = [f for f in F if f["escenario"] in CAMPANA_PRINCIPAL and _con_deficit(f) and not f["mejora"]]
    cat_b = [f for f in B if f["familia"] == "CAT"]
    seg = lambda p: sel([f for f in cont if f["perfil"] == "mas_seguridad"], periodo=p)   # noqa: E731
    pb = _pares(F, "M-BANDA", "mas_seguridad", lambda f: f["escenario"] in CAMPANA_PRINCIPAL and _con_deficit(f)
                and f["periodo"] == "1d")
    out["A6"] = {"ancho_1d": media(sel(cat_b, periodo="1d"), "ancho_dem_pct"),
                 "ancho_1m": media(sel(cat_b, periodo="1m"), "ancho_dem_pct"),
                 "cob_1d": media(sel(cat_b, periodo="1d"), "cobertura_dem_real_pct"),
                 "cob_1m": media(sel(cat_b, periodo="1m"), "cobertura_dem_real_pct"),
                 "np_p90_1d": media(seg("1d"), "ens_no_prevista_kwh"), "np_p90_1m": media(seg("1m"), "ens_no_prevista_kwh"),
                 "costo_np_1d": (media(seg("1d"), "costo_social_real_mxn") or 0) - (media(seg("1d"), "costo_social_mxn") or 0),
                 "banda_np": _media_par(pb, "ens_no_prevista_kwh", 1), "banda_recorte": _delta(pb, "ens_kwh"),
                 "banda_costo": _delta(pb, "costo_total_real_mxn"), "n_1d": len(seg("1d"))}
    out["A7"] = dict(conc.get("a7", {}))
    neg = lambda e, p: _pares(F, "M-DATOS", "aprueba", lambda f: f["escenario"] == e and f["periodo"] in p)   # noqa: E731
    p8, p8t = neg("F6.negativas", ("1d", "1s")), neg("F6.negativas-trafo55", ("1d", "1s"))
    out["A8"] = {"pron_min": _media_par(p8, "pronostico_min_kw", 0), "pron_min_datos": _media_par(p8, "pronostico_min_kw", 1),
                 "pct_negativo": 100 * sum(b["pronostico_min_kw"] < 0 for b, _ in p8 if _num(b.get("pronostico_min_kw")))
                 / max(len(p8), 1),
                 "mape": _media_par(p8, "mape_real_pct", 0), "mape_datos": _media_par(p8, "mape_real_pct", 1),
                 "sesgo": _media_par(p8, "sesgo_real_pct", 0), "sesgo_datos": _media_par(p8, "sesgo_real_pct", 1),
                 "np": _media_par(p8t, "ens_no_prevista_kwh", 0), "np_datos": _media_par(p8t, "ens_no_prevista_kwh", 1),
                 "costo": _delta(p8t, "costo_total_real_mxn"), "n": len(p8) + len(p8t)}
    p9, p9t = neg("F6.congelado", ("1d", "1s")), neg("F6.congelado-trafo55", ("1d", "1s"))
    p9a = neg("F6.congelado-trafo55", tuple(PERIODOS))
    out["A9"] = {"mape": _media_par(p9, "mape_real_pct", 0), "mape_datos": _media_par(p9, "mape_real_pct", 1),
                 "sesgo": _media_par(p9, "sesgo_real_pct", 0), "sesgo_datos": _media_par(p9, "sesgo_real_pct", 1),
                 "np": _media_par(p9t, "ens_no_prevista_kwh", 0), "np_datos": _media_par(p9t, "ens_no_prevista_kwh", 1),
                 "costo": _delta(p9t, "costo_total_real_mxn"), "costo_todos": _delta(p9a, "costo_total_real_mxn"),
                 "n": len(p9) + len(p9t)}
    fin_cat = [f for f in cat_b if f["periodo"] != "1d"]
    # A10 y B1 se miden en las contingencias del CATÁLOGO (sin F4/F5, que suman A5 y déficit extremo)
    cont_b = [f for f in cont if f["perfil"] == "aprueba" and f["familia"] == "CAT" and f.get("modo") == "contingencia"]
    es_cont = lambda f: f["familia"] == "CAT" and f.get("modo") == "contingencia"   # noqa: E731
    pr_fin = _pares(F, "M-PRON", "aprueba", lambda f: es_cont(f) and f["dia"] == "finde" and f["periodo"] != "1d")
    pr_lab = _pares(F, "M-PRON", "aprueba", lambda f: es_cont(f) and f["dia"] == "laboral" and f["periodo"] != "1d")
    cf = [f for f in cont_b if f["dia"] == "finde" and f["periodo"] != "1d"]
    out["A10"] = {"mape_fin": media(sel(fin_cat, dia="finde"), "mape_real_pct"),
                  "mape_lab": media(sel(fin_cat, dia="laboral"), "mape_real_pct"),
                  "mape_val_fin": media(sel(fin_cat, dia="finde"), "mape_validacion_pct"),
                  "mape_val_lab": media(sel(fin_cat, dia="laboral"), "mape_validacion_pct"),
                  "sesgo_fin": media(sel(fin_cat, dia="finde"), "sesgo_real_pct"),
                  "alerta_fin": 100 * (media(sel(fin_cat, dia="finde"), "alerta_deriva") or 0),
                  "alerta_lab": 100 * (media(sel(fin_cat, dia="laboral"), "alerta_deriva") or 0),
                  "sobre_recorte": media([f for f in cont_b if f["dia"] == "finde" and f["periodo"] != "1d"], "sobre_recorte_kwh"),
                  "ens_real_plan": media(cf, "ens_real_kwh"), "ens_real_s0": media(cf, "ens_real_s0_kwh"),
                  "ens_min": media(cf, "ens_minima_real_kwh"), "n_cf": len(cf),
                  "ahorro_pron": -(_delta(pr_fin, "costo_total_real_mxn") or 0),
                  "riesgo_fin": _media_par(pr_fin, "riesgo_max", 0), "riesgo_fin_pron": _media_par(pr_fin, "riesgo_max", 1)}
    lab_c = [f for f in cont_b if f["dia"] == "laboral" and f["periodo"] != "1d"]
    out["B1"] = {"sesgo_lab": media(sel(fin_cat, dia="laboral"), "sesgo_real_pct"),
                 "mape_lab": media(sel(fin_cat, dia="laboral"), "mape_real_pct"),
                 "mape_val_lab": media(sel(fin_cat, dia="laboral"), "mape_validacion_pct"),
                 "alerta_lab": 100 * (media(sel(fin_cat, dia="laboral"), "alerta_deriva") or 0),
                 "np": media(lab_c, "ens_no_prevista_kwh"), "np_pron": _media_par(pr_lab, "ens_no_prevista_kwh", 1),
                 "np_base_par": _media_par(pr_lab, "ens_no_prevista_kwh", 0),
                 "costo_social_np": (media(lab_c, "costo_social_real_mxn") or 0) - (media(lab_c, "costo_social_mxn") or 0),
                 "ahorro_pron": -(_delta(pr_lab, "costo_total_real_mxn") or 0), "n": len(lab_c)}
    rm = [f for f in F if f["escenario"] in CAMPANA_PRINCIPAL and not f["mejora"] and f["perfil"] == "recortar_menos"]
    rm_cont = [f for f in rm if f.get("propuesta_inicial", "-").endswith("CONT")]
    esc_rm = sel(rm_cont, resultado="escalamiento")
    ap = {(f["escenario"],) + clave(f): f for f in B}
    pares_rm = [(ap[(f["escenario"],) + clave(f)], f) for f in esc_rm if (f["escenario"],) + clave(f) in ap]
    out["B2"] = {"n_cont": len(rm_cont), "escalamiento_pct": 100 * len(esc_rm) / max(len(rm_cont), 1),
                 "escalamiento_total_pct": 100 * len(sel(rm, resultado="escalamiento")) / max(len(rm), 1),
                 "motivo": next((f["motivo_escalamiento"] for f in esc_rm if f.get("motivo_escalamiento")), ""),
                 "ens_real": _delta(pares_rm, "ens_real_kwh"), "ens_no_prevista_sin_aviso": _media_par(pares_rm, "no_servido_kwh", 1),
                 "costo_total": _delta(pares_rm, "costo_total_real_mxn"),
                 "costo_total_plan": _delta(pares_rm, "costo_total_mxn")}
    cc = [f for f in B if f.get("modo") == "contingencia"]
    neg_ = [f for f in cc if f.get("impacto_operador") == "negativo"]
    out["B3"] = {"n": len(cc), "negativo_pct": 100 * len(neg_) / max(len(cc), 1),
                 "negativo_mas_barato_pct": 100 * sum(f["costo_kwh"] < f["costo_kwh_s0"] for f in neg_) / max(len(cc), 1),
                 "sobrecosto": -(media(neg_, "ahorro_mxn") or 0), "efecto_servicio": media(neg_, "efecto_servicio_mxn"),
                 "kwh_s0": media(neg_, "costo_kwh_s0"), "kwh": media(neg_, "costo_kwh")}
    return out


def cuantificar_mejoras(F, MJ, conc) -> dict:
    out = {}
    main = lambda f: f["escenario"] in CAMPANA_PRINCIPAL   # noqa: E731
    p_all = _pares(F, "M-PRON", "aprueba", main)
    p_def = [p for p in p_all if _con_deficit(p[0])]
    p_seg = _pares(F, "M-PRON", "mas_seguridad", lambda f: main(f) and _con_deficit(f))
    d = {}
    for dia in ("laboral", "finde"):
        pa = [p for p in p_all if p[0]["dia"] == dia and p[0]["periodo"] != "1d"]
        pd_ = [p for p in p_def if p[0]["dia"] == dia and p[0]["familia"] == "CAT" and p[0].get("modo") == "contingencia"
               and p[0]["periodo"] != "1d"]
        d[dia] = {"mape": _media_par(pa, "mape_real_pct", 0), "mape_m": _media_par(pa, "mape_real_pct", 1),
                  "sesgo": _media_par(pa, "sesgo_real_pct", 0), "sesgo_m": _media_par(pa, "sesgo_real_pct", 1),
                  "cob": _media_par(pa, "cobertura_dem_real_pct", 0), "cob_m": _media_par(pa, "cobertura_dem_real_pct", 1),
                  "ancho": _media_par(pa, "ancho_dem_pct", 0), "ancho_m": _media_par(pa, "ancho_dem_pct", 1),
                  "riesgo": _media_par(pa, "riesgo_max", 0), "riesgo_m": _media_par(pa, "riesgo_max", 1),
                  "alerta": 100 * (_media_par(pa, "alerta_deriva", 0) or 0),
                  "alerta_m": 100 * (_media_par(pa, "alerta_deriva", 1) or 0),
                  "np": _media_par(pd_, "ens_no_prevista_kwh", 0), "np_m": _media_par(pd_, "ens_no_prevista_kwh", 1),
                  "sobre": _media_par(pd_, "sobre_recorte_kwh", 0), "sobre_m": _media_par(pd_, "sobre_recorte_kwh", 1),
                  "ens_real": _media_par(pd_, "ens_real_kwh", 0), "ens_real_m": _media_par(pd_, "ens_real_kwh", 1),
                  "costo": -(_delta(pd_, "costo_total_real_mxn") or 0)}
    out["M-PRON"] = {"dias": d, "beneficio_dia_deficit": -(_delta(p_def, "costo_total_real_mxn") or 0),
                     "beneficio_todos": -(_delta(p_all, "costo_total_real_mxn") or 0),
                     "energia": -(_delta(p_all, "costo_mxn") or 0),
                     "recorte_p90": _delta(p_seg, "ens_kwh"), "np_p90": _delta(p_seg, "ens_no_prevista_kwh"),
                     "costo_p90": -(_delta(p_seg, "costo_total_real_mxn") or 0)}
    ex = [m for m in MJ if m["mejora"] == "M-EXPORT"]
    exd = [m for m in ex if m.get("desperdiciada_actual_kwh", 0) > 1]
    out["M-EXPORT"] = {"n": len(exd), "desp": media(exd, "desperdiciada_actual_kwh"), "desp_m": media(exd, "desperdiciada_kwh"),
                       "exportada": media(exd, "exportada_kwh"), "aprov": media(exd, "pct_aprovechada_actual"),
                       "aprov_m": media(exd, "pct_aprovechada"), "ingreso": media(exd, "ingreso_mxn"),
                       "neto": media(exd, "ahorro_neto_mxn"), "horas_limite": media(exd, "horas_en_limite"),
                       "max_desp": max(_vals(exd, "desperdiciada_actual_kwh") or [0]),
                       "pct_corridas": 100 * len(exd) / max(len(ex), 1),
                       "principal_desp": max(_vals([m for m in ex if m["escenario"] in CAMPANA_PRINCIPAL],
                                                   "desperdiciada_actual_kwh") or [0])}
    pb_seg = _pares(F, "M-BANDA", "mas_seguridad", lambda f: (main(f) and _con_deficit(f)) or f["familia"] == "A5")
    pb_1d = [p for p in pb_seg if p[0]["periodo"] == "1d" and p[0]["familia"] != "A5"]
    pb_a5 = [p for p in pb_seg if p[0]["familia"] == "A5" and p[0]["periodo"] != "1d"]
    pb_ap = _pares(F, "M-BANDA", "aprueba", lambda f: f["familia"] == "A5" and f["periodo"] != "1d")
    out["M-BANDA"] = {"np_1d": _media_par(pb_1d, "ens_no_prevista_kwh", 0), "np_1d_m": _media_par(pb_1d, "ens_no_prevista_kwh", 1),
                      "recorte_1d": _delta(pb_1d, "ens_kwh"), "costo_1d": -(_delta(pb_1d, "costo_total_real_mxn") or 0),
                      "np_a5": _media_par(pb_a5, "ens_no_prevista_kwh", 0), "np_a5_m": _media_par(pb_a5, "ens_no_prevista_kwh", 1),
                      "recorte_a5": _delta(pb_a5, "ens_kwh"), "costo_a5": -(_delta(pb_a5, "costo_total_real_mxn") or 0),
                      "np_a5_p50": _media_par(pb_ap, "ens_no_prevista_kwh", 1),
                      "beneficio": -(_delta(pb_seg, "costo_total_real_mxn") or 0), "n": len(pb_seg)}
    rb = [m for m in MJ if m["mejora"] == "M-ROBUSTEZ" and m["escenario"] in CAMPANA_PRINCIPAL]
    r = {p: {k: media(rb, f"{p}_{k}") for k in ("recorte_kwh", "ens_no_prevista_real_kwh", "ens_esperada_kwh",
                                                "energia_esperada_mxn", "social_esperado_mxn", "total_esperado_mxn")}
         for p in ("P50", "TOPE", "MINCOSTO", "P90")}
    out["M-ROBUSTEZ"] = {"n": len(rb), "politicas": r, "alfa_tope": media(rb, "alfa_tope"), "alfa_min": media(rb, "alfa_mincosto"),
                         "mincosto_p50_pct": 100 * sum(m["alfa_mincosto"] == 0 for m in rb) / max(len(rb), 1),
                         "beneficio_vs_p90": (r["P90"]["total_esperado_mxn"] or 0) - (r["TOPE"]["total_esperado_mxn"] or 0),
                         "beneficio_vs_p50": (r["P50"]["total_esperado_mxn"] or 0) - (r["MINCOSTO"]["total_esperado_mxn"] or 0)}
    pdat = _pares(F, "M-DATOS", "aprueba", lambda f: f["escenario"].startswith("F6") or f["familia"] == "A2")
    out["M-DATOS"] = {"n": len(pdat),
                      "flujo": 100 * sum(b["resultado"] != "roto" for b, _ in pdat) / max(len(pdat), 1),
                      "flujo_m": 100 * sum(m["resultado"] != "roto" for _, m in pdat) / max(len(pdat), 1),
                      "mape": _media_par(pdat, "mape_real_pct", 0), "mape_m": _media_par(pdat, "mape_real_pct", 1),
                      "np": _media_par(pdat, "ens_no_prevista_kwh", 0), "np_m": _media_par(pdat, "ens_no_prevista_kwh", 1),
                      "ens_real": _media_par(pdat, "ens_real_kwh", 0), "ens_real_m": _media_par(pdat, "ens_real_kwh", 1),
                      "beneficio": -(_delta(pdat, "costo_total_real_mxn") or 0),
                      "f6_ens": media([b for b, _ in pdat if b["escenario"].startswith("F6")], "ens_real_kwh"),
                      "f6_ens_m": media([m for b, m in pdat if b["escenario"].startswith("F6")], "ens_real_kwh"),
                      "normal_sin_cambio": _delta([p for p in pdat if p[0]["escenario"] == "CAT.normal"], "costo_mxn")}
    out["M-CONCURRENCIA"] = {k: conc.get(k) for k in ("servidor", "procesos_1", "procesos_2", "procesos_4", "pedidos")}
    B = [f for f in F if f["perfil"] == "aprueba" and not f["mejora"]]
    clave = lambda f: (f["periodo"], f["semilla"], f["dia"])   # noqa: E731
    ref = {clave(f): f for f in sel(B, escenario="CAT.falla_transformador")}
    psup = [(f, ref[clave(f)]) for f in B if f["familia"] == "A1" and clave(f) in ref]
    out["M-SUPERVISOR"] = {"n": len(psup), "flujo": 0.0, "flujo_m": 100.0,
                           "ens_real": _media_par(psup, "ens_real_kwh", 0), "ens_real_m": _media_par(psup, "ens_real_kwh", 1),
                           "beneficio": -(_delta(psup, "costo_total_real_mxn") or 0)}
    return out


def _puntaje(mxn_dia: float | None, servicio: bool = False) -> int:
    """Puntaje de impacto 1–5 por el beneficio esperado (MXN/día) donde la mejora aplica:
    5 ≥ 100 000; 4 ≥ 10 000; 3 ≥ 1 000; 2 > 0 o mejora solo de servicio; 1 sin beneficio o negativo."""
    if mxn_dia is None or mxn_dia <= 0:
        return 2 if servicio else 1
    return 5 if mxn_dia >= 1e5 else 4 if mxn_dia >= 1e4 else 3 if mxn_dia >= 1e3 else 2


def filas_mejoras_csv(F, MJ, conc) -> list[dict]:
    """mejoras.csv: una fila por mejora × escenario (× día) con deltas = variante − actual."""
    out = []
    ks = [("ens_kwh", "delta_ens_plan_kwh"), ("ens_real_kwh", "delta_ens_real_kwh"),
          ("ens_no_prevista_kwh", "delta_ens_no_prevista_kwh"), ("sobre_recorte_kwh", "delta_sobre_recorte_kwh"),
          ("mape_real_pct", "delta_mape_real_pct"), ("sesgo_real_pct", "delta_sesgo_pct"),
          ("cobertura_dem_real_pct", "delta_cobertura_dem_pct"), ("ancho_dem_pct", "delta_ancho_dem_pct"),
          ("riesgo_max", "delta_riesgo_max"), ("alerta_deriva", "delta_alerta_deriva"), ("costo_mxn", "delta_costo_energia_mxn"),
          ("costo_total_real_mxn", "delta_costo_total_real_mxn")]
    for mejora, perfiles in (("M-PRON", ("aprueba", "mas_seguridad")), ("M-BANDA", ("aprueba", "mas_seguridad")),
                             ("M-DATOS", ("aprueba",))):
        for perfil in perfiles:
            pares = _pares(F, mejora, perfil)
            grupos: dict = {}
            for b, m in pares:
                grupos.setdefault((b["escenario"], b["dia"]), []).append((b, m))
            for (e, dia), pp in sorted(grupos.items()):
                f = {"mejora": mejora, "escenario": e, "dia": dia, "perfil": perfil, "corridas": len(pp)}
                for k, nom in ks:
                    f[nom] = _delta(pp, k)
                f["delta_flujo_completo_pct"] = 100 * (sum(m["resultado"] != "roto" for _, m in pp)
                                                       - sum(b["resultado"] != "roto" for b, _ in pp)) / len(pp)
                f["comando"] = comando(e, "1m", SUPUESTOS["semillas"][0], dia, perfil, mejora)
                out.append(f)
    grupos = {}
    for m in MJ:
        if m["mejora"] == "M-EXPORT":
            grupos.setdefault((m["escenario"], m["dia"]), []).append(m)
    for (e, dia), mm in sorted(grupos.items()):
        out.append({"mejora": "M-EXPORT", "escenario": e, "dia": dia, "perfil": "aprueba", "corridas": len(mm),
                    "delta_renov_desperdiciada_kwh": (media(mm, "desperdiciada_kwh") or 0) - (media(mm, "desperdiciada_actual_kwh") or 0),
                    "delta_pct_renov_aprovechada": (media(mm, "pct_aprovechada") or 0) - (media(mm, "pct_aprovechada_actual") or 0),
                    "exportada_kwh": media(mm, "exportada_kwh"), "ingreso_mxn": media(mm, "ingreso_mxn"),
                    "delta_costo_energia_mxn": -(media(mm, "ahorro_neto_mxn") or 0),
                    "delta_costo_total_real_mxn": -(media(mm, "ahorro_neto_mxn") or 0),
                    "comando": comando(e, "1m", SUPUESTOS["semillas"][0], dia)})
    grupos = {}
    for m in MJ:
        if m["mejora"] == "M-ROBUSTEZ":
            grupos.setdefault((m["escenario"], m["dia"]), []).append(m)
    for (e, dia), mm in sorted(grupos.items()):
        for pol, refp in (("TOPE", "P90"), ("MINCOSTO", "P50"), ("P90", "P50")):
            out.append({"mejora": f"M-ROBUSTEZ {pol} vs {refp}", "escenario": e, "dia": dia, "perfil": "-", "corridas": len(mm),
                        "delta_ens_plan_kwh": (media(mm, f"{pol}_recorte_kwh") or 0) - (media(mm, f"{refp}_recorte_kwh") or 0),
                        "delta_ens_no_prevista_kwh": (media(mm, f"{pol}_ens_no_prevista_real_kwh") or 0)
                        - (media(mm, f"{refp}_ens_no_prevista_real_kwh") or 0),
                        "delta_ens_esperada_kwh": (media(mm, f"{pol}_ens_esperada_kwh") or 0) - (media(mm, f"{refp}_ens_esperada_kwh") or 0),
                        "delta_costo_energia_mxn": (media(mm, f"{pol}_energia_esperada_mxn") or 0)
                        - (media(mm, f"{refp}_energia_esperada_mxn") or 0),
                        "delta_costo_total_real_mxn": (media(mm, f"{pol}_total_esperado_mxn") or 0)
                        - (media(mm, f"{refp}_total_esperado_mxn") or 0),
                        "comando": comando(e, "1m", SUPUESTOS["semillas"][0], dia)})
    B = [f for f in F if f["perfil"] == "aprueba" and not f["mejora"]]
    clave = lambda f: (f["periodo"], f["semilla"], f["dia"])   # noqa: E731
    ref = {clave(f): f for f in sel(B, escenario="CAT.falla_transformador")}
    grupos = {}
    for f in B:
        if f["familia"] == "A1" and clave(f) in ref:
            grupos.setdefault((f["escenario"], f["dia"]), []).append((f, ref[clave(f)]))
    for (e, dia), pp in sorted(grupos.items()):
        f = {"mejora": "M-SUPERVISOR", "escenario": e, "dia": dia, "perfil": "aprueba", "corridas": len(pp)}
        for k, nom in ks:
            f[nom] = _delta(pp, k)
        f["delta_flujo_completo_pct"] = 100.0
        f["comando"] = comando(e, "1m", SUPUESTOS["semillas"][0], dia)
        out.append(f)
    s, p2, p4 = conc.get("servidor") or {}, conc.get("procesos_2") or {}, conc.get("procesos_4") or {}
    for nom, x in (("2 procesos", p2), ("4 procesos", p4)):
        out.append({"mejora": "M-CONCURRENCIA", "escenario": f"ráfaga de {conc.get('pedidos')} análisis, {nom}", "dia": "-",
                    "perfil": "-", "corridas": conc.get("pedidos"),
                    "delta_p95_api_s": (x.get("p95_s") or 0) - (s.get("p95_s") or 0),
                    "delta_p50_api_s": (x.get("p50_s") or 0) - (s.get("p50_s") or 0), "comando": "python evaluar.py"})
    return out


# -------------------------------------------------------------- informe .md
def generar_informe(destino, F, MJ, conc, rapido=False, segundos_campana=0.0, segundos=0.0) -> dict:
    B = [f for f in F if f["perfil"] == "aprueba" and not f["mejora"]]
    P = PERIODO_TABLAS
    main = CAMPANA_PRINCIPAL
    BM = [f for f in B if f["escenario"] in main]
    fallas = cuantificar_fallas(F, conc)
    mej = cuantificar_mejoras(F, MJ, conc)
    imgs = graficas(destino, F, MJ)
    escribir_csv(os.path.join(destino, "mejoras.csv"), filas_mejoras_csv(F, MJ, conc))
    mxn = lambda x: fm(x, 0)   # noqa: E731

    # ---- cifras del dictamen
    dfc = [f for f in BM if _con_deficit(f) and f["familia"] == "CAT"]
    sin = [f for f in BM if not _con_deficit(f) and _num(f.get("ens_s0_kwh"))]
    ens_s0, ens_p = media(dfc, "ens_kwh"), None
    ens_s0, ens_p = media(dfc, "ens_s0_kwh"), media(dfc, "ens_kwh")
    red_ens = 100 * (1 - ens_p / ens_s0) if ens_s0 else 0.0
    crit = sum(1 for f in BM if f["resultado"] == "propuesta" and (f.get("ens_critica_kwh") or 0) > 0.05)
    n_prop = len(sel(BM, resultado="propuesta"))
    ahorro_sin = media(sin, "ahorro_mxn")
    ahorro_sin_pct = media(sin, "ahorro_pct")
    ct_s0, ct_p = media(dfc, "costo_total_s0_mxn"), media(dfc, "costo_total_mxn")
    red_ct = 100 * (1 - ct_p / ct_s0) if ct_s0 else 0.0
    ctr_s0, ctr_p = media(dfc, "costo_total_real_s0_mxn"), media(dfc, "costo_total_real_mxn")
    b1, a10 = fallas["B1"], fallas["A10"]
    dictamen = {
        "continuidad": {"cifra": f"ENS no prevista de {fm(b1['np'])} kWh/día con el plan aprobado en las contingencias "
                                 f"del catálogo en día laboral",
                        "reduccion_ens_planificada_pct": red_ens, "ens_s0_kwh": ens_s0, "ens_propuesta_kwh": ens_p,
                        "propuestas_con_ens_critica": crit, "propuestas": n_prop, "ens_no_prevista_laboral_kwh": b1["np"]},
        "eficiencia": {"cifra": f"MAPE real {fm(a10['mape_fin'], 1)} % en fin de semana contra {fm(a10['mape_lab'], 1)} % "
                                f"entre semana, con sesgo {fm(b1['sesgo_lab'], 1, True)} % en día laboral",
                       "mape_finde_pct": a10["mape_fin"], "mape_laboral_pct": a10["mape_lab"], "sesgo_laboral_pct": b1["sesgo_lab"]},
        "costo": {"cifra": f"ahorro de {fm(ahorro_sin_pct, 1)} % ({mxn(ahorro_sin)} MXN/día) sin déficit; costo total "
                           f"{fm(red_ct, 1)} % menor que S0 en el catálogo con déficit (planificado)",
                  "ahorro_sin_deficit_mxn": ahorro_sin, "ahorro_sin_deficit_pct": ahorro_sin_pct,
                  "reduccion_costo_total_pct": red_ct, "costo_total_real_s0_mxn": ctr_s0, "costo_total_real_mxn": ctr_p},
    }
    L = []
    w = L.append
    w("# Evaluación de resultados de ElectriAgent: continuidad, eficiencia y costo\n")
    w(f"Campaña {'RÁPIDA (solo 1m)' if rapido else 'completa'} generada por `python evaluar.py"
      f"{' --rapido' if rapido else ''}` el {datetime.now().date()} sobre la rama `evaluacion-resultados` (base "
      f"`main`). Regla: **evaluar, no corregir**: no se modificó la lógica de los agentes, la distribución, la "
      f"API, la web ni la app. Las mejoras son escenarios hipotéticos que viven solo en `evaluar.py`.\n")
    w(f"{len(F):,} corridas del motor ({len(B):,} de línea base con el perfil «aprueba»), {len(MJ):,} evaluaciones "
      f"posteriores (exportación y robustez) y una ráfaga de {conc.get('pedidos')} pedidos a la API. Campaña "
      f"{segundos_campana:.0f} s, total {segundos:.0f} s. Máquina: {platform.platform()}, {os.cpu_count()} núcleos "
      f"lógicos, Python {platform.python_version()}, numpy {np.__version__}.\n")
    inconsist = sum(1 for f in F if f.get("consistencia_ok") is False)
    w(f"Consistencia (criterio 2): en las {sum(1 for f in F if 'consistencia_ok' in f):,} corridas con plan, "
      f"ENS = recorte + no servido de la Distribucion (±0.1 kWh), Σ ENS por carga = total y costo recalculado "
      f"desde las series = Σ Distribucion.costo: **{inconsist} discrepancias**.\n")
    w("```powershell\npython evaluar.py                 # campaña completa y este informe (< 15 min)\n"
      "python evaluar.py --rapido        # solo 1m (< 2 min), en docs/evaluacion/rapido/\n"
      "python evaluar.py --corrida CAT.falla_transformador --periodo 1m --semilla 7 --dia laboral --perfil aprueba\n"
      "python evaluar.py --supuestos mis_supuestos.json   # otros CENS, precios, días o semillas\n"
      "python pruebas_evaluacion.py      # pruebas de los indicadores (casos calculados a mano)\n```\n")

    # 1. Dictamen
    w("## 1. Dictamen\n")
    w(f"ElectriAgent **cumple en el plan, pero su pronóstico lo deja corto en la realidad**. Con sus propios "
      f"datos, en las condiciones del catálogo con déficit la propuesta aprobada reduce la ENS planificada un {fm(red_ens, 1)} % "
      f"frente a operar sin cambios (de {fm(ens_s0)} a {fm(ens_p)} kWh/día), nunca recorta al hospital "
      f"({crit} de {n_prop} propuestas con ENS crítica) y baja el costo total (energía + costo social) un "
      f"{fm(red_ct, 1)} %. En costo, los días sin déficit ahorra **{fm(ahorro_sin_pct, 1)} % ({mxn(ahorro_sin)} "
      f"MXN/día)**, todo por desplazamiento tarifario. El punto débil es la **eficiencia del pronóstico**, que "
      f"ignora el tipo de día: **MAPE real de {fm(a10['mape_fin'], 1)} % en fin de semana** contra "
      f"{fm(a10['mape_lab'], 1)} % entre semana, con un sesgo de {fm(b1['sesgo_lab'], 1, True)} % los días "
      f"laborales que el MAPE no delata. Ese sesgo se convierte en un problema de **continuidad**: cuando el plan se "
      f"aplica al día real, entre semana quedan **{fm(b1['np'])} kWh/día de ENS no prevista y sin aviso** (B1) y "
      f"en fin de semana se recortan {fm(a10['sobre_recorte'])} kWh/día de más (A10). Además, una falla en B, G o H "
      f"(A1) o un medidor vacío (A2) dejan al operador sin plan. Prioridad: corregir A1 y los datos (horas) y "
      f"cambiar el pronosticador (M-PRON), que por sí solo recupera {mxn(mej['M-PRON']['beneficio_dia_deficit'])} "
      f"MXN/día de costo total en los días con déficit.\n")
    w(tabla(["Eje", "Cifra clave", "Lectura"], [
        ["Continuidad", dictamen["continuidad"]["cifra"], "regular: el plan protege al hospital pero subestima la demanda"],
        ["Eficiencia", dictamen["eficiencia"]["cifra"], "deficiente en el pronóstico; buena en uso de renovables"],
        ["Costo", dictamen["costo"]["cifra"], "buena: ahorro tarifario estable"]]) + "\n")

    # 2. Supuestos
    w("## 2. Supuestos\n")
    w("Valores **ilustrativos**. Se cambian en `SUPUESTOS` de `evaluar.py` o con `--supuestos archivo.json`; las "
      "tarifas y capacidades son las del modelo (`modelo_red.py`) y no se modifican.\n")
    cens = SUPUESTOS["cens_mxn_kwh"]
    t = RedDistribucion.TARIFAS
    w(tabla(["Supuesto", "Valor", "Uso"], [
        ["CENS residencial / comercial / industrial / servicio", " / ".join(fm(cens[k]) for k in ("residencial", "comercial",
                                                                                                 "industrial", "servicio"))
         + " MXN/kWh", "costo social de la ENS por tipo de carga"],
        ["CENS crítica (hospital)", f"{fm(cens['critica'])} MXN/kWh", "mucho más alto: Regla 1"],
        ["Factor sin aviso", f"× {SUPUESTOS['factor_sin_aviso']}", "el déficit no planificado cuesta más que el recorte avisado"],
        ["Tarifas (modelo_red.py)", f"base {t['base']} (0-5 h) · intermedia {t['intermedia']} · punta {t['punta']} (18-21 h) MXN/kWh",
         "costo de la energía"],
        ["Costo renovable (modelo_red.py)", f"{COSTO_RENOVABLE} MXN/kWh", "operación y mantenimiento"],
        ["Precio de exportación", f"{SUPUESTOS['precio_exportacion_mxn_kwh']} MXN/kWh", "M-EXPORT"],
        ["Probabilidad del escenario desfavorable", f"{SUPUESTOS['prob_desfavorable']}", "costo esperado de la robustez"],
        ["Mundos ex ante (α, peso)", ", ".join(f"({a}, {p})" for a, p in SUPUESTOS["mundos_exante"]), "M-ROBUSTEZ costo mínimo"],
        ["Tope de recorte robusto", f"{SUPUESTOS['tope_recorte']} × déficit central", "M-ROBUSTEZ tope"],
        ["Semiancho mínimo de banda", f"±{SUPUESTOS['semiancho_minimo'] * 100:.0f} % con < {SUPUESTOS['dias_historial_corto']} días",
         "M-BANDA"],
        ["Día recomendado", f"laboral {SUPUESTOS['dias']['laboral']} (jueves); fin de semana {SUPUESTOS['dias']['finde']} (domingo)",
         "misma estacionalidad, aísla A10"],
        ["Semillas", ", ".join(map(str, SUPUESTOS["semillas"])), "5 por combinación; media y P10–P90 entre semillas"],
        ["Realidad", f"simulador IoT con semilla + {SUPUESTOS['desfase_semilla_realidad']:,}, sin anomalías ni pérdidas, "
                     "con la condición aplicada", "ENS no prevista, MAPE y cobertura reales"],
        ["Sin plan (escalamiento o flujo roto)", "la red opera como S0 (sin cambios)", "supuesto conservador"],
        ["Memoria de aprendizaje", "nueva en cada corrida", "determinismo"],
        ["Perfiles del operador", "aprueba; rechaza una vez «" + PERFILES["recortar_menos"] + "»; rechaza una vez «"
         + PERFILES["mas_seguridad"] + "»", "modo web: responder()"],
    ]) + "\n")

    # 3. Indicadores
    w("## 3. Indicadores\n")
    w("Todos están en `evaluacion.py` (funciones puras) y tienen una prueba con un caso calculado a mano en "
      "`pruebas_evaluacion.py`. kWh/día: las series son de 24 h en kW con paso de 1 h.\n")
    w(tabla(["Eje", "Indicador", "Fórmula", "Unidades", "Fuente"], [list(x) for x in INDICADORES]) + "\n")

    # 4. Resultados
    w("## 4. Resultados por eje\n")
    w(f"Tablas por condición: periodo {P}, perfil «aprueba», **media [P10–P90 entre las 5 semillas]**. «S0» es la "
      "operación sin cambios; «propuesta» la aprobada (o S0 si no hay plan). «Plan» es la Distribucion de E con "
      "el pronóstico P50; «real» es el mismo plan aplicado al día simulado.\n")
    w("### 4.1 Continuidad\n")
    filas_t = []
    for e in main:
        r = sel(B, escenario=e, periodo=P, dia="laboral")
        filas_t.append([e, mi(r, "ens_s0_kwh"), mi(r, "ens_kwh"), f"{fm(media(r, 'pct_servida_s0'), 1)} → {fm(media(r, 'pct_servida'), 1)}",
                        mi(r, "ens_critica_kwh"), f"{fm(media(r, 'saidi_s0_h'), 1)} → {fm(media(r, 'saidi_h'), 1)}",
                        mi(r, "ens_real_s0_kwh"), mi(r, "ens_real_kwh"), mi(r, "ens_no_prevista_kwh")])
    w(f"**Día laboral ({SUPUESTOS['dias']['laboral']})**, kWh/día salvo indicación:\n")
    w(tabla(["Condición", "ENS S0", "ENS propuesta", "% servida S0 → prop.", "ENS hospital", "SAIDI S0 → prop. (h/carga)",
             "ENS real S0", "ENS real propuesta", "ENS no prevista"], filas_t) + "\n")
    w(f"![ENS por condición]({imgs['ens']})\n")
    filas_t = []
    for e in main:
        r = sel(B, escenario=e, periodo=P, dia="finde")
        if not _con_deficit({"ens_s0_kwh": media(r, "ens_s0_kwh")}):
            continue
        filas_t.append([e, mi(r, "ens_s0_kwh"), mi(r, "ens_kwh"), mi(r, "ens_real_s0_kwh"), mi(r, "ens_real_kwh"),
                        mi(r, "ens_minima_real_kwh"), mi(r, "sobre_recorte_kwh"), mi(r, "ens_no_prevista_kwh")])
    w(f"**Fin de semana ({SUPUESTOS['dias']['finde']})**, condiciones con déficit en S0. «ENS mínima real»: la que "
      "bastaba con información perfecta:\n")
    w(tabla(["Condición", "ENS S0", "ENS propuesta", "ENS real S0", "ENS real propuesta", "ENS mínima real",
             "Sobre-recorte", "ENS no prevista"], filas_t) + "\n")
    w("Por periodo (condiciones con déficit en S0, perfil «aprueba», media de condiciones y semillas):\n")
    filas_t = []
    for p in PERIODOS:
        for dia in ("laboral", "finde"):
            r = [f for f in BM if f["periodo"] == p and f["dia"] == dia and _con_deficit(f)]
            if r:
                filas_t.append([p, dia, fm(media(r, "ens_s0_kwh")), fm(media(r, "ens_kwh")), fm(media(r, "ens_real_kwh")),
                                fm(media(r, "ens_no_prevista_kwh")), fm(media(r, "sobre_recorte_kwh")),
                                fm(media(r, "horas_deficit_p90"), 1), fm(media(r, "saifi"), 2), fm(media(r, "caidi_h"), 1)])
    w(tabla(["Periodo", "Día", "ENS S0", "ENS propuesta", "ENS real", "ENS no prevista", "Sobre-recorte",
             "Horas con déficit P90", "SAIFI (eventos/carga)", "CAIDI (h/evento)"], filas_t) + "\n")
    filas_t = []
    for e in main:
        r = sel(B, escenario=e, periodo=P, dia="laboral")
        if not _con_deficit({"ens_s0_kwh": media(r, "ens_s0_kwh")}):
            continue
        filas_t.append([e, fm(media(r, "ens_A1_kwh")), fm(media(r, "ens_A2_kwh")), fm(media(r, "pct_servida_A1"), 1),
                        fm(media(r, "pct_servida_A2"), 1), fm(media(r, "ens_res_com_kwh")), fm(media(r, "ens_ind_serv_kwh")),
                        fm(media(r, "ens_critica_kwh")), fm(media(r, "cargas_afectadas"), 1)])
    w("ENS de la propuesta por alimentador y por grupo de interés (ISO/IEC 42005), día laboral:\n")
    w(tabla(["Condición", "ENS A1", "ENS A2", "% servida A1", "% servida A2", "Residencial y comercial",
             "Industria y servicios", "Hospital", "Cargas afectadas"], filas_t) + "\n")
    w("**Continuidad del servicio de decisión** (perfil «aprueba», todos los periodos y días):\n")
    filas_t = []
    fams = [("Catálogo (10)", lambda f: f["familia"] == "CAT"),
            ("Representativos F1–F6", lambda f: f["escenario"] in [e.id for e in REPRESENTATIVOS]),
            ("A1: falla en B, G o H", lambda f: f["familia"] == "A1"), ("A2: medidor vacío", lambda f: f["familia"] == "A2"),
            ("A3: demanda nula", lambda f: f["familia"] == "A3"), ("A4: hospital sin cobertura", lambda f: f["familia"] == "A4"),
            ("A5: renovable baja", lambda f: f["familia"] == "A5"),
            ("Datos F6 (congelado, vacío, negativas)", lambda f: f["escenario"] in [e.id for e in DATOS_ESC]),
            ("Excedente renovable", lambda f: f["escenario"] in [e.id for e in EXPORTA_ESC])]
    for nom, fn in fams:
        r = [f for f in B if fn(f)]
        if not r:
            continue
        n = len(r)
        filas_t.append([nom, n, fm(100 * len(sel(r, resultado="propuesta")) / n, 1),
                        fm(100 * len(sel(r, resultado="escalamiento")) / n, 1), fm(100 * len(sel(r, resultado="roto")) / n, 1),
                        fm(media(r, "mensajes"), 1), fm(media(r, "costo_coordinacion"), 1)])
    w(tabla(["Grupo", "Corridas", "% propuesta", "% escalamiento", "% flujo roto", "Mensajes", "Costo de coordinación"],
            filas_t) + "\n")

    w("### 4.2 Eficiencia\n")
    filas_t = []
    for e in main:
        r = sel(B, escenario=e, periodo=P, dia="laboral")
        filas_t.append([e, f"{fm(media(r, 'renovable_pct_s0'), 1)} → {fm(media(r, 'renovable_pct'), 1)}",
                        fm(media(r, "renov_desperdiciada_kwh")), fm(media(r, "perdidas_pct"), 2),
                        f"{fm(media(r, 'util_max_SE'))} / {fm(media(r, 'util_media_SE'))}",
                        fm(media(r, "util_max_A1")), f"{fm(media(r, 'util_max_A2'))} / {fm(media(r, 'util_max_trafoBT_A2'))}",
                        f"{fm(media(r, 'factor_carga_s0'), 2)} → {fm(media(r, 'factor_carga'), 2)}",
                        fm(media(r, "reduccion_pico_pct"), 1), fm(media(r, "desplazado_kwh")),
                        fm(media(r, "desplazado_por_deficit_evitado"), 2), fm(media(r, "acciones"), 1)])
    w(f"Energética y operativa, {P}, día laboral (utilización en %; «máx / media» o «cabecera / trafo BT»):\n")
    w(tabla(["Condición", "% renovable S0 → prop.", "Renovable desperdiciada (kWh)", "Pérdidas (%)", "Uso SE máx / medio",
             "Uso A1 máx", "Uso A2 / trafo BT máx", "Factor de carga S0 → prop.", "Reducción del pico (%)", "Desplazado (kWh)",
             "kWh desplazado por kWh evitado", "Acciones"], filas_t) + "\n")
    ex = [m for m in MJ if m["mejora"] == "M-EXPORT" and m["periodo"] == P]
    filas_t = []
    for e in [x.id for x in EXPORTA_ESC]:
        r = sel(B, escenario=e, periodo=P)
        mm = [m for m in ex if m["escenario"] == e]
        filas_t.append([e, fm(media(r, "renov_disponible_kwh")), fm(media(r, "renov_usada_kwh")),
                        fm(media(r, "renov_desperdiciada_kwh")), fm(media(r, "pct_renov_aprovechada"), 1),
                        fm(media(mm, "horas_en_limite"), 1)])
    w(f"Casos de excedente renovable ({P}, ambos días). El límite de inyección (800 kW) no actúa porque el modelo no "
      "exporta aguas arriba:\n")
    w(tabla(["Caso", "Renovable disponible", "Usada", "Desperdiciada", "% aprovechada", "Horas en las que exportar "
             "chocaría con el límite"], filas_t) + "\n")
    w(f"![Renovable aprovechada contra desperdiciada]({imgs['renovable']})\n")
    w("**Pronóstico** (catálogo, perfil «aprueba»; MAPE y cobertura «validación» son los que calcula D sobre el último "
      "día del historial; «real» se mide contra el día recomendado):\n")
    filas_t = []
    cat = [f for f in B if f["familia"] == "CAT"]
    for p in PERIODOS:
        for dia in ("laboral", "finde"):
            r = sel(cat, periodo=p, dia=dia)
            if r:
                filas_t.append([p, dia, fm(media(r, "mape_validacion_pct"), 1), fm(media(r, "mape_real_pct"), 1),
                                fm(media(r, "sesgo_real_pct"), 1, True), fm(media(r, "cobertura_validacion_pct"), 1),
                                fm(media(r, "cobertura_dem_real_pct"), 1), fm(media(r, "cobertura_ren_real_pct"), 1),
                                fm(media(r, "ancho_dem_pct"), 1), fm(media(r, "ancho_ren_pct"), 1),
                                fm(100 * (media(r, "alerta_deriva") or 0), 0)])
    w(tabla(["Periodo", "Día", "MAPE validación (%)", "MAPE real (%)", "Sesgo real (%)", "Cobertura validación (%)",
             "Cobertura demanda real (%)", "Cobertura renovable real (%)", "Ancho banda demanda (%)",
             "Ancho banda renovable (%)", "% con alerta de deriva"], filas_t) + "\n")
    w(f"![MAPE laboral contra fin de semana]({imgs['mape']})\n")
    w("**Sistema** (perfil «aprueba» salvo indicación; costo de coordinación = Σ pesos de aristas del grafo A–H):\n")
    filas_t = []
    for p in PERIODOS:
        r = [f for f in BM if f["periodo"] == p]
        r2 = [f for f in F if f["periodo"] == p and f["escenario"] in main and not f["mejora"] and f["perfil"] != "aprueba"]
        if r:
            filas_t.append([p, fm(float(np.median(_vals(r, "tiempo_s"))), 3), fm(max(_vals(r, "tiempo_s")), 3),
                            fm(media(r, "mensajes"), 1), fm(media(r, "costo_coordinacion"), 1),
                            fm(media(r2, "mensajes"), 1), fm(media(r2, "costo_coordinacion"), 1)])
    w(tabla(["Periodo", "Tiempo mediano (s)", "Tiempo máx. (s)", "Mensajes", "Costo de coordinación",
             "Mensajes con un rechazo", "Costo con un rechazo"], filas_t) + "\n")
    s_, c1, c2, c4 = (conc.get(k) or {} for k in ("servidor", "procesos_1", "procesos_2", "procesos_4"))
    w(f"API (`servidor.py` sin cambios, un análisis a la vez por `_motor_lock`), ráfaga de {conc.get('pedidos')} "
      f"análisis simultáneos (1m, 6m y 1a): p50 {fm(s_.get('p50_s'), 2)} s, **p95 {fm(s_.get('p95_s'), 2)} s**, máximo "
      f"{fm(s_.get('max_s'), 2)} s, {s_.get('errores')} errores.\n")

    w("### 4.3 Costos\n")
    filas_t = []
    for e in main:
        r = sel(B, escenario=e, periodo=P, dia="laboral")
        filas_t.append([e, mi(r, "costo_s0_mxn"), mi(r, "costo_mxn"),
                        f"{fm(media(r, 'costo_kwh_s0'), 3)} → {fm(media(r, 'costo_kwh'), 3)}",
                        f"{fm(media(r, 'ahorro_mxn'), 0, True)} ({fm(media(r, 'ahorro_pct'), 1, True)} %)",
                        fm(media(r, "efecto_tarifa_mxn"), 0, True), fm(media(r, "efecto_renovable_mxn"), 0, True),
                        fm(media(r, "efecto_perdidas_mxn"), 0, True), fm(media(r, "efecto_servicio_mxn"), 0, True),
                        f"{mxn(media(r, 'costo_social_s0_mxn'))} → {mxn(media(r, 'costo_social_mxn'))}",
                        f"{mxn(media(r, 'costo_total_s0_mxn'))} → {mxn(media(r, 'costo_total_mxn'))}"])
    w(f"{P}, día laboral, MXN/día (planificado). El ahorro se descompone exactamente en efecto tarifa + renovable + "
      "pérdidas + servicio; el «efecto servicio» es energía que se deja de comprar (o se compra de más) porque cambia la "
      "energía servida y **no es eficiencia**:\n")
    w(tabla(["Condición", "Costo S0", "Costo propuesta", "MXN/kWh servido S0 → prop.", "Ahorro", "Ef. tarifa",
             "Ef. renovable", "Ef. pérdidas", "Ef. servicio", "Costo social S0 → prop.", "Costo total S0 → prop."],
            filas_t) + "\n")
    w(f"![Costo por kWh servido]({imgs['costo']})\n")
    filas_t = []
    for p in PERIODOS:
        r = [f for f in BM if f["periodo"] == p]
        rs, rd = [f for f in r if not _con_deficit(f)], [f for f in r if _con_deficit(f)]
        if r:
            filas_t.append([p, fm(media(rs, "ahorro_pct"), 1), mxn(media(rs, "ahorro_mxn")),
                            f"{fm(media(rd, 'costo_kwh_s0'), 3)} → {fm(media(rd, 'costo_kwh'), 3)}",
                            f"{mxn(media(rd, 'costo_total_s0_mxn'))} → {mxn(media(rd, 'costo_total_mxn'))}",
                            f"{mxn(media(rd, 'costo_total_real_s0_mxn'))} → {mxn(media(rd, 'costo_total_real_mxn'))}"])
    w("Por periodo (ambos días): días sin déficit y días con déficit en S0:\n")
    w(tabla(["Periodo", "Ahorro sin déficit (%)", "Ahorro sin déficit (MXN/día)", "MXN/kWh servido con déficit S0 → prop.",
             "Costo total plan S0 → prop.", "Costo total real S0 → prop."], filas_t) + "\n")
    rob = [f for f in BM if _num(f.get("costo_robustez_mxn"))]
    filas_t = []
    for e in main:
        r = sel(rob, escenario=e, periodo=P)
        if r:
            filas_t.append([e, mi(r, "recorte_adicional_robustez_kwh"), mi(r, "deficit_p90_evitado_kwh"),
                            mxn(media(r, "costo_robustez_mxn")), mxn(media(r, "beneficio_robustez_mxn")),
                            fm((media(r, "beneficio_robustez_mxn") or 0) - (media(r, "costo_robustez_mxn") or 0), 0, True)])
    w(f"**Costo de la robustez** ({P}, ambos días): recorte adicional del plan P90 × CENS contra la ENS que evita si la "
      f"demanda llega a P90 (probabilidad {SUPUESTOS['prob_desfavorable']}):\n")
    w(tabla(["Condición", "Recorte adicional (kWh)", "Déficit P90 evitado (kWh)", "Costo (MXN)", "Beneficio esperado (MXN)",
             "Neto (MXN)"], filas_t) + "\n")

    # 5. Perfiles
    w("## 5. Perfiles del operador\n")
    w("Campaña principal (catálogo + F1–F6), todos los periodos y ambos días. Con un rechazo, C delibera otra vez "
      "(ronda 1). Sin plan (escalamiento) la red opera como S0.\n")
    filas_t = []
    todos = [f for f in F if f["escenario"] in main and not f["mejora"]]
    for grupo, fn in (("Con déficit en S0", _con_deficit), ("Sin déficit", lambda f: not _con_deficit(f))):
        for perfil in PERFILES:
            r = [f for f in todos if f["perfil"] == perfil and fn(f)]
            if not r:
                continue
            n = len(r)
            filas_t.append([grupo, perfil, n, fm(100 * len(sel(r, resultado="propuesta")) / n, 1),
                            fm(100 * len(sel(r, resultado="escalamiento")) / n, 1), fm(media(r, "ens_kwh")),
                            fm(media(r, "ens_real_kwh")), fm(media(r, "ens_no_prevista_kwh")), fm(media(r, "sobre_recorte_kwh")),
                            fm(media(r, "renovable_pct"), 1), mxn(media(r, "costo_mxn")), fm(media(r, "costo_kwh"), 3),
                            mxn(media(r, "costo_total_real_mxn")), fm(media(r, "decisiones_operador"), 1),
                            fm(media(r, "mensajes"), 1)])
    w(tabla(["Grupo", "Perfil", "Corridas", "% propuesta", "% escalamiento", "ENS plan", "ENS real", "ENS no prevista",
             "Sobre-recorte", "% renovable", "Costo energía", "MXN/kWh servido", "Costo total real", "Decisiones",
             "Mensajes"], filas_t) + "\n")
    w(f"- **Aprueba**: menor costo total real en los días con déficit, pero carga la ENS no prevista (B1) entre semana.\n"
      f"- **Recortar menos**: en contingencia el recorte P50 ya es el mínimo, así que C escala en el "
      f"{fm(fallas['B2']['escalamiento_pct'], 1)} % de esos casos con «No quedan medidas de contingencia» (B2); sin plan, el "
      f"déficit llega sin aviso y el costo total real sube {mxn(fallas['B2']['costo_total'])} MXN/día frente a aprobar.\n"
      f"- **Más seguridad**: el plan P90 casi elimina la ENS no prevista, pero recorta de más (sobre todo en fin de "
      f"semana, por A10) y es el perfil más caro en costo social.\n")

    # 6. Fallas
    w("## 6. Fallas con impacto cuantificado\n")
    w("Todas las cifras salen de las corridas de esta campaña (todas las semillas; periodos indicados). «Costo total» "
      "= energía + costo social de la ENS con los supuestos de la sección 2. Reproducir: el comando de cada falla "
      "(semilla 7; cambia `--semilla`, `--periodo` o `--dia`). Las nuevas (B1–B3) tienen una prueba "
      "`@unittest.expectedFailure` en `pruebas_adversas.py` (clase `DefectosEvaluacion`) y su entrada en `DEFECTOS`.\n")
    q = fallas
    impactos = {
        "A1": f"{fm(q['A1']['roto_pct'])} % de flujos rotos ({q['A1']['n']} corridas con el transformador al 55 %): sin "
              f"plan la ENS planificada sube de {fm(q['A1']['ens_con_plan'])} a {fm(q['A1']['ens_sin_plan'])} kWh/día y "
              f"llega sin aviso; costo total real {mxn(q['A1']['costo_total_con'])} → {mxn(q['A1']['costo_total_sin'])} "
              f"MXN/día (+{mxn((q['A1']['costo_total_sin'] or 0) - (q['A1']['costo_total_con'] or 0))}); "
              f"{fm(q['A1']['mensajes'], 1)} mensajes y ningún reporte.",
        "A2": f"{fm(q['A2']['roto_pct'])} % de flujos rotos; costo total real {mxn(q['A2']['costo_total_sin'])} MXN/día "
              f"contra {mxn(q['A2']['costo_total_con'])} con datos limpios (M-DATOS termina el {fm(q['A2']['propuesta_con_datos_pct'])} %); "
              f"ENS real {fm(q['A2']['ens_real_sin'])} → {fm(q['A2']['ens_real_con'])} kWh/día.",
        "A3": f"{fm(q['A3']['escalamiento_pct'])} % escalado con «{q['A3']['motivo'][:45]}…»: diagnóstico falso, "
              f"{fm(q['A3']['mensajes'], 1)} mensajes y costo de coordinación {fm(q['A3']['costo_coord'], 1)} (normal: "
              f"{fm(q['A3']['mensajes_normal'], 1)} y {fm(q['A3']['costo_coord_normal'], 1)}) y 1 decisión innecesaria del operador. "
              "Sin efecto en ENS ni en costo de energía (no hay demanda).",
        "A4": f"ENS del hospital {fm(q['A4']['ens_critica'])} kWh/día (real {fm(q['A4']['ens_critica_real'])}); costo social "
              f"del hospital {mxn(q['A4']['costo_social_hospital'])} MXN/día de {mxn(q['A4']['costo_social_total'])} totales; "
              f"el escalamiento lo menciona en el {fm(q['A4']['menciona_pct'])} % de los casos.",
        "A5": f"ENS no prevista con el plan aprobado: {fm(q['A5']['ens_np_lab'])} kWh/día laboral y {fm(q['A5']['ens_np_fin'])} "
              f"en fin de semana (costo social +{mxn(q['A5']['costo_social_np'])} MXN/día); la cobertura de validación marca "
              f"{fm(q['A5']['cob_valid'], 1)} % (solo demanda) y la alerta salta en el {fm(q['A5']['alerta_cal_pct'])} %, "
              f"mientras la cobertura renovable real es {fm(q['A5']['cob_ren_real'], 1)} %. Pronóstico optimista de F4: "
              f"{fm(q['A5']['f4_np_lab'])} kWh/día no previstos entre semana.",
        "A6": f"Ancho de banda {fm(q['A6']['ancho_1d'], 1)} % con 1 día contra {fm(q['A6']['ancho_1m'], 1)} % con 1 mes; "
              f"cobertura real {fm(q['A6']['cob_1d'], 1)} % contra {fm(q['A6']['cob_1m'], 1)} %. Con el plan P90 (perfil más "
              f"seguridad) quedan {fm(q['A6']['np_p90_1d'])} kWh/día no previstos en 1d contra {fm(q['A6']['np_p90_1m'])} en 1m "
              f"(costo social +{mxn(q['A6']['costo_np_1d'])} MXN/día).",
        "A7": f"{q['A7'].get('respuestas_500', '—')} de {q['A7'].get('entradas', '—')} entradas mal tipadas responden 500 "
              f"({'; '.join(q['A7'].get('detalle', [])[:2])}…): el cliente no recibe un mensaje útil. Sin efecto en ENS ni costo.",
        "A8": f"Pronóstico mínimo {fm(q['A8']['pron_min'])} kW (negativo en el {fm(q['A8']['pct_negativo'])} % de las corridas "
              f"1d–1s; con M-DATOS {fm(q['A8']['pron_min_datos'])} kW); MAPE real {fm(q['A8']['mape'], 1)} → "
              f"{fm(q['A8']['mape_datos'], 1)} %; con el transformador al 55 % la ENS no prevista pasa de "
              f"{fm(q['A8']['np'])} a {fm(q['A8']['np_datos'])} kWh/día con datos limpios (Δ costo total "
              f"{fm(q['A8']['costo'], 0, True)} MXN/día).",
        "A9": f"Sesgo real {fm(q['A9']['sesgo'], 1, True)} % y MAPE {fm(q['A9']['mape'], 1)} % (1d–1s) contra "
              f"{fm(q['A9']['sesgo_datos'], 1, True)} % y {fm(q['A9']['mape_datos'], 1)} % con M-DATOS; con el transformador al "
              f"55 %: ENS no prevista {fm(q['A9']['np'])} → {fm(q['A9']['np_datos'])} kWh/día, Δ costo total "
              f"{fm(q['A9']['costo'], 0, True)} MXN/día.",
        "A10": f"MAPE real {fm(q['A10']['mape_fin'], 1)} % en fin de semana contra {fm(q['A10']['mape_lab'], 1)} % (validación "
               f"{fm(q['A10']['mape_val_fin'], 1)} contra {fm(q['A10']['mape_val_lab'], 1)} %); sesgo "
               f"{fm(q['A10']['sesgo_fin'], 1, True)} %; alerta de deriva en el {fm(q['A10']['alerta_fin'])} % de los fines de "
               f"semana (falsa); en contingencia se recortan {fm(q['A10']['sobre_recorte'])} kWh/día de más y el plan deja "
               f"{fm(q['A10']['ens_real_plan'])} kWh/día de ENS real contra {fm(q['A10']['ens_real_s0'])} si no se hiciera nada "
               f"(bastaban {fm(q['A10']['ens_min'])}); M-PRON ahorra {mxn(q['A10']['ahorro_pron'])} MXN/día de costo total.",
        "B1": f"Sesgo real {fm(q['B1']['sesgo_lab'], 1, True)} % en día laboral (MAPE {fm(q['B1']['mape_lab'], 1)} %, validación "
              f"{fm(q['B1']['mape_val_lab'], 1)} %, alerta {fm(q['B1']['alerta_lab'])} %: nada lo detecta). En contingencia el "
              f"plan aprobado deja **{fm(q['B1']['np'])} kWh/día de ENS no prevista** sin aviso (con M-PRON "
              f"{fm(q['B1']['np_pron'])}); costo social +{mxn(q['B1']['costo_social_np'])} MXN/día; M-PRON ahorra "
              f"{mxn(q['B1']['ahorro_pron'])} MXN/día.",
        "B2": f"El {fm(q['B2']['escalamiento_pct'], 1)} % de los rechazos «recortar menos» en contingencia "
              f"({q['B2']['n_cont']} corridas) terminan en escalamiento con «{q['B2']['motivo'][:50]}…». Sin plan, la ENS real "
              f"cambia {fm(q['B2']['ens_real'], 0, True)} kWh/día pero {fm(q['B2']['ens_no_prevista_sin_aviso'])} kWh/día llegan "
              f"sin aviso y el costo total real sube {mxn(q['B2']['costo_total'])} MXN/día frente a aprobar.",
        "B3": f"{fm(q['B3']['negativo_pct'], 1)} % de las propuestas de contingencia ({q['B3']['n']}) marcan «negativo» al operador "
              f"por un sobrecosto medio de {mxn(q['B3']['sobrecosto'])} MXN/día que es efecto servicio "
              f"({fm(q['B3']['efecto_servicio'], 0, True)} MXN: sirven más energía); en el {fm(q['B3']['negativo_mas_barato_pct'], 1)} % "
              f"el costo por kWh servido baja ({fm(q['B3']['kwh_s0'], 3)} → {fm(q['B3']['kwh'], 3)} MXN/kWh).",
    }
    orden = ["B1", "A1", "A10", "A5", "A8", "A2", "A4", "B2", "A9", "A6", "A3", "B3", "A7"]
    w(tabla(["Id", "Severidad", "Falla", "Eje", "Impacto cuantificado"],
            [[i, FALLAS_INFO[i]["sev"], FALLAS_INFO[i]["titulo"], FALLAS_INFO[i]["ejes"], impactos[i]] for i in orden]) + "\n")
    w("Orden: por impacto en continuidad y costo social. Detalle de cada una:\n")
    for i in orden:
        fi = FALLAS_INFO[i]
        cmd = (f"python evaluar.py --corrida {fi['comando']}" if fi["comando"] and "--periodo" in fi["comando"]
               else f"python evaluar.py --corrida {fi['comando']} --periodo 1m" if fi["comando"]
               else "python pruebas_adversas.py F7API")
        if "--semilla" not in cmd and fi["comando"]:
            cmd += " --semilla 7"
        w(f"### {i} · {fi['sev']} · {fi['titulo']}\n")
        w(f"- **Eje**: {fi['ejes']}.\n- **Impacto**: {impactos[i]}\n- **Regla / norma**: {fi['regla']}.\n"
          f"- **Causa probable**: {fi['causa']}.\n- **Reproducir** (semilla 7): `{cmd}`\n")

    w("### Otros hallazgos verificados (no se registran como defecto)\n")
    rcont = [f for f in BM if f.get("modo") == "contingencia"]
    iguales = sum(1 for f in rcont if f.get("riesgo_max") == f.get("riesgo_max_s0"))
    w(f"- **Riesgo saturado**: en contingencia el riesgo máximo de la propuesta es 25 en el "
      f"{fm(100 * len([f for f in rcont if f.get('riesgo_max') == 25]) / max(len(rcont), 1), 1)} % de los casos e igual al "
      f"de S0 en el {fm(100 * iguales / max(len(rcont), 1), 1)} %: no distingue el plan de no hacer nada.\n"
      f"- **Política P50**: aun con un pronóstico sin sesgo (M-PRON) el recorte mínimo deja "
      f"{fm(mej['M-PRON']['dias']['laboral']['np_m'])} kWh/día no previstos entre semana: es el riesgo aceptado de "
      "planificar con la mediana (ver M-ROBUSTEZ).\n"
      f"- **Desperdicio renovable**: hasta {fm(mej['M-EXPORT']['max_desp'])} kWh/día con demanda −90 % y renovables al 150 %; "
      f"en la campaña principal el máximo es {fm(mej['M-EXPORT']['principal_desp'])} kWh/día (la red absorbe el excedente).\n"
      "- **Un análisis a la vez**: ver M-CONCURRENCIA.\n")

    # 7. Mejoras
    w("## 7. Mejoras evaluadas\n")
    w("Cada mejora se midió como escenario hipotético en las mismas claves (escenario, periodo, semilla, día, perfil) "
      "que la situación actual; Δ = mejora − actual. Detalle por escenario en `mejoras.csv`.\n")
    m = mej
    pr = m["M-PRON"]["dias"]
    desc_m = {
        "M-PRON": f"MAPE real laboral {fm(pr['laboral']['mape'], 1)} → {fm(pr['laboral']['mape_m'], 1)} % y fin de semana "
                  f"{fm(pr['finde']['mape'], 1)} → {fm(pr['finde']['mape_m'], 1)} %; sesgo {fm(pr['laboral']['sesgo'], 1, True)} → "
                  f"{fm(pr['laboral']['sesgo_m'], 1, True)} % y {fm(pr['finde']['sesgo'], 1, True)} → {fm(pr['finde']['sesgo_m'], 1, True)} %; "
                  f"cobertura real laboral {fm(pr['laboral']['cob'], 1)} → {fm(pr['laboral']['cob_m'], 1)} % con banda "
                  f"{fm(pr['laboral']['ancho'], 1)} → {fm(pr['laboral']['ancho_m'], 1)} %; alerta de deriva en fin de semana "
                  f"{fm(pr['finde']['alerta'])} → {fm(pr['finde']['alerta_m'])} %; riesgo máximo fin de semana "
                  f"{fm(pr['finde']['riesgo'], 1)} → {fm(pr['finde']['riesgo_m'], 1)}. Contingencias del catálogo: ENS no prevista laboral "
                  f"{fm(pr['laboral']['np'])} → {fm(pr['laboral']['np_m'])} kWh/día; sobre-recorte en fin de semana "
                  f"{fm(pr['finde']['sobre'])} → {fm(pr['finde']['sobre_m'])} kWh/día; costo total real en todos los días con "
                  f"déficit −{mxn(m['M-PRON']['beneficio_dia_deficit'])} MXN/día. Plan P90: recorte {fm(m['M-PRON']['recorte_p90'], 0, True)} kWh/día.",
        "M-EXPORT": f"En los {m['M-EXPORT']['n']} días con desperdicio: desperdicio {fm(m['M-EXPORT']['desp'])} → "
                    f"{fm(m['M-EXPORT']['desp_m'])} kWh/día, aprovechamiento {fm(m['M-EXPORT']['aprov'], 1)} → "
                    f"{fm(m['M-EXPORT']['aprov_m'], 1)} %, {fm(m['M-EXPORT']['exportada'])} kWh exportados, ingreso "
                    f"{mxn(m['M-EXPORT']['ingreso'])} y ahorro neto {mxn(m['M-EXPORT']['neto'])} MXN/día; el límite de 800 kW "
                    f"actuaría {fm(m['M-EXPORT']['horas_limite'], 1)} h/día. % renovable de la demanda: sin cambio (se exporta, no se consume).",
        "M-BANDA": f"1d con plan P90: ENS no prevista {fm(m['M-BANDA']['np_1d'])} → {fm(m['M-BANDA']['np_1d_m'])} kWh/día a cambio de "
                   f"{fm(m['M-BANDA']['recorte_1d'], 0, True)} kWh/día de recorte (costo total real {fm(-m['M-BANDA']['costo_1d'], 0, True)} "
                   f"MXN/día). Renovable baja (A5) con plan P90: {fm(m['M-BANDA']['np_a5'])} → {fm(m['M-BANDA']['np_a5_m'])} kWh/día, "
                   f"recorte {fm(m['M-BANDA']['recorte_a5'], 0, True)} kWh/día; con el plan P50 no cambia nada "
                   f"({fm(m['M-BANDA']['np_a5_p50'])} kWh/día): la alerta sí detecta la caída.",
        "M-ROBUSTEZ": "Contingencias de la campaña principal ({n} corridas), valores esperados por día: ".format(n=m["M-ROBUSTEZ"]["n"])
                      + "; ".join(f"{p} recorte {fm(v['recorte_kwh'])} kWh, no prevista {fm(v['ens_no_prevista_real_kwh'])} kWh, "
                                  f"costo total {mxn(v['total_esperado_mxn'])} MXN"
                                  for p, v in m["M-ROBUSTEZ"]["politicas"].items())
                      + f". El tope ahorra {mxn(m['M-ROBUSTEZ']['beneficio_vs_p90'])} MXN/día frente al P90; el costo mínimo "
                        f"elige α = 0 (P50) en el {fm(m['M-ROBUSTEZ']['mincosto_p50_pct'])} % de los casos.",
        "M-DATOS": f"Casos F6 y A2 ({m['M-DATOS']['n']} pares): flujos completos {fm(m['M-DATOS']['flujo'], 1)} → "
                   f"{fm(m['M-DATOS']['flujo_m'], 1)} %; MAPE real {fm(m['M-DATOS']['mape'], 1)} → {fm(m['M-DATOS']['mape_m'], 1)} %; "
                   f"ENS no prevista {fm(m['M-DATOS']['np'])} → {fm(m['M-DATOS']['np_m'])} kWh/día; ENS real en F6 "
                   f"{fm(m['M-DATOS']['f6_ens'])} → {fm(m['M-DATOS']['f6_ens_m'])} kWh/día; costo total real "
                   f"−{mxn(m['M-DATOS']['beneficio'])} MXN/día.",
        "M-CONCURRENCIA": f"Ráfaga de {conc.get('pedidos')} análisis: p95 servidor {fm(s_.get('p95_s'), 2)} s; 1 proceso "
                          f"{fm(c1.get('p95_s'), 2)} s; **2 procesos {fm(c2.get('p95_s'), 2)} s; 4 procesos {fm(c4.get('p95_s'), 2)} s** "
                          f"(p50 {fm(s_.get('p50_s'), 2)} → {fm(c2.get('p50_s'), 2)} → {fm(c4.get('p50_s'), 2)} s). Sin efecto en ENS ni costo.",
        "M-SUPERVISOR": f"Las {m['M-SUPERVISOR']['n']} corridas de A1 terminan con plan (0 → 100 %); ENS real "
                        f"{fm(m['M-SUPERVISOR']['ens_real'])} → {fm(m['M-SUPERVISOR']['ens_real_m'])} kWh/día; costo total real "
                        f"−{mxn(m['M-SUPERVISOR']['beneficio'])} MXN/día.",
    }
    beneficio = {"M-PRON": m["M-PRON"]["beneficio_dia_deficit"], "M-EXPORT": m["M-EXPORT"]["neto"],
                 "M-BANDA": m["M-BANDA"]["beneficio"], "M-ROBUSTEZ": m["M-ROBUSTEZ"]["beneficio_vs_p90"],
                 "M-DATOS": m["M-DATOS"]["beneficio"], "M-CONCURRENCIA": None, "M-SUPERVISOR": m["M-SUPERVISOR"]["beneficio"]}
    filas_m = []
    for k, info in MEJORAS_INFO.items():
        pt = _puntaje(beneficio[k], servicio=k == "M-CONCURRENCIA")
        filas_m.append({"id": k, "horas": info["horas"], "puntaje": pt, "beneficio": beneficio[k],
                        "razon": pt / info["horas"]})
    w(tabla(["Mejora", "Qué cambia", "Delta medido", "Esfuerzo (h)", "Riesgo", "Archivos", "Reglas / ISO", "Cierra"],
            [[k, MEJORAS_INFO[k]["desc"], desc_m[k], MEJORAS_INFO[k]["horas"], MEJORAS_INFO[k]["riesgo"],
              MEJORAS_INFO[k]["archivos"], MEJORAS_INFO[k]["reglas"], MEJORAS_INFO[k]["cierra"]] for k in MEJORAS_INFO]) + "\n")
    w(f"![Frontera costo-continuidad]({imgs['frontera']})\n")
    rr = m["M-ROBUSTEZ"]["politicas"]
    w(tabla(["Política de recorte (contingencia)", "Recorte (kWh/día)", "ENS no prevista real", "ENS esperada",
             "Energía esperada (MXN)", "Costo social esperado (MXN)", "Costo total esperado (MXN)"],
            [[p, fm(v["recorte_kwh"]), fm(v["ens_no_prevista_real_kwh"]), fm(v["ens_esperada_kwh"]), mxn(v["energia_esperada_mxn"]),
              mxn(v["social_esperado_mxn"]), mxn(v["total_esperado_mxn"])] for p, v in rr.items()]) + "\n")
    w("Lectura de M-ROBUSTEZ: con un CENS 20–60 veces mayor que la tarifa, recortar «por si acaso» cuesta más de lo que "
      "evita; el plan P90 solo se justifica si la probabilidad del escenario desfavorable es alta o el déficit no "
      "previsto afecta cargas con CENS muy superior. Con estos supuestos el costo mínimo coincide casi "
      "siempre con el P50 vigente; para quien pide más seguridad, el tope conserva buena parte de la protección del "
      "P90 (ENS no prevista) a menor costo.\n")

    # 8. Matriz
    w("## 8. Matriz impacto × esfuerzo\n")
    w("Impacto: puntaje 1–5 por el beneficio esperado en costo total (MXN/día) donde la mejora aplica (5 ≥ 100 000; "
      "4 ≥ 10 000; 3 ≥ 1 000; 2 > 0 o mejora solo de servicio; 1 sin beneficio). Esfuerzo: horas estimadas de "
      "implementación con pruebas.\n")
    img_m = grafica_matriz(destino, filas_m)
    w(tabla(["Mejora", "Beneficio (MXN/día donde aplica)", "Puntaje", "Esfuerzo (h)", "Puntaje por hora", "Cuadrante"],
            [[x["id"], mxn(x["beneficio"]), x["puntaje"], x["horas"], fm(x["razon"], 2),
              ("ganancia rápida" if x["horas"] <= 8 and x["puntaje"] >= 3 else "proyecto estructural" if x["puntaje"] >= 3
               else "relleno" if x["horas"] <= 8 else "baja prioridad")]
             for x in sorted(filas_m, key=lambda x: -x["razon"])]) + "\n")
    w(f"![Matriz impacto × esfuerzo]({img_m})\n")

    # 9. Hoja de ruta
    w("## 9. Hoja de ruta\n")
    w("**1. Correcciones inmediatas (≤ 1 semana, ~13 h)**\n")
    w(f"1. A1 · `AgenteMeta._manejar_falla` escala si no hay solicitud que repetir (M-SUPERVISOR, 1 h): recupera "
      f"{mxn(m['M-SUPERVISOR']['beneficio'])} MXN/día cuando falla B, G o H en contingencia.\n"
      f"2. A2, A8, A9 · datos limpios en B y D (M-DATOS, 6 h): flujos completos {fm(m['M-DATOS']['flujo'], 1)} → "
      f"{fm(m['M-DATOS']['flujo_m'], 1)} % y −{mxn(m['M-DATOS']['beneficio'])} MXN/día en los casos F6.\n"
      "3. A4 y B2 · textos de escalamiento que digan cuánta energía falta para el hospital, que el plan rechazado ya era el "
      "mínimo y cuánta ENS llegará sin aviso (2 h).\n"
      "4. A3 · proteger la división en `_metricas` y responder «no hay demanda» (1 h).\n"
      "5. B3 · el impacto al operador compara costo por kWh servido (1 h). A7 · validar tipos y Content-Length (2 h).\n")
    w("**2. Mejoras de corto plazo (≤ 1 mes, ~18 h)**\n")
    w(f"1. M-PRON (A10, B1, 6 h): la de mayor impacto; ENS no prevista laboral {fm(pr['laboral']['np'])} → "
      f"{fm(pr['laboral']['np_m'])} kWh/día y −{mxn(m['M-PRON']['beneficio_dia_deficit'])} MXN/día en días con déficit. "
      "Agregar el sesgo al monitoreo ISO/IEC 5338 junto con el MAPE.\n"
      f"2. M-ROBUSTEZ con tope (8 h): el plan robusto con tope ahorra {mxn(m['M-ROBUSTEZ']['beneficio_vs_p90'])} MXN/día frente "
      "al P90 actual; validar el CENS con el operador antes de usar el criterio de costo mínimo.\n"
      "3. M-BANDA solo como alerta (4 h): validar las bandas renovables (A5) y avisar con historial corto (A6), sin "
      "ensanchar automáticamente el plan P90, que recorta mucho más de lo que evita.\n")
    w("**3. Mejoras estructurales (trimestre, ~28 h + validación)**\n")
    w(f"1. M-EXPORT (12 h): requiere precio y acuerdo de exportación; recupera hasta {fm(m['M-EXPORT']['desp'])} kWh/día "
      f"desperdiciados ({mxn(m['M-EXPORT']['neto'])} MXN/día netos) solo en días de excedente.\n"
      f"2. M-CONCURRENCIA (16 h): p95 de {fm(s_.get('p95_s'), 2)} a {fm(c2.get('p95_s'), 2)} s con 2 procesos; exige memoria "
      "de aprendizaje por sesión y pruebas de aislamiento.\n"
      "3. Recalibrar la matriz de riesgo (ISO/IEC 23894) para que distinga el plan de S0 en contingencia, y sustituir los "
      "CENS ilustrativos por valores acordados con el operador.\n")

    with open(os.path.join(destino, "INFORME.md" if not rapido else "INFORME_rapido.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    resumen = {
        "modo": "rapido" if rapido else "completo", "fecha": str(datetime.now().date()),
        "corridas": len(F), "corridas_linea_base": len(B), "evaluaciones_posteriores": len(MJ),
        "segundos_campana": round(segundos_campana, 1), "segundos_total": round(segundos, 1),
        "inconsistencias": inconsist, "supuestos": SUPUESTOS, "dictamen": dictamen,
        "servicio_decision": {r: sum(1 for f in B if f["resultado"] == r) for r in ("propuesta", "escalamiento", "roto")},
        "fallas": {k: {"severidad": FALLAS_INFO[k]["sev"], "titulo": FALLAS_INFO[k]["titulo"], "impacto": impactos[k],
                       "cifras": v} for k, v in fallas.items()},
        "mejoras": {k: {"esfuerzo_h": MEJORAS_INFO[k]["horas"], "riesgo": MEJORAS_INFO[k]["riesgo"], "delta": desc_m[k],
                        "cifras": mej[k]} for k in MEJORAS_INFO},
        "matriz": filas_m, "concurrencia": conc, "graficas": list(imgs.values()) + [img_m],
    }
    with open(os.path.join(destino, "resumen.json" if not rapido else "resumen_rapido.json"), "w", encoding="utf-8") as fh:
        json.dump(resumen, fh, ensure_ascii=False, indent=1, default=lambda o: float(o) if isinstance(o, np.floating) else str(o))
    return resumen


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rapido", action="store_true", help="solo el periodo 1m (< 2 min)")
    ap.add_argument("--corrida", help="reproduce una corrida (id de escenario)")
    ap.add_argument("--periodo", default="1m", choices=list(PERIODOS))
    ap.add_argument("--semilla", type=int, default=7)
    ap.add_argument("--dia", default="laboral", choices=["laboral", "finde"])
    ap.add_argument("--perfil", default="aprueba", choices=list(PERFILES))
    ap.add_argument("--mejora", default=None, choices=["M-PRON", "M-BANDA", "M-DATOS"])
    ap.add_argument("--supuestos", help="archivo JSON que reemplaza claves de SUPUESTOS")
    ap.add_argument("--procesos", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--listar", action="store_true")
    args = ap.parse_args()
    if args.supuestos:
        with open(args.supuestos, encoding="utf-8") as fh:
            SUPUESTOS.update(json.load(fh))
    if args.listar:
        for e in ESCENARIOS.values():
            print(f"{e.id:<34} {e.familia:<4} {e.desc}")
        return 0
    if args.corrida:
        return reproducir(args.corrida, args.periodo, args.semilla, args.dia, args.perfil, args.mejora)

    destino = os.path.join(DIR_INFORME, "rapido") if args.rapido else DIR_INFORME
    os.makedirs(destino, exist_ok=True)
    t0 = time.perf_counter()
    print(f"Evaluación de resultados ({'rápida: 1m' if args.rapido else 'completa: ' + ', '.join(PERIODOS)}); "
          f"{len(SUPUESTOS['semillas'])} semillas, días {SUPUESTOS['dias']}, {args.procesos} procesos", flush=True)
    filas, mejoras = ejecutar_campana(args.rapido, args.procesos)
    t_campana = time.perf_counter() - t0
    print("  midiendo concurrencia de la API...", flush=True)
    conc = medir_concurrencia()
    escribir_csv(os.path.join(destino, "kpis.csv"), filas)
    resumen = generar_informe(destino, filas, aplanar_mejoras(mejoras), conc, rapido=args.rapido,
                              segundos_campana=t_campana, segundos=time.perf_counter() - t0)
    print(f"\n{len(filas)} corridas en {time.perf_counter() - t0:.0f} s. Resultados en {os.path.relpath(destino, AQUI)}")
    print(json.dumps(resumen["dictamen"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
