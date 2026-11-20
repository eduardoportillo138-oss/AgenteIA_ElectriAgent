"""
Pruebas de los mecanismos de coordinación de ElectriAgent.
Ejecutar:  python pruebas.py
"""
import contextlib
import io
import os
import subprocess
import sys

from coordinacion import (Bitacora, BusCoordinacion, Mensaje, Performativa as P, PermisoDenegado,
                          Pizarra, ViolacionJerarquia, Agente)

AQUI = os.path.dirname(os.path.abspath(__file__))


class Eco(Agente):
    def procesar(self, msg):
        pass


def _bus():
    b = Bitacora()
    bus = BusCoordinacion(b)
    pz = Pizarra(b)
    for n in "ABCDEFGH":
        Eco(n, bus, pz)
    return bus, pz


def prueba_jerarquia():
    bus, _ = _bus()
    bus.enviar(Mensaje("C", "D", P.REQUEST, {}, "t"))          # permitido
    for emisor, receptor in [("D", "E"), ("E", "D"), ("B", "D"), ("D", "H")]:
        try:
            bus.enviar(Mensaje(emisor, receptor, P.INFORM, {}, "t"))
        except ViolacionJerarquia:
            continue
        raise AssertionError(f"{emisor}->{receptor} debió bloquearse")
    bus.enviar(Mensaje("E", "C", P.FAILURE, {"error": "x"}, "t"))   # canal de excepciones
    print("OK  jerarquía: solo se permiten los enlaces del grafo (+ FAILURE al supervisor)")


def prueba_pizarra():
    _, pz = _bus()
    pz.escribir("D", "analisis", {"x": 1})
    try:
        pz.escribir("E", "analisis", {"x": 2})
    except PermisoDenegado:
        print("OK  pizarra: cada sección solo la escribe su agente dueño")
        return
    raise AssertionError("E no debe escribir 'analisis'")


