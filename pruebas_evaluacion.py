"""
Pruebas unitarias de los indicadores de evaluación (evaluacion.py).

Cada indicador se prueba con un caso CALCULADO A MANO (el cálculo va en el comentario) y,
al final, con una corrida real del motor para comprobar la consistencia de los números:
  - ENS = recorte + no servido de la Distribucion (±0.1 kWh);
  - la suma de la ENS por carga coincide con la total;
  - el costo de la energía recalculado desde las series coincide con Σ Distribucion.costo.

    python pruebas_evaluacion.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace

import numpy as np

AQUI = os.path.dirname(os.path.abspath(__file__))
if AQUI not in sys.path:
    sys.path.insert(0, AQUI)

import evaluacion as ev  # noqa: E402
from distribucion import Distribucion, Limites, asignar, limites_red  # noqa: E402
from modelo_red import Alimentador, Carga, RedDistribucion  # noqa: E402
from motor import crear_sistema  # noqa: E402

H = 24
INF = float("inf")
TARIFAS = RedDistribucion(subestacion="x", tension_mt_kv=13.2, tension_bt="-",
                          capacidad_convencional_kw=1).vector_tarifas()
# Σ tarifas = 6 h base × 1.10 + 14 h intermedia × 1.80 + 4 h punta × 3.20 = 6.6 + 25.2 + 12.8 = 44.6


def red_mano(cap_se: float = 100.0) -> RedDistribucion:
    """Un alimentador de MT sin pérdidas: Hospital (crítica) y Casa (residencial)."""
    red = RedDistribucion(subestacion="SE prueba", tension_mt_kv=13.2, tension_bt="-",
                          capacidad_convencional_kw=cap_se, perdidas_mt_pct=0.0, perdidas_bt_pct=0.0)
    red.cargas = [Carga("Hospital", "critica", "MT", "Alim 1", 10, 0.0, 99),
                  Carga("Casa", "residencial", "MT", "Alim 1", 40, 0.1, 4)]
    red.alimentadores = [Alimentador("A1", "Alim 1", "MT", capacidad_kw=100)]
    return red


def serie(valor: float = 0.0, **horas) -> np.ndarray:
    """Serie de 24 h con `valor` y excepciones h<n>=valor."""
    s = np.full(H, float(valor))
    for k, v in horas.items():
        s[int(k[1:])] = v
    return s


def dist(demanda, rec_carga=None, ns_carga=None, ru=None, rdisp=None, rdesp=None, perd=None,
         imp=None, flujo=None, cap_alim=100.0, cap_se=100.0) -> Distribucion:
    """Distribucion de un alimentador (A1) construida a mano."""
    z = np.zeros(H)
    rec_carga, ns_carga = rec_carga or {}, ns_carga or {}
    rec = sum(rec_carga.values(), z.copy())
    ns = sum(ns_carga.values(), z.copy())
    ru = z.copy() if ru is None else ru
    conv = demanda - rec - ns - ru
    perd = z.copy() if perd is None else perd
    imp = conv + perd if imp is None else imp
    flujo = imp if flujo is None else flujo
    lim = Limites(cap_se, {"A1": cap_alim}, {"A1": INF}, 800.0, {"A1": 0.0}, {"A1": 0.0})
    return Distribucion(
        claves=["A1"], limites=lim, demanda={"A1": demanda},
        renovable_disponible={"A1": ru if rdisp is None else rdisp}, renovable_usada={"A1": ru},
        renovable_desperdiciada={"A1": z if rdesp is None else rdesp}, convencional={"A1": conv},
        perdidas={"A1": perd}, deficit={"A1": rec + ns}, recorte={"A1": rec}, no_servido={"A1": ns},
        flujo_cabecera={"A1": flujo}, flujo_trafo_bt={"A1": z}, margen_pct={"A1": z},
        costo={"A1": imp * TARIFAS + 0.30 * ru}, importacion_subestacion=imp, requerido_subestacion=imp,
        margen_subestacion_pct=z, recorte_por_carga=rec_carga, deficit_por_carga={},
        no_servido_por_carga=ns_carga, con_carga_critica={"A1"})


def caso_h() -> Distribucion:
    """Caso H (a mano): demanda 50 kW × 24 h = 1 200 kWh.
    Recorte de Casa 10 kW de 18 a 21 h (40 kWh); no servido de Casa 5 kW a las 23 h (5 kWh)
    y del Hospital 2 kW a las 12 h (2 kWh).  ENS = 40 + 5 + 2 = 47 kWh."""
    return dist(serie(50),
                rec_carga={"Casa": serie(0, h18=10, h19=10, h20=10, h21=10)},
                ns_carga={"Casa": serie(0, h23=5), "Hospital": serie(0, h12=2)})


class Continuidad(unittest.TestCase):
    def test_ens_total_y_por_alimentador(self):
        d = caso_h()
        self.assertAlmostEqual(ev.ens_kwh(d), 47.0)
        self.assertEqual(ev.ens_por_alimentador(d), {"A1": 47.0})

    def test_ens_por_carga_y_grupo(self):
        d, red = caso_h(), red_mano()
        self.assertEqual(ev.ens_por_carga(d, red), {"Hospital": 2.0, "Casa": 45.0})   # 40 + 5
        g = ev.ens_por_grupo(d, red)
        self.assertEqual(g["Usuarios residenciales y comerciales"], 45.0)
        self.assertEqual(g["Industria y servicios públicos"], 0.0)
        self.assertEqual(g["Cargas críticas (hospital)"], 2.0)
        self.assertEqual(ev.ens_critica_kwh(d, red), 2.0)

    def test_pct_servida(self):
        d = caso_h()
        # 100 × (1 − 47 / 1 200) = 96.08333 %
        self.assertAlmostEqual(ev.pct_servida(d), 96.083333, places=5)
        self.assertAlmostEqual(ev.pct_servida_por_alimentador(d)["A1"], 96.083333, places=5)
        self.assertAlmostEqual(ev.servida_kwh(d), 1153.0)
        self.assertEqual(ev.pct_servida(dist(serie(0))), 100.0)       # sin demanda: 100 %

    def test_saidi_saifi_caidi(self):
        a = ev.afectacion(caso_h(), red_mano())
        # Casa: horas 18-21 y 23 → 5 h en 2 eventos; Hospital: hora 12 → 1 h en 1 evento
        self.assertEqual(a["horas_por_carga"], {"Hospital": 1, "Casa": 5})
        self.assertEqual(a["eventos_por_carga"], {"Hospital": 1, "Casa": 2})
        self.assertAlmostEqual(a["saidi_h"], 3.0)      # (1 + 5) / 2 cargas
        self.assertAlmostEqual(a["saifi"], 1.5)        # (1 + 2) / 2 cargas
        self.assertAlmostEqual(a["caidi_h"], 2.0)      # 6 h / 3 eventos
        self.assertEqual(a["cargas_afectadas"], 2)

    def test_horas_deficit_y_ens_no_prevista(self):
        d = caso_h()
        self.assertEqual(ev.horas_deficit(d), 2)       # no servido en las horas 12 y 23
        real = dist(serie(50), ns_carga={"Casa": serie(0, h23=8), "Hospital": serie(0, h12=2)})
        self.assertAlmostEqual(ev.ens_no_prevista_kwh(real, d), 3.0)   # (8 + 2) − (5 + 2)
        self.assertEqual(ev.ens_no_prevista_kwh(d, real), 0.0)          # nunca negativa

    def test_resultado_decision(self):
        def sis(estado):
            return SimpleNamespace(salida=SimpleNamespace(reporte=None if estado is None else {"estado": estado}))
        self.assertEqual(ev.resultado_decision(sis("APROBADA")), "propuesta")
        self.assertEqual(ev.resultado_decision(sis("ESCALADA AL OPERADOR")), "escalamiento")
        self.assertEqual(ev.resultado_decision(sis(None)), "roto")
        self.assertEqual(ev.resultado_decision(sis("APROBADA"), "KeyError: 'B'"), "roto")


class Eficiencia(unittest.TestCase):
    def test_renovable(self):
        # disponible 20 kW de 10 a 14 h (100 kWh), usada 15 kW (75 kWh), desperdiciada 25 kWh
        rd = serie(0, h10=20, h11=20, h12=20, h13=20, h14=20)
        ru = serie(0, h10=15, h11=15, h12=15, h13=15, h14=15)
        r = ev.renovable(dist(serie(50), ru=ru, rdisp=rd, rdesp=rd - ru))
        self.assertEqual((r["disponible_kwh"], r["usada_kwh"], r["desperdiciada_kwh"]), (100.0, 75.0, 25.0))
        self.assertAlmostEqual(r["pct_aprovechada"], 75.0)
        self.assertAlmostEqual(r["pct_desperdiciada"], 25.0)
        self.assertAlmostEqual(r["pct_demanda"], 6.25)          # 75 / 1 200

    def test_perdidas(self):
        # demanda 50 kW, renovable usada 5 kW → convencional 45 kW; pérdidas 0.9 kW por hora
        # pérdidas = 21.6 kWh; entregada = 24 × (5 + 45) = 1 200 kWh; 21.6 / 1 200 = 1.8 %
        p = ev.perdidas(dist(serie(50), ru=serie(5), perd=serie(0.9)))
        self.assertAlmostEqual(p["kwh"], 21.6)
        self.assertAlmostEqual(p["entregada_kwh"], 1200.0)
        self.assertAlmostEqual(p["pct"], 1.8)

    def test_utilizacion(self):
        # cabecera 40 kW salvo 80 kW a las 19 h, capacidad 100 kW → máx 80 %, media 1 000/24 = 41.667 %
        # subestación: importación 50 kW constante, capacidad 200 kW → 25 %
        d = dist(serie(50), flujo=serie(40, h19=80), cap_se=200.0)
        u = ev.utilizacion(d)
        self.assertAlmostEqual(u["A1"]["max_pct"], 80.0)
        self.assertAlmostEqual(u["A1"]["media_pct"], 41.666667, places=5)
        self.assertAlmostEqual(u["SE"]["max_pct"], 25.0)
        self.assertAlmostEqual(u["SE"]["media_pct"], 25.0)

    def test_factor_carga_y_pico(self):
        self.assertAlmostEqual(ev.factor_carga(np.array([1, 2, 3, 2])), 2 / 3)   # promedio 2 / pico 3
        self.assertEqual(ev.factor_carga(np.zeros(4)), 0.0)
        s0 = dist(serie(10, h19=12))
        p = dist(serie(10))
        self.assertAlmostEqual(ev.reduccion_pico_pct(s0, p), 100 * 2 / 12)      # 16.667 %

    def test_operativa(self):
        self.assertAlmostEqual(ev.desplazado_por_deficit_evitado(30, 50, 20), 1.0)   # 30 / (50 − 20)
        self.assertIsNone(ev.desplazado_por_deficit_evitado(30, 20, 20))
        esc = SimpleNamespace(movimientos=[([18], [2], 5.0), ([19], [3], 4.0)],
                              recorte={"Casa": serie(0, h18=40), "Otra": serie(0)})
        self.assertEqual(ev.n_acciones(esc), 3)          # 2 movimientos + 1 carga recortada
        self.assertEqual(ev.n_acciones(None), 0)

    def test_pronostico(self):
        self.assertAlmostEqual(ev.mape_pct(np.array([110, 90, 5]), np.array([100, 100, 0])), 10.0)
        # sesgo: (95 + 95 − 200) / 200 = −5 %; el MAPE del mismo caso es 5 % (no ve el signo)
        self.assertAlmostEqual(ev.sesgo_pct(np.array([95, 95]), np.array([100, 100])), -5.0)
        self.assertAlmostEqual(ev.sesgo_pct(np.array([110, 90]), np.array([100, 100])), 0.0)
        self.assertIsNone(ev.sesgo_pct(np.array([1.0]), np.array([0.0])))
        # hora 0: banda y real < 1 kW → no cuenta; hora 1 dentro; hora 2 fuera → 1 de 2
        self.assertEqual(ev.cobertura(np.array([0, 8, 8]), np.array([0, 12, 12]), np.array([0, 10, 13])), (1, 2))
        self.assertAlmostEqual(ev.ancho_relativo_pct(np.array([8, 0]), np.array([10, 0]), np.array([12, 0])), 40.0)

    def test_sistema(self):
        s = SimpleNamespace(bus=SimpleNamespace(costo_total=35, enviados=8))
        self.assertEqual(ev.coordinacion(s), {"costo": 35, "mensajes": 8})
        self.assertAlmostEqual(ev.percentil([1, 2, 3, 4, 5], 95), 4.8)      # 4 + 0.8 × (5 − 4)


class Costos(unittest.TestCase):
    def test_costo_energia(self):
        # importación 10 kW × Σ tarifas 44.6 = 446 MXN; + 0.30 × 24 × 5 kWh renovables = 36 MXN
        self.assertAlmostEqual(ev.costo_energia_mxn(dist(serie(10)), TARIFAS), 446.0)
        d = dist(serie(15), ru=serie(5))
        self.assertAlmostEqual(ev.costo_energia_mxn(d, TARIFAS), 482.0)
        # costo por kWh servido = 482 / 360 kWh
        self.assertAlmostEqual(ev.costo_por_kwh_servido(d, TARIFAS), 482 / 360)

    def test_ahorro_efecto_tarifa(self):
        # Mover 2 kW de punta (18-21 h) a base (0-3 h): −4×2×3.20 + 4×2×1.10 = −16.8 MXN
        s0 = dist(serie(10))
        p = dist(serie(10, h0=12, h1=12, h2=12, h3=12, h18=8, h19=8, h20=8, h21=8))
        a = ev.descomponer_ahorro(s0, p, TARIFAS)
        self.assertAlmostEqual(a["ahorro_mxn"], 16.8)
        self.assertAlmostEqual(a["efecto_tarifa_mxn"], 16.8)
        for k in ("efecto_renovable_mxn", "efecto_perdidas_mxn", "efecto_servicio_mxn"):
            self.assertAlmostEqual(a[k], 0.0)
        self.assertAlmostEqual(a["ahorro_pct"], 100 * 16.8 / 446)

    def test_ahorro_suma_exacta(self):
        # S0: 10 kW convencional. Propuesta: 2 kW renovables, 1 kW recortado y 0.1 kW de pérdidas.
        # efecto_renovable = 48 × (446/240 − 0.30); efecto_servicio = 24 × 446/240; pérdidas −2.4 × 446/240
        s0 = dist(serie(10))
        p = dist(serie(10), rec_carga={"Casa": serie(1)}, ru=serie(2), perd=serie(0.1))
        a = ev.descomponer_ahorro(s0, p, TARIFAS)
        t0 = 446 / 240
        self.assertAlmostEqual(a["efecto_renovable_mxn"], 48 * (t0 - 0.30))
        self.assertAlmostEqual(a["efecto_servicio_mxn"], 24 * t0)
        self.assertAlmostEqual(a["efecto_perdidas_mxn"], -2.4 * t0)
        suma = sum(a[k] for k in ("efecto_tarifa_mxn", "efecto_renovable_mxn", "efecto_perdidas_mxn",
                                  "efecto_servicio_mxn"))
        self.assertAlmostEqual(suma, a["ahorro_mxn"], places=9)

    def test_costo_social_y_total(self):
        # CENS: residencial 20, crítica 1 000 MXN/kWh; factor sin aviso 2
        # Casa: (40 recorte + 2 × 5 no servido) × 20 = 1 000; Hospital: 2 × 2 × 1 000 = 4 000
        cens = {"residencial": 20.0, "critica": 1000.0}
        d, red = caso_h(), red_mano()
        self.assertAlmostEqual(ev.costo_social_ens_mxn(d, red, cens, 2.0), 5000.0)
        self.assertAlmostEqual(ev.costo_social_ens_mxn(d, red, cens, 1.0), 45 * 20 + 2 * 1000)
        self.assertAlmostEqual(ev.costo_total_mxn(d, red, TARIFAS, cens, 2.0),
                               ev.costo_energia_mxn(d, TARIFAS) + 5000.0)

    def test_costo_robustez(self):
        # recorte adicional de Casa 20 kWh × 20 = 400 MXN; beneficio 0.1 × 30 kWh × 20 = 60 → neto −340
        cens = {"residencial": 20.0, "critica": 1000.0}
        d_min = dist(serie(50), rec_carga={"Casa": serie(0, h18=40)})
        d_rob = dist(serie(50), rec_carga={"Casa": serie(0, h18=40, h19=20)})
        r = ev.costo_robustez(d_min, d_rob, red_mano(), cens, evitado_kwh=30, prob_desfavorable=0.1)
        self.assertAlmostEqual(r["recorte_adicional_kwh"], 20.0)
        self.assertAlmostEqual(r["costo_mxn"], 400.0)
        self.assertAlmostEqual(r["beneficio_esperado_mxn"], 60.0)
        self.assertAlmostEqual(r["neto_mxn"], -340.0)


class ConAsignar(unittest.TestCase):
    """Caso a mano resuelto por distribucion.asignar: subestación de 40 kW, Hospital 10 kW y
    Casa 50 kW (60 kW de demanda), sin pérdidas ni renovables. Faltan 20 kW cada hora y, por
    prioridad, todos son de la Casa: ENS = 20 × 24 = 480 kWh; servida = 40 × 24 = 960 kWh;
    costo = 40 × 44.6 = 1 784 MXN; costo por kWh servido = 1 784 / 960 = 1.858 MXN/kWh."""

    def _asignar(self, recortar: bool):
        red = red_mano(cap_se=40.0)
        cargas = {"Hospital": serie(10), "Casa": serie(50)}
        return red, asignar(red, cargas, {}, limites_red(red), red.vector_tarifas(), recortar=recortar)

    def test_sin_medidas(self):
        red, d = self._asignar(False)
        self.assertAlmostEqual(ev.ens_kwh(d), 480.0)
        self.assertEqual(ev.ens_critica_kwh(d, red), 0.0)
        self.assertAlmostEqual(ev.costo_energia_mxn(d, red.vector_tarifas()), 1784.0)
        self.assertAlmostEqual(ev.costo_por_kwh_servido(d, red.vector_tarifas()), 1784 / 960)
        self.assertEqual(ev.horas_deficit(d), 24)

    def test_con_recorte(self):
        red, d = self._asignar(True)
        self.assertAlmostEqual(ev.ens_kwh(d), 480.0)              # ahora todo es recorte avisado
        self.assertAlmostEqual(float(d.total("no_servido").sum()), 0.0)
        self.assertEqual(ev.horas_deficit(d), 0)


class ConsistenciaMotor(unittest.TestCase):
    """Corrida real (falla del transformador, 1m, semilla 7): los indicadores cuadran con el motor."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="ea_eval_")
        s = crear_sistema(periodo="1m", condicion="falla_transformador", modo="web", semilla=7, salida=cls.dir,
                          ruta_memoria=os.path.join(cls.dir, "m.json"), verbose=False, fin=datetime(2026, 11, 19))
        s.iniciar()
        cls.s = s
        cls.lista = s.pizarra.leer("escenarios")["lista"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_hay_contingencia(self):
        self.assertTrue(any(e.estrategia == "contingencia" for e in self.lista))

    def test_ens_igual_a_recorte_mas_no_servido(self):
        for e in self.lista:
            for d in (e.distribucion, e.distribucion_p90):
                res = d.resumen()
                esperado = sum(a["recorte_kwh"] + a["no_servido_kwh"] for a in res.values())
                with self.subTest(escenario=e.id):
                    self.assertLessEqual(abs(ev.ens_kwh(d) - esperado), 0.1)
                    self.assertLessEqual(abs(sum(ev.ens_por_carga(d, self.s.red).values()) - ev.ens_kwh(d)), 0.1)
                    self.assertLessEqual(abs(sum(ev.ens_por_alimentador(d).values()) - ev.ens_kwh(d)), 0.1)

    def test_costo_recalculado_desde_series(self):
        t = self.s.red.vector_tarifas()
        for e in self.lista:
            with self.subTest(escenario=e.id):
                self.assertAlmostEqual(ev.costo_energia_mxn(e.distribucion, t), float(e.distribucion.total("costo").sum()),
                                       places=6)
                self.assertLessEqual(abs(ev.costo_energia_mxn(e.distribucion, t) - e.metricas["costo_mxn"]), 0.01)
                self.assertLessEqual(abs(ev.renovable(e.distribucion)["pct_demanda"] - e.metricas["renovable_pct"]), 0.01)

    def test_descomposicion_exacta_en_el_motor(self):
        t = self.s.red.vector_tarifas()
        s0 = next(e for e in self.lista if e.id == "S0")
        for e in self.lista:
            a = ev.descomponer_ahorro(s0.distribucion, e.distribucion, t)
            suma = sum(a[k] for k in ("efecto_tarifa_mxn", "efecto_renovable_mxn", "efecto_perdidas_mxn",
                                      "efecto_servicio_mxn"))
            with self.subTest(escenario=e.id):
                self.assertAlmostEqual(suma, a["ahorro_mxn"], places=6)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    unittest.main(verbosity=2)
