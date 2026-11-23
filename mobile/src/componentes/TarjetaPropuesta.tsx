/**
 * Propuesta del agente de meta (C) en espera de la decisión humana (G):
 * acciones, métricas frente a la operación actual, justificación de las reglas 1–4,
 * recorte parcial, detalle por alimentador y robustez P90 (si el servidor los envía).
 * Abajo queda fija la barra de decisión; ambas acciones piden confirmación,
 * se firman con el nombre del operador y dan retroalimentación háptica.
 */
import * as Haptics from 'expo-haptics';
import React, { useState } from 'react';
import { ScrollView, StyleSheet, Text, View } from 'react-native';

import type { ErrorApi } from '../api/cliente';
import type { Metricas, Vista } from '../api/tipos';
import { delta, fecha, kw, kwh, mxn, num, num1, pct, rangos, sinPrefijoRegla } from '../formato';
import { color, espacio, fuente, radio, texto } from '../tema';
import { Aviso, Insignia, Seccion } from './base';
import { BarraDecision, SIN_CONEXION } from './BarraDecision';
import { Cifra } from './Cifra';
import { HojaDecision, type ModoHoja } from './HojaDecision';

export interface RespuestaDecision {
  ok: boolean;
  aviso?: string;
  error?: ErrorApi;
}

interface Props {
  vista: Vista;
  enLinea: boolean;
  soloLectura: boolean;
  operador: string;
  onDecidir: (aprobado: boolean, motivo: string) => Promise<RespuestaDecision>;
  /** Contenido extra al inicio (p. ej. aviso de copia local) y al final (enlaces). */
  encabezado?: React.ReactNode;
  pie?: React.ReactNode;
}

/** Motivo por el que no se puede decidir ahora; null si se puede. */
export function bloqueoDecision(enLinea: boolean, soloLectura: boolean, operador: string): string | null {
  if (!enLinea) return `${SIN_CONEXION}.`;
  if (soloLectura) return `Estás viendo una copia guardada en el teléfono. ${SIN_CONEXION}: recarga la sesión.`;
  if (!operador.trim()) return 'Escribe tu nombre en Ajustes: cada decisión se firma con el nombre del operador.';
  return null;
}

async function vibrar(tipo: Haptics.NotificationFeedbackType) {
  try {
    await Haptics.notificationAsync(tipo);
  } catch {
    // Sin motor háptico (web o emulador): no pasa nada
  }
}

