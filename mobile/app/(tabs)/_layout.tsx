import { Tabs } from 'expo-router';
import React from 'react';

import { IconoAjustes, IconoAnalizar, IconoHistorial, IconoPropuesta } from '@/componentes/Iconos';
import { useApp } from '@/estado/ContextoApp';
import { color, fuente } from '@/tema';

export default function Pestanas() {
  const { sesion } = useApp();
  const pendiente = sesion?.estado === 'en_espera';
  return (
    <Tabs
      screenOptions={{
        headerStyle: { backgroundColor: color.fondo },
        headerTintColor: color.texto,
        headerTitleStyle: { fontFamily: fuente.titulo, fontSize: 20 },
        tabBarStyle: { backgroundColor: color.panel, borderTopColor: color.linea, minHeight: 60 },
        tabBarActiveTintColor: color.cian,
        tabBarInactiveTintColor: color.suave,
        tabBarLabelStyle: { fontSize: 12 },
        sceneStyle: { backgroundColor: color.fondo },
      }}
    >
      <Tabs.Screen name="index" options={{
        title: 'Analizar', headerTitle: 'ElectriAgent',
        tabBarIcon: ({ color: c }) => <IconoAnalizar color={c} />,
      }} />
      <Tabs.Screen name="propuesta" options={{
        title: 'Propuesta',
        tabBarIcon: ({ color: c }) => <IconoPropuesta color={c} />,
        // El amarillo se reserva para "falta tu decisión"
        tabBarBadge: pendiente ? '!' : undefined,
        tabBarBadgeStyle: { backgroundColor: color.rayo, color: color.fondo },
        tabBarAccessibilityLabel: pendiente ? 'Propuesta, falta tu decisión' : 'Propuesta',
      }} />
      <Tabs.Screen name="historial" options={{
        title: 'Historial',
        tabBarIcon: ({ color: c }) => <IconoHistorial color={c} />,
      }} />
      <Tabs.Screen name="ajustes" options={{
        title: 'Ajustes',
        tabBarIcon: ({ color: c }) => <IconoAjustes color={c} />,
      }} />
    </Tabs>
  );
}
