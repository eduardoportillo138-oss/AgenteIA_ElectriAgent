/**
 * Tipos que reflejan las respuestas de servidor.py (`vista()`, `configuracion()`,
 * `salud()` y `listar_sesiones()`). El contrato se verifica del lado de Python en
 * pruebas.py (prueba_api_movil). Los campos de distribución por alimentador y de
 * bandas P10–P90 son opcionales: la app funciona con un servidor anterior.
 */

export type IdNodo = 'A' | 'B' | 'C' | 'D' | 'E' | 'F' | 'G' | 'H';
export type EstadoSesion = 'en_espera' | 'aprobada' | 'escalada' | 'rechazada' | 'procesando';
export type Serie24 = number[];

// ------------------------------------------------------------ /api/salud
export interface Salud {
  estado: string;
  version_api: string;
  version_modelo: string;
}

// ------------------------------------------------------------ /api/config
export interface Periodo {
  id: string;
  nombre: string;
  dias: number;
}

export interface Condicion {
  id: string;
  nombre: string;
  descripcion?: string;
  parametros?: Record<string, unknown>;
}

export interface Arista {
  de: IdNodo;
  a: IdNodo;
  peso: number;
}

export interface CargaRed {
  nombre: string;
  tipo: string;
  nivel: string;
  alimentador: string;
  potencia_kw: number;
  critica: boolean;
}

export interface AlimentadorRed {
  clave: string;
  nombre: string;
  nivel: string;
  capacidad_kw: number;
  transformador_bt_kw: number | null;
}

export interface Red {
  subestacion: string;
  tension_mt_kv: number;
  tension_bt: string;
  capacidad_kw: number;
  cargas: CargaRed[];
  renovables: { nombre: string; tipo: string; capacidad_kw: number; alimentador: string }[];
  tarifas: Record<string, number>;
  periodo_tarifario: ('base' | 'intermedia' | 'punta')[];
  alimentadores?: AlimentadorRed[];
  perdidas_mt_pct?: number;
  perdidas_bt_pct?: number;
  limite_inyeccion_kw?: number;
}

export interface Configuracion {
  sistema: string;
  periodos: Periodo[];
  condiciones: Condicion[];
  grafo: { nodos: Record<IdNodo, string>; aristas: Arista[] };
  red: Red;
  tolerancia_riesgo: number;
  mape_reentrenar: number;
  alimentadores_falla?: string[];
  cobertura_nominal_pct?: number;
}

// ------------------------------------------------------------ vista de sesión
export interface ResumenAlimentador {
  demanda_kwh: number;
  renovable_usada_kwh: number;
  renovable_desperdiciada_kwh: number;
  convencional_kwh: number;
  perdidas_kwh: number;
  deficit_kwh: number;
  recorte_kwh: number;
  no_servido_kwh: number;
  margen_min_pct: number;
  utilizacion_max_pct: number;
  carga_critica: boolean;
  pico_cabecera_kw: number;
  capacidad_kw: number;
  capacidad_trafo_bt_kw: number | null;
  costo_mxn: number;
}

export interface Metricas {
  demanda_kwh?: number;
  renovable_usada_kwh?: number;
  renovable_desperdiciada_kwh: number;
  renovable_pct: number;
  convencional_kwh: number;
  pico_convencional_kw: number;
  margen_min_pct: number;
  deficit_kwh: number;
  horas_deficit: number[];
  costo_mxn: number;
  desplazado_kwh: number;
  desplazado_pct: number;
  suspendido_kwh: number;
  // Distribución por alimentador (opcionales)
  recorte_kwh?: number;
  perdidas_kwh?: number;
  margen_red_min_pct?: number;
  utilizacion_max_pct?: number;
  deficit_previo_kwh?: number;
  deficit_planificado_kwh?: number;
  deficit_p90_kwh?: number;
  margen_red_min_p90_pct?: number;
  robusto?: boolean;
  base_planificacion?: string;
  recorte_por_carga_kwh?: Record<string, number>;
  alimentadores?: Record<string, ResumenAlimentador>;
  // Política de recorte: plan mínimo (P50) por omisión y plan robusto (P90) como alternativa
  recorte_adicional_robustez_kwh?: number;
  deficit_p90_evitado_kwh?: number;
  alternativa_robusta?: string;
  plan_minimo?: string;
}

export interface Riesgo {
  riesgo: string;
  prob: number;
  impacto: number;
  nivel: number;
  categoria: string;
  tratamiento: string;
}

export interface Impacto {
  grupo: string;
  efecto: 'positivo' | 'negativo' | 'neutral';
  detalle: string;
  kwh_recortados?: number;
  pct_demanda_recortada?: number;
}

export interface EvaluacionISO {
  id: string;
  riesgos: Riesgo[];
  riesgo_max: number;
  categoria_max: string;
  impactos: Impacto[];
  aceptable: boolean;
  requiere_escalamiento: boolean;
  robusto?: boolean;
  deficit_p90_kwh?: number;
}

