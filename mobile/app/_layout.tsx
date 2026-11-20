import {
  BarlowSemiCondensed_500Medium,
  BarlowSemiCondensed_600SemiBold,
  useFonts,
} from '@expo-google-fonts/barlow-semi-condensed';
import { Stack } from 'expo-router';
import * as SplashScreen from 'expo-splash-screen';
import { StatusBar } from 'expo-status-bar';
import React, { useEffect } from 'react';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { ProveedorApp, useApp } from '@/estado/ContextoApp';
import { color, fuente } from '@/tema';

SplashScreen.preventAutoHideAsync().catch(() => undefined);

function Navegacion() {
  const { listo } = useApp();
  const [fuentes] = useFonts({ BarlowSemiCondensed_500Medium, BarlowSemiCondensed_600SemiBold });
  const visible = listo && fuentes;
  useEffect(() => {
    if (visible) SplashScreen.hideAsync().catch(() => undefined);
  }, [visible]);
  if (!visible) return null;
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: color.fondo },
        headerTintColor: color.texto,
        headerTitleStyle: { fontFamily: fuente.titulo, fontSize: 20 },
        contentStyle: { backgroundColor: color.fondo },
        headerBackTitle: 'Atrás',
      }}
    >
      <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
      <Stack.Screen name="recorrido" options={{ title: 'Recorrido de los agentes' }} />
      <Stack.Screen name="detalle" options={{ title: 'Evidencia del análisis' }} />
      <Stack.Screen name="resultado" options={{ title: 'Resultado' }} />
    </Stack>
  );
}

export default function Raiz() {
  return (
    <SafeAreaProvider>
      <ProveedorApp>
        <StatusBar style="light" />
        <Navegacion />
      </ProveedorApp>
    </SafeAreaProvider>
  );
}
