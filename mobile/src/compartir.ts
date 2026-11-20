/**
 * Comparte un JSON (reporte o bitácora). En el teléfono se escribe en la caché y
 * se abre la hoja de compartir del sistema; en Expo Web se descarga el archivo.
 */
import { File, Paths } from 'expo-file-system';
import * as Sharing from 'expo-sharing';
import { Platform } from 'react-native';

export async function compartirJson(nombre: string, datos: unknown, titulo: string): Promise<void> {
  const contenido = JSON.stringify(datos, null, 2);
  if (Platform.OS === 'web') {
    const url = URL.createObjectURL(new Blob([contenido], { type: 'application/json' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = nombre;
    a.click();
    URL.revokeObjectURL(url);
    return;
  }
  if (!(await Sharing.isAvailableAsync())) {
    throw new Error('Este dispositivo no permite compartir archivos.');
  }
  const archivo = new File(Paths.cache, nombre);
  await archivo.write(contenido);
  await Sharing.shareAsync(archivo.uri, { mimeType: 'application/json', UTI: 'public.json', dialogTitle: titulo });
}
