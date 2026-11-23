/* ElectriAgent: interfaz web / móvil del sistema multi-agente.
   Habla con servidor.py. Todo el texto dinámico se inserta con textContent. */
"use strict";

const API = "/api";
const SVG = "http://www.w3.org/2000/svg";
const num = new Intl.NumberFormat("es-MX", { maximumFractionDigits: 0 });
const num1 = new Intl.NumberFormat("es-MX", { maximumFractionDigits: 1 });
const sinMovimiento = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const estado = { config: null, sesion: null, aristasPintadas: 0, vista: "SE" };

// ------------------------------------------------------------ utilidades
function h(tag, attrs = {}, ...hijos) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of hijos.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : String(c));
  return e;
}
const sinNulos = (lista) => lista.filter((x) => x != null && x !== false);
function s(tag, attrs = {}, ...hijos) {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v);
  for (const c of hijos.flat()) if (c != null) e.append(c.nodeType ? c : String(c));
  return e;
}
const $ = (id) => document.getElementById(id);
const hora = (n) => `${String(n).padStart(2, "0")}:00`;
const fecha = (iso) => new Date(iso).toLocaleString("es-MX", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const nivelDe = (v) => (v <= 6 ? "bajo" : v <= 12 ? "medio" : v <= 19 ? "alto" : "crítico");
const espera = (ms) => new Promise((r) => setTimeout(r, ms));
// [7,8,9,18] -> "07:00-10:00, 18:00-19:00"
function rangos(horas) {
  const hs = [...new Set(horas)].sort((a, b) => a - b), out = [];
  let ini = hs[0];
  hs.forEach((x, i) => { if (hs[i + 1] !== x + 1) { out.push(`${hora(ini)}-${hora(x + 1)}`); ini = hs[i + 1]; } });
  return out.join(", ");
}

// ------------------------------------------------------------ token de acceso (ELECTRIAGENT_TOKEN)
// Se guarda en sessionStorage: dura lo que la pestaña y no queda en el equipo.
const CLAVE_TOKEN = "electriagent.token";
function leerToken() {
  try { return sessionStorage.getItem(CLAVE_TOKEN) || ""; } catch { return ""; }
}
function guardarToken(t) {
  try { t ? sessionStorage.setItem(CLAVE_TOKEN, t) : sessionStorage.removeItem(CLAVE_TOKEN); } catch { /* sin almacenamiento */ }
}

class ErrorAcceso extends Error {}

// Nombre de quien firma las decisiones: el único dato personal que guarda la web (localStorage)
const CLAVE_OPERADOR = "electriagent.operador";
function leerOperador() {
  try { return localStorage.getItem(CLAVE_OPERADOR) || ""; } catch { return ""; }
}
function guardarOperador(n) {
  try { n ? localStorage.setItem(CLAVE_OPERADOR, n) : localStorage.removeItem(CLAVE_OPERADOR); } catch { /* sin almacenamiento */ }
}

function cabeceras(extra = {}) {
  const t = leerToken();
  return { ...extra, ...(t ? { Authorization: `Bearer ${t}` } : {}) };
}

async function llamar(ruta, opciones = {}) {
  try {
    return await fetch(API + ruta, { ...opciones, headers: cabeceras(opciones.headers) });
  } catch {
    throw new Error("No hay conexión con el servidor de ElectriAgent. Revisa que servidor.py esté en ejecución.");
  }
}

async function pedir(ruta, opciones = {}) {
  const r = await llamar(ruta, { ...opciones, headers: { "Content-Type": "application/json" } });
  const datos = await r.json().catch(() => ({}));
  if (r.status === 401) {
    pedirToken(datos.error || "El servidor pide un token de acceso.");
    throw new ErrorAcceso(datos.error || "Falta el token de acceso o es incorrecto.");
  }
  if (!r.ok) throw new Error(datos.error || `El servidor respondió ${r.status}`);
  return datos;
}

/** Descarga un JSON de la API con la cabecera de autorización (un <a href> no puede llevarla). */
async function descargar(ruta, nombre, boton) {
  boton.disabled = true;
  try {
    const r = await llamar(ruta);
    if (r.status === 401) {
      const d = await r.json().catch(() => ({}));
      pedirToken(d.error || "El servidor pide un token de acceso.");
      return;
    }
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.error || `El servidor respondió ${r.status}`);
    }
    const url = URL.createObjectURL(await r.blob());
    const a = h("a", { href: url, download: nombre });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) {
    $("circuito-estado").textContent = e.message;
  } finally {
    boton.disabled = false;
  }
}

/** Muestra el formulario del token con el mensaje del servidor (solo tras un 401). */
function pedirToken(mensaje) {
  const caja = $("form-acceso");
  caja.hidden = false;
  $("acceso-error").textContent = leerToken() ? `${mensaje} Revisa el token e inténtalo de nuevo.` : mensaje;
  $("token").value = "";
  $("token").focus();
}

function prepararAcceso() {
  $("form-acceso").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    guardarToken($("token").value.trim());
    $("acceso-error").textContent = "";
    if (!estado.config) await iniciar();
    else $("form-acceso").hidden = true;
  });
}

