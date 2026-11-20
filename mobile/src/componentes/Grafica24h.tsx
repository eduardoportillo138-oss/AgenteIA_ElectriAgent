/**
 * Gráfica de 24 h del día recomendado: tarifas, renovables, límite de suministro,
 * demanda sin cambios, demanda con la recomendación, banda P10–P90 y horas de recorte.
 * Si el servidor entrega series por alimentador, un selector cambia entre
 * Subestación, A1 y A2.
 */
import React, { useMemo, useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';
import Svg, { Circle, G, Line, Path, Rect, Text as SvgText } from 'react-native-svg';

import type { Propuesta, SerieRed } from '../api/tipos';
import { hora, kw, num, num1, rangos } from '../formato';
import { color, espacio, fuente, texto } from '../tema';
import { Chip } from './Chip';

const W = 360;
const H = 240;
const M = { izq: 44, der: 8, arr: 18, aba: 26 };

type Tarifa = 'base' | 'intermedia' | 'punta';

/** Series a graficar: por alimentador si existen; si no, la serie clásica de la subestación. */
export function seriesDe(p: Propuesta): Record<string, SerieRed> {
  if (p.serie.por_alimentador && Object.keys(p.serie.por_alimentador).length) return p.serie.por_alimentador;
  const s = p.serie;
  const recorte = Array<number>(24).fill(0);
  return {
    SE: {
      nombre: 'Subestación',
      demanda_base: s.demanda_base,
      demanda_recomendada: s.demanda_recomendada,
      renovable: s.renovable,
      capacidad_kw: s.capacidad_kw,
      limite: s.renovable.map((r) => r + s.capacidad_kw),
      demanda_p10: s.bandas?.demanda_p10 ?? [],
      demanda_p90: s.bandas?.demanda_p90 ?? [],
      recorte,
      horas_recorte: s.horas_suspension,
    },
  };
}

export function Grafica24h({ propuesta, periodoTarifario }: { propuesta: Propuesta; periodoTarifario?: Tarifa[] }) {
  const series = useMemo(() => seriesDe(propuesta), [propuesta]);
  const claves = Object.keys(series);
  const [vista, setVista] = useState(claves[0] ?? 'SE');
  const se = series[vista] ?? series[claves[0]!]!;
  const conBanda = se.demanda_p10.length === 24 && se.demanda_p90.length === 24;
  const cambia = propuesta.escenario !== 'S0';

  const maxY = Math.max(...se.demanda_base, ...se.demanda_recomendada, ...se.limite, ...(conBanda ? se.demanda_p90 : [0]), 1) * 1.06;
  const x = (i: number) => M.izq + (i / 23) * (W - M.izq - M.der);
  const y = (v: number) => M.arr + (1 - v / maxY) * (H - M.arr - M.aba);
  const linea = (arr: number[]) => arr.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const paso = (W - M.izq - M.der) / 23;
  const banda = conBanda
    ? `${linea(se.demanda_p90)} ${se.demanda_p10.map((_, i) => `L${x(23 - i).toFixed(1)},${y(se.demanda_p10[23 - i]!).toFixed(1)}`).join(' ')} Z`
    : null;

  const resumen = [
    `${se.nombre}. Demanda máxima sin cambios ${kw(Math.max(...se.demanda_base))}`,
    cambia ? `, con la recomendación ${kw(Math.max(...se.demanda_recomendada))}` : '',
    `. Capacidad ${kw(se.capacidad_kw)}.`,
    conBanda ? ` Banda P10–P90 entre ${kw(Math.min(...se.demanda_p10))} y ${kw(Math.max(...se.demanda_p90))}.` : '',
    se.horas_recorte.length ? ` Recorte parcial en ${rangos(se.horas_recorte)}.` : '',
  ].join('');

  return (
    <View style={{ gap: espacio.s }}>
      {claves.length > 1 ? (
        <View style={estilos.selector} accessibilityRole="radiogroup" accessibilityLabel="Ver la red completa o un alimentador">
          {claves.map((k) => (
            <Chip key={k} texto={k === 'SE' ? 'Subestación' : k} seleccionado={k === vista} onPress={() => setVista(k)}
              etiquetaAccesible={k === 'SE' ? 'Subestación completa' : `Alimentador ${k}`} />
          ))}
        </View>
      ) : null}
      <Text style={texto.sub}>
        {vista === 'SE' ? `${se.nombre}: capacidad convencional ${kw(se.capacidad_kw)}.`
          : `${se.nombre}: puede entregar ${kw(se.capacidad_kw)} a sus cargas.`}
        {propuesta.alimentadores?.[vista] ? ` Uso máximo ${num1(propuesta.alimentadores[vista]!.utilizacion_max_pct)} %.` : ''}
      </Text>
      <View accessible accessibilityRole="image" accessibilityLabel={resumen} style={estilos.lienzo}>
        <Svg width="100%" height="100%" viewBox={`0 0 ${W} ${H}`}>
          {periodoTarifario?.map((t, i) => t === 'intermedia' ? null : (
            <Rect key={`t${i}`} x={x(i) - paso / 2} y={M.arr} width={paso} height={H - M.arr - M.aba}
              fill={t === 'punta' ? 'rgba(238,132,103,0.13)' : 'rgba(140,203,94,0.09)'} />
          ))}
          <G>
            {[0, 1, 2, 3, 4].map((k) => {
              const v = (maxY / 4) * k;
              return (
                <G key={`e${k}`}>
                  <Line x1={M.izq} x2={W - M.der} y1={y(v)} y2={y(v)} stroke={color.linea} strokeOpacity={k ? 0.35 : 1} />
                  <SvgText x={M.izq - 6} y={y(v) + 4} fontSize={10} fontFamily={fuente.rotulo} fill={color.suave} textAnchor="end">{num(v)}</SvgText>
                </G>
              );
            })}
            {[0, 6, 12, 18, 23].map((i) => (
              <SvgText key={`h${i}`} x={x(i)} y={H - 8} fontSize={10} fontFamily={fuente.rotulo} fill={color.suave} textAnchor={i === 23 ? 'end' : 'middle'}>{hora(i)}</SvgText>
            ))}
            <SvgText x={M.izq - 6} y={11} fontSize={10} fontFamily={fuente.rotulo} fill={color.suave} textAnchor="end">kW</SvgText>
          </G>
          <Path d={`${linea(se.renovable)} L${x(23)},${y(0)} L${x(0)},${y(0)} Z`} fill="rgba(140,203,94,0.38)" />
          {banda ? <Path d={banda} fill="rgba(201,214,210,0.16)" stroke="rgba(201,214,210,0.35)" strokeWidth={0.8} /> : null}
          <Path d={linea(se.limite)} fill="none" stroke={color.alerta} strokeWidth={1.4} strokeDasharray="2 4" />
          <Path d={linea(se.demanda_base)} fill="none" stroke={color.claro} strokeWidth={1.8} strokeDasharray={cambia ? '6 5' : undefined} />
          {cambia ? <Path d={linea(se.demanda_recomendada)} fill="none" stroke={color.cian} strokeWidth={2.6} /> : null}
          {se.horas_recorte.map((h) => (
            <Circle key={`r${h}`} cx={x(h)} cy={y(se.demanda_recomendada[h] ?? 0)} r={4} fill={color.alerta} />
          ))}
        </Svg>
      </View>
      <View style={estilos.leyenda}>
        <Item muestra={color.claro} texto="Demanda sin cambios" />
        {conBanda ? <Item muestra="rgba(201,214,210,0.45)" texto="Banda P10–P90" /> : null}
        {cambia ? <Item muestra={color.cian} texto="Con la recomendación" /> : null}
        <Item muestra="rgba(140,203,94,0.7)" texto={vista === 'SE' ? 'Renovable' : 'Renovable del alimentador'} />
        <Item muestra={color.alerta} texto="Límite de suministro" delgado />
        {se.horas_recorte.length ? <Item muestra={color.alerta} texto="Recorte parcial" punto /> : null}
        {periodoTarifario ? <Item muestra="rgba(140,203,94,0.3)" texto="Tarifa base" /> : null}
        {periodoTarifario ? <Item muestra="rgba(238,132,103,0.35)" texto="Tarifa punta" /> : null}
      </View>
    </View>
  );
}

function Item({ muestra, texto: t, delgado, punto }: { muestra: string; texto: string; delgado?: boolean; punto?: boolean }) {
  return (
    <View style={estilos.item}>
      <View style={[estilos.muestra, { backgroundColor: muestra }, delgado && { height: 3 }, punto && { width: 10, height: 10, borderRadius: 5 }]} />
      <Text style={estilos.itemTexto}>{t}</Text>
    </View>
  );
}

const estilos = StyleSheet.create({
  selector: { flexDirection: 'row', flexWrap: 'wrap', gap: espacio.s },
  lienzo: { width: '100%', aspectRatio: W / H },
  leyenda: { flexDirection: 'row', flexWrap: 'wrap', columnGap: espacio.l, rowGap: espacio.xs },
  item: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  muestra: { width: 16, height: 10, borderRadius: 2 },
  itemTexto: { fontSize: 13, color: color.suave },
});
