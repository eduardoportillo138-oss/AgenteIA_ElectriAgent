import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import React from 'react';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import type { Vista } from '../src/api/tipos';
import { TarjetaPropuesta } from '../src/componentes/TarjetaPropuesta';
import enEspera from './fixtures/vista_en_espera.json';
import fallaA1 from './fixtures/vista_falla_a1.json';

// Las respuestas reales del servidor deben encajar en los tipos de la app (contrato)
const VISTA: Vista = enEspera as Vista;
const VISTA_FALLA: Vista = fallaA1 as Vista;

const METRICAS = { frame: { x: 0, y: 0, width: 390, height: 844 }, insets: { top: 0, left: 0, right: 0, bottom: 0 } };

async function montar(props: Partial<React.ComponentProps<typeof TarjetaPropuesta>> = {}) {
  const onDecidir = jest.fn().mockResolvedValue({ ok: true });
  await render(
    <SafeAreaProvider initialMetrics={METRICAS}>
      <TarjetaPropuesta vista={VISTA} enLinea soloLectura={false} operador="Ing. Pruebas" onDecidir={onDecidir} {...props} />
    </SafeAreaProvider>,
  );
  return onDecidir;
}

describe('TarjetaPropuesta', () => {
  it('muestra la propuesta en espera con sus acciones y la justificación', async () => {
    await montar();
    expect(screen.getByText('Falta tu decisión')).toBeTruthy();
    expect(screen.getByText(/Desplazamiento de carga flexible de punta a base/)).toBeTruthy();
    expect(screen.getByText('Por qué esta recomendación')).toBeTruthy();
  });

  it('el rechazo sin motivo no se envía', async () => {
    const onDecidir = await montar();
    await fireEvent.press(screen.getByTestId('boton-rechazar'));
    await fireEvent.press(screen.getByTestId('confirmar-decision'));
    expect(await screen.findByTestId('aviso-motivo')).toBeTruthy();
    await fireEvent.changeText(screen.getByTestId('campo-motivo'), '   ');
    await fireEvent.press(screen.getByTestId('confirmar-decision'));
    expect(onDecidir).not.toHaveBeenCalled();
  });

  it('el rechazo con motivo se envía con el texto escrito', async () => {
    const onDecidir = await montar();
    await fireEvent.press(screen.getByTestId('boton-rechazar'));
    await fireEvent.changeText(screen.getByTestId('campo-motivo'), 'Muy invasiva para el bombeo');
    await fireEvent.press(screen.getByTestId('confirmar-decision'));
    await waitFor(() => expect(onDecidir).toHaveBeenCalledWith(false, 'Muy invasiva para el bombeo'));
  });

  it('aprobar pide confirmación antes de enviar', async () => {
    const onDecidir = await montar();
    await fireEvent.press(screen.getByTestId('boton-aprobar'));
    expect(onDecidir).not.toHaveBeenCalled();
    expect(screen.getByText('¿Apruebas la propuesta S2?')).toBeTruthy();
    expect(screen.getByText('Firma: Ing. Pruebas')).toBeTruthy();
    await fireEvent.press(screen.getByTestId('confirmar-decision'));
    await waitFor(() => expect(onDecidir).toHaveBeenCalledWith(true, ''));
  });

  it('muestra el recorte parcial, los alimentadores y la robustez cuando existen', async () => {
    await montar({ vista: VISTA_FALLA });
    expect(screen.getByText('Recorte parcial por carga')).toBeTruthy();
    expect(screen.getByText('Parque industrial')).toBeTruthy();
    expect(screen.getByText('Distribución por alimentador')).toBeTruthy();
    expect(screen.getByText('Robusta ante P90')).toBeTruthy();
    expect(screen.getByText('La demanda prevista supera la capacidad disponible')).toBeTruthy();
  });

  it('funciona con un servidor sin datos de distribución (campos opcionales)', async () => {
    const p = VISTA.propuesta!;
    const antigua: Vista = {
      ...VISTA,
      propuesta: { ...p, robusto: undefined, alimentadores: undefined, recorte_por_carga: undefined, metricas_p90: undefined,
        serie: { ...p.serie, bandas: undefined, por_alimentador: undefined } },
    };
    await montar({ vista: antigua });
    expect(screen.queryByText('Distribución por alimentador')).toBeNull();
    expect(screen.queryByText('Robusta ante P90')).toBeNull();
    expect(screen.getByTestId('boton-aprobar')).toBeTruthy();
  });
});
