import { Command } from 'commander';
import { describe, expect, it } from 'vitest';
import fs from 'fs-extra';
import * as path from 'node:path';
import {
  configureTaskWorkspaceCommand,
  taskWorkspaceControlOperation,
  type TaskWorkspaceOperation,
} from '../../cli/commands/task.js';
import {
  configureMergeCommand,
  mergeControlOperation,
  type MergeOperation,
} from '../../cli/commands/merge.js';
import { configureEvidenceCommand } from '../../cli/commands/evidence.js';
import { configureIntegrationCommand } from '../../cli/commands/integration.js';

const PROJECT_ROOT = path.resolve(__dirname, '../../..');
const YYLO_SOURCE = path.join(PROJECT_ROOT, 'src/bin/yylo.sh');

type ControlOperation = 'kanban' | 'orchestration';

/**
 * Parse the route_registered_product_control case allowlist from the shipped
 * shell wrapper. Each allowlist line maps `prefix:subcommand` alternatives to
 * exactly one effective_operation classification.
 */
function parseRouterAllowlist(source: string): Map<string, ControlOperation> {
  const classification = new Map<string, ControlOperation>();
  for (const line of source.split('\n')) {
    const match = line.match(
      /^\s*((?:task|merge|evidence|integration):[^\s|)]*(?:\|(?:task|merge|evidence|integration):[^\s|)]*)*)\)\s+effective_operation=(kanban|orchestration)\s+;;/,
    );
    if (!match) continue;
    for (const alternative of match[1].split('|')) {
      classification.set(alternative, match[2] as ControlOperation);
    }
  }
  return classification;
}

function registeredSubcommands(
  configure: (program: Command) => void,
  topLevel: string,
): string[] {
  const program = new Command();
  configure(program);
  const command = program.commands.find((entry) => entry.name() === topLevel);
  expect(command, `${topLevel} command must be registered`).toBeTruthy();
  return command!.commands.map((entry) => entry.name());
}

describe('yylo.sh router allowlist contract', () => {
  it('classifies every registered control-plane subcommand exactly like the CLI', async () => {
    const router = parseRouterAllowlist(await fs.readFile(YYLO_SOURCE, 'utf8'));

    const taskOperations = registeredSubcommands(
      (program) => configureTaskWorkspaceCommand(program, async () => undefined),
      'task',
    );
    const mergeOperations = registeredSubcommands(
      (program) => configureMergeCommand(program, async () => undefined),
      'merge',
    );
    const evidenceOperations = registeredSubcommands(
      (program) => configureEvidenceCommand(program, async () => undefined),
      'evidence',
    );
    const integrationOperations = registeredSubcommands(
      (program) => configureIntegrationCommand(program, async () => undefined),
      'integration',
    );

    const expected = new Map<string, ControlOperation>();
    for (const operation of taskOperations) {
      expected.set(
        `task:${operation}`,
        taskWorkspaceControlOperation(operation as TaskWorkspaceOperation),
      );
    }
    for (const operation of mergeOperations) {
      expected.set(
        `merge:${operation}`,
        mergeControlOperation(operation as MergeOperation),
      );
    }
    for (const operation of evidenceOperations) {
      expected.set(
        `evidence:${operation}`,
        taskWorkspaceControlOperation(`evidence-${operation}` as TaskWorkspaceOperation),
      );
    }
    for (const operation of integrationOperations) {
      expected.set(`integration:${operation}`, operation === 'status' ? 'kanban' : 'orchestration');
    }

    const missing = [...expected.keys()].filter((key) => !router.has(key));
    expect(
      missing,
      'route_registered_product_control must allowlist every registered CLI subcommand; add it with the classification the CLI itself uses',
    ).toEqual([]);

    const mismatches = [...expected.entries()]
      .filter(([key, classification]) => router.get(key) !== classification)
      .map(([key, classification]) => `${key}: expected ${classification}, found ${router.get(key)}`);
    expect(mismatches, 'the wrapper must classify control commands identically to the CLI').toEqual([]);

    const helpForms = new Set<string>();
    for (const prefix of ['task', 'merge', 'evidence', 'integration']) {
      helpForms.add(`${prefix}:`);
      helpForms.add(`${prefix}:-h`);
      helpForms.add(`${prefix}:--help`);
    }
    const stale = [...router.keys()].filter(
      (key) => !expected.has(key) && !helpForms.has(key),
    );
    expect(
      stale,
      'router allowlist entries must exist on the registered CLI surface (only bare/help forms are exempt)',
    ).toEqual([]);

    for (const key of helpForms) {
      expect(router.get(key), `${key} must stay classified for terminal help`).toBe('kanban');
    }
  });

  it('keeps control commands and local tmux utilities classified before checkout bootstrap', async () => {
    const source = await fs.readFile(YYLO_SOURCE, 'utf8');
    expect(source).toMatch(
      /-V\|--version\|info\|where\|capabilities\|benchmark\|ledger\|kanban\|task\|merge\|integration\|evidence\|tmux\) return 0/,
    );
    expect(source).toMatch(/case "\$operation" in ledger\|kanban\|task\|merge\|integration\|evidence\) ;; \*\) return 1 ;; esac/);
  });
});
