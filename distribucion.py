"""
Asignación horaria de energía por alimentador (la usa el agente deliberativo E).

Para cada hora y cada alimentador:
  1. Regla 2: primero la generación renovable local; los excedentes suben a la
     subestación (hasta `limite_inyeccion_kw`) y se reparten entre los demás
     alimentadores.
  2. El resto se cubre con energía convencional del transformador de la
     subestación, respetando su capacidad, la de cada alimentador (cabecera) y
     la de los transformadores MT/BT.
  3. Regla 1: si falta capacidad, el recorte es PARCIAL: solo los kW
     necesarios, por `prioridad_corte`, sin tocar nunca las cargas críticas.

Pérdidas técnicas aproximadas: un kW entregado en una carga de MT cuesta
(1 + pérdidas MT) kW en la barra de la subestación; en BT, (1 + MT)(1 + BT).
La renovable consumida en su propio alimentador no tiene pérdidas (se modela
en el mismo nodo). Es un balance de energía, no un flujo de potencia: no se
calculan tensiones ni potencia reactiva.

Todo es determinista y vectorizado (las 24 horas a la vez) con numpy.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from modelo_red import COSTO_RENOVABLE, RedDistribucion

HORAS = 24
_EPS = 1e-9


@dataclass
class Limites:
    """Capacidades efectivas del día, ya con la condición de la red aplicada."""
    subestacion_kw: float
    alimentador_kw: dict[str, float]          # clave -> kW en la cabecera
    transformador_bt_kw: dict[str, float]     # clave -> kW (inf si atiende en MT)
    inyeccion_kw: float                       # tope del excedente renovable que sube a la subestación
    perdidas: dict[str, float]                # clave -> fracción total al entregar en las cargas
    perdidas_bt: dict[str, float]             # clave -> fracción del tramo MT/BT (0 en MT)

    def entregable_kw(self, clave: str) -> float:
        """Máximo que la red puede entregar en las cargas del alimentador."""
        return min(self.alimentador_kw[clave] / (1 + self.perdidas[clave]),
                   self.transformador_bt_kw[clave] / (1 + self.perdidas_bt[clave]))


def limites_red(red: RedDistribucion, factor_subestacion: float = 1.0,
                factor_alimentador: dict[str, float] | None = None) -> Limites:
    """Limites de la red; los factores (<1) representan fallas o reconfiguraciones."""
    fa = factor_alimentador or {}
    mt, bt = red.perdidas_mt_pct / 100, red.perdidas_bt_pct / 100
    alim, trafo, perd, perd_bt = {}, {}, {}, {}
    for a in red.alimentadores:
        f = fa.get(a.clave, 1.0)
        alim[a.clave] = a.capacidad_kw * f
        es_bt = a.nivel == "BT"
        trafo[a.clave] = a.transformador_bt_kw * f if es_bt else float("inf")
        perd[a.clave] = (1 + mt) * (1 + bt) - 1 if es_bt else mt
        perd_bt[a.clave] = bt if es_bt else 0.0
    return Limites(red.capacidad_convencional_kw * factor_subestacion, alim, trafo,
                   red.limite_inyeccion_kw, perd, perd_bt)


# ------------------------------------------------------------- un balance
@dataclass
class _Paso:
    """Balance de una hora x alimentador sin decidir todavía quién se queda sin energía."""
    renovable_local: dict
    necesidad: dict          # demanda no cubierta por la renovable local (kW en carga)
    por_red: dict            # parte que la red puede llevar (limitada por cabecera / trafo BT)
    falta_red: dict          # lo que no cabe por el alimentador o el transformador BT
    renovable_importada: dict
    enviada: dict            # excedente renovable enviado a otros alimentadores (kW en origen)
    desperdiciada: dict
    convencional_barra: dict  # convencional requerida en la barra de la subestación (con pérdidas)
    convencional: dict        # convencional entregada en las cargas (si hubiera capacidad)
    perdidas: dict
    flujo_cabecera: dict      # kW requeridos en la cabecera (import o export)
    flujo_trafo_bt: dict
    requerido_subestacion: np.ndarray
    exceso_subestacion: np.ndarray   # kW en barra que superan al transformador


def _paso(claves: list[str], demanda: dict, renovable: dict, lim: Limites) -> _Paso:
    rl, nec, red_, falta, exp_barra, nec_barra = {}, {}, {}, {}, {}, {}
    excedente, exportable = {}, {}
    for k in claves:
        D, R, perd = demanda[k], renovable[k], lim.perdidas[k]
        rl[k] = np.minimum(R, D)
        nec[k] = D - rl[k]
        excedente[k] = R - rl[k]
        red_[k] = np.minimum(nec[k], lim.entregable_kw(k))
        falta[k] = nec[k] - red_[k]
        exportable[k] = np.minimum.reduce([excedente[k], np.full(HORAS, lim.inyeccion_kw),
                                           np.full(HORAS, lim.alimentador_kw[k]),
                                           np.full(HORAS, lim.transformador_bt_kw[k])])
        exp_barra[k] = exportable[k] / (1 + perd)
        nec_barra[k] = red_[k] * (1 + perd)

    T = sum(nec_barra.values())
    P = sum(exp_barra.values())
    S = np.minimum(P, T)                      # renovable que cambia de alimentador (en barra)
    ri, env, desp, cb, cv, pe, fc, ft = {}, {}, {}, {}, {}, {}, {}, {}
    for k in claves:
        perd, perd_bt = lim.perdidas[k], lim.perdidas_bt[k]
        recibe = np.where(T > _EPS, S * nec_barra[k] / np.maximum(T, _EPS), 0.0)
        usada_barra = np.where(P > _EPS, S * exp_barra[k] / np.maximum(P, _EPS), 0.0)
        ri[k] = recibe / (1 + perd)
        env[k] = usada_barra * (1 + perd)
        desp[k] = excedente[k] - env[k]
        cb[k] = nec_barra[k] - recibe
        cv[k] = cb[k] / (1 + perd)
        pe[k] = (ri[k] + cv[k]) * perd + (env[k] - usada_barra)
        fc[k] = np.maximum(nec[k] * (1 + perd), env[k])
        ft[k] = np.maximum(nec[k] * (1 + perd_bt), env[k]) if np.isfinite(lim.transformador_bt_kw[k]) \
            else np.zeros(HORAS)
    req = sum(cb.values())
    return _Paso(rl, nec, red_, falta, ri, env, desp, cb, cv, pe, fc, ft,
                 req, np.maximum(0.0, req - lim.subestacion_kw))


def _repartir_faltante(red: RedDistribucion, alim_de: dict, demanda: dict, p: _Paso,
                       lim: Limites) -> dict[str, np.ndarray]:
    """kW que no se pueden servir, asignados por carga según prioridad_corte.

    Las cargas no críticas se afectan primero (prioridad 1 antes que 2...); a
    igual prioridad y alimentador, en proporción a su demanda. Las cargas
    críticas solo aparecen aquí si ya no queda carga no crítica que recortar."""
    falt = {n: np.zeros(HORAS) for n in demanda}
    resto = {n: demanda[n].copy() for n in demanda}
    grupos: dict[tuple, list[str]] = {}
    for c in red.cargas:
        grupos.setdefault((c.critica, c.prioridad_corte, alim_de[c.nombre]), []).append(c.nombre)
    orden = sorted(grupos)

    def tomar(nombres, cantidad):
        disp = sum(resto[n] for n in nombres)
        x = np.minimum(cantidad, disp)
        for n in nombres:
            parte = np.where(disp > _EPS, x * resto[n] / np.maximum(disp, _EPS), 0.0)
            falt[n] += parte
            resto[n] -= parte
        return x

    # a) saturación del alimentador o del transformador de BT: solo dentro de ese alimentador
    for k, pend in p.falta_red.items():
        pend = pend.copy()
        for g in orden:
            if g[2] == k:
                pend -= tomar(grupos[g], pend)
    # b) saturación del transformador de la subestación: entre todos los alimentadores
    pend = p.exceso_subestacion.copy()
    por_red = {k: v.copy() for k, v in p.por_red.items()}
    for g in orden:
        k = g[2]
        fac = 1 + lim.perdidas[k]
        x = tomar(grupos[g], np.minimum(pend / fac, por_red[k]))
        por_red[k] -= x
        pend -= x * fac
    return falt


# ---------------------------------------------------------- resultado
@dataclass
class Distribucion:
    """Resultado de la asignación. Series de 24 h en kW (= kWh por hora)."""
    claves: list[str]
    limites: Limites
    demanda: dict                  # por alimentador, antes de cualquier recorte
    renovable_disponible: dict
    renovable_usada: dict          # local + recibida de otros alimentadores (en las cargas)
    renovable_desperdiciada: dict
    convencional: dict             # entregada en las cargas
    perdidas: dict
    deficit: dict                  # lo que faltaría sin medidas de contingencia
    recorte: dict                  # recorte controlado aplicado (Regla 1)
    no_servido: dict               # déficit que queda después de las medidas
    flujo_cabecera: dict           # kW requeridos en la cabecera tras las medidas
    flujo_trafo_bt: dict
    margen_pct: dict               # holgura mínima (cabecera / trafo BT) por hora
    costo: dict                    # MXN por hora
    importacion_subestacion: np.ndarray    # convencional tomada del transformador (con pérdidas)
    requerido_subestacion: np.ndarray      # convencional requerida tras las medidas
    margen_subestacion_pct: np.ndarray
    recorte_por_carga: dict[str, np.ndarray] = field(default_factory=dict)
    deficit_por_carga: dict[str, np.ndarray] = field(default_factory=dict)
    no_servido_por_carga: dict[str, np.ndarray] = field(default_factory=dict)
    con_carga_critica: set = field(default_factory=set)   # alimentadores con cargas críticas

    def total(self, campo: str) -> np.ndarray:
        return sum(getattr(self, campo)[k] for k in self.claves)

    def utilizacion_pct(self, clave: str) -> float:
        """Uso máximo del alimentador o de sus transformadores de BT (100 = al límite)."""
        u = float(self.flujo_cabecera[clave].max() / max(self.limites.alimentador_kw[clave], _EPS))
        if np.isfinite(self.limites.transformador_bt_kw[clave]):
            u = max(u, float(self.flujo_trafo_bt[clave].max() / max(self.limites.transformador_bt_kw[clave], _EPS)))
        return round(u * 100, 1)

    def utilizacion_max_pct(self) -> float:
        return max(self.utilizacion_pct(k) for k in self.claves)

    def resumen(self) -> dict:
        """Totales del día por alimentador (kWh) y holguras mínimas (%)."""
        out = {}
        for k in self.claves:
            out[k] = {
                "demanda_kwh": round(float(self.demanda[k].sum()), 1),
                "renovable_usada_kwh": round(float(self.renovable_usada[k].sum()), 1),
                "renovable_desperdiciada_kwh": round(float(self.renovable_desperdiciada[k].sum()), 1),
                "convencional_kwh": round(float(self.convencional[k].sum()), 1),
                "perdidas_kwh": round(float(self.perdidas[k].sum()), 1),
                "deficit_kwh": round(float(self.deficit[k].sum()), 1),
                "recorte_kwh": round(float(self.recorte[k].sum()), 1),
                "no_servido_kwh": round(float(self.no_servido[k].sum()), 1),
                "margen_min_pct": round(float(self.margen_pct[k].min()), 2) + 0.0,
                "utilizacion_max_pct": self.utilizacion_pct(k),
                "carga_critica": k in self.con_carga_critica,
                "pico_cabecera_kw": round(float(self.flujo_cabecera[k].max()), 1),
                "capacidad_kw": round(self.limites.alimentador_kw[k], 1),
                "capacidad_trafo_bt_kw": (round(self.limites.transformador_bt_kw[k], 1)
                                          if np.isfinite(self.limites.transformador_bt_kw[k]) else None),
                "costo_mxn": round(float(self.costo[k].sum()), 2),
            }
        return out

    def series(self, clave: str | None = None) -> dict:
        """Series horarias de un alimentador (o de la subestación si clave es None)."""
        pick = (lambda campo: getattr(self, campo)[clave]) if clave else self.total
        return {c: pick(c) for c in ("demanda", "renovable_disponible", "renovable_usada", "convencional",
                                     "perdidas", "deficit", "recorte", "no_servido")}


def asignar(red: RedDistribucion, cargas: dict[str, np.ndarray], renovables: dict[str, np.ndarray],
            limites: Limites, tarifas: np.ndarray, recortar: bool = False,
            recorte_fijo: dict[str, np.ndarray] | None = None) -> Distribucion:
    """Asigna la energía de un día, hora por hora y alimentador por alimentador.

    cargas / renovables: nombre -> serie de 24 h (kW).
    recortar: calcula y aplica el recorte parcial necesario para eliminar el déficit.
    recorte_fijo: aplica un plan de recorte ya calculado (p. ej. sobre otra banda).
    """
    claves = [a.clave for a in red.alimentadores]
    alim_de = {c.nombre: red.alimentador(c.alimentador).clave for c in red.cargas}
    critica = {c.nombre: c.critica for c in red.cargas}
    D = {c.nombre: np.clip(np.asarray(cargas[c.nombre], float), 0, None) for c in red.cargas}
    R = {k: np.zeros(HORAS) for k in claves}
    for f in red.renovables:
        R[red.alimentador(f.alimentador).clave] += np.clip(np.asarray(renovables.get(f.nombre, 0.0), float), 0, None)

    def por_alim(series):
        out = {k: np.zeros(HORAS) for k in claves}
        for n, v in series.items():
            out[alim_de[n]] += v
        return out

    # 1) Sin medidas: cuánto faltaría y a quién le tocaría por prioridad
    p0 = _paso(claves, por_alim(D), R, limites)
    falta0 = _repartir_faltante(red, alim_de, D, p0, limites)

    # 2) Medidas de contingencia (Regla 1): recorte parcial, nunca de cargas críticas
    if recorte_fijo is not None:
        rec = {n: np.zeros(HORAS) if critica[n] else
               np.minimum(np.clip(np.asarray(recorte_fijo.get(n, 0.0), float), 0, None), D[n]) for n in D}
    elif recortar:
        rec = {n: np.zeros(HORAS) if critica[n] else falta0[n].copy() for n in D}
    else:
        rec = {n: np.zeros(HORAS) for n in D}
    hay_recorte = any(v.any() for v in rec.values())
    D1 = {n: D[n] - rec[n] for n in D}
    if hay_recorte:
        p1 = _paso(claves, por_alim(D1), R, limites)
        falta1 = _repartir_faltante(red, alim_de, D1, p1, limites)
    else:
        p1, falta1 = p0, falta0

    # 3) Energía realmente servida: sin lo recortado ni lo que no cabe
    D2 = {n: D1[n] - falta1[n] for n in D}
    p2 = _paso(claves, por_alim(D2), R, limites) if any(v.any() for v in falta1.values()) else p1

    margen = {}
    for k in claves:
        m = (limites.alimentador_kw[k] - p1.flujo_cabecera[k]) / max(limites.alimentador_kw[k], _EPS)
        if np.isfinite(limites.transformador_bt_kw[k]):
            m = np.minimum(m, (limites.transformador_bt_kw[k] - p1.flujo_trafo_bt[k])
                           / max(limites.transformador_bt_kw[k], _EPS))
        margen[k] = m * 100
    costo = {k: p2.convencional_barra[k] * tarifas
             + (p2.renovable_local[k] + p2.renovable_importada[k]) * COSTO_RENOVABLE for k in claves}
    return Distribucion(
        claves=claves, limites=limites,
        demanda=por_alim(D),
        renovable_disponible=R,
        renovable_usada={k: p2.renovable_local[k] + p2.renovable_importada[k] for k in claves},
        renovable_desperdiciada=p2.desperdiciada,
        convencional=p2.convencional,
        perdidas=p2.perdidas,
        deficit=por_alim(falta0),
        recorte=por_alim(rec),
        no_servido=por_alim(falta1),
        flujo_cabecera=p1.flujo_cabecera,
        flujo_trafo_bt=p1.flujo_trafo_bt,
        margen_pct=margen,
        costo=costo,
        importacion_subestacion=sum(p2.convencional_barra.values()),
        requerido_subestacion=p1.requerido_subestacion,
        margen_subestacion_pct=(limites.subestacion_kw - p1.requerido_subestacion)
        / max(limites.subestacion_kw, _EPS) * 100,
        recorte_por_carga={n: v for n, v in rec.items() if v.any()},
        deficit_por_carga={n: v for n, v in falta0.items() if v.any()},
        no_servido_por_carga={n: v for n, v in falta1.items() if v.any()},
        con_carga_critica={alim_de[n] for n in D if critica[n]},
    )
