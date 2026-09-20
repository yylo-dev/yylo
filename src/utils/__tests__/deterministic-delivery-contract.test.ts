import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

describe('deterministic delivery instruction contract', () => {
  it.each([
    'src/templates/controller-agent/AGENTS.md',
    'src/templates/controller-agent/CLAUDE.md',
    'src/templates/skills/canonical/ralph-loop/references/implement.md',
    'src/templates/wiki/controller/git_worktree_lifecycle.md',
    'src/templates/wiki/controller/yy_pi_progress.md',
    'src/templates/prompts/life_cycle.md',
    'README.md',
  ])('%s does not direct agents into retired execution', (file) => {
    const source = readFileSync(resolve(process.cwd(), file), 'utf8');
    expect(source).not.toMatch(/\byy\s+task\s+(?:run|resume|recover-predispatch|recover-wall-budget)\b/);
    expect(source).not.toMatch(/\byy\s+watch\s+exec\b/);
    expect(source).toMatch(/external agent|External agents|external implementation|Execute authorized commands/i);
  });

  it('keeps observation outside delivery authority and retains the shared runner', () => {
    const instructions = readFileSync(resolve(process.cwd(), 'src/templates/controller-agent/AGENTS.md'), 'utf8');
    expect(instructions).toContain('without launching, retrying, cancelling or completing work');
    expect(instructions).toContain('A worker exit is not task completion');
    const task = readFileSync(resolve(process.cwd(), 'src/templates/scripts/task_workspace.py'), 'utf8');
    expect(task).not.toContain('def managed_task_run(');
    expect(task).not.toContain('def _launch_task_worker(');
    expect(task).toContain('managed_task_execution_retired');
    const runner = readFileSync(resolve(process.cwd(), 'src/templates/scripts/managed_agent_runner.py'), 'utf8');
    expect(runner).toContain('recover_settled_worker_capture');
  });
});
