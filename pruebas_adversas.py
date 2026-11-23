"""
Campaña de pruebas en escenarios adversos de ElectriAgent.

    python pruebas_adversas.py --rapido                    # solo el periodo 1m (< 2 min)
    python pruebas_adversas.py                             # 5 periodos: 1d, 1s, 1m, 6m, 1a
    python pruebas_adversas.py --caso F1.crec+60 --periodo 1a   # reproduce un caso y muestra sus invariantes
    python pruebas_adversas.py --listar                    # casos disponibles con su semilla
    python pruebas_adversas.py --servidor-capturas         # servidor con condiciones extremas (capturas)

Regla de la campaña: PROBAR, NO CORREGIR. Las pruebas miden el comportamiento; cada
defecto encontrado queda como @unittest.expectedFailure con su id (A1, A2, ...) que
remite a docs/pruebas_adversas/INFORME.md. Así la suite queda en verde y el defecto visible.

Solo biblioteca estándar + numpy. Toda aleatoriedad usa numpy.random.default_rng(semilla)
y cada caso imprime su semilla. Resultados: docs/pruebas_adversas/resultados.csv.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import http.client
import io
import json
import os
import platform
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import numpy as np

AQUI = os.path.dirname(os.path.abspath(__file__))
if AQUI not in sys.path:
    sys.path.insert(0, AQUI)

from agentes import MOTIVO_SIN_MEDIDAS  # noqa: E402
from condiciones import CONDICIONES, validar_personalizada  # noqa: E402
from coordinacion import ARISTAS  # noqa: E402
from distribucion import asignar  # noqa: E402
from modelo_red import PERIODOS  # noqa: E402
from motor import a_json, crear_sistema  # noqa: E402

DIR_INFORME = os.path.join(AQUI, "docs", "pruebas_adversas")
FIN = datetime(2026, 11, 22)          # día recomendado fijo: misma estacionalidad en cada corrida
TOL = 0.1                             # kW / kWh
SEMILLA_BASE = 7                      # semilla del simulador IoT por omisión (la del producto)
MAX_MENSAJES_FLUJO = 40
INVARIANTES = [f"I{i}" for i in range(1, 12)]
TIPOS = ("residencial", "comercial", "industrial", "servicio", "critica")
CAMPANA = {"periodos": list(PERIODOS), "rapido": False, "silencioso": False}
RESULTADOS: dict[tuple[str, str], "Resultado"] = {}
FILAS_EXTRA: list[dict] = []          # filas del CSV que no son corridas (relaciones M, API)
RENDIMIENTO: dict = {}


def periodos() -> list[str]:
    return CAMPANA["periodos"]


# ====================================================================== casos
def todas(f) -> dict:
    """Mismo factor para todos los tipos de carga."""
    return {t: f for t in TIPOS}


def horario(valor: float, horas, resto: float = 1.0) -> list[float]:
    v = np.full(24, resto, dtype=float)
    v[list(horas)] = valor
    return v.tolist()


@dataclass
class Caso:
    id: str
    familia: str
    desc: str
    condicion: str = "normal"
    personalizada: dict | Callable | None = None   # dict o función(rng) -> dict
    semilla: int = SEMILLA_BASE
    rechazos: int = 0
    falla: str = "ninguna"
    alimentador: str | None = None
    crecimiento: float | None = None
    datos: Callable | None = None       # (datos IoT, rng, registro) -> None: altera las lecturas
    agentes: Callable | None = None     # (sistema) -> None: parchea agentes (fallas persistentes)
    esperado: Callable | None = None    # (resultado) -> list[str] problemas
    escala_red: float | None = None     # M5: escala toda la red (cargas, capacidades, renovables)
    periodos: tuple | None = None       # periodos fijos (p. ej. solo 1d)
    fin: datetime | None = None         # día recomendado distinto del de la campaña

    def comando(self, periodo: str) -> str:
        return f"python pruebas_adversas.py --caso {self.id} --periodo {periodo}"


@dataclass
class Resultado:
    caso: Caso
    periodo: str
    sistema: object
    propuestas: list
    excepcion: str | None
    segundos: float
    registro: dict
    avisos: list
    invariantes: dict = field(default_factory=dict)
    esperado: tuple = ("NA", "")
    notas: list = field(default_factory=list)

    @property
    def estado(self) -> str:
        if self.excepcion:
            return "excepcion"
        rep = self.sistema.salida.reporte
        if rep is None:
            return "sin_terminar"
        return {"APROBADA": "aprobada", "ESCALADA AL OPERADOR": "escalada"}.get(rep["estado"], "rechazada")

    @property
    def lista_final(self) -> list:
        paquete = self.sistema.pizarra.leer("escenarios")
        return paquete["lista"] if paquete else []

    @property
    def lista_inicial(self) -> list:
        return self.propuestas[0]["lista"] if self.propuestas else self.lista_final

    def escenario(self, eid: str):
        return next((e for e in self.lista_inicial if e.id == eid), None)

    @property
    def propuesta(self):
        return self.propuestas[0]["esc"] if self.propuestas else None

    @property
    def propuesta_final(self) -> str:
        rep = self.sistema.salida.reporte if not self.excepcion else None
        if rep and rep.get("recomendacion"):
            return rep["recomendacion"]["escenario"]
        return "-"

    @property
    def motivo_escalamiento(self) -> str:
        d = self.sistema.pizarra.leer("decision") or {}
        return " ".join(d.get("justificacion") or []) if d.get("escenario") is None else ""


# --------------------------------------------------------------- ejecución
def _espiar_propuestas(s, propuestas: list):
    """Registra cada propuesta que C envía a G (instrumentación de la prueba, no del motor)."""
    g = s.aprobacion
    original = g.procesar

    def procesar(msg):
        dec = s.pizarra.leer("decision")
        if dec and dec.get("escenario"):
            paquete = s.pizarra.leer("escenarios")
            evals = {e["id"]: e for e in s.pizarra.leer("evaluacion")["escenarios"]}
            esc = next(e for e in paquete["lista"] if e.id == dec["escenario"])
            propuestas.append({"esc": esc, "decision": dict(dec), "ev": evals[esc.id], "evals": evals,
                               "lista": list(paquete["lista"]), "paquete": paquete})
        return original(msg)
    g.procesar = procesar


def _alterar_datos(s, fn, rng, registro):
    b = s.bus.agentes["B"]
    original = b.simulador.capturar

    def capturar(periodo, fin):
        datos = original(periodo, fin)
        registro["tiempos"] = datos["tiempos"]
        fn(datos, rng, registro)
        return datos
    b.simulador.capturar = capturar


def _escalar_red(red, k: float):
    red.capacidad_convencional_kw *= k
    red.limite_inyeccion_kw *= k
    for a in red.alimentadores:
        a.capacidad_kw *= k
        a.transformador_bt_kw *= k
    for c in red.cargas:
        c.potencia_kw *= k
    for r in red.renovables:
        r.capacidad_kw *= k


def correr(caso: Caso, periodo: str) -> Resultado:
    rng = np.random.default_rng(caso.semilla)
    cond = caso.personalizada(rng) if callable(caso.personalizada) else caso.personalizada
    d = tempfile.mkdtemp(prefix="ea_adv_")
    try:
        s = crear_sistema(periodo=periodo, condicion=caso.condicion, modo="auto", rechazos=caso.rechazos,
                          falla=caso.falla, semilla=caso.semilla, salida=d,
                          ruta_memoria=os.path.join(d, "memoria.json"), verbose=False, fin=caso.fin or FIN,
                          alimentador=caso.alimentador, crecimiento_pct=caso.crecimiento,
                          condicion_personalizada=cond)
        propuestas, registro = [], {}
        _espiar_propuestas(s, propuestas)
        if caso.escala_red:
            _escalar_red(s.red, caso.escala_red)
        if caso.datos:
            _alterar_datos(s, caso.datos, rng, registro)
        if caso.agentes:
            caso.agentes(s)
        excepcion = None
        t0 = time.perf_counter()
        with warnings.catch_warnings(record=True) as avisos, contextlib.redirect_stdout(io.StringIO()):
            warnings.simplefilter("always")
            try:
                s.iniciar()
            except Exception as e:        # el flujo se rompió: lo evalúa I9
                excepcion = f"{type(e).__name__}: {e}"
        seg = time.perf_counter() - t0
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return Resultado(caso, periodo, s, propuestas, excepcion, seg, registro,
                     [f"{a.category.__name__}: {a.message}" for a in avisos])


def obtener(caso: Caso, periodo: str) -> Resultado:
    clave = (caso.id, periodo)
    if clave not in RESULTADOS:
        r = correr(caso, periodo)
        evaluar(r)
        RESULTADOS[clave] = r
        if not CAMPANA["silencioso"]:
            fallas = [i for i, (e, _) in r.invariantes.items() if e == "FALLA"]
            extra = "" if r.esperado[0] != "FALLA" else " esperado:FALLA"
            print(f"    [{caso.id} | {periodo} | semilla={caso.semilla}] {r.estado} {r.propuesta_final}"
                  + (f" FALLA:{','.join(fallas)}" if fallas else "") + extra, flush=True)
    return RESULTADOS[clave]


def periodos_de(caso: Caso) -> list[str]:
    return list(caso.periodos) if caso.periodos else periodos()


# ================================================================== oráculos
def _escenarios_evaluados(r: Resultado) -> list:
    vistos, out = set(), []
    for lista in [p["lista"] for p in r.propuestas] + [r.lista_final]:
        for e in lista:
            if id(e) not in vistos:
                vistos.add(id(e))
                out.append(e)
    return out


def _evaluaciones(r: Resultado) -> list[dict]:
    evs = [ev for p in r.propuestas for ev in p["evals"].values()]
    final = r.sistema.pizarra.leer("evaluacion")
    if final:
        evs += final["escenarios"]
    return evs


def _resultado(fallas: list[str], aplica: bool = True) -> tuple[str, str]:
    if not aplica:
        return ("NA", "")
    return ("FALLA", "; ".join(fallas[:4]) + (f" (+{len(fallas) - 4})" if len(fallas) > 4 else "")) if fallas \
        else ("OK", "")


def i1_balance(r):
    fallas = []
    for e in _escenarios_evaluados(r):
        for banda, d in (("P50", e.distribucion), ("P90", e.distribucion_p90)):
            if d is None:
                continue
            for k in d.claves:
                lado = d.renovable_usada[k] + d.convencional[k] + d.recorte[k] + d.no_servido[k]
                err = np.abs(d.demanda[k] - lado)
                if err.max() > TOL:
                    h = int(err.argmax())
                    fallas.append(f"{e.id} {banda} {k} {h:02d}h |Δ|={err.max():.2f} kW")
    return _resultado(fallas, bool(_escenarios_evaluados(r)))


def i2_capacidades(r):
    fallas = []
    for p in r.propuestas:
        e = p["esc"]
        d, lim = e.distribucion, e.distribucion.limites
        if d.requerido_subestacion.max() > lim.subestacion_kw + TOL:
            fallas.append(f"{e.id} subestación {d.requerido_subestacion.max():.1f} > {lim.subestacion_kw:.1f} kW")
        for k in d.claves:
            if d.flujo_cabecera[k].max() > lim.alimentador_kw[k] + TOL:
                fallas.append(f"{e.id} {k} cabecera {d.flujo_cabecera[k].max():.1f} > {lim.alimentador_kw[k]:.1f} kW")
            if np.isfinite(lim.transformador_bt_kw[k]) and d.flujo_trafo_bt[k].max() > lim.transformador_bt_kw[k] + TOL:
                fallas.append(f"{e.id} {k} trafo BT {d.flujo_trafo_bt[k].max():.1f} > {lim.transformador_bt_kw[k]:.1f} kW")
    return _resultado(fallas, bool(r.propuestas))


def critica_insuficiente(r) -> float | None:
    """kWh de carga crítica que la red no puede servir aunque se recorte todo lo demás (P50).
    None si no hay contexto (el flujo no llegó a E)."""
    paquete = r.sistema.pizarra.leer("escenarios")
    if not paquete:
        return None
    ctx, red = paquete["contexto"], r.sistema.red
    criticas = {c.nombre for c in red.cargas if c.critica}
    solo = {n: (v if n in criticas else np.zeros(24)) for n, v in ctx.cargas.items()}
    d = asignar(red, solo, ctx.renovables, ctx.limites, ctx.tarifas)
    return float(d.total("no_servido").sum())


def i3_criticas(r):
    red = r.sistema.red
    criticas = {c.nombre for c in red.cargas if c.critica}
    fallas = []
    for p in r.propuestas:
        e = p["esc"]
        for n in criticas:
            rec = float(e.distribucion.recorte_por_carga.get(n, np.zeros(24)).sum())
            ns = float(e.distribucion.no_servido_por_carga.get(n, np.zeros(24)).sum())
            if rec > TOL or ns > TOL:
                fallas.append(f"{e.id}: {n} recorte {rec:.1f} kWh, sin servir {ns:.1f} kWh")
    insuf = critica_insuficiente(r)
    if insuf is not None and insuf > TOL:
        if r.propuestas:
            fallas.append(f"la red no alcanza para la carga crítica ({insuf:.0f} kWh) y aun así propuso "
                          f"{r.propuestas[0]['esc'].id}")
        elif r.estado != "escalada":
            fallas.append(f"carga crítica sin cubrir ({insuf:.0f} kWh) y el flujo no escaló ({r.estado})")
        elif "crític" not in r.motivo_escalamiento.lower():
            fallas.append(f"escaló sin decir que la carga crítica no alcanza ({insuf:.0f} kWh): "
                          f"'{r.motivo_escalamiento[:90]}'")
    return _resultado(fallas, bool(r.propuestas) or (insuf is not None and insuf > TOL))


def i4_propuestas_sin_deficit(r):
    fallas = [f"{p['esc'].id} déficit P50 {p['esc'].metricas['deficit_kwh']} kWh"
              for p in r.propuestas if p["esc"].metricas["deficit_kwh"] != 0]
    return _resultado(fallas, bool(r.propuestas))


def i5_politica_recorte(r):
    lista = r.lista_inicial
    conts = [e for e in lista if e.estrategia == "contingencia"]
    fallas = []
    p0 = r.propuestas[0] if r.propuestas else None
    if not conts:
        if p0 and p0["esc"].metricas["recorte_kwh"] > 0:
            fallas.append(f"{p0['esc'].id} recorta sin escenario de contingencia")
        return _resultado(fallas, bool(fallas))
    minimo = next((e for e in conts if e.base_planificacion == "P50"), None)
    robusto = next((e for e in conts if e.base_planificacion == "P90"), None)
    if minimo is not None and robusto is not None:
        extra = max(0.0, robusto.metricas["recorte_kwh"] - minimo.metricas["recorte_kwh"])
        for e in (minimo, robusto):
            dado = e.metricas.get("recorte_adicional_robustez_kwh")
            if dado is None or abs(dado - extra) > TOL:
                fallas.append(f"{e.id} recorte_adicional_robustez {dado} ≠ P90−P50 {extra:.1f}")
    if p0 and p0["esc"].estrategia == "contingencia":
        central = min((e.metricas["deficit_kwh"] for e in lista if e.estrategia != "contingencia"), default=None)
        rec = p0["esc"].metricas["recorte_kwh"]
        if central is not None and rec > central * 1.05 + TOL:
            fallas.append(f"propuesta inicial {p0['esc'].id} recorta {rec:.1f} > déficit central "
                          f"{central:.1f} + 5 %")
        dado = p0["decision"].get("recorte_adicional_robustez_kwh")
        if minimo is not None and robusto is not None and (dado is None or abs(dado - extra) > TOL):
            fallas.append(f"la decisión informa recorte adicional {dado} ≠ {extra:.1f}")
    return _resultado(fallas)


def i6_renovables(r):
    fallas = []
    for e in _escenarios_evaluados(r):
        for banda, d in (("P50", e.distribucion), ("P90", e.distribucion_p90)):
            if d is None:
                continue
            lim = d.limites
            usada, disp = d.total("renovable_usada"), d.total("renovable_disponible")
            if (usada - disp).max() > TOL:
                fallas.append(f"{e.id} {banda} usada > disponible ({(usada - disp).max():.1f} kW)")
            env, tope = {}, {}
            for k in d.claves:
                servida = d.demanda[k] - d.recorte[k] - d.no_servido[k]
                local = np.minimum(d.renovable_disponible[k], servida)
                env[k] = d.renovable_disponible[k] - local - d.renovable_desperdiciada[k]
                tope[k] = min(lim.inyeccion_kw, lim.alimentador_kw[k], lim.transformador_bt_kw[k])
                if d.renovable_desperdiciada[k].min() < -TOL:
                    fallas.append(f"{e.id} {banda} {k} desperdiciada negativa")
                if env[k].min() < -TOL:
                    fallas.append(f"{e.id} {banda} {k} inyección negativa ({env[k].min():.1f} kW)")
                if env[k].max() > lim.inyeccion_kw + TOL:
                    fallas.append(f"{e.id} {banda} {k} inyecta {env[k].max():.1f} > límite {lim.inyeccion_kw:.0f} kW")
            # Regla 2: no se desperdicia renovable mientras la red compra convencional que podía cubrir
            for k in d.claves:
                for h in np.where(d.renovable_desperdiciada[k] > TOL)[0]:
                    if d.convencional[k][h] > TOL:
                        fallas.append(f"{e.id} {banda} {k} {h:02d}h desperdicia "
                                      f"{d.renovable_desperdiciada[k][h]:.1f} kW y compra convencional local")
                    for j in d.claves:
                        if j != k and d.convencional[j][h] > TOL and env[k][h] < tope[k] - TOL:
                            fallas.append(f"{e.id} {banda} {h:02d}h {k} desperdicia "
                                          f"{d.renovable_desperdiciada[k][h]:.1f} kW, {j} compra "
                                          f"{d.convencional[j][h]:.1f} kW y la inyección ({env[k][h]:.0f}) "
                                          f"no llega al tope ({tope[k]:.0f})")
    return _resultado(fallas, bool(_escenarios_evaluados(r)))


def _numeros(obj, ruta=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _numeros(v, f"{ruta}.{k}")
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _numeros(v, ruta)
    elif isinstance(obj, np.ndarray):
        for v in obj.ravel():
            yield ruta, float(v)
    elif isinstance(obj, (bool, np.bool_)) or obj is None:
        return
    elif isinstance(obj, (int, float, np.integer, np.floating)):
        yield ruta, float(obj)


def i7_metricas(r):
    import servidor
    fallas = []
    escs = _escenarios_evaluados(r)
    for e in escs:
        for nombre, m in (("P50", e.metricas), ("P90", e.metricas_p90)):
            for ruta, v in _numeros(m):
                if not np.isfinite(v):
                    fallas.append(f"{e.id} {nombre}{ruta} = {v}")
                elif v < -1e-9 and "margen" not in ruta:
                    fallas.append(f"{e.id} {nombre}{ruta} = {v} (negativo)")
    analisis = r.sistema.pizarra.leer("analisis") or {}
    for clave in ("consumo_total_kwh", "pico_kw", "mape_pct", "generacion_renovable_kwh"):
        v = analisis.get(clave)
        if v is not None and not np.isfinite(v):
            fallas.append(f"análisis.{clave} = {v}")
    for ev in (r.sistema.bitacora.eventos):
        if ev["tipo"] == "falla" and ("division" in ev["error"] or "divide" in ev["error"]):
            fallas.append(f"división entre cero en {ev['nodo']}: {ev['error']}")
    for a in r.avisos:
        if "divide by zero" in a or "invalid value" in a:
            fallas.append(f"aviso numérico: {a}")
    if not r.excepcion:
        try:
            json.dumps(servidor.vista("prueba00000", r.sistema), default=a_json, allow_nan=False)
            json.dumps(r.sistema.salida.reporte, default=a_json, allow_nan=False)
        except Exception as e:      # vista de la API no serializable
            fallas.append(f"vista/reporte no serializable: {type(e).__name__}: {e}")
    return _resultado(fallas, bool(escs) or not r.excepcion)


_N = r"(-?[\d,]+(?:\.\d+)?)"


def _num(t: str) -> float:
    return float(t.replace(",", ""))


def _tol(t: str) -> float:
    """El texto redondea: '1,234' admite ±0.5; '12.34' admite ±0.1 (la tolerancia del oráculo)."""
    return 0.5 + 1e-6 if "." not in t else TOL + 1e-9


def _comparar(fallas, etiqueta, texto_num, esperado):
    if esperado is None:
        fallas.append(f"{etiqueta}: el texto cita {texto_num} pero la métrica no existe")
    elif abs(_num(texto_num) - esperado) > _tol(texto_num):
        fallas.append(f"{etiqueta}: texto {texto_num} ≠ métrica {esperado}")


def verificar_justificacion(p: dict) -> list[str]:
    esc, dec, ev, lista = p["esc"], p["decision"], p["ev"], p["lista"]
    m = esc.metricas
    base = next((e for e in lista if e.id == "S0"), None) or esc
    mb = base.metricas
    texto = "\n".join(dec["justificacion"])
    f: list[str] = []
    simples = [
        (rf"renovables cubren {_N} %", m["renovable_pct"]),
        (rf"\({_N} kWh renovables sin aprovechar", m["renovable_desperdiciada_kwh"]),
        (rf"Regla 3: costo {_N} MXN", m["costo_mxn"]),
        (rf"Regla 3: costo [\d,.]+ MXN \(ahorro de {_N} MXN", mb["costo_mxn"] - m["costo_mxn"]),
        (rf"Regla 3: costo [\d,.]+ MXN \(sobrecosto de {_N} MXN", m["costo_mxn"] - mb["costo_mxn"]),
        (rf"riesgo máximo {_N}", ev["riesgo_max"]),
        (rf"margen mínimo P90 {_N} %", m.get("margen_red_min_p90_pct")),
        (rf"Recorta {_N} kWh más que el plan mínimo", m.get("recorte_adicional_robustez_kwh")),
        (rf"para evitar {_N} kWh de déficit", m.get("deficit_p90_evitado_kwh")),
        (rf"quedarían {_N} kWh de déficit", m.get("deficit_p90_kwh")),
        (rf"recorta {_N} kWh adicionales", m.get("recorte_adicional_robustez_kwh")),
        (rf"pérdidas técnicas {_N} kWh", m["perdidas_kwh"]),
        (rf"quedarían {_N} kWh sin servir", m["deficit_kwh"]),
        (rf"\({_N} kWh de déficit en la banda", m.get("deficit_planificado_kwh")),
        (rf"recorte parcial de {_N} kWh solo en cargas", m["recorte_kwh"]),
        (rf"bastarían {_N} kWh", m.get("deficit_previo_kwh")),
        (rf"Sin cambios habría {_N} kWh de déficit", mb["deficit_kwh"]),
    ]
    for patron, valor in simples:
        for t in re.findall(patron, texto):
            _comparar(f, patron.split(" ")[0][:28], t, valor)
    for a, b in re.findall(rf"margen mínimo {_N} % en la subestación,? (?:y )?{_N} % en la red", texto):
        _comparar(f, "margen subestación", a, m["margen_min_pct"])
        _comparar(f, "margen red", b, m["margen_red_min_pct"])
    for k, mg, uso, rec in re.findall(rf"(A\d) margen {_N} %, uso máx\. {_N} %(?:, recorte {_N} kWh)?", texto):
        a = m["alimentadores"].get(k)
        if a is None:
            f.append(f"cita el alimentador {k}, que no existe")
            continue
        _comparar(f, f"{k} margen", mg, a["margen_min_pct"])
        _comparar(f, f"{k} uso", uso, a["utilizacion_max_pct"])
        if rec:
            _comparar(f, f"{k} recorte", rec, a["recorte_kwh"])
    lista_cargas = re.search(r"por prioridad \((.*?)\)\. ", texto)
    if lista_cargas:
        for nombre, kwh in re.findall(rf"([^,]+?) {_N} kWh", lista_cargas.group(1)):
            _comparar(f, f"recorte {nombre.strip()}", kwh, m["recorte_por_carga_kwh"].get(nombre.strip()))
    # Afirmaciones cualitativas
    if "sin déficit de suministro" in texto and m["deficit_kwh"] > 0:
        f.append(f"dice 'sin déficit' con {m['deficit_kwh']} kWh de déficit")
    if "Robustez: sin déficit en el escenario desfavorable P90" in texto and m.get("deficit_p90_kwh", 0) > 0:
        f.append(f"dice 'sin déficit en P90' con {m['deficit_p90_kwh']} kWh")
    if "ninguna estrategia viable es robusta" in texto:
        robustas = [e.id for e in lista if e.metricas["deficit_kwh"] == 0 and e.robusto]
        if robustas:
            f.append(f"dice 'ninguna estrategia viable es robusta' pero {robustas[:3]} lo son")
    if "conservan el 100 %" in texto or "cargas críticas protegidas" in texto:
        crit = m["no_servido_por_tipo_kwh"].get("critica", 0) + m["recorte_por_tipo_kwh"].get("critica", 0)
        if crit > TOL:
            f.append(f"dice que las críticas conservan el 100 % con {crit} kWh sin servir o recortados")
    cat = re.search(r"riesgo máximo \d+ \((\w+)\)", texto)
    if cat and cat.group(1) != ev["categoria_max"]:
        f.append(f"categoría de riesgo {cat.group(1)} ≠ {ev['categoria_max']}")
    return f


def i8_justificacion(r):
    fallas = []
    for p in r.propuestas:
        fallas += [f"{p['esc'].id}: {x}" for x in verificar_justificacion(p)]
    if r.estado == "escalada" and MOTIVO_SIN_MEDIDAS in r.motivo_escalamiento:
        viables = [e.id for e in r.lista_final if e.metricas["deficit_kwh"] == 0]
        if viables and not r.sistema.aprobacion.historial:
            fallas.append(f"escaló por falta de medidas con escenarios viables {viables[:3]}")
    return _resultado(fallas, bool(r.propuestas) or r.estado == "escalada")


def costo_esperado(r) -> int | None:
    """36 sin rechazos, +24 por rechazo; si el último rechazo agota el máximo, C escala sin
    volver a deliberar (-13: C->E, E->F, F->C)."""
    if any(e["tipo"] == "falla" for e in r.sistema.bitacora.eventos):
        return None
    rech = sum(not h["aprobado"] for h in r.sistema.aprobacion.historial)
    return 36 + 24 * rech - (13 if "máximo de" in r.motivo_escalamiento else 0)


def i9_flujo(r):
    s = r.sistema
    fallas = []
    if r.excepcion:
        fallas.append(f"el flujo se rompió: {r.excepcion}")
    elif r.estado not in ("aprobada", "escalada"):
        fallas.append(f"estado final {r.estado}")
    if s.bus.enviados >= MAX_MENSAJES_FLUJO:
        fallas.append(f"{s.bus.enviados} mensajes (≥ {MAX_MENSAJES_FLUJO})")
    msgs = [e for e in s.bitacora.eventos if e["tipo"] == "mensaje"]
    for e in msgs:
        enlace = (e["emisor"], e["receptor"])
        if enlace not in ARISTAS and not (e["performativa"] == "FAILURE" and e["receptor"] == "C"):
            fallas.append(f"enlace fuera del grafo {enlace}")
    if msgs and (msgs[0]["emisor"], msgs[0]["receptor"]) != ("A", "B"):
        fallas.append("el recorrido no empieza en A->B")
    if not r.excepcion and msgs and (msgs[-1]["emisor"], msgs[-1]["receptor"]) != ("G", "H"):
        fallas.append("el recorrido no termina en G->H")
    suma = sum(ARISTAS.get((e["emisor"], e["receptor"]), 0) for e in msgs)
    if suma != s.bus.costo_total:
        fallas.append(f"costo {s.bus.costo_total} ≠ suma de pesos {suma}")
    esperado = costo_esperado(r)
    if esperado is not None and not r.excepcion and s.bus.costo_total != esperado:
        fallas.append(f"costo {s.bus.costo_total} ≠ esperado {esperado}")
    return _resultado(fallas)


def huella_resultado(r) -> str:
    s = r.sistema
    datos = {"estado": r.estado, "excepcion": r.excepcion, "costo": s.bus.costo_total,
             "recorrido": s.bus.recorrido,
             "propuestas": [(p["esc"].id, p["esc"].metricas, p["decision"]["justificacion"]) for p in r.propuestas],
             "escenarios": [(e.id, e.metricas) for e in r.lista_final]}
    return json.dumps(datos, default=a_json, sort_keys=True)


def i10_determinismo(r):
    otra = correr(r.caso, r.periodo)
    a, b = huella_resultado(r), huella_resultado(otra)
    if a == b:
        return ("OK", "")
    i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return ("FALLA", f"dos corridas con semilla {r.caso.semilla} difieren desde '{a[max(0, i - 40):i + 40]}'")


def i11_riesgo_insuficiencia(r):
    fallas = []
    evs = _evaluaciones(r)
    for ev in evs:
        if ev["deficit_p90_kwh"] > 0:
            ins = next(x for x in ev["riesgos"] if x["riesgo"] == "Insuficiencia de capacidad")
            if ins["prob"] < 4:
                fallas.append(f"{ev['id']} déficit P90 {ev['deficit_p90_kwh']} kWh con probabilidad {ins['prob']}")
    return _resultado(fallas, bool(evs))


ORACULOS = {"I1": i1_balance, "I2": i2_capacidades, "I3": i3_criticas, "I4": i4_propuestas_sin_deficit,
            "I5": i5_politica_recorte, "I6": i6_renovables, "I7": i7_metricas, "I8": i8_justificacion,
            "I9": i9_flujo, "I10": i10_determinismo, "I11": i11_riesgo_insuficiencia}


def evaluar(r: Resultado):
    for inv, fn in ORACULOS.items():
        try:
            r.invariantes[inv] = fn(r)
        except Exception as e:      # el oráculo no pudo evaluarse: se reporta como falla
            r.invariantes[inv] = ("FALLA", f"oráculo {inv} no evaluable: {type(e).__name__}: {e}")
    problemas = []
    if r.caso.esperado:
        try:
            problemas = r.caso.esperado(r)
        except Exception as e:
            problemas = [f"esperado no evaluable: {type(e).__name__}: {e}"]
        r.esperado = ("FALLA", "; ".join(problemas)) if problemas else ("OK", "")


# ====================================================== expectativas por familia
def _base(r) -> list[str]:
    if r.excepcion:
        return [f"el flujo se rompió: {r.excepcion}"]
    if r.estado not in ("aprobada", "escalada"):
        return [f"estado {r.estado}"]
    return []


def esp_alta(estado: str | None = None):
    def f(r):
        p = _base(r)
        if p:
            return p
        s0 = r.escenario("S0")
        if estado and r.estado != estado:
            p.append(f"se esperaba {estado}, quedó {r.estado}")
        if s0 is not None and s0.metricas["deficit_kwh"] > 0 and r.propuesta is not None \
                and r.propuesta.estrategia != "contingencia" and r.propuesta.metricas["deficit_kwh"] > 0:
            p.append("S0 con déficit y la propuesta no aplica medidas")
        if r.propuesta is not None:
            r.notas.append(f"recorte {r.propuesta.metricas['recorte_kwh']:.0f} kWh, "
                           f"déficit S0 {s0.metricas['deficit_kwh']:.0f} kWh" if s0 else "")
        return p
    return f


CASO_NORMAL = Caso("REF.normal", "REF", "Referencia: operación normal (comparación de F2)")
CASO_MIERCOLES = Caso("REF.normal-jueves", "REF", "Referencia: operación normal recomendando un jueves "
                      "(el historial termina en miércoles)", fin=datetime(2026, 11, 19))


def esp_baja(r):
    p = _base(r)
    if p:
        return p
    prop, s0 = r.propuesta, r.escenario("S0")
    if prop is None:
        return [f"sin propuesta con demanda baja ({r.estado}: {r.motivo_escalamiento[:80]})"]
    m = prop.metricas
    if m["recorte_kwh"] > 0:
        p.append(f"recorte innecesario de {m['recorte_kwh']} kWh")
    if m["desplazado_kwh"] > 0 and s0 is not None:
        mejora_ren = m["renovable_pct"] - s0.metricas["renovable_pct"]
        ahorro = s0.metricas["costo_mxn"] - m["costo_mxn"]
        if mejora_ren < 0.1 and ahorro < 10:
            p.append(f"desplaza {m['desplazado_kwh']} kWh sin mejorar renovable ({mejora_ren:+.2f} pp) "
                     f"ni costo ({ahorro:+.0f} MXN)")
    if s0 is not None and s0.metricas["margen_red_min_pct"] < 10:
        p.append(f"margen de S0 {s0.metricas['margen_red_min_pct']} % < 10 %")
    ref = obtener(CASO_NORMAL, r.periodo)
    if ref.propuesta is not None and m["costo_mxn"] >= ref.propuesta.metricas["costo_mxn"]:
        p.append(f"costo {m['costo_mxn']:.0f} ≥ costo en normal {ref.propuesta.metricas['costo_mxn']:.0f} MXN")
    r.notas.append(f"renovable desperdiciada {m['renovable_desperdiciada_kwh']:.0f} kWh, "
                   f"renovable {m['renovable_pct']} %, costo {m['costo_mxn']:.0f} MXN")
    return p


def esp_renovable(r):
    p = _base(r)
    if p:
        return p
    if r.propuesta is not None:
        m = r.propuesta.metricas
        r.notas.append(f"renovable {m['renovable_pct']} %, desperdiciada {m['renovable_desperdiciada_kwh']:.0f} kWh, "
                       f"recorte {m['recorte_kwh']:.0f} kWh")
    return p


def esp_fuera(fuentes: tuple):
    def f(r):
        p = _base(r)
        if p:
            return p
        red = r.sistema.red
        for e in r.lista_inicial:          # cada alimentador tiene una sola fuente
            for fuente in red.renovables:
                if fuente.tipo in fuentes:
                    k = red.alimentador(fuente.alimentador).clave
                    disp = float(e.distribucion.renovable_disponible[k].sum())
                    if disp > TOL:
                        p.append(f"{e.id}: {k} conserva {disp:.0f} kWh de {fuente.tipo} fuera de servicio")
        if len(fuentes) == 2 and r.propuesta is not None and r.propuesta.metricas["renovable_usada_kwh"] > TOL:
            p.append(f"usa {r.propuesta.metricas['renovable_usada_kwh']} kWh renovables sin fuentes")
        return esp_renovable(r) + p
    return f


def deficit_no_previsto(r, factor_p10: float = 0.5) -> tuple[float, float]:
    """Aplica el plan propuesto con la renovable REAL = factor × P10 (pronóstico optimista).
    Devuelve (kWh sin servir no previstos, kWh renovables que faltaron frente a P50)."""
    p = r.propuestas[0]
    esc, ctx = p["esc"], p["paquete"]["contexto"]
    real = {n: v * factor_p10 for n, v in ctx.renovables_p10.items()}
    d = asignar(r.sistema.red, esc.cargas, real, ctx.limites, ctx.tarifas, recorte_fijo=esc.recorte or None)
    falto = sum(float(v.sum()) for v in ctx.renovables.values()) - sum(float(v.sum()) for v in real.values())
    return float(d.total("no_servido").sum()), falto


def esp_optimista(r):
    p = _base(r)
    if p or not r.propuestas:
        return p or ["sin propuesta"]
    ns, falto = deficit_no_previsto(r)
    r.notas.append(f"renovable real = 0.5×P10: faltan {falto:.0f} kWh renovables; déficit no previsto "
                   f"{ns:.0f} kWh con el plan {r.propuestas[0]['esc'].id}")
    return p


def esp_cobertura_renovable(r):
    """La renovable del último día del historial cae al 30 %: la validación fuera de muestra de
    las bandas (ISO/IEC 5338) debería advertirlo."""
    p = _base(r)
    if p:
        return p
    traz = r.sistema.pizarra.leer("evaluacion")["trazabilidad"]
    analisis = r.sistema.pizarra.leer("analisis")
    if analisis["dias"] < 3:
        r.notas.append("sin validación de bandas con menos de 3 días")
        return p
    r.notas.append(f"cobertura {traz['cobertura_bandas_pct']} %, alerta {traz['alerta_calibracion_bandas']}, "
                   f"lecturas validadas {traz['lecturas_validacion_bandas']}")
    if not traz["alerta_calibracion_bandas"]:
        p.append(f"la renovable real del último día quedó al 30 % (bajo P10) y la cobertura de bandas "
                 f"({traz['cobertura_bandas_pct']} % con {traz['lecturas_validacion_bandas']} lecturas, solo de "
                 "demanda) no lo detecta")
    return p


def esp_estado(estado: str, motivo: str | None = None, costo: int | None = None):
    def f(r):
        p = _base(r)
        if p:
            return p
        if r.estado != estado:
            p.append(f"se esperaba {estado}, quedó {r.estado}")
        if motivo and motivo not in r.motivo_escalamiento:
            p.append(f"motivo '{r.motivo_escalamiento[:80]}' no menciona '{motivo}'")
        if costo is not None and r.sistema.bus.costo_total != costo:
            p.append(f"costo {r.sistema.bus.costo_total} ≠ {costo}")
        r.notas.append(f"costo {r.sistema.bus.costo_total}, {r.sistema.bus.enviados} mensajes"
                       + (f"; motivo: {r.motivo_escalamiento[:70]}" if r.motivo_escalamiento else ""))
        return p
    return f


# -------------------------------------------------------------- datos IoT (F6)
CARGA_SENSOR = "Zona comercial centro"


def faltantes(frac: float):
    def f(datos, rng, reg):
        n = 0
        for grupo in ("consumo", "generacion"):
            for serie in datos[grupo].values():
                m = rng.random(len(serie)) < frac
                serie[m] = np.nan
                n += int(m.sum())
        reg["nan"] = n
    return f


def congelado(datos, rng, reg):
    x = datos["consumo"][CARGA_SENSOR]
    k = min(168, len(x))
    valor = x[-k] if np.isfinite(x[-k]) else np.nanmean(x)
    x[-k:] = valor
    reg["marcas"] = [(CARGA_SENSOR, i) for i in range(len(x) - k, len(x))]


def negativas(datos, rng, reg):
    reg["marcas"] = []
    for nombre, x in datos["consumo"].items():
        idx = np.where(rng.random(len(x)) < 0.05)[0]
        x[idx] = -np.abs(x[idx])
        reg["marcas"] += [(nombre, int(i)) for i in idx]


def picos(datos, rng, reg):
    reg["marcas"] = []
    for nombre, x in datos["consumo"].items():
        idx = np.where(rng.random(len(x)) < 0.01)[0]
        x[idx] *= 50
        reg["marcas"] += [(nombre, int(i)) for i in idx]


def reloj(datos, rng, reg):
    x = datos["consumo"][CARGA_SENSOR]
    x[:] = np.roll(x, 1)            # el medidor reporta con una hora de retraso
    reg["carga"] = CARGA_SENSOR


def sensor_fuera(datos, rng, reg):
    datos["consumo"][CARGA_SENSOR][:] = np.nan


def apagon(datos, rng, reg):
    """Último día del historial con todos los medidores en cero (apagón): ejercita el MAPE."""
    reg["marcas"] = []
    for nombre, x in datos["consumo"].items():
        x[-24:] = 0.0
        reg["marcas"] += [(nombre, i) for i in range(len(x) - 24, len(x))]


def renovable_ultimo_dia(datos, rng, reg):
    for g in datos["generacion"].values():
        g[-24:] *= 0.3


def _deteccion(r) -> tuple[int, int]:
    marcas = r.registro.get("marcas", [])
    tiempos = r.registro["tiempos"]
    detectadas = {(a["carga"], a["fecha"]) for a in r.sistema.pizarra.leer("analisis")["anomalias"]}
    aciertos = sum((c, tiempos[i].isoformat(timespec="minutes")) in detectadas for c, i in marcas)
    return aciertos, len(marcas)


def esp_datos(minimo_detectado: float | None = None, nan: bool = False):
    def f(r):
        p = _base(r)
        if p:
            return p
        an = r.sistema.pizarra.leer("analisis")
        if nan:
            interp = r.sistema.pizarra.leer("datos")["lecturas_perdidas_interpoladas"]
            r.notas.append(f"{r.registro['nan']} NaN inyectados, {interp} interpolados")
            if interp < r.registro["nan"]:
                p.append(f"interpoló {interp} < {r.registro['nan']} faltantes")
        if r.registro.get("marcas"):
            ok, total = _deteccion(r)
            pct = ok / total * 100 if total else 100.0
            r.notas.append(f"D marcó {ok} de {total} lecturas alteradas ({pct:.0f} %)")
            if minimo_detectado is not None and pct < minimo_detectado:
                p.append(f"D marcó solo {pct:.0f} % de las lecturas alteradas (< {minimo_detectado:.0f} %)")
        for n, v in an["pronostico_cargas"].items():
            if np.min(v) < 0:
                p.append(f"pronóstico negativo en {n} ({np.min(v):.0f} kW)")
            b = an["bandas_cargas"][n]
            if np.any(b["p10"] > b["p50"] + 1e-6) or np.any(b["p50"] > b["p90"] + 1e-6):
                p.append(f"bandas desordenadas en {n} (P10 ≤ P50 ≤ P90 no se cumple)")
        return p
    return f


def esp_historial_corto(r):
    p = _base(r)
    if p:
        return p
    an = r.sistema.pizarra.leer("analisis")
    traz = r.sistema.pizarra.leer("evaluacion")["trazabilidad"]
    if an["mape_pct"] is not None:
        p.append(f"MAPE {an['mape_pct']} con 1 día (debería ser 'sin datos')")
    if traz["cobertura_bandas_pct"] is not None:
        p.append("cobertura con 1 día (debería ser 'sin datos')")
    ctx = r.sistema.pizarra.leer("escenarios")["contexto"]
    p10, p50, p90 = (sum(getattr(ctx, c).values()) for c in ("cargas_p10", "cargas", "cargas_p90"))
    if np.any(p10 > p50 + 1e-6) or np.any(p50 > p90 + 1e-6):
        p.append("bandas desordenadas (P10 ≤ P50 ≤ P90 no se cumple)")
    ancho = float(np.max((p90 - p10) / np.maximum(p50, 1e-6)) * 100)
    ref = obtener(CASO_NORMAL, "1m") if "1m" in PERIODOS else None
    ctx_ref = ref.sistema.pizarra.leer("escenarios")["contexto"]
    a_ref = float(np.max((sum(ctx_ref.cargas_p90.values()) - sum(ctx_ref.cargas_p10.values()))
                         / np.maximum(sum(ctx_ref.cargas.values()), 1e-6)) * 100)
    r.notas.append(f"ancho máximo de la banda P10-P90 de demanda: {ancho:.1f} % con 1 día vs {a_ref:.1f} % con 1 mes")
    if not 5 <= ancho <= 100:
        p.append(f"ancho de banda {ancho:.1f} % fuera de [5, 100] %")
    if ancho < a_ref:   # con menos historial la incertidumbre no puede ser menor
        p.append(f"con 1 día la banda P10-P90 ({ancho:.1f} %) es más estrecha que con 1 mes ({a_ref:.1f} %): "
                 "subestima la incertidumbre")
    return p


# --------------------------------------------------------------- agentes (F7)
def falla_persistente(nodo: str):
    def f(s):
        def procesar(msg):
            raise RuntimeError(f"falla persistente simulada en {nodo}")
        s.bus.agentes[nodo].procesar = procesar
    return f


def falla_una_vez(*nodos: str):
    def f(s):
        for nodo in nodos:
            ag = s.bus.agentes[nodo]
            original = ag.procesar
            estado = {"fallo": False}

            def procesar(msg, original=original, estado=estado, nodo=nodo):
                if not estado["fallo"]:
                    estado["fallo"] = True
                    raise RuntimeError(f"falla simulada (una vez) en {nodo}")
                return original(msg)
            ag.procesar = procesar
    return f


def rechazo_pidiendo_seguridad(s):
    g = s.aprobacion
    original = g._registrar

    def registrar(escenario, aprobado, motivo, conv, operador=""):
        if not aprobado:
            motivo = "Pido más seguridad: plan robusto ante P90"
        return original(escenario, aprobado, motivo, conv, operador)
    g._registrar = registrar


def _f_d_luego_f(s):
    s.bus.agentes["F"].fallar = True


# ------------------------------------------------------------- catálogo de casos
_CALOR = {"residencial": 1.80, "comercial": 1.60, "critica": 1.10}
_VESP = range(17, 23)


def _nubes(rng):
    solar = np.ones(24)
    solar[8:18] = rng.uniform(0, 1, 10)
    return {"desc": "Intermitencia solar (nubes)", "renovable": {"solar": solar.tolist()}}


def _viento(rng):
    return {"desc": "Eólica volátil", "renovable": {"eolica": rng.uniform(0, 1.5, 24).tolist()}}


CASOS: dict[str, list[Caso]] = {
    "F1": [
        Caso("F1.crec+30", "F1", "Crecimiento uniforme +30 %", personalizada={"demanda": todas(1.3)}, esperado=esp_alta()),
        Caso("F1.crec+60", "F1", "Crecimiento uniforme +60 %", personalizada={"demanda": todas(1.6)}, esperado=esp_alta()),
        Caso("F1.crec+100", "F1", "Crecimiento uniforme +100 %", personalizada={"demanda": todas(2.0)}, esperado=esp_alta()),
        Caso("F1.pico-19a22", "F1", "Residencial y comercial al doble de 19:00 a 22:00",
             personalizada={"demanda": {"residencial": 2.0, "comercial": 2.0}, "horas": [19, 20, 21]}, esperado=esp_alta()),
        Caso("F1.alta-A2", "F1", "Demanda alta solo en A2 (residencial y comercial ×2): satura el trafo BT",
             personalizada={"demanda": {"residencial": 2.0, "comercial": 2.0}}, esperado=esp_alta()),
        Caso("F1.alta-A1", "F1", "Demanda alta solo en A1 (industrial y servicio ×1.8)",
             personalizada={"demanda": {"industrial": 1.8, "servicio": 1.8}}, esperado=esp_alta()),
        Caso("F1.hospital+50", "F1", "La carga crítica (hospital) crece 50 %",
             personalizada={"demanda": {"critica": 1.5}}, esperado=esp_alta()),
        Caso("F1.hospital-imposible", "F1", "Hospital ×3 con A1 al 20 %: A1 no alcanza ni para el hospital",
             personalizada={"demanda": {"critica": 3.0}, "alimentador": "A1", "factor_alimentador": 0.2},
             esperado=esp_alta("escalada")),
        Caso("F1.calor+30", "F1", "Ola de calor + crecimiento +30 %",
             personalizada={"demanda": _CALOR, "crecimiento_pct": 30}, esperado=esp_alta()),
    ],
    "F2": [
        Caso("F2.dem-30", "F2", "Demanda −30 %", personalizada={"demanda": todas(0.7)}, esperado=esp_baja),
        Caso("F2.dem-60", "F2", "Demanda −60 %", personalizada={"demanda": todas(0.4)}, esperado=esp_baja),
        Caso("F2.dem-90", "F2", "Demanda −90 % (casi nula)", personalizada={"demanda": todas(0.1)}, esperado=esp_baja),
        Caso("F2.madrugada-10", "F2", "Todas las cargas al 10 % de 00:00 a 06:00",
             personalizada={"demanda": todas(horario(0.1, range(0, 6)))}, esperado=esp_baja),
        Caso("F2.cero-A2", "F2", "Demanda cero en el alimentador A2 completo",
             personalizada={"demanda": {"residencial": 0.0, "comercial": 0.0}}, esperado=esp_baja),
        Caso("F2.cero-A1", "F2", "Demanda cero en el alimentador A1 completo (incluido el hospital)",
             personalizada={"demanda": {"industrial": 0.0, "servicio": 0.0, "critica": 0.0}}, esperado=esp_baja),
        Caso("F2.cero-total", "F2", "Demanda cero en toda la red", personalizada={"demanda": todas(0.0)},
             esperado=esp_baja),
    ],
    "F3": [Caso(f"F3.nubes-s{s}", "F3", "Intermitencia solar: factor aleatorio 0-1 de 08:00 a 18:00",
                personalizada=_nubes, semilla=s, esperado=esp_renovable) for s in range(101, 106)] + [
        Caso("F3.solar-0-12a14", "F3", "La solar cae a 0 de 12:00 a 14:00 con demanda normal",
             personalizada={"renovable": {"solar": horario(0.0, [12, 13])}}, esperado=esp_renovable),
    ] + [Caso(f"F3.eolica-volatil-s{s}", "F3", "Eólica volátil: factor aleatorio 0-1.5 por hora",
              personalizada=_viento, semilla=s, esperado=esp_renovable) for s in range(201, 204)] + [
        Caso("F3.rampa-17", "F3", "Rampa: la solar pasa de 100 % a 0 % a las 17:00 (pico vespertino)",
             personalizada={"renovable": {"solar": horario(0.0, range(17, 24))}}, esperado=esp_renovable),
        Caso("F3.renov150", "F3", "Renovables al 150 % con demanda normal",
             personalizada={"renovable": {"solar": 1.5, "eolica": 1.5}}, esperado=esp_renovable),
        Caso("F3.renov150-dem-60", "F3", "Renovables al 150 % con demanda −60 %",
             personalizada={"renovable": {"solar": 1.5, "eolica": 1.5}, "demanda": todas(0.4)}, esperado=esp_renovable),
        Caso("F3.renov150-dem-90", "F3", "Renovables al 150 % con demanda −90 % (choca con el límite de inyección)",
             personalizada={"renovable": {"solar": 1.5, "eolica": 1.5}, "demanda": todas(0.1)}, esperado=esp_renovable),
    ],
    "F4": [
        Caso("F4.solar-fuera", "F4", "Planta solar fuera de servicio todo el día",
             personalizada={"renovable": {"solar": 0.0}}, esperado=esp_fuera(("solar",))),
        Caso("F4.eolica-fuera", "F4", "Mini-eólica fuera de servicio todo el día",
             personalizada={"renovable": {"eolica": 0.0}}, esperado=esp_fuera(("eolica",))),
        Caso("F4.ambas-fuera", "F4", "Solar y eólica fuera de servicio",
             personalizada={"renovable": {"solar": 0.0, "eolica": 0.0}}, esperado=esp_fuera(("solar", "eolica"))),
        Caso("F4.parcial", "F4", "Disponibilidad parcial: solar 50 %, eólica 30 %",
             personalizada={"renovable": {"solar": 0.5, "eolica": 0.3}}, esperado=esp_renovable),
        Caso("F4.optimista", "F4", "Pronóstico optimista: se planea con renovable normal; la real queda en 0.5×P10",
             esperado=esp_optimista),
        Caso("F4.optimista-calor", "F4", "Pronóstico optimista en ola de calor (renovable real 0.5×P10)",
             condicion="ola_calor", esperado=esp_optimista),
        Caso("F4.optimista-crec+30", "F4", "Pronóstico optimista con demanda +30 % (renovable real 0.5×P10)",
             personalizada={"demanda": todas(1.3)}, esperado=esp_optimista),
        Caso("F4.optimista-trafo55", "F4", "Pronóstico optimista con el transformador al 55 % (renovable real 0.5×P10)",
             condicion="falla_transformador", esperado=esp_optimista),
        Caso("F4.cobertura-renovable", "F4", "La renovable del último día del historial cae al 30 % (bajo P10)",
             datos=renovable_ultimo_dia, esperado=esp_cobertura_renovable),
    ],
    "F5": [
        Caso("F5.dem+60-renov0-trafo55", "F5", "Demanda +60 %, renovables fuera y transformador al 55 %",
             personalizada={"demanda": todas(1.6), "renovable": {"solar": 0.0, "eolica": 0.0}, "capacidad": 0.55},
             esperado=esp_alta()),
        Caso("F5.baja-renov150-fallaA2", "F5", "Demanda −60 %, renovables al 150 % y falla de A2 (45 %)",
             personalizada={"demanda": todas(0.4), "renovable": {"solar": 1.5, "eolica": 1.5},
                            "alimentador": "A2", "factor_alimentador": 0.45}, esperado=esp_renovable),
        Caso("F5.fallaA1-hosp+50-pico", "F5", "Falla de A1 (45 %) + hospital +50 % + pico vespertino",
             personalizada={"demanda": {"residencial": horario(1.45, _VESP), "comercial": horario(1.25, _VESP),
                                        "critica": 1.5}, "alimentador": "A1", "factor_alimentador": 0.45},
             esperado=esp_alta()),
        Caso("F5.contingencia-3rech", "F5", "Contingencia natural con 3 rechazos del operador",
             condicion="contingencia_natural", rechazos=3, esperado=esp_estado("escalada")),
        Caso("F5.contingencia-3rech-seguridad", "F5",
             "Contingencia natural con 3 rechazos que piden más seguridad (plan robusto)",
             condicion="contingencia_natural", rechazos=3, agentes=rechazo_pidiendo_seguridad,
             esperado=esp_estado("escalada")),
    ],
    "F6": [
        Caso("F6.nan10", "F6", "10 % de lecturas faltantes (NaN)", datos=faltantes(0.10), esperado=esp_datos(nan=True)),
        Caso("F6.nan40", "F6", "40 % de lecturas faltantes (NaN)", datos=faltantes(0.40), esperado=esp_datos(nan=True)),
        Caso("F6.congelado", "F6", "Sensor de la zona comercial congelado (valor constante 7 días)",
             datos=congelado, esperado=esp_datos(minimo_detectado=90)),
        Caso("F6.negativas", "F6", "5 % de lecturas negativas", datos=negativas, esperado=esp_datos(minimo_detectado=90)),
        Caso("F6.picos50", "F6", "1 % de lecturas con picos de 50 veces el valor", datos=picos,
             esperado=esp_datos(minimo_detectado=90)),
        Caso("F6.reloj+1h", "F6", "Reloj del medidor comercial desfasado una hora", datos=reloj, esperado=esp_datos()),
        Caso("F6.apagon-ultimo-dia", "F6", "Último día del historial con todos los medidores en cero (apagón)",
             datos=apagon, esperado=esp_datos()),
        Caso("F6.sensor-fuera", "F6", "Medidor comercial sin ninguna lectura (100 % NaN)", datos=sensor_fuera,
             esperado=esp_datos()),
        Caso("F6.historial-1d", "F6", "Periodo 1d: casi sin historial (MAPE sin datos, bandas razonables)",
             periodos=("1d",), esperado=esp_historial_corto),
    ],
    "F7": [
        Caso("F7.persistente-D", "F7", "Falla persistente en D (dos veces seguidas)", agentes=falla_persistente("D"),
             esperado=esp_estado("escalada", "Falla persistente en D")),
        Caso("F7.persistente-E", "F7", "Falla persistente en E (dos veces seguidas)", agentes=falla_persistente("E"),
             esperado=esp_estado("escalada", "Falla persistente en E")),
        Caso("F7.persistente-F", "F7", "Falla persistente en F (dos veces seguidas)", agentes=falla_persistente("F"),
             esperado=esp_estado("escalada", "Falla persistente en F")),
        Caso("F7.D-luego-F", "F7", "Falla de D y luego de F en la misma sesión (una vez cada una)",
             falla="aprendizaje", agentes=_f_d_luego_f,
             # FAILURE por un enlace del grafo cobra su peso: D (2 + 8) y F (2 + 6 + 5)
             esperado=esp_estado("aprobada", costo=36 + 10 + 13)),
        Caso("F7.falla-B", "F7", "Falla única en B (captura IoT)", agentes=falla_una_vez("B"), esperado=_base),
        Caso("F7.falla-G", "F7", "Falla única en G (aprobación humana)", agentes=falla_una_vez("G"), esperado=_base),
        Caso("F7.falla-H", "F7", "Falla única en H (recomendación final)", agentes=falla_una_vez("H"), esperado=_base),
    ],
}
TODOS = {c.id: c for lista in CASOS.values() for c in lista}
TODOS[CASO_NORMAL.id] = CASO_NORMAL
TODOS[CASO_MIERCOLES.id] = CASO_MIERCOLES


# ======================================================= defectos conocidos
# id -> pares (caso, invariante o "E" de esperado) que el defecto hace fallar. Las pruebas
# generales omiten esos pares; la prueba @expectedFailure del defecto los exige.
DEFECTOS: dict[str, dict] = {
    "A1": {"severidad": "alta", "titulo": "Una falla en B, G o H tumba al supervisor (KeyError en AgenteMeta._manejar_falla)",
           "pares": [("F7.falla-B", "I9"), ("F7.falla-B", "E"), ("F7.falla-G", "I9"), ("F7.falla-G", "E"),
                     ("F7.falla-H", "I9"), ("F7.falla-H", "E")]},
    "A2": {"severidad": "media", "titulo": "Un medidor sin ninguna lectura válida hace fallar a B (np.interp sin puntos) "
                                           "y, por A1, rompe el flujo",
           "pares": [("F6.sensor-fuera", "I9"), ("F6.sensor-fuera", "E")]},
    "A3": {"severidad": "media", "titulo": "Demanda nula: división entre cero en desplazado_pct; E falla dos veces y "
                                           "se escala en lugar de proponer",
           "pares": [("F2.cero-total", "I7"), ("F2.cero-total", "E"), ("F6.apagon-ultimo-dia", "I7")]},
    "A4": {"severidad": "media", "titulo": "Si la red no alcanza ni para la carga crítica, el escalamiento no lo dice",
           "pares": [("F1.hospital-imposible", "I3")]},
    "A5": {"severidad": "media", "titulo": "La cobertura de bandas (ISO/IEC 5338) solo valida la demanda: un pronóstico "
                                           "renovable optimista no se detecta",
           "pares": [("F4.cobertura-renovable", "E")]},
    "A6": {"severidad": "media", "titulo": "Con 1 día de historial la banda P10-P90 es más estrecha que con 1 mes "
                                           "(subestima la incertidumbre)",
           "pares": [("F6.historial-1d", "E")]},
    "A8": {"severidad": "alta", "titulo": "Con historial corto (1d, 1s) las lecturas negativas producen una referencia "
                                          "negativa: D reemplaza lecturas válidas, el pronóstico sale negativo y P90 < P10",
           "pares": [("F6.negativas", "E")], "periodos": ("1d", "1s")},
    "A9": {"severidad": "media", "titulo": "D no detecta un sensor congelado: sin detector de valor constante, lo marca "
                                           "solo si el valor se aleja de la referencia (0 % en 1d y 1s, 86 % en 1m)",
           "pares": [("F6.congelado", "E")], "periodos": ("1d", "1s", "1m")},
    "A10": {"severidad": "media", "titulo": "El pronóstico ignora el tipo de día: si el historial termina en fin de "
                                            "semana el MAPE sube a ~19 % y la alerta de deriva queda siempre activa",
            "pares": [("REF.mape-fin-de-semana", "E")]},
    "A7": {"severidad": "baja", "titulo": "La API responde 500 ante tipos JSON inesperados o un Content-Length inválido",
           "pares": [(f"F7.api-{c[0]}", "E") for c in []]},   # se completa al definir ENTRADAS_DEFECTO
}


def conocido(caso_id: str, inv: str) -> str | None:
    for did, d in DEFECTOS.items():
        if (caso_id, inv) in d["pares"]:
            return did
    return None


class _Familia:
    familia = ""

    def test_invariantes(self):
        for caso in CASOS[self.familia]:
            for periodo in periodos_de(caso):
                r = obtener(caso, periodo)
                for inv in INVARIANTES:
                    if conocido(caso.id, inv):
                        continue
                    estado, det = r.invariantes[inv]
                    with self.subTest(caso=caso.id, periodo=periodo, inv=inv, semilla=caso.semilla):
                        self.assertNotEqual(estado, "FALLA", f"{det}\n  reproducir: {caso.comando(periodo)}")

    def test_esperado(self):
        for caso in CASOS[self.familia]:
            for periodo in periodos_de(caso):
                r = obtener(caso, periodo)
                if conocido(caso.id, "E"):
                    continue
                with self.subTest(caso=caso.id, periodo=periodo, semilla=caso.semilla):
                    self.assertNotEqual(r.esperado[0], "FALLA", f"{r.esperado[1]}\n  reproducir: {caso.comando(periodo)}")


def exigir(*pares, en: tuple | None = None):
    """Falla si alguno de los pares (caso, invariante|'E') falla en algún periodo de la campaña.
    `en`: periodos donde el defecto se manifiesta (se prueban también en el modo rápido)."""
    fallas = []
    for caso_id, inv in pares:
        caso = TODOS[caso_id]
        for periodo in sorted(set(periodos_de(caso)) | set(en or ()), key=list(PERIODOS).index):
            r = obtener(caso, periodo)
            estado, det = r.esperado if inv == "E" else r.invariantes[inv]
            if estado == "FALLA":
                fallas.append(f"{caso_id} {periodo} {inv}: {det}")
    if fallas:
        raise AssertionError("\n".join(fallas[:6]))


class Defectos(unittest.TestCase):
    """Cada prueba exige el comportamiento correcto y hoy falla: @expectedFailure con su id.
    Si un defecto se corrige, su prueba pasa ("unexpected success") y la suite lo avisa."""

    @unittest.expectedFailure
    def test_A1_falla_en_B_G_o_H_rompe_el_flujo(self):
        exigir(*DEFECTOS["A1"]["pares"])

    @unittest.expectedFailure
    def test_A2_medidor_sin_lecturas(self):
        exigir(*DEFECTOS["A2"]["pares"])

    @unittest.expectedFailure
    def test_A3_demanda_nula_division_entre_cero(self):
        exigir(*DEFECTOS["A3"]["pares"])

    @unittest.expectedFailure
    def test_A4_escalamiento_no_dice_que_falta_para_la_carga_critica(self):
        exigir(*DEFECTOS["A4"]["pares"])

    @unittest.expectedFailure
    def test_A5_cobertura_no_valida_renovables(self):
        exigir(*DEFECTOS["A5"]["pares"])

    @unittest.expectedFailure
    def test_A6_bandas_estrechas_con_1_dia(self):
        exigir(*DEFECTOS["A6"]["pares"])

    @unittest.expectedFailure
    def test_A10_pronostico_ignora_fin_de_semana(self):
        """Operación normal, día recomendado domingo 2026-11-22 (validación sobre el sábado): el MAPE
        debe quedar bajo MAPE_REENTRENAR, como cuando la validación cae entre semana."""
        from agentes import MAPE_REENTRENAR
        fallas, notas = [], []
        for periodo in [p for p in periodos() if p != "1d"]:      # con 1 día no hay MAPE
            domingo = obtener(CASO_NORMAL, periodo).sistema.pizarra.leer("analisis")["mape_pct"]
            jueves = obtener(CASO_MIERCOLES, periodo).sistema.pizarra.leer("analisis")["mape_pct"]
            notas.append(f"{periodo}: MAPE {domingo} % validando sábado vs {jueves} % validando miércoles")
            if domingo > MAPE_REENTRENAR:
                fallas.append(f"{periodo}: MAPE {domingo} % > {MAPE_REENTRENAR} % (entre semana {jueves} %)")
        FILAS_EXTRA.append({"familia": "REF", "escenario": "REF.mape-fin-de-semana", "periodo": ",".join(periodos()),
                            "semilla": SEMILLA_BASE, "propuesta": "-", "estado_final": "-",
                            **{i: "NA" for i in INVARIANTES}, "esperado": "FALLA" if fallas else "OK",
                            "defecto": "", "detalle": "; ".join(fallas), "notas": " | ".join(notas),
                            "comando": "python pruebas_adversas.py Defectos.test_A10_pronostico_ignora_fin_de_semana"})
        self.assertFalse(fallas, fallas)

    @unittest.expectedFailure
    def test_A8_lecturas_negativas_con_historial_corto(self):
        exigir(*DEFECTOS["A8"]["pares"], en=DEFECTOS["A8"]["periodos"])

    @unittest.expectedFailure
    def test_A9_sensor_congelado_sin_deteccion(self):
        exigir(*DEFECTOS["A9"]["pares"], en=DEFECTOS["A9"]["periodos"])


class F1DemandaAlta(_Familia, unittest.TestCase):
    familia = "F1"


class F2DemandaBaja(_Familia, unittest.TestCase):
    familia = "F2"


class F3VariacionRenovable(_Familia, unittest.TestCase):
    familia = "F3"


class F4DisponibilidadRenovable(_Familia, unittest.TestCase):
    familia = "F4"


class F5Combinados(_Familia, unittest.TestCase):
    familia = "F5"


class F6DatosIoT(_Familia, unittest.TestCase):
    familia = "F6"


class F7Agentes(_Familia, unittest.TestCase):
    familia = "F7"


# ====================================================== relaciones metamórficas
def _caso_m(cid: str, cond: dict | None = None, escala: float | None = None, semilla: int = SEMILLA_BASE) -> Caso:
    return Caso(cid, "M", cid, personalizada=cond, escala_red=escala, semilla=semilla)


def _s0(r):
    return r.escenario("S0")


def _recorte_propuesto(r) -> float:
    if r.propuesta is not None:
        return r.propuesta.metricas["recorte_kwh"]
    cont = next((e for e in r.lista_inicial if e.estrategia == "contingencia" and e.base_planificacion == "P50"), None)
    return cont.metricas["recorte_kwh"] if cont is not None else 0.0


def _serie(nombre: str, casos: list[Caso], periodo: str, medir) -> tuple[list, list]:
    rs = [obtener(c, periodo) for c in casos]
    for c in casos:
        TODOS.setdefault(c.id, c)
    return rs, [medir(r) for r in rs]


def _monotona(valores, creciente: bool) -> list[str]:
    out = []
    for i in range(len(valores) - 1):
        a, b = valores[i], valores[i + 1]
        if (creciente and b < a - TOL) or (not creciente and b > a + TOL):
            out.append(f"paso {i}→{i + 1}: {a:.1f} → {b:.1f}")
    return out


def _fila_m(rel: str, periodo: str, problemas: list[str], notas: str):
    FILAS_EXTRA.append({"familia": "M", "escenario": rel, "periodo": periodo, "semilla": SEMILLA_BASE,
                        "propuesta": "-", "estado_final": "-", **{i: "NA" for i in INVARIANTES},
                        "esperado": "FALLA" if problemas else "OK", "defecto": "",
                        "detalle": "; ".join(problemas), "notas": notas,
                        "comando": f"python pruebas_adversas.py --relacion {rel.split()[0]} --periodo {periodo}"})


def relacion_m1(periodo):
    """Más demanda nunca reduce el déficit de S0 ni el recorte propuesto."""
    problemas, notas = [], []
    for cap in (1.0, 0.55):
        fs = [1.0, 1.3, 1.6, 2.0, 2.5]
        casos = [_caso_m(f"M1.dem×{f}-cap{cap}", {"demanda": todas(f), "capacidad": cap}) for f in fs]
        rs, deficit = _serie("M1", casos, periodo, lambda r: _s0(r).metricas["deficit_kwh"])
        recorte = [_recorte_propuesto(r) for r in rs]
        problemas += [f"cap {cap} déficit S0 {x}" for x in _monotona(deficit, True)]
        problemas += [f"cap {cap} recorte {x}" for x in _monotona(recorte, True)]
        notas.append(f"cap {cap}: déficit S0 {[round(x) for x in deficit]}, recorte {[round(x) for x in recorte]}")
    return problemas, " | ".join(notas)


def relacion_m2(periodo):
    """Más renovable disponible nunca aumenta la convencional ni el costo de S0."""
    from modelo_red import COSTO_RENOVABLE
    problemas, notas = [], []
    for dem in (1.0, 2.0):
        fs = [0.0, 0.5, 1.0, 1.5, 2.0]
        casos = [_caso_m(f"M2.renov×{f}-dem{dem}", {"demanda": todas(dem), "renovable": {"solar": f, "eolica": f}})
                 for f in fs]
        rs, conv = _serie("M2", casos, periodo, lambda r: _s0(r).metricas["convencional_kwh"])
        m = [_s0(r).metricas for r in rs]
        costo = [x["costo_mxn"] for x in m]
        costo_conv = [x["costo_mxn"] - x["renovable_usada_kwh"] * COSTO_RENOVABLE for x in m]
        deficit = [x["deficit_kwh"] for x in m]
        problemas += [f"dem {dem} convencional {x}" for x in _monotona(conv, False)]
        problemas += [f"dem {dem} costo de la energía convencional {x}" for x in _monotona(costo_conv, False)]
        # El costo total solo es comparable si ambos puntos sirven la misma energía (sin déficit).
        # Con déficit, más renovable sirve más demanda y suma su costo de O&M: se documenta como hallazgo.
        literal = _monotona(costo, False)
        for x in literal:
            i = int(x.split()[1].split("→")[0])
            if deficit[i] == 0 and deficit[i + 1] == 0:
                problemas.append(f"dem {dem} costo total sin déficit {x}")
        notas.append(f"dem {dem}: conv {[round(x) for x in conv]}, costo {[round(x) for x in costo]}, "
                     f"déficit {[round(x) for x in deficit]}"
                     + (f"; costo total literal sube con déficit en {len(literal)} pasos (más energía servida)"
                        if literal else ""))
    return problemas, " | ".join(notas)


def relacion_m3(periodo):
    """Menos capacidad (subestación o alimentador) nunca reduce el déficit de S0."""
    problemas, notas = [], []
    fs = [1.0, 0.85, 0.7, 0.55, 0.4]
    variantes = [("SE", lambda f: {"demanda": todas(1.3), "capacidad": f})]
    variantes += [(k, (lambda f, k=k: {"demanda": todas(1.3), "alimentador": k, "factor_alimentador": f}))
                  for k in ("A1", "A2")]
    for nombre, cond in variantes:
        casos = [_caso_m(f"M3.{nombre}×{f}", cond(f)) for f in fs]
        _, deficit = _serie("M3", casos, periodo, lambda r: _s0(r).metricas["deficit_kwh"])
        problemas += [f"{nombre}: {x}" for x in _monotona(deficit, True)]
        notas.append(f"{nombre}: déficit S0 {[round(x) for x in deficit]}")
    return problemas, " | ".join(notas)


def relacion_m4(periodo):
    """Quitar la renovable de un alimentador nunca mejora su margen."""
    problemas, notas = [], []
    for dem in (1.0, 1.6, 0.1):
        for k, fuente in (("A2", "solar"), ("A1", "eolica")):
            con = _caso_m(f"M4.dem{dem}-con-{fuente}", {"demanda": todas(dem)})
            sin = _caso_m(f"M4.dem{dem}-sin-{fuente}", {"demanda": todas(dem), "renovable": {fuente: 0.0}})
            rs, margen = _serie("M4", [con, sin], periodo,
                                lambda r, k=k: _s0(r).metricas["alimentadores"][k]["margen_min_pct"])
            if margen[1] > margen[0] + TOL:
                pico = _s0(rs[0]).distribucion.flujo_cabecera[k]
                exporta = float(np.max(_s0(rs[0]).distribucion.renovable_disponible[k]
                                       - _s0(rs[0]).distribucion.demanda[k]))
                problemas.append(f"dem {dem}: sin {fuente} el margen de {k} sube {margen[0]} → {margen[1]} % "
                                 f"(excedente máx. {exporta:.0f} kW, cabecera máx. {pico.max():.0f} kW)")
            notas.append(f"dem {dem} {k}: {margen[0]} → {margen[1]} %")
    return problemas, " | ".join(notas)


def relacion_m5(periodo):
    """Escalar demanda y capacidades por el mismo factor no cambia el % de recorte (±1 punto)."""
    problemas, notas = [], []
    cond = {"capacidad": 0.55}
    pcts = {}
    for k in (1.0, 0.5, 2.0):
        r = obtener(_caso_m(f"M5.red×{k}", cond, escala=None if k == 1.0 else k), periodo)
        TODOS.setdefault(r.caso.id, r.caso)
        m = (r.propuesta or _s0(r)).metricas
        pcts[k] = m["recorte_kwh"] / max(m["demanda_kwh"], 1e-6) * 100
    for k in (0.5, 2.0):
        if abs(pcts[k] - pcts[1.0]) > 1.0:
            problemas.append(f"×{k}: recorte {pcts[k]:.2f} % vs {pcts[1.0]:.2f} %")
    notas.append("recorte % " + ", ".join(f"×{k}: {v:.2f}" for k, v in pcts.items()))
    return problemas, " | ".join(notas)


RELACIONES = {"M1": relacion_m1, "M2": relacion_m2, "M3": relacion_m3, "M4": relacion_m4, "M5": relacion_m5}
_RESULTADO_M: dict[tuple[str, str], tuple] = {}


def resultado_relacion(rel: str, periodo: str):
    if (rel, periodo) not in _RESULTADO_M:
        problemas, notas = RELACIONES[rel](periodo)
        _RESULTADO_M[(rel, periodo)] = (problemas, notas)
        _fila_m(f"{rel} {RELACIONES[rel].__doc__.strip()}", periodo, problemas, notas)
        if not CAMPANA["silencioso"]:
            print(f"    [{rel} | {periodo}] {'OK' if not problemas else 'FALLA: ' + problemas[0]}", flush=True)
    return _RESULTADO_M[(rel, periodo)]


class Metamorficas(unittest.TestCase):
    def _relacion(self, rel):
        for periodo in periodos():
            if conocido(rel, periodo) or conocido(rel, "*"):
                resultado_relacion(rel, periodo)
                continue
            problemas, notas = resultado_relacion(rel, periodo)
            with self.subTest(relacion=rel, periodo=periodo):
                self.assertFalse(problemas, "; ".join(problemas[:3]))

    def test_M1_mas_demanda(self):
        self._relacion("M1")

    def test_M2_mas_renovable(self):
        self._relacion("M2")

    def test_M3_menos_capacidad(self):
        self._relacion("M3")

    def test_M4_quitar_renovable(self):
        self._relacion("M4")

    def test_M5_escala(self):
        self._relacion("M5")

    def test_invariantes_corridas_metamorficas(self):
        """Las corridas de las relaciones también deben cumplir I1-I11."""
        for rel in RELACIONES:
            for periodo in periodos():
                resultado_relacion(rel, periodo)
        for (cid, periodo), r in list(RESULTADOS.items()):
            if r.caso.familia != "M":
                continue
            for inv in INVARIANTES:
                if conocido(cid, inv) or conocido("M*", inv):
                    continue
                estado, det = r.invariantes[inv]
                with self.subTest(caso=cid, periodo=periodo, inv=inv):
                    self.assertNotEqual(estado, "FALLA", det)


# =========================================================== gancho de pruebas
class GanchoDePruebas(unittest.TestCase):
    def test_validacion(self):
        malos = [{"demanda": {"residencial": 3.5}}, {"demanda": {"residencial": -0.1}},
                 {"demanda": {"residencial": [1.0] * 23}}, {"demanda": {"residencial": float("nan")}},
                 {"demanda": {"desconocido": 1.0}}, {"renovable": {"solar": 2.5}}, {"renovable": {"hidro": 1}},
                 {"capacidad": 1.2}, {"capacidad": 0}, {"alimentador": "A3"}, {"factor_alimentador": 1.5},
                 {"crecimiento_pct": 250}, {"crecimiento_pct": -60}, {"otra": 1},
                 {"demanda": {"residencial": [1.0] * 24}, "horas": [19]}, {"horas": [25]}]
        for d in malos:
            with self.subTest(condicion=d):
                with self.assertRaises(ValueError):
                    validar_personalizada(d)
        c = validar_personalizada({"demanda": {"critica": [1.0] * 24}, "renovable": {"solar": 0.0},
                                   "capacidad": 0.5, "alimentador": "A2"})
        self.assertEqual(c["alimentador"], "A2")
        self.assertEqual(c["factor_alimentador"], 0.45)

    def test_sin_gancho_mismo_resultado(self):
        """Una condición personalizada vacía da lo mismo que 'normal' (el gancho no altera el motor)."""
        a = obtener(CASO_NORMAL, "1m")
        b = correr(Caso("vacia", "REF", "", personalizada={}), "1m")
        self.assertEqual(json.loads(huella_resultado(a))["escenarios"], json.loads(huella_resultado(b))["escenarios"])

    def test_no_expuesto(self):
        for ruta in ("servidor.py", "main.py", os.path.join("web", "app.js")):
            with open(os.path.join(AQUI, ruta), encoding="utf-8") as f:
                self.assertNotIn("condicion_personalizada", f.read(), ruta)
        movil = os.path.join(AQUI, "mobile", "src")
        for raiz, _, archivos in os.walk(movil):
            for a in archivos:
                with open(os.path.join(raiz, a), encoding="utf-8", errors="ignore") as f:
                    self.assertNotIn("condicion_personalizada", f.read(), a)


# ===================================================================== API
@contextlib.contextmanager
def servidor_prueba():
    import servidor
    dir_original = servidor.DIR_SALIDA
    servidor.DIR_SALIDA = tempfile.mkdtemp(prefix="ea_api_")
    srv = servidor.crear_servidor("127.0.0.1", 0, silencioso=True, token="")
    hilo = threading.Thread(target=srv.serve_forever, daemon=True)
    hilo.start()
    try:
        yield srv, servidor.DIR_SALIDA
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(servidor.DIR_SALIDA, ignore_errors=True)
        servidor.DIR_SALIDA = dir_original


def pedir(srv, metodo, ruta, cuerpo=None, crudo: bytes | None = None, cabeceras=None, timeout=60):
    con = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=timeout)
    try:
        datos = crudo if crudo is not None else (None if cuerpo is None else json.dumps(cuerpo).encode())
        h = {"Content-Type": "application/json", **(cabeceras or {})}
        if datos is not None and "Content-Length" not in h:
            h["Content-Length"] = str(len(datos))
        con.putrequest(metodo, "/api" + ruta)
        for k, v in h.items():
            con.putheader(k, v)
        con.endheaders()
        if datos:
            try:
                con.send(datos)
            except (ConnectionError, OSError):
                pass
        resp = con.getresponse()
        texto = resp.read().decode("utf-8", errors="replace")
        try:
            return resp.status, json.loads(texto)
        except json.JSONDecodeError:
            return resp.status, texto
    finally:
        con.close()


ENTRADAS_ADVERSAS = [
    # (id, descripción, método, ruta, cuerpo, crudo, cabeceras)
    ("crec-250", "crecimiento_pct = 250", "POST", "/sesiones",
     {"condicion": "crecimiento_demanda", "crecimiento_pct": 250}, None, None),
    ("crec-menos60", "crecimiento_pct = −60", "POST", "/sesiones",
     {"condicion": "crecimiento_demanda", "crecimiento_pct": -60}, None, None),
    ("crec-texto", "crecimiento_pct como texto", "POST", "/sesiones",
     {"condicion": "crecimiento_demanda", "crecimiento_pct": "20"}, None, None),
    ("alim-A3", "alimentador inválido", "POST", "/sesiones", {"condicion": "falla_alimentador", "alimentador": "A3"},
     None, None),
    ("json-malformado", "JSON malformado", "POST", "/sesiones", None, b'{"periodo": "1m",', None),
    ("cuerpo-11kb", "cuerpo de 11 KB", "POST", "/sesiones", None,
     json.dumps({"periodo": "1m", "relleno": "x" * 11_000}).encode(), None),
    ("json-lista", "cuerpo JSON que no es objeto", "POST", "/sesiones", None, b"[1, 2]", None),
    ("semilla-negativa", "semilla negativa", "POST", "/sesiones", {"semilla": -1}, None, None),
    ("falla-invalida", "falla inexistente", "POST", "/sesiones", {"falla": "total"}, None, None),
    ("ruta-inexistente", "ruta desconocida", "GET", "/nada", None, None, None),
    ("sesion-inexistente", "sesión inexistente", "GET", "/sesiones/0123456789", None, None, None),
    ("metodo", "GET en aprobación", "GET", "/sesiones/0123456789/aprobacion", None, None, None),
]
ENTRADAS_DEFECTO = [
    ("periodo-lista", "periodo como lista", "POST", "/sesiones", {"periodo": ["1m"]}, None, None),
    ("condicion-objeto", "condición como objeto", "POST", "/sesiones", {"condicion": {"id": "normal"}}, None, None),
    ("content-length-texto", "Content-Length no numérico", "POST", "/sesiones", None, b"{}",
     {"Content-Length": "abc"}),
    ("content-length-negativo", "Content-Length negativo", "POST", "/sesiones", None, b"{}",
     {"Content-Length": "-5"}),
]
DEFECTOS["A7"]["pares"] = [(f"F7.api-{c[0]}", "E") for c in ENTRADAS_DEFECTO]


def _resp_adversa(srv, caso):
    _, _, metodo, ruta, cuerpo, crudo, cab = caso
    try:
        return pedir(srv, metodo, ruta, cuerpo, crudo, cab, timeout=10)
    except (ConnectionError, socket.timeout, OSError) as e:
        return None, f"{type(e).__name__}: {e}"


def _fila_api(escenario, estado_ok: bool, detalle: str, notas: str = "", defecto: str = ""):
    FILAS_EXTRA.append({"familia": "F7", "escenario": escenario, "periodo": "1m", "semilla": SEMILLA_BASE,
                        "propuesta": "-", "estado_final": "-",
                        **{i: "NA" for i in INVARIANTES}, "esperado": "OK" if estado_ok else "FALLA",
                        "defecto": defecto, "detalle": detalle, "notas": notas,
                        "comando": "python pruebas_adversas.py F7API"})


class F7API(unittest.TestCase):
    def test_concurrencia_20_sesiones(self):
        condiciones = [c for c, v in CONDICIONES.items() if "alias_de" not in v]
        pedidos = []
        for i in range(20):
            cond = condiciones[i % len(condiciones)]
            cuerpo = {"periodo": periodos()[i % len(periodos())], "condicion": cond, "semilla": 1000 + i}
            if cond == "falla_alimentador":
                cuerpo["alimentador"] = ("A1", "A2")[i % 2]
            if cond == "crecimiento_demanda":
                cuerpo["crecimiento_pct"] = [-40, 25, 80][i % 3]
            pedidos.append(cuerpo)
        with servidor_prueba() as (srv, dir_salida):
            def crear(cuerpo):
                t0 = time.perf_counter()
                st, v = pedir(srv, "POST", "/sesiones", cuerpo)
                return st, v, time.perf_counter() - t0
            with ThreadPoolExecutor(max_workers=20) as ex:
                res = list(ex.map(crear, pedidos))
            estados = [st for st, _, _ in res]
            tiempos = sorted(t for _, _, t in res)

            def aprobar(v):
                if v.get("estado") != "en_espera":
                    return 200, v
                return pedir(srv, "POST", f"/sesiones/{v['id']}/aprobacion",
                             {"aprobado": True, "operador": "Prueba concurrente"})
            with ThreadPoolExecutor(max_workers=20) as ex:
                aprob = list(ex.map(aprobar, [v for _, v, _ in res if isinstance(v, dict)]))
            import servidor
            aislamiento = []
            for cuerpo, (st, v, _) in zip(pedidos, res):
                if st != 201:
                    continue
                if v["condicion"]["id"] != cuerpo["condicion"] or v["periodo"]["id"] != cuerpo["periodo"]:
                    aislamiento.append(f"{v['id']} devolvió {v['condicion']['id']}/{v['periodo']['id']}")
                s = servidor.obtener(v["id"])
                convs = {e.get("conversacion") for e in s.bitacora.eventos if e["tipo"] == "mensaje"}
                if convs != {s.conversacion}:
                    aislamiento.append(f"{v['id']} con mensajes de otras conversaciones {convs}")
            with open(os.path.join(dir_salida, "memoria_aprendizaje.json"), encoding="utf-8") as f:
                memoria = json.load(f)
        p95 = float(np.percentile(tiempos, 95))
        RENDIMIENTO["api"] = {"sesiones": 20, "p50_s": round(float(np.percentile(tiempos, 50)), 2),
                              "p95_s": round(p95, 2), "max_s": round(tiempos[-1], 2),
                              "periodos": sorted({c["periodo"] for c in pedidos})}
        ids = [v["id"] for st, v, _ in res if st == 201]
        problemas = []
        if any(st >= 500 for st in estados) or any(st >= 500 for st, _ in aprob):
            problemas.append(f"errores 500: creación {estados}, aprobación {[st for st, _ in aprob]}")
        if len(set(ids)) != 20:
            problemas.append(f"{len(set(ids))} ids únicos de 20")
        if memoria.get("ejecuciones") != 20:
            problemas.append(f"memoria con {memoria.get('ejecuciones')} ejecuciones (esperadas 20)")
        problemas += aislamiento
        _fila_api("F7.api-20-concurrentes", not problemas, "; ".join(problemas),
                  f"p50 {RENDIMIENTO['api']['p50_s']} s, p95 {p95:.2f} s, máx {tiempos[-1]:.2f} s")
        self.assertFalse(problemas, problemas)
        self.assertLess(p95, 15.0, f"p95 de la API {p95:.1f} s ≥ 15 s")

    def _adversas(self, casos):
        with servidor_prueba() as (srv, _):
            st, v = pedir(srv, "POST", "/sesiones", {"periodo": "1d"})
            casos = casos + [("motivo-501", "motivo de 501 caracteres", "POST", f"/sesiones/{v['id']}/aprobacion",
                              {"aprobado": False, "motivo": "x" * 501}, None, None),
                             ("operador-no-texto", "operador numérico", "POST", f"/sesiones/{v['id']}/aprobacion",
                              {"aprobado": True, "operador": 5}, None, None),
                             ("aprobado-texto", "aprobado como texto", "POST", f"/sesiones/{v['id']}/aprobacion",
                              {"aprobado": "si"}, None, None)] if casos is ENTRADAS_ADVERSAS else casos
            salida = []
            for caso in casos:
                st, cuerpo = _resp_adversa(srv, caso)
                ok = st is not None and 400 <= st < 500 and isinstance(cuerpo, dict) and cuerpo.get("error")
                salida.append((caso, st, cuerpo, ok))
        return salida

    def test_entradas_adversas_4xx(self):
        for (cid, desc, *_), st, cuerpo, ok in self._adversas(ENTRADAS_ADVERSAS):
            _fila_api(f"F7.api-{cid}", bool(ok), "" if ok else f"{st}: {str(cuerpo)[:90]}", f"{desc} → {st}")
            with self.subTest(entrada=cid):
                self.assertTrue(ok, f"{desc}: {st} {str(cuerpo)[:120]}")

    @unittest.expectedFailure
    def test_A7_api_500_con_tipos_inesperados(self):
        """A7: periodo como lista, condición como objeto o Content-Length inválido dan 500, no 4xx."""
        fallas = []
        for (cid, desc, *_), st, cuerpo, ok in self._adversas(ENTRADAS_DEFECTO):
            _fila_api(f"F7.api-{cid}", bool(ok), "" if ok else f"{st}: {str(cuerpo)[:90]}", f"{desc} → {st}",
                      "" if ok else "A7")
            if not ok:
                fallas.append(f"{desc}: {st} {str(cuerpo)[:80]}")
        self.assertFalse(fallas, fallas)

    def test_gancho_ignorado_por_la_api(self):
        with servidor_prueba() as (srv, _):
            st, v = pedir(srv, "POST", "/sesiones", {"periodo": "1d", "condicion_personalizada":
                                                      {"demanda": {"critica": 3.0}}})
        self.assertEqual(st, 201)
        self.assertEqual(v["condicion"]["id"], "normal")
        self.assertNotIn("personalizada", json.dumps(v))


# ============================================================== rendimiento
class Rendimiento(unittest.TestCase):
    def test_tiempo_por_analisis(self):
        tiempos = {}
        for periodo in periodos():
            ts = [correr(CASO_NORMAL, periodo).segundos for _ in range(3)]
            ts += [correr(TODOS["F1.crec+60"], periodo).segundos]
            tiempos[periodo] = {"mediana_s": round(float(np.median(ts)), 3), "max_s": round(max(ts), 3)}
        RENDIMIENTO["analisis"] = tiempos
        RENDIMIENTO["maquina"] = {"sistema": platform.platform(), "procesador": platform.processor(),
                                  "nucleos": os.cpu_count(), "python": platform.python_version(),
                                  "numpy": np.__version__}
        if "1a" in tiempos:
            self.assertLess(tiempos["1a"]["max_s"], 10.0)


# ============================================================ salida y main
COLUMNAS = ["familia", "escenario", "periodo", "semilla", "propuesta", "estado_final", *INVARIANTES,
            "esperado", "defecto", "detalle", "notas", "comando"]


def filas_csv() -> list[dict]:
    filas = []
    for (cid, periodo), r in sorted(RESULTADOS.items(), key=lambda x: (x[1].caso.familia, x[0][0],
                                                                         list(PERIODOS).index(x[0][1]))):
        fila = {"familia": r.caso.familia, "escenario": cid, "periodo": periodo, "semilla": r.caso.semilla,
                "propuesta": r.propuesta_final, "estado_final": r.estado}
        defectos, detalles = set(), []
        for inv in INVARIANTES:
            estado, det = r.invariantes[inv]
            did = conocido(cid, inv) if estado == "FALLA" else None
            fila[inv] = estado if not did else f"FALLA({did})"
            if did:
                defectos.add(did)
            if det:
                detalles.append(f"{inv}: {det}")
        did = conocido(cid, "E") if r.esperado[0] == "FALLA" else None
        fila["esperado"] = r.esperado[0] if not did else f"FALLA({did})"
        if did:
            defectos.add(did)
        if r.esperado[1]:
            detalles.append(f"E: {r.esperado[1]}")
        fila.update({"defecto": ",".join(sorted(defectos)), "detalle": " | ".join(detalles),
                     "notas": " | ".join(n for n in r.notas if n), "comando": r.caso.comando(periodo)})
        filas.append(fila)
    for f in FILAS_EXTRA:
        rel = f["escenario"].split()[0]
        did = conocido(rel, f["periodo"]) or conocido(rel, "*") or conocido(f["escenario"], "E")
        if did and f["esperado"] == "FALLA":
            f = {**f, "esperado": f"FALLA({did})", "defecto": did}
        filas.append(f)
    return filas


def escribir_resultados():
    os.makedirs(DIR_INFORME, exist_ok=True)
    nombre = "resultados_rapido.csv" if CAMPANA["rapido"] else "resultados.csv"
    filas = filas_csv()
    with open(os.path.join(DIR_INFORME, nombre), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)
    evaluadas = [f[i] for f in filas for i in INVARIANTES if f[i] != "NA"]
    ok = sum(v == "OK" for v in evaluadas)
    esp = [f["esperado"] for f in filas if f["esperado"] != "NA"]
    resumen = {"modo": "rapido" if CAMPANA["rapido"] else "completo", "periodos": periodos(),
               "corridas": len(RESULTADOS), "filas": len(filas),
               "invariantes_evaluadas": len(evaluadas), "invariantes_ok": ok,
               "pct_invariantes_ok": round(ok / max(len(evaluadas), 1) * 100, 2),
               "esperados_evaluados": len(esp), "esperados_ok": sum(v == "OK" for v in esp),
               "rendimiento": RENDIMIENTO, "defectos": {k: v["titulo"] for k, v in DEFECTOS.items()}}
    with open(os.path.join(DIR_INFORME, nombre.replace(".csv", ".json").replace("resultados", "resumen")),
              "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, indent=2)
    return nombre, resumen


def reproducir(caso_id: str, periodo: str):
    caso = TODOS[caso_id]
    r = obtener(caso, periodo)
    print(f"\n{caso.id}: {caso.desc}\n  periodo {periodo}, semilla {caso.semilla}, estado {r.estado}, "
          f"propuesta {r.propuesta_final}, costo {r.sistema.bus.costo_total}, {r.sistema.bus.enviados} mensajes")
    if r.excepcion:
        print(f"  EXCEPCIÓN: {r.excepcion}")
    for inv in INVARIANTES:
        estado, det = r.invariantes[inv]
        print(f"  {inv:<4} {estado:<6} {det}")
    print(f"  esperado {r.esperado[0]} {r.esperado[1]}")
    for n in r.notas:
        print(f"  nota: {n}")
    if r.motivo_escalamiento:
        print(f"  motivo de escalamiento: {r.motivo_escalamiento}")


def servidor_capturas(puerto: int):
    """Servidor de demostración para las capturas del informe: añade, SOLO en este proceso,
    condiciones extremas al catálogo para que la web las muestre con su nombre real."""
    import servidor
    extra = {
        "prueba_demanda_alta_100": {"desc": "[Prueba] Demanda alta +100 %",
                                    "descripcion": "Campaña adversa F1: toda la demanda al doble.",
                                    "demanda": todas(2.0), "capacidad": 1.0},
        "prueba_demanda_baja_90": {"desc": "[Prueba] Demanda baja −90 %",
                                   "descripcion": "Campaña adversa F2: todas las cargas al 10 %.",
                                   "demanda": todas(0.1), "capacidad": 1.0},
        "prueba_renovables_fuera": {"desc": "[Prueba] Renovables fuera de servicio",
                                    "descripcion": "Campaña adversa F4: solar y mini-eólica fuera todo el día.",
                                    "demanda": {}, "capacidad": 1.0, "renovable": {"solar": 0.0, "eolica": 0.0}},
        "prueba_hospital_imposible": {"desc": "[Prueba] A1 no alcanza ni para el hospital",
                                      "descripcion": "Campaña adversa F1: hospital ×3 y A1 al 20 %.",
                                      "demanda": {"critica": 3.0}, "capacidad": 1.0,
                                      "alimentador": "A1", "factor_alimentador": 0.2},
    }
    CONDICIONES.update(extra)
    servidor.DIR_SALIDA = tempfile.mkdtemp(prefix="ea_capturas_")
    srv = servidor.crear_servidor("127.0.0.1", puerto, silencioso=True, token="")
    print(f"Servidor de capturas en http://127.0.0.1:{puerto} con {', '.join(extra)}", flush=True)
    srv.serve_forever()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rapido", action="store_true", help="solo el periodo 1m (< 2 minutos)")
    ap.add_argument("--caso", help="reproduce un caso (id) y muestra sus invariantes")
    ap.add_argument("--relacion", help="evalúa una relación metamórfica (M1..M5)")
    ap.add_argument("--periodo", default="1m")
    ap.add_argument("--listar", action="store_true")
    ap.add_argument("--silencioso", action="store_true", help="no imprime cada corrida")
    ap.add_argument("--servidor-capturas", nargs="?", const=8765, type=int, metavar="PUERTO")
    args, resto = ap.parse_known_args()
    CAMPANA["silencioso"] = args.silencioso
    if args.servidor_capturas:
        return servidor_capturas(args.servidor_capturas)
    if args.listar:
        for c in TODOS.values():
            print(f"{c.id:<34} semilla={c.semilla:<4} {c.desc}")
        return 0
    if args.caso:
        CAMPANA["periodos"] = [args.periodo]
        return reproducir(args.caso, args.periodo)
    if args.relacion:
        problemas, notas = RELACIONES[args.relacion](args.periodo)
        print(f"{args.relacion} {args.periodo}: {'OK' if not problemas else 'FALLA'}\n  {notas}")
        for p in problemas:
            print(f"  - {p}")
        return 0
    if args.rapido:
        CAMPANA.update(periodos=["1m"], rapido=True)
    print(f"Campaña adversa ({'rápida: 1m' if args.rapido else 'completa: ' + ', '.join(periodos())}); "
          f"día recomendado {FIN.date()}", flush=True)
    t0 = time.perf_counter()
    programa = unittest.main(argv=[sys.argv[0], *resto], verbosity=2, exit=False)
    if [a for a in resto if not a.startswith("-")]:      # selección de pruebas: no pisa el CSV de la campaña
        print("\nSelección parcial de pruebas: no se escriben resultados.")
        return 0 if programa.result.wasSuccessful() else 1
    nombre, resumen = escribir_resultados()
    print(f"\n{resumen['corridas']} corridas, {resumen['invariantes_evaluadas']} invariantes evaluadas, "
          f"{resumen['pct_invariantes_ok']} % OK; expectativas {resumen['esperados_ok']}/{resumen['esperados_evaluados']}; "
          f"{len(DEFECTOS)} defectos documentados; {time.perf_counter() - t0:.0f} s. "
          f"Resultados en docs/pruebas_adversas/{nombre}")
    return 0 if programa.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