// ------------------------------------------------------------ grafo A-H
// Dos disposiciones del mismo grafo: horizontal (como en el documento) y vertical para celulares.
const DISPOSICION = {
  horizontal: {
    caja: "0 0 940 690",
    pos: { A: [70, 140], B: [300, 140], C: [540, 140], F: [860, 140], D: [370, 330], E: [740, 330], G: [540, 490], H: [540, 630] },
    etiqueta: { // [texto, dx, dy, ancla]
      A: ["Usuario (inicio)", 0, -48, "middle"], B: ["Captura IoT", 0, -48, "middle"],
      C: ["Agente de meta", 0, -48, "middle"], F: ["Evaluación ISO", 0, -48, "middle"],
      D: ["Aprendizaje", 0, 58, "middle"], E: ["Deliberativo", 0, 58, "middle"],
      G: ["Aprobación humana", 46, 6, "start"], H: ["Recomendación final", 46, 6, "start"] },
    curva: { "C-D": 45, "D-C": 45, "C-G": 38, "G-C": 38 },
  },
  vertical: {
    caja: "0 0 420 760",
    pos: { A: [60, 70], B: [210, 70], C: [210, 240], F: [360, 240], D: [70, 400], E: [350, 400], G: [210, 560], H: [210, 700] },
    etiqueta: {
      A: ["Usuario", 0, 56, "middle"], B: ["Captura IoT", 42, 6, "start"],
      C: ["Agente de meta", -42, 6, "end"], F: ["Evaluación ISO", 0, -44, "middle"],
      D: ["Aprendizaje", 0, 56, "middle"], E: ["Deliberativo", 0, 56, "middle"],
      G: ["Aprobación humana", 42, 6, "start"], H: ["Recomendación final", 42, 6, "start"] },
    curva: { "C-D": 34, "D-C": 34, "C-G": 30, "G-C": 30 },
  },
};
const ROL = { A: "io", B: "io", C: "supervisor", D: "trabajador", E: "trabajador", F: "trabajador", G: "io", H: "io" };
const consultaMovil = window.matchMedia("(max-width: 640px)");
let LAYOUT = DISPOSICION.horizontal, POS = LAYOUT.pos, ETIQUETA = LAYOUT.etiqueta, CURVA = LAYOUT.curva;
const R = 30;

function geometria(a, b) {
  const [x1, y1] = POS[a], [x2, y2] = POS[b];
  const dx = x2 - x1, dy = y2 - y1, L = Math.hypot(dx, dy);
  const nx = -dy / L, ny = dx / L, k = CURVA[`${a}-${b}`] || 0;
  const cx = (x1 + x2) / 2 + nx * k, cy = (y1 + y2) / 2 + ny * k;
  const ini = (px, py, d) => { const ux = cx - px, uy = cy - py, l = Math.hypot(ux, uy) || 1; return [px + ux / l * d, py + uy / l * d]; };
  const [sx, sy] = ini(x1, y1, R + 2), [ex, ey] = ini(x2, y2, R + 9);
  return { d: `M${sx},${sy} Q${cx},${cy} ${ex},${ey}`, lx: 0.25 * sx + 0.5 * cx + 0.25 * ex, ly: 0.25 * sy + 0.5 * cy + 0.25 * ey };
}

function construirGrafo() {
  LAYOUT = consultaMovil.matches ? DISPOSICION.vertical : DISPOSICION.horizontal;
  ({ pos: POS, etiqueta: ETIQUETA, curva: CURVA } = LAYOUT);
  const svg = $("grafo");
  svg.setAttribute("viewBox", LAYOUT.caja);
  svg.classList.toggle("vertical", consultaMovil.matches);
  svg.querySelectorAll("defs, g").forEach((n) => n.remove());
  const defs = s("defs", {},
    ["flecha", "flecha-activa"].map((id) => s("marker", { id, viewBox: "0 0 10 10", refX: 3, refY: 5, markerWidth: 5, markerHeight: 5, orient: "auto-start-reverse" },
      s("path", { d: "M0,0 L10,5 L0,10 z", class: id === "flecha" ? "punta" : "punta-activa" }))));
  svg.append(defs);
  for (const { de, a, peso } of estado.config.grafo.aristas) {
    const g = geometria(de, a);
    const ancho = String(peso).length * 10 + 12;
    svg.append(s("g", { class: `arista${de === "G" && a === "C" ? " retorno" : ""}`, "data-arista": `${de}->${a}` },
      s("path", { d: g.d, "marker-end": "url(#flecha)" }),
      s("rect", { class: "etiqueta-fondo", x: g.lx - ancho / 2, y: g.ly - 12, width: ancho, height: 24, rx: 4 }),
      s("text", { x: g.lx, y: g.ly + 6, "text-anchor": "middle" }, String(peso))));
  }
  for (const [id, [x, y]] of Object.entries(POS)) {
    const [txt, dx, dy, ancla] = ETIQUETA[id];
    svg.append(s("g", { class: `nodo ${ROL[id]} inactivo`, "data-nodo": id },
      s("circle", { class: "halo", cx: x, cy: y, r: R }),
      s("circle", { cx: x, cy: y, r: R }),
      s("text", { class: "letra", x, y }, id),
      s("text", { class: "nombre", x: x + dx, y: y + dy, "text-anchor": ancla }, txt)));
  }
}

