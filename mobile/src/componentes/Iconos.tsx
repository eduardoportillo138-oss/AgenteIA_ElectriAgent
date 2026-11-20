/** Íconos simples de la barra de pestañas (SVG, sin dependencias extra). */
import React from 'react';
import Svg, { Circle, Path } from 'react-native-svg';

import type { ColorValue } from 'react-native';

type Props = { color: ColorValue; size?: number };

export function IconoAnalizar({ color, size = 24 }: Props) {
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <Path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z" stroke={color} strokeWidth={2} strokeLinejoin="round" />
    </Svg>
  );
}

export function IconoPropuesta({ color, size = 24 }: Props) {
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <Path d="M6 2h9l4 4v16H6V2Z" stroke={color} strokeWidth={2} strokeLinejoin="round" />
      <Path d="m9 13 2.5 2.5L16 11" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
    </Svg>
  );
}

export function IconoHistorial({ color, size = 24 }: Props) {
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <Circle cx={12} cy={12} r={9} stroke={color} strokeWidth={2} />
      <Path d="M12 7v5l3 2" stroke={color} strokeWidth={2} strokeLinecap="round" />
    </Svg>
  );
}

export function IconoAjustes({ color, size = 24 }: Props) {
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <Path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0" stroke={color} strokeWidth={2} strokeLinecap="round" />
      <Circle cx={16} cy={6} r={2} stroke={color} strokeWidth={2} />
      <Circle cx={10} cy={12} r={2} stroke={color} strokeWidth={2} />
      <Circle cx={18} cy={18} r={2} stroke={color} strokeWidth={2} />
    </Svg>
  );
}
