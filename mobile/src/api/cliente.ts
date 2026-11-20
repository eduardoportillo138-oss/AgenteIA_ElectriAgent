/**
 * Cliente tipado de la API REST de ElectriAgent (servidor.py).
 *
 * - Tiempo límite por solicitud (AbortController).
 * - Token opcional en `Authorization: Bearer`.
 * - Reintento solo en lecturas (GET). Una decisión (POST) nunca se reintenta
 *   ni se encola: o llega al servidor o se informa el error.
 * - Errores en español, con el mensaje del servidor (campo `error`) y qué hacer.
 */
import type {
  Configuracion,
  ResumenSesion,
  Salud,
  SolicitudAnalisis,
  SolicitudDecision,
  Vista,
} from './tipos';

export type TipoError = 'red' | 'tiempo' | 'autorizacion' | 'conflicto' | 'no_encontrado' | 'servidor' | 'solicitud' | 'configuracion';

export class ErrorApi extends Error {
  readonly tipo: TipoError;
  readonly estado: number | null;
  /** Qué puede hacer la persona para resolverlo. */
  readonly sugerencia: string;

  constructor(tipo: TipoError, mensaje: string, sugerencia: string, estado: number | null = null) {
    super(mensaje);
    this.name = 'ErrorApi';
    this.tipo = tipo;
    this.estado = estado;
    this.sugerencia = sugerencia;
  }
}

export interface OpcionesCliente {
  url: string;
  token?: string | null;
  /** Tiempo límite de las lecturas (ms). */
  tiempoLimiteMs?: number;
  /** Tiempo límite al crear un análisis, que puede tardar más (ms). */
  tiempoAnalisisMs?: number;
  /** Reintentos adicionales para GET ante fallas de red, tiempo agotado o error 5xx. */
  reintentos?: number;
  esperaReintentoMs?: number;
  fetchImpl?: typeof fetch;
}