export interface SerieRed {
  nombre: string;
  demanda_base: Serie24;
  demanda_recomendada: Serie24;
  renovable: Serie24;
  capacidad_kw: number;
  limite: Serie24;
  demanda_p10: Serie24;
  demanda_p90: Serie24;
  renovable_p10?: Serie24;
  renovable_p90?: Serie24;
  recorte: Serie24;
  horas_recorte: number[];
}

export interface Serie {
  demanda_base: Serie24;
  demanda_recomendada: Serie24;
  renovable: Serie24;
  capacidad_kw: number;
  horas_suspension: number[];
  bandas?: { demanda_p10: Serie24; demanda_p90: Serie24; renovable_p10: Serie24; renovable_p90: Serie24 };
  por_alimentador?: Record<string, SerieRed>;
}

export interface RecorteCarga {
  carga: string;
  kwh: number;
  kw_max: number;
  horas: number[];
}

export interface Propuesta {
  escenario: string;
  nombre: string;
  estrategia: string;
  modo: string;
  justificacion: string[];
  acciones: string[];
  metricas: Metricas;
  metricas_base: Metricas | null;
  evaluacion_iso: EvaluacionISO | null;
  serie: Serie;
  robusto?: boolean;
  hay_alternativa_robusta?: boolean | null;
  base_planificacion?: string;
  metricas_p90?: Metricas;
  alimentadores?: Record<string, ResumenAlimentador>;
  recorte_por_carga?: RecorteCarga[];
  /** kWh que el plan robusto P90 recorta de más frente al recorte mínimo (solo en contingencia). */
  recorte_adicional_robustez_kwh?: number | null;
  /** Id del plan robusto ofrecido como alternativa (cuando la propuesta es el recorte mínimo). */
  alternativa_robusta?: string | null;
}

export interface Alternativa {
  id: string;
  nombre: string;
  costo_mxn: number;
  renovable_pct: number;
  deficit_kwh: number;
  desplazado_kwh: number;
  suspendido_kwh: number;
  riesgo_max: number | null;
  aceptable: boolean | null;
  recorte_kwh?: number;
  robusto?: boolean;
  deficit_p90_kwh?: number;
  utilizacion_max_pct?: number;
  recorte_adicional_robustez_kwh?: number | null;
}

export interface Anomalia {
  carga: string;
  fecha: string;
  kw: number;
  esperado_kw: number;
  z: number;
}

export interface Analisis {
  dias: number;
  consumo_total_kwh: number;
  consumo_por_carga_kwh: Record<string, number>;
  pico_kw: number;
  fecha_pico: string;
  horas_pico: number[];
  perfil_horario_kw: number[];
  variacion_estacional_kw: Record<string, number>;
  deteccion: { inyectadas: number; detectadas: number; aciertos: number };
  generacion_renovable_kwh: number;
  mape_pct: number | null;
  experiencia_ejecuciones: number;
  cobertura_bandas?: { pct: number | null; dentro: number; lecturas: number; nominal_pct: number };
}

export interface Trazabilidad {
  version_modelo: string;
  etapa_ciclo_vida: string;
  huella_analisis: string;
  mape_pct: number | null;
  alerta_deriva: boolean;
  accion_monitoreo: string;
  cobertura_bandas_pct?: number | null;
  cobertura_nominal_pct?: number;
  lecturas_validacion_bandas?: number;
  alerta_calibracion_bandas?: boolean;
}

export interface Decision {
  aprobado: boolean;
  escenario: string;
  motivo: string;
  fecha: string;
  operador?: string | null;
}

export interface MensajeConsola {
  fecha: string;
  nodo: IdNodo;
  agente: string;
  texto: string;
}

export interface Vista {
  id: string;
  estado: EstadoSesion;
  periodo: { id: string; nombre: string };
  condicion: Condicion;
  dia_recomendado: string;
  coordinacion: { recorrido: string[]; costo: number; mensajes: number };
  consola: MensajeConsola[];
  analisis: Analisis | null;
  anomalias: Anomalia[];
  propuesta: Propuesta | null;
  alternativas: Alternativa[];
  trazabilidad: Trazabilidad | null;
  historial: Decision[];
  motivo_escalamiento: string[] | null;
  terminado: boolean;
}

export interface ResumenSesion {
  id: string;
  fecha: string;
  periodo: { id: string; nombre: string };
  condicion: { id: string; nombre: string };
  estado: EstadoSesion;
  escenario: string | null;
}

export interface SolicitudAnalisis {
  periodo: string;
  condicion: string;
  alimentador?: string;
  crecimiento_pct?: number;
  falla?: string;
  semilla?: number;
}

export interface SolicitudDecision {
  aprobado: boolean;
  motivo?: string;
  operador?: string;
}
