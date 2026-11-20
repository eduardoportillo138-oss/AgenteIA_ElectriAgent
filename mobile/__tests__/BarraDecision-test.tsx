import { fireEvent, render, screen } from '@testing-library/react-native';
import React from 'react';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import type { Vista } from '../src/api/tipos';
import { BarraDecision } from '../src/componentes/BarraDecision';
import { bloqueoDecision, TarjetaPropuesta } from '../src/componentes/TarjetaPropuesta';
import enEspera from './fixtures/vista_en_espera.json';

const METRICAS = { frame: { x: 0, y: 0, width: 390, height: 844 }, insets: { top: 0, left: 0, right: 0, bottom: 0 } };
const envolver = (ui: React.ReactElement) => render(<SafeAreaProvider initialMetrics={METRICAS}>{ui}</SafeAreaProvider>);

describe('Barra de decisión', () => {
  it('sin conexión queda deshabilitada y lo explica', async () => {
    const onAprobar = jest.fn();
    const onRechazar = jest.fn();
    await envolver(<BarraDecision bloqueo={bloqueoDecision(false, false, 'Ing. Pruebas')} onAprobar={onAprobar} onRechazar={onRechazar} />);
    expect(screen.getByTestId('barra-bloqueo').props.children).toMatch('Necesitas conexión con el servidor para decidir');
    expect(screen.getByTestId('boton-aprobar')).toBeDisabled();
    expect(screen.getByTestId('boton-rechazar')).toBeDisabled();
    await fireEvent.press(screen.getByTestId('boton-aprobar'));
    await fireEvent.press(screen.getByTestId('boton-rechazar'));
    expect(onAprobar).not.toHaveBeenCalled();
    expect(onRechazar).not.toHaveBeenCalled();
  });

  it('con conexión y operador se puede decidir', async () => {
    await envolver(<BarraDecision bloqueo={bloqueoDecision(true, false, 'Ing. Pruebas')} onAprobar={jest.fn()} onRechazar={jest.fn()} />);
    expect(screen.queryByTestId('barra-bloqueo')).toBeNull();
    expect(screen.getByTestId('boton-aprobar')).toBeEnabled();
  });

  it('una copia local o la falta de nombre también bloquean la decisión', async () => {
    expect(bloqueoDecision(true, true, 'Ing. Pruebas')).toMatch(/copia guardada/);
    expect(bloqueoDecision(true, false, '  ')).toMatch(/Escribe tu nombre en Ajustes/);
  });

  it('en la propuesta sin conexión no se envía nada', async () => {
    const onDecidir = jest.fn();
    await envolver(<TarjetaPropuesta vista={enEspera as Vista} enLinea={false} soloLectura={false} operador="Ing. Pruebas" onDecidir={onDecidir} />);
    expect(screen.getByTestId('boton-aprobar')).toBeDisabled();
    await fireEvent.press(screen.getByTestId('boton-aprobar'));
    expect(screen.queryByTestId('confirmar-decision')).toBeNull();
    expect(onDecidir).not.toHaveBeenCalled();
  });
});