function marcarArista(enlace, activa) {
  const g = document.querySelector(`[data-arista="${enlace}"]`);
  if (!g) return;
  g.classList.toggle("activa", activa);
  g.querySelector("path").setAttribute("marker-end", activa ? "url(#flecha-activa)" : "url(#flecha)");
}
function marcarNodo(id, clase) {
  const g = document.querySelector(`[data-nodo="${id}"]`);
  g.classList.remove("inactivo", "espera", "fin");
  if (clase) g.classList.add(clase);
}

function reiniciarGrafo() {
  document.querySelectorAll("[data-arista]").forEach((g) => { g.classList.remove("activa", "fluye"); marcarArista(g.dataset.arista, false); });
  document.querySelectorAll("[data-nodo]").forEach((g) => { g.classList.remove("espera", "fin"); g.classList.add("inactivo"); });
  estado.aristasPintadas = 0;
}

async function pintarRecorrido(ses) {
  const rec = ses.coordinacion.recorrido;
  document.querySelectorAll(".arista.fluye").forEach((g) => g.classList.remove("fluye"));
  marcarNodo("A", null);
  for (let i = estado.aristasPintadas; i < rec.length; i++) {
    const [de, a] = rec[i].split("->");
    marcarArista(rec[i], true);
    marcarNodo(de, null);
    marcarNodo(a, null);
    if (!sinMovimiento) await espera(110);
  }
  estado.aristasPintadas = rec.length;
  if (ses.estado === "en_espera") {
    marcarNodo("G", "espera");
    const ultima = document.querySelector(`[data-arista="${rec[rec.length - 1]}"]`);
    if (ultima) ultima.classList.add("fluye");
  }
  if (ses.estado === "aprobada") marcarNodo("H", "fin");

  const st = $("circuito-estado");
  st.classList.toggle("espera", ses.estado === "en_espera");
  st.textContent = {
    en_espera: "Falta tu decisión: la propuesta está detenida en Aprobación humana (G).",
    aprobada: "Aprobaste la recomendación. El reporte está listo.",
    escalada: "El agente de meta escaló el caso: necesita tu criterio como operador.",
    rechazada: "Rechazaste la recomendación.",
    procesando: "Los agentes están trabajando…",
  }[ses.estado];
  $("circuito-pie").textContent =
    `${ses.coordinacion.mensajes} mensajes entre agentes. Costo de coordinación: ${ses.coordinacion.costo} (suma de los pesos recorridos).`;
}

// ------------------------------------------------------------ formulario
function construirFormulario() {
  const { periodos, condiciones, red } = estado.config;
  $("opciones-periodo").replaceChildren(...periodos.map((p) =>
    h("label", { class: "chip" },
      h("input", { type: "radio", name: "periodo", value: p.id, checked: p.id === "1m" }),
      h("span", {}, p.nombre))));
  $("condicion").replaceChildren(...condiciones.map((c) => h("option", { value: c.id }, c.nombre)));
  const nombreAlim = Object.fromEntries(red.alimentadores.map((a) => [a.clave, `${a.clave} · ${a.nivel === "MT" ? "media tensión" : "baja tensión"}`]));
  $("alimentador").replaceChildren(...estado.config.alimentadores_falla.map((k) => h("option", { value: k }, nombreAlim[k] || k)));
  $("condicion").addEventListener("change", describirCondicion);
  describirCondicion();
  $("red-resumen").textContent = `${red.subestacion}, ${red.tension_mt_kv} kV a ${red.tension_bt}`;
  $("form-analisis").addEventListener("submit", analizar);
}

function describirCondicion() {
  const id = $("condicion").value;
  const c = estado.config.condiciones.find((x) => x.id === id);
  $("condicion-desc").textContent = c ? c.descripcion : "";
  $("campo-alimentador").hidden = id !== "falla_alimentador";
  $("campo-crecimiento").hidden = id !== "crecimiento_demanda";
}

async function analizar(ev) {
  ev.preventDefault();
  const btn = $("btn-analizar");
  const datos = new FormData(ev.target);
  btn.disabled = true;
  btn.textContent = "Analizando…";
  reiniciarGrafo();
  $("circuito-estado").classList.remove("espera");
  $("circuito-estado").textContent = "Los agentes están trabajando…";
  $("circuito-pie").textContent = "";
  const cuerpo = { periodo: datos.get("periodo"), condicion: datos.get("condicion"), falla: datos.get("falla") || "ninguna" };
  if (cuerpo.condicion === "falla_alimentador") cuerpo.alimentador = datos.get("alimentador");
  if (cuerpo.condicion === "crecimiento_demanda") cuerpo.crecimiento_pct = Number(datos.get("crecimiento") || 15);
  try {
    const ses = await pedir("/sesiones", { method: "POST", body: JSON.stringify(cuerpo) });
    await mostrar(ses);
    $("decision").focus({ preventScroll: true });
  } catch (e) {
    $("circuito-estado").textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Analizar consumo";
  }
}

