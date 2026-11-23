"""
Indicadores de evaluación de ElectriAgent en tres ejes: continuidad, eficiencia y costo.

Funciones PURAS: leen una `Distribucion` (distribucion.py), un `Escenario` (agentes.py) o un
`Sistema` terminado (motor.py) y devuelven números. No modifican nada ni dependen del estado
global. Cada función documenta su fórmula, sus unidades y de dónde salen los datos.

Convenciones
  - Las series de una Distribucion son de 24 h en kW; como el paso es de 1 h, kW = kWh por hora.
  - ENS (energía no suministrada) = recorte controlado + no servido (Distribucion.recorte y
    Distribucion.no_servido). El recorte es planificado y avisado (Regla 1); el no servido es
    déficit sin medida.
  - "Afectada" = una carga con ENS > UMBRAL_KW en una hora (mismo umbral de 0.05 que usa
    agentes.AgenteDeliberativo._metricas para `horas_deficit`).

Uso: evaluar.py arma la campaña; pruebas_evaluacion.py prueba cada indicador con un caso
calculado a mano.
"""
from __future__ import annotations

import numpy as np

from distribucion import Distribucion
from modelo_red import COSTO_RENOVABLE, RedDistribucion

UMBRAL_KW = 0.05
_EPS = 1e-9

# ISO/IEC 42005: grupos de interés por tipo de carga (los de agentes._GRUPOS más las críticas)
GRUPOS = {
    "Usuarios residenciales y comerciales": ("residencial", "comercial"),
    "Industria y servicios públicos": ("industrial", "servicio"),
    "Cargas críticas (hospital)": ("critica",),
}


def _r(x: float, n: int = 2) -> float:
    return round(float(x), n) + 0.0       # + 0.0 evita "-0.0"


# =================================================================== CONTINUIDAD
def ens_serie(d: Distribucion) -> np.ndarray:
    """ENS horaria de la red [kWh por hora] = Σ_alimentadores (recorte + no_servido).
    Fuente: Distribucion.recorte, Distribucion.no_servido."""
    return d.total("recorte") + d.total("no_servido")


def ens_kwh(d: Distribucion) -> float:
    """ENS del día [kWh/día] = Σ_h Σ_k (recorte_k,h + no_servido_k,h)."""
    return float(ens_serie(d).sum())


def ens_por_alimentador(d: Distribucion) -> dict[str, float]:
    """ENS_k [kWh/día] = Σ_h (recorte_k,h + no_servido_k,h), por alimentador k."""
    return {k: float((d.recorte[k] + d.no_servido[k]).sum()) for k in d.claves}


def ens_series_por_carga(d: Distribucion, red: RedDistribucion) -> dict[str, np.ndarray]:
    """ENS_c,h [kWh por hora] = recorte_c,h + no_servido_c,h por carga c.
    Fuente: Distribucion.recorte_por_carga y no_servido_por_carga (solo traen cargas con valor)."""
    cero = np.zeros(24)
    return {c.nombre: d.recorte_por_carga.get(c.nombre, cero) + d.no_servido_por_carga.get(c.nombre, cero)
            for c in red.cargas}


def ens_por_carga(d: Distribucion, red: RedDistribucion) -> dict[str, float]:
    """ENS_c [kWh/día] = Σ_h ENS_c,h. La suma sobre c coincide con ens_kwh(d)."""
    return {n: float(v.sum()) for n, v in ens_series_por_carga(d, red).items()}


def ens_por_grupo(d: Distribucion, red: RedDistribucion) -> dict[str, float]:
    """ENS por grupo de interés de ISO/IEC 42005 [kWh/día] = Σ_{c ∈ grupo} ENS_c."""
    por_carga = ens_por_carga(d, red)
    tipo = {c.nombre: c.tipo for c in red.cargas}
    return {g: float(sum(v for n, v in por_carga.items() if tipo[n] in tipos)) for g, tipos in GRUPOS.items()}


def ens_critica_kwh(d: Distribucion, red: RedDistribucion) -> float:
    """ENS de las cargas críticas (hospital) [kWh/día]. Regla 1: debe ser 0."""
    return float(sum(v for n, v in ens_por_carga(d, red).items()
                     if next(c for c in red.cargas if c.nombre == n).critica))


def demanda_kwh(d: Distribucion) -> float:
    """Demanda del día antes de cualquier medida [kWh/día] = Σ Distribucion.demanda."""
    return float(d.total("demanda").sum())


