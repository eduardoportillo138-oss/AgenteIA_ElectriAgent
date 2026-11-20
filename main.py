"""
ElectriAgent - Prototipo del sistema multi-agente (arquitectura vertical).

Ejemplos:
    python main.py                                  # 1 mes, interactivo
    python main.py --periodo 1a --modo auto         # 1 año, aprobación automática
    python main.py --condicion ola_calor --modo auto
    python main.py --condicion falla --modo auto --mostrar
    python main.py --modo auto --rechazos 2         # demuestra el ciclo de rechazo
    python main.py --modo auto --falla aprendizaje  # demuestra el manejo de fallas

Requiere: numpy, matplotlib
Para la aplicación web / móvil:  python servidor.py
"""
import argparse
import os

import numpy as np

from agentes import CONDICIONES
from modelo_red import PERIODOS
from motor import FALLAS, crear_sistema


def graficar(reporte, escenarios, red, ruta, mostrar):
    import matplotlib
    if not mostrar:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rec = reporte["recomendacion"]
    lista = {e.id: e for e in escenarios["lista"]}
    base = lista.get("S0")
    elegido = lista.get(rec["escenario"]) if rec else None
    h = np.arange(24)

    fig, ax = plt.subplots(figsize=(11, 5.5))
    for hora in h:   # sombreado tarifario
        p = red.periodo_tarifario(hora)
        color = {"base": "#E1F5EE", "intermedia": "#FAEEDA", "punta": "#FCEBEB"}[p]
        ax.axvspan(hora - 0.5, hora + 0.5, color=color, lw=0)
    if base is not None:
        ax.plot(h, base.demanda, color="#5F5E5A", lw=2, label="Demanda pronosticada (sin cambios)")
    ax.fill_between(h, 0, base.renovable if base is not None else 0, color="#97C459", alpha=.6,
                    label="Generación renovable pronosticada")
    if elegido is not None and elegido is not base:
        ax.plot(h, elegido.demanda, color="#534AB7", lw=2.2, ls="--",
                label=f"Demanda con recomendación {elegido.id}")
        if elegido.suspensiones:
            hs = sorted({s[0] for s in elegido.suspensiones})
            ax.scatter(hs, elegido.demanda[hs], color="#A32D2D", zorder=5, label="Suspensión controlada")
    cap = escenarios["capacidad_kw"]
    renov = base.renovable if base is not None else 0
    ax.plot(h, cap + renov, color="#A32D2D", lw=1, ls=":", label="Límite de suministro (capacidad + renovable)")
    ax.set_xticks(h)
    ax.set_xlabel("Hora del día pronosticado (fondo: tarifa base / intermedia / punta)")
    ax.set_ylabel("kW")
    ax.set_title(f"ElectriAgent - {reporte['condicion_red']} - estado: {reporte['estado']}")
    ax.legend(loc="upper left", fontsize=8)
    ax.margins(x=0)
    fig.tight_layout()
    fig.savefig(ruta, dpi=140)
    if mostrar:
        fig.canvas.manager.set_window_title("ElectriAgent - Recomendación")
        plt.show()
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description="Prototipo ElectriAgent")
    ap.add_argument("--periodo", choices=PERIODOS, default="1m")
    ap.add_argument("--condicion", choices=CONDICIONES, default="normal")
    ap.add_argument("--modo", choices=["interactivo", "auto"], default="interactivo",
                    help="aprobación humana por consola o automática (demostración)")
    ap.add_argument("--rechazos", type=int, default=0, help="rechazos automáticos antes de aprobar")
    ap.add_argument("--falla", choices=FALLAS, default="ninguna",
                    help="simula una falla transitoria en un agente")
    ap.add_argument("--semilla", type=int, default=7)
    ap.add_argument("--salida", default="salida")
    ap.add_argument("--mostrar", action="store_true", help="abre la gráfica en una ventana")
    args = ap.parse_args()

    s = crear_sistema(periodo=args.periodo, condicion=args.condicion, modo=args.modo,
                      rechazos=args.rechazos, falla=args.falla, semilla=args.semilla,
                      salida=args.salida)
    red, bus, pizarra = s.red, s.bus, s.pizarra

    print("=" * 70)
    print(f"  ELECTRIAGENT | {red.subestacion} ({red.tension_mt_kv} kV MT / {red.tension_bt} BT)")
    print(f"  Día a recomendar: {s.fin:%Y-%m-%d} | Condición: {CONDICIONES[args.condicion]['desc']}")
    print("=" * 70)
    s.iniciar()

    rep = s.salida.reporte
    print("\n" + "=" * 70)
    print(f"  RESULTADO: {rep['estado']}")
    if rep["recomendacion"]:
        r = rep["recomendacion"]
        print(f"  Escenario {r['escenario']}: {r['nombre']}")
        for a in r["acciones"]:
            print(f"   > {a}")
        m = r["metricas"]
        print(f"  Costo {m['costo_mxn']:,.0f} MXN | Renovables {m['renovable_pct']} % | "
              f"Margen mínimo {m['margen_min_pct']} % | Déficit {m['deficit_kwh']} kWh")
    else:
        for j in rep["motivo_escalamiento"]:
            print(f"   > {j}")
    t = rep["trazabilidad_iso_5338"]
    if t and t["alerta_deriva"]:
        print(f"  ALERTA ISO/IEC 5338: {t['accion_monitoreo']}")
    print("-" * 70)
    print(f"  Coordinación: {bus.enviados} mensajes | recorrido {' '.join(bus.recorrido)}")
    print(f"  Costo de coordinación (pesos del grafo): {bus.costo_total}")
    print("=" * 70)

    s.guardar_bitacora()
    escenarios = pizarra.leer("escenarios")
    if escenarios:
        graficar(rep, escenarios, red, os.path.join(args.salida, "grafica_recomendacion.png"), args.mostrar)
    print(f"  Archivos generados en: {os.path.abspath(args.salida)}")


if __name__ == "__main__":
    main()