async function mostrar(ses) {
  estado.sesion = ses;
  $("resultados").hidden = false;
  pintarDecision(ses);
  pintarGrafica(ses);
  pintarConsumo(ses);
  pintarISO(ses);
  pintarAlternativas(ses);
  pintarBitacora(ses);
  await pintarRecorrido(ses);
}

// ------------------------------------------------------------ decisión
function lineaMetrica(etiqueta, valor, delta, mejorSiMenor) {
  let d = null;
  if (delta != null && Math.abs(delta) >= 0.05) {
    const mejor = mejorSiMenor ? delta < 0 : delta > 0;
    d = h("span", { class: `delta ${mejor ? "mejor" : "peor"}` },
      `${delta > 0 ? "+" : "−"}${num1.format(Math.abs(delta))} frente a hoy`);
  } else if (delta != null) {
    d = h("span", { class: "delta" }, "igual que hoy");
  }
  return h("div", {}, h("dt", {}, etiqueta), h("dd", {}, valor, d));
}

function pintarDecision(ses) {
  const caja = $("decision");
  caja.className = `decision ${ses.estado === "en_espera" ? "pendiente" : ses.estado}`;
  const p = ses.propuesta;
  const hijos = [];

  const titulos = {
    en_espera: "Falta tu decisión",
    aprobada: "Recomendación aprobada",
    escalada: "Caso escalado al operador",
    rechazada: "Recomendación rechazada",
  };
  hijos.push(h("p", { class: "decision-estado" }, titulos[ses.estado] || ""));

  if (p) {
    hijos.push(h("h2", { id: "t-decision" }, p.nombre, " ", h("span", { class: "escenario-id" }, `(${p.escenario})`)));
    hijos.push(h("div", { class: "insignias" },
      p.robusto
        ? h("span", { class: "insignia ok", title: "Sin déficit aunque la demanda llegue a P90 y las renovables bajen a P10" }, "Robusta ante P90")
        : h("span", { class: "insignia riesgo", title: "Con demanda P90 y renovables P10 habría déficit" },
          `No robusta: ${num.format(p.metricas.deficit_p90_kwh)} kWh de déficit en P90`),
      p.metricas.recorte_kwh > 0 ? h("span", { class: "insignia" }, `Recorte planificado con la banda ${p.base_planificacion}`) : null));
    if (p.modo === "contingencia") {
      hijos.push(h("p", { class: "error" }, "La demanda prevista supera la capacidad disponible. Esta propuesta recorta parcialmente cargas no críticas, solo los kW necesarios y por prioridad; el hospital conserva el 100 % de su suministro."));
    }
    // Política de recorte: mínimo por omisión; el plan robusto P90 se ofrece como alternativa
    if (p.recorte_adicional_robustez_kwh != null) {
      hijos.push(h("p", { class: "robustez" }, p.alternativa_robusta
        ? `Alternativa robusta ${p.alternativa_robusta}: recorta ${num.format(p.recorte_adicional_robustez_kwh)} kWh adicionales y evita ${num.format(p.metricas.deficit_p90_kwh)} kWh de déficit si la demanda llega a P90. Para recibirla, rechaza esta propuesta pidiendo más seguridad.`
        : `Plan robusto: recorta ${num.format(p.recorte_adicional_robustez_kwh)} kWh más que el recorte mínimo para no tener déficit si la demanda llega a P90.`));
    }
    const lista = h("ul", { class: "acciones" },
      p.acciones.map((a) => a.startsWith("  ·")
        ? h("li", { class: "detalle" }, a.replace(/^\s*·\s*/, ""))
        : h("li", {}, a)));
    hijos.push(lista);

    const m = p.metricas, b = p.metricas_base;
    const dif = (k) => (b && p.escenario !== "S0" ? m[k] - b[k] : null);
    const metr = h("dl", { class: "metricas" },
      lineaMetrica("Costo del día", `${num.format(m.costo_mxn)} MXN`, dif("costo_mxn"), true),
      lineaMetrica("Cubierto con renovables", `${num1.format(m.renovable_pct)} %`, dif("renovable_pct"), false),
      lineaMetrica("Margen mínimo de capacidad", `${num1.format(m.margen_min_pct)} %`, dif("margen_min_pct"), false),
      lineaMetrica("Consumo que cambia de horario", `${num.format(m.desplazado_kwh)} kWh`, null),
      m.recorte_kwh > 0 ? lineaMetrica("Recorte parcial", `${num.format(m.recorte_kwh)} kWh`, null) : null,
      lineaMetrica("Pérdidas técnicas", `${num.format(m.perdidas_kwh)} kWh`, null),
      p.evaluacion_iso ? lineaMetrica("Riesgo máximo", `${p.evaluacion_iso.riesgo_max} (${p.evaluacion_iso.categoria_max})`, null) : null);
    hijos.push(metr);

    if (p.recorte_por_carga.length) {
      const maxR = Math.max(...p.recorte_por_carga.map((r) => r.kwh));
      hijos.push(h("h3", {}, "Recorte parcial por carga"));
      hijos.push(h("ul", { class: "barras recortes" }, p.recorte_por_carga.map((r) =>
        h("li", {},
          h("div", { class: "rotulo" }, h("span", {}, r.carga), h("b", {}, `${num.format(r.kwh)} kWh`)),
          h("div", { class: "sub" }, `Hasta ${num.format(r.kw_max)} kW, ${r.horas.length} h (${rangos(r.horas)})`),
          h("div", { class: "barra-recorte" }, h("i", { style: `width:${(r.kwh / maxR) * 100}%` }))))));
    }

    hijos.push(h("h3", {}, "Distribución por alimentador"));
    hijos.push(h("div", { class: "alimentadores" }, Object.entries(p.alimentadores).map(([k, a]) =>
      h("div", {},
        h("b", {}, `${k}${a.carga_critica ? " · con carga crítica" : ""}`),
        h("div", { class: "uso", title: `Uso máximo ${num1.format(a.utilizacion_max_pct)} %` },
          h("i", { class: a.utilizacion_max_pct >= 90 ? "alto" : "", style: `width:${Math.min(100, a.utilizacion_max_pct)}%` })),
        `Uso máx. ${num1.format(a.utilizacion_max_pct)} % · margen ${num1.format(a.margen_min_pct)} %`,
        h("div", { class: "sub" }, `Renovable ${num.format(a.renovable_usada_kwh)} kWh · convencional ${num.format(a.convencional_kwh)} kWh` +
          (a.recorte_kwh ? ` · recorte ${num.format(a.recorte_kwh)} kWh` : ""))))));

    hijos.push(h("h3", {}, "Por qué esta recomendación"));
    hijos.push(h("ol", { class: "reglas" }, p.justificacion.map((j) => h("li", {}, j.replace(/^Regla \d: /, "")))));  } else if (ses.motivo_escalamiento) {
    hijos.push(h("h2", { id: "t-decision" }, "No hay una propuesta que puedas aprobar"));
    hijos.push(h("ul", { class: "acciones" }, ses.motivo_escalamiento.map((t) => h("li", {}, t))));
  }

  if (ses.historial.length) {
    hijos.push(h("h3", {}, "Tus decisiones en esta sesión"));
    hijos.push(h("ul", { class: "historial" }, ses.historial.map((x) =>
      h("li", {}, `${x.aprobado ? "Aprobada" : "Rechazada"} ${x.escenario}${x.operador ? ` por ${x.operador}` : " (sin firma)"}${x.motivo ? `: “${x.motivo}”` : ""}`))));
  }

  if (ses.estado === "en_espera") {
    const error = h("p", { class: "error", role: "alert" });
    const motivo = h("textarea", { id: "motivo", maxlength: 500, placeholder: "Por ejemplo: no quiero mover el bombeo de agua en la madrugada" });
    const rechazo = h("div", { class: "rechazo", hidden: true },
      h("label", { for: "motivo" }, "¿Qué no te convence? El agente de meta buscará una alternativa menos invasiva."),
      motivo,
      h("div", { class: "botonera" },
        h("button", { class: "boton boton-peligro", type: "button", onclick: () => decidir(false, motivo.value, error) }, "Enviar rechazo"),
        h("button", { class: "boton boton-secundario", type: "button", onclick: () => { rechazo.hidden = true; botones.hidden = false; } }, "Cancelar")));
    const aprobar = h("button", { class: "boton boton-aprobar", type: "button", onclick: () => decidir(true, "", error) }, "Aprobar recomendación");
    const rechazar = h("button", { class: "boton boton-secundario", type: "button", onclick: () => { botones.hidden = true; rechazo.hidden = false; motivo.focus(); } }, "Rechazar y pedir alternativa");
    const botones = h("div", { class: "botonera" }, aprobar, rechazar);
    // Firma: sin nombre no se puede decidir (trazabilidad ISO/IEC 5338)
    const aviso = h("p", { class: "firma-aviso", id: "firma-aviso" });
    const nombre = h("input", { id: "operador", type: "text", maxlength: 80, autocomplete: "name",
      value: leerOperador(), placeholder: "Ej.: Ing. Laura Méndez", "aria-describedby": "firma-aviso" });
    const actualizarFirma = () => {
      const n = nombre.value.trim();
      guardarOperador(n);
      const falta = !n;
      [aprobar, rechazar].forEach((b) => (b.disabled = falta));
      document.querySelectorAll(".rechazo .boton-peligro").forEach((b) => (b.disabled = falta));
      aviso.textContent = falta ? "Escribe tu nombre para firmar la decisión: queda en la bitácora con ella." : `Firmarás como ${n}.`;
    };
    nombre.addEventListener("input", actualizarFirma);
    const firma = h("div", { class: "firma" }, h("label", { class: "campo", for: "operador" }, h("span", {}, "Nombre de quien decide"), nombre), aviso);
    hijos.push(firma, botones, rechazo, error);
    queueMicrotask(actualizarFirma);
  } else if (ses.terminado) {
    hijos.push(h("div", { class: "botonera" },
      h("button", { class: "boton boton-principal", type: "button",
        onclick: (ev) => descargar(`/sesiones/${ses.id}/reporte`, `reporte_electriagent_${ses.id}.json`, ev.currentTarget) }, "Descargar reporte"),
      h("button", { class: "boton boton-secundario", type: "button", onclick: () => window.scrollTo({ top: 0, behavior: sinMovimiento ? "auto" : "smooth" }) }, "Analizar otro periodo")));
  }
  caja.replaceChildren(...hijos);
}

