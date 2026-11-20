import { delta, dia, fecha, hora, nivelRiesgo, num, num1, pct, rangos, sinPrefijoRegla } from '../src/formato';

describe('números', () => {
  it('separa miles con coma y redondea', () => {
    expect(num(2124860.4)).toBe('2,124,860');
    expect(num(1234.56, 1)).toBe('1,234.6');
    expect(num(0)).toBe('0');
    expect(num(-9657.6)).toBe('−9,658');
  });
  it('maneja valores vacíos', () => {
    expect(num(null)).toBe('—');
    expect(num(Number.NaN)).toBe('—');
  });
  it('num1 quita el decimal cero', () => {
    expect(num1(13)).toBe('13');
    expect(num1(13.03)).toBe('13');
    expect(num1(87.25)).toBe('87.3');
    expect(pct(9.95)).toBe('10 %');
  });
  it('evita el "-0"', () => {
    expect(num(-0.0001, 1)).toBe('0.0');
    expect(delta(0.01)).toBe('0');
  });
  it('delta lleva signo', () => {
    expect(delta(5853)).toBe('+5,853');
    expect(delta(-5.1, 1)).toBe('−5.1');
  });
});

describe('horas y fechas', () => {
  it('formatea horas con dos dígitos', () => {
    expect(hora(7)).toBe('07:00');
    expect(hora(24)).toBe('24:00');
  });
  it('agrupa horas consecutivas', () => {
    expect(rangos([18, 19, 20, 21])).toBe('18:00–22:00');
    expect(rangos([9, 7, 8, 18])).toBe('07:00–10:00, 18:00–19:00');
    expect(rangos([])).toBe('');
  });
  it('fechas en español de México', () => {
    expect(fecha('2026-11-20T08:31:05')).toBe('20 nov, 08:31');
    expect(fecha('2026-01-03')).toBe('3 ene');
    expect(dia('2026-11-20')).toBe('20 de noviembre de 2026');
  });
});

describe('textos', () => {
  it('quita el prefijo de regla y clasifica el riesgo', () => {
    expect(sinPrefijoRegla('Regla 2: renovables cubren 13 %')).toBe('renovables cubren 13 %');
    expect([6, 12, 15, 20].map(nivelRiesgo)).toEqual(['bajo', 'medio', 'alto', 'crítico']);
  });
});
