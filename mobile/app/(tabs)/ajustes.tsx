/** Ajustes y primera configuración: servidor, token opcional y nombre del operador. */
import { router, useLocalSearchParams } from 'expo-router';
import React, { useState } from 'react';
import { KeyboardAvoidingView, Platform, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';

import { crearCliente, ErrorApi, normalizarUrl } from '@/api/cliente';
import type { Salud } from '@/api/tipos';
import { borrarCopias } from '@/almacen';
import { Aviso, Boton, Seccion } from '@/componentes/base';
import { useApp } from '@/estado/ContextoApp';
import { color, espacio, radio, TACTIL, texto } from '@/tema';

const MAX_OPERADOR = 80;

export default function Ajustes() {
  const { ajustes, guardarAjustes, configurado } = useApp();
  const { primera } = useLocalSearchParams<{ primera?: string }>();
  const [url, setUrl] = useState(ajustes.url);
  const [token, setToken] = useState(ajustes.token);
  const [operador, setOperador] = useState(ajustes.operador);
  const [probando, setProbando] = useState(false);
  const [salud, setSalud] = useState<Salud | null>(null);
  const [error, setError] = useState<ErrorApi | null>(null);
  const [guardado, setGuardado] = useState(false);

  const urlValida = normalizarUrl(url);

  const probar = async () => {
    setProbando(true);
    setSalud(null);
    setError(null);
    try {
      setSalud(await crearCliente({ url, token: token || null, reintentos: 0, tiempoLimiteMs: 8000 }).salud());
    } catch (e) {
      setError(e instanceof ErrorApi ? e : new ErrorApi('servidor', String(e), 'Revisa la URL.'));
    } finally {
      setProbando(false);
    }
  };

  const guardar = async () => {
    await guardarAjustes({ url: urlValida ?? url, token, operador });
    setGuardado(true);
    if (primera || !configurado) router.navigate('/');
  };

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView contentContainerStyle={estilos.contenido} keyboardShouldPersistTaps="handled">
        {!configurado ? (
          <Aviso tipo="info" titulo="Conecta la app con el servidor de ElectriAgent"
            detalle="En la PC ejecuta: python servidor.py --host 0.0.0.0. Luego escribe aquí la dirección IP de la PC en la misma red Wi-Fi." />
        ) : null}

        <Seccion titulo="Servidor">
          <Campo etiqueta="URL del servidor" valor={url} onCambio={(t) => { setUrl(t); setSalud(null); setGuardado(false); }}
            placeholder="http://192.168.1.20:8000" teclado="url"
            ayuda={url && !urlValida ? 'Escribe una dirección como http://192.168.1.20:8000' : undefined} />
          <Campo etiqueta="Token de acceso (opcional)" valor={token} onCambio={(t) => { setToken(t); setSalud(null); setGuardado(false); }}
            placeholder="Solo si el servidor usa ELECTRIAGENT_TOKEN" secreto />
          <Boton titulo="Probar conexión" variante="secundario" onPress={probar} cargando={probando} deshabilitado={!urlValida} />
          {salud ? (
            <Aviso tipo="ok" titulo="Conexión correcta"
              detalle={`API ${salud.version_api} · modelo ${salud.version_modelo}`} />
          ) : null}
          {error ? <Aviso tipo="error" titulo={error.message} detalle={error.sugerencia} /> : null}
        </Seccion>

        <Seccion titulo="Operador">
          <Campo etiqueta="Tu nombre" valor={operador} onCambio={(t) => { setOperador(t); setGuardado(false); }}
            placeholder="Ej.: Ing. Laura Méndez" maximo={MAX_OPERADOR} autoCap
            ayuda="Firma cada aprobación o rechazo en la bitácora (ISO/IEC 5338). Es el único dato personal que guarda la app." />
        </Seccion>

        <Boton titulo={guardado ? 'Ajustes guardados' : 'Guardar ajustes'} onPress={guardar} deshabilitado={!urlValida} />

        <Seccion titulo="Datos en este teléfono">
          <Text style={texto.sub}>
            La URL y el token se guardan cifrados en el teléfono. La app conserva una copia de tus últimas 10 sesiones
            para consultarlas sin conexión, en solo lectura. Nunca guarda decisiones para enviarlas después.
          </Text>
          <Boton titulo="Borrar copias de sesiones" variante="secundario" onPress={() => { borrarCopias(); }} />
        </Seccion>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

function Campo({ etiqueta, valor, onCambio, placeholder, ayuda, secreto, teclado, maximo, autoCap }: {
  etiqueta: string; valor: string; onCambio: (t: string) => void; placeholder?: string; ayuda?: string;
  secreto?: boolean; teclado?: 'url' | 'default'; maximo?: number; autoCap?: boolean;
}) {
  return (
    <View style={{ gap: espacio.xs }}>
      <Text style={texto.sub}>{etiqueta}</Text>
      <TextInput
        value={valor}
        onChangeText={onCambio}
        placeholder={placeholder}
        placeholderTextColor={color.suave}
        secureTextEntry={secreto}
        keyboardType={teclado === 'url' ? 'url' : 'default'}
        autoCapitalize={autoCap ? 'words' : 'none'}
        autoCorrect={false}
        maxLength={maximo}
        accessibilityLabel={etiqueta}
        accessibilityHint={ayuda}
        style={estilos.campo}
      />
      {ayuda ? <Text style={texto.sub}>{ayuda}</Text> : null}
    </View>
  );
}

const estilos = StyleSheet.create({
  contenido: { padding: espacio.l, gap: espacio.l, paddingBottom: espacio.xxl },
  campo: {
    minHeight: TACTIL,
    borderWidth: 1,
    borderColor: color.linea,
    borderRadius: radio.s,
    paddingHorizontal: espacio.m,
    color: color.texto,
    fontSize: 16,
    backgroundColor: color.fondo,
  },
});