export function TarjetaPropuesta({ vista, enLinea, soloLectura, operador, onDecidir, encabezado, pie }: Props) {
  const [modo, setModo] = useState<ModoHoja>(null);
  const [enviando, setEnviando] = useState(false);
  const [errorHoja, setErrorHoja] = useState<string | null>(null);
  const [aviso, setAviso] = useState<string | null>(null);
  const p = vista.propuesta;
  const enEspera = vista.estado === 'en_espera';
  const bloqueo = bloqueoDecision(enLinea, soloLectura, operador);

  const confirmar = async (motivo: string) => {
    if (!modo || bloqueo) return;
    const aprobado = modo === 'aprobar';
    if (!aprobado && !motivo.trim()) return;          // el rechazo sin motivo nunca se envía
    setEnviando(true);
    setErrorHoja(null);
    const r = await onDecidir(aprobado, motivo);
    setEnviando(false);
    if (r.ok) {
      setModo(null);
      setAviso(null);
      await vibrar(aprobado ? Haptics.NotificationFeedbackType.Success : Haptics.NotificationFeedbackType.Warning);
    } else if (r.aviso) {
      setModo(null);
      setAviso(r.aviso);
      await vibrar(Haptics.NotificationFeedbackType.Warning);
    } else if (r.error) {
      setErrorHoja(`${r.error.message} ${r.error.sugerencia}`);
      await vibrar(Haptics.NotificationFeedbackType.Error);
    }
  };

  return (
    <View style={{ flex: 1 }}>
      <ScrollView contentContainerStyle={estilos.contenido} testID="tarjeta-propuesta">
        {encabezado}
        {aviso ? <Aviso tipo="info" titulo={aviso} /> : null}
        {p ? <Contenido vista={vista} /> : (
          <Seccion titulo="No hay una propuesta que puedas aprobar">
            {(vista.motivo_escalamiento ?? []).map((t) => <Text key={t} style={texto.cuerpo}>{t}</Text>)}
          </Seccion>
        )}
        {vista.historial.length ? (
          <Seccion titulo="Decisiones en esta sesión">
            {vista.historial.map((d, i) => (
              <Text key={`${d.fecha}-${i}`} style={texto.sub}>
                {`${fecha(d.fecha)} · ${d.aprobado ? 'Aprobada' : 'Rechazada'} ${d.escenario}`}
                {d.operador ? ` por ${d.operador}` : ''}
                {d.motivo ? `: “${d.motivo}”` : ''}
              </Text>
            ))}
          </Seccion>
        ) : null}
        {pie}
      </ScrollView>
      {enEspera && p ? (
        <BarraDecision
          bloqueo={bloqueo}
          enviando={enviando}
          onAprobar={() => { setErrorHoja(null); setModo('aprobar'); }}
          onRechazar={() => { setErrorHoja(null); setModo('rechazar'); }}
        />
      ) : null}
      {p ? (
        <HojaDecision modo={modo} escenario={p.escenario} operador={operador} enviando={enviando} error={errorHoja}
          onConfirmar={confirmar} onCancelar={() => !enviando && setModo(null)} />
      ) : null}
    </View>
  );
}

/** Política de recorte: el plan mínimo se propone por omisión y el robusto P90 es la alternativa. */
export function AvisoRobustez({ vista }: { vista: Vista }) {
  const p = vista.propuesta;
  const extra = p?.recorte_adicional_robustez_kwh;
  if (!p || extra == null) return null;
  return p.alternativa_robusta ? (
    <View testID="aviso-robustez">
      <Aviso tipo="info" titulo={`Alternativa robusta ${p.alternativa_robusta}: recorta ${kwh(extra)} adicionales`}
        detalle={`Evitaría ${kwh(p.metricas.deficit_p90_kwh)} de déficit si la demanda llega a P90 (renovables en P10). Para recibirla, rechaza esta propuesta pidiendo más seguridad.`} />
    </View>
  ) : (
    <View testID="aviso-robustez">
      <Aviso tipo="info" titulo={`Plan robusto: recorta ${kwh(extra)} más que el recorte mínimo`}
        detalle="Así no queda déficit aunque la demanda llegue a P90 con las renovables en P10." />
    </View>
  );
}

const TITULO_ESTADO: Record<string, string> = {
  en_espera: 'Falta tu decisión',
  aprobada: 'Recomendación aprobada',
  escalada: 'Caso escalado al operador',
  rechazada: 'Recomendación rechazada',
};

function dif(m: Metricas, b: Metricas | null, clave: keyof Metricas, escenario: string, decimales = 0) {
  if (!b || escenario === 'S0') return null;
  const a = m[clave];
  const c = b[clave];
  if (typeof a !== 'number' || typeof c !== 'number') return null;
  return { valor: a - c, texto: delta(a - c, decimales) };
}

