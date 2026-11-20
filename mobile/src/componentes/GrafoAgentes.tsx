/**
 * Grafo etiquetado A–H con la disposición vertical de la web (web/app.js).
 * Las aristas recorridas se iluminan en orden con su peso; el nodo G pulsa
 * mientras espera la decisión. Con "reducir movimiento" no hay animaciones.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Animated, Easing, Platform, StyleSheet, View } from 'react-native';
import Svg, { Circle, G, Path, Polygon, Rect, Text as SvgText } from 'react-native-svg';

import type { Arista, EstadoSesion, IdNodo } from '../api/tipos';
import { color, fuente } from '../tema';

// Mismas coordenadas que DISPOSICION.vertical en web/app.js
export const CAJA = { ancho: 420, alto: 760 };
export const POS: Record<IdNodo, [number, number]> = {
  A: [60, 70], B: [210, 70], C: [210, 240], F: [360, 240], D: [70, 400], E: [350, 400], G: [210, 560], H: [210, 700],
};
const ETIQUETA: Record<IdNodo, [string, number, number, 'start' | 'middle' | 'end']> = {
  A: ['Usuario', 0, 56, 'middle'], B: ['Captura IoT', 42, 6, 'start'],
  C: ['Agente de meta', -42, 6, 'end'], F: ['Evaluación ISO', 0, -44, 'middle'],
  D: ['Aprendizaje', 0, 56, 'middle'], E: ['Deliberativo', 0, 56, 'middle'],
  G: ['Aprobación humana', 42, 6, 'start'], H: ['Recomendación final', 42, 6, 'start'],
};
const CURVA: Record<string, number> = { 'C-D': 34, 'D-C': 34, 'C-G': 30, 'G-C': 30 };
const ROL: Record<IdNodo, 'io' | 'supervisor' | 'trabajador'> = {
  A: 'io', B: 'io', C: 'supervisor', D: 'trabajador', E: 'trabajador', F: 'trabajador', G: 'io', H: 'io',
};
const R = 30;
const NODOS = Object.keys(POS) as IdNodo[];

export function geometria(a: IdNodo, b: IdNodo) {
  const [x1, y1] = POS[a];
  const [x2, y2] = POS[b];
  const dx = x2 - x1;
  const dy = y2 - y1;
  const L = Math.hypot(dx, dy);
  const nx = -dy / L;
  const ny = dx / L;
  const k = CURVA[`${a}-${b}`] ?? 0;
  const cx = (x1 + x2) / 2 + nx * k;
  const cy = (y1 + y2) / 2 + ny * k;
  const hacia = (px: number, py: number, d: number): [number, number] => {
    const ux = cx - px;
    const uy = cy - py;
    const l = Math.hypot(ux, uy) || 1;
    return [px + (ux / l) * d, py + (uy / l) * d];
  };
  const [sx, sy] = hacia(x1, y1, R + 2);
  const [ex, ey] = hacia(x2, y2, R + 4);
  // Punta de flecha: triángulo orientado desde el punto de control hacia el final
  const ax = ex - cx;
  const ay = ey - cy;
  const al = Math.hypot(ax, ay) || 1;
  const ux = ax / al;
  const uy = ay / al;
  const punta = [
    [ex, ey],
    [ex - ux * 11 - uy * 6, ey - uy * 11 + ux * 6],
    [ex - ux * 11 + uy * 6, ey - uy * 11 - ux * 6],
  ].map((p) => p.join(',')).join(' ');
  return {
    d: `M${sx},${sy} Q${cx},${cy} ${ex - ux * 8},${ey - uy * 8}`,
    punta,
    lx: 0.25 * sx + 0.5 * cx + 0.25 * ex,
    ly: 0.25 * sy + 0.5 * cy + 0.25 * ey,
  };
}

const AnimCircle = Animated.createAnimatedComponent(Circle);
// En la web, Animated + SVG pasa `collapsable` al DOM: ahí el halo de G es fijo.
const PULSO_ANIMADO = Platform.OS !== 'web';

interface Props {
  aristas: Arista[];
  recorrido: string[];
  estado: EstadoSesion | null;
  costo?: number;
  reducirMovimiento: boolean;
  /** ms entre aristas al animar */
  paso?: number;
}