async function decidir(aprobado, motivo, error) {
  error.textContent = "";
  const operador = leerOperador();
  if (!operador) {
    error.textContent = "Escribe tu nombre para firmar la decisión.";
    return;
  }
  if (!aprobado && !motivo.trim()) {
    error.textContent = "Escribe qué no te convence para que el sistema pueda replantear la propuesta.";
    return;
  }
  document.querySelectorAll("#decision button").forEach((b) => (b.disabled = true));
  try {
    const ses = await pedir(`/sesiones/${estado.sesion.id}/aprobacion`, { method: "POST", body: JSON.stringify({ aprobado, motivo, operador }) });
    await mostrar(ses);
    $("decision").focus();
  } catch (e) {
    error.textContent = e.message;
    document.querySelectorAll("#decision button").forEach((b) => (b.disabled = false));
  }
}

// ------------------------------------------------------------ gráfica 24 h
function pintarGrafica(ses) {
  const cont = $("grafica");
  const p = ses.propuesta;
  $("grafica-sub").textContent = `Pronóstico para el ${new Date(ses.dia_recomendado + "T12:00").toLocaleDateString("es-MX", { weekday: "long", day: "numeric", month: "long" })}. ${ses.condicion.nombre}.`;
  if (!p) {
    cont.replaceChildren(h("p", { class: "sub" }, "Sin propuesta que graficar."));
    $("grafica-leyenda").replaceChildren(); $("grafica-vista").replaceChildren(); $("grafica-resumen").textContent = "";
    return;
  }

  // Selector: subestación completa o un alimentador
  const vistas = p.serie.por_alimentador;
  if (!vistas[estado.vista]) estado.vista = "SE";
  $("grafica-vista").replaceChildren(...Object.keys(vistas).map((k) =>
    h("button", { type: "button", "aria-pressed": String(k === estado.vista),
      onclick: () => { estado.vista = k; pintarGrafica(estado.sesion); } },
      k === "SE" ? "Subestación" : k)));
  const se = vistas[estado.vista];
  const resumenAlim = estado.vista === "SE" ? null : p.alimentadores[estado.vista];
  $("grafica-resumen").textContent = estado.vista === "SE"
    ? `${se.nombre}: capacidad convencional ${num.format(se.capacidad_kw)} kW.`
    : `${se.nombre}: puede entregar ${num.format(se.capacidad_kw)} kW a sus cargas (cabecera ${num.format(resumenAlim.capacidad_kw)} kW` +
      (resumenAlim.capacidad_trafo_bt_kw ? `, transformadores BT ${num.format(resumenAlim.capacidad_trafo_bt_kw)} kW` : "") +
      `). Uso máximo ${num1.format(resumenAlim.utilizacion_max_pct)} %.`;

  const W = 760, H = 330, ml = 58, mr = 12, mt = 30, mb = 36;
  const base = se.demanda_base, rec = se.demanda_recomendada, ren = se.renovable;
  const p10 = se.demanda_p10, p90 = se.demanda_p90;
  const limite = se.limite;
  const horasRecorte = se.horas_recorte;
  const maxY = Math.max(...base, ...rec, ...limite, ...p90) * 1.06;
  const x = (i) => ml + (i / 23) * (W - ml - mr);
  const y = (v) => mt + (1 - v / maxY) * (H - mt - mb);
  const linea = (arr) => arr.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const paso = (W - ml - mr) / 23;

  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Demanda pronosticada, demanda con la recomendación, generación renovable y límite de suministro por hora" });
  const tarifas = estado.config.red.periodo_tarifario;
  tarifas.forEach((t, i) => {
    if (t === "intermedia") return;
    svg.append(s("rect", { x: x(i) - paso / 2, y: mt, width: paso, height: H - mt - mb,
      fill: t === "punta" ? "rgba(238,132,103,.13)" : "rgba(140,203,94,.09)" }));
  });
  const eje = s("g", { class: "eje" });
  for (let k = 0; k <= 4; k++) {
    const v = (maxY / 4) * k;
    eje.append(s("line", { x1: ml, x2: W - mr, y1: y(v), y2: y(v), "stroke-opacity": k ? 0.35 : 1 }),
      s("text", { x: ml - 8, y: y(v) + 4, "text-anchor": "end" }, `${num.format(v)}`));
  }
  for (let i = 0; i < 24; i += 3) eje.append(s("text", { x: x(i), y: H - 12, "text-anchor": "middle" }, hora(i)));
  eje.append(s("text", { x: ml - 8, y: 14, "text-anchor": "end" }, "kW"));
  svg.append(eje);
  svg.append(s("path", { d: `${linea(ren)} L${x(23)},${y(0)} L${x(0)},${y(0)} Z`, fill: "rgba(140,203,94,.38)" }));
  // Banda P10-P90 del pronóstico de demanda
  const banda = `${linea(p90)} ${p10.map((v, i) => `L${x(23 - i).toFixed(1)},${y(p10[23 - i]).toFixed(1)}`).join(" ")} Z`;
  svg.append(s("path", { d: banda, fill: "rgba(201,214,210,.16)", stroke: "rgba(201,214,210,.35)", "stroke-width": 0.8 }));
  svg.append(s("path", { d: linea(limite), fill: "none", stroke: "#EE8467", "stroke-width": 1.6, "stroke-dasharray": "2 5" }));
  svg.append(s("path", { d: linea(base), fill: "none", stroke: "#C9D6D2", "stroke-width": 2, "stroke-dasharray": p.escenario === "S0" ? null : "6 5" }));
  if (p.escenario !== "S0") svg.append(s("path", { d: linea(rec), fill: "none", stroke: "#35D6F0", "stroke-width": 2.8 }));
  for (const hs of horasRecorte) {
    const t = s("circle", { cx: x(hs), cy: y(rec[hs]), r: 5, fill: "#EE8467" });
    t.append(s("title", {}, `${hora(hs)}: recorte de ${num.format(se.recorte[hs])} kW`));
    svg.append(t);
  }
  cont.replaceChildren(svg);

  const item = (estilo, texto) => h("li", {}, h("i", { style: estilo }), texto);
  $("grafica-leyenda").replaceChildren(...sinNulos([
    item("background:#C9D6D2", "Demanda sin cambios"),
    item("background:rgba(201,214,210,.3);border:1px solid rgba(201,214,210,.5)", "Banda P10–P90 de la demanda"),
    p.escenario !== "S0" ? item("background:#35D6F0", "Demanda con la recomendación") : null,
    item("background:rgba(140,203,94,.6)", estado.vista === "SE" ? "Generación renovable" : "Renovable de este alimentador"),
    item("background:#EE8467;height:2px", "Límite de suministro"),
    horasRecorte.length ? item("background:#EE8467;border-radius:50%;width:.7rem", "Recorte parcial") : null,
    item("background:rgba(140,203,94,.25)", "Tarifa base"),
    item("background:rgba(238,132,103,.3)", "Tarifa punta")]));
}