function Contenido({ vista }: { vista: Vista }) {
  const p = vista.propuesta!;
  const m = p.metricas;
  const b = p.metricas_base;
  const recorte = m.recorte_kwh ?? m.suspendido_kwh;
  const enEspera = vista.estado === 'en_espera';
  return (
    <>
      <View style={[estilos.cabecera, enEspera && estilos.cabeceraEspera]}>
        <Text style={[estilos.estado, { color: enEspera ? color.rayo : vista.estado === 'aprobada' ? color.verde : color.alerta }]}>
          {TITULO_ESTADO[vista.estado] ?? ''}
        </Text>
        <Text style={texto.h2} accessibilityRole="header">
          {p.nombre} <Text style={{ color: color.suave }}>({p.escenario})</Text>
        </Text>
        <View style={estilos.insignias}>
          {p.robusto === true ? <Insignia tono="ok" texto="Robusta ante P90" /> : null}
          {p.robusto === false ? (
            <Insignia tono="riesgo" texto={`No robusta: ${kwh(m.deficit_p90_kwh)} de déficit en P90`} />
          ) : null}
          {recorte > 0 && p.base_planificacion ? <Insignia texto={`Recorte planificado con la banda ${p.base_planificacion}`} /> : null}
        </View>
        {p.modo === 'contingencia' ? (
          <Aviso tipo="error" titulo="La demanda prevista supera la capacidad disponible"
            detalle="La propuesta recorta parcialmente cargas no críticas, solo los kW necesarios y por prioridad. El hospital conserva el 100 % de su suministro." />
        ) : null}
        <AvisoRobustez vista={vista} />
        <View style={{ gap: espacio.s }}>
          {p.acciones.map((a, i) => {
            const detalle = a.startsWith('  ·');
            return (
              <View key={`${i}-${a}`} style={[estilos.accion, detalle && { paddingLeft: espacio.xl }]}>
                <View style={[estilos.vineta, detalle && estilos.vinetaDetalle]} />
                <Text style={[detalle ? texto.sub : texto.cuerpo, { flex: 1 }]}>{a.replace(/^\s*·\s*/, '')}</Text>
              </View>
            );
          })}
        </View>
      </View>

      <Seccion titulo="Cifras del día">
        <View style={estilos.cifras}>
          <Cifra etiqueta="Costo del día" valor={mxn(m.costo_mxn)} diferencia={dif(m, b, 'costo_mxn', p.escenario)} mejorSiMenor />
          <Cifra etiqueta="Cubierto con renovables" valor={pct(m.renovable_pct)} diferencia={dif(m, b, 'renovable_pct', p.escenario, 1)} />
          <Cifra etiqueta="Margen mínimo de capacidad" valor={pct(m.margen_min_pct)} diferencia={dif(m, b, 'margen_min_pct', p.escenario, 1)} />
          <Cifra etiqueta="Consumo que cambia de horario" valor={kwh(m.desplazado_kwh)} />
          {recorte > 0 ? <Cifra etiqueta="Recorte parcial" valor={kwh(recorte)} /> : null}
          {m.perdidas_kwh != null ? <Cifra etiqueta="Pérdidas técnicas" valor={kwh(m.perdidas_kwh)} /> : null}
          {p.evaluacion_iso ? (
            <Cifra etiqueta="Riesgo máximo" valor={`${p.evaluacion_iso.riesgo_max} (${p.evaluacion_iso.categoria_max})`} />
          ) : null}
        </View>
      </Seccion>

      {p.recorte_por_carga?.length ? (
        <Seccion titulo="Recorte parcial por carga">
          {p.recorte_por_carga.map((r) => {
            const max = Math.max(...p.recorte_por_carga!.map((x) => x.kwh), 1);
            return (
              <View key={r.carga} accessible accessibilityLabel={`${r.carga}: ${kwh(r.kwh)}, hasta ${kw(r.kw_max)}, ${rangos(r.horas)}`}>
                <View style={estilos.filaEntre}>
                  <Text style={[texto.cuerpo, { flex: 1 }]}>{r.carga}</Text>
                  <Text style={estilos.valor}>{kwh(r.kwh)}</Text>
                </View>
                <Text style={texto.sub}>{`Hasta ${kw(r.kw_max)} · ${r.horas.length} h (${rangos(r.horas)})`}</Text>
                <View style={estilos.pista}><View style={[estilos.relleno, { width: `${(r.kwh / max) * 100}%`, backgroundColor: color.alerta }]} /></View>
              </View>
            );
          })}
        </Seccion>
      ) : null}

      {p.alimentadores && Object.keys(p.alimentadores).length ? (
        <Seccion titulo="Distribución por alimentador">
          {Object.entries(p.alimentadores).map(([k, a]) => (
            <View key={k} style={estilos.alim} accessible
              accessibilityLabel={`${k}${a.carga_critica ? ', con carga crítica' : ''}. Uso máximo ${num1(a.utilizacion_max_pct)} por ciento, margen ${num1(a.margen_min_pct)} por ciento.`}>
              <Text style={estilos.alimTitulo}>{`${k}${a.carga_critica ? ' · con carga crítica' : ''}`}</Text>
              <View style={estilos.pista}>
                <View style={[estilos.relleno, { width: `${Math.min(100, a.utilizacion_max_pct)}%`,
                  backgroundColor: a.utilizacion_max_pct >= 90 ? color.alerta : color.cian }]} />
              </View>
              <Text style={texto.cuerpo}>{`Uso máx. ${num1(a.utilizacion_max_pct)} % · margen ${num1(a.margen_min_pct)} %`}</Text>
              <Text style={texto.sub}>
                {`Renovable ${kwh(a.renovable_usada_kwh)} · convencional ${kwh(a.convencional_kwh)}`}
                {a.recorte_kwh ? ` · recorte ${kwh(a.recorte_kwh)}` : ''}
              </Text>
            </View>
          ))}
          {p.metricas_p90 ? (
            <Text style={texto.sub}>
              {`Escenario desfavorable P90 (demanda P90, renovables P10): déficit ${kwh(p.metricas_p90.deficit_kwh)}, margen mínimo ${num1(p.metricas_p90.margen_red_min_pct ?? p.metricas_p90.margen_min_pct)} %.`}
            </Text>
          ) : null}
        </Seccion>
      ) : null}

      <Seccion titulo="Por qué esta recomendación">
        {p.justificacion.map((j, i) => (
          <View key={`${i}-${j.slice(0, 12)}`} style={estilos.accion}>
            <Text style={estilos.numero}>{`${i + 1}.`}</Text>
            <Text style={[texto.cuerpo, { flex: 1 }]}>{sinPrefijoRegla(j)}</Text>
          </View>
        ))}
        <Text style={texto.sub}>{`Costo de coordinación entre agentes: ${num(vista.coordinacion.costo)} (${vista.coordinacion.mensajes} mensajes).`}</Text>
      </Seccion>
    </>
  );
}

