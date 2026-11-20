/**
 * Formato de números, horas y fechas en español de México.
 * Se implementa a mano para que sea idéntico en Hermes, la web y las pruebas.
 */

const MESES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic'];

/** 1234567.891 -> "1,234,568"; con decimales: num(1234.56, 1) -> "1,234.6". Negativos con "−". */
export function num(valor: number | null | undefined, decimales = 0): string {
  if (valor == null || !Number.isFinite(valor)) return '—';
  const factor = 10 ** decimales;
  const redondeado = Math.round(Math.abs(valor) * factor) / factor;
  const [entero = '0', fraccion] = redondeado.toFixed(decimales).split('.');
  const conMiles = entero.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const signo = valor < 0 && redondeado !== 0 ? '−' : '';
  return `${signo}${conMiles}${fraccion ? `.${fraccion}` : ''}`;
}

/** Como num(), pero quita los ceros decimales sobrantes: 13.0 -> "13", 13.25 -> "13.3" (1 decimal). */
export function num1(valor: number | null | undefined): string {
  if (valor == null || !Number.isFinite(valor)) return '—';
  const s = num(valor, 1);
  return s.endsWith('.0') ? s.slice(0, -2) : s;
}

export const kwh = (v: number | null | undefined) => `${num(v)} kWh`;
export const kw = (v: number | null | undefined) => `${num(v)} kW`;
export const mxn = (v: number | null | undefined) => `${num(v)} MXN`;
export const pct = (v: number | null | undefined) => `${num1(v)} %`;

/** 7 -> "07:00"; 24 -> "24:00". */
export function hora(h: number): string {
  return `${String(h).padStart(2, '0')}:00`;
}

/** [7, 8, 9, 18] -> "07:00–10:00, 18:00–19:00" (agrupa horas consecutivas). */
export function rangos(horas: number[]): string {
  const hs = [...new Set(horas)].sort((a, b) => a - b);
  if (!hs.length) return '';
  const partes: string[] = [];
  let inicio = hs[0]!;
  for (let i = 0; i < hs.length; i++) {
    const actual = hs[i]!;
    const siguiente = hs[i + 1];
    if (siguiente !== actual + 1) {
      partes.push(`${hora(inicio)}–${hora(actual + 1)}`);
      if (siguiente !== undefined) inicio = siguiente;
    }
  }
  return partes.join(', ');
}

/** "2026-11-20T08:31:05" -> "20 nov, 08:31". Usa la hora local escrita por el servidor. */
export function fecha(iso: string | null | undefined): string {
  if (!iso) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/.exec(iso);
  if (!m) return iso;
  const [, , mes, dia, hh, mm] = m;
  const base = `${Number(dia)} ${MESES[Number(mes) - 1] ?? mes}`;
  return hh ? `${base}, ${hh}:${mm}` : base;
}

/** "2026-11-20" -> "20 de noviembre de 2026". */
export function dia(iso: string): string {
  const LARGOS = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre',
    'octubre', 'noviembre', 'diciembre'];
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return iso;
  return `${Number(m[3])} de ${LARGOS[Number(m[2]) - 1]} de ${m[1]}`;
}

/** Diferencia con signo para "frente a hoy": +1,234 / −56.7. */
export function delta(valor: number, decimales = 0): string {
  const s = num(Math.abs(valor), decimales);
  if (Math.round(Math.abs(valor) * 10 ** decimales) === 0) return s;
  return `${valor > 0 ? '+' : '−'}${s}`;
}

/** Quita el prefijo "Regla N: " de la justificación, como hace la web. */
export function sinPrefijoRegla(texto: string): string {
  return texto.replace(/^Regla \d: /, '');
}

/** Nivel de riesgo (probabilidad x impacto) igual que en agentes.py. */
export function nivelRiesgo(v: number): 'bajo' | 'medio' | 'alto' | 'crítico' {
  return v <= 6 ? 'bajo' : v <= 12 ? 'medio' : v <= 19 ? 'alto' : 'crítico';
}
