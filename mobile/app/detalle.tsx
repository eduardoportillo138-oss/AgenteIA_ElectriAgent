/** Evidencia del análisis: gráfica, consumo, anomalías, ISO, escenarios y bitácora. */
import React, { useState } from 'react';
import { ScrollView, StyleSheet, Text, View } from 'react-native';

import type { Vista } from '@/api/tipos';
import { Insignia, Seccion, Vacio } from '@/componentes/base';
import { Chip } from '@/componentes/Chip';
import { Cifra } from '@/componentes/Cifra';
import { Grafica24h } from '@/componentes/Grafica24h';
import { ListaBitacora } from '@/componentes/ListaBitacora';
import { ListaImpactos, NivelRiesgo, TablaRiesgos } from '@/componentes/TablaRiesgos';
import { useApp } from '@/estado/ContextoApp';
import { dia, fecha, hora, kw, kwh, mxn, nivelRiesgo, num, num1, pct } from '@/formato';
import { color, espacio, fuente, texto } from '@/tema';

const SECCIONES = ['Gráfica', 'Consumo', 'ISO', 'Escenarios', 'Bitácora'] as const;
type SeccionId = (typeof SECCIONES)[number];

export default function Detalle() {
  const { sesion, config } = useApp();
  const [seccion, setSeccion] = useState<SeccionId>('Gráfica');
  if (!sesion) return <Vacio titulo="Todavía no hay un análisis" />;
  return (
    <ScrollView contentContainerStyle={estilos.contenido} stickyHeaderIndices={[0]}>
      <View style={estilos.pestanas} accessibilityRole="tablist">
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: espacio.s }}>
          {SECCIONES.map((s) => <Chip key={s} texto={s} seleccionado={s === seccion} onPress={() => setSeccion(s)} />)}
        </ScrollView>
      </View>
      {seccion === 'Gráfica' ? <SeccionGrafica vista={sesion} tarifas={config?.red.periodo_tarifario} /> : null}
      {seccion === 'Consumo' ? <SeccionConsumo vista={sesion} criticas={new Set(config?.red.cargas.filter((c) => c.critica).map((c) => c.nombre))} /> : null}
      {seccion === 'ISO' ? <SeccionIso vista={sesion} /> : null}
      {seccion === 'Escenarios' ? <SeccionEscenarios vista={sesion} /> : null}
      {seccion === 'Bitácora' ? (
        <Seccion titulo="Bitácora de los agentes"><ListaBitacora consola={sesion.consola} /></Seccion>
      ) : null}
    </ScrollView>
  );
}

function SeccionGrafica({ vista, tarifas }: { vista: Vista; tarifas?: ('base' | 'intermedia' | 'punta')[] }) {
  return (
    <Seccion titulo="Día recomendado, hora por hora">
      <Text style={texto.sub}>{`Pronóstico para el ${dia(vista.dia_recomendado)}. ${vista.condicion.nombre}.`}</Text>
      {vista.propuesta ? <Grafica24h propuesta={vista.propuesta} periodoTarifario={tarifas} />
        : <Text style={texto.sub}>Sin propuesta que graficar.</Text>}
    </Seccion>
  );
}

