/** Pantalla principal: la propuesta y la decisión del operador. */
import { router } from 'expo-router';
import React, { useState } from 'react';
import { View } from 'react-native';

import { ErrorApi } from '@/api/cliente';
import { Aviso, Boton, Vacio } from '@/componentes/base';
import { TarjetaPropuesta } from '@/componentes/TarjetaPropuesta';
import { useApp } from '@/estado/ContextoApp';
import { espacio } from '@/tema';

export default function PantallaPropuesta() {
  const { sesion, origen, enLinea, ajustes, decidir, recargar } = useApp();
  const [recargando, setRecargando] = useState(false);
  const [errorRecarga, setErrorRecarga] = useState<ErrorApi | null>(null);

  if (!sesion) {
    return (
      <Vacio titulo="Todavía no hay un análisis" detalle="Elige el periodo y la condición de la red para que los agentes preparen una propuesta.">
        <Boton titulo="Analizar consumo" onPress={() => router.navigate('/')} />
      </Vacio>
    );
  }

  const intentarRecarga = async () => {
    setRecargando(true);
    setErrorRecarga(null);
    try {
      await recargar();
    } catch (e) {
      setErrorRecarga(e instanceof ErrorApi ? e : null);
    } finally {
      setRecargando(false);
    }
  };

  const onDecidir = async (aprobado: boolean, motivo: string) => {
    const r = await decidir(aprobado, motivo);
    if (r.ok && aprobado) router.push('/resultado');
    return r;
  };

  const encabezado = (
    <>
      {origen === 'copia_local' ? (
        <Aviso tipo="info" titulo="Copia guardada en este teléfono (solo lectura)"
          detalle="Puede no reflejar el estado actual. Recárgala desde el servidor para decidir.">
          <Boton titulo="Recargar desde el servidor" variante="secundario" onPress={intentarRecarga}
            cargando={recargando} deshabilitado={!enLinea} />
        </Aviso>
      ) : null}
      {errorRecarga ? <Aviso tipo="error" titulo={errorRecarga.message} detalle={errorRecarga.sugerencia} /> : null}
    </>
  );

  const pie = (
    <View style={{ gap: espacio.s }}>
      <Boton titulo="Ver gráfica y evidencia" variante="secundario" onPress={() => router.push('/detalle')} />
      <Boton titulo="Ver recorrido de los agentes" variante="secundario" onPress={() => router.push('/recorrido')} />
      {sesion.terminado ? <Boton titulo="Ver resultado y compartir" onPress={() => router.push('/resultado')} /> : null}
    </View>
  );

  return (
    <TarjetaPropuesta
      vista={sesion}
      enLinea={enLinea}
      soloLectura={origen === 'copia_local'}
      operador={ajustes.operador}
      onDecidir={onDecidir}
      encabezado={encabezado}
      pie={pie}
    />
  );
}
