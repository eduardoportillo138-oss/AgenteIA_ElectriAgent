/**
 * Lenguaje visual de ElectriAgent (mismo que la web y el logotipo).
 * El amarillo `rayo` tiene un solo significado: "falta tu decisión".
 */
import type { TextStyle } from 'react-native';

export const color = {
  fondo: '#0A2730',
  panel: '#0F3540',
  panel2: '#134250',
  linea: '#24596A',
  texto: '#E4F1EC',
  suave: '#9DBDB8',
  verde: '#8CCB5E',
  azul: '#3E7FD6',
  cian: '#35D6F0',
  rayo: '#F2D43B',
  alerta: '#EE8467',
  critico: '#F25C5C',
  claro: '#C9D6D2',
  sobreClaro: '#062027',
  arista: '#3B6E7C',
} as const;

export const fuente = {
  titulo: 'BarlowSemiCondensed_600SemiBold',
  cifra: 'BarlowSemiCondensed_600SemiBold',
  rotulo: 'BarlowSemiCondensed_500Medium',
} as const;

/** Números tabulares para que las cifras no "bailen". */
export const tabular: TextStyle = { fontVariant: ['tabular-nums'] };

export const espacio = { xs: 4, s: 8, m: 12, l: 16, xl: 24, xxl: 32 } as const;
export const radio = { s: 6, m: 14, pastilla: 999 } as const;

/** Área táctil mínima (dp). */
export const TACTIL = 48;

export const texto = {
  h1: { fontFamily: fuente.titulo, fontSize: 28, lineHeight: 32, color: color.texto } satisfies TextStyle,
  h2: { fontFamily: fuente.titulo, fontSize: 22, lineHeight: 26, color: color.texto } satisfies TextStyle,
  h3: { fontFamily: fuente.rotulo, fontSize: 17, lineHeight: 22, color: color.suave } satisfies TextStyle,
  cuerpo: { fontSize: 16, lineHeight: 23, color: color.texto } satisfies TextStyle,
  sub: { fontSize: 14, lineHeight: 20, color: color.suave } satisfies TextStyle,
  cifra: { fontFamily: fuente.cifra, fontSize: 22, lineHeight: 26, color: color.texto, ...tabular } satisfies TextStyle,
};

export const NIVEL_COLOR: Record<string, string> = {
  bajo: color.verde,
  medio: '#D7C46A',
  alto: color.alerta,
  crítico: color.critico,
};
