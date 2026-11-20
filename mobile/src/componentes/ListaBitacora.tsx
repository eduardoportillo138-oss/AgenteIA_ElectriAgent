/** Lo que dijo cada agente durante la sesión (consola del bus de coordinación). */
import React from 'react';
import { StyleSheet, Text, View } from 'react-native';

import type { IdNodo, MensajeConsola } from '../api/tipos';
import { color, espacio, fuente, radio, texto } from '../tema';

const FONDO: Partial<Record<IdNodo, string>> = { C: color.azul, D: color.verde, E: color.verde, F: color.verde };

export function ListaBitacora({ consola }: { consola: MensajeConsola[] }) {
  if (!consola.length) return <Text style={texto.sub}>Sin mensajes.</Text>;
  return (
    <View style={{ gap: espacio.s }}>
      {consola.map((c, i) => (
        <View key={`${i}-${c.nodo}`} style={estilos.fila} accessible accessibilityLabel={`${c.agente} (${c.nodo}): ${c.texto}`}>
          <View style={[estilos.nodo, { backgroundColor: FONDO[c.nodo] ?? color.claro }]}>
            <Text style={[estilos.letra, c.nodo === 'C' && { color: '#FFFFFF' }]}>{c.nodo}</Text>
          </View>
          <Text style={[texto.sub, { color: color.texto, flex: 1 }]}>{c.texto}</Text>
        </View>
      ))}
    </View>
  );
}

const estilos = StyleSheet.create({
  fila: { flexDirection: 'row', gap: espacio.s, alignItems: 'flex-start' },
  nodo: { width: 28, borderRadius: radio.s, alignItems: 'center', paddingVertical: 1 },
  letra: { fontFamily: fuente.titulo, fontSize: 15, color: color.fondo },
});
