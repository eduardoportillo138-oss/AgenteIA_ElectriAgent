/**
 * Almacenamiento local de la app.
 *
 * - URL del servidor y token: expo-secure-store (en la web, que no lo soporta,
 *   se usa localStorage solo para pruebas con Expo Web).
 * - Nombre del operador: único dato personal; lo escribe la persona y firma sus decisiones.
 * - Copias de las últimas sesiones: AsyncStorage, solo para consultarlas sin conexión.
 *   Nunca se guardan decisiones pendientes de envío.
 */
import AsyncStorage from '@react-native-async-storage/async-storage';
import * as SecureStore from 'expo-secure-store';
import { Platform } from 'react-native';

import type { Vista } from './api/tipos';

const CLAVE_URL = 'electriagent.url';
const CLAVE_TOKEN = 'electriagent.token';
const CLAVE_OPERADOR = 'electriagent.operador';
const CLAVE_COPIAS = 'electriagent.copias_sesiones';
const MAX_COPIAS = 10;

export interface Ajustes {
  url: string;
  token: string;
  operador: string;
}

export interface CopiaSesion {
  guardada: string;
  vista: Vista;
}

const seguro = {
  async leer(clave: string): Promise<string | null> {
    if (Platform.OS === 'web') return globalThis.localStorage?.getItem(clave) ?? null;
    return SecureStore.getItemAsync(clave);
  },
  async escribir(clave: string, valor: string): Promise<void> {
    if (Platform.OS === 'web') {
      if (valor) globalThis.localStorage?.setItem(clave, valor);
      else globalThis.localStorage?.removeItem(clave);
      return;
    }
    if (valor) await SecureStore.setItemAsync(clave, valor);
    else await SecureStore.deleteItemAsync(clave);
  },
};

export async function leerAjustes(): Promise<Ajustes> {
  const [url, token, operador] = await Promise.all([
    seguro.leer(CLAVE_URL),
    seguro.leer(CLAVE_TOKEN),
    AsyncStorage.getItem(CLAVE_OPERADOR),
  ]);
  return { url: url ?? '', token: token ?? '', operador: operador ?? '' };
}

export async function guardarAjustes(a: Ajustes): Promise<void> {
  await Promise.all([
    seguro.escribir(CLAVE_URL, a.url.trim()),
    seguro.escribir(CLAVE_TOKEN, a.token.trim()),
    a.operador.trim()
      ? AsyncStorage.setItem(CLAVE_OPERADOR, a.operador.trim())
      : AsyncStorage.removeItem(CLAVE_OPERADOR),
  ]);
}

export async function leerCopias(): Promise<CopiaSesion[]> {
  try {
    const texto = await AsyncStorage.getItem(CLAVE_COPIAS);
    const copias = texto ? (JSON.parse(texto) as CopiaSesion[]) : [];
    return Array.isArray(copias) ? copias : [];
  } catch {
    return [];
  }
}

/** Guarda (o reemplaza) la copia de una sesión; la más reciente queda primero. */
export async function guardarCopia(vista: Vista): Promise<void> {
  const copias = (await leerCopias()).filter((c) => c.vista.id !== vista.id);
  copias.unshift({ guardada: new Date().toISOString(), vista });
  await AsyncStorage.setItem(CLAVE_COPIAS, JSON.stringify(copias.slice(0, MAX_COPIAS)));
}

export async function borrarCopias(): Promise<void> {
  await AsyncStorage.removeItem(CLAVE_COPIAS);
}
