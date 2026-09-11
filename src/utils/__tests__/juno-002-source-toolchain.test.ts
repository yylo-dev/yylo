import { afterEach, describe, expect, it } from 'vitest';
import fs from 'fs-extra';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const tool = path.resolve(process.cwd(), 'scripts/juno-002-source-toolchain.sh');
const policy = path.resolve(process.cwd(), 'src/templates/scripts/juno-toolchain-policy.sh');
const tempDirs: string[] = [];

async function executable(file: string, content: string) {
  await fs.outputFile(file, content);
  await fs.chmod(file, 0o755);
}

function run(args: string[], env: Record<string, string>) {
  return spawnSync('bash', [tool, ...args], {
    env: { ...process.env, ...env },
    encoding: 'utf8',
  });
}

afterEach(async () => {
  await Promise.all(tempDirs.splice(0).map((dir) => fs.remove(dir)));
});

describe('Juno 2 Kanban compatibility policy', () => {
  it.each([
    ['task 1.99.9', false],
    ['task 2.0.0', false],
    ['task 2.0.5', true],
    ['juno-kanban 2.8.4', true],
    ['task 3.0.0', false],
    ['', false],
    ['not-a-version', false],
    ['task 2.0.5 extra 2.1.0', false],
  ])('validates %j', (output, accepted) => {
    const result = spawnSync(
      'bash',
      ['-c', 'source "$1"; juno_kanban_parse_compatible_version "$2"', 'test', policy, output],
      { encoding: 'utf8' },
    );
    expect(result.status === 0).toBe(accepted);
  });

  it.each([
    ['yylo-ledger 0.3.0', true],
    ['yylo-ledger 0.2.0', false],
    ['yylo-ledger 0.3.0rc1', false],
    ['yylo-ledger 0.4.0', false],
  ])('validates exact Ledger identity %j', (output, accepted) => {
    const result = spawnSync(
      'bash',
      ['-c', 'source "$1"; yylo_ledger_parse_compatible_version "$2"', 'test', policy, output],
      { encoding: 'utf8' },
    );
    expect(result.status === 0).toBe(accepted);
  });
});

describe('Juno 2 shipped guidance', () => {
  it('keeps aliases, compatibility, controller routing, and rollback boundaries aligned', async () => {
    const repositoryRoot = path.resolve(process.cwd(), '..');
    const read = (relative: string) => fs.readFile(path.join(repositoryRoot, relative), 'utf8');
    const [rootReadme, codeReadme, policyText, newTask, runWorkflow, cleanWorktree, parallelReview, reviewPrompt, agents] = await Promise.all([
      read('README.md'),
      read('juno-code/README.md'),
      read('juno-code/src/templates/scripts/juno-toolchain-policy.sh'),
      read('.juno_task/prompts/new_task_workflow.md'),
      read('.juno_task/prompts/run_workflow.md'),
      read('.juno_task/prompts/clean_worktree.md'),
      read('.juno_task/wiki/parallel_runner_and_spec_review.md'),
      read('.juno_task/prompts/review_commit_parallel_runner.md'),
      read('AGENTS.md'),
    ]);

    for (const documentation of [rootReadme, codeReadme]) {
      expect(documentation).toContain('yy-juno-002');
      expect(documentation).toContain('juno-kanban-juno-002');
      expect(documentation).toContain('>=2.0.5,<3.0.0');
      expect(documentation).toMatch(/(branch switch[^\n]*not[^\n]*(roll back|rollback|downgrade)|branches[^\n]*never[^\n]*(downgrade|restore))/i);
    }
    expect(policyText).toContain("JUNO_KANBAN_COMPAT_RANGE='>=2.0.5,<3.0.0'");
    expect(codeReadme).toContain('rollback-selection');
    expect(codeReadme).toContain('register-controller');
    expect(codeReadme).toContain('Controller |');
    expect(codeReadme).toContain('Small fix worktree |');

    for (const prompt of [newTask, runWorkflow, cleanWorktree]) {
      expect(prompt).toContain('controller');
      expect(prompt).toContain('TASK_ROOT');
      expect(prompt).toContain('yy task preflight TASK_ID');
      expect(prompt).toContain('../wiki/controller/task_dependency_hydration.md');
      expect(prompt).not.toContain('../wiki/task_dependency_hydration.md');
      expect(prompt).toMatch(/never (switches|silently switch)|never clean or switch/i);
    }
    for (const guidance of [cleanWorktree, parallelReview, reviewPrompt]) {
      expect(guidance).toContain('yy pi');
      expect(guidance).toMatch(/bare `pi`|bare pi/i);
      expect(guidance).toMatch(/provider\/model|provider and model/);
    }
    expect(agents).toContain('Implementation and repair agents never launch lifecycle-semantic reviewers');
    expect(agents).toContain('yy task preflight TASK_ID');

    const skillPaths = [
      '.pi/skills/kanban-workflow/SKILL.md',
      '.claude/skills/kanban-workflow/SKILL.md',
      'juno-code/src/templates/skills/pi/kanban-workflow/SKILL.md',
      'juno-code/src/templates/skills/claude/kanban-workflow/SKILL.md',
      'juno-code/src/templates/skills/codex/kanban-workflow/SKILL.md',
    ];
    const skills = await Promise.all(skillPaths.map(read));
    for (const skill of skills) {
      expect(skill).toContain('### Canonical Controller Routing');
      expect(skill).toContain('explicit `JUNO_TASK_ROOT`, repository-local registration, then the current project root');
      expect(skill).toContain('JUNO_WORKSPACE_ENFORCEMENT');
      expect(skill).toContain('product checkout separately as `TASK_ROOT`');
    }

    for (const name of [
      'life_cycle',
      'clean_worktree',
      'review_commit_parallel_runner',
      'reflect',
      'new_task_workflow',
      'run_workflow',
      'migrate_controller_wiki_and_hydration',
      'migrate_juno_code_v1_to_v2',
      'migrate_juno_code_v2_to_v2_1',
      'migrate_juno_kanban_v1_to_v2',
    ]) {
      expect(
        await read(`.juno_task/prompts/${name}.md`),
        `managed prompt drift: ${name}`,
      ).toBe(await read(`juno-code/src/templates/prompts/${name}.md`));
    }
  });
});