export function GrafoAgentes({ aristas, recorrido, estado, costo, reducirMovimiento, paso = 140 }: Props) {
  const clave = recorrido.join('|');
  const total = recorrido.length;
  // Avance de la animación ligado al recorrido que se está pintando
  const [avance, setAvance] = useState({ clave: '', n: 0 });
  const pintadas = reducirMovimiento ? total : avance.clave === clave ? avance.n : 0;

  // Ilumina las aristas una por una (o todas de golpe si se reduce el movimiento)
  useEffect(() => {
    if (reducirMovimiento) return;
    let i = 0;
    const t = setInterval(() => {
      i += 1;
      setAvance({ clave, n: i });
      if (i >= total) clearInterval(t);
    }, paso);
    return () => clearInterval(t);
  }, [clave, total, reducirMovimiento, paso]);

  const activas = useMemo(() => new Set(recorrido.slice(0, pintadas)), [recorrido, pintadas]);
  const visitados = useMemo(() => {
    const s = new Set<IdNodo>(['A']);
    recorrido.slice(0, pintadas).forEach((r) => r.split('->').forEach((n) => s.add(n as IdNodo)));
    return s;
  }, [recorrido, pintadas]);
  const terminado = pintadas >= recorrido.length;
  const esperaG = estado === 'en_espera' && terminado;

  // Pulso del nodo G
  const [pulso] = useState(() => new Animated.Value(0));
  useEffect(() => {
    if (!esperaG || reducirMovimiento || !PULSO_ANIMADO) {
      pulso.setValue(0);
      return;
    }
    const ciclo = Animated.loop(Animated.timing(pulso, {
      toValue: 1, duration: 1600, easing: Easing.out(Easing.quad), useNativeDriver: false,
    }));
    ciclo.start();
    return () => ciclo.stop();
  }, [esperaG, reducirMovimiento, pulso]);

  const descripcion = [
    `Grafo de agentes A a H. ${recorrido.length} mensajes`,
    recorrido.length ? `: ${recorrido.join(', ').replace(/->/g, ' a ')}` : '',
    costo != null ? `. Costo de coordinación ${costo}` : '',
    estado === 'en_espera' ? '. La propuesta espera tu decisión en Aprobación humana (G).' : '',
    estado === 'aprobada' ? '. Recomendación aprobada; llegó a Recomendación final (H).' : '',
    estado === 'escalada' ? '. El caso se escaló al operador.' : '',
  ].join('');

  const colorNodo = (n: IdNodo) => {
    if (n === 'G' && esperaG) return color.rayo;
    if (n === 'H' && estado === 'aprobada' && terminado) return color.verde;
    if (!visitados.has(n)) return color.panel;
    return ROL[n] === 'supervisor' ? color.azul : ROL[n] === 'trabajador' ? color.verde : color.claro;
  };

  return (
    <View style={estilos.marco} accessible accessibilityRole="image" accessibilityLabel={descripcion}>
      <Svg width="100%" height="100%" viewBox={`0 0 ${CAJA.ancho} ${CAJA.alto}`}>
        {aristas.map(({ de, a, peso }) => {
          const id = `${de}->${a}`;
          const g = geometria(de, a);
          const activa = activas.has(id);
          const ultima = esperaG && recorrido[recorrido.length - 1] === id;
          const trazo = activa ? color.cian : color.arista;
          const ancho = String(peso).length * 10 + 12;
          return (
            <G key={id}>
              <Path d={g.d} fill="none" stroke={trazo} strokeWidth={activa ? 3.4 : 2.2}
                strokeDasharray={de === 'G' && a === 'C' ? '7 6' : ultima ? '10 8' : undefined} />
              <Polygon points={g.punta} fill={trazo} />
              <Rect x={g.lx - ancho / 2} y={g.ly - 12} width={ancho} height={24} rx={4} fill={color.fondo} />
              <SvgText x={g.lx} y={g.ly + 6} fontSize={18} fontFamily={fuente.titulo} fill={activa ? color.cian : color.suave} textAnchor="middle">
                {String(peso)}
              </SvgText>
            </G>
          );
        })}
        {NODOS.map((n) => {
          const [x, y] = POS[n];
          const [txt, dx, dy, ancla] = ETIQUETA[n];
          const relleno = colorNodo(n);
          const inactivo = relleno === color.panel;
          return (
            <G key={n}>
              {n === 'G' && esperaG && PULSO_ANIMADO && !reducirMovimiento ? (
                <AnimCircle cx={x} cy={y} fill="none" stroke={color.rayo} strokeWidth={2}
                  r={pulso.interpolate({ inputRange: [0, 1], outputRange: [R, R * 1.9] })}
                  opacity={pulso.interpolate({ inputRange: [0, 1], outputRange: [0.9, 0] })} />
              ) : null}
              {n === 'G' && esperaG && (!PULSO_ANIMADO || reducirMovimiento) ? (
                <Circle cx={x} cy={y} r={R + 8} fill="none" stroke={color.rayo} strokeWidth={2} opacity={0.8} />
              ) : null}
              <Circle cx={x} cy={y} r={R} fill={relleno} stroke={inactivo ? color.linea : relleno} strokeWidth={3} />
              <SvgText x={x} y={y + 9} fontSize={27} fontFamily={fuente.titulo} textAnchor="middle"
                fill={inactivo ? color.suave : ROL[n] === 'supervisor' && !(n === 'G') ? '#FFFFFF' : color.fondo}>
                {n}
              </SvgText>
              <SvgText x={x + dx} y={y + dy} fontSize={17} fontFamily={fuente.rotulo} fill={color.texto} textAnchor={ancla}>{txt}</SvgText>
            </G>
          );
        })}
      </Svg>
    </View>
  );
}

const estilos = StyleSheet.create({
  marco: { width: '100%', maxWidth: 460, aspectRatio: CAJA.ancho / CAJA.alto, alignSelf: 'center' },
});