// ------------------------------------------------------------ consumo
function pintarConsumo(ses) {
  const a = ses.analisis;
  if (!a) return;
  const cifra = (dt, dd, nota) => h("div", {}, h("dt", {}, dt), h("dd", {}, dd, nota ? h("small", {}, nota) : null));
  $("cifras").replaceChildren(
    cifra("Consumo total", `${num.format(a.consumo_total_kwh)} kWh`, ses.periodo.nombre),
    cifra("Demanda máxima", `${num.format(a.pico_kw)} kW`, fecha(a.fecha_pico)),
    cifra("Horas pico habituales", a.horas_pico.map(hora).join(", ")),
    cifra("Generación renovable", `${num.format(a.generacion_renovable_kwh)} kWh`),
    cifra("Error de pronóstico (MAPE)", a.mape_pct == null ? "sin datos" : `${num1.format(a.mape_pct)} %`,
      a.mape_pct == null ? "Se necesitan al menos 2 días" : null),
    cifra("Análisis aprendidos", num.format(a.experiencia_ejecuciones), "Memoria del agente de aprendizaje"));

  const criticas = new Set(estado.config.red.cargas.filter((c) => c.critica).map((c) => c.nombre));
  const entradas = Object.entries(a.consumo_por_carga_kwh).sort((x, y) => y[1] - x[1]);
  const max = entradas[0][1];
  $("por-carga").replaceChildren(...entradas.map(([n, v]) =>
    h("li", { class: criticas.has(n) ? "critica" : "" },
      h("div", { class: "rotulo" }, h("span", {}, n, criticas.has(n) ? " (crítica)" : ""), h("b", {}, `${num.format(v)} kWh`)),
      h("div", { class: "pista" }, h("div", { class: "relleno", style: `width:${(v / max) * 100}%` })))));

  const d = a.deteccion;
  $("anomalias-sub").textContent = ses.anomalias.length
    ? `El agente de aprendizaje marcó ${d.detectadas} lecturas; coinciden ${d.aciertos} de las ${d.inyectadas} anomalías simuladas. Se limpian antes de pronosticar.`
    : "No se encontraron lecturas fuera de patrón en este periodo.";
  $("anomalias").replaceChildren(...ses.anomalias.map((x) =>
    h("li", {},
      h("span", {}, x.carga, h("span", { class: "cuando" }, ` ${fecha(x.fecha)}`)),
      h("span", { class: "valor" }, `${num.format(x.kw)} kW`, h("span", { class: "cuando" }, ` / esperado ${num.format(x.esperado_kw)}`)))));
}