describe('repository-local Juno 2 source installer', () => {
  it('quotes source paths, repeats safely, preserves source identity, and rolls selector back', async () => {
    const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'juno 002 toolchain '));
    tempDirs.push(temp);
    const state = path.join(temp, 'state with spaces');
    const codeSource = path.join(temp, 'code source');
    const kanbanSource = path.join(temp, 'kanban source');
    const fakeBin = path.join(temp, 'fake bin');
    await fs.outputJson(path.join(codeSource, 'package.json'), { name: '@yylo/cli', version: '2.0.1' });
    await fs.outputFile(path.join(kanbanSource, 'setup.py'), '# fixture\n');

    const fakePython = path.join(fakeBin, 'python fixture');
    await executable(
      fakePython,
      `#!/usr/bin/env bash
set -eu
if [[ "$1 $2" == "-m venv" ]]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
  chmod +x "$3/bin/python"
  exit 0
fi
if [[ "$1 $2" == "-m pip" ]]; then
  target="$(cd "$(dirname "$0")" && pwd)/juno-kanban"
  printf '#!/usr/bin/env bash\\nprintf "task %%s\\n" "${'${FAKE_KANBAN_VERSION:-2.0.5}'}"\\n' > "$target"
  chmod +x "$target"
  exit 0
fi
exit 9
`,
    );
    const fakeNpm = path.join(fakeBin, 'npm fixture');
    await executable(
      fakeNpm,
      `#!/usr/bin/env bash
set -eu
[[ "${'${FAIL_NPM:-0}'}" == 1 ]] && exit 19
if [[ "$1" == ci ]]; then
  touch "${codeSource}/.npm-ci-ran"
  mkdir -p "${codeSource}/node_modules/.bin"
  touch "${codeSource}/node_modules/.bin/tsup"
  chmod +x "${codeSource}/node_modules/.bin/tsup"
  exit 0
fi
[[ "$1" == run ]] && exit 0
prefix=""
while [[ $# -gt 0 ]]; do
  [[ "$1" == --prefix ]] && { prefix="$2"; shift 2; continue; }
  shift
done
mkdir -p "$prefix/node_modules/.bin"
printf '#!/usr/bin/env bash\\necho 2.0.1\\n' > "$prefix/node_modules/.bin/yy"
chmod +x "$prefix/node_modules/.bin/yy"
`,
    );
    const env = {
      JUNO_002_STATE_DIR: state,
      JUNO_002_CODE_SOURCE: codeSource,
      JUNO_002_KANBAN_SOURCE: kanbanSource,
      JUNO_002_PYTHON: fakePython,
      JUNO_002_NPM: fakeNpm,
    };

    const first = run(['install'], env);
    expect(first.status, `${first.stdout}\n${first.stderr}`).toBe(0);
    expect(await fs.pathExists(path.join(codeSource, '.npm-ci-ran'))).toBe(true);
    const second = run(['install'], env);
    expect(second.status, `${second.stdout}\n${second.stderr}`).toBe(0);

    const interrupted = run(['install'], { ...env, FAKE_KANBAN_VERSION: '3.0.0', FAIL_NPM: '1' });
    expect(interrupted.status).toBe(1);
    expect(interrupted.stderr).toContain('previous executable selection was preserved');
    const preserved = run(['status'], env);
    expect(preserved.status, preserved.stderr).toBe(0);
    expect(await fs.readdir(path.join(state, 'generations'))).toHaveLength(2);

    const yy = spawnSync(path.join(state, 'bin', 'yy-juno-002'), ['--version'], {
      env: { ...process.env, JUNO_002_STATE_DIR: state }, encoding: 'utf8',
    });
    expect(yy.status, yy.stderr).toBe(0);
    expect(yy.stdout.trim()).toBe('2.0.1');
    const kanban = spawnSync(path.join(state, 'bin', 'juno-kanban-juno-002'), ['--version'], {
      env: { ...process.env, JUNO_002_STATE_DIR: state }, encoding: 'utf8',
    });
    expect(kanban.status, kanban.stderr).toBe(0);
    expect(kanban.stdout.trim()).toBe('task 2.0.5');

    const alternateCode = path.join(temp, 'alternate code');
    const alternateKanban = path.join(temp, 'alternate kanban');
    await executable(alternateCode, '#!/usr/bin/env bash\necho 2.0.1\n');
    await executable(alternateKanban, '#!/usr/bin/env bash\necho task 2.7.0\n');
    expect(run(['select', alternateCode, alternateKanban, codeSource, kanbanSource], env).status).toBe(0);
    expect(run(['rollback-selection'], env).status).toBe(0);
    const status = run(['status'], env);
    expect(status.status, status.stderr).toBe(0);
    expect(status.stdout).toContain(`juno_code_source=${await fs.realpath(codeSource)}`);
    expect(status.stdout).toContain(`juno_kanban_source=${await fs.realpath(kanbanSource)}`);
    expect(status.stdout).toContain('policy=>=2.0.5,<3.0.0');
  }, 30_000);

  it.each(['1.9.9', '2.0.4', '3.0.0'])('rejects source Kanban %s during install', async (version) => {
    const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-002-reject-'));
    tempDirs.push(temp);
    const codeSource = path.join(temp, 'code');
    const kanbanSource = path.join(temp, 'kanban');
    await fs.outputJson(path.join(codeSource, 'package.json'), { version: '2.0.1' });
    await fs.outputFile(path.join(kanbanSource, 'setup.py'), '# fixture\n');
    const fakePython = path.join(temp, 'python');
    await executable(fakePython, `#!/usr/bin/env bash
if [[ "$1 $2" == "-m venv" ]]; then mkdir -p "$3/bin"; cp "$0" "$3/bin/python"; chmod +x "$3/bin/python"; exit 0; fi
printf '#!/usr/bin/env bash\\necho task ${version}\\n' > "$(dirname "$0")/juno-kanban"; chmod +x "$(dirname "$0")/juno-kanban"
`);
    const fakeNpm = path.join(temp, 'npm');
    await executable(fakeNpm, '#!/usr/bin/env bash\n[[ "$1" == run ]] && exit 0\nwhile [[ $# -gt 0 ]]; do [[ "$1" == --prefix ]] && { p="$2"; shift 2; continue; }; shift; done\nmkdir -p "$p/node_modules/.bin"; printf "#!/usr/bin/env bash\\necho 2.0.1\\n" > "$p/node_modules/.bin/yy"; chmod +x "$p/node_modules/.bin/yy"\n');
    const result = run(['install'], {
      JUNO_002_STATE_DIR: path.join(temp, 'state'), JUNO_002_CODE_SOURCE: codeSource,
      JUNO_002_KANBAN_SOURCE: kanbanSource, JUNO_002_PYTHON: fakePython, JUNO_002_NPM: fakeNpm,
    });
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain('identity rejected');
  });
});
