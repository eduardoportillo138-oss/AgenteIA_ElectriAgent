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

from coordinacion import Agente, Mensaje, Performativa as P, huella
from modelo_red import (COSTO_RENOVABLE, NOMBRE_PERIODO, PERIODOS, RedDistribucion,
                        SimuladorIoT)

VERSION_MODELO = "ElectriAgent-proto 0.1"
TOLERANCIA_RIESGO = 12        # nivel máximo aceptable (probabilidad x impacto)
MARGEN_OBJETIVO = 0.10        # margen mínimo de capacidad deseado
MAPE_REENTRENAR = 15.0        # % de error que dispara alerta de deriva (ISO/IEC 5338)

CONDICIONES = {   # condición de la red prevista para el día a recomendar
    "normal": {"demanda": {}, "capacidad": 1.0,
               "desc": "Operación normal"},
    "ola_calor": {"demanda": {"residencial": 1.80, "comercial": 1.60, "critica": 1.10},
                  "capacidad": 1.0, "desc": "Ola de calor (pico de demanda)"},
    "falla": {"demanda": {}, "capacidad": 0.55,
              "desc": "Falla parcial del transformador de la subestación"},
}


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
            "mape_pct": mape,
            "experiencia_ejecuciones": memoria["ejecuciones"],
        }
        self.pizarra.escribir("D", "analisis", analisis)
        self.log(f"consumo {analisis['consumo_total_kwh']:,.0f} kWh, horas pico {horas_pico}, "
                 f"{len(anomalias)} anomalías, error de pronóstico (MAPE) {mape}%")
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
class Escenario:
    id: str
    nombre: str
    estrategia: str
    intensidad: float
    demanda: np.ndarray
    renovable: np.ndarray
    capacidad_kw: float
    tarifas: np.ndarray
    desplazamientos: dict = field(default_factory=dict)   # carga -> kWh movidos
    movimientos: list = field(default_factory=list)       # (horas origen, horas destino, kWh)
    suspensiones: list = field(default_factory=list)      # (hora, carga, kW)
    metricas: dict = field(default_factory=dict)

    def calcular(self, demanda_original_kwh: float):
        D, R, cap = self.demanda, self.renovable, self.capacidad_kw
        r_usada = np.minimum(R, D)
        conv = D - r_usada
        deficit = np.maximum(0, conv - cap)
        conv_servida = np.minimum(conv, cap)
        costo = float((conv_servida * self.tarifas).sum() + r_usada.sum() * COSTO_RENOVABLE)
        desplazado = float(sum(self.desplazamientos.values()))
        self.metricas = {
            "demanda_kwh": round(float(D.sum()), 1),
            "renovable_usada_kwh": round(float(r_usada.sum()), 1),
            "renovable_desperdiciada_kwh": round(float((R - r_usada).sum()), 1),
            "renovable_pct": round(float(r_usada.sum() / D.sum() * 100), 2),
            "convencional_kwh": round(float(conv_servida.sum()), 1),
            "pico_convencional_kw": round(float(conv.max()), 1),
            "margen_min_pct": round(float((cap - conv.max()) / cap * 100), 2),
            "deficit_kwh": round(float(deficit.sum()), 1),
            "horas_deficit": [int(h) for h in np.where(deficit > 0)[0]],
            "costo_mxn": round(costo, 2),
            "desplazado_kwh": round(desplazado, 1),
            "desplazado_pct": round(desplazado / demanda_original_kwh * 100, 2),
            "suspendido_kwh": round(sum(s[2] for s in self.suspensiones), 1),
        }


