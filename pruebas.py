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
    r = subprocess.run([sys.executable, "main.py", "--modo", "auto", "--salida", "salida_pruebas", *args],
                       cwd=AQUI, capture_output=True, text=True)
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
    print("OK  contingencia: suspensiones controladas sin tocar la carga crítica")
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
    srv = servidor.crear_servidor("127.0.0.1", 0, silencioso=True)
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


if __name__ == "__main__":
    with contextlib.redirect_stderr(io.StringIO()):
        prueba_jerarquia()
        prueba_pizarra()
        prueba_flujos()
        prueba_api()
    print("\nTodas las pruebas pasaron.")
