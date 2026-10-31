/**
 * Pluggable Zero-Overhead OpenTelemetry (OTEL) Tracing layer for Jev.
 *
 * Design Principles:
 * 1. Zero Runtime Overhead When Inactive: If OTEL is not configured or enabled,
 *    tracing operations resolve to no-ops with zero heap allocations or async overhead.
 * 2. Decoupled Architecture: Core routing logic never imports OTEL packages directly.
 * 3. Hook Budget Safety: Fast CLI tools and Claude Code hooks exit immediately without
 *    stalling on network export flushes. Emits spans asynchronously without blocking.
 */
import * as fsSync from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import * as crypto from 'node:crypto';
import { assertEgressAllowed } from './core/egress.js';

export interface JevSpan {
  setAttribute(key: string, value: string | number | boolean): void;
  setAttributes(attributes: Record<string, string | number | boolean>): void;
  setError(err: Error | string): void;
  end(): void;
}

export interface JevTracer {
  startSpan(name: string, attributes?: Record<string, string | number | boolean>): JevSpan;
}

export interface TracingConfig {
  enabled?: boolean;
  endpoint?: string;
  serviceName?: string;
}

// ── Zero-Overhead No-Op Implementation ───────────────────────────────────────

class NoopSpan implements JevSpan {
  setAttribute(_key: string, _value: string | number | boolean): void {}
  setAttributes(_attributes: Record<string, string | number | boolean>): void {}
  setError(_err: Error | string): void {}
  end(): void {}
}

const NOOP_SPAN = new NoopSpan();

class NoopTracer implements JevTracer {
  startSpan(_name: string, _attributes?: Record<string, string | number | boolean>): JevSpan {
    return NOOP_SPAN;
  }
}

// ── Asynchronous OTLP Span Implementation ────────────────────────────────────

function formatOtlpAttributeValue(val: string | number | boolean): Record<string, unknown> {
  if (typeof val === 'string') return { stringValue: val };
  if (typeof val === 'boolean') return { boolValue: val };
  if (Number.isInteger(val)) return { intValue: val.toString() };
  return { doubleValue: val };
}

function emitOtlpSpan(data: {
  endpoint: string;
  serviceName: string;
  traceId: string;
  spanId: string;
  name: string;
  startTimeNano: string;
  endTimeNano: string;
  attributes: Map<string, string | number | boolean>;
  error: Error | string | null;
}): void {
  try {
    const otlpAttrs: Array<{ key: string; value: unknown }> = [];
    for (const [k, v] of data.attributes.entries()) {
      otlpAttrs.push({ key: k, value: formatOtlpAttributeValue(v) });
    }

    if (data.error) {
      const msg = data.error instanceof Error ? data.error.message : String(data.error);
      otlpAttrs.push({ key: 'error.message', value: { stringValue: msg } });
    }

    const payload = {
      resourceSpans: [
        {
          resource: {
            attributes: [
              {
                key: 'service.name',
                value: { stringValue: data.serviceName || 'agentic-hub-jev' }
              }
            ]
          },
          scopeSpans: [
            {
              scope: {
                name: 'agentic-hub-jev',
                version: '2.3.0'
              },
              spans: [
                {
                  traceId: data.traceId,
                  spanId: data.spanId,
                  name: data.name,
                  kind: 1, // SPAN_KIND_INTERNAL
                  startTimeUnixNano: data.startTimeNano,
                  endTimeUnixNano: data.endTimeNano,
                  attributes: otlpAttrs,
                  status: data.error ? { code: 2, message: String(data.error) } : { code: 1 }
                }
              ]
            }
          ]
        }
      ]
    };

    const targetUrl = data.endpoint.endsWith('/v1/traces')
      ? data.endpoint
      : `${data.endpoint.replace(/\/$/, '')}/v1/traces`;
    const body = JSON.stringify(payload);
    // Same egress policy as every other outbound call; a blocked span is dropped by the catch below.
    assertEgressAllowed(targetUrl, body);

    // Asynchronous fire-and-forget: short abort timeout, unref'd timer, never blocks process exit
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 1200);
    if (typeof (timer as any)?.unref === 'function') {
      (timer as any).unref();
    }

    fetch(targetUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      signal: controller.signal
    })
      .catch(() => {
        // Silently drop network export failures to preserve fast CLI exits
      })
      .finally(() => {
        clearTimeout(timer);
      });
  } catch {
    // Non-blocking best-effort
  }
}

class OtlpSpan implements JevSpan {
  private name: string;
  private endpoint: string;
  private serviceName: string;
  private traceId: string;
  private spanId: string;
  private startTimeNano: string;
  private attributes: Map<string, string | number | boolean> = new Map();
  private error: Error | string | null = null;
  private ended = false;

  constructor(
    name: string,
    endpoint: string,
    serviceName: string,
    initialAttributes?: Record<string, string | number | boolean>
  ) {
    this.name = name;
    this.endpoint = endpoint;
    this.serviceName = serviceName;
    this.traceId = crypto.randomUUID().replace(/-/g, '');
    this.spanId = crypto.randomUUID().replace(/-/g, '').slice(0, 16);
    this.startTimeNano = (BigInt(Date.now()) * 1_000_000n).toString();
    if (initialAttributes) {
      this.setAttributes(initialAttributes);
    }
  }