// ------------------------------------------------------------ ISO
function pintarISO(ses) {
  const ev = ses.propuesta && ses.propuesta.evaluacion_iso;
  const cuerpo = document.querySelector("#tabla-riesgos tbody");
  cuerpo.replaceChildren(...(ev ? ev.riesgos : []).map((r) =>
    h("tr", {},
      h("td", {}, r.riesgo, h("div", { class: "sub" }, r.tratamiento)),
      h("td", { class: "num" }, r.prob), h("td", { class: "num" }, r.impacto),
      h("td", {}, h("span", { class: `nivel ${r.categoria}` }, `${r.nivel} ${r.categoria}`)))));
  $("impactos").replaceChildren(...(ev ? ev.impactos : []).map((i) =>
    h("li", {}, h("b", {}, i.grupo, h("span", { class: `efecto ${i.efecto}` }, i.efecto)), i.detalle)));
  const t = ses.trazabilidad;
  const p = $("trazabilidad");
  p.classList.toggle("alerta", !!(t && (t.alerta_deriva || t.alerta_calibracion_bandas)));
  const cobertura = t && t.cobertura_bandas_pct != null
    ? ` Cobertura de las bandas P10–P90: ${num1.format(t.cobertura_bandas_pct)} % de ${t.lecturas_validacion_bandas} lecturas reales (nominal ${t.cobertura_nominal_pct} %).`
    : " Cobertura de las bandas: sin datos (se necesitan al menos 3 días).";
  p.textContent = t
    ? `Modelo ${t.version_modelo}, etapa “${t.etapa_ciclo_vida}”. Error de pronóstico: ${t.mape_pct ?? "sin datos"} %.${cobertura} ${t.accion_monitoreo}. Huella de los datos analizados: ${t.huella_analisis}.`
    : "";
}

