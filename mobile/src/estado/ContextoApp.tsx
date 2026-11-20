/**
 * Estado compartido de la app con hooks de React (sin librerías de estado).
 *
 * Reglas del documento que se aplican aquí:
 * - Autoridad humana: una decisión solo se envía en línea; nunca se encola.
 * - Si la sesión cambió (409), se recarga y se avisa.
 * - Sin conexión se puede consultar la última copia local, en solo lectura.
 */
import NetInfo from '@react-native-community/netinfo';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { AccessibilityInfo } from 'react-native';

import { crearCliente, ErrorApi, normalizarUrl, type ClienteApi } from '../api/cliente';
import type { Configuracion, SolicitudAnalisis, Vista } from '../api/tipos';
import * as almacen from '../almacen';
import type { Ajustes } from '../almacen';

export type OrigenSesion = 'servidor' | 'copia_local';

export interface ResultadoDecision {
  ok: boolean;
  aviso?: string;
  error?: ErrorApi;
}

interface ValorContexto {
  listo: boolean;
  ajustes: Ajustes;
  configurado: boolean;
  cliente: ClienteApi | null;
  config: Configuracion | null;
  errorConfig: ErrorApi | null;
  sesion: Vista | null;
  origen: OrigenSesion;
  enLinea: boolean;
  reducirMovimiento: boolean;
  guardarAjustes: (a: Ajustes) => Promise<void>;
  cargarConfig: () => Promise<void>;
  analizar: (datos: SolicitudAnalisis) => Promise<Vista>;
  abrirSesion: (id: string) => Promise<OrigenSesion>;
  mostrarCopia: (vista: Vista) => void;
  recargar: () => Promise<void>;
  decidir: (aprobado: boolean, motivo: string) => Promise<ResultadoDecision>;
}

const Contexto = createContext<ValorContexto | null>(null);

