import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import fs from 'fs-extra';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const templateWrapper = path.resolve(process.cwd(), 'src/templates/scripts/kanban.sh');
const templateResolver = path.resolve(process.cwd(), 'src/templates/scripts/controller_resolver.py');
const templatePolicy = path.resolve(process.cwd(), 'src/templates/scripts/juno-toolchain-policy.sh');

describe('kanban wrapper runtime selection', () => {
  let projectRoot: string;

  beforeEach(async () => {
    projectRoot = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'kanban-wrapper-runtime-')));

    const scriptsDir = path.join(projectRoot, '.juno_task', 'scripts');
    const venvBin = path.join(projectRoot, '.venv_juno', 'bin');
    const installedSite = path.join(projectRoot, 'installed-site');
    const localSource = path.join(projectRoot, 'juno_kanban', 'src', 'kanban');

    await Promise.all([
      fs.ensureDir(scriptsDir),
      fs.ensureDir(venvBin),
      fs.ensureDir(path.join(installedSite, 'kanban')),
      fs.ensureDir(localSource),
      fs.ensureDir(path.join(projectRoot, '.juno_task', 'tasks')),
    ]);

    await fs.copy(templateWrapper, path.join(scriptsDir, 'kanban.sh'));
    await fs.copy(templateResolver, path.join(scriptsDir, 'controller_resolver.py'));
    await fs.copy(templatePolicy, path.join(scriptsDir, 'juno-toolchain-policy.sh'));
    await fs.chmod(path.join(scriptsDir, 'kanban.sh'), 0o755);
    await fs.writeJson(path.join(projectRoot, '.juno_task', 'tasks', 'config.json'), {
      storage: 'legacy-ndjson',
    });
    await fs.writeJson(path.join(projectRoot, '.juno_task', 'config.json'), {});
    await fs.writeFile(path.join(projectRoot, '.juno_task', 'tasks', 'backlog.ndjson'), '');

    await fs.writeFile(path.join(installedSite, 'kanban', '__init__.py'), "RUNTIME = 'installed-controller-v2'\n");
    await fs.writeFile(path.join(localSource, '__init__.py'), "RUNTIME = 'local-v2'\n");
    await fs.writeFile(
      path.join(venvBin, 'activate'),
      `export VIRTUAL_ENV=${JSON.stringify(path.join(projectRoot, '.venv_juno'))}\nexport PATH=${JSON.stringify(venvBin)}:$PATH\n`,
    );
    await fs.writeFile(
      path.join(venvBin, 'juno-kanban'),
      `#!/usr/bin/env bash
if [[ "${'$'}{1:-}" == "--version" ]]; then
  if IFS= read -r unexpected; then echo "version probe consumed stdin: ${'$'}unexpected" >&2; exit 2; fi
  echo "task 2.0.5"
  exit 0
fi
config=""
if [[ "${'$'}{1:-}" == "--config" || "${'$'}{1:-}" == "-c" ]]; then
  config="${'$'}2"
  shift 2
fi
if [[ "${'$'}{1:-}" == "create" ]]; then
  body=${'$'}(cat)
  printf 'created:%s\\n' "${'$'}body"
  exit 0
fi
if [[ ${'$'}# -eq 0 ]]; then
  body=${'$'}(cat)
  printf 'implicit-created:%s\\n' "${'$'}body"
  exit 0
fi
if [[ "${'$'}{1:-}" == "show-env" ]]; then
  printf '%s\\n' "${'$'}{VIRTUAL_ENV:-}"
  exit 0
fi
if [[ "${'$'}{1:-}" == "get" && "${'$'}{2:-}" == "show-config" ]]; then
  printf '%s\\n' "${'$'}config"
  exit 0
fi
if [[ "${'$'}{1:-}" == "--project" ]]; then
  printf '%s|%s\\n' "${'$'}{JUNO_KANBAN_INVOCATION_ROOT:-}" "${'$'}*"
  exit 0
fi
python3 -c 'import os, kanban; print(kanban.RUNTIME + "|" + os.environ["JUNO_TASK_ROOT"])'
`,
    );
    await fs.chmod(path.join(venvBin, 'juno-kanban'), 0o755);
  });

  afterEach(async () => {
    await fs.remove(projectRoot);
  });

  it('preserves native format scope and legacy global normalization without changing payload arguments', async () => {
    const executable = path.join(projectRoot, '.venv_juno/bin/yylo-ledger');
    await fs.writeFile(executable, `#!/usr/bin/env python3
import json, sys
if sys.argv[1:] == ['--version']: print('yylo-ledger 0.4.0')
else: print(json.dumps(sys.argv[1:]))
`);
    await fs.chmod(executable, 0o755);
    const wrapper = path.join(projectRoot, '.juno_task/scripts/kanban.sh');
    for (const scope of ['record', 'task', 'wiki', 'workflow', 'artifact']) {
      for (const format of [['-f', 'json'], ['--format', 'json'], ['--format=ndjson']]) {
        const args = [scope, 'search', '--text', 'spaces and unicode λ', ...format, '--cursor', 'opaque=='];
        const result = spawnSync(wrapper, args, { cwd: projectRoot, encoding: 'utf8', env: { ...process.env, JUNO_TASK_ROOT: projectRoot } });
        expect(result.status, result.stderr).toBe(0);
        expect(JSON.parse(result.stdout)).toEqual(['--config', path.join(projectRoot, '.juno_task/config.json'), ...args]);
      }
    }
    const legacy = spawnSync(wrapper, ['search', '--body', 'needle', '-f', 'json', '--raw'], {
      cwd: projectRoot, encoding: 'utf8', env: { ...process.env, JUNO_TASK_ROOT: projectRoot },
    });
    expect(legacy.status, legacy.stderr).toBe(0);
    expect(JSON.parse(legacy.stdout)).toEqual(['--config', path.join(projectRoot, '.juno_task/config.json'), '-f', 'json', '--raw', 'search', '--body', 'needle']);
  });

  it('closes stdin for the identity probe without consuming a heredoc create body', () => {
    const result = spawnSync(path.join(projectRoot, '.juno_task', 'scripts', 'kanban.sh'), ['create'], {
      cwd: projectRoot,
      encoding: 'utf8',
      input: 'heredoc regression body\n',
      env: {
        ...process.env,
        JUNO_CONTROLLER_BRANCH: '',
        JUNO_WORKSPACE_ENFORCEMENT: 'off',
        JUNO_WORKSPACE_ROLE: '',
        JUNO_TASK_ROOT: '',
        VIRTUAL_ENV: '',
        PYTHONPATH: path.join(projectRoot, 'installed-site'),
      },
    });

    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout.trim()).toBe('created:heredoc regression body');
    expect(result.stderr).not.toContain('version probe consumed stdin');
  });

  it('forwards commandless heredoc input for the implicit create shortcut', () => {
    const result = spawnSync(path.join(projectRoot, '.juno_task', 'scripts', 'kanban.sh'), [], {
      cwd: projectRoot,
      encoding: 'utf8',
      input: 'commandless heredoc body\n',
      env: {
        ...process.env,
        JUNO_CONTROLLER_BRANCH: '',
        JUNO_WORKSPACE_ENFORCEMENT: 'off',
        JUNO_WORKSPACE_ROLE: '',
        JUNO_TASK_ROOT: '',
        VIRTUAL_ENV: '',
        PYTHONPATH: path.join(projectRoot, 'installed-site'),
      },
    });

    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout.trim()).toBe('implicit-created:commandless heredoc body');
    expect(result.stderr).not.toContain('version probe consumed stdin');
  });

  it('reactivates this project when the caller inherited another project .venv_juno', () => {
    const otherVenv = path.join(path.dirname(projectRoot), 'other-project', '.venv_juno');
    const result = spawnSync(path.join(projectRoot, '.juno_task', 'scripts', 'kanban.sh'), ['show-env'], {
      cwd: projectRoot,
      encoding: 'utf8',
      env: {
        ...process.env,
        JUNO_CONTROLLER_BRANCH: '',
        JUNO_WORKSPACE_ENFORCEMENT: 'off',
        JUNO_WORKSPACE_ROLE: '',
        JUNO_TASK_ROOT: '',
        VIRTUAL_ENV: otherVenv,
        PATH: `${path.join(otherVenv, 'bin')}:${process.env.PATH}`,
      },
    });

    expect(result.status, result.stderr).toBe(0);
    expect(fs.realpathSync(result.stdout.trim())).toBe(fs.realpathSync(path.join(projectRoot, '.venv_juno')));
  });

  it('binds config discovery to the resolved controller and preserves explicit override', async () => {
    const wrapper = path.join(projectRoot, '.juno_task', 'scripts', 'kanban.sh');
    const env = {
      ...process.env,
      JUNO_CONTROLLER_BRANCH: '',
      JUNO_WORKSPACE_ENFORCEMENT: 'off',
      JUNO_WORKSPACE_ROLE: '',
      JUNO_TASK_ROOT: '',
      VIRTUAL_ENV: '',
    };
    const defaultResult = spawnSync(wrapper, ['get', 'show-config'], {
      cwd: projectRoot, encoding: 'utf8', env,
    });
    expect(defaultResult.status, defaultResult.stderr).toBe(0);
    expect(fs.realpathSync(defaultResult.stdout.trim())).toBe(
      fs.realpathSync(path.join(projectRoot, '.juno_task', 'config.json')),
    );

    const override = path.join(projectRoot, 'maintenance.json');
    await fs.writeJson(override, {});
    const overrideResult = spawnSync(wrapper, ['get', 'show-config', '--config', override], {
      cwd: projectRoot, encoding: 'utf8', env,
    });
    expect(overrideResult.status, overrideResult.stderr).toBe(0);
    expect(fs.realpathSync(overrideResult.stdout.trim())).toBe(fs.realpathSync(override));

    const mutationOverride = spawnSync(wrapper, ['create', '--body', 'must-not-run', '--config', override], {
      cwd: projectRoot, encoding: 'utf8', env,
    });
    expect(mutationOverride.status).not.toBe(0);
    expect(mutationOverride.stderr).toContain('mutation config must be canonical controller config');
    expect(mutationOverride.stdout).toBe('');
  });

  it('normalizes --project and preserves the initialized source root', () => {
    const result = spawnSync(
      path.join(projectRoot, '.juno_task', 'scripts', 'kanban.sh'),
      ['list', '--project', 'destination'],
      {
        cwd: projectRoot,
        encoding: 'utf8',
        env: {
          ...process.env,
          JUNO_CONTROLLER_BRANCH: '',
          JUNO_WORKSPACE_ENFORCEMENT: 'off',
          JUNO_WORKSPACE_ROLE: '',
          JUNO_TASK_ROOT: '',
          VIRTUAL_ENV: '',
          PYTHONPATH: path.join(projectRoot, 'installed-site'),
        },
      },
    );

    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout.trim()).toBe(
      `${fs.realpathSync(projectRoot)}|--project destination list`,
    );
  });

  it('prefers the canonical yylo-ledger runtime silently and rejects malformed identity', async () => {
    const venvBin = path.join(projectRoot, '.venv_juno', 'bin');
    await fs.writeFile(
      path.join(venvBin, 'yylo-ledger'),
      `#!/usr/bin/env bash
if [[ "${'$'}{1:-}" == "--version" ]]; then
  printf 'yylo-ledger 0.4.0\\n'
  exit 0
fi
printf 'yylo-ledger-runtime\\n'
`,
    );
    await fs.chmod(path.join(venvBin, 'yylo-ledger'), 0o755);

    const env = {
      ...process.env,
      JUNO_CONTROLLER_BRANCH: '',
      JUNO_WORKSPACE_ENFORCEMENT: 'off',
      JUNO_WORKSPACE_ROLE: '',
      JUNO_TASK_ROOT: '',
      VIRTUAL_ENV: '',
    };
    const selected = spawnSync(path.join(projectRoot, '.juno_task', 'scripts', 'kanban.sh'), ['list'], {
      cwd: projectRoot,
      encoding: 'utf8',
      env,
    });
    expect(selected.status, selected.stderr).toBe(0);
    expect(selected.stdout.trim()).toBe('yylo-ledger-runtime');
    expect(selected.stderr).not.toContain('juno-kanban identity:');
    expect(selected.stderr).not.toContain('policy=');

    await fs.writeFile(
      path.join(venvBin, 'yylo-ledger'),
      `#!/usr/bin/env bash
if [[ "${'$'}{1:-}" == "--version" ]]; then
  printf 'juno-kanban is deprecated; install yylo-ledger and use yylo-ledger instead.\\nyylo-ledger 0.9.0\\n'
  exit 0
fi
printf 'must-not-run\\n'
`,
    );
    await fs.chmod(path.join(venvBin, 'yylo-ledger'), 0o755);
    const rejected = spawnSync(path.join(projectRoot, '.juno_task', 'scripts', 'kanban.sh'), ['list'], {
      cwd: projectRoot,
      encoding: 'utf8',
      env,
    });
    expect(rejected.status).not.toBe(0);
    expect(rejected.stdout).toBe('');
    expect(rejected.stderr).toContain('identity rejected');
  });

  it('uses the compatible controller executable even when a neighboring source tree is present', () => {
    const result = spawnSync(path.join(projectRoot, '.juno_task', 'scripts', 'kanban.sh'), ['list'], {
      cwd: projectRoot,
      encoding: 'utf8',
      env: {
        ...process.env,
        JUNO_CONTROLLER_BRANCH: '',
        JUNO_WORKSPACE_ENFORCEMENT: 'off',
        JUNO_WORKSPACE_ROLE: '',
        JUNO_TASK_ROOT: '',
        VIRTUAL_ENV: '',
        PYTHONPATH: path.join(projectRoot, 'installed-site'),
      },
    });

    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout.trim()).toBe(`installed-controller-v2|${fs.realpathSync(projectRoot)}`);
  });

  it('does not start the E2E write validator for read-only commands', async () => {
    await fs.writeFile(
      path.join(projectRoot, '.juno_task', 'scripts', 'e2e_housekeeping.py'),
      'raise SystemExit("read command incorrectly entered write validator")\n',
    );

    const result = spawnSync(path.join(projectRoot, '.juno_task', 'scripts', 'kanban.sh'), ['search', '--body', 'needle'], {
      cwd: projectRoot,
      encoding: 'utf8',
      env: {
        ...process.env,
        JUNO_CONTROLLER_BRANCH: '',
        JUNO_WORKSPACE_ENFORCEMENT: 'off',
        JUNO_WORKSPACE_ROLE: '',
        JUNO_TASK_ROOT: '',
        VIRTUAL_ENV: '',
        PYTHONPATH: path.join(projectRoot, 'installed-site'),
      },
    });

    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout.trim()).toBe(`installed-controller-v2|${fs.realpathSync(projectRoot)}`);
  });

  it('keeps create routed through the E2E write validator when installed', async () => {
    await fs.writeFile(
      path.join(projectRoot, '.juno_task', 'scripts', 'e2e_housekeeping.py'),
      'import sys\nprint("validator:" + "|".join(sys.argv[1:]))\n',
    );

    const result = spawnSync(path.join(projectRoot, '.juno_task', 'scripts', 'kanban.sh'), ['create', '--body', 'task'], {
      cwd: projectRoot,
      encoding: 'utf8',
      env: {
        ...process.env,
        JUNO_CONTROLLER_BRANCH: '',
        JUNO_WORKSPACE_ENFORCEMENT: 'off',
        JUNO_WORKSPACE_ROLE: '',
        JUNO_TASK_ROOT: '',
        VIRTUAL_ENV: '',
        PYTHONPATH: path.join(projectRoot, 'installed-site'),
      },
    });

    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout).toContain('validator:validate-kanban-write');
    expect(result.stdout).toContain('create|--body|task');
  });
});