  setAttribute(key: string, value: string | number | boolean): void {
    if (this.ended) return;
    this.attributes.set(key, value);
  }

  setAttributes(attributes: Record<string, string | number | boolean>): void {
    if (this.ended) return;
    for (const [k, v] of Object.entries(attributes)) {
      this.attributes.set(k, v);
    }
  }

  setError(err: Error | string): void {
    if (this.ended) return;
    this.error = err;
  }

  end(): void {
    if (this.ended) return;
    this.ended = true;
    const endTimeNano = (BigInt(Date.now()) * 1_000_000n).toString();

    emitOtlpSpan({
      endpoint: this.endpoint,
      serviceName: this.serviceName,
      traceId: this.traceId,
      spanId: this.spanId,
      name: this.name,
      startTimeNano: this.startTimeNano,
      endTimeNano,
      attributes: this.attributes,
      error: this.error
    });
  }
}

class OtlpTracer implements JevTracer {
  private endpoint: string;
  private serviceName: string;

  constructor(endpoint: string, serviceName: string) {
    this.endpoint = endpoint;
    this.serviceName = serviceName;
  }

  startSpan(name: string, attributes?: Record<string, string | number | boolean>): JevSpan {
    return new OtlpSpan(name, this.endpoint, this.serviceName, attributes);
  }
}

// ── Configuration & Tracer Lifecycle ─────────────────────────────────────────

let activeTracer: JevTracer = new NoopTracer();

function resolveTracingConfig(override?: TracingConfig): {
  enabled: boolean;
  endpoint: string | null;
  serviceName: string;
} {
  let fileConfig: { endpoint?: string; enabled?: boolean; serviceName?: string } = {};
  const configPath = path.join(os.homedir(), '.config', 'jev', 'config.json');
  try {
    if (fsSync.existsSync(configPath)) {
      const raw = JSON.parse(fsSync.readFileSync(configPath, 'utf8'));
      if (raw && typeof raw === 'object') {
        const tracing = raw.tracing || raw.otel || {};
        fileConfig = {
          endpoint: tracing.endpoint || raw.otelEndpoint || raw.otel_endpoint,
          enabled: tracing.enabled,
          serviceName: tracing.serviceName || tracing.service_name
        };
      }
    }
  } catch {
    // ignore
  }

  const endpoint =
    override?.endpoint ||
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT ||
    process.env.JEV_OTEL_ENDPOINT ||
    fileConfig.endpoint ||
    null;

  const envEnabled = process.env.JEV_OTEL_ENABLED;
  const isExplicitlyDisabled =
    override?.enabled === false ||
    envEnabled === 'false' ||
    fileConfig.enabled === false;

  const isExplicitlyEnabled =
    override?.enabled === true ||
    envEnabled === 'true' ||
    fileConfig.enabled === true;

  const enabled = !isExplicitlyDisabled && (isExplicitlyEnabled || Boolean(endpoint));

  const serviceName =
    override?.serviceName ||
    process.env.OTEL_SERVICE_NAME ||
    fileConfig.serviceName ||
    'agentic-hub-jev';

  return { enabled, endpoint, serviceName };
}

/**
 * Initialize tracing synchronously based on ~/.config/jev/config.json, env vars, or override.
 */
export function initTracingSync(configOverride?: TracingConfig): JevTracer {
  const { enabled, endpoint, serviceName } = resolveTracingConfig(configOverride);

  if (!enabled || !endpoint) {
    activeTracer = new NoopTracer();
    return activeTracer;
  }

  activeTracer = new OtlpTracer(endpoint, serviceName);
  return activeTracer;
}

/**
 * Initialize tracing dynamically if JEV_OTEL_ENABLED is set, an OTLP endpoint is configured in
 * ~/.config/jev/config.json, or @opentelemetry/api is present.
 */
export async function initTracing(configOverride?: TracingConfig): Promise<JevTracer> {
  const { enabled, endpoint, serviceName } = resolveTracingConfig(configOverride);

  if (!enabled) {
    activeTracer = new NoopTracer();
    return activeTracer;
  }

  // Attempt dynamic import of @opentelemetry/api if available in environment
  try {
    const otelApi = await import('@opentelemetry/api' as any);
    if (otelApi && otelApi.trace && typeof otelApi.trace.getTracer === 'function') {
      const tracer = otelApi.trace.getTracer(serviceName, '2.3.0');
      activeTracer = {
        startSpan(name: string, attributes?: Record<string, string | number | boolean>): JevSpan {
          const span = tracer.startSpan(name, { attributes });
          return {
            setAttribute: (k: string, v: string | number | boolean) => span.setAttribute(k, v),
            setAttributes: (attrs: Record<string, string | number | boolean>) => {
              for (const [k, v] of Object.entries(attrs)) span.setAttribute(k, v);
            },
            setError: (err: Error | string) =>
              span.recordException?.(err instanceof Error ? err : new Error(String(err))),
            end: () => span.end()
          };
        }
      };
      return activeTracer;
    }
  } catch {
    // @opentelemetry/api not installed; proceed to lightweight native OTLP
  }

  if (endpoint) {
    activeTracer = new OtlpTracer(endpoint, serviceName);
    return activeTracer;
  }

  activeTracer = new NoopTracer();
  return activeTracer;
}

export function getTracer(): JevTracer {
  return activeTracer;
}

export function resetTracerForTesting(): void {
  activeTracer = new NoopTracer();
}