export function ProveedorApp({ children }: { children: React.ReactNode }) {
  const [listo, setListo] = useState(false);
  const [ajustes, setAjustes] = useState<Ajustes>({ url: '', token: '', operador: '' });
  const [config, setConfig] = useState<Configuracion | null>(null);
  const [errorConfig, setErrorConfig] = useState<ErrorApi | null>(null);
  const [sesion, setSesion] = useState<Vista | null>(null);
  const [origen, setOrigen] = useState<OrigenSesion>('servidor');
  const [enLinea, setEnLinea] = useState(true);
  const [reducirMovimiento, setReducirMovimiento] = useState(false);
  const sesionRef = useRef<Vista | null>(null);
  useEffect(() => {
    sesionRef.current = sesion;
  }, [sesion]);

  const cliente = useMemo(
    () => (normalizarUrl(ajustes.url) ? crearCliente({ url: ajustes.url, token: ajustes.token || null }) : null),
    [ajustes.url, ajustes.token],
  );

  // Arranque: ajustes guardados y última sesión (copia local)
  useEffect(() => {
    let vivo = true;
    (async () => {
      const [a, copias] = await Promise.all([almacen.leerAjustes(), almacen.leerCopias()]);
      if (!vivo) return;
      setAjustes(a);
      const ultima = copias[0];
      if (ultima) {
        setSesion(ultima.vista);
        setOrigen('copia_local');
      }
      setListo(true);
    })();
    return () => {
      vivo = false;
    };
  }, []);

  // Conectividad del dispositivo
  useEffect(() => {
    const quitar = NetInfo.addEventListener((estado) => setEnLinea(estado.isConnected !== false));
    return quitar;
  }, []);

  // Preferencia del sistema "reducir movimiento"
  useEffect(() => {
    AccessibilityInfo.isReduceMotionEnabled().then(setReducirMovimiento).catch(() => undefined);
    const sub = AccessibilityInfo.addEventListener('reduceMotionChanged', setReducirMovimiento);
    return () => sub.remove();
  }, []);

  const aceptarVista = useCallback(async (v: Vista) => {
    setSesion(v);
    setOrigen('servidor');
    await almacen.guardarCopia(v);
  }, []);

  const cargarConfig = useCallback(async () => {
    if (!cliente) return;
    try {
      setConfig(await cliente.configuracion());
      setErrorConfig(null);
    } catch (e) {
      setErrorConfig(e instanceof ErrorApi ? e : null);
    }
  }, [cliente]);

  useEffect(() => {
    if (!listo || !cliente) return;
    let vivo = true;
    cliente.configuracion()
      .then((c) => {
        if (!vivo) return;
        setConfig(c);
        setErrorConfig(null);
      })
      .catch((e) => vivo && setErrorConfig(e instanceof ErrorApi ? e : null));
    // Si hay una copia local, intenta traer su estado actual del servidor
    const actual = sesionRef.current;
    if (actual) {
      cliente.sesion(actual.id).then((v) => { if (vivo) aceptarVista(v); }).catch(() => undefined);
    }
    return () => {
      vivo = false;
    };
  }, [listo, cliente, aceptarVista]);

  const guardarAjustes = useCallback(async (a: Ajustes) => {
    await almacen.guardarAjustes(a);
    setAjustes({ url: a.url.trim(), token: a.token.trim(), operador: a.operador.trim() });
  }, []);

  const exigirCliente = useCallback((): ClienteApi => {
    if (!cliente) {
      throw new ErrorApi('configuracion', 'Falta la dirección del servidor.',
        'Escríbela en Ajustes, por ejemplo http://192.168.1.20:8000.');
    }
    return cliente;
  }, [cliente]);

  const analizar = useCallback(async (datos: SolicitudAnalisis) => {
    const v = await exigirCliente().analizar(datos);
    await aceptarVista(v);
    return v;
  }, [exigirCliente, aceptarVista]);

  const abrirSesion = useCallback(async (id: string): Promise<OrigenSesion> => {
    try {
      await aceptarVista(await exigirCliente().sesion(id));
      return 'servidor';
    } catch (e) {
      // Expiró en el servidor o no hay conexión: se muestra la copia local en solo lectura
      const copia = (await almacen.leerCopias()).find((c) => c.vista.id === id);
      if (copia && e instanceof ErrorApi && (e.tipo === 'no_encontrado' || e.tipo === 'red' || e.tipo === 'tiempo')) {
        setSesion(copia.vista);
        setOrigen('copia_local');
        return 'copia_local';
      }
      throw e;
    }
  }, [exigirCliente, aceptarVista]);

  const mostrarCopia = useCallback((v: Vista) => {
    setSesion(v);
    setOrigen('copia_local');
  }, []);

  const recargar = useCallback(async () => {
    const actual = sesionRef.current;
    if (actual) await abrirSesion(actual.id);
  }, [abrirSesion]);

  const decidir = useCallback(async (aprobado: boolean, motivo: string): Promise<ResultadoDecision> => {
    const actual = sesionRef.current;
    if (!actual) return { ok: false };
    if (!enLinea || origen !== 'servidor') {
      return { ok: false, error: new ErrorApi('red', 'Necesitas conexión con el servidor para decidir.',
        'Conéctate a la misma red que el servidor y recarga la sesión.') };
    }
    try {
      const v = await exigirCliente().decidir(actual.id, {
        aprobado,
        motivo: motivo.trim(),
        operador: ajustes.operador.trim() || undefined,
      });
      await aceptarVista(v);
      return { ok: true };
    } catch (e) {
      const error = e instanceof ErrorApi ? e : new ErrorApi('servidor', String(e), 'Intenta de nuevo.');
      if (error.tipo === 'conflicto') {
        await abrirSesion(actual.id).catch(() => undefined);
        return { ok: false, aviso: 'Esta propuesta ya se había decidido. Se cargó su estado actual.' };
      }
      return { ok: false, error };
    }
  }, [enLinea, origen, ajustes.operador, exigirCliente, aceptarVista, abrirSesion]);

  const valor: ValorContexto = {
    listo,
    ajustes,
    configurado: Boolean(normalizarUrl(ajustes.url)),
    cliente,
    config,
    errorConfig,
    sesion,
    origen,
    enLinea,
    reducirMovimiento,
    guardarAjustes,
    cargarConfig,
    analizar,
    abrirSesion,
    mostrarCopia,
    recargar,
    decidir,
  };
  return <Contexto.Provider value={valor}>{children}</Contexto.Provider>;
}

export function useApp(): ValorContexto {
  const v = useContext(Contexto);
  if (!v) throw new Error('useApp debe usarse dentro de <ProveedorApp>');
  return v;
}
