/** Riesgos ISO/IEC 23894 (probabilidad x impacto) e impactos ISO/IEC 42005 por grupo. */
import React from 'react';
import { StyleSheet, Text, View } from 'react-native';

import type { Impacto, Riesgo } from '../api/tipos';
import { color, espacio, fuente, NIVEL_COLOR, radio, tabular, texto } from '../tema';

export function NivelRiesgo({ nivel, categoria }: { nivel: number; categoria: string }) {
  const fondo = NIVEL_COLOR[categoria] ?? color.suave;
  return (
    <View style={[estilos.nivel, { backgroundColor: fondo }]}>
      <Text style={[estilos.nivelTexto, categoria === 'crítico' && { color: '#FFFFFF' }]}>{`${nivel} ${categoria}`}</Text>
    </View>
  );
}

export function TablaRiesgos({ riesgos }: { riesgos: Riesgo[] }) {
  return (
    <View style={{ gap: espacio.s }}>
      {riesgos.map((r) => (
        <View key={r.riesgo} style={estilos.fila} accessible
          accessibilityLabel={`${r.riesgo}. Probabilidad ${r.prob}, impacto ${r.impacto}, nivel ${r.nivel} ${r.categoria}. ${r.tratamiento}`}>
          <View style={estilos.cabeza}>
            <Text style={[texto.cuerpo, { flex: 1, fontWeight: '600' }]}>{r.riesgo}</Text>
            <NivelRiesgo nivel={r.nivel} categoria={r.categoria} />
          </View>
          <Text style={[texto.sub, tabular]}>{`Probabilidad ${r.prob} × impacto ${r.impacto}`}</Text>
          <Text style={texto.sub}>{r.tratamiento}</Text>
        </View>
      ))}
    </View>
  );
}

const COLOR_EFECTO = { positivo: color.verde, negativo: color.alerta, neutral: color.suave } as const;

export function ListaImpactos({ impactos }: { impactos: Impacto[] }) {
  return (
    <View style={{ gap: espacio.m }}>
      {impactos.map((i) => (
        <View key={i.grupo} accessible accessibilityLabel={`${i.grupo}: efecto ${i.efecto}. ${i.detalle}`}>
          <Text style={[texto.cuerpo, { fontWeight: '600' }]}>
            {i.grupo} <Text style={{ color: COLOR_EFECTO[i.efecto], fontWeight: '400', fontSize: 14 }}>{i.efecto}</Text>
          </Text>
          <Text style={texto.sub}>{i.detalle}</Text>
        </View>
      ))}
    </View>
  );
}

const estilos = StyleSheet.create({
  fila: { borderBottomWidth: 1, borderBottomColor: color.linea, paddingBottom: espacio.s, gap: 2 },
  cabeza: { flexDirection: 'row', alignItems: 'flex-start', gap: espacio.s },
  nivel: { borderRadius: radio.pastilla, paddingHorizontal: espacio.s, paddingVertical: 2 },
  nivelTexto: { fontFamily: fuente.titulo, fontSize: 14, color: color.fondo },
});
