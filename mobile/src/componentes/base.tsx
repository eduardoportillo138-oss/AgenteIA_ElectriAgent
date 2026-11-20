/** Primitivas de interfaz: botón, sección, aviso y estado vacío. */
import React from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View, type StyleProp, type ViewStyle } from 'react-native';

import { color, espacio, fuente, radio, TACTIL, texto } from '../tema';

type VarianteBoton = 'principal' | 'aprobar' | 'secundario' | 'peligro';

interface PropsBoton {
  titulo: string;
  onPress: () => void;
  variante?: VarianteBoton;
  deshabilitado?: boolean;
  cargando?: boolean;
  etiquetaAccesible?: string;
  pista?: string;
  estilo?: StyleProp<ViewStyle>;
  testID?: string;
}

const FONDO: Record<VarianteBoton, string> = {
  principal: color.cian,
  aprobar: color.verde,
  secundario: 'transparent',
  peligro: color.alerta,
};
const TINTA: Record<VarianteBoton, string> = {
  principal: color.sobreClaro,
  aprobar: color.fondo,
  secundario: color.texto,
  peligro: '#2A0E06',
};

export function Boton({ titulo, onPress, variante = 'principal', deshabilitado, cargando, etiquetaAccesible, pista, estilo, testID }: PropsBoton) {
  const inactivo = deshabilitado || cargando;
  return (
    <Pressable
      testID={testID}
      accessibilityRole="button"
      accessibilityLabel={etiquetaAccesible ?? titulo}
      accessibilityHint={pista}
      accessibilityState={{ disabled: !!inactivo, busy: !!cargando }}
      aria-disabled={!!inactivo}
      aria-busy={!!cargando}
      disabled={inactivo}
      onPress={onPress}
      style={({ pressed }) => [
        estilos.boton,
        { backgroundColor: FONDO[variante] },
        variante === 'secundario' && estilos.bordeado,
        inactivo && estilos.inactivo,
        pressed && !inactivo && estilos.presionado,
        estilo,
      ]}
    >
      {cargando ? <ActivityIndicator color={TINTA[variante]} style={{ marginRight: espacio.s }} /> : null}
      <Text style={[estilos.botonTexto, { color: TINTA[variante] }]}>{titulo}</Text>
    </Pressable>
  );
}

export function Seccion({ titulo, children, estilo, derecha }: {
  titulo?: string; children: React.ReactNode; estilo?: StyleProp<ViewStyle>; derecha?: React.ReactNode;
}) {
  return (
    <View style={[estilos.seccion, estilo]}>
      {titulo ? (
        <View style={estilos.seccionCabeza}>
          <Text style={texto.h2} accessibilityRole="header">{titulo}</Text>
          {derecha}
        </View>
      ) : null}
      {children}
    </View>
  );
}

type TipoAviso = 'info' | 'error' | 'espera' | 'ok';
const COLOR_AVISO: Record<TipoAviso, string> = {
  info: color.linea, error: color.alerta, espera: color.rayo, ok: color.verde,
};

/** Mensaje con el problema y qué hacer. */
export function Aviso({ tipo = 'info', titulo, detalle, children }: {
  tipo?: TipoAviso; titulo: string; detalle?: string; children?: React.ReactNode;
}) {
  return (
    <View
      style={[estilos.aviso, { borderColor: COLOR_AVISO[tipo] }]}
      accessibilityRole={tipo === 'error' ? 'alert' : 'summary'}
      accessibilityLiveRegion={tipo === 'error' ? 'assertive' : 'polite'}
    >
      <Text style={[estilos.avisoTitulo, tipo === 'error' && { color: color.alerta }, tipo === 'espera' && { color: color.rayo }]}>
        {titulo}
      </Text>
      {detalle ? <Text style={texto.sub}>{detalle}</Text> : null}
      {children}
    </View>
  );
}

export function Vacio({ titulo, detalle, children }: { titulo: string; detalle?: string; children?: React.ReactNode }) {
  return (
    <View style={estilos.vacio}>
      <Text style={[texto.h2, { textAlign: 'center' }]}>{titulo}</Text>
      {detalle ? <Text style={[texto.sub, { textAlign: 'center', marginTop: espacio.s }]}>{detalle}</Text> : null}
      <View style={{ marginTop: espacio.l, alignSelf: 'stretch' }}>{children}</View>
    </View>
  );
}

export function Insignia({ texto: t, tono = 'neutral' }: { texto: string; tono?: 'ok' | 'riesgo' | 'neutral' | 'espera' }) {
  const c = tono === 'ok' ? color.verde : tono === 'riesgo' ? color.alerta : tono === 'espera' ? color.rayo : color.suave;
  return (
    <View style={[estilos.insignia, { borderColor: c }]}>
      <Text style={[estilos.insigniaTexto, { color: c }]}>{t}</Text>
    </View>
  );
}

export const estilos = StyleSheet.create({
  boton: {
    minHeight: TACTIL,
    paddingHorizontal: espacio.l,
    paddingVertical: espacio.m,
    borderRadius: radio.s,
    alignItems: 'center',
    justifyContent: 'center',
    flexDirection: 'row',
  },
  bordeado: { borderWidth: 1, borderColor: color.suave },
  inactivo: { opacity: 0.45 },
  presionado: { opacity: 0.8 },
  botonTexto: { fontFamily: fuente.titulo, fontSize: 17, letterSpacing: 0.3, textAlign: 'center' },
  seccion: { backgroundColor: color.panel, borderRadius: radio.m, padding: espacio.l, gap: espacio.m },
  seccionCabeza: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: espacio.s, flexWrap: 'wrap' },
  aviso: { borderLeftWidth: 4, backgroundColor: color.panel, borderRadius: radio.s, padding: espacio.m, gap: espacio.xs },
  avisoTitulo: { fontSize: 16, lineHeight: 22, color: color.texto, fontWeight: '600' },
  vacio: { padding: espacio.xl, alignItems: 'center' },
  insignia: { borderWidth: 1, borderRadius: radio.pastilla, paddingHorizontal: espacio.m, paddingVertical: 3, alignSelf: 'flex-start' },
  insigniaTexto: { fontFamily: fuente.rotulo, fontSize: 14 },
});