class AgenteDeliberativo(Agente):
    def __init__(self, *a, red: RedDistribucion, fallar: bool = False, **k):
        super().__init__(*a, **k)
        self.red = red
        self.fallar = fallar

    # -- Movimiento de carga flexible (energía constante, respeta la capacidad)
    def _mover(self, esc, cargas, flex_rest, donantes, receptores, pesos, intensidad):
        cap_util = 0.90 * esc.capacidad_kw          # deja 10 % de margen en receptores
        holgura = np.array([max(0.0, cap_util + esc.renovable[h] - esc.demanda[h]) for h in receptores])
        if holgura.sum() <= 0:
            return
        oferta = {h: intensidad * flex_rest[h] for h in donantes}
        total = sum(oferta.values())
        if total <= 0:
            return
        movible = min(total, holgura.sum())
        f = movible / total
        for h in donantes:
            quitar = oferta[h] * f
            for c in self.red.cargas:          # reparto por carga (para las acciones)
                if not c.critica and flex_rest[h] > 0:
                    parte = c.desplazable * cargas[c.nombre][h] / sum(
                        x.desplazable * cargas[x.nombre][h] for x in self.red.cargas if not x.critica)
                    esc.desplazamientos[c.nombre] = esc.desplazamientos.get(c.nombre, 0) + quitar * parte
            esc.demanda[h] -= quitar
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
        for h, v in zip(receptores, reparto):
            esc.demanda[h] += v
        esc.movimientos.append((list(donantes), list(receptores), round(float(movible), 1)))

    def _simular(self, id_, nombre, estrategia, intensidad, cargas, R, cap):
        D0 = sum(cargas.values())
        flex = sum(c.desplazable * cargas[c.nombre] for c in self.red.cargas if not c.critica)
        esc = Escenario(id_, nombre, estrategia, intensidad, D0.copy(), R, cap, self.red.vector_tarifas())
        flex_rest = flex.copy()
        if estrategia in ("renovable", "combinada"):
            neta = D0 - R
            donantes = sorted(np.argsort(neta)[-4:].tolist())
            receptores = [h for h in range(24)
                          if R.max() > 0 and R[h] > 0.3 * R.max() and h not in donantes]
            if receptores:
                self._mover(esc, cargas, flex_rest, donantes, receptores,
                            [R[h] for h in receptores], intensidad)
        if estrategia in ("tarifa", "combinada"):
            donantes = [h for h in range(24) if self.red.periodo_tarifario(h) == "punta"]
            receptores = [h for h in range(24) if self.red.periodo_tarifario(h) == "base"]
            self._mover(esc, cargas, flex_rest, donantes, receptores, [1] * len(receptores), intensidad)
        esc.calcular(D0.sum())
        return esc

    def _contingencia(self, base: Escenario, cargas):
        """Regla 1: suspensiones controladas de cargas no críticas por prioridad."""
        esc = Escenario(f"{base.id}-CONT", f"Contingencia: suspensión controlada sobre {base.id}",
                        "contingencia", base.intensidad, base.demanda.copy(), base.renovable,
                        base.capacidad_kw, base.tarifas, dict(base.desplazamientos), list(base.movimientos))
        D0 = sum(cargas.values())
        orden = sorted([c for c in self.red.cargas if not c.critica], key=lambda c: c.prioridad_corte)
        for h in range(24):
            falta = esc.demanda[h] - esc.renovable[h] - esc.capacidad_kw
            for c in orden:
                if falta <= 0:
                    break
                kw = cargas[c.nombre][h] * esc.demanda[h] / D0[h]
                esc.suspensiones.append((h, c.nombre, round(float(kw), 1)))
                esc.demanda[h] -= kw
                falta -= kw
        esc.calcular(D0.sum())
        return esc

    def procesar(self, msg: Mensaje):
        if self.fallar:
            self.fallar = False
            raise RuntimeError("el simulador de escenarios no respondió (falla simulada)")

        analisis = self.pizarra.leer("analisis")
        cond = CONDICIONES[msg.contenido["condicion"]]
        excluir = set(msg.contenido.get("excluir", []))
        ronda = msg.contenido.get("ronda", 0)

        cargas = {}
        for c in self.red.cargas:
            cargas[c.nombre] = analisis["pronostico_cargas"][c.nombre] * cond["demanda"].get(c.tipo, 1.0)
        R = sum(analisis["pronostico_renovables"].values())
        cap = self.red.capacidad_convencional_kw * cond["capacidad"]

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
                escenarios.append(self._simular(sid, nom, estr, intensidad, cargas, R, cap))

        if all(e.metricas["deficit_kwh"] > 0 for e in escenarios):
            mejor = min(escenarios, key=lambda e: e.metricas["deficit_kwh"])
            escenarios.append(self._contingencia(mejor, cargas))
        escenarios = [e for e in escenarios if e.id not in excluir]

        self.pizarra.escribir("E", "escenarios", {"lista": escenarios, "condicion": cond["desc"],
                                                  "capacidad_kw": cap})
        self.log(f"{len(escenarios)} escenarios simulados ({cond['desc']}, capacidad {cap:,.0f} kW)")
        self.enviar("F", P.PROPOSE, {"clave": "escenarios", "n": len(escenarios)}, msg.conversacion)


# ============================================================ F - Evaluación ISO
def _nivel(valor: int) -> str:
    return "bajo" if valor <= 6 else "medio" if valor <= 12 else "alto" if valor <= 19 else "crítico"


