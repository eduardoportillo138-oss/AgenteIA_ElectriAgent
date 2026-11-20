/**
 * Barra fija con la acción principal de la app: aprobar o rechazar.
 * Si no se puede decidir (sin conexión, copia local, sin nombre de operador)
 * los botones quedan deshabilitados y se explica por qué.
 */
import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { color, espacio } from '../tema';
import { Boton } from './base';

export const SIN_CONEXION = 'Necesitas conexión con el servidor para decidir';

export function BarraDecision({ bloqueo, enviando, onAprobar, onRechazar }: {
  /** Motivo por el que no se puede decidir; null si se puede. */
  bloqueo: string | null;
  enviando?: boolean;
  onAprobar: () => void;
  onRechazar: () => void;
}) {
  const insets = useSafeAreaInsets();
  const deshabilitada = bloqueo !== null;
  return (
    <View style={[estilos.barra, { paddingBottom: Math.max(insets.bottom, espacio.m) }]} testID="barra-decision">
      {bloqueo ? (
        <Text style={estilos.bloqueo} accessibilityRole="alert" testID="barra-bloqueo">{bloqueo}</Text>
      ) : null}
      <View style={estilos.botones}>
        <Boton testID="boton-aprobar" titulo="Aprobar recomendación" variante="aprobar" onPress={onAprobar}
          deshabilitado={deshabilitada} cargando={enviando} estilo={estilos.boton}
          pista="Pide confirmación antes de enviar tu aprobación firmada" />
        <Boton testID="boton-rechazar" titulo="Rechazar y pedir alternativa" variante="secundario" onPress={onRechazar}
          deshabilitado={deshabilitada || enviando} estilo={estilos.boton}
          pista="Abre una hoja para escribir el motivo del rechazo" />
      </View>
    </View>
  );
}

const estilos = StyleSheet.create({
  barra: {
    backgroundColor: color.panel2,
    borderTopWidth: 1,
    borderTopColor: color.linea,
    paddingHorizontal: espacio.l,
    paddingTop: espacio.m,
    gap: espacio.s,
  },
  botones: { flexDirection: 'row', flexWrap: 'wrap', gap: espacio.s },
  boton: { flexGrow: 1, flexBasis: 160 },
  bloqueo: { color: color.alerta, fontSize: 14, lineHeight: 20 },
});