/** "192.168.1.20:8000/" -> "http://192.168.1.20:8000". Devuelve null si no es válida. */
export function normalizarUrl(entrada: string): string | null {
  let url = entrada.trim();
  if (!url) return null;
  if (!/^https?:\/\//i.test(url)) url = `http://${url}`;
  url = url.replace(/\/+$/, '').replace(/\/api$/, '');
  const ok = /^https?:\/\/[^\s/:?#]+(:\d{1,5})?(\/[^\s?#]*)?$/i.test(url);
  return ok ? url : null;
}

const espera = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export function crearCliente(opciones: OpcionesCliente) {
  const base = normalizarUrl(opciones.url);
  const tiempoLimite = opciones.tiempoLimiteMs ?? 15_000;
  const tiempoAnalisis = opciones.tiempoAnalisisMs ?? 90_000;
  const reintentos = opciones.reintentos ?? 2;
  const esperaReintento = opciones.esperaReintentoMs ?? 600;
  const hacerFetch = opciones.fetchImpl ?? fetch;

  async function una<T>(metodo: 'GET' | 'POST', ruta: string, cuerpo: unknown, limite: number): Promise<T> {
    if (!base) {
      throw new ErrorApi('configuracion', 'La dirección del servidor no es válida.',
        'Escribe una URL como http://192.168.1.20:8000 en Ajustes.');
    }
    const control = new AbortController();
    const reloj = setTimeout(() => control.abort(), limite);
    const cabeceras: Record<string, string> = { Accept: 'application/json' };
    if (cuerpo !== undefined) cabeceras['Content-Type'] = 'application/json';
    if (opciones.token) cabeceras.Authorization = `Bearer ${opciones.token}`;
    let respuesta: Response;
    try {
      respuesta = await hacerFetch(`${base}/api${ruta}`, {
        method: metodo,
        headers: cabeceras,
        body: cuerpo === undefined ? undefined : JSON.stringify(cuerpo),
        signal: control.signal,
      });
    } catch {
      if (control.signal.aborted) {
        throw new ErrorApi('tiempo', `El servidor no respondió en ${Math.round(limite / 1000)} s.`,
          'Revisa que el celular y la PC estén en la misma red Wi-Fi e intenta de nuevo.');
      }
      throw new ErrorApi('red', `No hay conexión con el servidor en ${base}.`,
        'Revisa que servidor.py esté en ejecución con --host 0.0.0.0 y que el celular esté en la misma red Wi-Fi.');
    } finally {
      clearTimeout(reloj);
    }

    let datos: unknown = null;
    const texto = await respuesta.text();
    if (texto) {
      try {
        datos = JSON.parse(texto);
      } catch {
        throw new ErrorApi('servidor', `El servidor respondió ${respuesta.status} con un formato inesperado.`,
          'Comprueba que la URL apunte a servidor.py de ElectriAgent y no a otro servicio.', respuesta.status);
      }
    }
    if (respuesta.ok) return datos as T;

    const delServidor = typeof datos === 'object' && datos && 'error' in datos
      ? String((datos as { error: unknown }).error) : null;
    const s = respuesta.status;
    if (s === 401) {
      throw new ErrorApi('autorizacion', delServidor ?? 'El servidor rechazó el token de acceso.',
        'Escribe el token correcto en Ajustes (el mismo valor de ELECTRIAGENT_TOKEN).', s);
    }
    if (s === 404) {
      throw new ErrorApi('no_encontrado', delServidor ?? 'El servidor no encontró lo que pediste.',
        'La sesión pudo expirar: el servidor guarda las sesiones en memoria. Inicia un análisis nuevo.', s);
    }
    if (s === 409) {
      throw new ErrorApi('conflicto', delServidor ?? 'La sesión cambió en el servidor.',
        'Se recargará la sesión para mostrar su estado actual.', s);
    }
    if (s >= 500) {
      throw new ErrorApi('servidor', delServidor ?? `El servidor respondió ${s}.`,
        'Revisa la consola de servidor.py y vuelve a intentarlo.', s);
    }
    throw new ErrorApi('solicitud', delServidor ?? `El servidor rechazó la solicitud (${s}).`,
      'Corrige los datos indicados y vuelve a enviarla.', s);
  }

  async function leer<T>(ruta: string): Promise<T> {
    let ultimo: unknown;
    for (let intento = 0; intento <= reintentos; intento++) {
      try {
        return await una<T>('GET', ruta, undefined, tiempoLimite);
      } catch (e) {
        ultimo = e;
        const reintentable = e instanceof ErrorApi && (e.tipo === 'red' || e.tipo === 'tiempo' || e.tipo === 'servidor');
        if (!reintentable || intento === reintentos) throw e;
        await espera(esperaReintento * (intento + 1));
      }
    }
    throw ultimo;
  }

  return {
    base,
    salud: () => leer<Salud>('/salud'),
    configuracion: () => leer<Configuracion>('/config'),
    sesiones: async () => (await leer<{ sesiones: ResumenSesion[] }>('/sesiones')).sesiones,
    sesion: (id: string) => leer<Vista>(`/sesiones/${encodeURIComponent(id)}`),
    reporte: (id: string) => leer<unknown>(`/sesiones/${encodeURIComponent(id)}/reporte`),
    bitacora: (id: string) => leer<unknown>(`/sesiones/${encodeURIComponent(id)}/bitacora`),
    /** Crea y analiza una sesión. No se reintenta: cada intento sería un análisis nuevo. */
    analizar: (datos: SolicitudAnalisis) => una<Vista>('POST', '/sesiones', datos, tiempoAnalisis),
    /** Decisión del operador. Nunca se reintenta ni se encola. */
    decidir: (id: string, datos: SolicitudDecision) =>
      una<Vista>('POST', `/sesiones/${encodeURIComponent(id)}/aprobacion`, datos, tiempoAnalisis),
  };
}

export type ClienteApi = ReturnType<typeof crearCliente>;
