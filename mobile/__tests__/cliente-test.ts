import { crearCliente, ErrorApi, normalizarUrl } from '../src/api/cliente';

type Llamada = { url: string; init: RequestInit };

/** fetch falso que responde en orden con las respuestas dadas y registra las llamadas. */
function fetchFalso(respuestas: (() => Promise<Response>)[]) {
  const llamadas: Llamada[] = [];
  const impl = jest.fn(async (url: string, init: RequestInit) => {
    llamadas.push({ url, init });
    const r = respuestas.shift();
    if (!r) throw new Error('sin respuesta preparada');
    return r();
  });
  return { impl: impl as unknown as typeof fetch, llamadas };
}

const json = (status: number, cuerpo: unknown) => () =>
  Promise.resolve(new Response(JSON.stringify(cuerpo), { status, headers: { 'Content-Type': 'application/json' } }));
const caida = () => () => Promise.reject(new TypeError('Network request failed'));

const SALUD = { estado: 'ok', version_api: '1.1', version_modelo: 'ElectriAgent-proto 0.2' };

describe('normalizarUrl', () => {
  it('agrega http:// y quita la diagonal final o /api', () => {
    expect(normalizarUrl('192.168.1.20:8000/')).toBe('http://192.168.1.20:8000');
    expect(normalizarUrl(' https://srv.ejemplo.mx/api ')).toBe('https://srv.ejemplo.mx');
  });
  it('rechaza entradas vacías o con espacios', () => {
    expect(normalizarUrl('')).toBeNull();
    expect(normalizarUrl('http://mi servidor')).toBeNull();
  });
});

describe('cliente de la API', () => {
  it('envía el token como Bearer y arma la ruta /api', async () => {
    const f = fetchFalso([json(200, SALUD)]);
    const c = crearCliente({ url: '10.0.0.5:8000', token: 'secreto', fetchImpl: f.impl });
    await expect(c.salud()).resolves.toEqual(SALUD);
    expect(f.llamadas[0]!.url).toBe('http://10.0.0.5:8000/api/salud');
    expect((f.llamadas[0]!.init.headers as Record<string, string>).Authorization).toBe('Bearer secreto');
  });

  it('sin token no envía Authorization', async () => {
    const f = fetchFalso([json(200, SALUD)]);
    await crearCliente({ url: 'http://x:8000', fetchImpl: f.impl }).salud();
    expect((f.llamadas[0]!.init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it('401: usa el mensaje del servidor y sugiere revisar el token', async () => {
    const f = fetchFalso([json(401, { error: 'Falta el token de acceso o es incorrecto.' })]);
    const e = await crearCliente({ url: 'http://x:8000', fetchImpl: f.impl }).configuracion().catch((x) => x);
    expect(e).toBeInstanceOf(ErrorApi);
    expect(e.tipo).toBe('autorizacion');
    expect(e.message).toBe('Falta el token de acceso o es incorrecto.');
    expect(e.sugerencia).toMatch(/token/);
    expect(f.llamadas).toHaveLength(1);           // un 401 no se reintenta
  });

  it('400 y 409 devuelven el campo error del servidor', async () => {
    const f = fetchFalso([
      json(400, { error: 'Escribe el motivo del rechazo para replantear la propuesta' }),
      json(409, { error: 'Esta sesión no tiene una propuesta en espera' }),
    ]);
    const c = crearCliente({ url: 'http://x:8000', fetchImpl: f.impl });
    const e1 = await c.decidir('abc', { aprobado: false }).catch((x) => x);
    expect(e1.tipo).toBe('solicitud');
    expect(e1.message).toMatch(/motivo del rechazo/);
    const e2 = await c.decidir('abc', { aprobado: true }).catch((x) => x);
    expect(e2.tipo).toBe('conflicto');
    expect(e2.estado).toBe(409);
  });

  it('reintenta las lecturas ante una falla de red', async () => {
    const f = fetchFalso([caida(), json(200, SALUD)]);
    const c = crearCliente({ url: 'http://x:8000', fetchImpl: f.impl, esperaReintentoMs: 1 });
    await expect(c.salud()).resolves.toEqual(SALUD);
    expect(f.llamadas).toHaveLength(2);
  });

  it('nunca reintenta una decisión (POST)', async () => {
    const f = fetchFalso([caida(), json(200, {})]);
    const c = crearCliente({ url: 'http://x:8000', fetchImpl: f.impl, esperaReintentoMs: 1 });
    const e = await c.decidir('abc', { aprobado: true, operador: 'Ing. Pruebas' }).catch((x) => x);
    expect(e.tipo).toBe('red');
    expect(e.message).toMatch(/No hay conexión con el servidor en http:\/\/x:8000/);
    expect(e.sugerencia).toMatch(/--host 0\.0\.0\.0/);
    expect(f.llamadas).toHaveLength(1);
    expect(JSON.parse(String(f.llamadas[0]!.init.body))).toEqual({ aprobado: true, operador: 'Ing. Pruebas' });
  });

  it('corta por tiempo y lo explica', async () => {
    const lenta = jest.fn((_url: string, init: RequestInit) => new Promise<Response>((_, rechazar) => {
      init.signal?.addEventListener('abort', () => rechazar(new Error('abortado')));
    }));
    const c = crearCliente({ url: 'http://x:8000', fetchImpl: lenta as unknown as typeof fetch,
      tiempoLimiteMs: 30, reintentos: 0 });
    const e = await c.salud().catch((x) => x);
    expect(e.tipo).toBe('tiempo');
    expect(e.message).toBe('El servidor no respondió en 0 s.');
  });

  it('una URL inválida no llega a hacer la solicitud', async () => {
    const f = fetchFalso([]);
    const e = await crearCliente({ url: '', fetchImpl: f.impl }).salud().catch((x) => x);
    expect(e.tipo).toBe('configuracion');
    expect(f.llamadas).toHaveLength(0);
  });
});
