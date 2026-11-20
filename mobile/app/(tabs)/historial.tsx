/**
 * Historial: sesiones en memoria del servidor (/api/sesiones) y copias locales.
 * Si una sesión expiró en el servidor (404), se abre su copia local en solo lectura.
 */
import { router, useFocusEffect } from 'expo-router';
import React, { useCallback, useState } from 'react';
import { FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';

import { ErrorApi } from '@/api/cliente';
import type { EstadoSesion, ResumenSesion } from '@/api/tipos';
import { leerCopias } from '@/almacen';
import { Aviso, Insignia, Vacio } from '@/componentes/base';
import { useApp } from '@/estado/ContextoApp';
import { fecha } from '@/formato';
import { color, espacio, fuente, radio, TACTIL, texto } from '@/tema';

interface Fila extends ResumenSesion {
  local: boolean;
  enServidor: boolean;
}

const ESTADO: Record<EstadoSesion, { texto: string; tono: 'ok' | 'riesgo' | 'neutral' | 'espera' }> = {
  en_espera: { texto: 'Falta tu decisión', tono: 'espera' },
  aprobada: { texto: 'Aprobada', tono: 'ok' },
  escalada: { texto: 'Escalada', tono: 'riesgo' },
  rechazada: { texto: 'Rechazada', tono: 'riesgo' },
  procesando: { texto: 'Procesando', tono: 'neutral' },
};

export default function Historial() {
  const { cliente, abrirSesion, enLinea } = useApp();
  const [filas, setFilas] = useState<Fila[]>([]);
  const [cargando, setCargando] = useState(false);
  const [error, setError] = useState<ErrorApi | null>(null);
  const [abriendo, setAbriendo] = useState<string | null>(null);

  const cargar = useCallback(async () => {
    setCargando(true);
    setError(null);
    const copias = await leerCopias();
    let servidor: ResumenSesion[] = [];
    try {
      if (cliente) servidor = await cliente.sesiones();
    } catch (e) {
      setError(e instanceof ErrorApi ? e : null);
    }
    const ids = new Set(servidor.map((s) => s.id));
    const locales = new Set(copias.map((c) => c.vista.id));
    const soloLocales: Fila[] = copias.filter((c) => !ids.has(c.vista.id)).map((c) => ({
      id: c.vista.id,
      fecha: c.guardada,
      periodo: c.vista.periodo,
      condicion: { id: c.vista.condicion.id, nombre: c.vista.condicion.nombre },
      estado: c.vista.estado,
      escenario: c.vista.propuesta?.escenario ?? null,
      local: true,
      enServidor: false,
    }));
    setFilas([...servidor.map((s) => ({ ...s, local: locales.has(s.id), enServidor: true })), ...soloLocales]);
    setCargando(false);
  }, [cliente]);

  useFocusEffect(useCallback(() => {
    cargar();
  }, [cargar]));

  const abrir = async (f: Fila) => {
    setAbriendo(f.id);
    setError(null);
    try {
      const origen = await abrirSesion(f.id);
      router.navigate(origen === 'servidor' && f.estado !== 'en_espera' ? '/resultado' : '/propuesta');
    } catch (e) {
      setError(e instanceof ErrorApi ? e : null);
    } finally {
      setAbriendo(null);
    }
  };

  return (
    <FlatList
      data={filas}
      keyExtractor={(f) => f.id}
      contentContainerStyle={estilos.contenido}
      refreshControl={<RefreshControl refreshing={cargando} onRefresh={cargar} tintColor={color.cian} />}
      ListHeaderComponent={
        <View style={{ gap: espacio.m }}>
          {!enLinea ? <Aviso tipo="error" titulo="Sin conexión" detalle="Solo se muestran las copias guardadas en este teléfono." /> : null}
          {error ? <Aviso tipo="error" titulo={error.message} detalle={error.sugerencia} /> : null}
          <Text style={texto.sub}>El servidor guarda hasta 100 sesiones en memoria; al reiniciarse se pierden. Las marcadas “En este teléfono” se pueden consultar sin conexión.</Text>
        </View>
      }
      ListEmptyComponent={cargando ? undefined : <Vacio titulo="Sin sesiones" detalle="Los análisis que hagas aparecerán aquí." />}
      renderItem={({ item: f }) => {
        const e = ESTADO[f.estado] ?? ESTADO.procesando;
        return (
          <Pressable onPress={() => abrir(f)} accessibilityRole="button"
            accessibilityLabel={`${f.condicion.nombre}, ${f.periodo.nombre}, ${e.texto}${f.escenario ? `, escenario ${f.escenario}` : ''}, ${fecha(f.fecha)}`}
            accessibilityState={{ busy: abriendo === f.id }}
            style={({ pressed }) => [estilos.fila, pressed && { opacity: 0.85 }]}>
            <View style={estilos.filaCabeza}>
              <Text style={estilos.titulo}>{f.condicion.nombre}</Text>
              <Insignia texto={e.texto} tono={e.tono} />
            </View>
            <Text style={texto.sub}>
              {`${f.periodo.nombre} · ${fecha(f.fecha)}${f.escenario ? ` · ${f.escenario}` : ''}`}
            </Text>
            <Text style={texto.sub}>
              {f.enServidor ? (f.local ? 'En el servidor y en este teléfono' : 'En el servidor') : 'Solo en este teléfono (expiró en el servidor)'}
            </Text>
          </Pressable>
        );
      }}
      ItemSeparatorComponent={() => <View style={{ height: espacio.s }} />}
    />
  );
}

const estilos = StyleSheet.create({
  contenido: { padding: espacio.l, gap: espacio.m, paddingBottom: espacio.xxl },
  fila: { backgroundColor: color.panel, borderRadius: radio.m, padding: espacio.l, minHeight: TACTIL, gap: 4 },
  filaCabeza: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: espacio.s, flexWrap: 'wrap' },
  titulo: { fontFamily: fuente.titulo, fontSize: 18, color: color.texto, flexShrink: 1 },
});
