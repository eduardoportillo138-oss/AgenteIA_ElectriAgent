import React from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { color, espacio, texto } from '../tema';

/**
 * Métrica con su etiqueta y, opcionalmente, la diferencia frente a la operación actual.
 * `mejorSiMenor` decide si una diferencia negativa se pinta en verde (costo) o en rojo.
 */
export function Cifra({ etiqueta, valor, nota, diferencia, mejorSiMenor }: {
  etiqueta: string;
  valor: string;
  nota?: string;
  diferencia?: { valor: number; texto: string } | null;
  mejorSiMenor?: boolean;
}) {
  let tono: string = color.suave;
  let textoDif: string | null = null;
  if (diferencia) {
    if (Math.abs(diferencia.valor) < 0.05) {
      textoDif = 'igual que hoy';
    } else {
      const mejor = mejorSiMenor ? diferencia.valor < 0 : diferencia.valor > 0;
      tono = mejor ? color.verde : color.alerta;
      textoDif = `${diferencia.texto} frente a hoy`;
    }
  }
  return (
    <View style={estilos.cifra} accessible accessibilityLabel={`${etiqueta}: ${valor}${textoDif ? `, ${textoDif}` : ''}${nota ? `, ${nota}` : ''}`}>
      <Text style={estilos.etiqueta}>{etiqueta}</Text>
      <Text style={texto.cifra}>{valor}</Text>
      {textoDif ? <Text style={[estilos.nota, { color: tono }]}>{textoDif}</Text> : null}
      {nota ? <Text style={estilos.nota}>{nota}</Text> : null}
    </View>
  );
}

const estilos = StyleSheet.create({
  cifra: { borderLeftWidth: 3, borderLeftColor: color.linea, paddingLeft: espacio.m, minWidth: 140, flexGrow: 1, flexBasis: '45%' },
  etiqueta: { fontSize: 13, lineHeight: 18, color: color.suave },
  nota: { fontSize: 13, lineHeight: 18, color: color.suave },
});
