"""
Agentes del sistema multi-agente ElectriAgent (nodos A-H del grafo).

  A Usuario               -> elige el periodo de análisis
  B Captura de datos IoT  -> recolecta y prepara las lecturas
  C Agente de meta        -> supervisor: objetivos, enrutamiento, reglas de decisión
  D Agente de aprendizaje -> patrones, horas pico, anomalías, pronóstico
  E Agente deliberativo   -> genera y simula escenarios de operación
  F Evaluación ISO        -> riesgo (23894), impacto (42005), trazabilidad (5338)
  G Aprobación humana     -> acepta o rechaza la recomendación
  H Recomendación final   -> entrega el resultado y los archivos
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime

import numpy as np

from condiciones import (CONDICIONES, factor_demanda, factor_renovable, factores_capacidad,
                         parametros, resolver)
from coordinacion import Agente, Mensaje, Performativa as P, huella
from distribucion import Distribucion, Limites, asignar, limites_red
from modelo_red import NOMBRE_PERIODO, PERIODOS, RedDistribucion, SimuladorIoT

VERSION_MODELO = "ElectriAgent-proto 0.2"
TOLERANCIA_RIESGO = 12        # nivel máximo aceptable (probabilidad x impacto)
MOTIVO_SIN_MEDIDAS = "No quedan medidas de contingencia; la red no cubre la demanda"
MARGEN_OBJETIVO = 0.10        # margen mínimo de capacidad deseado
MAPE_REENTRENAR = 15.0        # % de error que dispara alerta de deriva (ISO/IEC 5338)
COBERTURA_NOMINAL = 80.0      # % esperado de lecturas dentro de la banda P10-P90
COBERTURA_MINIMA = 60.0       # por debajo: alerta de calibración de las bandas (ISO/IEC 5338)
CUANTILES = (10, 50, 90)


# ============================================================ A - Usuario
class Usuario(Agente):
    def iniciar(self, periodo: str, conversacion: str):
        self.log(f"solicita análisis del periodo: {NOMBRE_PERIODO[periodo]}")
        self.enviar("B", P.REQUEST, {"periodo": periodo}, conversacion)

    def procesar(self, msg: Mensaje):
        pass


# ============================================================ B - IoT
class CapturaIoT(Agente):
    def __init__(self, *a, simulador: SimuladorIoT, fin: datetime, **k):
        super().__init__(*a, **k)
        self.simulador = simulador
        self.fin = fin

    def procesar(self, msg: Mensaje):
        periodo = msg.contenido["periodo"]
        datos = self.simulador.capturar(periodo, self.fin)
        perdidas = 0
        for nombre, serie in list(datos["consumo"].items()) + list(datos["generacion"].items()):
            nan = np.isnan(serie)
            perdidas += int(nan.sum())
            if nan.any():   # preparación: interpolación de lecturas perdidas
                idx = np.arange(len(serie))
                serie[nan] = np.interp(idx[nan], idx[~nan], serie[~nan])
        datos["lecturas_perdidas_interpoladas"] = perdidas
        self.pizarra.escribir("B", "datos", datos)
        n = len(datos["tiempos"])
        self.log(f"{n} lecturas horarias x {len(datos['consumo'])} nodos de carga; "
                 f"{perdidas} lecturas perdidas interpoladas")
        self.enviar("C", P.INFORM, {"clave": "datos", "horas": n,
                                    "huella": huella({k: v.tolist() for k, v in datos['consumo'].items()})},
                    msg.conversacion)


# ============================================================ D - Aprendizaje
class AgenteAprendizaje(Agente):
    def __init__(self, *a, ruta_memoria: str, fallar: bool = False, **k):
        super().__init__(*a, **k)
        self.ruta_memoria = ruta_memoria
        self.fallar = fallar

    @staticmethod
    def _referencia(x: np.ndarray, tiempos: list, dias: int) -> np.ndarray:
        """Valor esperado de cada lectura: mediana de la misma hora en días del
        mismo tipo (laboral / fin de semana) dentro de una ventana de ±14 días.
        Así la referencia sigue la estacionalidad en periodos largos."""
        if dias < 3:
            return np.array([np.median(x[max(0, i - 2):i + 3]) for i in range(len(x))])
        m = x.reshape(dias, 24)
        finde = np.array([tiempos[d * 24].weekday() >= 5 for d in range(dias)])
        ref = np.empty_like(m)
        for d in range(dias):
            ventana = np.arange(max(0, d - 14), min(dias, d + 15))
            mismos = ventana[finde[ventana] == finde[d]]
            if len(mismos) < 2:
                mismos = ventana
            ref[d] = np.median(m[mismos], axis=0)
        return ref.ravel()

    @staticmethod
    def _pronostico(m: np.ndarray) -> np.ndarray:
        """m: matriz días x 24. Promedio ponderado de los últimos días + tendencia."""
        dias = m.shape[0]
        k = min(7, dias)
        pesos = np.arange(1, k + 1)
        base = (m[-k:] * pesos[:, None]).sum(0) / pesos.sum()
        if dias >= 14:
            tendencia = np.clip(m[-7:].mean() / m[-14:-7].mean(), 0.9, 1.1)
            base = base * tendencia
        return base

    @classmethod
    def _residuales(cls, m: np.ndarray) -> np.ndarray:
        """Errores relativos históricos del pronóstico de carga: cada día se
        pronostica con los días previos (backtest de hasta 60 días). Con un solo
        día se usa la desviación frente a la mediana local de ±2 horas."""
        dias = m.shape[0]
        if dias >= 2:
            res = []
            for d in range(max(1, dias - 60), dias):
                p = cls._pronostico(m[:d])
                res.append((m[d] - p) / np.maximum(p, 1e-6))
            return np.concatenate(res)
        x = m[0]
        ref = np.array([np.median(x[max(0, i - 2):i + 3]) for i in range(len(x))])
        return (x - ref) / np.maximum(ref, 1e-6)

    @staticmethod
    def _residuales_renovable(m: np.ndarray) -> np.ndarray:
        """Error relativo de la energía diaria renovable (la nubosidad y el viento
        cambian el día completo). Sin historial se usa una banda por omisión."""
        dias = m.shape[0]
        if dias < 2:
            return np.array([-0.35, 0.0, 0.20])
        res = []
        for d in range(max(1, dias - 60), dias):
            p = m[max(0, d - 3):d].mean(0)
            res.append(m[d].sum() / max(p.sum(), 1e-6) - 1)
        return np.array(res)

    @staticmethod
    def _banda(central: np.ndarray, res: np.ndarray) -> dict:
        """Bandas P10/P50/P90 centradas en el pronóstico puntual: P50 es el valor
        central y P10/P90 usan la distancia de esos cuantiles a la mediana de los
        residuales. Por construcción P10 <= P50 <= P90."""
        q10, q50, q90 = np.percentile(res, CUANTILES)
        return {"p10": np.clip(central * (1 + q10 - q50), 0, None),
                "p50": central.copy(),
                "p90": central * (1 + q90 - q50)}

    @classmethod
    def _cobertura(cls, limpias: dict, reales: dict, dias: int) -> dict:
        """Validación fuera de muestra de las bandas: se construyen con los días
        previos al último y se cuenta cuántas lecturas reales del último día caen
        dentro de P10-P90 (ISO/IEC 5338, monitoreo)."""
        if dias < 3:
            return {"pct": None, "dentro": 0, "lecturas": 0, "nominal_pct": COBERTURA_NOMINAL}
        dentro = total = 0
        for nombre, x in limpias.items():
            m = x.reshape(dias, 24)[:-1]
            b = cls._banda(cls._pronostico(m), cls._residuales(m))
            real = reales[nombre][-24:]
            dentro += int(((real >= b["p10"] - 1e-6) & (real <= b["p90"] + 1e-6)).sum())
            total += 24
        return {"pct": round(dentro / total * 100, 1), "dentro": dentro, "lecturas": total,
                "nominal_pct": COBERTURA_NOMINAL}

    def procesar(self, msg: Mensaje):
        if self.fallar:
            self.fallar = False
            raise RuntimeError("tiempo de espera agotado al cargar el modelo (falla simulada)")

        datos = self.pizarra.leer(msg.contenido["clave"])
        tiempos = datos["tiempos"]
        dias = len(tiempos) // 24
        injectadas = set(datos["anomalias_inyectadas"])

        anomalias, limpias = [], {}
        for nombre, x in datos["consumo"].items():
            ref = self._referencia(x, tiempos, dias)
            r = (x - ref) / np.maximum(ref, 1e-6)          # desviación relativa
            mad = np.median(np.abs(r - np.median(r))) * 1.4826 + 1e-9
            z = (r - np.median(r)) / mad
            umbral = 0.25 if dias >= 3 else 0.45     # con 1-2 días la referencia es menos precisa
            idx = np.where((np.abs(z) > 5) & (np.abs(r) > umbral))[0]
            for i in idx:
                anomalias.append({"carga": nombre, "fecha": tiempos[i].isoformat(timespec="minutes"),
                                  "kw": round(float(x[i]), 1), "esperado_kw": round(float(ref[i]), 1),
                                  "z": round(float(z[i]), 1)})
            xc = x.copy()
            xc[idx] = ref[idx]           # se limpian antes de aprender
            limpias[nombre] = xc

        detectadas = {(a["carga"], a["fecha"]) for a in anomalias}
        aciertos = sum((c, t.isoformat(timespec="minutes")) in detectadas for c, t in injectadas)

        total = sum(limpias.values())
        perfil_horario = total.reshape(dias, 24).mean(0)
        horas_pico = sorted(np.argsort(perfil_horario)[-3:].tolist())
        i_pico = int(np.argmax(total))

        estacional = {}
        if dias >= 60:
            for t, v in zip(tiempos, total):
                estacional.setdefault(t.strftime("%Y-%m"), []).append(v)
            estacional = {k: round(float(np.mean(v)), 1) for k, v in estacional.items()}

        # Pronóstico del siguiente día (por carga y por fuente renovable)
        memoria = self._leer_memoria()
        pron_cargas = {}
        for nombre, x in limpias.items():
            p = self._pronostico(x.reshape(dias, 24))
            if nombre in memoria["perfiles"]:     # aprende de ejecuciones anteriores
                p = 0.75 * p + 0.25 * np.array(memoria["perfiles"][nombre])
            pron_cargas[nombre] = p
        pron_renov = {n: g.reshape(dias, 24)[-min(3, dias):].mean(0)
                      for n, g in datos["generacion"].items()}

        # Bandas de pronóstico (cuantiles de los residuales históricos)
        bandas_cargas = {n: self._banda(pron_cargas[n], self._residuales(x.reshape(dias, 24)))
                         for n, x in limpias.items()}
        bandas_renov = {n: self._banda(pron_renov[n], self._residuales_renovable(g.reshape(dias, 24)))
                        for n, g in datos["generacion"].items()}
        cobertura = self._cobertura(limpias, datos["consumo"], dias)

        mape = None
        if dias >= 2:   # validación: pronosticar el último día con los anteriores
            pred = sum(self._pronostico(x.reshape(dias, 24)[:-1]) for x in limpias.values())
            real = total[-24:]
            mape = round(float(np.mean(np.abs(pred - real) / real) * 100), 2)

        memoria["ejecuciones"] += 1
        memoria["perfiles"] = {k: v.round(2).tolist() for k, v in pron_cargas.items()}
        memoria["historial_mape"].append(mape)
        self._guardar_memoria(memoria)

        analisis = {
            "dias": dias,
            "consumo_total_kwh": round(float(total.sum()), 0),
            "consumo_por_carga_kwh": {k: round(float(v.sum()), 0) for k, v in limpias.items()},
            "pico_kw": round(float(total[i_pico]), 1),
            "fecha_pico": tiempos[i_pico].isoformat(timespec="minutes"),
            "horas_pico": horas_pico,
            "perfil_horario_kw": perfil_horario.round(1).tolist(),
            "variacion_estacional_kw": estacional,
            "anomalias": anomalias,
            "deteccion": {"inyectadas": len(injectadas), "detectadas": len(anomalias),
                          "aciertos": aciertos},
            "generacion_renovable_kwh": round(float(sum(g.sum() for g in datos["generacion"].values())), 0),
            "pronostico_cargas": pron_cargas,
            "pronostico_renovables": pron_renov,
            "bandas_cargas": bandas_cargas,
            "bandas_renovables": bandas_renov,
            "cobertura_bandas": cobertura,
            "mape_pct": mape,
            "experiencia_ejecuciones": memoria["ejecuciones"],
        }
        self.pizarra.escribir("D", "analisis", analisis)
        cob = "sin datos" if cobertura["pct"] is None else f"{cobertura['pct']} %"
        self.log(f"consumo {analisis['consumo_total_kwh']:,.0f} kWh, horas pico {horas_pico}, "
                 f"{len(anomalias)} anomalías, error de pronóstico (MAPE) {mape}%, "
                 f"bandas P10-P90 con cobertura {cob}")
        self.enviar("C", P.INFORM, {"clave": "analisis", "mape_pct": mape,
                                    "anomalias": len(anomalias)}, msg.conversacion)

    def _leer_memoria(self) -> dict:
        try:
            with open(self.ruta_memoria, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"ejecuciones": 0, "perfiles": {}, "historial_mape": []}

    def _guardar_memoria(self, memoria: dict):
        with open(self.ruta_memoria, "w", encoding="utf-8") as f:
            json.dump(memoria, f, ensure_ascii=False, indent=1)


# ============================================================ E - Deliberativo
@dataclass
class Contexto:
    """Datos del día para simular: pronósticos por banda con la condición ya aplicada."""
    cargas: dict              # P50 (valor central) por carga, serie de 24 h
    cargas_p10: dict
    cargas_p90: dict
    renovables: dict          # P50 por fuente
    renovables_p10: dict
    renovables_p90: dict
    limites: Limites
    tarifas: np.ndarray


@dataclass
class Escenario:
    id: str
    nombre: str
    estrategia: str
    intensidad: float
    demanda: np.ndarray            # demanda total planificada (tras desplazar y recortar)
    renovable: np.ndarray          # renovable total disponible (P50)
    capacidad_kw: float            # capacidad efectiva del transformador de la subestación
    tarifas: np.ndarray
    desplazamientos: dict = field(default_factory=dict)   # carga -> kWh movidos
    movimientos: list = field(default_factory=list)       # (horas origen, horas destino, kWh)
    suspensiones: list = field(default_factory=list)      # (hora, carga, kW) recorte parcial
    metricas: dict = field(default_factory=dict)          # evaluación en P50
    cargas: dict = field(default_factory=dict)            # carga -> 24 h tras desplazar (antes del recorte)
    recorte: dict = field(default_factory=dict)           # carga -> 24 h de recorte planificado
    base_planificacion: str = "P50"                       # banda con la que se calculó el recorte
    distribucion: Distribucion | None = None              # asignación por alimentador en P50
    distribucion_p90: Distribucion | None = None          # asignación en el escenario desfavorable
    metricas_p90: dict = field(default_factory=dict)
    robusto: bool = False                                 # sin déficit en P90


class AgenteDeliberativo(Agente):
    """Simula estrategias de operación y asigna la energía por alimentador.

    Cada estrategia se evalúa en dos escenarios de demanda:
      P50 -> pronóstico central de D
      P90 -> desfavorable: demanda P90 y renovables P10
    Una estrategia es robusta si no deja déficit en P90."""

    def __init__(self, *a, red: RedDistribucion, fallar: bool = False, **k):
        super().__init__(*a, **k)
        self.red = red
        self.fallar = fallar

    # -- Pronósticos del día con la condición de la red
    def _contexto(self, analisis: dict, cond: dict) -> Contexto:
        bc, br = analisis["bandas_cargas"], analisis["bandas_renovables"]
        c50, c10, c90, r50, r10, r90 = {}, {}, {}, {}, {}, {}
        for c in self.red.cargas:
            f = factor_demanda(cond, c.tipo)
            c50[c.nombre] = analisis["pronostico_cargas"][c.nombre] * f
            c10[c.nombre] = bc[c.nombre]["p10"] * f
            c90[c.nombre] = bc[c.nombre]["p90"] * f
        for r in self.red.renovables:
            f = factor_renovable(cond, r.tipo)
            r50[r.nombre] = np.clip(analisis["pronostico_renovables"][r.nombre] * f, 0, r.capacidad_kw)
            r10[r.nombre] = np.clip(br[r.nombre]["p10"] * f, 0, r.capacidad_kw)
            r90[r.nombre] = np.clip(br[r.nombre]["p90"] * f, 0, r.capacidad_kw)
        fs, fa = factores_capacidad(cond)
        return Contexto(c50, c10, c90, r50, r10, r90, limites_red(self.red, fs, fa),
                        self.red.vector_tarifas())

    def _metricas(self, d: Distribucion, cargas: dict, desplazamientos: dict,
                  demanda_original_kwh: float) -> dict:
        tipo = {c.nombre: c.tipo for c in self.red.cargas}

        def por_tipo(valores: dict) -> dict:
            out = {}
            for n, v in valores.items():
                kwh = float(np.sum(v))
                if kwh >= 0.05:
                    out[tipo[n]] = round(out.get(tipo[n], 0.0) + kwh, 1)
            return out
        D = d.total("demanda")
        usada = d.total("renovable_usada")
        no_serv = d.total("no_servido")
        recorte = float(d.total("recorte").sum())
        desplazado = float(sum(desplazamientos.values()))
        margen_se = float(d.margen_subestacion_pct.min())
        return {
            "demanda_kwh": round(float(D.sum()), 1),
            "renovable_usada_kwh": round(float(usada.sum()), 1),
            "renovable_desperdiciada_kwh": round(float(d.total("renovable_desperdiciada").sum()), 1),
            "renovable_pct": round(float(usada.sum() / max(D.sum(), 1e-6) * 100), 2),
            "convencional_kwh": round(float(d.total("convencional").sum()), 1),
            "perdidas_kwh": round(float(d.total("perdidas").sum()), 1),
            "importacion_subestacion_kwh": round(float(d.importacion_subestacion.sum()), 1),
            "pico_convencional_kw": round(float(d.requerido_subestacion.max()), 1),
            "margen_min_pct": round(margen_se, 2) + 0.0,       # + 0.0 evita mostrar "-0.0"
            "margen_red_min_pct": round(min([margen_se] + [float(d.margen_pct[k].min()) for k in d.claves]), 2) + 0.0,
            "utilizacion_max_pct": d.utilizacion_max_pct(),
            "deficit_kwh": round(float(no_serv.sum()), 1),
            "deficit_previo_kwh": round(float(d.total("deficit").sum()), 1),
            "horas_deficit": [int(h) for h in np.where(no_serv > 0.05)[0]],
            "costo_mxn": round(float(d.total("costo").sum()), 2),
            "desplazado_kwh": round(desplazado, 1),
            "desplazado_pct": round(desplazado / demanda_original_kwh * 100, 2),
            "suspendido_kwh": round(recorte, 1),          # nombre histórico: hoy es recorte parcial
            "recorte_kwh": round(recorte, 1),
            "recorte_por_carga_kwh": {n: round(float(v.sum()), 1) for n, v in d.recorte_por_carga.items()
                                      if v.sum() >= 0.05},
            "recorte_por_tipo_kwh": por_tipo(d.recorte_por_carga),
            "no_servido_por_tipo_kwh": por_tipo(d.no_servido_por_carga),
            "demanda_por_tipo_kwh": por_tipo(cargas),
            "desplazado_por_tipo_kwh": por_tipo(desplazamientos),
            "alimentadores": d.resumen(),
        }

    def _evaluar(self, esc: Escenario, ctx: Contexto, demanda_original_kwh: float):
        """Asigna la energía del escenario en P50 y en P90 y calcula sus métricas."""
        plan = esc.recorte or None
        d50 = asignar(self.red, esc.cargas, ctx.renovables, ctx.limites, ctx.tarifas, recorte_fijo=plan)
        c90 = {n: np.clip(ctx.cargas_p90[n] + esc.cargas[n] - ctx.cargas[n], 0, None) for n in esc.cargas}
        d90 = asignar(self.red, c90, ctx.renovables_p10, ctx.limites, ctx.tarifas, recorte_fijo=plan)
        esc.distribucion, esc.distribucion_p90 = d50, d90
        esc.metricas = self._metricas(d50, esc.cargas, esc.desplazamientos, demanda_original_kwh)
        esc.metricas_p90 = self._metricas(d90, c90, esc.desplazamientos, demanda_original_kwh)
        esc.robusto = esc.metricas_p90["deficit_kwh"] == 0
        esc.metricas.update({
            "robusto": esc.robusto,
            "deficit_p90_kwh": esc.metricas_p90["deficit_kwh"],
            "margen_red_min_p90_pct": esc.metricas_p90["margen_red_min_pct"],
            "base_planificacion": esc.base_planificacion,
            "deficit_planificado_kwh": esc.metricas["deficit_previo_kwh"],
        })
        esc.demanda = d50.total("demanda") - d50.total("recorte")
        esc.suspensiones = [(h, n, round(float(v[h]), 1)) for n, v in d50.recorte_por_carga.items()
                            for h in range(24) if v[h] >= 0.05]

    # -- Movimiento de carga flexible (energía constante por carga, respeta capacidades)
    def _mover(self, esc, ctx, flex_rest, donantes, receptores, pesos, intensidad):
        no_criticas = [c for c in self.red.cargas if not c.critica]
        cargas = ctx.cargas
        oferta = {h: intensidad * flex_rest[h] for h in donantes}
        total = sum(oferta.values())
        if total <= 0:
            return
        # Qué fracción del movimiento aporta cada carga (y por tanto cada alimentador)
        partes = {}
        for h in donantes:
            den = sum(c.desplazable * cargas[c.nombre][h] for c in no_criticas)
            partes[h] = {c.nombre: c.desplazable * cargas[c.nombre][h] / den if den > 0 else 0.0
                         for c in no_criticas}
        cuota = {}
        for c in no_criticas:
            k = self.red.alimentador(c.alimentador).clave
            cuota[k] = cuota.get(k, 0.0) + sum(oferta[h] * partes[h][c.nombre] for h in donantes) / total

        # Holgura en los receptores: 10 % de margen en la subestación y en cada alimentador
        demanda = sum(esc.cargas.values())
        holgura = 0.90 * ctx.limites.subestacion_kw + esc.renovable - demanda
        for k, q in cuota.items():
            if q <= 1e-9:
                continue
            dk = sum(esc.cargas[c.nombre] for c in self.red.cargas_de(k))
            rk = sum((ctx.renovables[r.nombre] for r in self.red.renovables_de(k)), np.zeros(24))
            holgura = np.minimum(holgura, (0.90 * ctx.limites.entregable_kw(k) + rk - dk) / q)
        holgura = np.clip(holgura[receptores], 0, None)
        if holgura.sum() <= 0:
            return
        movible = min(total, holgura.sum())
        f = movible / total
        movido = {}
        for h in donantes:
            quitar = oferta[h] * f
            if flex_rest[h] > 0:
                for c in no_criticas:
                    x = quitar * partes[h][c.nombre]
                    esc.desplazamientos[c.nombre] = esc.desplazamientos.get(c.nombre, 0) + x
                    esc.cargas[c.nombre][h] -= x
                    movido[c.nombre] = movido.get(c.nombre, 0) + x
            flex_rest[h] -= quitar
        reparto = np.minimum(holgura, movible * np.array(pesos) / np.sum(pesos))
        faltante = movible - reparto.sum()
        while faltante > 1e-6:      # redistribuye lo que no cupo por holgura
            libre = holgura - reparto
            if libre.sum() <= 1e-6:
                break
            extra = np.minimum(libre, faltante * libre / libre.sum())
            reparto += extra
            faltante -= extra.sum()
        reparto *= movible / reparto.sum()          # cada carga conserva su energía
        for h, v in zip(receptores, reparto):
            for n, kwh in movido.items():
                esc.cargas[n][h] += v * kwh / movible
        esc.movimientos.append((list(donantes), list(receptores), round(float(movible), 1)))

    def _simular(self, id_, nombre, estrategia, intensidad, ctx: Contexto):
        cargas = ctx.cargas
        D0 = sum(cargas.values())
        R = sum(ctx.renovables.values())
        flex = sum(c.desplazable * cargas[c.nombre] for c in self.red.cargas if not c.critica)
        esc = Escenario(id_, nombre, estrategia, intensidad, D0.copy(), R, ctx.limites.subestacion_kw,
                        ctx.tarifas, cargas={n: v.copy() for n, v in cargas.items()})
        flex_rest = flex.copy()
        if estrategia in ("renovable", "combinada"):
            neta = D0 - R
            donantes = sorted(np.argsort(neta)[-4:].tolist())
            receptores = [h for h in range(24)
                          if R.max() > 0 and R[h] > 0.3 * R.max() and h not in donantes]
            if receptores:
                self._mover(esc, ctx, flex_rest, donantes, receptores,
                            [R[h] for h in receptores], intensidad)
        if estrategia in ("tarifa", "combinada"):
            donantes = [h for h in range(24) if self.red.periodo_tarifario(h) == "punta"]
            receptores = [h for h in range(24) if self.red.periodo_tarifario(h) == "base"]
            self._mover(esc, ctx, flex_rest, donantes, receptores, [1] * len(receptores), intensidad)
        self._evaluar(esc, ctx, float(D0.sum()))
        return esc

    def _contingencia(self, base: Escenario, ctx: Contexto, banda: str) -> Escenario:
        """Regla 1: recorte PARCIAL de cargas no críticas por prioridad, solo los kW
        necesarios. Con banda P90 el plan cubre también el escenario desfavorable."""
        if banda == "P90":
            plan_c = {n: np.clip(ctx.cargas_p90[n] + base.cargas[n] - ctx.cargas[n], 0, None)
                      for n in base.cargas}
            plan_r = ctx.renovables_p10
            id_, nombre = (f"{base.id}-CONT-P90",
                           f"Contingencia robusta: recorte parcial sobre {base.id} planificado con la banda P90")
        else:
            plan_c, plan_r = base.cargas, ctx.renovables
            id_, nombre = f"{base.id}-CONT", f"Contingencia: recorte parcial sobre {base.id}"
        plan = asignar(self.red, plan_c, plan_r, ctx.limites, ctx.tarifas, recortar=True)
        esc = Escenario(id_, nombre, "contingencia", base.intensidad, base.demanda.copy(), base.renovable,
                        base.capacidad_kw, base.tarifas, dict(base.desplazamientos), list(base.movimientos),
                        cargas={n: v.copy() for n, v in base.cargas.items()},
                        recorte=plan.recorte_por_carga, base_planificacion=banda)
        self._evaluar(esc, ctx, float(sum(ctx.cargas.values()).sum()))
        esc.metricas["deficit_planificado_kwh"] = round(float(plan.total("deficit").sum()), 1)
        return esc

    def procesar(self, msg: Mensaje):
        if self.fallar:
            self.fallar = False
            raise RuntimeError("el simulador de escenarios no respondió (falla simulada)")

        analisis = self.pizarra.leer("analisis")
        cond = resolver(msg.contenido["condicion"], **msg.contenido.get("parametros", {}))
        excluir = set(msg.contenido.get("excluir", []))
        ronda = msg.contenido.get("ronda", 0)
        ctx = self._contexto(analisis, cond)

        catalogo = [("S0", "Operación actual (sin cambios)", "base")]
        catalogo += [("S1", "Prioridad renovable: mover carga flexible a horas solares", "renovable"),
                     ("S2", "Desplazamiento de carga flexible de punta a base", "tarifa"),
                     ("S3", "Combinado: renovable + desplazamiento tarifario", "combinada")]
        intensidades = [1.0] + [0.5 ** r for r in range(1, ronda + 1)]   # variantes conservadoras

        escenarios = []
        for intensidad in intensidades:
            for id_, nombre, estr in catalogo:
                if estr == "base" and intensidad < 1:
                    continue
                sid = id_ if intensidad == 1 else f"{id_}-{int(intensidad * 100)}%"
                nom = nombre if intensidad == 1 else f"{nombre} (conservador {int(intensidad * 100)} %)"
                escenarios.append(self._simular(sid, nom, estr, intensidad, ctx))

        if all(e.metricas["deficit_kwh"] > 0 for e in escenarios):
            mejor = min(escenarios, key=lambda e: e.metricas["deficit_kwh"])
            minimo = self._contingencia(mejor, ctx, "P50")
            robusto = self._contingencia(mejor, ctx, "P90")
            # Cuánto más recorta el plan robusto y cuánto déficit P90 evita frente al mínimo
            extra = round(max(0.0, robusto.metricas["recorte_kwh"] - minimo.metricas["recorte_kwh"]), 1)
            evitado = round(max(0.0, minimo.metricas["deficit_p90_kwh"] - robusto.metricas["deficit_p90_kwh"]), 1)
            for e in (minimo, robusto):
                e.metricas["recorte_adicional_robustez_kwh"] = extra
                e.metricas["deficit_p90_evitado_kwh"] = evitado
            minimo.metricas["alternativa_robusta"] = robusto.id
            robusto.metricas["plan_minimo"] = minimo.id
            escenarios += [minimo, robusto]
        escenarios = [e for e in escenarios if e.id not in excluir]

        cap = ctx.limites.subestacion_kw
        self.pizarra.escribir("E", "escenarios", {"lista": escenarios, "condicion": cond["desc"],
                                                  "condicion_id": cond["id"], "parametros": parametros(cond),
                                                  "capacidad_kw": cap, "limites": ctx.limites,
                                                  "contexto": ctx})
        robustos = sum(e.robusto for e in escenarios)
        self.log(f"{len(escenarios)} escenarios simulados por alimentador en P50 y P90 "
                 f"({cond['desc']}, capacidad {cap:,.0f} kW); {robustos} robustos ante P90")
        self.enviar("F", P.PROPOSE, {"clave": "escenarios", "n": len(escenarios)}, msg.conversacion)


# ============================================================ F - Evaluación ISO
def _nivel(valor: int) -> str:
    return "bajo" if valor <= 6 else "medio" if valor <= 12 else "alto" if valor <= 19 else "crítico"


_GRUPOS = [   # ISO/IEC 42005: grupos de interés por tipo de carga
    ("Usuarios residenciales y comerciales", ("residencial", "comercial")),
    ("Industria y servicios públicos", ("industrial", "servicio")),
]


class EvaluadorISO(Agente):
    def __init__(self, *a, fallar: bool = False, **k):
        super().__init__(*a, **k)
        self.fallar = fallar

    def _riesgos(self, esc, mape: float | None) -> list[dict]:
        """ISO/IEC 23894: identificar, analizar (probabilidad x impacto) y tratar."""
        m, m90 = esc.metricas, esc.metricas_p90
        # Insuficiencia: la probabilidad se estima con el escenario desfavorable P90
        margen90 = m90["margen_red_min_pct"]
        p_cap = (5 if m90["deficit_kwh"] > 0 else 1 if margen90 >= 20 else 2 if margen90 >= 10
                 else 3 if margen90 >= 5 else 4)
        # Saturación: el alimentador o transformador de BT más cargado (P50, tras las medidas)
        alims = m["alimentadores"]
        k_max = max(alims, key=lambda k: alims[k]["utilizacion_max_pct"])
        u = alims[k_max]["utilizacion_max_pct"]
        p_sat = 5 if u > 100.05 else 4 if u >= 95 else 3 if u >= 90 else 2 if u >= 80 else 1
        margen = m["margen_red_min_pct"]
        e = mape if mape is not None else 20
        p_pron = 1 if e < 5 else 2 if e < 10 else 3 if e < 15 else 4 if e < 25 else 5
        d = m["desplazado_pct"]
        riesgos = [
            {"riesgo": "Insuficiencia de capacidad", "prob": p_cap, "impacto": 5,
             "tratamiento": f"Reservar margen y vigilar la carga del transformador (banda P90: margen "
                            f"{margen90} %" + (f", déficit {m90['deficit_kwh']:,.0f} kWh)" if m90["deficit_kwh"] else ")")},
            {"riesgo": "Saturación de alimentador", "prob": p_sat,
             "impacto": 5 if alims[k_max]["carga_critica"] else 4,
             "tratamiento": f"Vigilar la cabecera y los transformadores de {k_max} (uso máximo {u} %); "
                            "transferir carga o reconfigurar si se acerca al límite"},
            {"riesgo": "Error de pronóstico de demanda/renovables", "prob": p_pron,
             "impacto": 4 if margen < 10 else 3,
             "tratamiento": "Revisar el pronóstico con datos en tiempo real"},
            {"riesgo": "Afectación al usuario por desplazamiento de cargas",
             "prob": 3 if d > 0 else 1,
             "impacto": 1 if d < 2 else 2 if d < 5 else 3 if d < 10 else 4,
             "tratamiento": "Desplazar solo cargas no críticas y notificar con anticipación"},
        ]
        if m["recorte_kwh"] > 0:
            riesgos.append({"riesgo": "Recorte controlado del servicio", "prob": 5, "impacto": 4,
                            "tratamiento": f"Recorte parcial de {m['recorte_kwh']:,.0f} kWh por prioridad; cargas "
                                           "críticas protegidas; requiere autorización del operador"})
        for r in riesgos:
            r["nivel"] = r["prob"] * r["impacto"]
            r["categoria"] = _nivel(r["nivel"])
        return riesgos

    @staticmethod
    def _impactos(m: dict, base: dict) -> list[dict]:
        """ISO/IEC 42005: efectos sobre los grupos de interés con los kWh reales
        desplazados y recortados a cada grupo."""
        ahorro = base["costo_mxn"] - m["costo_mxn"]
        rec, dem, desp = m["recorte_por_tipo_kwh"], m["demanda_por_tipo_kwh"], m["desplazado_por_tipo_kwh"]

        def efecto(cond_pos, cond_neg):
            return "positivo" if cond_pos else "negativo" if cond_neg else "neutral"
        impactos = []
        for grupo, tipos in _GRUPOS:
            kwh = round(sum(rec.get(t, 0.0) for t in tipos), 1)
            movido = sum(desp.get(t, 0.0) for t in tipos)
            demanda = sum(dem.get(t, 0.0) for t in tipos)
            pct = round(kwh / demanda * 100, 1) if demanda else 0.0
            detalle = f"{movido:,.0f} kWh de consumo flexible cambian de horario"
            if kwh:
                detalle += f"; {kwh:,.0f} kWh recortados ({pct} % de su demanda del día)"
            impactos.append({"grupo": grupo, "efecto": efecto(False, movido > 0.5 or kwh > 0),
                             "kwh_recortados": kwh, "pct_demanda_recortada": pct, "detalle": detalle})
        critica_ns = m["no_servido_por_tipo_kwh"].get("critica", 0.0)
        impactos += [
            {"grupo": "Cargas críticas (hospital)",
             "efecto": "negativo" if critica_ns else "positivo" if m["recorte_kwh"] else "neutral",
             "kwh_recortados": round(m["recorte_por_tipo_kwh"].get("critica", 0.0), 1),
             "pct_demanda_recortada": 0.0,
             "detalle": (f"{critica_ns:,.0f} kWh sin servir: la red no alcanza ni para las cargas críticas"
                         if critica_ns else "0 kWh recortados: suministro garantizado (nunca se desplaza ni se recorta)")},
            {"grupo": "Operador de la red", "efecto": efecto(ahorro > 0, ahorro < 0),
             "detalle": f"Costo {m['costo_mxn']:,.0f} MXN ({_ahorro(ahorro)} frente a la operación actual); "
                        f"pérdidas técnicas {m['perdidas_kwh']:,.0f} kWh"},
            {"grupo": "Medio ambiente", "efecto": efecto(m["renovable_desperdiciada_kwh"] < base["renovable_desperdiciada_kwh"], False),
             "detalle": f"{m['renovable_pct']} % de la demanda cubierta con renovables"},
        ]
        return impactos

    def procesar(self, msg: Mensaje):
        if self.fallar:
            self.fallar = False
            raise RuntimeError("no se pudo cargar la matriz de riesgos (falla simulada)")

        paquete = self.pizarra.leer(msg.contenido["clave"])
        analisis = self.pizarra.leer("analisis")
        mape = analisis["mape_pct"]
        base = next((e.metricas for e in paquete["lista"] if e.id == "S0"), None)
        evaluados = []
        for esc in paquete["lista"]:
            riesgos = self._riesgos(esc, mape)
            riesgo_max = max(r["nivel"] for r in riesgos)
            evaluados.append({
                "id": esc.id,
                "riesgos": riesgos,
                "riesgo_max": riesgo_max,
                "categoria_max": _nivel(riesgo_max),
                "impactos": self._impactos(esc.metricas, base or esc.metricas),
                "aceptable": riesgo_max <= TOLERANCIA_RIESGO and esc.metricas["recorte_kwh"] == 0,
                "requiere_escalamiento": esc.metricas["recorte_kwh"] > 0,
                "robusto": esc.robusto,
                "deficit_p90_kwh": esc.metricas_p90["deficit_kwh"],
            })
        cob = analisis.get("cobertura_bandas") or {}
        cob_pct = cob.get("pct")
        alerta_cal = cob_pct is not None and cob_pct < COBERTURA_MINIMA
        acciones = []
        if mape and mape > MAPE_REENTRENAR:
            acciones.append("Re-entrenar / revisar el modelo de aprendizaje")
        if alerta_cal:
            acciones.append("Recalibrar las bandas P10-P90 (cobertura por debajo del mínimo)")
        trazabilidad = {   # ISO/IEC 5338: ciclo de vida, monitoreo y registro
            "version_modelo": VERSION_MODELO,
            "etapa_ciclo_vida": "Operación y monitoreo",
            "huella_analisis": huella({k: analisis[k] for k in ("consumo_total_kwh", "pico_kw", "horas_pico")}),
            "mape_pct": mape,
            "alerta_deriva": mape is not None and mape > MAPE_REENTRENAR,
            "cobertura_bandas_pct": cob_pct,
            "cobertura_nominal_pct": cob.get("nominal_pct", COBERTURA_NOMINAL),
            "lecturas_validacion_bandas": cob.get("lecturas", 0),
            "alerta_calibracion_bandas": alerta_cal,
            "accion_monitoreo": "; ".join(acciones) or "Sin acción: desempeño dentro de tolerancia",
        }
        self.pizarra.escribir("F", "evaluacion", {"escenarios": evaluados, "trazabilidad": trazabilidad})
        ok = sum(e["aceptable"] for e in evaluados)
        self.log(f"{len(evaluados)} escenarios evaluados, {ok} dentro de tolerancia "
                 f"(riesgo ≤ {TOLERANCIA_RIESGO}); cobertura de bandas "
                 f"{'sin datos' if cob_pct is None else f'{cob_pct} %'}")
        self.enviar("C", P.INFORM, {"clave": "evaluacion", "aceptables": ok}, msg.conversacion)


# ============================================================ C - Meta
class AgenteMeta(Agente):
    """Supervisor: mantiene los objetivos, enruta y aplica las reglas de decisión.

    Regla 1 Prioridad de suministro y contingencias (sin déficit; si no hay
            alternativa, recorte parcial controlado con supervisión humana).
            Entre las viables se prefieren las robustas (sin déficit en P90).
    Regla 2 Máximo aprovechamiento de renovables
    Regla 3 Menor costo operativo (solo con suministro garantizado)
    Regla 4 Validación humana de toda recomendación (trazable, ISO 23894/42005)
    """
    OBJETIVOS = ["Suministro continuo sin déficit", f"Margen de capacidad ≥ {int(MARGEN_OBJETIVO * 100)} %",
                 "Maximizar renovables", "Minimizar costo operativo"]

    def __init__(self, *a, condicion: str, parametros: dict | None = None, max_rechazos: int = 3, **k):
        super().__init__(*a, **k)
        self.condicion = condicion
        self.parametros = parametros or {}      # p. ej. {"alimentador": "A2"} o {"crecimiento_pct": 20}
        self.max_rechazos = max_rechazos
        self.rechazos = 0
        self.excluidos: list[str] = []
        self.ultimo_motivo = ""                 # motivo del último rechazo del operador
        self.reintentos: dict[str, int] = {}
        self.ultima_solicitud: dict[str, dict] = {}

    def _solicitar(self, nodo: str, contenido: dict, conv: str):
        self.ultima_solicitud[nodo] = contenido
        self.enviar(nodo, P.REQUEST, contenido, conv)

    def _deliberar(self, conv: str):
        self._solicitar("E", {"condicion": self.condicion, "parametros": self.parametros,
                              "excluir": self.excluidos, "ronda": self.rechazos}, conv)

    def procesar(self, msg: Mensaje):
        conv = msg.conversacion
        if msg.performativa == P.INFORM and msg.emisor == "B":
            self.log("objetivos: " + "; ".join(self.OBJETIVOS))
            self._solicitar("D", {"clave": "datos"}, conv)
        elif msg.performativa == P.INFORM and msg.emisor == "D":
            self._deliberar(conv)
        elif msg.performativa == P.INFORM and msg.emisor == "F":
            self._decidir(conv)
        elif msg.performativa == P.REJECT:
            self.rechazos += 1
            self.excluidos.append(msg.contenido["escenario"])
            self.ultimo_motivo = msg.contenido.get("motivo") or ""
            self.log(f"rechazo #{self.rechazos} ({msg.contenido.get('motivo') or 'sin motivo'}); "
                     "se replantean medidas alternas")
            if self.rechazos >= self.max_rechazos:
                self._escalar(conv, f"Se alcanzó el máximo de {self.max_rechazos} rechazos")
            else:
                self._deliberar(conv)
        elif msg.performativa == P.FAILURE:
            self._manejar_falla(msg)

    def _manejar_falla(self, msg: Mensaje):
        nodo = msg.emisor
        destino = "E" if nodo == "F" else nodo     # si falla F, se repite la deliberación
        self.reintentos[nodo] = self.reintentos.get(nodo, 0) + 1
        self.log(f"FALLA en {nodo}: {msg.contenido['error']}")
        if self.reintentos[nodo] <= 1:
            self.log(f"reintento de la tarea en {destino}")
            self.enviar(destino, P.REQUEST, self.ultima_solicitud[destino], msg.conversacion)
        else:
            self._escalar(msg.conversacion, f"Falla persistente en {nodo}")

    def _decidir(self, conv: str):
        escenarios = {e.id: e for e in self.pizarra.leer("escenarios")["lista"]}
        evaluacion = {e["id"]: e for e in self.pizarra.leer("evaluacion")["escenarios"]}
        candidatos = [i for i in escenarios if i not in self.excluidos]
        if not candidatos:
            return self._escalar(conv, "No quedan escenarios alternativos")

        # Regla 1: solo se proponen escenarios sin déficit en P50 (después de sus medidas).
        # Si ninguno lo logra, no se propone un plan inseguro: se escala al operador.
        viables = [i for i in candidatos if escenarios[i].metricas["deficit_kwh"] == 0]
        if not viables:
            return self._escalar(conv, MOTIVO_SIN_MEDIDAS)
        modo = "contingencia" if any(escenarios[i].estrategia == "contingencia" for i in viables) else "normal"
        aceptables = [i for i in viables if evaluacion[i]["aceptable"]] or viables
        hay_robusta = any(escenarios[i].robusto for i in aceptables)
        rech = self.pizarra.leer("decision") if self.excluidos else None
        if modo == "contingencia":
            # Política de recorte (a): por omisión el recorte mínimo (*-CONT, P50); el plan
            # robusto (*-CONT-P90) se ofrece como alternativa explícita.
            if rech and rech.get("escenario") and _pide_mas_seguridad(self.ultimo_motivo):
                aceptables = [i for i in aceptables if escenarios[i].robusto]
            elif rech and rech.get("escenario"):
                # Rechazo sin pedir más seguridad: solo medidas que recorten menos que la rechazada
                limite = rech.get("recorte_kwh")
                aceptables = [i for i in aceptables
                              if limite is not None and escenarios[i].metricas["recorte_kwh"] < limite]
            if not aceptables:
                return self._escalar(conv, MOTIVO_SIN_MEDIDAS)
        else:
            if rech and rech.get("escenario"):
                # Tras un rechazo se buscan medidas alternas menos invasivas que la rechazada
                limite = rech.get("desplazado_kwh")
                menos = [i for i in aceptables
                         if limite is not None and escenarios[i].metricas["desplazado_kwh"] < limite]
                aceptables = menos or aceptables
            if not any(evaluacion[i]["aceptable"] for i in viables):
                modo = "riesgo fuera de tolerancia"
            # Robustez: se prefieren las que tampoco tienen déficit en la banda desfavorable P90
            aceptables = [i for i in aceptables if escenarios[i].robusto] or aceptables

        # Reglas 2 y 3 (orden lexicográfico) + menor riesgo e impacto al usuario.
        # En contingencia manda el menor recorte (política de recorte mínimo).
        def clave(i):
            m = escenarios[i].metricas
            orden = (m["deficit_kwh"], -round(m["renovable_pct"], 1), round(m["costo_mxn"], -1),
                     evaluacion[i]["riesgo_max"], m["desplazado_kwh"], m["recorte_kwh"])
            return ((m["recorte_kwh"],) + orden) if modo == "contingencia" else orden
        elegido = min(aceptables, key=clave)
        esc = escenarios[elegido]
        base = escenarios.get("S0") or esc

        if esc.estrategia == "contingencia":
            modo = "contingencia"
        m = esc.metricas
        # Invariante (Regla 1): nada con déficit llega a la aprobación humana
        if m["deficit_kwh"] != 0:
            raise RuntimeError(f"Invariante violada: {elegido} tiene {m['deficit_kwh']} kWh de déficit en P50")
        r1 = _texto_regla1(esc, base)
        if m["recorte_kwh"] > 0:
            r3 = "Regla 3: no aplica, el suministro no está garantizado (prevalece la estabilidad de la red)."
        else:
            r3 = (f"Regla 3: costo {m['costo_mxn']:,.0f} MXN "
                  f"({_ahorro(base.metricas['costo_mxn'] - m['costo_mxn'])} frente a la operación actual).")
        alims = "; ".join(f"{k} margen {a['margen_min_pct']} %, uso máx. {a['utilizacion_max_pct']} %"
                          + (f", recorte {a['recorte_kwh']:,.0f} kWh" if a["recorte_kwh"] else "")
                          for k, a in m["alimentadores"].items())
        extra = m.get("recorte_adicional_robustez_kwh")
        if esc.robusto:
            robustez = ("Robustez: sin déficit en el escenario desfavorable P90 (demanda P90, renovables P10); "
                        f"margen mínimo P90 {m['margen_red_min_p90_pct']} %.")
            if m.get("plan_minimo") and extra is not None:
                robustez += (f" Recorta {extra:,.0f} kWh más que el plan mínimo {m['plan_minimo']} para evitar "
                             f"{m['deficit_p90_evitado_kwh']:,.0f} kWh de déficit si la demanda llega a P90.")
        elif m.get("alternativa_robusta") and extra is not None:
            robustez = (f"Robustez: este es el recorte mínimo; si la demanda llega a P90 (con renovables en P10) "
                        f"quedarían {m['deficit_p90_kwh']:,.0f} kWh de déficit. Alternativa robusta "
                        f"{m['alternativa_robusta']}: recorta {extra:,.0f} kWh adicionales y evita ese riesgo. "
                        "Para recibirla, rechaza esta propuesta pidiendo más seguridad.")
        else:
            robustez = (f"Robustez: ninguna estrategia viable es robusta; en el escenario desfavorable P90 quedarían "
                        f"{m['deficit_p90_kwh']:,.0f} kWh de déficit. Se trata como riesgo (ISO/IEC 23894) y "
                        "debe vigilarse la demanda en tiempo real.")
        justificacion = [
            r1,
            f"Regla 2: renovables cubren {m['renovable_pct']} % de la demanda "
            f"({m['renovable_desperdiciada_kwh']:,.0f} kWh renovables sin aprovechar).",
            r3,
            f"Regla 4: riesgo máximo {evaluacion[elegido]['riesgo_max']} "
            f"({evaluacion[elegido]['categoria_max']}); queda sujeto a aprobación humana"
            + (" y requiere autorización expresa del operador." if evaluacion[elegido]["requiere_escalamiento"] else "."),
            robustez,
            f"Distribución por alimentador: {alims}; pérdidas técnicas {m['perdidas_kwh']:,.0f} kWh.",
        ]
        if m["margen_red_min_pct"] < MARGEN_OBJETIVO * 100 and m["deficit_kwh"] == 0:
            justificacion.append("Aviso: el margen queda por debajo del objetivo; vigilar la operación.")
        decision = {"escenario": elegido, "modo": modo, "justificacion": justificacion,
                    "desplazado_kwh": m["desplazado_kwh"], "recorte_kwh": m["recorte_kwh"],
                    "robusto": esc.robusto, "hay_alternativa_robusta": hay_robusta,
                    "recorte_adicional_robustez_kwh": extra,
                    "alternativa_robusta": m.get("alternativa_robusta"),
                    "rechazos_previos": self.rechazos, "alternativas_evaluadas": len(candidatos)}
        self.pizarra.escribir("C", "decision", decision)
        self.log(f"selecciona {elegido} ({modo}{', robusta' if esc.robusto else ', no robusta'}) "
                 f"entre {len(candidatos)} alternativas")
        self.enviar("G", P.PROPOSE, {"clave": "decision", "escenario": elegido}, conv)

    def _escalar(self, conv: str, motivo: str):
        self.pizarra.escribir("C", "decision", {"escenario": None, "modo": "escalamiento",
                                                "justificacion": [motivo + ": se requiere decisión del operador."],
                                                "rechazos_previos": self.rechazos})
        self.log(f"ESCALAMIENTO al operador: {motivo}")
        self.enviar("G", P.PROPOSE, {"clave": "decision", "escenario": None, "escalamiento": True}, conv)


# ============================================================ G - Humano
class AprobacionHumana(Agente):
    """Nodo G. Modos:
      interactivo -> pregunta por consola
      auto        -> aprueba (tras `rechazos_auto` rechazos) para demostraciones
      web         -> deja la propuesta en espera; el operador responde con `responder()`
    """
    def __init__(self, *a, modo: str = "interactivo", rechazos_auto: int = 0, **k):
        super().__init__(*a, **k)
        self.modo = modo
        self.rechazos_auto = rechazos_auto
        self.pendiente: dict | None = None      # propuesta que espera decisión (modo web)
        self.historial: list[dict] = []         # decisiones del operador

    def responder(self, aprobado: bool, motivo: str = "", operador: str = ""):
        """Registra la decisión del operador sobre la propuesta pendiente (modo web).
        `operador` es el nombre de quien firma (único dato personal que se guarda).
        Después hay que llamar a bus.ejecutar() para que el flujo continúe."""
        if self.pendiente is None:
            raise ValueError("No hay ninguna propuesta en espera de aprobación")
        p, self.pendiente = self.pendiente, None
        self._registrar(p["escenario"], aprobado, motivo.strip(), p["conversacion"], operador.strip())

    def _registrar(self, escenario: str, aprobado: bool, motivo: str, conv: str, operador: str = ""):
        registro = {"aprobado": aprobado, "escenario": escenario, "motivo": motivo,
                    "operador": operador or None,
                    "fecha": datetime.now().isoformat(timespec="seconds")}
        self.historial.append(registro)
        self.pizarra.escribir("G", "aprobacion", registro)
        # ISO/IEC 5338: la decisión humana queda en la bitácora con quien la firma
        self.pizarra.bitacora.registrar("decision_humana", conversacion=conv, **registro)
        self.log(f"{'APROBADA' if aprobado else 'RECHAZADA'} la propuesta {escenario}"
                 + (f" por {operador}" if operador else "")
                 + (f" (motivo: {motivo})" if motivo else ""))
        if aprobado:
            self.enviar("H", P.INFORM, {"clave": "decision", "aprobado": True, "operador": operador}, conv)
        else:
            self.enviar("C", P.REJECT, {"escenario": escenario, "motivo": motivo, "operador": operador}, conv)

    def _mostrar(self, texto: str):
        """Imprime la propuesta en consola. En modo interactivo siempre (la persona debe
        verla para decidir); en modo automático solo si el bus es verbose."""
        if self.modo == "interactivo" or self.bus.verbose:
            print(texto)

    def procesar(self, msg: Mensaje):
        decision = self.pizarra.leer("decision")
        if decision["escenario"] is None:          # escalamiento: no hay propuesta que aprobar
            self.pizarra.escribir("G", "aprobacion", {"aprobado": False, "escalado": True})
            self.log("caso escalado; se entrega el expediente al operador")
            return self.enviar("H", P.INFORM, {"clave": "decision", "aprobado": False}, msg.conversacion)

        esc = next(e for e in self.pizarra.leer("escenarios")["lista"] if e.id == decision["escenario"])
        ev = next(e for e in self.pizarra.leer("evaluacion")["escenarios"] if e["id"] == esc.id)
        m = esc.metricas

        if self.modo == "web":
            self.pendiente = {"escenario": esc.id, "conversacion": msg.conversacion}
            self.log(f"propuesta {esc.id} en espera de la decisión del operador")
            return

        self._mostrar("\n" + "-" * 70)
        self._mostrar(f"  PROPUESTA PARA APROBACIÓN  [{esc.id}] {esc.nombre}")
        self._mostrar(f"  Modo: {decision['modo']} | Riesgo máximo: {ev['riesgo_max']} ({ev['categoria_max']})")
        self._mostrar(f"  Costo: {m['costo_mxn']:,.0f} MXN | Renovables: {m['renovable_pct']} % | "
                      f"Desplazado: {m['desplazado_kwh']:,.0f} kWh | Recorte parcial: {m['recorte_kwh']:,.0f} kWh | "
                      f"{'Robusta' if esc.robusto else 'No robusta'} ante P90")
        for j in decision["justificacion"]:
            self._mostrar(f"   - {j}")
        self._mostrar("-" * 70)

        if self.modo == "interactivo":
            resp = input("  ¿Aprueba la recomendación? (s/n): ").strip().lower()
            aprobado = resp.startswith("s")
            motivo = "" if aprobado else input("  Motivo del rechazo: ").strip()
        else:
            aprobado = self.rechazos_auto <= 0
            motivo = "" if aprobado else "Rechazo de prueba: se solicita una alternativa menos invasiva"
            self.rechazos_auto -= 1
            self._mostrar(f"  (modo automático) {'APROBADO' if aprobado else 'RECHAZADO'}")

        self._registrar(esc.id, aprobado, motivo, msg.conversacion)


# ============================================================ H - Salida
class RecomendacionFinal(Agente):
    def __init__(self, *a, red: RedDistribucion, periodo: str, salida: str, **k):
        super().__init__(*a, **k)
        self.red = red
        self.periodo = periodo
        self.salida = salida
        self.reporte: dict | None = None

    @staticmethod
    def recorte_por_carga(esc: Escenario) -> dict[str, tuple[float, float, list[int]]]:
        """carga -> (kWh recortados, kW máximos, horas), de mayor a menor energía."""
        out = {}
        for h, c, kw in esc.suspensiones:
            kwh, kw_max, hs = out.get(c, (0.0, 0.0, []))
            out[c] = (kwh + kw, max(kw_max, kw), hs + [h])
        return dict(sorted(out.items(), key=lambda x: -x[1][0]))

    def _acciones(self, esc: Escenario) -> list[str]:
        acciones = []
        for origen, destino, kwh in esc.movimientos:
            acciones.append(f"Mover {kwh:,.0f} kWh de consumo flexible de las {_horas(origen)} "
                            f"a las {_horas(destino)}")
        for carga, kwh in sorted(esc.desplazamientos.items(), key=lambda x: -x[1]):
            if kwh >= 1:
                acciones.append(f"  · {carga}: {kwh:,.0f} kWh")
        for c, (kwh, kw_max, hs) in self.recorte_por_carga(esc).items():
            acciones.append(f"Recorte parcial de '{c}': {kwh:,.0f} kWh (máx. {kw_max:,.0f} kW) en las "
                            f"{_horas(hs)} (cargas críticas protegidas)")
        if not acciones:
            acciones.append("Mantener la operación actual: no hay desplazamientos que reduzcan costo o riesgo")
        return acciones

    def procesar(self, msg: Mensaje):
        decision = self.pizarra.leer("decision")
        aprobacion = self.pizarra.leer("aprobacion")
        analisis = self.pizarra.leer("analisis") or {}
        evaluacion = self.pizarra.leer("evaluacion") or {"escenarios": [], "trazabilidad": None}
        paquete = self.pizarra.leer("escenarios") or {"condicion": None}
        esc = None
        if decision["escenario"]:
            esc = next(e for e in self.pizarra.leer("escenarios")["lista"] if e.id == decision["escenario"])

        reporte = {
            "sistema": VERSION_MODELO,
            "fecha": datetime.now().isoformat(timespec="seconds"),
            "periodo_analizado": NOMBRE_PERIODO[self.periodo],
            "condicion_red": paquete["condicion"],
            "parametros_condicion": paquete.get("parametros"),
            "estado": ("APROBADA" if aprobacion["aprobado"] else
                       "ESCALADA AL OPERADOR" if aprobacion.get("escalado") else "RECHAZADA"),
            "motivo_escalamiento": None if esc else decision["justificacion"],
            "analisis": {k: analisis.get(k) for k in ("consumo_total_kwh", "pico_kw", "fecha_pico", "horas_pico",
                                                      "variacion_estacional_kw", "deteccion", "mape_pct",
                                                      "experiencia_ejecuciones")},
            "anomalias": analisis.get("anomalias", [])[:20],
            "recomendacion": None if esc is None else {
                "escenario": esc.id, "nombre": esc.nombre, "modo": decision["modo"],
                "acciones": self._acciones(esc), "metricas": esc.metricas,
                "robusto": esc.robusto, "metricas_p90": esc.metricas_p90,
                "recorte_por_carga": {c: {"kwh": round(kwh, 1), "kw_max": kw, "horas": hs}
                                      for c, (kwh, kw, hs) in self.recorte_por_carga(esc).items()},
                "justificacion": decision["justificacion"],
                "evaluacion_iso": next(e for e in evaluacion["escenarios"] if e["id"] == esc.id),
            },
            "trazabilidad_iso_5338": evaluacion["trazabilidad"],
            "aprobacion": aprobacion,
        }
        self.pizarra.escribir("H", "reporte", reporte)
        self.reporte = reporte
        os.makedirs(self.salida, exist_ok=True)
        with open(os.path.join(self.salida, "reporte_electriagent.json"), "w", encoding="utf-8") as f:
            json.dump(reporte, f, ensure_ascii=False, indent=2, default=str)
        self.log(f"recomendación {reporte['estado']} entregada al usuario")


_PALABRAS_SEGURIDAD = ("segur", "robust", "p90", "riesgo", "estabil", "conservador", "proteg", "margen")


def _pide_mas_seguridad(motivo: str) -> bool:
    """¿El operador rechazó pidiendo más seguridad (plan robusto) en lugar de menos recorte?"""
    texto = motivo.lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u")):
        texto = texto.replace(a, b)
    return any(palabra in texto for palabra in _PALABRAS_SEGURIDAD)


def _texto_regla1(esc: "Escenario", base: "Escenario") -> str:
    """Texto de la Regla 1 a partir del déficit y los márgenes reales del escenario.
    Nunca afirma "sin déficit" si hay energía sin servir o un margen negativo."""
    m = esc.metricas
    margen_se, margen_red = m["margen_min_pct"], m["margen_red_min_pct"]
    if m["deficit_kwh"] > 0:
        return (f"Regla 1: el suministro NO está garantizado: quedarían {m['deficit_kwh']:,.0f} kWh sin servir "
                f"(margen mínimo {margen_se} % en la subestación, {margen_red} % en la red).")
    if m["recorte_kwh"] > 0:
        cargas = ", ".join(f"{n} {kwh:,.0f} kWh" for n, kwh in
                           sorted(m["recorte_por_carga_kwh"].items(), key=lambda x: -x[1]))
        texto = (f"Regla 1: la demanda supera la capacidad disponible ({m['deficit_planificado_kwh']:,.0f} kWh de "
                 f"déficit en la banda {esc.base_planificacion}); se recomienda un recorte parcial de "
                 f"{m['recorte_kwh']:,.0f} kWh solo en cargas no críticas, por prioridad ({cargas}). "
                 "Las cargas críticas conservan el 100 % de su suministro.")
        if esc.base_planificacion == "P90":
            texto += (f" Con el pronóstico central bastarían {m['deficit_previo_kwh']:,.0f} kWh; el excedente "
                      "protege la estabilidad si la demanda llega a P90.")
        return texto
    if min(margen_se, margen_red) < 0:
        return (f"Regla 1: suministro al límite: margen mínimo {margen_se} % en la subestación y {margen_red} % "
                "en la red; vigilar la operación.")
    texto = (f"Regla 1: sin déficit de suministro (margen mínimo {margen_se} % en la subestación, "
             f"{margen_red} % en la red).")
    if base.metricas["deficit_kwh"] > 0 and base is not esc:
        texto += f" Sin cambios habría {base.metricas['deficit_kwh']:,.0f} kWh de déficit."
    return texto


def _ahorro(valor: float) -> str:
    if abs(valor) < 1:
        return "mismo costo"
    return f"ahorro de {valor:,.0f} MXN" if valor > 0 else f"sobrecosto de {-valor:,.0f} MXN"


def _horas(hs: list[int]) -> str:
    """[18,19,20,21] -> '18:00-22:00'; agrupa rangos consecutivos."""
    hs = sorted(set(hs))
    rangos, ini = [], hs[0]
    for a, b in zip(hs, hs[1:] + [None]):
        if b != a + 1:
            rangos.append(f"{ini:02d}:00-{a + 1:02d}:00")
            if b is not None:
                ini = b
    return ", ".join(rangos)
