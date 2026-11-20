import React from 'react';
import { Pressable, StyleSheet, Text } from 'react-native';

import { color, espacio, fuente, radio, TACTIL } from '../tema';

/** Opción seleccionable tipo pastilla (periodos, condiciones, alimentadores). */
export function Chip({ texto, seleccionado, onPress, etiquetaAccesible, testID }: {
  texto: string; seleccionado: boolean; onPress: () => void; etiquetaAccesible?: string; testID?: string;
}) {
  return (
    <Pressable
      testID={testID}
      accessibilityRole="radio"
      accessibilityState={{ selected: seleccionado, checked: seleccionado }}
      aria-checked={seleccionado}
      accessibilityLabel={etiquetaAccesible ?? texto}
      onPress={onPress}
      hitSlop={4}
      style={({ pressed }) => [estilos.chip, seleccionado && estilos.activo, pressed && { opacity: 0.85 }]}
    >
      <Text style={[estilos.texto, seleccionado && estilos.textoActivo]}>{texto}</Text>
    </Pressable>
  );
}

const estilos = StyleSheet.create({
  chip: {
    minHeight: TACTIL,
    minWidth: 72,
    paddingHorizontal: espacio.l,
    justifyContent: 'center',
    alignItems: 'center',
    borderRadius: radio.pastilla,
    borderWidth: 1,
    borderColor: color.linea,
    backgroundColor: color.panel,
  },
  activo: { backgroundColor: color.verde, borderColor: color.verde },
  texto: { fontFamily: fuente.rotulo, fontSize: 17, color: color.texto },
  textoActivo: { fontFamily: fuente.titulo, color: color.fondo },
});