class EvaluadorISO(Agente):
    def __init__(self, *a, fallar: bool = False, **k):
        super().__init__(*a, **k)
        self.fallar = fallar

    def _riesgos(self, m: dict, mape: float | None) -> list[dict]:
        """ISO/IEC 23894: identificar, analizar (probabilidad x impacto) y tratar."""
        margen = m["margen_min_pct"]
        p_cap = 1 if margen >= 20 else 2 if margen >= 10 else 3 if margen >= 5 else 4 if margen >= 0 else 5
        e = mape if mape is not None else 20
        p_pron = 1 if e < 5 else 2 if e < 10 else 3 if e < 15 else 4 if e < 25 else 5
        d = m["desplazado_pct"]
        riesgos = [
            {"riesgo": "Insuficiencia de capacidad", "prob": p_cap, "impacto": 5,
             "tratamiento": "Reservar margen y vigilar la carga del transformador"},
            {"riesgo": "Error de pronóstico de demanda/renovables", "prob": p_pron,
             "impacto": 4 if margen < 10 else 3,
             "tratamiento": "Revisar el pronóstico con datos en tiempo real"},
            {"riesgo": "Afectación al usuario por desplazamiento de cargas",
             "prob": 3 if d > 0 else 1,
             "impacto": 1 if d < 2 else 2 if d < 5 else 3 if d < 10 else 4,
             "tratamiento": "Desplazar solo cargas no críticas y notificar con anticipación"},
        ]
        if m["suspendido_kwh"] > 0:
            riesgos.append({"riesgo": "Suspensión controlada del servicio", "prob": 5, "impacto": 4,
                            "tratamiento": "Cargas críticas protegidas; requiere autorización del operador"})
        for r in riesgos:
            r["nivel"] = r["prob"] * r["impacto"]
            r["categoria"] = _nivel(r["nivel"])
        return riesgos

    @staticmethod
    def _impactos(m: dict, base: dict) -> list[dict]:
        """ISO/IEC 42005: efectos sobre los grupos de interés."""
        ahorro = base["costo_mxn"] - m["costo_mxn"]

        def efecto(cond_pos, cond_neg):
            return "positivo" if cond_pos else "negativo" if cond_neg else "neutral"
        return [
            {"grupo": "Usuarios residenciales y comerciales",
             "efecto": efecto(False, m["desplazado_kwh"] > 0 or m["suspendido_kwh"] > 0),
             "detalle": f"{m['desplazado_kwh']:,.0f} kWh de consumo flexible cambian de horario"
                        + (f"; {m['suspendido_kwh']:,.0f} kWh suspendidos" if m["suspendido_kwh"] else "")},
            {"grupo": "Cargas críticas (hospital)", "efecto": "positivo" if m["suspendido_kwh"] else "neutral",
             "detalle": "Suministro garantizado (nunca se desplaza ni se suspende)"},
            {"grupo": "Operador de la red", "efecto": efecto(ahorro > 0, ahorro < 0),
             "detalle": f"Costo {m['costo_mxn']:,.0f} MXN ({_ahorro(ahorro)} frente a la operación actual)"},
            {"grupo": "Medio ambiente", "efecto": efecto(m["renovable_desperdiciada_kwh"] < base["renovable_desperdiciada_kwh"], False),
             "detalle": f"{m['renovable_pct']} % de la demanda cubierta con renovables"},
        ]

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
            riesgos = self._riesgos(esc.metricas, mape)
            riesgo_max = max(r["nivel"] for r in riesgos)
            evaluados.append({
                "id": esc.id,
                "riesgos": riesgos,
                "riesgo_max": riesgo_max,
                "categoria_max": _nivel(riesgo_max),
                "impactos": self._impactos(esc.metricas, base or esc.metricas),
                "aceptable": riesgo_max <= TOLERANCIA_RIESGO and esc.metricas["suspendido_kwh"] == 0,
                "requiere_escalamiento": esc.metricas["suspendido_kwh"] > 0,
            })
        trazabilidad = {   # ISO/IEC 5338: ciclo de vida, monitoreo y registro
            "version_modelo": VERSION_MODELO,
            "etapa_ciclo_vida": "Operación y monitoreo",
            "huella_analisis": huella({k: analisis[k] for k in ("consumo_total_kwh", "pico_kw", "horas_pico")}),
            "mape_pct": mape,
            "alerta_deriva": mape is not None and mape > MAPE_REENTRENAR,
            "accion_monitoreo": ("Re-entrenar / revisar el modelo de aprendizaje" if mape and mape > MAPE_REENTRENAR
                                 else "Sin acción: desempeño dentro de tolerancia"),
        }
        self.pizarra.escribir("F", "evaluacion", {"escenarios": evaluados, "trazabilidad": trazabilidad})
        ok = sum(e["aceptable"] for e in evaluados)
        self.log(f"{len(evaluados)} escenarios evaluados, {ok} dentro de tolerancia "
                 f"(riesgo ≤ {TOLERANCIA_RIESGO})")
        self.enviar("C", P.INFORM, {"clave": "evaluacion", "aceptables": ok}, msg.conversacion)