def servida_kwh(d: Distribucion) -> float:
    """Energía servida [kWh/día] = demanda − ENS."""
    return demanda_kwh(d) - ens_kwh(d)


def pct_servida(d: Distribucion) -> float:
    """% de la demanda servida = 100 × (1 − ENS / demanda). Sin demanda: 100 %."""
    dem = demanda_kwh(d)
    return 100.0 if dem <= _EPS else 100.0 * (1 - ens_kwh(d) / dem)


def pct_servida_por_alimentador(d: Distribucion) -> dict[str, float]:
    """% servida por alimentador = 100 × (1 − ENS_k / demanda_k)."""
    ens = ens_por_alimentador(d)
    out = {}
    for k in d.claves:
        dem = float(d.demanda[k].sum())
        out[k] = 100.0 if dem <= _EPS else 100.0 * (1 - ens[k] / dem)
    return out


def _eventos(afectada: np.ndarray) -> int:
    """Número de bloques de horas consecutivas afectadas."""
    a = np.asarray(afectada, bool)
    return int(a[0]) + int(np.sum(a[1:] & ~a[:-1])) if a.size else 0


def afectacion(d: Distribucion, red: RedDistribucion) -> dict:
    """Análogos diarios de SAIDI, SAIFI y CAIDI con las cargas como "usuarios".

      horas_c   = #{h : ENS_c,h > UMBRAL_KW}                         [h/día]
      eventos_c = bloques de horas consecutivas afectadas de la carga c [eventos/día]
      SAIDI_d   = Σ_c horas_c / N_cargas                              [h/carga·día]
      SAIFI_d   = Σ_c eventos_c / N_cargas                            [eventos/carga·día]
      CAIDI_d   = Σ_c horas_c / Σ_c eventos_c (duración media)        [h/evento]
      cargas_afectadas = #{c : horas_c > 0}
    """
    series = ens_series_por_carga(d, red)
    horas = {n: int((v > UMBRAL_KW).sum()) for n, v in series.items()}
    eventos = {n: _eventos(v > UMBRAL_KW) for n, v in series.items()}
    n = max(len(series), 1)
    th, te = sum(horas.values()), sum(eventos.values())
    return {"horas_por_carga": horas, "eventos_por_carga": eventos,
            "saidi_h": th / n, "saifi": te / n, "caidi_h": th / te if te else 0.0,
            "cargas_afectadas": sum(1 for h in horas.values() if h > 0)}


def horas_deficit(d: Distribucion) -> int:
    """Horas con déficit sin medida [h/día] = #{h : Σ_k no_servido_k,h > UMBRAL_KW}.
    Aplicado a Escenario.distribucion_p90 da las horas con déficit en P90."""
    return int((d.total("no_servido") > UMBRAL_KW).sum())


def ens_no_prevista_kwh(d_real: Distribucion, d_plan: Distribucion) -> float:
    """ENS no prevista [kWh/día] = no_servido(real) − no_servido(plan), acotada en 0.
    d_real: el MISMO plan (desplazamientos y recorte fijo) aplicado a la demanda y la renovable
    que realmente ocurren; d_plan: el plan en su banda de planificación (P50)."""
    return max(0.0, float(d_real.total("no_servido").sum() - d_plan.total("no_servido").sum()))


def resultado_decision(sistema, excepcion: str | None = None) -> str:
    """Continuidad del servicio de decisión: 'propuesta' (el operador aprobó una propuesta),
    'escalamiento' (C escaló o el operador rechazó sin alternativa) o 'roto' (excepción o el
    flujo no terminó). Fuente: Sistema.salida.reporte['estado']."""
    if excepcion or sistema is None or sistema.salida.reporte is None:
        return "roto"
    return "propuesta" if sistema.salida.reporte["estado"] == "APROBADA" else "escalamiento"


# ==================================================================== EFICIENCIA
def renovable(d: Distribucion) -> dict:
    """Aprovechamiento renovable [kWh/día y %].
      disponible = Σ renovable_disponible; usada = Σ renovable_usada;
      desperdiciada = Σ renovable_desperdiciada;
      pct_aprovechada = 100 × usada / disponible;
      pct_desperdiciada = 100 × desperdiciada / disponible;
      pct_demanda = 100 × usada / demanda (igual que metricas['renovable_pct'] de E)."""
    disp = float(d.total("renovable_disponible").sum())
    usada = float(d.total("renovable_usada").sum())
    desp = float(d.total("renovable_desperdiciada").sum())
    dem = demanda_kwh(d)
    return {"disponible_kwh": disp, "usada_kwh": usada, "desperdiciada_kwh": desp,
            "pct_aprovechada": 100 * usada / disp if disp > _EPS else 0.0,
            "pct_desperdiciada": 100 * desp / disp if disp > _EPS else 0.0,
            "pct_demanda": 100 * usada / max(dem, 1e-6)}