// ------------------------------------------------------------ alternativas
function pintarAlternativas(ses) {
  const elegido = ses.propuesta && ses.propuesta.escenario;
  const rechazados = new Set(ses.historial.filter((x) => !x.aprobado).map((x) => x.escenario));
  document.querySelector("#tabla-alternativas tbody").replaceChildren(...ses.alternativas.map((e) =>
    h("tr", { class: e.id === elegido ? "elegido" : "" },
      h("td", {}, h("b", {}, e.id), " ", e.nombre, e.id === elegido ? h("div", { class: "sub" }, "Propuesta actual") : null,
        rechazados.has(e.id) ? h("div", { class: "sub" }, "Rechazada por ti") : null),
      h("td", { class: "num" }, num.format(e.costo_mxn)),
      h("td", { class: "num" }, `${num1.format(e.renovable_pct)} %`),
      h("td", { class: "num" }, num.format(e.deficit_kwh)),
      h("td", { class: "num" }, num.format(e.desplazado_kwh)),
      h("td", { class: "num" }, num.format(e.recorte_kwh)),
      h("td", {}, e.robusto ? h("span", { class: "efecto positivo" }, "Sí")
        : h("span", { class: "efecto negativo", title: `Déficit en P90: ${num.format(e.deficit_p90_kwh)} kWh` }, "No")),
      h("td", {}, e.riesgo_max == null ? "" : h("span", { class: `nivel ${nivelDe(e.riesgo_max)}` }, e.riesgo_max)))));
}

// ------------------------------------------------------------ bitácora
function pintarBitacora(ses) {
  $("bitacora").replaceChildren(...ses.consola.map((c) =>
    h("li", {}, h("span", { class: `n ${c.nodo}`, title: c.agente }, c.nodo), h("span", {}, c.texto))));
  $("descargar-bitacora").onclick = (ev) => descargar(`/sesiones/${ses.id}/bitacora`, `bitacora_${ses.id}.json`, ev.currentTarget);
}

// ------------------------------------------------------------ inicio
async function iniciar() {
  try {
    estado.config = await pedir("/config");
  } catch (e) {
    $("circuito-estado").textContent = e instanceof ErrorAcceso
      ? "Escribe el token de acceso para usar ElectriAgent." : e.message;
    return;
  }
  $("form-acceso").hidden = true;
  $("circuito-estado").textContent = "Elige un periodo y pulsa “Analizar consumo”.";
  construirFormulario();
  construirGrafo();
  marcarNodo("A", null);
  consultaMovil.addEventListener("change", () => {   // al girar el teléfono o redimensionar
    construirGrafo();
    estado.aristasPintadas = 0;
    if (estado.sesion) pintarRecorrido(estado.sesion); else marcarNodo("A", null);
  });
  const local = ["localhost", "127.0.0.1"].includes(location.hostname);
  if ("serviceWorker" in navigator && (location.protocol === "https:" || local)) {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
}
prepararAcceso();
iniciar();
