import type { Command } from 'commander';
import {
  MACHINE_RESPONSE_SCHEMA,
  resolveMachineOutput,
  writeMachineResponse,
} from '../machine-output.js';

export const MACHINE_CAPABILITIES_SCHEMA = 'yylo.machine-capabilities.v1' as const;

export const machineCapabilities = {
  schema_version: MACHINE_CAPABILITIES_SCHEMA,
  response_schema: MACHINE_RESPONSE_SCHEMA,
  formats: ['json', 'ndjson'],
  framing: { json: 'one JSON document', ndjson: 'one response object per line' },
  surfaces: {
    task: { projections: ['default'], selector: '--format json|ndjson [--raw]' },
    merge: { projections: ['default', 'detail', 'full', 'plan'], selector: 'status --detail|--full or plan, plus --format json|ndjson [--raw]' },
    integration: { projections: ['default'], selector: '--format json|ndjson [--raw]' },
    ledger: { projections: ['command-defined'], selector: '-f json|ndjson --raw' },
    managed_run: { projections: ['execution'], selector: '--execution-envelope' },
    release: { projections: ['plan', 'status'], selector: null, availability: 'schema-reserved; no public release command is advertised' },
  },
  compatibility: {
    human_default: 'unchanged',
    machine_v1: 'additive opt-in; payload from the legacy command is nested under data',
  },
} as const;

export function configureCapabilitiesCommand(program: Command): void {
  program.command('capabilities')
    .description('Publish supported machine formats, projections, and schema identities')
    .option('--format <format>', 'Machine response format: json or ndjson', 'json')
    .option('--raw', 'Emit compact output')
    .action(() => {
      const request = resolveMachineOutput(process.argv.slice(2)) ?? {
        format: 'json' as const, raw: process.argv.includes('--raw'), projection: 'capabilities',
      };
      writeMachineResponse({
        schema_version: MACHINE_RESPONSE_SCHEMA,
        command: { name: 'capabilities', version: 1 },
        status: 'success',
        projection: 'capabilities',
        data: machineCapabilities,
        error: null,
      }, request);
    });
}