def perdidas(d: Distribucion) -> dict:
    """Pérdidas técnicas [kWh/día y %].
      kwh = Σ Distribucion.perdidas;
      entregada = Σ (renovable_usada + convencional)  (energía entregada en las cargas);
      pct = 100 × kwh / entregada."""
    kwh = float(d.total("perdidas").sum())
    entregada = float((d.total("renovable_usada") + d.total("convencional")).sum())
    return {"kwh": kwh, "entregada_kwh": entregada, "pct": 100 * kwh / entregada if entregada > _EPS else 0.0}


def utilizacion(d: Distribucion) -> dict:
    """Utilización máxima y media [%] de cada elemento.
      alimentador k:  flujo_cabecera_k,h / capacidad_k                (Distribucion.flujo_cabecera)
      trafo BT de k:  flujo_trafo_bt_k,h / transformador_bt_k         (solo alimentadores de BT)
      subestación:    importacion_subestacion_h / capacidad_subestación
    Devuelve {elemento: {"max_pct", "media_pct"}}."""
    out = {}
    lim = d.limites
    for k in d.claves:
        u = d.flujo_cabecera[k] / max(lim.alimentador_kw[k], _EPS) * 100
        out[k] = {"max_pct": float(u.max()), "media_pct": float(u.mean())}
        if np.isfinite(lim.transformador_bt_kw[k]):
            t = d.flujo_trafo_bt[k] / max(lim.transformador_bt_kw[k], _EPS) * 100
            out[f"{k}-trafoBT"] = {"max_pct": float(t.max()), "media_pct": float(t.mean())}
    s = d.importacion_subestacion / max(lim.subestacion_kw, _EPS) * 100
    out["SE"] = {"max_pct": float(s.max()), "media_pct": float(s.mean())}
    return out


def factor_carga(serie: np.ndarray) -> float:
    """Factor de carga [adimensional] = promedio / pico. Sin flujo: 0."""
    s = np.asarray(serie, float)
    pico = float(s.max()) if s.size else 0.0
    return float(s.mean() / pico) if pico > _EPS else 0.0


def reduccion_pico_pct(d_base: Distribucion, d: Distribucion) -> float:
    """Reducción del pico de importación de la subestación frente a S0 [%]
    = 100 × (pico_S0 − pico) / pico_S0, con pico = max_h importacion_subestacion."""
    p0 = float(d_base.importacion_subestacion.max())
    return 100 * (p0 - float(d.importacion_subestacion.max())) / p0 if p0 > _EPS else 0.0


def desplazado_por_deficit_evitado(desplazado_kwh: float, ens_base: float, ens: float) -> float | None:
    """Energía desplazada por kWh de déficit evitado [kWh/kWh] = desplazado / (ENS_S0 − ENS).
    None si la propuesta no evita déficit (no hay nada que dividir)."""
    evitado = ens_base - ens
    return desplazado_kwh / evitado if evitado > UMBRAL_KW else None


def n_acciones(esc) -> int:
    """Acciones que se le piden al operador = bloques de movimiento de carga (Escenario.movimientos)
    + cargas con recorte (Escenario.recorte con ≥ 0.05 kWh). S0 pide 0 acciones."""
    if esc is None:
        return 0
    recortadas = sum(1 for v in (esc.recorte or {}).values() if float(np.sum(v)) >= UMBRAL_KW)
    return len(esc.movimientos) + recortadas


# ---------------------------------------------------------------- pronóstico
def mape_pct(pronostico: np.ndarray, real: np.ndarray) -> float | None:
    """MAPE [%] = 100 × promedio_h |pronóstico_h − real_h| / real_h, en horas con real_h > 0.
    Mismo cálculo que el MAPE de validación de D (agentes.py), aplicado al día real."""
    p, r = np.asarray(pronostico, float), np.asarray(real, float)
    m = r > _EPS
    return float(np.mean(np.abs(p[m] - r[m]) / r[m]) * 100) if m.any() else None