# ============================================================ C - Meta
class AgenteMeta(Agente):
    """Supervisor: mantiene los objetivos, enruta y aplica las reglas de decisión.

    Regla 1 Prioridad de suministro y contingencias (sin déficit; si no hay
            alternativa, suspensiones controladas con supervisión humana)
    Regla 2 Máximo aprovechamiento de renovables
    Regla 3 Menor costo operativo (solo con suministro garantizado)
    Regla 4 Validación humana de toda recomendación (trazable, ISO 23894/42005)
    """
    OBJETIVOS = ["Suministro continuo sin déficit", f"Margen de capacidad ≥ {int(MARGEN_OBJETIVO * 100)} %",
                 "Maximizar renovables", "Minimizar costo operativo"]

    def __init__(self, *a, condicion: str, max_rechazos: int = 3, **k):
        super().__init__(*a, **k)
        self.condicion = condicion
        self.max_rechazos = max_rechazos
        self.rechazos = 0
        self.excluidos: list[str] = []
        self.reintentos: dict[str, int] = {}
        self.ultima_solicitud: dict[str, dict] = {}

    def _solicitar(self, nodo: str, contenido: dict, conv: str):
        self.ultima_solicitud[nodo] = contenido
        self.enviar(nodo, P.REQUEST, contenido, conv)

    def _deliberar(self, conv: str):
        self._solicitar("E", {"condicion": self.condicion, "excluir": self.excluidos,
                              "ronda": self.rechazos}, conv)

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

        # Regla 1: suministro garantizado
        viables = [i for i in candidatos if escenarios[i].metricas["deficit_kwh"] == 0]
        modo = "normal"
        if not viables:
            viables = [i for i in candidatos if escenarios[i].estrategia == "contingencia"] or candidatos
            modo = "contingencia"
        aceptables = [i for i in viables if evaluacion[i]["aceptable"]] or viables
        if self.excluidos and modo == "normal":
            # Tras un rechazo se buscan medidas alternas menos invasivas que la rechazada
            rech = self.pizarra.leer("decision")
            limite = rech.get("desplazado_kwh") if rech else None
            menos = [i for i in aceptables
                     if limite is not None and escenarios[i].metricas["desplazado_kwh"] < limite]
            aceptables = menos or aceptables
        if modo == "normal" and not any(evaluacion[i]["aceptable"] for i in viables):
            modo = "riesgo fuera de tolerancia"

        # Reglas 2 y 3 (orden lexicográfico) + menor riesgo e impacto al usuario
        def clave(i):
            m = escenarios[i].metricas
            return (m["deficit_kwh"], -round(m["renovable_pct"], 1), round(m["costo_mxn"], -1),
                    evaluacion[i]["riesgo_max"], m["desplazado_kwh"])
        elegido = min(aceptables, key=clave)
        esc = escenarios[elegido]
        base = escenarios.get("S0") or esc

        if esc.estrategia == "contingencia":
            modo = "contingencia"
        m = esc.metricas
        if m["suspendido_kwh"] > 0:
            r1 = (f"Regla 1: la demanda supera la capacidad disponible; se cubre con suspensiones controladas "
                  f"de cargas no críticas ({m['suspendido_kwh']:,.0f} kWh), protegiendo las cargas críticas.")
            r3 = "Regla 3: no aplica, el suministro no está garantizado (prevalece la estabilidad de la red)."
        else:
            r1 = f"Regla 1: sin déficit de suministro (margen mínimo {m['margen_min_pct']} %)."
            if base.metricas["deficit_kwh"] > 0 and base is not esc:
                r1 += f" Sin cambios habría {base.metricas['deficit_kwh']:,.0f} kWh de déficit."
            r3 = (f"Regla 3: costo {m['costo_mxn']:,.0f} MXN "
                  f"({_ahorro(base.metricas['costo_mxn'] - m['costo_mxn'])} frente a la operación actual).")
        justificacion = [
            r1,
            f"Regla 2: renovables cubren {m['renovable_pct']} % de la demanda "
            f"({m['renovable_desperdiciada_kwh']:,.0f} kWh renovables sin aprovechar).",
            r3,
            f"Regla 4: riesgo máximo {evaluacion[elegido]['riesgo_max']} "
            f"({evaluacion[elegido]['categoria_max']}); queda sujeto a aprobación humana"
            + (" y requiere autorización expresa del operador." if evaluacion[elegido]["requiere_escalamiento"] else "."),
        ]
        if esc.metricas["margen_min_pct"] < MARGEN_OBJETIVO * 100 and esc.metricas["deficit_kwh"] == 0:
            justificacion.append("Aviso: el margen queda por debajo del objetivo; vigilar la operación.")
        decision = {"escenario": elegido, "modo": modo, "justificacion": justificacion,
                    "desplazado_kwh": esc.metricas["desplazado_kwh"],
                    "rechazos_previos": self.rechazos, "alternativas_evaluadas": len(candidatos)}
        self.pizarra.escribir("C", "decision", decision)
        self.log(f"selecciona {elegido} ({modo}) entre {len(candidatos)} alternativas")
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

    def responder(self, aprobado: bool, motivo: str = ""):
        """Registra la decisión del operador sobre la propuesta pendiente (modo web).
        Después hay que llamar a bus.ejecutar() para que el flujo continúe."""
        if self.pendiente is None:
            raise ValueError("No hay ninguna propuesta en espera de aprobación")
        p, self.pendiente = self.pendiente, None
        self._registrar(p["escenario"], aprobado, motivo.strip(), p["conversacion"])

    def _registrar(self, escenario: str, aprobado: bool, motivo: str, conv: str):
        registro = {"aprobado": aprobado, "escenario": escenario, "motivo": motivo,
                    "fecha": datetime.now().isoformat(timespec="seconds")}
        self.historial.append(registro)
        self.pizarra.escribir("G", "aprobacion", registro)
        self.log(f"{'APROBADA' if aprobado else 'RECHAZADA'} la propuesta {escenario}"
                 + (f" (motivo: {motivo})" if motivo else ""))
        if aprobado:
            self.enviar("H", P.INFORM, {"clave": "decision", "aprobado": True}, conv)
        else:
            self.enviar("C", P.REJECT, {"escenario": escenario, "motivo": motivo}, conv)

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

        print("\n" + "-" * 70)
        print(f"  PROPUESTA PARA APROBACIÓN  [{esc.id}] {esc.nombre}")
        print(f"  Modo: {decision['modo']} | Riesgo máximo: {ev['riesgo_max']} ({ev['categoria_max']})")
        print(f"  Costo: {m['costo_mxn']:,.0f} MXN | Renovables: {m['renovable_pct']} % | "
              f"Desplazado: {m['desplazado_kwh']:,.0f} kWh | Suspendido: {m['suspendido_kwh']:,.0f} kWh")
        for j in decision["justificacion"]:
            print("   -", j)
        print("-" * 70)

        if self.modo == "interactivo":
            resp = input("  ¿Aprueba la recomendación? (s/n): ").strip().lower()
            aprobado = resp.startswith("s")
            motivo = "" if aprobado else input("  Motivo del rechazo: ").strip()
        else:
            aprobado = self.rechazos_auto <= 0
            motivo = "" if aprobado else "Rechazo de prueba: se solicita una alternativa menos invasiva"
            self.rechazos_auto -= 1
            print(f"  (modo automático) {'APROBADO' if aprobado else 'RECHAZADO'}")

        self._registrar(esc.id, aprobado, motivo, msg.conversacion)


# ============================================================ H - Salida
class RecomendacionFinal(Agente):
    def __init__(self, *a, red: RedDistribucion, periodo: str, salida: str, **k):
        super().__init__(*a, **k)
        self.red = red
        self.periodo = periodo
        self.salida = salida
        self.reporte: dict | None = None

    def _acciones(self, esc: Escenario) -> list[str]:
        acciones = []
        for origen, destino, kwh in esc.movimientos:
            acciones.append(f"Mover {kwh:,.0f} kWh de consumo flexible de las {_horas(origen)} "
                            f"a las {_horas(destino)}")
        for carga, kwh in sorted(esc.desplazamientos.items(), key=lambda x: -x[1]):
            if kwh >= 1:
                acciones.append(f"  · {carga}: {kwh:,.0f} kWh")
        if esc.suspensiones:
            por_carga = {}
            for h, c, kw in esc.suspensiones:
                por_carga.setdefault(c, []).append(h)
            for c, hs in por_carga.items():
                acciones.append(f"Suspensión controlada de '{c}' en las {_horas(hs)} (cargas críticas protegidas)")
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