function SeccionConsumo({ vista, criticas }: { vista: Vista; criticas: Set<string> }) {
  const a = vista.analisis;
  if (!a) return <Text style={texto.sub}>Sin análisis.</Text>;
  const entradas = Object.entries(a.consumo_por_carga_kwh).sort((x, y) => y[1] - x[1]);
  const max = entradas[0]?.[1] ?? 1;
  return (
    <>
      <Seccion titulo="Tu consumo en el periodo">
        <View style={estilos.cifras}>
          <Cifra etiqueta="Consumo total" valor={kwh(a.consumo_total_kwh)} nota={vista.periodo.nombre} />
          <Cifra etiqueta="Demanda máxima" valor={kw(a.pico_kw)} nota={fecha(a.fecha_pico)} />
          <Cifra etiqueta="Horas pico habituales" valor={a.horas_pico.map(hora).join(', ')} />
          <Cifra etiqueta="Generación renovable" valor={kwh(a.generacion_renovable_kwh)} />
          <Cifra etiqueta="Error de pronóstico (MAPE)" valor={a.mape_pct == null ? 'sin datos' : pct(a.mape_pct)}
            nota={a.mape_pct == null ? 'Se necesitan al menos 2 días' : undefined} />
          <Cifra etiqueta="Análisis aprendidos" valor={num(a.experiencia_ejecuciones)} nota="Memoria del agente de aprendizaje" />
        </View>
      </Seccion>
      <Seccion titulo="Consumo por carga">
        {entradas.map(([n, v]) => (
          <View key={n} accessible accessibilityLabel={`${n}${criticas.has(n) ? ', carga crítica' : ''}: ${kwh(v)}`}>
            <View style={estilos.filaEntre}>
              <Text style={[texto.cuerpo, { flex: 1 }]}>{n}{criticas.has(n) ? ' (crítica)' : ''}</Text>
              <Text style={estilos.valor}>{kwh(v)}</Text>
            </View>
            <View style={estilos.pista}>
              <View style={[estilos.relleno, { width: `${(v / max) * 100}%`, backgroundColor: criticas.has(n) ? color.verde : color.azul }]} />
            </View>
          </View>
        ))}
      </Seccion>
      <Seccion titulo="Lecturas fuera de patrón">
        <Text style={texto.sub}>
          {vista.anomalias.length
            ? `El agente de aprendizaje marcó ${a.deteccion.detectadas} lecturas; coinciden ${a.deteccion.aciertos} de las ${a.deteccion.inyectadas} anomalías simuladas. Se limpian antes de pronosticar.`
            : 'No se encontraron lecturas fuera de patrón en este periodo.'}
        </Text>
        {vista.anomalias.map((x, i) => (
          <View key={`${x.carga}-${x.fecha}-${i}`} style={estilos.anomalia}>
            <Text style={[texto.cuerpo, { flex: 1 }]}>{x.carga}{'\n'}<Text style={texto.sub}>{fecha(x.fecha)}</Text></Text>
            <Text style={[estilos.valor, { textAlign: 'right' }]}>{kw(x.kw)}{'\n'}<Text style={texto.sub}>{`esperado ${num(x.esperado_kw)}`}</Text></Text>
          </View>
        ))}
      </Seccion>
    </>
  );
}

function SeccionIso({ vista }: { vista: Vista }) {
  const ev = vista.propuesta?.evaluacion_iso;
  const t = vista.trazabilidad;
  return (
    <>
      <Seccion titulo="Riesgos ISO/IEC 23894">
        {ev ? <TablaRiesgos riesgos={ev.riesgos} /> : <Text style={texto.sub}>Sin evaluación.</Text>}
      </Seccion>
      <Seccion titulo="Impacto por grupo ISO/IEC 42005">
        {ev ? <ListaImpactos impactos={ev.impactos} /> : null}
      </Seccion>
      <Seccion titulo="Monitoreo ISO/IEC 5338">
        {t ? (
          <Text style={[texto.cuerpo, (t.alerta_deriva || t.alerta_calibracion_bandas) && { color: color.alerta }]}>
            {`Modelo ${t.version_modelo}, etapa “${t.etapa_ciclo_vida}”. Error de pronóstico: ${t.mape_pct == null ? 'sin datos' : pct(t.mape_pct)}. `}
            {t.cobertura_bandas_pct != null
              ? `Cobertura de las bandas P10–P90: ${num1(t.cobertura_bandas_pct)} % de ${t.lecturas_validacion_bandas ?? 0} lecturas reales (nominal ${num1(t.cobertura_nominal_pct ?? 80)} %). `
              : t.cobertura_bandas_pct === null ? 'Cobertura de las bandas: sin datos (se necesitan al menos 3 días). ' : ''}
            {`${t.accion_monitoreo}. Huella de los datos analizados: ${t.huella_analisis}.`}
          </Text>
        ) : null}
        <Text style={texto.sub}>
          {`Decisiones registradas en esta sesión: ${vista.historial.length}${vista.historial.some((d) => d.operador) ? ', firmadas por el operador' : ''}.`}
        </Text>
      </Seccion>
    </>
  );
}

