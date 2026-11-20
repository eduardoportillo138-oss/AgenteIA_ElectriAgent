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


if __name__ == "__main__":
    with contextlib.redirect_stderr(io.StringIO()):
        prueba_jerarquia()
        prueba_pizarra()
        prueba_flujos()
    print("\nTodas las pruebas pasaron.")
