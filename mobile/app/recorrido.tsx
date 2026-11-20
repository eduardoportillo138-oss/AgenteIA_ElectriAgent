/** Recorrido de los agentes sobre el grafo A–H con el costo de coordinación. */
import { router } from 'expo-router';
import React from 'react';
import { ScrollView, StyleSheet, Text, View } from 'react-native';

import { Boton, Vacio } from '@/componentes/base';
import { GrafoAgentes } from '@/componentes/GrafoAgentes';
import { useApp } from '@/estado/ContextoApp';
import { num } from '@/formato';
import { color, espacio, texto } from '@/tema';

const ESTADO: Record<string, string> = {
  en_espera: 'Falta tu decisión: la propuesta está detenida en Aprobación humana (G).',
  aprobada: 'Aprobaste la recomendación. El reporte está listo.',
  escalada: 'El agente de meta escaló el caso: necesita tu criterio como operador.',
  rechazada: 'Rechazaste la recomendación.',
  procesando: 'Los agentes están trabajando…',
};

export default function Recorrido() {
  const { sesion, config, reducirMovimiento } = useApp();
  if (!sesion) {
    return (
      <Vacio titulo="Todavía no hay un análisis" detalle="Elige un periodo y pulsa “Analizar consumo”.">
        <Boton titulo="Ir a Analizar" onPress={() => router.navigate('/')} />
      </Vacio>
    );
  }
  const aristas = config?.grafo.aristas ?? [];
  return (
    <ScrollView contentContainerStyle={estilos.contenido}>
      <Text style={[texto.cuerpo, sesion.estado === 'en_espera' && { color: color.rayo }]}
        accessibilityRole="summary" accessibilityLiveRegion="polite">
        {ESTADO[sesion.estado]}
      </Text>
      <GrafoAgentes aristas={aristas} recorrido={sesion.coordinacion.recorrido} estado={sesion.estado}
        costo={sesion.coordinacion.costo} reducirMovimiento={reducirMovimiento} />
      <Text style={[texto.sub, { textAlign: 'center' }]}>
        {`${sesion.coordinacion.mensajes} mensajes entre agentes. Costo de coordinación: ${num(sesion.coordinacion.costo)} (suma de los pesos recorridos).`}
      </Text>
      <View style={{ gap: espacio.s }}>
        {sesion.propuesta && sesion.estado === 'en_espera' ? (
          <Boton titulo="Revisar propuesta" onPress={() => router.navigate('/propuesta')} />
        ) : null}
        {sesion.terminado ? <Boton titulo="Ver resultado" onPress={() => router.push('/resultado')} /> : null}
      </View>
    </ScrollView>
  );
}

const estilos = StyleSheet.create({
  contenido: { padding: espacio.l, gap: espacio.l, paddingBottom: espacio.xxl },
});