def sesgo_pct(pronostico: np.ndarray, real: np.ndarray) -> float | None:
    """Sesgo del pronóstico [%] = 100 × (Σ_h pronóstico_h − Σ_h real_h) / Σ_h real_h.
    Negativo: subestima la demanda (riesgo de ENS no prevista); positivo: la sobreestima
    (recortes mayores de lo necesario). El MAPE no distingue un error sistemático del ruido."""
    p, r = float(np.sum(pronostico)), float(np.sum(real))
    return 100 * (p - r) / r if r > _EPS else None


def cobertura(p10: np.ndarray, p90: np.ndarray, real: np.ndarray, minimo_kw: float = 1.0,
              tol: float = 1e-6) -> tuple[int, int]:
    """Cobertura de una banda: (lecturas dentro de [P10, P90], lecturas evaluadas).
    Se omiten las horas en que la banda y el valor real valen < minimo_kw (p. ej. la solar de
    noche), que inflarían la cobertura. Cobertura [%] = 100 × dentro / evaluadas."""
    p10, p90, r = (np.asarray(x, float) for x in (p10, p90, real))
    m = np.maximum(p90, r) >= minimo_kw
    dentro = (r >= p10 - tol) & (r <= p90 + tol)
    return int((dentro & m).sum()), int(m.sum())


def ancho_relativo_pct(p10: np.ndarray, p50: np.ndarray, p90: np.ndarray) -> float:
    """Ancho relativo medio de la banda [%] = 100 × promedio_h (P90 − P10) / P50, en horas con P50 > 0."""
    p10, p50, p90 = (np.asarray(x, float) for x in (p10, p50, p90))
    m = p50 > _EPS
    return float(np.mean((p90[m] - p10[m]) / p50[m]) * 100) if m.any() else 0.0


# -------------------------------------------------------------------- sistema
def coordinacion(sistema) -> dict:
    """Costo de coordinación del grafo A–H: suma de los pesos de las aristas recorridas
    (BusCoordinacion.costo_total, pesos en coordinacion.ARISTAS) y mensajes enviados
    (BusCoordinacion.enviados) en el análisis."""
    return {"costo": int(sistema.bus.costo_total), "mensajes": int(sistema.bus.enviados)}


def percentil(valores, q: float) -> float | None:
    """Percentil q [mismas unidades] (numpy, interpolación lineal). Para el p95 de la API."""
    v = [float(x) for x in valores]
    return float(np.percentile(v, q)) if v else None


# ======================================================================== COSTO
def costo_energia_mxn(d: Distribucion, tarifas: np.ndarray) -> float:
    """Costo de la energía del día [MXN], recalculado desde las series:
      Σ_h importacion_subestacion_h × tarifa_h + COSTO_RENOVABLE × Σ_h renovable_usada_h
    con tarifa_h de RedDistribucion.vector_tarifas() (base / intermedia / punta) y
    COSTO_RENOVABLE de modelo_red.py. Coincide con Σ Distribucion.costo."""
    return float(np.sum(d.importacion_subestacion * np.asarray(tarifas, float))
                 + COSTO_RENOVABLE * d.total("renovable_usada").sum())


def costo_por_kwh_servido(d: Distribucion, tarifas: np.ndarray) -> float | None:
    """Costo por kWh SERVIDO [MXN/kWh] = costo de la energía / energía servida.
    Compara propuestas a igual energía servida (un plan que recorta más compra menos)."""
    s = servida_kwh(d)
    return costo_energia_mxn(d, tarifas) / s if s > _EPS else None