const estilos = StyleSheet.create({
  contenido: { padding: espacio.l, gap: espacio.l, paddingBottom: espacio.xxl },
  cabecera: { backgroundColor: color.panel, borderRadius: radio.m, padding: espacio.l, gap: espacio.m, borderWidth: 2, borderColor: color.linea },
  cabeceraEspera: { borderColor: color.rayo },
  estado: { fontFamily: fuente.titulo, fontSize: 16 },
  insignias: { flexDirection: 'row', flexWrap: 'wrap', gap: espacio.s },
  accion: { flexDirection: 'row', gap: espacio.s, alignItems: 'flex-start' },
  vineta: { width: 9, height: 9, borderRadius: 2, backgroundColor: color.cian, marginTop: 8 },
  vinetaDetalle: { width: 6, height: 6, backgroundColor: color.suave },
  cifras: { flexDirection: 'row', flexWrap: 'wrap', gap: espacio.m },
  filaEntre: { flexDirection: 'row', justifyContent: 'space-between', gap: espacio.m },
  valor: { fontFamily: fuente.cifra, fontSize: 17, color: color.texto, fontVariant: ['tabular-nums'] },
  pista: { height: 8, backgroundColor: color.fondo, borderRadius: 4, overflow: 'hidden', marginTop: 4 },
  relleno: { height: '100%', borderRadius: 4 },
  alim: { backgroundColor: color.panel2, borderRadius: radio.s, padding: espacio.m, gap: 4 },
  alimTitulo: { fontFamily: fuente.titulo, fontSize: 17, color: color.texto },
  numero: { fontFamily: fuente.titulo, fontSize: 16, color: color.suave, minWidth: 20, lineHeight: 23 },
});