def _ejecutar(*args):
    # UTF-8 explícito: en Windows la consola redirigida usa cp1252 y rompe acentos y símbolos
    r = subprocess.run([sys.executable, "main.py", "--modo", "auto", "--salida", "salida_pruebas", *args],
                       cwd=AQUI, capture_output=True, text=True, encoding="utf-8",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert r.returncode == 0, r.stderr
    return r.stdout


def _costo(salida):
    return int(salida.split("pesos del grafo): ")[1].split()[0])


def prueba_flujos():
    assert _costo(_ejecutar()) == 36
    print("OK  flujo aprobado a la primera: costo 36 (A-B-C-D-C-E-F-C-G-H)")
    assert _costo(_ejecutar("--rechazos", "1")) == 60
    print("OK  un rechazo agrega el ciclo G-C-E-F-C-G: costo 60")
    s = _ejecutar("--rechazos", "9")
    assert "ESCALADA AL OPERADOR" in s
    print("OK  tras 3 rechazos el supervisor escala al operador")
    s = _ejecutar("--condicion", "falla")
    assert "contingencia" in s and "Hospital General" not in s.split("RESULTADO")[1]
    print("OK  contingencia: recorte parcial controlado sin tocar la carga crítica")
    s = _ejecutar("--condicion", "ola_calor")
    assert "Déficit 0.0 kWh" in s
    print("OK  ola de calor: el desplazamiento elimina el déficit de la operación actual")
    for agente in ("aprendizaje", "deliberativo", "iso"):
        s = _ejecutar("--falla", agente)
        assert "reintento" in s and "APROBADA" in s
    print("OK  fallas transitorias en D, E o F: el supervisor reintenta y el flujo termina")


def prueba_api():
    """Servidor web: la propuesta espera al operador; rechazo y aprobación por la API."""
    import json
    import threading
    import urllib.error
    import urllib.request

    import servidor
    srv = servidor.crear_servidor("127.0.0.1", 0, silencioso=True, token="")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/api"

    def pedir(metodo, ruta, cuerpo=None):
        req = urllib.request.Request(base + ruta, method=metodo,
                                     data=None if cuerpo is None else json.dumps(cuerpo).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        st, v = pedir("POST", "/sesiones", {"periodo": "1m", "condicion": "ola_calor"})
        assert st == 201 and v["estado"] == "en_espera" and v["propuesta"], v
        assert v["coordinacion"]["recorrido"][-1] == "C->G" and not v["terminado"]
        sid, primera = v["id"], v["propuesta"]
        print("OK  API: la propuesta queda en espera en G; nada se ejecuta sin el operador")

        st, _ = pedir("POST", f"/sesiones/{sid}/aprobacion", {"aprobado": False})
        assert st == 400
        st, v = pedir("POST", f"/sesiones/{sid}/aprobacion", {"aprobado": False, "motivo": "Muy invasiva"})
        assert st == 200 and v["estado"] == "en_espera"
        segunda = v["propuesta"]
        assert segunda["escenario"] != primera["escenario"]
        assert segunda["metricas"]["desplazado_kwh"] < primera["metricas"]["desplazado_kwh"]
        print("OK  API: el rechazo exige motivo y produce una alternativa menos invasiva")

        st, v = pedir("POST", f"/sesiones/{sid}/aprobacion", {"aprobado": True})
        assert st == 200 and v["estado"] == "aprobada" and v["terminado"]
        assert v["coordinacion"]["costo"] == 60
        st, rep = pedir("GET", f"/sesiones/{sid}/reporte")
        assert st == 200 and rep["estado"] == "APROBADA"
        st, _ = pedir("POST", f"/sesiones/{sid}/aprobacion", {"aprobado": True})
        assert st == 409
        print("OK  API: aprobación, reporte final y bloqueo de una segunda decisión")

        st, v = pedir("POST", "/sesiones", {"condicion": "falla"})
        assert v["propuesta"]["modo"] == "contingencia" and v["propuesta"]["serie"]["horas_suspension"]
        assert pedir("POST", "/sesiones", {"periodo": "2a"})[0] == 400
        assert pedir("GET", "/sesiones/0000000000")[0] == 404
        print("OK  API: contingencia visible y validación de entradas")
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------ distribución y escenarios
TOL = 0.1   # kW


def _catalogo():
    from condiciones import ALIMENTADORES_FALLA, CONDICIONES
    lista = []
    for c, v in CONDICIONES.items():
        if "alias_de" in v:
            continue
        lista += [(c, a) for a in ALIMENTADORES_FALLA] if c == "falla_alimentador" else [(c, None)]
    return lista


_CACHE: dict = {}


def _sistema(condicion, alimentador=None, periodo="1m"):
    """Ejecuta el flujo completo en modo auto (en proceso) y lo guarda para reutilizarlo."""
    import tempfile
    from motor import crear_sistema
    clave = (condicion, alimentador, periodo)
    if clave not in _CACHE:
        s = crear_sistema(periodo=periodo, condicion=condicion, alimentador=alimentador, modo="auto",
                          verbose=False, salida=tempfile.mkdtemp(prefix="electriagent_"))
        with contextlib.redirect_stdout(io.StringIO()):
            s.iniciar()
        assert s.terminado, f"{clave}: el flujo no terminó"
        _CACHE[clave] = s
    return _CACHE[clave]


def _recomendado(s):
    dec = s.pizarra.leer("decision")
    return next(e for e in s.pizarra.leer("escenarios")["lista"] if e.id == dec["escenario"])


def prueba_balance():
    """Criterio 1: demanda = renovable usada + convencional + recorte (+ no servido) por hora y alimentador."""
    for cond, alim in _catalogo():
        s = _sistema(cond, alim)
        for esc in s.pizarra.leer("escenarios")["lista"]:
            for d in (esc.distribucion, esc.distribucion_p90):
                for k in d.claves:
                    resto = d.demanda[k] - (d.renovable_usada[k] + d.convencional[k] + d.recorte[k] + d.no_servido[k])
                    assert abs(resto).max() <= TOL, (cond, alim, esc.id, k, abs(resto).max())
        d = _recomendado(s).distribucion
        for k in d.claves:
            assert d.no_servido[k].max() <= TOL, (cond, alim, k)
            resto = d.demanda[k] - (d.renovable_usada[k] + d.convencional[k] + d.recorte[k])
            assert abs(resto).max() <= TOL, (cond, alim, k, abs(resto).max())
    print("OK  balance por hora y alimentador: demanda = renovable + convencional + recorte (±0.1 kW)")


def prueba_capacidades():
    """Criterio 2: el escenario recomendado no supera ningún alimentador, transformador BT ni la subestación."""
    for cond, alim in _catalogo():
        s = _sistema(cond, alim)
        d = _recomendado(s).distribucion
        lim = d.limites
        assert d.importacion_subestacion.max() <= lim.subestacion_kw + TOL, (cond, alim)
        for k in d.claves:
            assert d.flujo_cabecera[k].max() <= lim.alimentador_kw[k] + TOL, (cond, alim, k)
            if lim.transformador_bt_kw[k] != float("inf"):
                assert d.flujo_trafo_bt[k].max() <= lim.transformador_bt_kw[k] + TOL, (cond, alim, k)
    print("OK  ningún alimentador, transformador BT o de subestación se excede en lo recomendado")


def prueba_falla_alimentador_hospital():
    """Criterio 3: con falla del A1 el hospital conserva el 100 % de su suministro."""
    s = _sistema("falla_alimentador", "A1")
    esc = _recomendado(s)
    assert esc.estrategia == "contingencia" and esc.metricas["recorte_kwh"] > 0
    for d in (esc.distribucion, esc.distribucion_p90):
        assert "Hospital General" not in d.recorte_por_carga
        assert "Hospital General" not in d.no_servido_por_carga
    assert "critica" not in esc.metricas["recorte_por_tipo_kwh"]
    assert esc.metricas["alimentadores"]["A1"]["no_servido_kwh"] == 0
    rep = s.salida.reporte
    assert "Hospital General" not in str(rep["recomendacion"]["acciones"])
    print("OK  falla del alimentador A1: el hospital conserva el 100 % de su suministro")


def prueba_recorte_parcial():
    """Criterio 4: lo recortado nunca supera el déficit (de la banda planificada) más 5 %."""
    revisados = 0
    for cond, alim in _catalogo():
        for esc in _sistema(cond, alim).pizarra.leer("escenarios")["lista"]:
            m = esc.metricas
            assert m["recorte_kwh"] <= m["deficit_planificado_kwh"] * 1.05 + TOL, (cond, alim, esc.id)
            if esc.estrategia == "contingencia" and esc.base_planificacion == "P50":
                d = esc.distribucion
                assert m["recorte_kwh"] <= m["deficit_previo_kwh"] * 1.05 + TOL, (cond, alim, esc.id)
                for k in d.claves:   # también hora por hora
                    assert (d.recorte[k] <= d.deficit[k] * 1.05 + TOL).all(), (cond, alim, esc.id, k)
                revisados += 1
    assert revisados >= 4
    print("OK  recorte parcial: la energía recortada nunca supera el déficit + 5 %")


def prueba_baja_renovable():
    """Criterio 5: baja la cobertura renovable y la regla 1 se cumple (sin déficit o con contingencia)."""
    normal, baja = _recomendado(_sistema("normal")), _recomendado(_sistema("baja_renovable"))
    assert baja.metricas["renovable_pct"] < normal.metricas["renovable_pct"]
    dec = _sistema("baja_renovable").pizarra.leer("decision")
    assert baja.metricas["deficit_kwh"] == 0 or dec["modo"] == "contingencia"
    print(f"OK  baja renovable: cobertura {normal.metricas['renovable_pct']} % -> "
          f"{baja.metricas['renovable_pct']} % y la regla 1 se cumple")


def prueba_bandas():
    """Criterio 6: P10 <= P50 <= P90 y la cobertura medida queda en la evaluación ISO/IEC 5338."""
    for periodo in ("1d", "1m"):
        s = _sistema("normal", periodo=periodo)
        a = s.pizarra.leer("analisis")
        for grupo in ("bandas_cargas", "bandas_renovables"):
            for nombre, b in a[grupo].items():
                assert (b["p10"] <= b["p50"] + 1e-9).all() and (b["p50"] <= b["p90"] + 1e-9).all(), (periodo, nombre)
        t = s.pizarra.leer("evaluacion")["trazabilidad"]
        assert "cobertura_bandas_pct" in t and "alerta_calibracion_bandas" in t
        if periodo == "1m":
            assert 0 <= t["cobertura_bandas_pct"] <= 100 and t["lecturas_validacion_bandas"] > 0
        else:
            assert t["cobertura_bandas_pct"] is None      # con 1 día no hay validación fuera de muestra
    for esc in _sistema("normal").pizarra.leer("escenarios")["lista"]:
        assert esc.metricas_p90["demanda_kwh"] >= esc.metricas["demanda_kwh"] - TOL
    print(f"OK  bandas P10 <= P50 <= P90; cobertura registrada en ISO/IEC 5338 ({t['cobertura_bandas_pct']} % con 1 mes)")


def prueba_catalogo():
    """Criterio 7: cada condición termina A->H sin error en modo auto (consola) y en la API."""
    import json
    import threading
    import urllib.request
    import servidor

    for cond, alim in _catalogo():
        extra = ["--alimentador", alim] if alim else []
        s = _ejecutar("--condicion", cond, *extra)
        assert "RESULTADO: APROBADA" in s and _costo(s) == 36, (cond, alim)
    print(f"OK  catálogo: las {len(_catalogo())} condiciones completan A->H en modo auto (costo 36)")

    srv = servidor.crear_servidor("127.0.0.1", 0, silencioso=True, token="")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/api"

    def post(ruta, cuerpo):
        req = urllib.request.Request(base + ruta, method="POST", data=json.dumps(cuerpo).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    try:
        for cond, alim in _catalogo():
            cuerpo = {"condicion": cond, **({"alimentador": alim} if alim else {})}
            st, v = post("/sesiones", cuerpo)
            assert st == 201 and v["estado"] == "en_espera", (cond, alim, v.get("estado"))
            p = v["propuesta"]
            assert set(p["serie"]["por_alimentador"]) == {"SE", "A1", "A2"} and "bandas" in p["serie"]
            assert isinstance(p["robusto"], bool) and "alimentadores" in p
            st, v = post(f"/sesiones/{v['id']}/aprobacion", {"aprobado": True})
            assert st == 200 and v["estado"] == "aprobada" and v["coordinacion"]["costo"] == 36, (cond, alim)
        st, v = post("/sesiones", {"condicion": "crecimiento_demanda", "crecimiento_pct": 30})
        assert st == 201 and v["condicion"]["parametros"]["crecimiento_pct"] == 30
    finally:
        srv.shutdown()
        srv.server_close()
    print("OK  catálogo en la API: propuesta por alimentador con bandas, aprobación y costo 36")


# ------------------------------------------------ API para la app móvil
# Claves de la vista que consume la app (mobile/src/api/tipos.ts). Si cambian, hay que
# actualizar los tipos de la app: esta prueba es el contrato entre ambos.
CONTRATO_VISTA = {"id", "estado", "periodo", "condicion", "dia_recomendado", "coordinacion", "consola",
                  "analisis", "anomalias", "propuesta", "alternativas", "trazabilidad", "historial",
                  "motivo_escalamiento", "terminado"}
CONTRATO_PROPUESTA = {"escenario", "nombre", "estrategia", "modo", "justificacion", "acciones", "metricas",
                      "metricas_base", "evaluacion_iso", "serie", "robusto", "metricas_p90",
                      "base_planificacion", "alimentadores", "recorte_por_carga"}
CONTRATO_METRICAS = {"costo_mxn", "renovable_pct", "margen_min_pct", "desplazado_kwh", "deficit_kwh",
                     "recorte_kwh", "perdidas_kwh", "deficit_p90_kwh"}
CONTRATO_SERIE = {"demanda_base", "demanda_recomendada", "renovable", "capacidad_kw", "horas_suspension",
                  "bandas", "por_alimentador"}
CONTRATO_SERIE_ALIM = {"nombre", "demanda_base", "demanda_recomendada", "renovable", "capacidad_kw", "limite",
                       "demanda_p10", "demanda_p90", "recorte", "horas_recorte"}
CONTRATO_ALTERNATIVA = {"id", "nombre", "costo_mxn", "renovable_pct", "deficit_kwh", "desplazado_kwh",
                        "suspendido_kwh", "riesgo_max", "aceptable", "recorte_kwh", "robusto"}
CONTRATO_CONFIG = {"sistema", "periodos", "condiciones", "grafo", "red", "tolerancia_riesgo",
                   "alimentadores_falla"}


def _api_cliente(srv):
    import json
    import urllib.error
    import urllib.request
    base = f"http://127.0.0.1:{srv.server_address[1]}/api"

    def pedir(metodo, ruta, cuerpo=None, cabeceras=None, crudo=False):
        req = urllib.request.Request(base + ruta, method=metodo,
                                     data=None if cuerpo is None else json.dumps(cuerpo).encode(),
                                     headers={"Content-Type": "application/json", **(cabeceras or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return (r.status, dict(r.headers)) if crudo else (r.status, json.loads(r.read() or b"{}"))
        except urllib.error.HTTPError as e:
            return (e.code, dict(e.headers)) if crudo else (e.code, json.loads(e.read() or b"{}"))
    return pedir


def _servidor(**kw):
    import threading
    import servidor
    srv = servidor.crear_servidor("127.0.0.1", 0, silencioso=True, **{"token": "", **kw})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def prueba_api_movil():
    import servidor
    # Salud y autenticación por token
    srv = _servidor(token="token-de-prueba")
    pedir = _api_cliente(srv)
    try:
        assert pedir("GET", "/salud")[0] == 401
        st, v = pedir("GET", "/config", cabeceras={"Authorization": "Bearer otro"})
        assert st == 401 and "token" in v["error"]
        st, v = pedir("GET", "/salud", cabeceras={"Authorization": "Bearer token-de-prueba"})
        assert st == 200 and v["estado"] == "ok" and v["version_api"] and v["version_modelo"]
        assert pedir("OPTIONS", "/sesiones", crudo=True)[0] == 204    # la verificación previa no exige token
    finally:
        srv.shutdown()
        srv.server_close()
    print("OK  API: token obligatorio cuando existe (401 si falta o es incorrecto) y /api/salud")

    srv = _servidor(origenes=("http://localhost:8081",))
    pedir = _api_cliente(srv)
    try:
        st, v = pedir("GET", "/salud")
        assert st == 200 and set(v) == {"estado", "version_api", "version_modelo"}
        # CORS restringido
        _, h = pedir("GET", "/salud", cabeceras={"Origin": "http://localhost:8081"}, crudo=True)
        assert h.get("Access-Control-Allow-Origin") == "http://localhost:8081"
        _, h = pedir("GET", "/salud", cabeceras={"Origin": "http://sitio-ajeno.example"}, crudo=True)
        assert "Access-Control-Allow-Origin" not in h
        print("OK  API: sin token se comporta como antes; CORS solo para orígenes permitidos")

        # Operador que firma la decisión: historial, pizarra y bitácora
        st, v = pedir("POST", "/sesiones", {"condicion": "ola_calor"})
        sid = v["id"]
        st, e = pedir("POST", f"/sesiones/{sid}/aprobacion", {"aprobado": True, "operador": "x" * 81})
        assert st == 400 and "operador" in e["error"]
        st, v = pedir("POST", f"/sesiones/{sid}/aprobacion",
                      {"aprobado": False, "motivo": "Muy invasiva", "operador": "Ing. Pruebas"})
        assert st == 200 and v["historial"][-1]["operador"] == "Ing. Pruebas"
        st, v = pedir("POST", f"/sesiones/{sid}/aprobacion", {"aprobado": True, "operador": " Ing. Pruebas "})
        assert st == 200 and v["estado"] == "aprobada" and v["historial"][-1]["operador"] == "Ing. Pruebas"
        s = servidor.obtener(sid)
        assert s.pizarra.leer("aprobacion")["operador"] == "Ing. Pruebas"
        firmas = [e for e in s.bitacora.eventos if e["tipo"] == "decision_humana"]
        assert len(firmas) == 2 and all(f["operador"] == "Ing. Pruebas" for f in firmas)
        st, rep = pedir("GET", f"/sesiones/{sid}/reporte")
        assert rep["aprobacion"]["operador"] == "Ing. Pruebas"
        assert pedir("POST", f"/sesiones/{sid}/aprobacion", {"aprobado": True})[0] == 409
        print("OK  API: el operador que firma queda en el historial, la pizarra, el reporte y la bitácora")

        # Listado de sesiones en memoria
        st, otra = pedir("POST", "/sesiones", {"condicion": "falla_alimentador", "alimentador": "A1"})
        st, lista = pedir("GET", "/sesiones")
        assert st == 200
        ids = [x["id"] for x in lista["sesiones"]]
        assert ids.index(otra["id"]) < ids.index(sid)       # la más reciente primero
        item = next(x for x in lista["sesiones"] if x["id"] == sid)
        assert set(item) == {"id", "fecha", "periodo", "condicion", "estado", "escenario"}
        assert item["estado"] == "aprobada" and item["escenario"]
        print("OK  API: /api/sesiones lista las sesiones en memoria (id, fecha, periodo, condición, estado, escenario)")

        # Contrato con la app móvil
        st, cfg = pedir("GET", "/config")
        assert CONTRATO_CONFIG <= set(cfg)
        for vista in (otra, pedir("GET", f"/sesiones/{sid}")[1]):
            assert CONTRATO_VISTA <= set(vista), CONTRATO_VISTA - set(vista)
            p = vista["propuesta"]
            assert CONTRATO_PROPUESTA <= set(p), CONTRATO_PROPUESTA - set(p)
            assert CONTRATO_METRICAS <= set(p["metricas"])
            assert CONTRATO_SERIE <= set(p["serie"])
            for serie in p["serie"]["por_alimentador"].values():
                assert CONTRATO_SERIE_ALIM <= set(serie)
                assert all(len(serie[k]) == 24 for k in ("demanda_base", "demanda_p10", "demanda_p90", "limite"))
            for a in vista["alternativas"]:
                assert CONTRATO_ALTERNATIVA <= set(a)
            assert {"riesgos", "impactos", "riesgo_max", "categoria_max"} <= set(p["evaluacion_iso"])
            assert {"version_modelo", "accion_monitoreo", "cobertura_bandas_pct"} <= set(vista["trazabilidad"])
            assert {"recorrido", "costo", "mensajes"} == set(vista["coordinacion"])
        print("OK  API: contrato de la vista y de la configuración que consume la app móvil")
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    with contextlib.redirect_stderr(io.StringIO()):
        prueba_jerarquia()
        prueba_pizarra()
        prueba_flujos()
        prueba_api()
        prueba_balance()
        prueba_capacidades()
        prueba_falla_alimentador_hospital()
        prueba_recorte_parcial()
        prueba_baja_renovable()
        prueba_bandas()
        prueba_catalogo()
        prueba_api_movil()
    print("\nTodas las pruebas pasaron.")
