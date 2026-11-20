/** Resultado de la sesión (aprobada o escalada) y exportación del reporte y la bitácora. */
import { router } from 'expo-router';
import React, { useState } from 'react';
import { ScrollView, StyleSheet, Text, View } from 'react-native';

import { ErrorApi } from '@/api/cliente';
import { Aviso, Boton, Seccion, Vacio } from '@/componentes/base';
import { Cifra } from '@/componentes/Cifra';
import { compartirJson } from '@/compartir';
import { useApp } from '@/estado/ContextoApp';
import { fecha, kwh, mxn, pct } from '@/formato';
import { color, espacio, fuente, radio, texto } from '@/tema';

export default function Resultado() {
  const { sesion, cliente, enLinea, origen } = useApp();
  const [ocupado, setOcupado] = useState<'reporte' | 'bitacora' | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!sesion) {
    return (
      <Vacio titulo="Todavía no hay un resultado">
        <Boton titulo="Analizar consumo" onPress={() => router.navigate('/')} />
      </Vacio>
    );
  }

  const aprobada = sesion.estado === 'aprobada';
  const escalada = sesion.estado === 'escalada';
  const p = sesion.propuesta;
  const ultima = sesion.historial[sesion.historial.length - 1];
  const puedeCompartir = enLinea && origen === 'servidor' && !!cliente;

  const compartir = async (tipo: 'reporte' | 'bitacora') => {
    if (!cliente) return;
    setOcupado(tipo);
    setError(null);
    try {
      const datos = tipo === 'reporte' ? await cliente.reporte(sesion.id) : await cliente.bitacora(sesion.id);
      await compartirJson(
        tipo === 'reporte' ? `reporte_electriagent_${sesion.id}.json` : `bitacora_${sesion.id}.json`,
        datos,
        tipo === 'reporte' ? 'Reporte de ElectriAgent' : 'Bitácora de trazabilidad',
      );
    } catch (e) {
      setError(e instanceof ErrorApi ? `${e.message} ${e.sugerencia}` : String((e as Error).message ?? e));
    } finally {
      setOcupado(null);
    }
  };

  return (
    <ScrollView contentContainerStyle={estilos.contenido}>
      <View style={[estilos.estado, { borderColor: aprobada ? color.verde : escalada ? color.alerta : color.rayo }]}
        accessibilityRole="summary">
        <Text style={[estilos.estadoTexto, { color: aprobada ? color.verde : escalada ? color.alerta : color.rayo }]}>
          {aprobada ? 'Recomendación aprobada' : escalada ? 'Caso escalado al operador' : 'Falta tu decisión'}
        </Text>
        {p && aprobada ? <Text style={texto.h2}>{`${p.nombre} (${p.escenario})`}</Text> : null}
        {ultima ? (
          <Text style={texto.sub}>
            {`${ultima.aprobado ? 'Aprobada' : 'Rechazada'} el ${fecha(ultima.fecha)}${ultima.operador ? ` por ${ultima.operador}` : ''}.`}
          </Text>
        ) : null}
        {escalada ? (sesion.motivo_escalamiento ?? []).map((m) => <Text key={m} style={texto.cuerpo}>{m}</Text>) : null}
        {aprobada ? (
          <Text style={texto.cuerpo}>
            ElectriAgent no ejecuta acciones: comparte el reporte con las personas responsables de operar la red.
          </Text>
        ) : null}
      </View>

      {p && aprobada ? (
        <Seccion titulo="Lo que se aprobó">
          {p.acciones.filter((a) => !a.startsWith('  ·')).map((a) => <Text key={a} style={texto.cuerpo}>{`• ${a}`}</Text>)}
          <View style={estilos.cifras}>
            <Cifra etiqueta="Costo del día" valor={mxn(p.metricas.costo_mxn)} />
            <Cifra etiqueta="Renovables" valor={pct(p.metricas.renovable_pct)} />
            {(p.metricas.recorte_kwh ?? 0) > 0 ? <Cifra etiqueta="Recorte parcial" valor={kwh(p.metricas.recorte_kwh)} /> : null}
            {p.robusto != null ? <Cifra etiqueta="Robustez P90" valor={p.robusto ? 'Robusta' : 'No robusta'} /> : null}
          </View>
        </Seccion>
      ) : null}

      <Seccion titulo="Compartir (JSON)">
        {!puedeCompartir ? (
          <Aviso tipo="info" titulo="Necesitas conexión con el servidor para descargar el reporte y la bitácora" />
        ) : null}
        <Boton titulo="Compartir reporte" onPress={() => compartir('reporte')} cargando={ocupado === 'reporte'}
          deshabilitado={!puedeCompartir || !sesion.terminado || ocupado !== null} />
        <Boton titulo="Compartir bitácora" variante="secundario" onPress={() => compartir('bitacora')}
          cargando={ocupado === 'bitacora'} deshabilitado={!puedeCompartir || ocupado !== null} />
        {error ? <Aviso tipo="error" titulo={error} /> : null}
      </Seccion>

      <Boton titulo="Analizar otro periodo" variante="secundario" onPress={() => router.navigate('/')} />
    </ScrollView>
  );
}

const estilos = StyleSheet.create({
  contenido: { padding: espacio.l, gap: espacio.l, paddingBottom: espacio.xxl },
  estado: { borderWidth: 2, borderRadius: radio.m, padding: espacio.l, gap: espacio.s, backgroundColor: color.panel },
  estadoTexto: { fontFamily: fuente.titulo, fontSize: 18 },
  cifras: { flexDirection: 'row', flexWrap: 'wrap', gap: espacio.m },
});
