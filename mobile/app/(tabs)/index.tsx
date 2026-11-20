/** Analizar: periodo, condición prevista de la red y "Analizar consumo". */
import { Redirect, router } from 'expo-router';
import React, { useEffect, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';

import { ErrorApi } from '@/api/cliente';
import { Aviso, Boton, Seccion } from '@/componentes/base';
import { Chip } from '@/componentes/Chip';
import { useApp } from '@/estado/ContextoApp';
import { color, espacio, fuente, radio, TACTIL, texto } from '@/tema';

export default function Analizar() {
  const { configurado, config, errorConfig, cargarConfig, analizar, enLinea } = useApp();
  const [periodo, setPeriodo] = useState('1m');
  const [condicion, setCondicion] = useState('normal');
  const [alimentador, setAlimentador] = useState('A1');
  const [crecimiento, setCrecimiento] = useState('15');
  const [enviando, setEnviando] = useState(false);
  const [error, setError] = useState<ErrorApi | null>(null);

  useEffect(() => {
    if (configurado && !config) cargarConfig();
  }, [configurado, config, cargarConfig]);

  if (!configurado) return <Redirect href="/ajustes?primera=1" />;

  const elegida = config?.condiciones.find((c) => c.id === condicion);
  const crecimientoNum = Number(crecimiento.replace(',', '.'));
  const crecimientoValido = Number.isFinite(crecimientoNum) && crecimientoNum >= -50 && crecimientoNum <= 200;

  const enviar = async () => {
    setEnviando(true);
    setError(null);
    try {
      await analizar({
        periodo,
        condicion,
        ...(condicion === 'falla_alimentador' ? { alimentador } : {}),
        ...(condicion === 'crecimiento_demanda' ? { crecimiento_pct: crecimientoNum } : {}),
      });
      router.push('/recorrido');
    } catch (e) {
      setError(e instanceof ErrorApi ? e : new ErrorApi('servidor', String(e), 'Intenta de nuevo.'));
    } finally {
      setEnviando(false);
    }
  };

  return (
    <ScrollView contentContainerStyle={estilos.contenido}>
      <Text style={texto.h1} accessibilityRole="header">¿Qué periodo quieres analizar?</Text>
      {!enLinea ? <Aviso tipo="error" titulo="Sin conexión" detalle="Para analizar necesitas conexión con el servidor. Puedes consultar la última sesión en Propuesta." /> : null}
      {errorConfig && !config ? (
        <Aviso tipo="error" titulo={errorConfig.message} detalle={errorConfig.sugerencia}>
          <Boton titulo="Reintentar" variante="secundario" onPress={cargarConfig} />
        </Aviso>
      ) : null}

      <View style={estilos.chips} accessibilityRole="radiogroup" accessibilityLabel="Periodo de datos IoT">
        {(config?.periodos ?? []).map((p) => (
          <Chip key={p.id} texto={p.nombre} seleccionado={p.id === periodo} onPress={() => setPeriodo(p.id)} />
        ))}
      </View>

      <Seccion titulo="Condición prevista de la red">
        <View accessibilityRole="radiogroup" style={{ gap: espacio.s }}>
          {(config?.condiciones ?? []).map((c) => {
            const activa = c.id === condicion;
            return (
              <Pressable key={c.id} onPress={() => setCondicion(c.id)} accessibilityRole="radio"
                accessibilityState={{ selected: activa, checked: activa }} aria-checked={activa} accessibilityLabel={c.nombre}
                accessibilityHint={c.descripcion}
                style={[estilos.opcion, activa && estilos.opcionActiva]}>
                <View style={[estilos.radio, activa && estilos.radioActivo]} />
                <View style={{ flex: 1 }}>
                  <Text style={[estilos.opcionTexto, activa && { color: color.texto }]}>{c.nombre}</Text>
                  {activa && c.descripcion ? <Text style={texto.sub}>{c.descripcion}</Text> : null}
                </View>
              </Pressable>
            );
          })}
        </View>
        {condicion === 'falla_alimentador' ? (
          <View style={{ gap: espacio.s }}>
            <Text style={texto.sub}>Alimentador en falla</Text>
            <View style={estilos.chips} accessibilityRole="radiogroup">
              {(config?.alimentadores_falla ?? ['A1', 'A2']).map((k) => {
                const a = config?.red.alimentadores?.find((x) => x.clave === k);
                return <Chip key={k} texto={`${k}${a ? ` · ${a.nivel === 'MT' ? 'media tensión' : 'baja tensión'}` : ''}`}
                  seleccionado={k === alimentador} onPress={() => setAlimentador(k)} />;
              })}
            </View>
          </View>
        ) : null}
        {condicion === 'crecimiento_demanda' ? (
          <View style={{ gap: espacio.s }}>
            <Text style={texto.sub}>Crecimiento de la demanda (%)</Text>
            <TextInput value={crecimiento} onChangeText={setCrecimiento} keyboardType="numeric" maxLength={6}
              accessibilityLabel="Crecimiento de la demanda en por ciento" style={estilos.campo} />
            {!crecimientoValido ? <Text style={{ color: color.alerta }}>Escribe un número entre −50 y 200.</Text> : null}
          </View>
        ) : null}
      </Seccion>

      {error ? <Aviso tipo="error" titulo={error.message} detalle={error.sugerencia} /> : null}
      <Boton titulo={enviando ? 'Analizando…' : 'Analizar consumo'} onPress={enviar} cargando={enviando}
        deshabilitado={!config || !enLinea || (condicion === 'crecimiento_demanda' && !crecimientoValido)}
        pista={elegida ? `Condición: ${elegida.nombre}` : undefined} />
      <Text style={texto.sub}>
        ElectriAgent solo analiza y recomienda. No enciende, apaga ni controla equipos ni la red: cada recomendación espera tu aprobación.
      </Text>
    </ScrollView>
  );
}

const estilos = StyleSheet.create({
  contenido: { padding: espacio.l, gap: espacio.l, paddingBottom: espacio.xxl },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: espacio.s },
  opcion: {
    flexDirection: 'row', alignItems: 'flex-start', gap: espacio.m, minHeight: TACTIL,
    paddingVertical: espacio.m, paddingHorizontal: espacio.m, borderRadius: radio.s,
    borderWidth: 1, borderColor: color.linea,
  },
  opcionActiva: { borderColor: color.verde, backgroundColor: color.panel2 },
  opcionTexto: { fontFamily: fuente.rotulo, fontSize: 17, color: color.suave },
  radio: { width: 20, height: 20, borderRadius: 10, borderWidth: 2, borderColor: color.suave, marginTop: 2 },
  radioActivo: { borderColor: color.verde, backgroundColor: color.verde },
  campo: {
    minHeight: TACTIL, borderWidth: 1, borderColor: color.linea, borderRadius: radio.s,
    paddingHorizontal: espacio.m, color: color.texto, fontSize: 16, backgroundColor: color.fondo, maxWidth: 160,
  },
});
