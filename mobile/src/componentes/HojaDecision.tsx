/**
 * Hoja inferior que confirma una decisión antes de enviarla.
 * - Aprobar: muestra qué se aprueba y quién firma.
 * - Rechazar: exige el motivo; sin motivo no se envía.
 * Usa un Modal propio (Alert no permite botones en la web).
 */
import React, { useState } from 'react';
import { KeyboardAvoidingView, Modal, Platform, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import { color, espacio, radio, texto } from '../tema';
import { Boton } from './base';

export type ModoHoja = 'aprobar' | 'rechazar' | null;
export const MAX_MOTIVO = 500;

interface PropsHoja {
  modo: ModoHoja;
  escenario: string;
  operador: string;
  enviando: boolean;
  error: string | null;
  onConfirmar: (motivo: string) => void;
  onCancelar: () => void;
}

export function HojaDecision(props: PropsHoja) {
  const { modo, onCancelar } = props;
  return (
    <Modal visible={modo !== null} transparent animationType="slide" onRequestClose={onCancelar}>
      <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={estilos.fondo}>
        <Pressable style={StyleSheet.absoluteFill} onPress={onCancelar} accessibilityLabel="Cerrar sin decidir" />
        {/* key: cada vez que se abre, el formulario empieza vacío */}
        {modo ? <Cuerpo key={modo} {...props} modo={modo} /> : null}
      </KeyboardAvoidingView>
    </Modal>
  );
}

function Cuerpo({ modo, escenario, operador, enviando, error, onConfirmar, onCancelar }: PropsHoja & { modo: 'aprobar' | 'rechazar' }) {
  const [motivo, setMotivo] = useState('');
  const [aviso, setAviso] = useState<string | null>(null);

  const confirmar = () => {
    if (modo === 'rechazar' && !motivo.trim()) {
      setAviso('Escribe qué no te convence: el agente de meta lo usa para replantear la propuesta.');
      return;
    }
    onConfirmar(motivo.trim());
  };

  return (
    <View style={estilos.hoja} accessibilityViewIsModal>
      <Text style={texto.h2} accessibilityRole="header">
        {modo === 'aprobar' ? `¿Apruebas la propuesta ${escenario}?` : `Rechazar la propuesta ${escenario}`}
      </Text>
      {modo === 'aprobar' ? (
        <Text style={texto.cuerpo}>
          Quedará registrada con tu firma en la bitácora (ISO/IEC 5338). ElectriAgent no enciende ni apaga
          equipos: el reporte indica qué hacer y la operación la realizan las personas responsables.
        </Text>
      ) : (
        <>
          <Text style={texto.cuerpo}>El agente de meta buscará una alternativa menos invasiva con tu motivo.</Text>
          <TextInput
            testID="campo-motivo"
            value={motivo}
            onChangeText={(t) => { setMotivo(t); if (aviso) setAviso(null); }}
            placeholder="Ej.: no quiero mover el bombeo de agua en la madrugada"
            placeholderTextColor={color.suave}
            multiline
            maxLength={MAX_MOTIVO}
            accessibilityLabel="Motivo del rechazo (obligatorio)"
            style={estilos.campo}
          />
        </>
      )}
      <Text style={texto.sub}>{`Firma: ${operador}`}</Text>
      {aviso ? <Text style={estilos.error} accessibilityRole="alert" testID="aviso-motivo">{aviso}</Text> : null}
      {error ? <Text style={estilos.error} accessibilityRole="alert">{error}</Text> : null}
      <View style={estilos.botones}>
        <Boton testID="confirmar-decision" titulo={modo === 'aprobar' ? 'Sí, aprobar' : 'Enviar rechazo'}
          variante={modo === 'aprobar' ? 'aprobar' : 'peligro'} onPress={confirmar} cargando={enviando}
          estilo={estilos.boton} />
        <Boton titulo="Cancelar" variante="secundario" onPress={onCancelar} deshabilitado={enviando} estilo={estilos.boton} />
      </View>
    </View>
  );
}

const estilos = StyleSheet.create({
  fondo: { flex: 1, justifyContent: 'flex-end', backgroundColor: 'rgba(4,16,20,0.6)' },
  hoja: {
    backgroundColor: color.panel,
    borderTopLeftRadius: radio.m,
    borderTopRightRadius: radio.m,
    padding: espacio.xl,
    gap: espacio.m,
    borderTopWidth: 2,
    borderColor: color.rayo,
  },
  campo: {
    minHeight: 96,
    borderWidth: 1,
    borderColor: color.linea,
    borderRadius: radio.s,
    padding: espacio.m,
    color: color.texto,
    fontSize: 16,
    textAlignVertical: 'top',
    backgroundColor: color.fondo,
  },
  error: { color: color.alerta, fontSize: 15, lineHeight: 21 },
  botones: { flexDirection: 'row', flexWrap: 'wrap', gap: espacio.s },
  boton: { flexGrow: 1, flexBasis: 140 },
});