def descomponer_ahorro(d_base: Distribucion, d: Distribucion, tarifas: np.ndarray) -> dict:
    """Ahorro de la propuesta frente a S0 [MXN] y su descomposición EXACTA:

      ahorro = C_S0 − C, con C = E · t̄ + c_r · RU
        E  = importación de la subestación [kWh] = convencional + pérdidas de la convencional (L)
        t̄  = tarifa media ponderada de la importación [MXN/kWh];  c_r = COSTO_RENOVABLE
        RU = renovable usada;  S = energía servida
      efecto_tarifa     = E · (t̄_S0 − t̄)                     (desplazamiento a horas baratas)
      efecto_renovable  = (RU − RU_S0) · (t̄_S0 − c_r)        (renovable que sustituye convencional)
      efecto_perdidas   = (L_S0 − L) · t̄_S0
      efecto_servicio   = (S_S0 − S) · t̄_S0                  (energía que se deja de comprar por
                                                             servir menos: NO es eficiencia)
    La suma de los cuatro efectos es igual al ahorro (prueba en pruebas_evaluacion.py)."""
    t = np.asarray(tarifas, float)

    def partes(x: Distribucion):
        e = float(x.importacion_subestacion.sum())
        tm = float(np.sum(x.importacion_subestacion * t)) / e if e > _EPS else 0.0
        ru = float(x.total("renovable_usada").sum())
        conv = float(x.total("convencional").sum())
        return e, tm, ru, e - conv, servida_kwh(x)
    e0, t0, ru0, l0, s0 = partes(d_base)
    e1, t1, ru1, l1, s1 = partes(d)
    out = {"costo_s0_mxn": costo_energia_mxn(d_base, t), "costo_mxn": costo_energia_mxn(d, t),
           "efecto_tarifa_mxn": e1 * (t0 - t1),
           "efecto_renovable_mxn": (ru1 - ru0) * (t0 - COSTO_RENOVABLE),
           "efecto_perdidas_mxn": (l0 - l1) * t0,
           "efecto_servicio_mxn": (s0 - s1) * t0}
    out["ahorro_mxn"] = out["costo_s0_mxn"] - out["costo_mxn"]
    out["ahorro_pct"] = 100 * out["ahorro_mxn"] / out["costo_s0_mxn"] if out["costo_s0_mxn"] > _EPS else 0.0
    return out


def costo_social_ens_mxn(d: Distribucion, red: RedDistribucion, costo_ens: dict[str, float],
                         factor_sin_aviso: float = 1.0) -> float:
    """Costo social de la ENS [MXN] = Σ_c (recorte_c + factor_sin_aviso × no_servido_c) × CENS(tipo_c)
    CENS: costo de la energía no suministrada por tipo de carga [MXN/kWh] (supuesto ilustrativo,
    tabla de supuestos del informe). factor_sin_aviso ≥ 1 encarece el déficit no planificado
    frente al recorte avisado."""
    cero = np.zeros(24)
    total = 0.0
    for c in red.cargas:
        rec = float(d.recorte_por_carga.get(c.nombre, cero).sum())
        ns = float(d.no_servido_por_carga.get(c.nombre, cero).sum())
        total += (rec + factor_sin_aviso * ns) * costo_ens[c.tipo]
    return total


def costo_total_mxn(d: Distribucion, red: RedDistribucion, tarifas: np.ndarray, costo_ens: dict[str, float],
                    factor_sin_aviso: float = 1.0) -> float:
    """Costo total [MXN] = costo de la energía + costo social de la ENS."""
    return costo_energia_mxn(d, tarifas) + costo_social_ens_mxn(d, red, costo_ens, factor_sin_aviso)


def costo_robustez(d_min: Distribucion, d_rob: Distribucion, red: RedDistribucion, costo_ens: dict[str, float],
                   evitado_kwh: float, prob_desfavorable: float, factor_sin_aviso: float = 1.0) -> dict:
    """Costo de la robustez del plan P90 frente al plan mínimo P50 [MXN]:
      costo     = Σ_c (recorte_rob_c − recorte_min_c) × CENS(tipo_c)   (≈ recorte_adicional × CENS)
      beneficio = prob_desfavorable × evitado_kwh × CENS_media × factor_sin_aviso
    con evitado_kwh = deficit_p90_evitado_kwh de E y CENS_media = costo medio por kWh del recorte
    adicional. neto = beneficio − costo (> 0: la robustez se paga)."""
    cero = np.zeros(24)
    extra_c = {c.nombre: float(d_rob.recorte_por_carga.get(c.nombre, cero).sum()
                               - d_min.recorte_por_carga.get(c.nombre, cero).sum()) for c in red.cargas}
    tipo = {c.nombre: c.tipo for c in red.cargas}
    costo = sum(max(v, 0.0) * costo_ens[tipo[n]] for n, v in extra_c.items())
    extra = sum(max(v, 0.0) for v in extra_c.values())
    cens_media = costo / extra if extra > _EPS else 0.0
    beneficio = prob_desfavorable * evitado_kwh * cens_media * factor_sin_aviso
    return {"recorte_adicional_kwh": extra, "costo_mxn": costo, "beneficio_esperado_mxn": beneficio,
            "neto_mxn": beneficio - costo, "cens_media_mxn_kwh": cens_media}