function SeccionEscenarios({ vista }: { vista: Vista }) {
  const elegido = vista.propuesta?.escenario;
  const rechazados = new Set(vista.historial.filter((d) => !d.aprobado).map((d) => d.escenario));
  return (
    <Seccion titulo="Escenarios que se evaluaron">
      {vista.alternativas.map((e) => (
        <View key={e.id} style={[estilos.escenario, e.id === elegido && estilos.elegido]} accessible
          accessibilityLabel={`${e.id} ${e.nombre}. Costo ${mxn(e.costo_mxn)}, renovable ${pct(e.renovable_pct)}, déficit ${kwh(e.deficit_kwh)}, desplazado ${kwh(e.desplazado_kwh)}${e.recorte_kwh ? `, recorte ${kwh(e.recorte_kwh)}` : ''}${e.robusto != null ? `, ${e.robusto ? 'robusta' : 'no robusta'}` : ''}${e.riesgo_max != null ? `, riesgo ${e.riesgo_max}` : ''}${e.id === elegido ? '. Propuesta actual' : ''}${rechazados.has(e.id) ? '. Rechazada por ti' : ''}`}>
          <View style={estilos.filaEntre}>
            <Text style={[estilos.escenarioId]}>{e.id}</Text>
            {e.riesgo_max != null ? <NivelRiesgo nivel={e.riesgo_max} categoria={nivelRiesgo(e.riesgo_max)} /> : null}
          </View>
          <Text style={texto.cuerpo}>{e.nombre}</Text>
          <Text style={texto.sub}>
            {`${mxn(e.costo_mxn)} · renovable ${pct(e.renovable_pct)} · déficit ${kwh(e.deficit_kwh)} · desplazado ${kwh(e.desplazado_kwh)}`}
            {e.recorte_kwh ? ` · recorte ${kwh(e.recorte_kwh)}` : ''}
          </Text>
          <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: espacio.s }}>
            {e.robusto != null ? <Insignia texto={e.robusto ? 'Robusta' : 'No robusta'} tono={e.robusto ? 'ok' : 'riesgo'} /> : null}
            {e.id === elegido ? <Insignia texto="Propuesta actual" /> : null}
            {rechazados.has(e.id) ? <Insignia texto="Rechazada por ti" tono="riesgo" /> : null}
          </View>
        </View>
      ))}
    </Seccion>
  );
}

const estilos = StyleSheet.create({
  contenido: { padding: espacio.l, gap: espacio.l, paddingBottom: espacio.xxl },
  pestanas: { backgroundColor: color.fondo, paddingVertical: espacio.s },
  cifras: { flexDirection: 'row', flexWrap: 'wrap', gap: espacio.m },
  filaEntre: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: espacio.m },
  valor: { fontFamily: fuente.cifra, fontSize: 17, color: color.texto, fontVariant: ['tabular-nums'] },
  pista: { height: 8, backgroundColor: color.fondo, borderRadius: 4, overflow: 'hidden', marginTop: 4 },
  relleno: { height: '100%', borderRadius: 4 },
  anomalia: { flexDirection: 'row', gap: espacio.m, borderBottomWidth: 1, borderBottomColor: color.linea, paddingVertical: espacio.s },
  escenario: { borderLeftWidth: 3, borderLeftColor: color.linea, paddingLeft: espacio.m, gap: 4, paddingVertical: espacio.s },
  elegido: { borderLeftColor: color.cian, backgroundColor: 'rgba(53,214,240,0.08)' },
  escenarioId: { fontFamily: fuente.titulo, fontSize: 18, color: color.texto },
});
