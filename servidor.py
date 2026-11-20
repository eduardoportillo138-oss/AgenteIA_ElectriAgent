"""
Servidor web de ElectriAgent: API REST + aplicación web / móvil (PWA).

Solo usa la biblioteca estándar de Python (más numpy, que ya usa el motor).

    python servidor.py                      # http://127.0.0.1:8000
    python servidor.py --host 0.0.0.0       # para abrirlo desde el celular en la misma red Wi-Fi

API
    GET  /api/config                        periodos, condiciones, grafo y red
    POST /api/sesiones                      {periodo, condicion, alimentador?, crecimiento_pct?,
                                             falla?, semilla?} -> crea y analiza
    GET  /api/sesiones/<id>                 estado de la sesión
    POST /api/sesiones/<id>/aprobacion      {aprobado: bool, motivo?: str} -> decisión del operador
    GET  /api/sesiones/<id>/reporte         reporte final (JSON descargable)
    GET  /api/sesiones/<id>/bitacora        bitácora de trazabilidad (ISO/IEC 5338)

El sistema es informativo: no controla dispositivos ni la red. Toda
recomendación queda en espera hasta que una persona la aprueba o la rechaza.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import threading
import uuid
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import numpy as np

from agentes import COBERTURA_NOMINAL, MAPE_REENTRENAR, TOLERANCIA_RIESGO, VERSION_MODELO
from condiciones import ALIMENTADORES_FALLA, CONDICIONES, parametros, resolver
from coordinacion import ARISTAS, NODOS
from modelo_red import NOMBRE_PERIODO, PERIODOS, red_ejemplo
from motor import FALLAS, Sistema, a_json, crear_sistema

AQUI = os.path.dirname(os.path.abspath(__file__))
DIR_WEB = os.path.join(AQUI, "web")
DIR_SALIDA = os.path.join(AQUI, "salida", "web")
MAX_SESIONES = 100
MAX_MOTIVO = 500

# El motor escribe la memoria de aprendizaje compartida: una ejecución a la vez.
_motor_lock = threading.Lock()
_sesiones: "OrderedDict[str, Sistema]" = OrderedDict()
_sesiones_lock = threading.Lock()


class ErrorAPI(Exception):
    def __init__(self, estado: HTTPStatus, mensaje: str):
        super().__init__(mensaje)
        self.estado = estado
        self.mensaje = mensaje


# ----------------------------------------------------------------- Vistas
def configuracion() -> dict:
    red = red_ejemplo()
    return {
        "sistema": VERSION_MODELO,
        "periodos": [{"id": k, "nombre": NOMBRE_PERIODO[k], "dias": v} for k, v in PERIODOS.items()],
        "condiciones": [{"id": k, "nombre": v["desc"], "descripcion": v["descripcion"],
                         "parametros": parametros(resolver(k))} for k, v in CONDICIONES.items()
                        if "alias_de" not in v],
        "alimentadores_falla": list(ALIMENTADORES_FALLA),
        "grafo": {"nodos": NODOS, "aristas": [{"de": a, "a": b, "peso": p} for (a, b), p in ARISTAS.items()]},
        "red": _red(red),
        "tolerancia_riesgo": TOLERANCIA_RIESGO,
        "mape_reentrenar": MAPE_REENTRENAR,
        "cobertura_nominal_pct": COBERTURA_NOMINAL,
    }


def _red(red) -> dict:
    return {
        "subestacion": red.subestacion, "tension_mt_kv": red.tension_mt_kv, "tension_bt": red.tension_bt,
        "capacidad_kw": red.capacidad_convencional_kw,
        "cargas": [{"nombre": c.nombre, "tipo": c.tipo, "nivel": c.nivel, "alimentador": c.alimentador,
                    "potencia_kw": c.potencia_kw, "critica": c.critica} for c in red.cargas],
        "renovables": [{"nombre": r.nombre, "tipo": r.tipo, "capacidad_kw": r.capacidad_kw,
                        "alimentador": r.alimentador} for r in red.renovables],
        "tarifas": red.TARIFAS,
        "periodo_tarifario": [red.periodo_tarifario(h) for h in range(24)],
        "alimentadores": [{"clave": a.clave, "nombre": a.nombre, "nivel": a.nivel, "capacidad_kw": a.capacidad_kw,
                           "transformador_bt_kw": a.transformador_bt_kw or None} for a in red.alimentadores],
        "perdidas_mt_pct": red.perdidas_mt_pct,
        "perdidas_bt_pct": red.perdidas_bt_pct,
        "limite_inyeccion_kw": red.limite_inyeccion_kw,
    }


def _series_red(esc, base, paquete, red) -> dict:
    """Series de 24 h para la gráfica: subestación (SE) y cada alimentador, con la
    banda P10-P90 del pronóstico de demanda y de renovables."""
    ctx, lim = paquete["contexto"], paquete["limites"]
    d, db = esc.distribucion, (base or esc).distribucion
    alim_de = {c.nombre: red.alimentador(c.alimentador).clave for c in red.cargas}
    fuente_de = {r.nombre: red.alimentador(r.alimentador).clave for r in red.renovables}

    def suma(series: dict, de: dict, clave):
        return sum((v for n, v in series.items() if clave is None or de[n] == clave), np.zeros(24))
    out = {}
    for clave in [None] + d.claves:
        sel = d.series(clave)
        recorte = sel["recorte"]
        renovable = sel["renovable_disponible"]
        cap = lim.subestacion_kw if clave is None else lim.entregable_kw(clave)
        out[clave or "SE"] = {
            "nombre": red.subestacion if clave is None else red.alimentador(clave).nombre,
            "demanda_base": db.series(clave)["demanda"],
            "demanda_recomendada": sel["demanda"] - recorte,
            "renovable": renovable,
            "capacidad_kw": cap,
            "limite": renovable + cap,
            "demanda_p10": suma(ctx.cargas_p10, alim_de, clave),
            "demanda_p90": suma(ctx.cargas_p90, alim_de, clave),
            "renovable_p10": suma(ctx.renovables_p10, fuente_de, clave),
            "renovable_p90": suma(ctx.renovables_p90, fuente_de, clave),
            "recorte": recorte,
            "horas_recorte": [int(h) for h in np.where(recorte > 0.05)[0]],
        }
    return out


def vista(sid: str, s: Sistema) -> dict:
    pz = s.pizarra
    analisis = pz.leer("analisis") or {}
    paquete = pz.leer("escenarios")
    evaluacion = pz.leer("evaluacion")
    decision = pz.leer("decision")
    reporte = s.salida.reporte

    if s.en_espera:
        estado = "en_espera"
    elif reporte:
        estado = {"APROBADA": "aprobada", "ESCALADA AL OPERADOR": "escalada"}.get(reporte["estado"], "rechazada")
    else:
        estado = "procesando"

    escenarios = {e.id: e for e in paquete["lista"]} if paquete else {}
    cond = resolver(s.condicion, **s.parametros)
    evals = {e["id"]: e for e in evaluacion["escenarios"]} if evaluacion else {}

    esc_id = None
    if s.en_espera:
        esc_id = s.aprobacion.pendiente["escenario"]
    elif reporte and reporte["recomendacion"]:
        esc_id = reporte["recomendacion"]["escenario"]

    propuesta = None
    if esc_id and esc_id in escenarios:
        esc = escenarios[esc_id]
        base = escenarios.get("S0")
        por_alim = _series_red(esc, base, paquete, s.red)
        se = por_alim["SE"]
        propuesta = {
            "escenario": esc.id, "nombre": esc.nombre, "estrategia": esc.estrategia,
            "modo": decision["modo"], "justificacion": decision["justificacion"],
            "acciones": s.salida._acciones(esc), "metricas": esc.metricas,
            "metricas_base": base.metricas if base is not None else None,
            "evaluacion_iso": evals.get(esc.id),
            "robusto": esc.robusto,
            "hay_alternativa_robusta": decision.get("hay_alternativa_robusta"),
            "base_planificacion": esc.base_planificacion,
            "metricas_p90": esc.metricas_p90,
            "alimentadores": esc.metricas["alimentadores"],
            "recorte_por_carga": [{"carga": c, "kwh": round(kwh, 1), "kw_max": kw, "horas": hs}
                                  for c, (kwh, kw, hs) in s.salida.recorte_por_carga(esc).items()],
            "serie": {
                "demanda_base": base.demanda if base is not None else esc.demanda,
                "demanda_recomendada": esc.demanda,
                "renovable": esc.renovable,
                "capacidad_kw": esc.capacidad_kw,
                "horas_suspension": sorted({h for h, _, _ in esc.suspensiones}),
                "bandas": {k: se[k] for k in ("demanda_p10", "demanda_p90", "renovable_p10", "renovable_p90")},
                "por_alimentador": por_alim,
            },
        }

    return {
        "id": sid,
        "estado": estado,
        "periodo": {"id": s.periodo, "nombre": NOMBRE_PERIODO[s.periodo]},
        "condicion": {"id": s.condicion, "nombre": cond["desc"],
                      "descripcion": CONDICIONES[s.condicion]["descripcion"], "parametros": parametros(cond)},
        "dia_recomendado": s.fin.date().isoformat(),
        "coordinacion": {"recorrido": s.bus.recorrido, "costo": s.bus.costo_total,
                         "mensajes": s.bus.enviados},
        "consola": s.bus.consola,
        "analisis": {k: analisis.get(k) for k in (
            "dias", "consumo_total_kwh", "consumo_por_carga_kwh", "pico_kw", "fecha_pico", "horas_pico",
            "perfil_horario_kw", "variacion_estacional_kw", "deteccion", "generacion_renovable_kwh",
            "mape_pct", "experiencia_ejecuciones", "cobertura_bandas")} if analisis else None,
        "anomalias": (analisis.get("anomalias") or [])[:30],
        "propuesta": propuesta,
        "alternativas": [{
            "id": e.id, "nombre": e.nombre, "costo_mxn": e.metricas["costo_mxn"],
            "renovable_pct": e.metricas["renovable_pct"], "deficit_kwh": e.metricas["deficit_kwh"],
            "desplazado_kwh": e.metricas["desplazado_kwh"], "suspendido_kwh": e.metricas["suspendido_kwh"],
            "riesgo_max": evals[e.id]["riesgo_max"] if e.id in evals else None,
            "aceptable": evals[e.id]["aceptable"] if e.id in evals else None,
            "recorte_kwh": e.metricas["recorte_kwh"], "robusto": e.robusto,
            "deficit_p90_kwh": e.metricas["deficit_p90_kwh"],
            "utilizacion_max_pct": e.metricas["utilizacion_max_pct"],
        } for e in escenarios.values()],
        "trazabilidad": evaluacion["trazabilidad"] if evaluacion else None,
        "historial": s.aprobacion.historial,
        "motivo_escalamiento": decision["justificacion"] if decision and decision.get("escenario") is None else None,
        "terminado": s.terminado,
    }


# --------------------------------------------------------------- Acciones
def crear_sesion(datos: dict) -> tuple[str, Sistema]:
    periodo = datos.get("periodo", "1m")
    condicion = datos.get("condicion", "normal")
    falla = datos.get("falla", "ninguna")
    semilla = datos.get("semilla", 7)
    alimentador = datos.get("alimentador")
    crecimiento = datos.get("crecimiento_pct")
    if periodo not in PERIODOS:
        raise ErrorAPI(HTTPStatus.BAD_REQUEST, f"Elige un periodo válido: {', '.join(PERIODOS)}")
    if condicion not in CONDICIONES:
        raise ErrorAPI(HTTPStatus.BAD_REQUEST, f"Elige una condición válida: {', '.join(CONDICIONES)}")
    if falla not in FALLAS:
        raise ErrorAPI(HTTPStatus.BAD_REQUEST, f"Falla no válida: {', '.join(FALLAS)}")
    if not isinstance(semilla, int) or isinstance(semilla, bool) or not 0 <= semilla < 2**31:
        raise ErrorAPI(HTTPStatus.BAD_REQUEST, "La semilla debe ser un entero positivo")
    if alimentador is not None and alimentador not in ALIMENTADORES_FALLA:
        raise ErrorAPI(HTTPStatus.BAD_REQUEST, f"Alimentador no válido: {', '.join(ALIMENTADORES_FALLA)}")
    if crecimiento is not None and (not isinstance(crecimiento, (int, float)) or isinstance(crecimiento, bool)
                                    or not -50 <= crecimiento <= 200):
        raise ErrorAPI(HTTPStatus.BAD_REQUEST, "El crecimiento debe ser un número entre -50 y 200 (%)")

    sid = uuid.uuid4().hex[:10]
    with _motor_lock:
        s = crear_sistema(periodo=periodo, condicion=condicion, modo="web", falla=falla, semilla=semilla,
                          alimentador=alimentador, crecimiento_pct=crecimiento,
                          salida=os.path.join(DIR_SALIDA, sid),
                          ruta_memoria=os.path.join(DIR_SALIDA, "memoria_aprendizaje.json"),
                          verbose=False)
        s.iniciar()
        if s.terminado:
            s.guardar_bitacora()
    with _sesiones_lock:
        _sesiones[sid] = s
        while len(_sesiones) > MAX_SESIONES:
            _sesiones.popitem(last=False)
    return sid, s


def obtener(sid: str) -> Sistema:
    with _sesiones_lock:
        s = _sesiones.get(sid)
    if s is None:
        raise ErrorAPI(HTTPStatus.NOT_FOUND, "La sesión no existe o expiró. Inicia un análisis nuevo.")
    return s


def responder(sid: str, datos: dict) -> Sistema:
    s = obtener(sid)
    aprobado = datos.get("aprobado")
    motivo = datos.get("motivo", "") or ""
    if not isinstance(aprobado, bool):
        raise ErrorAPI(HTTPStatus.BAD_REQUEST, "Indica 'aprobado': true o false")
    if not isinstance(motivo, str) or len(motivo) > MAX_MOTIVO:
        raise ErrorAPI(HTTPStatus.BAD_REQUEST, f"El motivo debe ser texto de hasta {MAX_MOTIVO} caracteres")
    if not aprobado and not motivo.strip():
        raise ErrorAPI(HTTPStatus.BAD_REQUEST, "Escribe el motivo del rechazo para replantear la propuesta")
    with _motor_lock:
        if not s.en_espera:
            raise ErrorAPI(HTTPStatus.CONFLICT, "Esta sesión no tiene una propuesta en espera")
        s.responder(aprobado, motivo)
        if s.terminado:
            s.guardar_bitacora()
    return s


# --------------------------------------------------------------- HTTP
_RUTA_SESION = re.compile(r"^/api/sesiones/([0-9a-f]{10})(?:/(aprobacion|reporte|bitacora))?$")


class Manejador(BaseHTTPRequestHandler):
    server_version = "ElectriAgent/0.2"

    def log_message(self, fmt, *args):   # registro breve en consola
        if not getattr(self.server, "silencioso", False):
            super().log_message(fmt, *args)

    # -- utilidades
    def _json(self, estado: int, cuerpo, descarga: str | None = None):
        datos = json.dumps(cuerpo, ensure_ascii=False, default=a_json).encode("utf-8")
        self.send_response(estado)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-store")
        if descarga:
            self.send_header("Content-Disposition", f'attachment; filename="{descarga}"')
        self.end_headers()
        self.wfile.write(datos)

    def _leer_json(self) -> dict:
        largo = int(self.headers.get("Content-Length") or 0)
        if largo > 10_000:
            raise ErrorAPI(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Solicitud demasiado grande")
        if largo == 0:
            return {}
        try:
            datos = json.loads(self.rfile.read(largo).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ErrorAPI(HTTPStatus.BAD_REQUEST, "El cuerpo debe ser JSON válido")
        if not isinstance(datos, dict):
            raise ErrorAPI(HTTPStatus.BAD_REQUEST, "El cuerpo debe ser un objeto JSON")
        return datos

    def _despachar(self, metodo: str):
        ruta = urlparse(self.path).path
        try:
            if ruta.startswith("/api/"):
                return self._api(metodo, ruta)
            if metodo != "GET":
                raise ErrorAPI(HTTPStatus.METHOD_NOT_ALLOWED, "Método no permitido")
            return self._estatico(ruta)
        except ErrorAPI as e:
            self._json(e.estado, {"error": e.mensaje})
        except Exception as e:   # error inesperado del motor
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Error interno: {e}"})

    def do_GET(self):
        self._despachar("GET")

    def do_POST(self):
        self._despachar("POST")

    # -- API
    def _api(self, metodo: str, ruta: str):
        if ruta == "/api/config" and metodo == "GET":
            return self._json(HTTPStatus.OK, configuracion())
        if ruta == "/api/sesiones" and metodo == "POST":
            sid, s = crear_sesion(self._leer_json())
            return self._json(HTTPStatus.CREATED, vista(sid, s))
        m = _RUTA_SESION.match(ruta)
        if not m:
            raise ErrorAPI(HTTPStatus.NOT_FOUND, "Ruta de la API no encontrada")
        sid, sub = m.groups()
        if sub is None and metodo == "GET":
            return self._json(HTTPStatus.OK, vista(sid, obtener(sid)))
        if sub == "aprobacion" and metodo == "POST":
            s = responder(sid, self._leer_json())
            return self._json(HTTPStatus.OK, vista(sid, s))
        if sub == "reporte" and metodo == "GET":
            s = obtener(sid)
            if not s.terminado:
                raise ErrorAPI(HTTPStatus.CONFLICT, "El reporte estará listo cuando apruebes o rechaces la propuesta")
            return self._json(HTTPStatus.OK, s.salida.reporte, descarga=f"reporte_electriagent_{sid}.json")
        if sub == "bitacora" and metodo == "GET":
            s = obtener(sid)
            return self._json(HTTPStatus.OK, s.bitacora.eventos, descarga=f"bitacora_{sid}.json")
        raise ErrorAPI(HTTPStatus.METHOD_NOT_ALLOWED, "Método no permitido")

    # -- archivos de la app
    def _estatico(self, ruta: str):
        if ruta in ("", "/"):
            ruta = "/index.html"
        destino = os.path.realpath(os.path.join(DIR_WEB, ruta.lstrip("/")))
        if not destino.startswith(os.path.realpath(DIR_WEB) + os.sep) or not os.path.isfile(destino):
            raise ErrorAPI(HTTPStatus.NOT_FOUND, "Archivo no encontrado")
        tipo = mimetypes.guess_type(destino)[0] or "application/octet-stream"
        if destino.endswith(".webmanifest"):
            tipo = "application/manifest+json"
        with open(destino, "rb") as f:
            datos = f.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", tipo + ("; charset=utf-8" if tipo.startswith(("text/", "application/j")) else ""))
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(datos)


def crear_servidor(host: str = "127.0.0.1", puerto: int = 8000, silencioso: bool = False) -> ThreadingHTTPServer:
    os.makedirs(DIR_SALIDA, exist_ok=True)
    srv = ThreadingHTTPServer((host, puerto), Manejador)
    srv.silencioso = silencioso
    return srv


def main():
    ap = argparse.ArgumentParser(description="Servidor web de ElectriAgent")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 para abrirlo desde otros dispositivos")
    ap.add_argument("--puerto", type=int, default=8000)
    args = ap.parse_args()
    srv = crear_servidor(args.host, args.puerto)
    print(f"ElectriAgent en http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.puerto}")
    print("Ctrl+C para detener.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
