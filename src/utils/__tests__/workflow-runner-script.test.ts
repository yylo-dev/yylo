import { describe, it, expect, beforeEach, afterEach, afterAll, vi } from 'vitest';
import fs from 'fs-extra';
import * as path from 'node:path';
import * as os from 'node:os';
import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { contentionBudgetMs } from '../../test-utils/contention-budget.js';

const repoRoot = path.resolve(process.cwd(), '..');
const templateScript = path.resolve(process.cwd(), 'src/templates/scripts/workflow_runner.sh');
const runtimeScript = path.resolve(repoRoot, '.juno_task/scripts/workflow_runner.sh');
// Workflow children spawn real interpreters; their bounded operation budget must
// tolerate shared-host contention or every lane fails together under load.
const WORKFLOW_CHILD_TIMEOUT_MS = contentionBudgetMs(120_000);
let workflowFixtureController: string | undefined;
let workflowFixturePythonExecutable: string | undefined;

type FixtureDiscoveryResult = {
  error?: Error;
  status: number | null;
  stdout: string | Buffer | null;
  stderr: string | Buffer | null;
};

function requireFixtureDiscoveryOutput(
  label: string,
  result: FixtureDiscoveryResult,
): string {
  const stdout = result.stdout?.toString().trim() ?? '';
  const stderr = result.stderr?.toString().trim() ?? '';
  if (result.error || result.status !== 0 || !stdout) {
    const detail = result.error?.message || stderr || `exit=${String(result.status)}`;
    throw new Error(`Workflow Runner fixture dependency discovery failed for ${label}: ${detail}`);
  }
  return stdout;
}

function cleanPythonDiscoveryEnvironment(): NodeJS.ProcessEnv {
  const env = { ...process.env };
  delete env.PYTHONHOME;
  delete env.PYTHONPATH;
  delete env.VIRTUAL_ENV;
  return env;
}

type FixturePython = {
  executable: string;
  version: string;
  yamlPackage: string;
};

type FixtureSpawn = (
  command: string,
  args: string[],
  options: { encoding: 'utf8'; env: NodeJS.ProcessEnv },
) => FixtureDiscoveryResult;

function fixturePythonCandidates(start: string): string[] {
  const candidates: string[] = [];
  let cursor = path.resolve(start);
  for (let depth = 0; depth < 10; depth += 1) {
    candidates.push(path.join(cursor, '.venv_juno', 'bin', 'python'));
    const parent = path.dirname(cursor);
    if (parent === cursor) break;
    cursor = parent;
  }
  candidates.push('python3');
  return [...new Set(candidates)];
}

function discoverFixturePython(start: string, spawnFixture: FixtureSpawn): FixturePython {
  const environment = cleanPythonDiscoveryEnvironment();
  const diagnostics: string[] = [];
  const discoveryCode = [
    'import json, pathlib, sys, yaml',
    'print(json.dumps({',
    "  'executable': str(pathlib.Path(sys.executable).resolve()),",
    "  'version': f'{sys.version_info.major}.{sys.version_info.minor}',",
    "  'yamlPackage': str(pathlib.Path(yaml.__file__).parent.resolve()),",
    '}))',
  ].join('\n');

  for (const candidate of fixturePythonCandidates(start)) {
    const result = spawnFixture(candidate, ['-c', discoveryCode], {
      encoding: 'utf8',
      env: environment,
    });
    try {
      const output = requireFixtureDiscoveryOutput(candidate, result);
      const parsed = JSON.parse(output) as Partial<FixturePython>;
      if (!parsed.executable || !parsed.version || !parsed.yamlPackage) {
        throw new Error('discovery payload omitted executable, version, or yamlPackage');
      }
      return parsed as FixturePython;
    } catch (error) {
      diagnostics.push(`${candidate}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  throw new Error(`Workflow Runner fixture could not find a PyYAML-capable Python:\n${diagnostics.join('\n')}`);
}

// This process-heavy file runs real Python/Git subprocesses. Keep the larger
// budget file-scoped while every child remains independently bounded above.
vi.setConfig({ testTimeout: contentionBudgetMs(300_000), hookTimeout: contentionBudgetMs(60_000) });
afterAll(() => vi.resetConfig());

function runWorkflowScript(scriptPath: string, args: string[], input?: string, env?: NodeJS.ProcessEnv) {
  if (!workflowFixtureController) throw new Error('workflow subprocess fixture controller is not initialized');
  return spawnSync('python3', [scriptPath, ...args], {
    input,
    // Never let a default workflow subprocess discover the real task/controller
    // checkout. Dedicated integration fixtures still use real Git explicitly.
    cwd: workflowFixtureController,
    encoding: 'utf8',
    timeout: WORKFLOW_CHILD_TIMEOUT_MS,
    env: {
      ...process.env,
      ...env,
      // Test-specific command settings may inherit routing from the ambient
      // repository, but this subprocess belongs to the synthetic controller.
      JUNO_TASK_ROOT: workflowFixtureController,
      JUNO_CONTROLLER_BRANCH: 'fixture-controller',
      JUNO_WORKSPACE_ROLE: 'controller',
      JUNO_WORKSPACE_ENFORCEMENT: 'strict',
      YYLO_SESSION_METADATA_DIRECTORY: path.join(workflowFixtureController, '.test-metadata'),
    },
  });
}

function runWorkflow(args: string[], input?: string, env?: NodeJS.ProcessEnv) {
  return runWorkflowScript(templateScript, args, input, env);
}

const fakeScopeResolver = `
def resolve_scope(parent_pid, cwd):
    override = os.environ.get('YYLO_CONTINUE_SCOPE', '').strip()
    if override:
        descriptor = 'YYLO_CONTINUE_SCOPE:' + override
    else:
        project = str(pathlib.Path(cwd).resolve())
        marker = next((f'{key}:{os.environ[key].strip()}' for key in ['TMUX_PANE','WEZTERM_PANE','KITTY_WINDOW_ID','KITTY_PID','TERM_SESSION_ID','WT_SESSION','ZELLIJ_PANE_ID','STY','WINDOWID','SSH_TTY'] if os.environ.get(key, '').strip()), None)
        if marker:
            descriptor = '\\n'.join(['PROJECT:' + project, 'STABLE_TERMINAL:' + marker])
        else:
            lineage, current = [], parent_pid
            for _ in range(8):
                if not current or current <= 0 or current in lineage: break
                lineage.append(current)
                try:
                    out = subprocess.run(['ps', '-o', 'ppid=', '-p', str(current)], text=True, capture_output=True, timeout=0.5, check=False).stdout.strip()
                    current = int(out) if out else 0
                except Exception: current = 0
            descriptor = '\\n'.join(['PROJECT:' + project, 'SHELL_LINEAGE:' + ('>'.join(map(str, lineage)) if lineage else str(parent_pid))])
    digest = hashlib.sha256(descriptor.encode()).hexdigest()[:16].upper()
    scope = 'SCOPE_' + digest
    return scope

def continuity_path():
    root = pathlib.Path(os.environ.get('YYLO_SESSION_METADATA_DIRECTORY', pathlib.Path.cwd() / '.juno_task'))
    root.mkdir(parents=True, exist_ok=True)
    return root / 'session_continuity.v2.json'

def persist_scope(scope, session_id, settings):
    state_path = continuity_path()
    try: document = json.loads(state_path.read_text())
    except Exception: document = {'version': 2, 'scopes': {}}
    now = '2026-07-30T00:00:00.000Z'
    previous = document.setdefault('scopes', {}).get(scope, {})
    document['scopes'][scope] = {'source':'test','createdAt':previous.get('createdAt', now),'lastUsedAt':now,'pinned':False,'settings':settings,'active':'main','branches':{'main':{'session_id':session_id,'parent':None,'updated_at':now}}}
    state_path.write_text(json.dumps(document))

def handle_scope_command():
    if len(sys.argv) < 2 or sys.argv[1] != 'continue-scope': return False
    cwd = sys.argv[sys.argv.index('--cwd') + 1]
    parent_pid = int(sys.argv[sys.argv.index('--parent-pid') + 1])
    scope = resolve_scope(parent_pid, cwd)
    if '--handoff-session' in sys.argv:
        session_id = sys.argv[sys.argv.index('--handoff-session') + 1]
        settings = json.loads(sys.argv[sys.argv.index('--handoff-settings') + 1])
        persist_scope(scope, session_id, settings)
    try:
        document = json.loads(continuity_path().read_text())
        scopes = document.get('scopes', {})
        entry = scopes.get(scope, {})
        if not entry and len(scopes) == 1: entry = next(iter(scopes.values()))
        session_id = entry.get('branches', {}).get(entry.get('active'), {}).get('session_id')
    except Exception: session_id = None
    print(json.dumps({'status':'finished' if session_id else 'not_found','hash':scope[6:12],'fullHash':scope,'scopeSource':'test','sessionEnvKey':'YYLO_LAST_SESSION_ID_' + scope,'settingsEnvKey':'YYLO_LAST_EXECUTION_SETTINGS_' + scope,'sessionId':session_id,'isCurrentScope':True,'pid':None}))
    return True
`;

function canonicalJson(value: unknown): string {
  const sort = (item: unknown): unknown => {
    if (Array.isArray(item)) return item.map(sort);
    if (item !== null && typeof item === 'object') {
      return Object.fromEntries(Object.entries(item).sort(([left], [right]) => left.localeCompare(right))
        .map(([key, child]) => [key, sort(child)]));
    }
    return item;
  };
  return `${JSON.stringify(sort(value))}\n`;
}

async function installCanonicalMetadataControllerContract(controller: string) {
  const scripts = path.join(controller, '.juno_task', 'scripts');
  await fs.copyFile(path.resolve(process.cwd(), 'src/templates/scripts/metadata_controller.py'), path.join(scripts, 'metadata_controller.py'));
  const policy = await fs.readJson(path.resolve(process.cwd(), 'src/templates/config/metadata-controller.json'));
  policy.controller_branch = 'refs/heads/fixture-controller';
  for (const field of ['copied_metadata', 'generated_metadata', 'product_forbidden', 'tracked_exact',
    'tracked_recursive', 'tracked_top_level_files']) {
    policy[field].sort();
  }
  policy.runtime.ignored_roots.sort();
  await fs.ensureDir(path.join(controller, '.juno_task', 'config'));
  await fs.writeFile(path.join(controller, '.juno_task', 'config', 'metadata-controller.json'), canonicalJson(policy));
}

async function installFakeJunoExecutable(dir: string, name = 'yy') {
  const binDir = path.join(dir, 'bin');
  await fs.ensureDir(binDir);
  const executablePath = path.join(binDir, name);
  await fs.writeFile(
    executablePath,
    `#!/usr/bin/env python3
import hashlib, json, os, pathlib, subprocess, sys
${fakeScopeResolver}
if handle_scope_command(): raise SystemExit(0)
if len(sys.argv) > 1 and sys.argv[1] == '--quiet': sys.argv.pop(1)
prompt = sys.argv[3] if len(sys.argv) > 3 else sys.argv[2]
print(f"tool={os.environ.get('JUNO_TOOL_ID', 'unset')} capture={os.environ.get('JUNO_SUBAGENT_CAPTURE_PATH', 'unset')}")
capture = os.environ.get('JUNO_SUBAGENT_CAPTURE_PATH')
if capture:
    pathlib.Path(capture).write_text('{invalid json' if prompt == 'invalid' else json.dumps({'type':'result','subtype':'success','is_error':False,'result':'captured ' + prompt,'session_id':'session-' + prompt}) + '\\n')
if prompt != 'invalid': persist_scope(resolve_scope(os.getppid(), pathlib.Path.cwd()), 'session-' + prompt, {'version':1,'subagent':'pi'})
if prompt == 'fail': raise SystemExit(7)
`,
  );
  await fs.chmod(executablePath, 0o755);
  return { binDir, executablePath };
}

async function installFakeChildEvidenceProducer(dir: string) {
  const script = path.join(dir, 'integration_owner_preflight.py');
  await fs.writeFile(script, `#!/usr/bin/env python3
import datetime, hashlib, json, os, pathlib
root = pathlib.Path(os.environ['JUNO_WORKFLOW_CHILD_EVIDENCE_DIR'])
root.mkdir(parents=True, exist_ok=True)
def put(name, value):
    path = root / name
    path.write_text(value, encoding='utf-8')
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
stdout = put('stdout.txt', 'child log\\n')
stderr = put('stderr.txt', '')
response = put('response.txt', 'actual target accepted\\n')
capture = put('capture.json', json.dumps({'session_id':'child-session-1','result':'actual target accepted'}) + '\\n')
receipt_path = root / 'actual-review.json'
receipt_path.write_text(json.dumps({'reviewed_tip':'a' * 40, 'passed':True}) + '\\n', encoding='utf-8')
receipt = {'path': str(receipt_path.resolve()), 'sha256': hashlib.sha256(receipt_path.read_bytes()).hexdigest()}
now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
event = {
 'schema_version':'juno_workflow_child_step.v1', 'child_id':'actual_target_review',
 'role':'actual_target_review', 'parent_workflow_id':os.environ['JUNO_WORKFLOW_ID'],
 'parent_run_id':os.environ['JUNO_WORKFLOW_RUN_ID'], 'parent_step_id':os.environ['JUNO_WORKFLOW_STEP_ID'],
 'parent_step_digest':os.environ['JUNO_WORKFLOW_STEP_DIGEST'], 'invocation_mode':'fresh_session',
 'rendered_command_sha256':hashlib.sha256(b'yy pi review').hexdigest(),
 'rendered_argv_sha256':hashlib.sha256(b'argv').hexdigest(),
 'rendered_prompt_sha256':hashlib.sha256(b'review').hexdigest(), 'started_at':now,
 'completed_at':now, 'duration_seconds':0.01, 'exit_code':0, 'transport_status':'success',
 'semantic_outcome':'accepted', 'session_id':'child-session-1', 'reviewed_target_sha':'a' * 40,
 'artifacts':{'stdout':stdout,'stderr':stderr,'response':response,'capture':capture,'review_receipt':receipt}
}
(root / 'actual_target_review.event.json').write_text(json.dumps(event, sort_keys=True) + '\\n', encoding='utf-8')
print('parent complete')
`);
  await fs.chmod(script, 0o755);
  return script;
}

async function installFakeTopLevelPersistingJuno(dir: string, name = 'yy') {
  const binDir = path.join(dir, 'bin');
  await fs.ensureDir(binDir);
  const executablePath = path.join(binDir, name);
  await fs.writeFile(
    executablePath,
    `#!/usr/bin/env python3
import hashlib, json, os, pathlib, subprocess, sys
${fakeScopeResolver}
if handle_scope_command(): raise SystemExit(0)
prompt = sys.argv[-1] if len(sys.argv) > 1 else 'empty'
scope = resolve_scope(os.getppid(), pathlib.Path.cwd())
persist_scope(scope, 'session-' + prompt, {'version': 1, 'subagent': 'pi'})
print('FINAL ' + prompt)
print('Session ID(s):')
print('  session-' + prompt)
`,
  );
  await fs.chmod(executablePath, 0o755);
  return { binDir, executablePath };
}

describe('workflow_runner.sh child environment contract', () => {
  it('filters continuity from workflow child environments without dropping routing/config', () => {
    for (const script of [templateScript, runtimeScript]) {
      const code = `
import importlib.machinery, json
mod = importlib.machinery.SourceFileLoader('workflow_runner', ${JSON.stringify(script)}).load_module()
env = mod.child_process_environment({
  'YYLO_LAST_SESSION_ID_SCOPE_0123456789ABCDEF': 'historical',
  'YYLO_LAST_SESSION_ID_SCOPE_malformed_old_suffix': 'historical-malformed',
  'YYLO_LAST_EXECUTION_SETTINGS_SCOPE_': 'historical-empty-suffix',
  'YYLO_LAST_EXECUTION_SETTINGS': 'legacy',
  'JUNO_TASK_ROOT': '/controller',
  'ARBITRARY_CONFIG': 'preserved',
})
print(json.dumps({'continuity': sorted(k for k in env if k.startswith('YYLO_LAST_')), 'root': env.get('JUNO_TASK_ROOT'), 'config': env.get('ARBITRARY_CONFIG')}))
`;
      const result = spawnSync('python3', ['-c', code], { cwd: repoRoot, encoding: 'utf8' });
      expect(result.status, result.stderr).toBe(0);
      expect(JSON.parse(result.stdout.trim())).toEqual({ continuity: [], root: '/controller', config: 'preserved' });
    }
  });
});

describe('workflow_runner.sh template script', () => {
  let testDir: string;

  beforeEach(async () => {
    // Other fast-suite files intentionally mock node:child_process. Resolve the
    // real implementation explicitly so this process-heavy fixture cannot
    // inherit a hoisted mock when Vitest co-locates files in one worker.
    const { spawnSync: nativeSpawnSync } = await vi.importActual<
      typeof import('node:child_process')
    >('node:child_process');
    testDir = await fs.mkdtemp(path.join(os.tmpdir(), 'workflow-runner-test-'));
    workflowFixtureController = path.join(testDir, 'controller');
    const fixtureScripts = path.join(workflowFixtureController, '.juno_task', 'scripts');
    await fs.ensureDir(fixtureScripts);
    await fs.copyFile(
      path.resolve(process.cwd(), 'src/templates/scripts/controller_resolver.py'),
      path.join(fixtureScripts, 'controller_resolver.py'),
    );
    const fixtureBin = path.join(workflowFixtureController, '.venv_juno', 'bin');
    await fs.ensureDir(fixtureBin);
    // Prefer a managed ancestor interpreter (including the controller that owns
    // merge candidates), then accept ambient python3 only when that exact
    // interpreter proves it owns PyYAML. Candidate checkouts intentionally do
    // not depend on user-site packages.
    const fixturePython = discoverFixturePython(process.cwd(), nativeSpawnSync);
    const realPythonExecutable = fixturePython.executable;
    workflowFixturePythonExecutable = realPythonExecutable;
    await fs.symlink(realPythonExecutable, path.join(fixtureBin, 'python'));
    const pythonVersion = fixturePython.version;
    await fs.writeFile(
      path.join(workflowFixtureController, '.venv_juno', 'pyvenv.cfg'),
      `home = ${path.dirname(realPythonExecutable)}\ninclude-system-site-packages = false\nversion = ${pythonVersion}\n`,
    );
    const yamlPackage = fixturePython.yamlPackage;
    const managedSitePackages = path.join(workflowFixtureController, '.venv_juno', 'lib', `python${pythonVersion}`, 'site-packages');
    await fs.ensureDir(managedSitePackages);
    await fs.copy(yamlPackage, path.join(managedSitePackages, 'yaml'));
    nativeSpawnSync('git', ['init', '-b', 'fixture-controller'], {
      cwd: workflowFixtureController,
      encoding: 'utf8',
    });
  }, 30_000);

  afterEach(async () => {
    workflowFixtureController = undefined;
    workflowFixturePythonExecutable = undefined;
    await fs.remove(testDir);
  }, 30_000);

  it('keeps the packaged template as the Workflow Runner source of truth', async () => {
    expect(await fs.pathExists(templateScript)).toBe(true);
    const templateContent = await fs.readFile(templateScript, 'utf8');
    expect(templateContent).not.toContain('_dt.UTC');
    expect(templateContent).not.toContain('def canonicalize_working_directory');
    expect(templateContent).not.toContain('def resolve_parent_pid');
    expect(templateContent).not.toContain('def build_parent_shell_lineage');
    expect(templateContent).not.toContain('def collect_terminal_scope_markers');
    expect(templateContent).not.toContain('def resolve_continue_scope_context');
    expect(templateContent).toContain('continue-scope');
    expect(templateContent).toContain('--parent-pid');
    expect(templateContent).not.toContain('def command_owns_actual_review_child');
    expect(templateContent).not.toContain('env["JUNO_WORKFLOW_DIRECT_OWNER"]');
    expect(templateContent).toContain('legacy local_integration execution is read-only');
    expect(templateContent).toContain('yy task start TASK_ID');
    expect(templateContent).toContain('yy merge land TASK_ID');
    expect(templateContent).not.toContain('yy merge next');
    expect(templateContent).not.toContain('local_integration requires schema_version: 2');
    expect(templateContent).toContain('required_fields must include producer_step_digest');
    expect(templateContent).not.toContain('--checkpoint-controller');
  });

  it('fails fixture dependency discovery before an empty path reaches the filesystem', () => {
    expect(() => requireFixtureDiscoveryOutput('PyYAML package', {
      status: 1,
      stdout: '',
      stderr: 'ModuleNotFoundError: No module named yaml',
    })).toThrow(
      'Workflow Runner fixture dependency discovery failed for PyYAML package: ModuleNotFoundError: No module named yaml',
    );
  });

  it('selects an ancestor managed interpreter when ambient Python lacks PyYAML', () => {
    const start = '/controller/.juno_task/runtime/merge-queue/candidates/task-candidate/yylo';
    const managed = '/controller/.venv_juno/bin/python';
    const selected = discoverFixturePython(start, (command) => command === managed
      ? {
          status: 0,
          stdout: JSON.stringify({ executable: managed, version: '3.13', yamlPackage: '/managed/yaml' }),
          stderr: '',
        }
      : {
          status: 1,
          stdout: '',
          stderr: command === 'python3' ? 'ModuleNotFoundError: No module named yaml' : 'not found',
        });
    expect(selected).toEqual({ executable: managed, version: '3.13', yamlPackage: '/managed/yaml' });
  });

  it('reports every attempted interpreter when none owns PyYAML', () => {
    expect(() => discoverFixturePython('/isolated/task/yylo', (command) => ({
      status: 1,
      stdout: '',
      stderr: command === 'python3' ? 'ModuleNotFoundError: No module named yaml' : 'not found',
    }))).toThrow(/could not find a PyYAML-capable Python:[\s\S]*python3:[\s\S]*ModuleNotFoundError/);
  });

  it('delegates managed-agent steps, binds live artifacts, fails closed, and recovery detects drift', async () => {
    const controller = await fs.realpath(workflowFixtureController!);
    const scripts = path.join(controller, '.juno_task', 'scripts');
    await fs.copyFile(path.resolve(process.cwd(), 'src/templates/scripts/managed_agent_runner.py'), path.join(scripts, 'managed_agent_runner.py'));
    await fs.copyFile(path.resolve(process.cwd(), 'src/templates/scripts/invocation_correlation.py'), path.join(scripts, 'invocation_correlation.py'));
    await fs.writeFile(path.join(controller, '.juno_task', 'config.json'), canonicalJson({
      controllerWorkspace: { mode: 'metadata-only', policy: '.juno_task/config/metadata-controller.json' },
    }));
    await fs.writeFile(path.join(controller, '.gitignore'), '.venv_juno/\n.test-metadata/\n__pycache__/\n*.pyc\n');
    spawnSync('git', ['add', '.'], { cwd: controller });
    spawnSync('git', ['-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'incomplete controller'], { cwd: controller });
    const candidate = path.join(testDir, 'candidate');
    await fs.ensureDir(candidate); spawnSync('git', ['init', '-b', 'candidate'], { cwd: candidate });
    await fs.writeFile(path.join(candidate, 'tracked.txt'), 'candidate\n'); spawnSync('git', ['add', '.'], { cwd: candidate });
    spawnSync('git', ['-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'candidate'], { cwd: candidate });
    const candidateSha = spawnSync('git', ['rev-parse', 'HEAD'], { cwd: candidate, encoding: 'utf8' }).stdout.trim();
    const fake = await installFakeJunoExecutable(testDir);
    await fs.writeFile(fake.executablePath, `#!/usr/bin/env python3
import json,os,pathlib,sys,time
prompt=pathlib.Path(sys.argv[sys.argv.index('-f')+1]).read_text()
print('live stdout',flush=True);print('live stderr',file=sys.stderr,flush=True);time.sleep(.05)
payload={'session_id':'managed-session','result':'managed response'}
if 'typed-' in prompt: payload['terminal_outcome']={'schema_version':'juno_managed_agent_terminal_result.v1','state':prompt.split('typed-',1)[1].split()[0]}
if 'semantic-failure' in prompt: payload['is_error']=True
if 'stage-mutation' in prompt: pathlib.Path(${JSON.stringify(path.join(candidate, 'future-child.txt'))}).write_text('early mutation\\n')
pathlib.Path(os.environ['JUNO_SUBAGENT_CAPTURE_PATH']).write_text(json.dumps(payload)+'\\n')
`); await fs.chmod(fake.executablePath, 0o755);
    const execute = async (name: string, promptText: string) => {
      const prompt = path.join(testDir, `${name}.md`); await fs.writeFile(prompt, promptText);
      const runDir = path.join(testDir, name);
      const workflowPath = path.join(testDir, `${name}.json`);
      await fs.writeJson(workflowPath, {
        schema_version: 1,
        workflow_id: `managed_${name}`,
        managed_agent_policy: { external_side_effects: 'forbidden', lifecycle_hooks: 'disabled' },
        steps: [{
          id: 'agent', fail_workflow: false, capture_session: false,
          stage_boundary: { root: candidate, admitted_paths: ['tracked.txt'] },
          managed_agent: { mode: 'reviewer', controller_root: controller, controller_branch: 'fixture-controller',
            agent_root: '{{ out_dir }}/managed/agent-root', prompt_file: prompt, out_dir: '{{ out_dir }}/managed',
            candidate_sha: candidateSha, candidate_root: candidate,
            ...(promptText.includes('typed-') ? { require_terminal_result: true } : {}) },
        }],
      });
      const result = runWorkflow(['--workflow', workflowPath, '--out-dir', runDir, '--print-output', 'none'], undefined,
        { PATH: `${fake.binDir}${path.delimiter}${process.env.PATH}` });
      return { result, runDir, workflowPath };
    };
    const incomplete = await execute('incomplete-controller', 'review');
    expect(incomplete.result.status).not.toBe(0);
    expect(incomplete.result.stderr).toContain('canonical metadata controller policy is missing or malformed');

    await installCanonicalMetadataControllerContract(controller);
    spawnSync('git', ['add', '.juno_task'], { cwd: controller });
    spawnSync('git', ['-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'canonical controller'], { cwd: controller });

    const success = await execute('success', 'review');
    expect(success.result.status, success.result.stderr).toBe(0);
    const dryDir = path.join(testDir, 'managed-dry-run');
    const dryRun = runWorkflow([
      '--workflow', success.workflowPath, '--out-dir', dryDir, '--dry-run', '--print-output', 'none',
    ]);
    expect(dryRun.status, dryRun.stderr).toBe(0);
    expect((await fs.readJson(path.join(dryDir, 'run_contract.json'))).managed_agent_policy).toEqual({
      external_side_effects: 'forbidden', lifecycle_hooks: 'disabled',
    });
    const invalidWorkflow = await fs.readJson(success.workflowPath);
    delete invalidWorkflow.managed_agent_policy;
    const invalidWorkflowPath = path.join(testDir, 'managed-policy-missing.json');
    await fs.writeJson(invalidWorkflowPath, invalidWorkflow);
    const linted = runWorkflow(['lint', '--workflow', invalidWorkflowPath]);
    expect(linted.status).not.toBe(0);
    expect(linted.stderr).toContain('managed-agent workflows require exact managed_agent_policy');
    const receipt = await fs.readJson(path.join(success.runDir, 'managed', 'receipt.json'));
    expect(receipt.session_id).toBe('managed-session');
    expect(receipt.effective_hook_policy).toMatchObject({
      external_side_effects: 'forbidden', lifecycle_hooks: 'disabled', enforcement: 'yy_pi_no_hooks',
    });
    expect(await fs.readFile(path.join(success.runDir, 'managed', 'stdout.log'), 'utf8')).toContain('live stdout');
    expect(await fs.readFile(path.join(success.runDir, 'managed', 'stderr.log'), 'utf8')).toContain('live stderr');
    expect(await fs.readFile(path.join(success.runDir, 'managed', 'combined.log'), 'utf8')).toContain('[stdout] live stdout');
    const contractPath = path.join(success.runDir, 'run_contract.json');
    const contract = await fs.readJson(contractPath);
    expect(contract.completed_steps.agent.managed_agent.sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(contract.completed_steps.agent.stage_boundary.sha256).toMatch(/^[a-f0-9]{64}$/);
    expect((await fs.readJson(contract.completed_steps.agent.stage_boundary.path))).toMatchObject({
      schema_version: 'juno_workflow_stage_boundary.v1', passed: true,
      admitted_paths: ['tracked.txt'], changed_paths: [], unexpected_paths: [],
    });
    contract.attempts[contract.attempts.length - 1].status = 'interrupted';
    await fs.writeJson(contractPath, contract);
    const recovered = runWorkflow(['recover-attempt', success.runDir, '--dry-run']);
    expect(recovered.status, recovered.stderr).toBe(0);
    await fs.appendFile(path.join(success.runDir, 'managed', 'response.txt'), 'drift');
    const drifted = runWorkflow(['recover-attempt', success.runDir, '--dry-run']);
    expect(drifted.status).not.toBe(0); expect(drifted.stderr).toContain('managed-agent artifact response drifted');
    const mutation = await execute('stage-mutation', 'stage-mutation');
    expect(mutation.result.status).not.toBe(0);
    expect(mutation.result.stderr).toContain('managed-agent stage boundary violation');
    expect(await fs.readJson(path.join(mutation.runDir, 'steps', 'agent', 'stage_boundary.json'))).toMatchObject({
      passed: false, unexpected_paths: ['future-child.txt'],
    });
    await fs.remove(path.join(candidate, 'future-child.txt'));
    const blocked = await execute('typed-blocked', 'typed-blocked');
    expect(blocked.result.status).not.toBe(0);
    expect(blocked.result.stderr).toContain('managed-agent terminal outcome is blocked');
    const failure = await execute('failure', 'semantic-failure');
    expect(failure.result.status).not.toBe(0);
  }, 60_000);

  it('rechecks persisted task authority before every generated write dispatch', async () => {
    const controller = await fs.realpath(workflowFixtureController!);
    const ambientController = path.join(testDir, 'ambient-controller');
    await fs.ensureDir(path.join(ambientController, '.juno_task'));
    expect(spawnSync('git', ['init', '-b', 'ambient-controller'], {
      cwd: ambientController, encoding: 'utf8',
    }).status).toBe(0);
    spawnSync('git', ['config', '--local', 'juno.controller.path', ambientController], { cwd: ambientController });
    spawnSync('git', ['config', '--local', 'juno.controller.branch', 'ambient-controller'], { cwd: ambientController });
    const ambientResolution = spawnSync('python3', [
      path.resolve(process.cwd(), 'src/templates/scripts/controller_resolver.py'),
      '--cwd', ambientController, '--operation', 'orchestration',
    ], {
      cwd: ambientController,
      encoding: 'utf8',
      env: {
        ...process.env,
        JUNO_TASK_ROOT: ambientController,
        JUNO_CONTROLLER_BRANCH: 'ambient-controller',
        JUNO_WORKSPACE_ROLE: 'controller',
        JUNO_WORKSPACE_ENFORCEMENT: 'strict',
      },
    });
    expect(ambientResolution.status, ambientResolution.stderr).toBe(0);
    expect(JSON.parse(ambientResolution.stdout)).toMatchObject({
      path: await fs.realpath(ambientController), source: 'registration', valid: true,
    });

    await fs.writeFile(path.join(controller, 'tracked.txt'), 'base\n');
    spawnSync('git', ['add', 'tracked.txt'], { cwd: controller });
    spawnSync('git', ['-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'base'], { cwd: controller });
    spawnSync('git', ['config', '--local', 'extensions.worktreeConfig', 'true'], { cwd: controller });
    spawnSync('git', ['config', '--local', 'juno.controller.path', controller], { cwd: controller });
    spawnSync('git', ['config', '--local', 'juno.controller.branch', 'fixture-controller'], { cwd: controller });

    const taskRootConfigured = path.join(testDir, 'task-worktree');
    expect(spawnSync('git', ['worktree', 'add', '-b', 'task-branch', taskRootConfigured, 'HEAD'], {
      cwd: controller, encoding: 'utf8',
    }).status).toBe(0);
    const taskRoot = await fs.realpath(taskRootConfigured);
    const head = spawnSync('git', ['rev-parse', 'HEAD'], { cwd: taskRoot, encoding: 'utf8' }).stdout.trim();
    const common = await fs.realpath(spawnSync('git', ['rev-parse', '--path-format=absolute', '--git-common-dir'], {
      cwd: taskRoot, encoding: 'utf8',
    }).stdout.trim());
    const manifestPath = path.join(testDir, 'create-manifest.json');
    await fs.writeJson(manifestPath, { schema_version: 'juno_worktree_lifecycle.v5', operation: 'create', task_id: 'T1' });
    const manifestSha = createHash('sha256').update(await fs.readFile(manifestPath)).digest('hex');
    const expectedPathsSha = createHash('sha256').update(JSON.stringify(['tracked.txt'])).digest('hex');
    const manifestIdentity = createHash('sha256').update('task-manifest-T1').digest('hex');
    const roleValues: Record<string, string> = {
      role: 'task', taskId: 'T1', manifestIdentity, roleBase: head,
      createReceiptSha256: manifestSha, expectedPathsSha256: expectedPathsSha,
    };
    const persistRole = () => {
      for (const [key, value] of Object.entries(roleValues)) {
        expect(spawnSync('git', ['config', '--worktree', `juno.workspace.${key}`, value], { cwd: taskRoot }).status).toBe(0);
      }
    };
    persistRole();

    const receiptPath = path.join(testDir, 'edit-admission.json');
    const markerPath = path.join(testDir, 'dispatch.json');
    const producer = (receiptRoot: string) => {
      const payload = {
        schema_version: 'juno_edit_preflight.v1', passed: true, task_id: 'T1',
        current: { root: receiptRoot, git_common_dir: common, branch_ref: 'refs/heads/task-branch', head, clean: true },
        workspace: {
          valid: true, current_root: receiptRoot, role: 'task', role_source: 'worktree-registration',
          role_base: head, task_id: 'T1', manifest_identity: manifestIdentity,
          create_receipt_sha256: manifestSha, expected_paths_sha256: expectedPathsSha, role_authority: null,
        },
        target: { target_ref: 'refs/heads/main' }, expected_paths: ['tracked.txt'],
        manifest: { path: manifestPath, sha256: manifestSha },
      };
      return [
        'python3', '-c',
        `import json,os,pathlib; payload=json.loads(${JSON.stringify(JSON.stringify(payload))}); payload["producer_step_digest"]=os.environ["JUNO_WORKFLOW_STEP_DIGEST"]; pathlib.Path(${JSON.stringify(receiptPath)}).write_text(json.dumps(payload))`,
      ];
    };
    const workflow = (receiptRoot: string, mutation?: string[]) => ({
      schema_version: 1,
      workflow_id: 'generated_edit_dispatch',
      receipts: [{
        id: 'edit', producer: 'admit', path: receiptPath, schema_version: 'juno_edit_preflight.v1',
        required_fields: ['producer_step_digest', 'passed', 'task_id', 'current.root', 'current.git_common_dir', 'current.branch_ref', 'current.head', 'current.clean', 'workspace.role', 'workspace.current_root'],
        expected_fields: { passed: true, task_id: 'T1', 'current.root': taskRoot, 'current.clean': true, 'workspace.role': 'task', 'workspace.current_root': taskRoot },
      }],
      steps: [
        { id: 'admit', capture_session: false, fail_workflow: true, command: producer(receiptRoot) },
        ...(mutation ? [{ id: 'mutate-after-admission', capture_session: false, fail_workflow: true, command: mutation }] : []),
        {
          id: 'edit', capture_session: false, fail_workflow: true, edit_capable: true, requires_receipts: ['edit'],
          generated_task_contract: { role: 'implementation', write_contract: 'product_edit', task_root_receipt: 'edit' },
          command: ['python3', '-c', `import json,os,pathlib; pathlib.Path(${JSON.stringify(markerPath)}).write_text(json.dumps({"cwd":str(pathlib.Path.cwd()),"task_root":os.environ.get("TASK_ROOT"),"controller_root":os.environ.get("JUNO_TASK_ROOT")}))`],
        },
      ],
    });
    const workflowPath = path.join(testDir, 'generated-edit.json');
    const runCase = async (name: string, document: object, env?: NodeJS.ProcessEnv) => {
      await fs.remove(markerPath);
      await fs.writeJson(workflowPath, document);
      return runWorkflow(
        ['--workflow', workflowPath, '--out-dir', path.join(testDir, name), '--print-output', 'none'],
        undefined,
        env,
      );
    };

    for (const writeContract of ['review_fix', 'product_edit']) {
      const rejectedReview: any = workflow(taskRoot);
      rejectedReview.steps[1].generated_task_contract = {
        role: 'review', write_contract: writeContract, task_root_receipt: 'edit',
      };
      const rejected = await runCase(`review-${writeContract}`, rejectedReview, {
        // Model a test process launched through another repository's valid
        // registered controller route. Fixture identity must win so the
        // write-contract domain guard remains the observable refusal.
        JUNO_TASK_ROOT: ambientController,
        JUNO_CONTROLLER_BRANCH: 'refs/heads/ambient-controller',
        JUNO_WORKSPACE_ROLE: 'controller',
      });
      expect(rejected.status).not.toBe(0);
      expect(rejected.stderr + rejected.stdout).toContain(
        'generated review step edit must use write_contract read_only',
      );
      expect(await fs.pathExists(markerPath)).toBe(false);
    }

    const readOnlyWithAdmission: any = workflow(taskRoot);
    readOnlyWithAdmission.steps[1].generated_task_contract = {
      role: 'review', write_contract: 'read_only', task_root_receipt: 'edit',
    };
    const rejectedReadOnlyAdmission = await runCase('review-read-only-with-admission', readOnlyWithAdmission);
    expect(rejectedReadOnlyAdmission.status).not.toBe(0);
    expect(rejectedReadOnlyAdmission.stderr + rejectedReadOnlyAdmission.stdout).toContain(
      'generated read-only step edit cannot declare edit admission',
    );
    expect(await fs.pathExists(markerPath)).toBe(false);

    const accepted = await runCase('accepted', workflow(taskRoot));
    expect(accepted.status, accepted.stderr).toBe(0);
    expect(await fs.readJson(markerPath)).toEqual({ cwd: taskRoot, task_root: taskRoot, controller_root: controller });

    const removed = await runCase('role-removed', workflow(taskRoot, [
      'git', '-C', taskRoot, 'config', '--worktree', '--unset-all', 'juno.workspace.role',
    ]));
    expect(removed.status).not.toBe(0);
    expect(removed.stderr + removed.stdout).toContain('persisted task authority is invalid or unregistered');
    expect(await fs.pathExists(markerPath)).toBe(false);

    persistRole();
    const changed = await runCase('manifest-changed', workflow(taskRoot, [
      'git', '-C', taskRoot, 'config', '--worktree', 'juno.workspace.manifestIdentity', 'stale-manifest',
    ]));
    expect(changed.status).not.toBe(0);
    expect(changed.stderr + changed.stdout).toContain('persisted task role/manifest authority changed');
    expect(await fs.pathExists(markerPath)).toBe(false);

    persistRole();
    const alias = path.join(testDir, 'task-path-alias');
    await fs.symlink(taskRoot, alias);
    const pathDrift = await runCase('path-drift', workflow(alias));
    expect(pathDrift.status).not.toBe(0);
    expect(await fs.pathExists(markerPath)).toBe(false);

    const branchDrift = await runCase('branch-drift', workflow(taskRoot, [
      'git', '-C', taskRoot, 'checkout', '--detach',
    ]));
    expect(branchDrift.status).not.toBe(0);
    expect(branchDrift.stderr + branchDrift.stdout).toContain('exact task identity changed');
    expect(await fs.pathExists(markerPath)).toBe(false);
  });

  it('hard-rejects legacy local-integration lint after the task lifecycle cutover', async () => {
    const workflowPath = path.join(testDir, 'local-integration.json');
    const workflow: any = {
      schema_version: 2,
      workflow_id: 'local_integration_contract',
      workflow_class: 'local_integration',
      orchestration_workspace: 'controller',
      risk_tier: 'high',
      integration_step: 'integrate',
      terminal_gate: 'integrate',
      integration_policy: {
        queue: 'automatic_after_review_pass',
        channel_scope: 'git_common_dir_and_target_ref',
        target_movement: 'rebuild_and_rereview',
        checked_out_target: 'detach_same_sha',
      },
      validation_ownership: {
        pre_merge_review: 'pre_merge_review',
        candidate_review: 'candidate_review',
        actual_target_review: 'integrate',
      },
      receipts: [
        {
          id: 'integration',
          producer: 'integrate',
          path: 'integration.json',
          schema_version: 'juno_local_integration.v3',
          required_fields: ['producer_step_digest', 'outcome', 'feature_tag_policy'],
          expected_fields: { outcome: 'integrated' },
        },
      ],
      steps: [
        { id: 'pre_merge_review', command: ['yy', 'pi', 'Review the exact task tip.'] },
        { id: 'candidate_review', command: 'true' },
        {
          id: 'integrate',
          command: [
            'python3',
            '.juno_task/scripts/integration_owner_preflight.py',
            'integrate',
            '--candidate-receipt',
            'candidate.json',
            '--risk-tier',
            'high',
            '--checked-out-target',
            'detach_same_sha',
            '--actual-review-command',
            'yy pi review',
            '--actual-review-receipt',
            'actual.json',
          ],
        },
      ],
    };
    await fs.writeJson(workflowPath, workflow);

    const accepted = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(accepted.status).not.toBe(0);
    expect(accepted.stderr + accepted.stdout).toContain(
      'legacy local_integration execution is read-only',
    );
    expect(accepted.stderr + accepted.stdout).toContain('yy task start TASK_ID');
    expect(accepted.stderr + accepted.stdout).toContain('yy merge land TASK_ID');
    expect(accepted.stderr + accepted.stdout).not.toContain('yy merge next');
    return;

    for (const reviewStepId of ['pre_merge_review', 'candidate_review']) {
      const editReview = JSON.parse(JSON.stringify(workflow));
      editReview.receipts.unshift({
        id: 'edit', producer: 'admit', path: 'edit.json', schema_version: 'juno_edit_preflight.v1',
        required_fields: ['producer_step_digest', 'passed'], expected_fields: { passed: true },
      });
      editReview.steps.unshift({ id: 'admit', command: 'true' });
      const reviewStep = editReview.steps.find((step: any) => step.id === reviewStepId);
      reviewStep.edit_capable = true;
      reviewStep.requires_receipts = ['edit'];
      reviewStep.generated_task_contract = {
        role: 'implementation', write_contract: 'product_edit', task_root_receipt: 'edit',
      };
      await fs.writeJson(workflowPath, editReview);
      const rejectedEditReview = runWorkflow(['lint', '--workflow', workflowPath]);
      expect(rejectedEditReview.status).not.toBe(0);
      expect(rejectedEditReview.stderr + rejectedEditReview.stdout).toContain(
        `independent review step ${reviewStepId} cannot declare edit authority`,
      );
    }
    await fs.writeJson(workflowPath, workflow);

    const controllerConfig = path.join(testDir, '.juno_task', 'config.json');
    await fs.ensureDir(path.dirname(controllerConfig));
    await fs.writeJson(controllerConfig, { workflowModels: [':luna'] });
    workflow.orchestration_workspace = testDir;
    workflow.steps[0].command = ['yy', 'pi', '--model', ':luna', 'Review the exact task tip.'];
    workflow.steps[1].command = ['yy', 'pi', '--model', ':luna', 'Review the candidate.'];
    workflow.steps[1].candidate_read_only = { path: '{{ candidate_path }}', sha: '{{ candidate_sha }}' };
    const actualReviewIndexForAllowed = workflow.steps[2].command.indexOf('--actual-review-command') + 1;
    workflow.steps[2].command[actualReviewIndexForAllowed] = 'yy pi --model :luna review';
    await fs.writeJson(workflowPath, workflow);
    const allowedReviews = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(allowedReviews.status, allowedReviews.stderr).toBe(0);
    workflow.steps[0].command = ['yy', 'pi', 'Review the exact task tip.'];
    workflow.steps[1].command = 'true';
    delete workflow.steps[1].candidate_read_only;
    workflow.steps[2].command[actualReviewIndexForAllowed] = 'yy pi review';
    workflow.orchestration_workspace = 'controller';
    await fs.remove(controllerConfig);

    delete workflow.orchestration_workspace;
    await fs.writeJson(workflowPath, workflow);
    const checkoutLocal = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(checkoutLocal.status).not.toBe(0);
    expect(checkoutLocal.stderr).toMatch(/orchestration_workspace must be controller or an explicit absolute external path/);
    workflow.orchestration_workspace = 'controller';

    workflow.steps[1].command = ['yy', 'pi', 'Review composed candidate.'];
    await fs.writeJson(workflowPath, workflow);
    const undeclaredCandidate = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(undeclaredCandidate.status).not.toBe(0);
    expect(undeclaredCandidate.stderr).toMatch(/candidate_review must declare candidate_read_only with exactly path and sha/);
    workflow.steps[1].candidate_read_only = { path: '{{ candidate_path }}', sha: '{{ candidate_sha }}' };
    await fs.writeJson(workflowPath, workflow);
    expect(runWorkflow(['lint', '--workflow', workflowPath]).status).toBe(0);
    delete workflow.steps[1].candidate_read_only;
    workflow.steps[1].command = 'true';

    workflow.steps[0].command = ['pi', '-p', 'Review outside Juno.'];
    await fs.writeJson(workflowPath, workflow);
    const barePi = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(barePi.status).not.toBe(0);
    expect(barePi.stderr).toMatch(/must launch through yy pi, not bare pi/);

    workflow.steps[0].command = ['yy', 'pi', '--provider', 'openai-codex', 'Review with override.'];
    await fs.writeJson(workflowPath, workflow);
    const unapprovedOverride = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(unapprovedOverride.status).not.toBe(0);
    expect(unapprovedOverride.stderr).toMatch(/explicit --provider requires explicit --model/);

    workflow.steps[0].provider_model_override_authorization = 'self-asserted authorization must not bypass policy';
    await fs.writeJson(workflowPath, workflow);
    const claimedOverride = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(claimedOverride.status).not.toBe(0);
    delete workflow.steps[0].provider_model_override_authorization;
    workflow.steps[0].command = ['yy', 'pi', 'Review the exact task tip.'];

    workflow.steps[1].command = ['codex', 'Review through a direct provider CLI.'];
    await fs.writeJson(workflowPath, workflow);
    const directProviderReview = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(directProviderReview.status).not.toBe(0);
    expect(directProviderReview.stderr).toMatch(/must launch through yy pi, not direct agent CLI codex/);

    workflow.steps[1].command = ['env', 'REVIEW_MODE=1', 'codex', 'Wrapped provider review.'];
    await fs.writeJson(workflowPath, workflow);
    const wrappedProviderReview = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(wrappedProviderReview.status).not.toBe(0);
    expect(wrappedProviderReview.stderr).toMatch(/must launch through yy pi, not direct agent CLI codex/);

    workflow.steps[1].command = ['env', '-P', '/usr/local/bin:/usr/bin', 'codex', 'Path-wrapped provider review.'];
    await fs.writeJson(workflowPath, workflow);
    const pathWrappedProviderReview = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(pathWrappedProviderReview.status).not.toBe(0);
    expect(pathWrappedProviderReview.stderr).toMatch(/must launch through yy pi, not direct agent CLI codex/);

    workflow.steps[1].command = 'true';
    workflow.steps[0].command = ['env', '-P', '/usr/local/bin:/usr/bin', 'yy', 'pi', '--model', ':sol', 'Path-wrapped override.'];
    await fs.writeJson(workflowPath, workflow);
    const pathWrappedModelOverride = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(pathWrappedModelOverride.status).not.toBe(0);
    expect(pathWrappedModelOverride.stderr).toMatch(/is not exactly allowlisted by workflowModels/);

    workflow.steps[0].command = ['env', '--unknown-env-option', 'yy', 'pi', 'Ambiguous wrapper.'];
    await fs.writeJson(workflowPath, workflow);
    const unknownEnvWrapper = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(unknownEnvWrapper.status).not.toBe(0);
    expect(unknownEnvWrapper.stderr).toMatch(/unsupported env wrapper/);

    workflow.steps[0].command = ['env', 'yy', 'pi', '--provider', 'openai-codex', 'Wrapped override.'];
    await fs.writeJson(workflowPath, workflow);
    const wrappedProviderOverride = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(wrappedProviderOverride.status).not.toBe(0);
    expect(wrappedProviderOverride.stderr).toMatch(/explicit --provider requires explicit --model/);

    workflow.steps[0].command = ['env', '-i', 'yy', 'pi', 'Review with defaults.'];
    await fs.writeJson(workflowPath, workflow);
    const wrappedDefault = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(wrappedDefault.status).toBe(0);

    workflow.steps[0].command = ['yy', 'codex', 'Review through the wrong Juno subagent.'];
    await fs.writeJson(workflowPath, workflow);
    const nonPiReview = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(nonPiReview.status).not.toBe(0);
    expect(nonPiReview.stderr).toMatch(/dedicated yy pi agent step/);

    workflow.steps[0].command = ['yy', 'pi', 'Review the exact task tip.'];
    const actualReviewIndex = workflow.steps[2].command.indexOf('--actual-review-command') + 1;
    workflow.steps[2].command[actualReviewIndex] = 'pi -p "Review outside Juno"';
    await fs.writeJson(workflowPath, workflow);
    const bareActualReview = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(bareActualReview.status).not.toBe(0);
    expect(bareActualReview.stderr).toMatch(/actual_target_review must launch through yy pi/);

    workflow.steps[2].command[actualReviewIndex] = 'yy pi --model :sol "Review with override"';
    await fs.writeJson(workflowPath, workflow);
    const unapprovedActualOverride = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(unapprovedActualOverride.status).not.toBe(0);
    expect(unapprovedActualOverride.stderr).toMatch(/is not exactly allowlisted by workflowModels/);

    workflow.steps[2].provider_model_override_authorization = 'self-asserted authorization must not bypass policy';
    await fs.writeJson(workflowPath, workflow);
    const claimedActualOverride = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(claimedActualOverride.status).not.toBe(0);
    delete workflow.steps[2].provider_model_override_authorization;
    workflow.steps[2].command[actualReviewIndex] = 'yy pi --continue review old-session';
    await fs.writeJson(workflowPath, workflow);
    const continuedActualReview = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(continuedActualReview.status).not.toBe(0);
    expect(continuedActualReview.stderr).toMatch(/actual_target_review must use a fresh session/);
    workflow.steps[2].command[actualReviewIndex] = 'yy pi -rold-session review';
    await fs.writeJson(workflowPath, workflow);
    const compactResumedActualReview = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(compactResumedActualReview.status).not.toBe(0);
    expect(compactResumedActualReview.stderr).toMatch(/actual_target_review must use a fresh session/);
    workflow.steps[2].command[actualReviewIndex] = 'yy pi review';

    workflow.steps[0].command = ['yy', 'pi', '--resume', 'old-session', 'Review again.'];
    await fs.writeJson(workflowPath, workflow);
    const resumedReview = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(resumedReview.status).not.toBe(0);
    expect(resumedReview.stderr).toMatch(/fresh session without resume/);
    workflow.steps[0].command = ['yy', 'pi', 'Review the exact task tip.'];
    workflow.steps[1].command = ['yy', 'pi', '--resume=old-session', 'Review again.'];
    workflow.steps[1].candidate_read_only = { path: '{{ candidate_path }}', sha: '{{ candidate_sha }}' };
    await fs.writeJson(workflowPath, workflow);
    const equalResumedCandidateReview = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(equalResumedCandidateReview.status).not.toBe(0);
    expect(equalResumedCandidateReview.stderr).toMatch(/fresh session without resume/);

    workflow.steps[0].command = ['yy', 'pi', '--', '--resume is prompt text'];
    workflow.steps[1].command = ['yy', 'pi', '--', '--continue is prompt text'];
    workflow.steps[2].command[actualReviewIndex] = 'yy pi -- "cc and -rold are prompt text"';
    await fs.writeJson(workflowPath, workflow);
    const delimitedPromptReviews = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(delimitedPromptReviews.status, delimitedPromptReviews.stderr).toBe(0);
    workflow.steps[0].command = ['yy', 'pi', 'Review the exact task tip.'];
    workflow.steps[1].command = 'true';
    delete workflow.steps[1].candidate_read_only;
    workflow.steps[2].command[actualReviewIndex] = 'yy pi review';

    workflow.steps[2].command =
      'python3 .juno_task/scripts/integration_owner_preflight.py --checkpoint-controller --exec-command integrate.sh';
    await fs.writeJson(workflowPath, workflow);
    const retired = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(retired.status).not.toBe(0);
    expect(retired.stderr).toMatch(/directly executed argv-list/);

    workflow.steps[2].command =
      'python3 .juno_task/scripts/integration_owner_preflight.py integrate --candidate-receipt candidate.json --risk-tier high --checked-out-target detach_same_sha --actual-review-command "yy pi review" --actual-review-receipt actual.json';
    await fs.writeJson(workflowPath, workflow);
    const shellOwner = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(shellOwner.status).not.toBe(0);
    expect(shellOwner.stderr).toMatch(/directly executed argv-list/);

    workflow.steps[2].command = [
      'python3', '.juno_task/scripts/integration_owner_preflight.py', 'integrate',
      '--candidate-receipt', 'candidate.json', '--risk-tier', 'high',
      '--checked-out-target', 'detach_same_sha', '--actual-review-command', 'yy pi review',
      '--actual-review-receipt', 'actual.json',
    ];
    workflow.schema_version = 1;
    await fs.writeJson(workflowPath, workflow);
    const legacySchema = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(legacySchema.status).not.toBe(0);
    expect(legacySchema.stderr).toMatch(/schema_version: 2; migration required/);

    workflow.schema_version = 2;
    workflow.receipts[0].required_fields = ['step_digest', 'outcome', 'feature_tag_policy'];
    await fs.writeJson(workflowPath, workflow);
    const undeclaredProducerDigest = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(undeclaredProducerDigest.status).not.toBe(0);
    expect(undeclaredProducerDigest.stderr).toMatch(/must include producer_step_digest/);
  }, contentionBudgetMs(300_000));

  it('rejects old local-integration start, resume, recovery, and amendment while doctor remains readable', async () => {
    const workflowPath = path.join(testDir, 'retired-local-integration.json');
    await fs.writeJson(workflowPath, {
      schema_version: 2,
      workflow_id: 'retired_local_integration',
      workflow_class: 'local_integration',
      steps: [{ id: 'old', command: 'true' }],
    });
    for (const extra of [[], ['--from-step', 'old'], ['--amends-run', path.join(testDir, 'prior-run')]]) {
      const result = runWorkflow(['--workflow', workflowPath, '--out-dir', path.join(testDir, `retired-${extra.length}`), ...extra]);
      expect(result.status).toBe(2);
      expect(result.stderr).toContain('legacy local_integration execution is read-only');
    }
    const historical = path.join(testDir, 'historical-local-run');
    await fs.ensureDir(historical);
    await fs.writeJson(path.join(historical, 'run_contract.json'), {
      schema_version: 'juno_workflow_run_contract.v1', workflow_class: 'local_integration',
    });
    const recovery = runWorkflow(['recover-attempt', historical, '--dry-run']);
    expect(recovery.status).toBe(2);
    expect(recovery.stderr).toContain('legacy local_integration execution is read-only');
    await fs.writeJson(path.join(historical, 'manifest.json'), {
      schema_version: '1.0', workflow_class: 'local_integration', steps: [], status: 'failed',
    });
    const doctor = runWorkflow(['doctor', historical]);
    expect(doctor.status, doctor.stderr).toBe(0);
  });

  it('enforces exact workflowModels selectors and closes indirect yy pi override channels', async () => {
    const workflowPath = path.join(testDir, 'workflow-models.json');
    const configPath = path.join(testDir, '.juno_task', 'config.json');
    await fs.ensureDir(path.dirname(configPath));
    await fs.writeJson(configPath, {
      defaultModels: { pi: ':gpt' },
      workflowModels: [':luna', 'openai/gpt-4o', 'openai/gpt-4.1'],
    });
    const lint = async (command: string[] | string, summary?: string[] | string) => {
      await fs.writeJson(workflowPath, {
        schema_version: 1,
        workflow_id: 'workflow-models',
        steps: [{ id: 'review', command }],
        ...(summary ? { summary: { command: summary } } : {}),
      });
      return runWorkflow(['lint', '--workflow', workflowPath, '--project-root', testDir]);
    };

    expect((await lint(['yy', 'pi', 'inherit defaults'])).status).toBe(0);
    expect((await lint(['yy', 'pi', '-m', ':luna', 'shorthand'])).status).toBe(0);
    expect((await lint(['yy', 'pi', '-m', 'openai/gpt-4o', 'qualified'])).status).toBe(0);
    expect((await lint(['yy', 'pi', '--provider', 'openai', '--model', 'gpt-4.1', 'split'])).status).toBe(0);
    expect((await lint(['yy', '--quiet', '-l', 'workflow.log', 'pi', 'ordinary flags'])).status).toBe(0);
    expect((await lint('yy pi "quoted prompt; punctuation is data"')).status).toBe(0);
    expect((await lint('TRACE_ID=review yy pi "assignment-prefixed canonical launch"')).status).toBe(0);
    for (const punctuation of [';', '|', '&', '<', '>', '(', ')']) {
      expect((await lint(`yy pi '${punctuation}'`)).status).toBe(0);
    }
    expect((await lint("yy pi 'quoted ; | & < > ( ) ` $( and newline\npunctuation is data'")).status).toBe(0);
    expect((await lint(['yy', 'pi', 'argv ; | & < > ( ) ` $( and newline\npunctuation is data'])).status).toBe(0);
    expect((await lint("printf '%s\\n' 'Use yy pi for independent reviews' > prompt.md")).status).toBe(0);
    expect((await lint("sh -c 'printf %s \"Use yy pi for independent reviews\" > prompt.md'")).status).toBe(0);
    expect((await lint(['echo', 'step'], ['yy', 'pi', '--model=:luna', 'summary'])).status).toBe(0);

    for (const compound of [
      'yy pi allowed-prompt; PI_MODEL=:sol yy pi bypass',
      'yy pi allowed-prompt && yy pi bypass',
      'yy pi allowed-prompt\nyy pi bypass',
      'printf input | PI_PROVIDER=openai PI_MODEL=gpt-4o yy pi bypass',
      'echo $(yy pi bypass)',
      'echo "$(yy pi bypass)"',
      'echo `pi`',
      "bash -lc 'yy pi --model :evil hidden'",
      "sh -c 'PI_MODEL=:evil yy pi hidden'",
      "eval 'yy pi --model :evil hidden'",
      'command yy pi --model :evil hidden',
      "bash -lc 'codex hidden'",
      "dash -c 'pi hidden'",
      "zsh -c 'yylo pi hidden'",
      "X=1 bash -lc 'ypl hidden'",
      "exec yy pi --model :evil hidden",
      "exec -a harmless yy pi --model :evil hidden",
    ]) {
      const result = await lint(compound);
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('compound shell syntax');
    }

    for (const wrappedArgv of [
      ['bash', '-lc', 'yy pi --model :evil hidden'],
      ['sh', '-c', 'codex hidden'],
      ['eval', 'yy', 'pi', '--model', ':evil', 'hidden'],
      ['command', 'yy', 'pi', '--model', ':evil', 'hidden'],
      ['exec', '-a', 'harmless', 'pi', 'hidden'],
      ['command', 'bash', '-lc', 'eval "yy pi --model :evil hidden"'],
      ['command', 'exec', '-a', 'harmless', 'yy', 'pi', '--provider', 'evil', '--model', 'hidden'],
      [...Array(10).fill('command'), 'yy', 'pi', '--model', ':evil', 'hidden'],
    ]) {
      const result = await lint(wrappedArgv);
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain('compound shell syntax');
    }

    const malformedShell = await lint('echo "unterminated argument');
    expect(malformedShell.status).not.toBe(0);
    expect(malformedShell.stderr).toContain('compound shell syntax');

    const compoundSummary = await lint(['echo', 'step'], 'yy pi review | yy pi bypass');
    expect(compoundSummary.status).not.toBe(0);
    expect(compoundSummary.stderr).toContain('compound shell syntax');

    for (const [command, message] of [
      [['yy', 'pi', '-m', ':sol', 'unlisted'], 'not exactly allowlisted'],
      [['yy', 'pi', '--provider', 'openai', 'provider only'], 'requires explicit --model'],
      [['yy', 'pi', '--provider', 'openai', '-m', ':luna', 'ambiguous'], 'ambiguous'],
      [['yy', 'pi', '-m', ':luna', '--model', ':luna', 'duplicate'], 'duplicate'],
      [['yy', 'pi', '--model'], 'missing value'],
      [['PI_MODEL=:luna', 'yy', 'pi', 'hidden'], 'environment assignment PI_MODEL'],
      [['env', 'PI_PROVIDER=openai', 'yy', 'pi', 'hidden'], 'environment assignment PI_PROVIDER'],
      [['env', '-S', 'PI_MODEL=:luna yy pi hidden'], 'environment assignment PI_MODEL'],
      [['env', 'env', 'PI_PROVIDER=openai', 'yy', 'pi', 'hidden'], 'environment assignment PI_PROVIDER'],
      [['X=1', 'env', 'env', 'env', 'env', 'yy', 'pi', '--model', ':unapproved', 'hidden'], 'not exactly allowlisted'],
      [['X=1', 'env', 'env', 'env', 'env', 'env', 'PI_MODEL=:luna', 'yy', 'pi', 'hidden'], 'environment assignment PI_MODEL'],
      [['X=1', 'env', '-v', 'yy', 'pi', '--model', ':unapproved', 'hidden'], 'unsupported env wrapper'],
      [['X=1', 'env', '-Syy pi --model :unapproved hidden'], 'not exactly allowlisted'],
      [['X=1', 'env', '--split-string=PI_MODEL=:luna yy pi hidden'], 'environment assignment PI_MODEL'],
      [['YYLO_CONFIG=other.json', 'yy', 'pi', 'hidden'], 'alternate config through environment assignment'],
      [['PI_ADDITIONAL_ARGS=--model :luna', 'yy', 'pi', 'hidden'], 'inject additional args'],
      [['yy', 'pi', '--additional-args=--model :luna', 'hidden'], '--additional-args'],
      [['yy', '--config', 'other.json', 'pi', 'hidden'], 'alternate config'],
      [['yy', 'pi', '-cother.json', 'hidden'], 'alternate config'],
      [['yy', '-cother.json', 'pi', 'hidden'], 'alternate config'],
      [['yy', '--config=other.json', 'pi', 'hidden'], 'alternate config'],
      [['yy', 'pi', '-m:unapproved', 'hidden'], 'malformed model selector flag'],
      [['yy', '-m:unapproved', 'pi', 'hidden'], 'malformed model selector flag'],
      [['yy', '--model=:unapproved', 'pi', 'hidden'], 'not exactly allowlisted'],
      [['yy', '--no-color', '--model', ':unapproved', 'pi', 'hidden'], 'not exactly allowlisted'],
      [['yy', '--log-level', 'debug', '--model', ':unapproved', 'pi', 'hidden'], 'not exactly allowlisted'],
      [['yy', '-lworkflow.log', '--model', ':unapproved', 'pi', 'hidden'], 'not exactly allowlisted'],
      [['yy', '--force-update', '--model', ':unapproved', 'pi', 'hidden'], 'not exactly allowlisted'],
    ] as Array<[string[], string]>) {
      const result = await lint(command);
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain(message);
    }

    await fs.writeJson(configPath, { defaultModels: { pi: ':gpt' }, workflowModels: [] });
    expect((await lint(['yy', 'pi', 'inherit defaults'])).status).toBe(0);
    expect((await lint(['yy', 'pi', '-m', ':luna', 'explicit'])).status).not.toBe(0);
  });

  it('guards a real linked candidate from an external review cwd and preserves exact mutation evidence', async () => {
    const repository = path.join(testDir, 'review-repository');
    const candidateConfigured = path.join(testDir, 'immutable-candidate');
    await fs.ensureDir(repository);
    spawnSync('git', ['init', '-q'], { cwd: repository });
    await fs.writeFile(path.join(repository, 'product.txt'), 'original\n');
    spawnSync('git', ['add', '.'], { cwd: repository });
    spawnSync('git', ['-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'base'], { cwd: repository });
    expect(spawnSync('git', ['worktree', 'add', '--detach', candidateConfigured, 'HEAD'], { cwd: repository }).status).toBe(0);
    const candidate = await fs.realpath(candidateConfigured);
    const candidateSha = spawnSync('git', ['-C', candidate, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).stdout.trim();
    const indexPath = spawnSync('git', ['-C', candidate, 'rev-parse', '--path-format=absolute', '--git-path', 'index'], { encoding: 'utf8' }).stdout.trim();
    const indexBefore = await fs.readFile(indexPath);
    const reviewerBin = path.join(testDir, 'review-bin');
    await fs.ensureDir(reviewerBin);
    await fs.writeFile(path.join(reviewerBin, 'yy'), `#!/usr/bin/env python3
import json, os, pathlib, shutil, subprocess, sys
if len(sys.argv) > 1 and sys.argv[1] == 'continue-scope':
    print(json.dumps({'fullHash':'SCOPE_0123456789ABCDEF','sessionEnvKey':'YYLO_LAST_SESSION_ID_SCOPE_0123456789ABCDEF','settingsEnvKey':'YYLO_LAST_EXECUTION_SETTINGS_SCOPE_0123456789ABCDEF','sessionId':None}))
    raise SystemExit(0)
candidate, expected, mode = pathlib.Path(sys.argv[2]), sys.argv[3], sys.argv[4]
pathlib.Path(os.environ['REVIEW_INVOCATION']).write_text(json.dumps({'cwd': os.getcwd(), 'candidate': str(candidate), 'sha': expected}))
if mode in {'mutate', 'index', 'commit'}:
    (candidate / 'product.txt').write_text('reviewer mutation\\n')
if mode == 'index': subprocess.run(['git', '-C', str(candidate), 'add', 'product.txt'], check=True)
if mode == 'commit':
    subprocess.run(['git', '-C', str(candidate), 'add', 'product.txt'], check=True)
    subprocess.run(['git', '-C', str(candidate), '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'reviewer mutation'], check=True)
if mode == 'delete': shutil.rmtree(candidate)
if mode == 'replace':
    backup = candidate.with_name(candidate.name + '.original')
    candidate.rename(backup)
    shutil.copytree(backup, candidate, symlinks=True)
capture = os.environ.get('JUNO_SUBAGENT_CAPTURE_PATH')
if capture: pathlib.Path(capture).write_text(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'PASS','session_id':'review-session'}) + '\\n')
print('PASS')
`);
    await fs.chmod(path.join(reviewerBin, 'yy'), 0o755);

    const makeWorkflow = (mode: string) => ({
      schema_version: 1,
      workflow_id: `candidate_${mode}`,
      fail_fast: true,
      vars: { candidate_path: candidate, candidate_sha: candidateSha, review_mode: mode },
      steps: [{
        id: 'candidate_review',
        command: ['yy', 'pi', '{{ candidate_path }}', '{{ candidate_sha }}', '{{ review_mode }}'],
        candidate_read_only: { path: '{{ candidate_path }}', sha: '{{ candidate_sha }}' },
      }],
    });
    const workflowPath = path.join(testDir, 'candidate-review.json');
    const invocationPath = path.join(testDir, 'review-invocation.json');
    const cleanOut = path.join(testDir, 'candidate-clean-out');
    await fs.writeJson(workflowPath, makeWorkflow('observe'));
    const clean = runWorkflow(['--workflow', workflowPath, '--out-dir', cleanOut], undefined, {
      PATH: `${reviewerBin}:${process.env.PATH}`,
      REVIEW_INVOCATION: invocationPath,
    });
    expect(clean.status, clean.stderr).toBe(0);
    expect(await fs.readJson(invocationPath)).toEqual({ cwd: await fs.realpath(workflowFixtureController!), candidate, sha: candidateSha });
    const cleanEvidence = await fs.readJson(path.join(cleanOut, 'steps', 'candidate_review', 'candidate_read_only.json'));
    expect(cleanEvidence).toMatchObject({
      schema_version: 'juno_candidate_read_only.v2', passed: true, changed_fields: [], expected_sha: candidateSha,
    });
    expect(JSON.stringify(cleanEvidence)).not.toContain('base64');
    expect(Object.keys(cleanEvidence.before.raw_index).sort()).toEqual(['bytes', 'sha256']);
    expect(cleanEvidence.before.raw_index).toEqual(cleanEvidence.after.raw_index);
    expect(cleanEvidence.before.logical_index).toEqual(cleanEvidence.after.logical_index);
    expect(cleanEvidence.before.status_porcelain_v2_z).toEqual(cleanEvidence.after.status_porcelain_v2_z);
    const cleanContract = await fs.readJson(path.join(cleanOut, 'run_contract.json'));
    const guardAnchor = cleanContract.completed_steps.candidate_review.candidate_read_only;
    expect(guardAnchor.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(guardAnchor.evidence_sha256).toMatch(/^[0-9a-f]{64}$/);
    const cleanAttempt = cleanContract.attempts.at(-1);
    const archivedManifest = await fs.readJson(cleanAttempt.manifest);
    const archivedGuard = archivedManifest.steps[0].candidate_read_only_evidence;
    expect(archivedGuard.path).toContain(`${path.sep}attempts${path.sep}`);
    expect(createHash('sha256').update(await fs.readFile(archivedGuard.path)).digest('hex')).toBe(archivedGuard.sha256);
    expect(await fs.readFile(indexPath)).toEqual(indexBefore);
    expect(spawnSync('git', ['-C', candidate, 'status', '--porcelain=v2'], { encoding: 'utf8' }).stdout).toBe('');

    const mutationOut = path.join(testDir, 'candidate-mutation-out');
    await fs.writeJson(workflowPath, makeWorkflow('mutate'));
    const mutated = runWorkflow(['--workflow', workflowPath, '--out-dir', mutationOut], undefined, {
      PATH: `${reviewerBin}:${process.env.PATH}`,
      REVIEW_INVOCATION: invocationPath,
    });
    expect(mutated.status).not.toBe(0);
    expect(mutated.stderr).toMatch(/candidate_read_only mutation detected; no cleanup performed/);
    expect(await fs.readFile(path.join(candidate, 'product.txt'), 'utf8')).toBe('reviewer mutation\n');
    const mutationEvidence = await fs.readJson(path.join(mutationOut, 'steps', 'candidate_review', 'candidate_read_only.json'));
    expect(mutationEvidence.passed).toBe(false);
    expect(mutationEvidence.changed_fields).toContain('status_porcelain_v2_z');
    expect(mutationEvidence.before.head).toBe(candidateSha);
    expect(mutationEvidence.after.head).toBe(candidateSha);

    const restoreCandidate = async (mode: string) => {
      if (mode === 'delete') {
        spawnSync('git', ['worktree', 'repair', candidate], { cwd: repository });
        spawnSync('git', ['worktree', 'remove', '--force', candidate], { cwd: repository });
        spawnSync('git', ['worktree', 'add', '--detach', candidate, candidateSha], { cwd: repository });
      } else if (mode === 'replace') {
        await fs.remove(candidate);
        await fs.move(`${candidate}.original`, candidate);
      }
      spawnSync('git', ['-C', candidate, 'reset', '--hard', candidateSha]);
      spawnSync('git', ['-C', candidate, 'clean', '-fd']);
    };
    await restoreCandidate('mutate');
    for (const mode of ['index', 'commit', 'delete', 'replace']) {
      const out = path.join(testDir, `candidate-${mode}-out`);
      await fs.writeJson(workflowPath, makeWorkflow(mode));
      const result = runWorkflow(['--workflow', workflowPath, '--out-dir', out], undefined, {
        PATH: `${reviewerBin}:${process.env.PATH}`, REVIEW_INVOCATION: invocationPath,
      });
      expect(result.status, `${mode}: ${result.stderr}`).not.toBe(0);
      expect(result.stderr).toMatch(/candidate_read_only mutation detected; no cleanup performed/);
      const evidence = await fs.readJson(path.join(out, 'steps', 'candidate_review', 'candidate_read_only.json'));
      expect(evidence.passed).toBe(false);
      expect(evidence.changed_fields.length).toBeGreaterThan(0);
      expect(JSON.stringify(evidence)).not.toContain('base64');
      await restoreCandidate(mode);
    }

    const nestedDirectory = path.join(candidate, 'nested');
    await fs.ensureDir(nestedDirectory);
    const nestedWorkflow = makeWorkflow('observe');
    nestedWorkflow.vars.candidate_path = nestedDirectory;
    await fs.writeJson(workflowPath, nestedWorkflow);
    const nested = runWorkflow(['--workflow', workflowPath, '--out-dir', path.join(testDir, 'nested-out')], undefined, {
      PATH: `${reviewerBin}:${process.env.PATH}`, REVIEW_INVOCATION: invocationPath,
    });
    expect(nested.status).not.toBe(0);
    expect(nested.stderr).toMatch(/exact Git worktree top-level/);
    await fs.remove(nestedDirectory);

    const alias = path.join(testDir, 'candidate-alias');
    await fs.symlink(candidate, alias);
    const aliasWorkflow = makeWorkflow('observe');
    aliasWorkflow.vars.candidate_path = alias;
    await fs.writeJson(workflowPath, aliasWorkflow);
    const aliased = runWorkflow(['--workflow', workflowPath, '--out-dir', path.join(testDir, 'alias-out')], undefined, {
      PATH: `${reviewerBin}:${process.env.PATH}`, REVIEW_INVOCATION: invocationPath,
    });
    expect(aliased.status).not.toBe(0);
    expect(aliased.stderr).toMatch(/exact canonical path/);

    const shaWorkflow = makeWorkflow('observe');
    shaWorkflow.vars.candidate_sha = 'f'.repeat(40);
    await fs.writeJson(workflowPath, shaWorkflow);
    const substitutedSha = runWorkflow(['--workflow', workflowPath, '--out-dir', path.join(testDir, 'sha-out')], undefined, {
      PATH: `${reviewerBin}:${process.env.PATH}`, REVIEW_INVOCATION: invocationPath,
    });
    expect(substitutedSha.status).not.toBe(0);
    expect(substitutedSha.stderr).toMatch(/SHA mismatch/);

    const reusableWorkflow: any = makeWorkflow('observe');
    reusableWorkflow.steps.push({ id: 'suffix', capture_session: false, command: 'true' });
    await fs.writeJson(workflowPath, reusableWorkflow);
    const reuseOut = path.join(testDir, 'candidate-reuse-out');
    expect(runWorkflow(['--workflow', workflowPath, '--out-dir', reuseOut], undefined, {
      PATH: `${reviewerBin}:${process.env.PATH}`, REVIEW_INVOCATION: invocationPath,
    }).status).toBe(0);
    const reuseGuard = path.join(reuseOut, 'steps', 'candidate_review', 'candidate_read_only.json');
    await fs.writeFile(reuseGuard, `${await fs.readFile(reuseGuard, 'utf8')} `);
    const resumed = runWorkflow(['--workflow', workflowPath, '--out-dir', reuseOut, '--from-step', 'suffix'], undefined, {
      PATH: `${reviewerBin}:${process.env.PATH}`, REVIEW_INVOCATION: invocationPath,
    });
    expect(resumed.status).not.toBe(0);
    expect(resumed.stderr).toMatch(/candidate_read_only checkpoint\[candidate_review\] artifact hash mismatch/);

    const amendmentParent = path.join(testDir, 'candidate-amendment-parent');
    await fs.writeJson(workflowPath, reusableWorkflow);
    expect(runWorkflow(['--workflow', workflowPath, '--out-dir', amendmentParent], undefined, {
      PATH: `${reviewerBin}:${process.env.PATH}`, REVIEW_INVOCATION: invocationPath,
    }).status).toBe(0);
    const amendmentGuard = path.join(amendmentParent, 'steps', 'candidate_review', 'candidate_read_only.json');
    const amendmentEvidence = await fs.readJson(amendmentGuard);
    amendmentEvidence.after.head = '0'.repeat(40);
    await fs.writeJson(amendmentGuard, amendmentEvidence);
    reusableWorkflow.amendment_mode = 'harness_only_validation';
    await fs.writeJson(workflowPath, reusableWorkflow);
    const amended = runWorkflow([
      '--workflow', workflowPath, '--out-dir', path.join(testDir, 'candidate-amendment'),
      '--amends-run', amendmentParent, '--from-step', 'suffix',
    ], undefined, { PATH: `${reviewerBin}:${process.env.PATH}`, REVIEW_INVOCATION: invocationPath });
    expect(amended.status).not.toBe(0);
    expect(amended.stderr).toMatch(/candidate_read_only checkpoint\[candidate_review\] artifact hash mismatch/);

    delete reusableWorkflow.amendment_mode;
    await fs.writeJson(workflowPath, reusableWorkflow);
    const recoveryOut = path.join(testDir, 'candidate-recovery-out');
    const interrupted = runWorkflow(['--workflow', workflowPath, '--out-dir', recoveryOut], undefined, {
      PATH: `${reviewerBin}:${process.env.PATH}`, REVIEW_INVOCATION: invocationPath,
      JUNO_WORKFLOW_TEST_INTERRUPT_AT: 'checkpoint_before_terminal_manifest',
    });
    expect(interrupted.status).toBe(86);
    await fs.remove(path.join(recoveryOut, 'active_step.json'));
    const recoveryGuard = path.join(recoveryOut, 'steps', 'candidate_review', 'candidate_read_only.json');
    await fs.writeFile(recoveryGuard, `${await fs.readFile(recoveryGuard, 'utf8')} `);
    const recovered = runWorkflow(['recover-attempt', recoveryOut, '--dry-run'], undefined, {
      PATH: `${reviewerBin}:${process.env.PATH}`,
    });
    expect(recovered.status).not.toBe(0);
    expect(recovered.stderr).toMatch(/candidate_read_only checkpoint\[candidate_review\] artifact hash mismatch/);
  }, contentionBudgetMs(300_000));

  it('warns when a runtime copy differs from the installed workflow template', async () => {
    const templateDir = path.join(testDir, 'templates');
    const staleScript = path.join(testDir, 'workflow_runner.sh');
    await fs.ensureDir(templateDir);
    await fs.copyFile(templateScript, path.join(templateDir, 'workflow_runner.sh'));
    await fs.copyFile(
      path.resolve(process.cwd(), 'src/templates/scripts/workflow_run_evidence.py'),
      path.join(testDir, 'workflow_run_evidence.py'),
    );
    await fs.copyFile(
      path.resolve(process.cwd(), 'src/templates/scripts/invocation_correlation.py'),
      path.join(testDir, 'invocation_correlation.py'),
    );
    await fs.writeFile(staleScript, `${await fs.readFile(templateScript, 'utf8')}\n# local stale edit\n`);

    const result = runWorkflowScript(staleScript, ['--help'], undefined, {
      YYLO_SCRIPT_TEMPLATE_DIR: templateDir,
    });

    expect(result.status, result.stderr).toBe(0);
    expect(result.stderr).toContain('workflow_runner.sh: warning: this runtime script differs from the installed yylo template.');
    expect(result.stderr).toContain(`installed template: ${await fs.realpath(path.join(templateDir, 'workflow_runner.sh'))}`);
    expect(result.stderr).toContain('update with: yy scripts update --force');
  });

  it('selects the controller .venv_juno runtime like the Kanban launcher', async () => {
    const workflowPath = path.join(testDir, 'runtime-check.json');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'runtime_check',
      steps: [{ id: 'noop', command: ['printf', 'ok'] }],
    });

    const result = runWorkflow(['lint', '--workflow', workflowPath], undefined, {
      JUNO_DEBUG: 'true',
      VIRTUAL_ENV: '',
    });

    expect(result.status).toBe(0);
    expect(result.stderr).toContain(
      `[DEBUG] workflow_runner.sh Python runtime: ${path.join(await fs.realpath(workflowFixtureController!), '.venv_juno', 'bin', 'python')}`,
    );
  });

  it('re-execs when only the managed prefix has PyYAML', async () => {
    const controller = await fs.realpath(workflowFixtureController!);
    const venv = path.join(controller, '.venv_juno');
    const python = path.join(venv, 'bin', 'python');
    const pythonVersion = spawnSync(python, ['-c', 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")'], {
      encoding: 'utf8',
    }).stdout.trim();
    await fs.writeFile(
      path.join(venv, 'pyvenv.cfg'),
      `home = ${path.dirname(await fs.realpath(python))}\ninclude-system-site-packages = false\nversion = ${pythonVersion.slice(6)}\n`,
    );

    const input = `schema_version: 2
workflow_id: managed_prefix_stdin
steps:
  - id: noop
    command:
      - printf
      - |
        stdin and argv preserved
summary:
  command:
    - printf
    - done
`;
    const result = spawnSync('python3', ['-S', templateScript, 'lint', '--workflow', '-'], {
      cwd: controller,
      input,
      encoding: 'utf8',
      timeout: WORKFLOW_CHILD_TIMEOUT_MS,
      env: {
        ...process.env,
        JUNO_TASK_ROOT: controller,
        JUNO_WORKSPACE_ROLE: 'controller',
        JUNO_WORKSPACE_ENFORCEMENT: 'strict',
        YYLO_SESSION_METADATA_DIRECTORY: path.join(controller, '.test-metadata'),
        JUNO_DEBUG: 'true',
      },
    });

    expect(result.status, result.stderr).toBe(0);
    expect(workflowFixturePythonExecutable).toBeDefined();
    expect(await fs.realpath(python)).toBe(await fs.realpath(workflowFixturePythonExecutable!));
    expect(result.stderr).toContain(`[DEBUG] workflow_runner.sh Python runtime: ${python}`);
    expect(result.stderr).toContain(`prefix: ${venv}`);
    expect(result.stderr).toContain(`PyYAML:`);
    expect(result.stdout).toContain('Workflow lint');
    expect(result.stdout).toContain('OK: no issues found');
  });

  it('ignores an inherited plain-target re-exec marker', async () => {
    const controller = await fs.realpath(workflowFixtureController!);
    const venv = path.join(controller, '.venv_juno');
    const result = spawnSync('python3', ['-S', templateScript, '--help'], {
      cwd: controller,
      encoding: 'utf8',
      timeout: WORKFLOW_CHILD_TIMEOUT_MS,
      env: {
        ...process.env,
        JUNO_TASK_ROOT: controller,
        JUNO_WORKSPACE_ROLE: 'controller',
        JUNO_WORKSPACE_ENFORCEMENT: 'strict',
        JUNO_WORKFLOW_PYTHON_REEXEC: venv,
      },
    });

    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout).toContain('Run an ordered YAML workflow');
  });

  it('fails actionably after a genuine same-process re-exec does not establish the managed prefix', async () => {
    const controller = await fs.realpath(workflowFixtureController!);
    const venv = path.join(controller, '.venv_juno');
    const python = path.join(venv, 'bin', 'python');
    const ambientPython = spawnSync('sh', ['-c', 'command -v python3'], { encoding: 'utf8' }).stdout.trim();
    await fs.remove(python);
    await fs.writeFile(python, `#!/usr/bin/env bash\nexec ${JSON.stringify(ambientPython)} -S "$@"\n`, { mode: 0o755 });

    const result = spawnSync('python3', ['-S', templateScript, '--help'], {
      cwd: controller,
      encoding: 'utf8',
      timeout: WORKFLOW_CHILD_TIMEOUT_MS,
      env: {
        ...process.env,
        JUNO_TASK_ROOT: controller,
        JUNO_WORKSPACE_ROLE: 'controller',
        JUNO_WORKSPACE_ENFORCEMENT: 'strict',
      },
    });

    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain('controller Python re-exec did not establish the managed environment');
    expect(result.stderr).toContain(`expected prefix ${venv}`);
    expect(result.stderr).toContain('active prefix');
    expect(result.stderr).toContain('managed site-packages');
  });

  it('provisions a missing controller venv and preserves activation, argv, and stdin across re-exec', async () => {
    const controller = path.join(testDir, 'provision-controller');
    const scriptsDir = path.join(controller, '.juno_task', 'scripts');
    const installerLog = path.join(testDir, 'installer.log');
    await fs.ensureDir(scriptsDir);
    await fs.writeFile(
      path.join(scriptsDir, 'install_requirements.sh'),
      `#!/usr/bin/env bash
set -euo pipefail
[[ "$PWD" == "$EXPECTED_CONTROLLER" ]]
[[ -z "${'${VIRTUAL_ENV:-}'}" ]]
[[ -z "${'${CONDA_PREFIX:-}'}" ]]
[[ -z "${'${CONDA_DEFAULT_ENV:-}'}" ]]
[[ -z "${'${PYTHONHOME:-}'}" ]]
[[ -z "${'${PYTHONPATH:-}'}" ]]
[[ ":$PATH:" != *":/foreign/project/.venv_juno/bin:"* ]]
if IFS= read -r unexpected; then
  echo "installer consumed stdin: $unexpected" >&2
  exit 41
fi
mkdir -p .venv_juno/bin
cp "$REAL_PYTHON" .venv_juno/bin/python
chmod +x .venv_juno/bin/python
printf '%s\n' "$PWD" > "$INSTALLER_LOG"
`,
      { mode: 0o755 },
    );

    const harness = `
import importlib.machinery, importlib.util, json, os, pathlib, sys
script, controller = sys.argv[1:]
loader = importlib.machinery.SourceFileLoader("workflow_runner_under_test", script)
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
os.environ["VIRTUAL_ENV"] = "/foreign/project/.venv_juno"
os.environ["CONDA_PREFIX"] = "/foreign/conda"
os.environ["CONDA_DEFAULT_ENV"] = "foreign"
os.environ["PYTHONHOME"] = "/foreign/python-home"
os.environ["PATH"] = "/foreign/project/.venv_juno/bin:/foreign/conda/bin:" + os.environ.get("PATH", "")
module.sys.argv = [script, "lint", "--workflow", "-"]
def capture_exec(executable, argv, env):
    print(json.dumps({
        "executable": executable,
        "argv": argv,
        "virtual_env": env.get("VIRTUAL_ENV"),
        "path": env.get("PATH", ""),
        "path_head": env.get("PATH", "").split(os.pathsep)[0],
        "has_conda": "CONDA_PREFIX" in env or "CONDA_DEFAULT_ENV" in env,
        "has_pythonhome": "PYTHONHOME" in env,
        "has_pythonpath": "PYTHONPATH" in env,
        "stdin": sys.stdin.read(),
    }))
    raise SystemExit(0)
module.os.execve = capture_exec
module.ensure_controller_python_environment({"JUNO_TASK_ROOT": controller, "JUNO_CONTROLLER_SOURCE": "test"})
`;
    const input = 'schema_version: 1\nworkflow_id: stdin-preserved\n';
    const result = spawnSync('python3', ['-c', harness, templateScript, controller], {
      cwd: repoRoot,
      input,
      encoding: 'utf8',
      timeout: WORKFLOW_CHILD_TIMEOUT_MS,
      env: {
        ...process.env,
        EXPECTED_CONTROLLER: controller,
        INSTALLER_LOG: installerLog,
        REAL_PYTHON: spawnSync('sh', ['-c', 'command -v python3'], { encoding: 'utf8' }).stdout.trim(),
        PYTHONPATH: spawnSync('python3', ['-c', 'import pathlib, yaml; print(pathlib.Path(yaml.__file__).parent.parent)'], {
          encoding: 'utf8',
        }).stdout.trim(),
      },
    });

    expect(result.status, result.stderr).toBe(0);
    const payload = JSON.parse(result.stdout.trim().split('\n').at(-1) ?? '{}');
    const canonicalController = await fs.realpath(controller);
    const expectedVenv = path.join(canonicalController, '.venv_juno');
    expect(await fs.readFile(installerLog, 'utf8')).toBe(`${canonicalController}\n`);
    expect(payload.executable).toBe(path.join(expectedVenv, 'bin', 'python'));
    expect(payload.argv).toEqual([
      path.join(expectedVenv, 'bin', 'python'),
      await fs.realpath(templateScript),
      'lint',
      '--workflow',
      '-',
    ]);
    expect(payload.virtual_env).toBe(expectedVenv);
    expect(payload.path_head).toBe(path.join(expectedVenv, 'bin'));
    expect(payload.path).not.toContain('/foreign/project/.venv_juno/bin');
    expect(payload.path).not.toContain('/foreign/conda/bin');
    expect(payload.has_conda).toBe(false);
    expect(payload.has_pythonhome).toBe(false);
    expect(payload.has_pythonpath).toBe(false);
    expect(payload.stdin).toBe(input);
  });

  it('does not let hostile PYTHONPATH modules satisfy or replace managed PyYAML', async () => {
    const controller = await fs.realpath(workflowFixtureController!);
    const hostilePath = path.join(testDir, 'hostile-pythonpath');
    await fs.ensureDir(hostilePath);
    await fs.writeFile(path.join(hostilePath, 'yaml.py'), 'raise RuntimeError("hostile yaml imported")\n');
    const workflowPath = path.join(testDir, 'hostile-pythonpath.yaml');
    await fs.writeFile(workflowPath, 'schema_version: 1\nworkflow_id: hostile_path\nsteps:\n  - id: noop\n    command: true\n');

    const managedPython = path.join(controller, '.venv_juno', 'bin', 'python');
    const result = spawnSync(managedPython, [templateScript, 'lint', '--workflow', workflowPath], {
      cwd: controller,
      encoding: 'utf8',
      timeout: WORKFLOW_CHILD_TIMEOUT_MS,
      env: {
        ...process.env,
        PYTHONPATH: hostilePath,
        JUNO_TASK_ROOT: controller,
        JUNO_WORKSPACE_ROLE: 'controller',
        JUNO_WORKSPACE_ENFORCEMENT: 'strict',
      },
    });

    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout).toContain('OK: no issues found');
    expect(result.stderr).not.toContain('hostile yaml imported');
  });

  it('allows workflow stale-runtime warnings to be disabled', async () => {
    const templateDir = path.join(testDir, 'templates');
    const staleScript = path.join(testDir, 'workflow_runner_skip.sh');
    await fs.ensureDir(templateDir);
    await fs.copyFile(templateScript, path.join(templateDir, 'workflow_runner.sh'));
    await fs.copyFile(
      path.resolve(process.cwd(), 'src/templates/scripts/workflow_run_evidence.py'),
      path.join(testDir, 'workflow_run_evidence.py'),
    );
    await fs.copyFile(
      path.resolve(process.cwd(), 'src/templates/scripts/invocation_correlation.py'),
      path.join(testDir, 'invocation_correlation.py'),
    );
    await fs.writeFile(staleScript, `${await fs.readFile(templateScript, 'utf8')}\n# local stale edit\n`);

    const result = runWorkflowScript(staleScript, ['--help'], undefined, {
      YYLO_SCRIPT_TEMPLATE_DIR: templateDir,
      YYLO_SKIP_SCRIPT_STALE_CHECK: '1',
    });

    expect(result.status).toBe(0);
    expect(result.stderr).not.toContain('runtime script differs');
  });

  it('documents workflow options, failure policy, and auto capture behavior in --help', () => {
    const result = runWorkflow(['--help']);

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('--workflow');
    expect(result.stdout).toContain("'-' to read from stdin");
    expect(result.stdout).toContain('--dry-run');
    expect(result.stdout).toContain('--from-step');
    expect(result.stdout).toContain('--var NAME=VALUE');
    expect(result.stdout).toContain('--run-root');
    expect(result.stdout).toContain('--print-output');
    expect(result.stdout).toContain('summary, none, all, <step_id>');
    expect(result.stdout).toContain('step:<step_id>');
    expect(result.stdout).toContain('--print-step-stdout');
    expect(result.stdout).toContain('--no-print-step-stdout');
    expect(result.stdout).toContain('--tmux');
    expect(result.stdout).toContain('--tmux-session');
    expect(result.stdout).toContain('--init-example NAME PATH');
    expect(result.stdout).toContain('production-triage-handoff');
    expect(result.stdout).toContain('parallel-kanban-review');
    expect(result.stdout).toContain('workflow_runner.sh lint --workflow WORKFLOW.yaml');
    expect(result.stdout).toContain('workflow_runner.sh doctor RUN_DIR');
    expect(result.stdout).toContain('workflow_runner.sh recover-attempt RUN_DIR');
    expect(result.stdout).toContain('detached observer only; the producer remains');
    expect(result.stdout).toContain('attached to this foreground command');
    expect(result.stdout).toContain('fail_workflow: true');
    expect(result.stdout).toContain('yylo, yy, and ypl');
    expect(result.stdout).toContain('capture_session: false');
    expect(result.stdout).toContain('does not inject --quiet');
    expect(result.stdout).toContain('empty response');
  });

  it('provides dedicated help for lint and doctor helper commands', () => {
    const lintHelp = runWorkflow(['lint', '--help']);
    expect(lintHelp.status).toBe(0);
    expect(lintHelp.stdout).toContain('Lint workflow YAML');
    expect(lintHelp.stdout).toContain('steps.<id>.response');

    const doctorHelp = runWorkflow(['doctor', '--help']);
    expect(doctorHelp.status).toBe(0);
    expect(doctorHelp.stdout).toContain('Inspect a workflow run directory');
    expect(doctorHelp.stdout).toContain('workflow_runner.sh dr');
  });

  it('fallback-parses literal blocks in command and summary.command lists without PyYAML', async () => {
    const controller = await fs.realpath(workflowFixtureController!);
    const managedLib = path.join(controller, '.venv_juno', 'lib');
    const yamlDirectories = (await fs.readdir(managedLib)).map((version) => path.join(managedLib, version, 'site-packages', 'yaml'));
    await Promise.all(yamlDirectories.map((directory) => fs.remove(directory)));
    const workflowPath = path.join(testDir, 'literal-command-lists.yaml');
    await fs.writeFile(
      workflowPath,
      `schema_version: 1
workflow_id: literal_command_lists
steps:
  - id: multiline
    command:
      - printf
      - |
        first line
        second line
summary:
  command:
    - printf
    - |
      summary line one
      summary line two
`,
    );

    const hostilePath = path.join(testDir, 'fallback-hostile-pythonpath');
    await fs.ensureDir(hostilePath);
    await fs.writeFile(path.join(hostilePath, 'yaml.py'), 'raise RuntimeError("fallback imported hostile yaml")\n');
    const result = runWorkflow(['lint', '--workflow', workflowPath], undefined, { PYTHONPATH: hostilePath });

    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout).toContain('OK: no issues found');
  });

  it('lints workflow YAML for noisy agent stdout/stderr template usage', async () => {
    const { executablePath } = await installFakeJunoExecutable(testDir, 'yy');
    const workflowPath = path.join(testDir, 'lint-me.yaml');
    await fs.writeFile(
      workflowPath,
      `schema_version: 1
workflow_id: lint_me
steps:
  - id: agent
    command:
      - ${JSON.stringify(executablePath)}
      - pi
      - prompt
  - id: summarize
    command: |
      printf '{{ steps.agent.stdout }} {{ steps.agent.stderr }}'
summary: |
  Agent stdout: {{ steps.agent.stdout }}
  Agent stderr: {{ steps.agent.stderr }}
`,
    );

    const result = runWorkflow(['lint', '--workflow', workflowPath]);

    expect(result.status).toBe(1);
    expect(result.stdout).toContain('AGENT_STDOUT_TEMPLATE');
    expect(result.stdout).toContain('NOISY_STEP_STDERR_TEMPLATE');
    expect(result.stdout).toContain('use steps.agent.response');
  });

  it('doctors workflow run artifacts for empty successful agent responses and quiet argv', async () => {
    const runDir = path.join(testDir, 'run');
    await fs.ensureDir(runDir);
    const responsePath = path.join(runDir, '001_agent.response.txt');
    const stdoutPath = path.join(runDir, '001_agent.stdout.txt');
    const stderrPath = path.join(runDir, '001_agent.stderr.txt');
    await fs.writeFile(responsePath, '');
    await fs.writeFile(stdoutPath, '');
    await fs.writeFile(stderrPath, 'logs only\n');
    await fs.writeJson(path.join(runDir, 'manifest.json'), {
      steps: [
        {
          id: 'agent',
          command: ['yy', '--quiet', 'pi', 'prompt'],
          status: 'success',
          response_path: responsePath,
          stdout_path: stdoutPath,
          stderr_path: stderrPath,
        },
      ],
    });

    const result = runWorkflow(['dr', runDir]);

    expect(result.status).toBe(1);
    expect(result.stdout).toContain('EMPTY_SUCCESS_AGENT_RESPONSE');
    expect(result.stdout).toContain('AGENT_QUIET_ARG');
    expect(result.stdout).toContain('SUCCESS_STDERR_ARTIFACT');
  });

  it('writes named example workflows on demand and refuses accidental overwrite', async () => {
    const target = path.join(testDir, 'agent_chain.yaml');

    const first = runWorkflow(['--init-example', 'agent-chain', target]);
    expect(first.status).toBe(0);
    const content = await fs.readFile(target, 'utf8');
    expect(content).toContain('workflow_id: example_agent_chain');
    expect(content).toContain('{{ steps.first_agent.session_id }}');
    expect(content).toContain('- yy');
    expect(content).toContain('- --resume');

    const second = runWorkflow(['--init-example', 'agent-chain', target]);
    expect(second.status).toBe(2);
    expect(second.stderr).toContain('refusing to overwrite');

    const forced = runWorkflow(['--init-example', 'command-pipeline', target, '--force']);
    expect(forced.status).toBe(0);
    expect(await fs.readFile(target, 'utf8')).toContain('workflow_id: example_command_pipeline');
  });

  it('provides all approved boilerplate example names without auto-installing workflows', async () => {
    for (const name of ['agent-chain', 'command-pipeline', 'daily-ops', 'production-triage-handoff', 'parallel-kanban-review']) {
      const target = path.join(testDir, `${name}.yaml`);
      const result = runWorkflow(['--init-example', name, target]);
      expect(result.status).toBe(0);
      expect(await fs.pathExists(target)).toBe(true);
    }
    expect(await fs.pathExists(path.join(repoRoot, '.juno_task', 'workflows', 'agent_chain.yaml'))).toBe(false);
  });

  it('writes practical tmux handoff and kanban review examples that dry-run cleanly', async () => {
    const examples = [
      {
        name: 'production-triage-handoff',
        expected: ['workflow_id: production_triage_handoff', '--tmux panes', '--tmux-handoff', '--max-panes-per-session 4', '--output-dir "{{ out_dir }}/parallel"'],
      },
      {
        name: 'parallel-kanban-review',
        expected: [
          'workflow_id: parallel_kanban_review',
          'TASK_IDS=',
          'aggregation_*.json',
          '--output-dir "{{ out_dir }}/parallel"',
          '- id: prepare_master_review',
          '- "{{ out_dir }}/master_review_prompt.md"',
          '      - --prompt-file',
        ],
      },
    ];

    for (const example of examples) {
      const target = path.join(testDir, `${example.name}.yaml`);
      const outDir = path.join(testDir, `${example.name}-out`);
      const init = runWorkflow(['--init-example', example.name, target, '--force']);
      expect(init.status).toBe(0);
      const content = await fs.readFile(target, 'utf8');
      for (const expected of example.expected) {
        expect(content).toContain(expected);
      }

      const dryRun = runWorkflow(['--workflow', target, '--out-dir', outDir, '--dry-run', '--final-output', 'none']);
      expect(dryRun.status).toBe(0);
      const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
      expect(manifest.dry_run).toBe(true);
      expect(manifest.steps.length).toBeGreaterThan(0);
    }
  });

  it('dry-run renders a minimal YAML workflow and writes manifest/summary artifacts', async () => {
    const workflowPath = path.join(testDir, 'workflow.yml');
    const outDir = path.join(testDir, 'out');
    await fs.writeFile(
      workflowPath,
      `name: dry-run-test
vars:
  who: workflow
steps:
  - id: greet
    command: |
      printf 'hello {{ vars.who }}\\n'
summary: |
  status={{ steps.greet.status }}
`,
    );

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--dry-run']);

    expect(result.status).toBe(0);
    expect(result.stdout).toContain("printf 'hello workflow\\n'");
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.dry_run).toBe(true);
    expect(manifest.steps[0].status).toBe('dry_run');
    expect(await fs.readFile(path.join(outDir, 'summary.md'), 'utf8')).toContain('status=dry_run');
  });

  it('withholds the retired integration child-evidence capability from generic workflows', async () => {
    const workflowPath = path.join(testDir, 'retired-child-evidence.json');
    const outDir = path.join(testDir, 'retired-child-evidence-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'retired_child_evidence',
      steps: [{
        id: 'probe',
        fail_workflow: true,
        command: ['python3', '-c', "import os; print(os.environ.get('JUNO_WORKFLOW_CHILD_EVIDENCE_DIR', 'CLEARED'))"],
      }],
    });
    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none']);
    expect(result.status, result.stderr).toBe(0);
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(await fs.readFile(manifest.steps[0].stdout_path, 'utf8')).toBe('CLEARED\n');
    expect(await fs.pathExists(path.join(outDir, 'child_steps', 'probe'))).toBe(false);
  });

  it('withholds child-evidence capability from unrelated workflow steps', async () => {
    const workflowPath = path.join(testDir, 'unrelated-child-capability.json');
    const outDir = path.join(testDir, 'unrelated-child-capability-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'unrelated_child_capability',
      steps: [{ id: 'validate', command: ['python3', '-c', "import os; print(os.environ.get('JUNO_WORKFLOW_CHILD_EVIDENCE_DIR', 'CLEARED'))"] }],
    });
    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none']);
    expect(result.status, result.stderr).toBe(0);
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(await fs.readFile(manifest.steps[0].stdout_path, 'utf8')).toBe('CLEARED\n');
    expect(await fs.pathExists(path.join(outDir, 'child_steps', 'validate'))).toBe(false);
  });

  it('does not grant capability when owner markers are only attacker arguments', async () => {
    const workflowPath = path.join(testDir, 'spoofed-child-capability.json');
    const outDir = path.join(testDir, 'spoofed-child-capability-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'spoofed_child_capability',
      steps: [{
        id: 'attacker',
        command: [
          'python3',
          '-c',
          "import os; print(os.environ.get('JUNO_WORKFLOW_CHILD_EVIDENCE_DIR', 'CLEARED'))",
          'integration_owner_preflight.py',
          'integrate',
          '--actual-review-command',
          'yy pi review',
          '--actual-review-receipt',
          'actual.json',
        ],
      }],
    });
    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none']);
    expect(result.status, result.stderr).toBe(0);
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(await fs.readFile(manifest.steps[0].stdout_path, 'utf8')).toBe('CLEARED\n');
    expect(await fs.pathExists(path.join(outDir, 'child_steps', 'attacker'))).toBe(false);
  });

  it('withholds child-evidence capability from a directly executed same-basename script', async () => {
    const attackerDir = path.join(testDir, 'attacker');
    await fs.ensureDir(attackerDir);
    const attacker = path.join(attackerDir, 'integration_owner_preflight.py');
    await fs.writeFile(attacker, "import os; print(os.environ.get('JUNO_WORKFLOW_CHILD_EVIDENCE_DIR', 'CLEARED'))\n");
    const workflowPath = path.join(testDir, 'same-basename-child-capability.json');
    const outDir = path.join(testDir, 'same-basename-child-capability-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'same_basename_child_capability',
      steps: [{ id: 'attacker', command: ['python3', attacker, 'integrate', '--actual-review-command', 'yy pi review', '--actual-review-receipt', 'actual.json'] }],
    });
    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none']);
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('legacy local_integration execution is read-only');
    expect(await fs.pathExists(path.join(outDir, 'manifest.json'))).toBe(false);
  });

  it('does not grant retired child-evidence capability through a generic canonical-owner command', async () => {
    const projectRoot = path.join(testDir, 'relative-owner-project');
    const managedScripts = path.join(projectRoot, '.juno_task', 'scripts');
    await fs.ensureDir(managedScripts);
    const managedRunner = path.join(managedScripts, 'workflow_runner.sh');
    await fs.copy(templateScript, managedRunner);
    await fs.copy(path.join(path.dirname(templateScript), 'workflow_run_evidence.py'), path.join(managedScripts, 'workflow_run_evidence.py'));
    await fs.copy(path.join(path.dirname(templateScript), 'invocation_correlation.py'), path.join(managedScripts, 'invocation_correlation.py'));
    await installFakeChildEvidenceProducer(managedScripts);
    const workflowPath = path.join(testDir, 'relative-owner-child-capability.json');
    const outDir = path.join(testDir, 'relative-owner-child-capability-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'relative_owner_child_capability',
      steps: [{ id: 'integrate', command: ['.juno_task/scripts/integration_owner_preflight.py', 'integrate', '--actual-review-command', 'yy pi review', '--actual-review-receipt', 'actual.json'] }],
    });
    const result = runWorkflowScript(managedRunner, ['--project-root', projectRoot, '--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none'], undefined, { YYLO_SKIP_SCRIPT_STALE_CHECK: '1' });
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('legacy local_integration execution is read-only');
    expect(await fs.pathExists(path.join(outDir, 'manifest.json'))).toBe(false);
  });

  it('withholds child-evidence capability from lexical aliases of the canonical owner path', async () => {
    let managedScripts = path.join(testDir, 'managed-alias-scripts');
    await fs.ensureDir(managedScripts);
    managedScripts = await fs.realpath(managedScripts);
    const managedRunner = path.join(managedScripts, 'workflow_runner.sh');
    await fs.copy(templateScript, managedRunner);
    await fs.copy(path.join(path.dirname(templateScript), 'workflow_run_evidence.py'), path.join(managedScripts, 'workflow_run_evidence.py'));
    await fs.copy(path.join(path.dirname(templateScript), 'invocation_correlation.py'), path.join(managedScripts, 'invocation_correlation.py'));
    const owner = path.join(managedScripts, 'integration_owner_preflight.py');
    await fs.writeFile(owner, "#!/usr/bin/env python3\nimport os; print(os.environ.get('JUNO_WORKFLOW_CHILD_EVIDENCE_DIR', 'CLEARED'))\n");
    await fs.chmod(owner, 0o755);
    const alias = `${managedScripts}/../${path.basename(managedScripts)}/integration_owner_preflight.py`;
    const workflowPath = path.join(testDir, 'alias-child-capability.json');
    const outDir = path.join(testDir, 'alias-child-capability-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'alias_child_capability',
      steps: [{ id: 'alias', command: [alias, 'integrate', '--actual-review-command', 'yy pi review', '--actual-review-receipt', 'actual.json'] }],
    });
    const result = runWorkflowScript(managedRunner, ['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none'], undefined, { YYLO_SKIP_SCRIPT_STALE_CHECK: '1' });
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('legacy local_integration execution is read-only');
    expect(await fs.pathExists(path.join(outDir, 'manifest.json'))).toBe(false);
  });

  it('accepts stdin workflow via --workflow -', async () => {
    const outDir = path.join(testDir, 'stdin-out');
    const result = runWorkflow(
      ['--workflow', '-', '--out-dir', outDir, '--dry-run', '--final-output', 'none'],
      `name: stdin-test
steps:
  - id: from_stdin
    command: echo stdin-ok
`,
    );

    expect(result.status).toBe(0);
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.steps[0].id).toBe('from_stdin');
    expect(manifest.steps[0].command).toBe('echo stdin-ok');
  });

  it('lints task hydration as a bounded fail-closed argv profile', async () => {
    const workflowPath = path.join(testDir, 'unsafe-task-hydration.json');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'unsafe_task_hydration',
      workflow_class: 'task_hydration',
      steps: [{
        id: 'unsafe', probe: ['false'], command: ['yy', 'task', 'finish', 'X'],
        timeout_seconds: 30, fail_workflow: true, non_interactive: true,
        network: false, sensitive: false, outputs: [],
      }],
    });
    const result = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('forbidden controller/lifecycle surface');
  });

  it('terminates a timed-out task hydration step with typed exit 124 evidence', async () => {
    const workflowPath = path.join(testDir, 'timeout-task-hydration.json');
    const outDir = path.join(testDir, 'timeout-task-hydration-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'timeout_task_hydration',
      workflow_class: 'task_hydration',
      steps: [{
        id: 'timeout', probe: ['python3', '-c', 'raise SystemExit(1)'],
        command: ['python3', '-c', 'import time; time.sleep(30)'],
        timeout_seconds: 1, fail_workflow: true, non_interactive: true,
        network: false, sensitive: false, outputs: [],
      }],
    });
    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none']);
    expect(result.status).toBe(124);
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.steps[0].exit_code).toBe(124);
    expect(await fs.readFile(path.join(outDir, '001_timeout.stderr.txt'), 'utf8'))
      .toContain('command timed out after 1s');
  });

  it('does not reuse prior-step process-group ownership for a probe-satisfied step', async () => {
    const workflowPath = path.join(testDir, 'probe-after-launch.json');
    const outDir = path.join(testDir, 'probe-after-launch-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'probe_after_launch',
      steps: [
        { id: 'launched', command: ['python3', '-c', "print('launched')"] },
        {
          id: 'probe_satisfied',
          probe: ['python3', '-c', 'raise SystemExit(0)'],
          command: ['python3', '-c', "raise SystemExit('must not run')"],
        },
      ],
    });
    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none']);
    expect(result.status, result.stderr).toBe(0);
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.status).toBe('success');
    expect(manifest.steps.map((step: { status: string }) => step.status)).toEqual(['success', 'success']);
    expect(manifest.steps[1].probe_satisfied).toBe(true);
  });

  it('fails closed when a launched command leaves a live process-group descendant', async () => {
    const workflowPath = path.join(testDir, 'live-descendant.json');
    const outDir = path.join(testDir, 'live-descendant-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'live_descendant',
      steps: [{
        id: 'leaks',
        command: ['bash', '-c', 'sleep 30 </dev/null >/dev/null 2>&1 &'],
        fail_workflow: true,
      }],
    });
    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none']);
    expect(result.status).toBe(1);
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.steps[0].failure_reason).toContain('command process group remains active');
    const active = await fs.readJson(path.join(outDir, 'active_step.json'));
    try { process.kill(-active.process_group_id, 'SIGKILL'); } catch {}
  });

  it('does not exit non-zero for a failed step by default but reports the failure', async () => {
    const workflowPath = path.join(testDir, 'fail-default.yml');
    const outDir = path.join(testDir, 'fail-default-out');
    await fs.writeFile(
      workflowPath,
      `name: fail-default
steps:
  - id: fail
    command: python3 -c "import sys; print('before-fail'); sys.exit(7)"
  - id: after
    command: echo after-ran
`,
    );

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--final-output', 'none']);

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('before-fail');
    expect(result.stdout).toContain('after-ran');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.status).toBe('failed');
    expect(manifest.failed_steps).toEqual(['fail']);
    expect(manifest.steps.map((step: { id: string }) => step.id)).toEqual(['fail', 'after']);
  });

  it('exits non-zero when a step opts into fail_workflow', async () => {
    const workflowPath = path.join(testDir, 'fail-fast.yml');
    const outDir = path.join(testDir, 'fail-fast-out');
    await fs.writeFile(
      workflowPath,
      `name: fail-fast
steps:
  - id: fail
    command: python3 -c "import sys; sys.exit(9)"
    fail_workflow: true
  - id: skipped
    command: echo should-not-run
`,
    );

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--final-output', 'none']);

    expect(result.status).toBe(9);
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.failed_steps).toEqual(['fail']);
    expect(manifest.steps.map((step: { id: string }) => step.id)).toEqual(['fail']);
  });

  it('renders builtins, direct var aliases, prior step fields, artifact layout, and selected step output', async () => {
    const workflowPath = path.join(testDir, 'context.yml');
    const outDir = path.join(testDir, 'context-out');
    await fs.writeFile(
      workflowPath,
      `schema_version: 1
workflow_id: context-run
vars:
  who: workflow
steps:
  - id: first
    command: printf 'hello {{ who }} {{ today_utc }} {{ repo_root }}'
  - id: second
    command: printf 'status={{ steps.first.status }} exit={{ steps.first.exit_code }} stdout={{ steps.first.stdout }}'
summary: |
  run={{ run_id }} workflow={{ workflow_id }} dir={{ workflow_dir }}
`,
    );

    const result = runWorkflow([
      '--workflow',
      workflowPath,
      '--out-dir',
      outDir,
      '--var',
      'who=override',
      '--print-output',
      'second',
    ]);

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('status=success exit=0 stdout=hello override');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.workflow_id).toBe('context-run');
    expect(manifest.repo_root).toBe(await fs.realpath(workflowFixtureController!));
    expect(await fs.pathExists(path.join(outDir, '001_first.stdout.txt'))).toBe(true);
    expect(await fs.pathExists(path.join(outDir, 'summary.stdout.txt'))).toBe(true);
    expect(await fs.readFile(path.join(outDir, 'summary.md'), 'utf8')).toContain('workflow=context-run');
  });

  it('prints color-ready start, response, and end separators while leaving response text plain', async () => {
    const workflowPath = path.join(testDir, 'separators.json');
    const outDir = path.join(testDir, 'separators-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'separators',
      steps: [{ id: 'alpha', command: ['bash', '-lc', 'echo ACTUAL_RESPONSE'] }],
    });

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none']);

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('START: step 1 [alpha]');
    expect(result.stdout).toContain('RESPONSE: step 1 [alpha]');
    expect(result.stdout).toContain('ACTUAL_RESPONSE\n');
    expect(result.stdout).toContain('END: step 1 [alpha] status=success');
  });

  it('runs from a zero-based step index and records skipped prior steps', async () => {
    const workflowPath = path.join(testDir, 'from-index.json');
    const outDir = path.join(testDir, 'from-index-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'from-index',
      steps: [
        { id: 'first', command: ['bash', '-lc', 'echo first'] },
        { id: 'second', command: ['bash', '-lc', 'echo second'] },
        { id: 'third', command: ['bash', '-lc', 'echo third'] },
      ],
    });

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--from-step', '1', '--print-output', 'none']);

    expect(result.status).toBe(0);
    expect(result.stdout).not.toContain('first\n');
    expect(result.stdout).toContain('second\n');
    expect(result.stdout).toContain('third\n');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.from_step_index).toBe(1);
    expect(manifest.steps.map((step: { status: string }) => step.status)).toEqual(['skipped', 'success', 'success']);
  });

  it('runs from a named step and supports -1 for only the last step', async () => {
    const workflowPath = path.join(testDir, 'from-name.json');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'from-name',
      steps: [
        { id: 'first', command: ['bash', '-lc', 'echo first'] },
        { id: 'second', command: ['bash', '-lc', 'echo second'] },
        { id: 'third', command: ['bash', '-lc', 'echo third'] },
      ],
    });

    const byName = runWorkflow([
      '--workflow',
      workflowPath,
      '--out-dir',
      path.join(testDir, 'from-name-out'),
      '--from-step',
      'second',
      '--print-output',
      'none',
    ]);
    expect(byName.status).toBe(0);
    expect(byName.stdout).not.toContain('first\n');
    expect(byName.stdout).toContain('second\n');
    expect(byName.stdout).toContain('third\n');

    const lastOnly = runWorkflow([
      '--workflow',
      workflowPath,
      '--out-dir',
      path.join(testDir, 'from-last-out'),
      '--from-step',
      '-1',
      '--print-output',
      'none',
    ]);
    expect(lastOnly.status).toBe(0);
    expect(lastOnly.stdout).not.toContain('first\n');
    expect(lastOnly.stdout).not.toContain('second\n');
    expect(lastOnly.stdout).toContain('third\n');
  });

  it('resolves workflow vars against builtins before rendering commands', async () => {
    const workflowPath = path.join(testDir, 'vars.yml');
    const outDir = path.join(testDir, 'vars-out');
    await fs.writeFile(
      workflowPath,
      `schema_version: 1
workflow_id: vars-render
vars:
  run_date: "{{ yesterday_utc }}"
steps:
  - id: show
    command: printf 'date={{ run_date }} vars={{ vars.run_date }}'
`,
    );

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--dry-run', '--final-output', 'none']);

    expect(result.status).toBe(0);
    const manifestText = await fs.readFile(path.join(outDir, 'manifest.json'), 'utf8');
    expect(manifestText).not.toContain('{{ yesterday_utc }}');
    const manifest = JSON.parse(manifestText);
    expect(manifest.steps[0].command).toContain('date=');
    expect(manifest.steps[0].command).not.toContain('{{');
  });

  it('executes summary.command argv lists with the same semantics as step commands', async () => {
    const workflowPath = path.join(testDir, 'summary-argv.json');
    const outDir = path.join(testDir, 'summary-argv-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'summary-argv',
      steps: [{ id: 'first', command: ['bash', '-lc', 'echo first-ok'] }],
      summary: { command: ['bash', '-lc', 'printf "summary sees {{ steps.first.stdout }}"'] },
    });

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none']);

    expect(result.status).toBe(0);
    expect(await fs.readFile(path.join(outDir, 'summary.stdout.txt'), 'utf8')).toContain('summary sees first-ok');
    expect(await fs.readFile(path.join(outDir, 'summary.command.sh'), 'utf8')).toContain("bash -lc");
    expect(await fs.readFile(path.join(outDir, 'summary.command.sh'), 'utf8')).not.toContain("['bash'");
  });

  it('uses summary.command stdout for summary.md and default selected output', async () => {
    const workflowPath = path.join(testDir, 'summary-output.json');
    const outDir = path.join(testDir, 'summary-output-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'summary-output',
      steps: [{ id: 'first', command: ['bash', '-lc', 'echo step-output'] }],
      summary: { command: ['bash', '-lc', 'echo AGENT-SUMMARY'] },
    });

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--no-print-step-stdout']);

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('AGENT-SUMMARY');
    const semanticSummary = await fs.readFile(path.join(outDir, 'summary.md'), 'utf8');
    expect(semanticSummary).toContain('Controlling gate: none');
    expect(semanticSummary).toContain('Semantic outcome: completed');
    expect(semanticSummary).toMatch(/AGENT-SUMMARY\n$/);
    expect(await fs.readFile(path.join(outDir, 'summary.stdout.txt'), 'utf8')).toBe('AGENT-SUMMARY\n');
  });

  it('creates a dedicated detached tmux observer with live hidden step output', async () => {
    const binDir = path.join(testDir, 'tmux-bin');
    const tmuxLog = path.join(testDir, 'tmux-invocations.log');
    await fs.ensureDir(binDir);
    await fs.writeFile(
      path.join(binDir, 'tmux'),
      `#!/usr/bin/env sh
printf '%s\\n' "$*" >> "$FAKE_TMUX_LOG"
if [ "\${1:-}" = "has-session" ]; then exit 1; fi
exit 0
`,
    );
    await fs.chmod(path.join(binDir, 'tmux'), 0o755);
    const workflowPath = path.join(testDir, 'tmux-observer.yml');
    const outDir = path.join(testDir, 'tmux-observer-out');
    await fs.writeFile(
      workflowPath,
      `workflow_id: tmux-observer
steps:
  - id: hidden
    command: python3 -c "import time; print(''.join(map(chr, [76,73,86,69,95,72,73,68,68,69,78,95,79,85,84,80,85,84])), flush=True); time.sleep(0.05)"
`,
    );

    const result = runWorkflow(
      [
        '--workflow',
        workflowPath,
        '--out-dir',
        outDir,
        '--tmux',
        '--tmux-session',
        'review-session',
        '--no-print-step-stdout',
        '--final-output',
        'none',
      ],
      undefined,
      {
        PATH: `${binDir}:${process.env.PATH}`,
        FAKE_TMUX_LOG: tmuxLog,
      },
    );

    expect(result.status).toBe(0);
    expect(result.stdout).toContain("Workflow observer tmux session: review-session");
    expect(result.stdout).toContain("tmux attach -t review-session");
    expect(result.stdout).not.toContain('LIVE_HIDDEN_OUTPUT');
    expect(await fs.readFile(path.join(outDir, 'workflow.live.log'), 'utf8')).toContain('LIVE_HIDDEN_OUTPUT');
    expect(await fs.readFile(tmuxLog, 'utf8')).toContain('new-session -d -s review-session');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.tmux_observer).toMatchObject({ enabled: true, session: 'review-session' });
  });

  it('rejects --tmux-session without --tmux', async () => {
    const workflowPath = path.join(testDir, 'tmux-invalid.yml');
    await fs.writeFile(workflowPath, 'name: invalid\nsteps:\n  - id: one\n    command: echo one\n');

    const result = runWorkflow([
      '--workflow', workflowPath, '--tmux-session', 'orphan',
      '--run-root', testDir, '--out-dir', path.join(testDir, 'tmux-invalid-out'),
    ]);

    expect(result.status).toBe(2);
    expect(result.stderr).toContain('--tmux-session requires --tmux');
  });

  it('--no-print-step-stdout suppresses console stdout while preserving artifact stdout', async () => {
    const workflowPath = path.join(testDir, 'quiet.yml');
    const outDir = path.join(testDir, 'quiet-out');
    await fs.writeFile(
      workflowPath,
      `name: quiet
steps:
  - id: noisy
    command: python3 -c "print(''.join(map(chr, [83, 69, 67, 82, 69, 84, 95, 83, 84, 69, 80, 95, 83, 84, 68, 79, 85, 84])))"
`,
    );

    const result = runWorkflow([
      '--workflow',
      workflowPath,
      '--out-dir',
      outDir,
      '--no-print-step-stdout',
      '--final-output',
      'none',
    ]);

    expect(result.status).toBe(0);
    expect(result.stdout).not.toContain('SECRET_STEP_STDOUT');
    expect(await fs.readFile(path.join(outDir, 'steps/noisy/stdout.txt'), 'utf8')).toBe(
      'SECRET_STEP_STDOUT\n',
    );
  });

  it('does not inject quiet mode and keeps successful agent stderr out of console output', async () => {
    const binDir = path.join(testDir, 'quiet-bin');
    await fs.ensureDir(binDir);
    const executablePath = path.join(binDir, 'yy');
    await fs.writeFile(
      executablePath,
      `#!/usr/bin/env sh
if [ "\${1:-}" = "--quiet" ]; then
  echo SHOULD_NOT_BE_QUIET
  exit 3
fi
echo VERBOSE_INTERNAL_LOG >&2
echo FINAL_ONLY
`,
    );
    await fs.chmod(executablePath, 0o755);
    const workflowPath = path.join(testDir, 'quiet-juno.json');
    const outDir = path.join(testDir, 'quiet-juno-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'quiet-juno',
      steps: [{ id: 'agent', command: [executablePath, 'pi', 'prompt'], capture_session: false }],
    });

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none']);

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('FINAL_ONLY');
    expect(result.stdout).not.toContain('VERBOSE_INTERNAL_LOG');
    expect(result.stderr).not.toContain('VERBOSE_INTERNAL_LOG');
    expect(await fs.readFile(path.join(outDir, '001_agent.stderr.txt'), 'utf8')).toContain('VERBOSE_INTERNAL_LOG');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.steps[0].command).toEqual([executablePath, 'pi', 'prompt']);
  });

  it('marks a detected agent command failed when exit is zero but response is empty', async () => {
    const binDir = path.join(testDir, 'empty-agent-bin');
    await fs.ensureDir(binDir);
    const executablePath = path.join(binDir, 'yy');
    await fs.writeFile(
      executablePath,
      `#!/usr/bin/env sh
echo ONLY_LOGS_NO_RESPONSE >&2
exit 0
`,
    );
    await fs.chmod(executablePath, 0o755);
    const workflowPath = path.join(testDir, 'empty-agent.json');
    const outDir = path.join(testDir, 'empty-agent-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'empty-agent',
      steps: [{ id: 'agent', command: [executablePath, 'pi', 'prompt'], capture_session: false }],
    });

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none']);

    expect(result.status).toBe(0);
    expect(result.stderr).toContain('ONLY_LOGS_NO_RESPONSE');
    expect(result.stdout).toContain('(response is empty)');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.status).toBe('failed');
    expect(manifest.failed_steps).toEqual(['agent']);
    expect(manifest.steps[0].status).toBe('failed');
    expect(manifest.steps[0].exit_code).toBe(1);
    expect(manifest.steps[0].transport_exit_code).toBe(0);
    expect(manifest.steps[0].failure_reason).toBe('empty response from detected agent command');
  });

  it('prints canonical captured response for juno commands while preserving raw stdout artifacts', async () => {
    const binDir = path.join(testDir, 'verbose-bin');
    await fs.ensureDir(binDir);
    const executablePath = path.join(binDir, 'yy');
    await fs.writeFile(
      executablePath,
      `#!/usr/bin/env sh
if [ "\${1:-}" = "continue-scope" ]; then printf '%s\\n' '{"fullHash":"SCOPE_0123456789ABCDEF","sessionEnvKey":"YYLO_LAST_SESSION_ID_SCOPE_0123456789ABCDEF","settingsEnvKey":"YYLO_LAST_EXECUTION_SETTINGS_SCOPE_0123456789ABCDEF"}'; exit 0; fi
echo 'VERBOSE INTERNAL LOG LINE'
printf '{"type":"result","subtype":"success","is_error":false,"result":"FINAL_AGENT_RESPONSE","session_id":"session-final"}\n' > "$JUNO_SUBAGENT_CAPTURE_PATH"
`,
    );
    await fs.chmod(executablePath, 0o755);
    const workflowPath = path.join(testDir, 'canonical-response.json');
    const outDir = path.join(testDir, 'canonical-response-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'canonical-response',
      steps: [{ id: 'agent', command: [executablePath, 'pi', 'prompt'] }],
    });

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none']);

    expect(result.status).toBe(0);
    const responseStart = result.stdout.indexOf('RESPONSE: step 1 [agent]');
    const responseEnd = result.stdout.indexOf('END: step 1 [agent]');
    const responseBlock = result.stdout.slice(responseStart, responseEnd);
    expect(responseBlock).toContain('FINAL_AGENT_RESPONSE');
    expect(responseBlock).not.toContain('VERBOSE INTERNAL LOG LINE');
    expect(await fs.readFile(path.join(outDir, '001_agent.stdout.txt'), 'utf8')).toContain('VERBOSE INTERNAL LOG LINE');
    expect(await fs.readFile(path.join(outDir, '001_agent.response.txt'), 'utf8')).toBe('FINAL_AGENT_RESPONSE');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.steps[0].response_path).toContain('001_agent.response.txt');
  });

  it('captures current yy footer session ids from stderr without parsing cost values', async () => {
    const binDir = path.join(testDir, 'footer-bin');
    await fs.ensureDir(binDir);
    const executablePath = path.join(binDir, 'yy');
    const footerSessionId = '019f441e-2515-7e76-9500-39e6f3ad525a';
    await fs.writeFile(
      executablePath,
      `#!/usr/bin/env sh
if [ "\${1:-}" = "continue-scope" ]; then printf '%s\\n' '{"fullHash":"SCOPE_0123456789ABCDEF","sessionEnvKey":"YYLO_LAST_SESSION_ID_SCOPE_0123456789ABCDEF","settingsEnvKey":"YYLO_LAST_EXECUTION_SETTINGS_SCOPE_0123456789ABCDEF"}'; exit 0; fi
printf 'footer response\\n'
printf 'debug cost: $0.999999 before footer\\n' >&2
printf '🔑 Session ID(s):\\n' >&2
printf '   ${footerSessionId}    cost: $0.158907\\n' >&2
`,
    );
    await fs.chmod(executablePath, 0o755);
    const workflowPath = path.join(testDir, 'footer-session.json');
    const outDir = path.join(testDir, 'footer-session-out');
    await fs.writeJson(workflowPath, {
      name: 'footer-session',
      steps: [{ id: 'footer', command: [executablePath, 'pi', 'prompt'] }],
    });

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none']);

    expect(result.status).toBe(0);
    expect(result.stdout).toContain(`step 1 [footer]: ${footerSessionId}`);
    expect(result.stdout).not.toContain('0.158907');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.steps[0].session_id).toBe(footerSessionId);
  });

  it('keeps capture_session false metadata out of tracked project files and disables handoff', async () => {
    const binDir = path.join(testDir, 'bin');
    const executablePath = path.join(binDir, 'yy');
    await fs.ensureDir(binDir);
    await fs.ensureDir(path.join(testDir, '.juno_task'));
    await fs.writeJson(path.join(testDir, '.juno_task', 'session_history.json'), { version: 1, sessions: [{ id: 'tracked' }] });
    await fs.writeJson(path.join(testDir, '.juno_task', 'session_branches.json'), { version: 1, scopes: { tracked: {} } });
    await fs.writeFile(
      executablePath,
      `#!/usr/bin/env python3
import json, os, pathlib
root = pathlib.Path(os.environ['YYLO_SESSION_METADATA_DIRECTORY'])
root.mkdir(parents=True, exist_ok=True)
(root / 'session_history.json').write_text(json.dumps({'version': 1, 'sessions': [{'id': 'artifact'}]}))
(root / 'session_branches.json').write_text(json.dumps({'version': 1, 'scopes': {'artifact': {}}}))
print('response with session_id=session-must-not-be-captured')
`,
    );
    await fs.chmod(executablePath, 0o755);
    const workflowPath = path.join(testDir, 'no-capture.json');
    const outDir = path.join(testDir, 'no-capture-out');
    await fs.writeJson(workflowPath, {
      name: 'no-capture',
      steps: [{ id: 'agent', command: [executablePath, 'pi', 'prompt'], capture_session: false }],
    });

    const result = runWorkflow([
      '--workflow', workflowPath, '--run-root', testDir, '--out-dir', outDir, '--print-output', 'none',
    ]);

    expect(result.status).toBe(0);
    expect(result.stdout).not.toContain('Session ID(s):');
    expect(result.stdout).not.toContain('persisted for yy cc');
    expect(await fs.readJson(path.join(testDir, '.juno_task', 'session_history.json'))).toEqual({ version: 1, sessions: [{ id: 'tracked' }] });
    expect(await fs.readJson(path.join(testDir, '.juno_task', 'session_branches.json'))).toEqual({ version: 1, scopes: { tracked: {} } });
    expect(await fs.readJson(path.join(outDir, 'session_metadata', 'session_history.json'))).toEqual({ version: 1, sessions: [{ id: 'artifact' }] });
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.steps[0].capture_enabled).toBe(false);
    expect(manifest.steps[0].session_id).toBe('');
    expect(manifest.continue).toBeUndefined();
  });

  it('uses yy rather than dispatching ypl for continue-scope control calls', async () => {
    const { binDir } = await installFakeJunoExecutable(testDir, 'yy');
    const yplPath = path.join(binDir, 'ypl');
    const controlMarker = path.join(testDir, 'ypl-control-dispatch.txt');
    await fs.writeFile(
      yplPath,
      `#!/usr/bin/env sh
if [ "\${1:-}" = "continue-scope" ]; then printf 'unexpected-control-dispatch' > ${JSON.stringify(controlMarker)}; exit 91; fi
exec "$(dirname "$0")/yy" pi --live "$@"
`,
    );
    await fs.chmod(yplPath, 0o755);
    const workflowPath = path.join(testDir, 'ypl-control-boundary.json');
    const outDir = path.join(testDir, 'ypl-control-boundary-out');
    await fs.writeJson(workflowPath, {
      name: 'ypl-control-boundary',
      steps: [{ id: 'agent', command: [yplPath, 'prompt'] }],
    });

    const result = runWorkflow(
      ['--workflow', workflowPath, '--run-root', testDir, '--out-dir', outDir, '--print-output', 'none'],
      undefined,
      { PATH: `${binDir}${path.delimiter}${process.env.PATH ?? ''}` },
    );

    expect(result.status).toBe(0);
    expect(await fs.pathExists(controlMarker)).toBe(false);
    expect(result.stdout).toContain('persisted for yy cc');
  });

  it('prints juno step session ids and persists the last one for yy cc continue', async () => {
    const { executablePath } = await installFakeJunoExecutable(testDir, 'yy');
    await fs.ensureDir(path.join(testDir, '.juno_task'));
    await fs.writeJson(path.join(testDir, '.juno_task', 'config.json'), { envFilePath: '.env.yylo' });
    const workflowPath = path.join(testDir, 'session-summary.json');
    const outDir = path.join(testDir, 'session-summary-out');
    await fs.writeJson(workflowPath, {
      name: 'session-summary',
      steps: [
        { id: 'first', command: [executablePath, 'pi', 'alpha'] },
        { id: 'second', command: [executablePath, 'pi', 'omega'] },
      ],
    });

    const result = runWorkflow(
      ['--workflow', workflowPath, '--run-root', testDir, '--out-dir', outDir, '--print-output', 'none'],
      undefined,
      { YYLO_CONTINUE_SCOPE: 'workflow-test-scope' },
    );

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('Session ID(s):');
    expect(result.stdout).toContain('step 1 [first]: session-alpha');
    expect(result.stdout).toContain('step 2 [second]: session-omega');
    expect(result.stdout).toContain('handoff: step 2 [second] persisted for yy cc');
    expect(result.stdout).toContain('metadata_file: session_continuity.v2.json');
    expect(await fs.pathExists(path.join(testDir, '.env.yylo'))).toBe(false);
  });

  it('captures summary.command sessions and uses summary as the default yy cc handoff', async () => {
    const { executablePath } = await installFakeJunoExecutable(testDir, 'yy');
    await fs.ensureDir(path.join(testDir, '.juno_task'));
    await fs.writeJson(path.join(testDir, '.juno_task', 'config.json'), { envFilePath: '.env.yylo' });
    const workflowPath = path.join(testDir, 'summary-session.json');
    const outDir = path.join(testDir, 'summary-session-out');
    await fs.writeJson(workflowPath, {
      name: 'summary-session',
      steps: [{ id: 'normal', command: [executablePath, 'pi', 'normal'] }],
      summary: { command: [executablePath, 'pi', 'summary'] },
    });

    const result = runWorkflow(
      ['--workflow', workflowPath, '--run-root', testDir, '--out-dir', outDir],
      undefined,
      { YYLO_CONTINUE_SCOPE: 'workflow-summary-session' },
    );

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('captured summary');
    expect(result.stdout).toContain('Session ID(s):');
    expect(result.stdout).toContain('step 1 [normal]: session-normal');
    expect(result.stdout).toContain('summary [summary]: session-summary');
    expect(result.stdout).toContain('handoff: summary [summary] persisted for yy cc');
    expect(result.stdout.indexOf('captured summary')).toBeLessThan(result.stdout.indexOf('Session ID(s):'));
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.summary.session_id).toBe('session-summary');
    expect(manifest.summary.capture_result).toBe('captured summary');
    expect(manifest.continue.step_id).toBe('summary');
    expect(manifest.continue.session_id).toBe('session-summary');
    expect(result.stdout).toContain('metadata_file: session_continuity.v2.json');
    expect(await fs.pathExists(path.join(testDir, '.env.yylo'))).toBe(false);
  });

  it('keeps default yy cc handoff on the last successful agent when summary.command fails with a session', async () => {
    const { executablePath } = await installFakeJunoExecutable(testDir, 'yy');
    await fs.ensureDir(path.join(testDir, '.juno_task'));
    await fs.writeJson(path.join(testDir, '.juno_task', 'config.json'), { envFilePath: '.env.yylo' });
    const workflowPath = path.join(testDir, 'failed-summary-session.json');
    const outDir = path.join(testDir, 'failed-summary-session-out');
    await fs.writeJson(workflowPath, {
      name: 'failed-summary-session',
      steps: [{ id: 'normal', command: [executablePath, 'pi', 'normal'] }],
      summary: { command: [executablePath, 'pi', 'fail'] },
    });

    const result = runWorkflow(
      ['--workflow', workflowPath, '--run-root', testDir, '--out-dir', outDir, '--print-output', 'none'],
      undefined,
      { YYLO_CONTINUE_SCOPE: 'workflow-failed-summary-session' },
    );

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('summary [summary]: session-fail');
    expect(result.stdout).toContain('handoff: step 1 [normal] persisted for yy cc');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.summary.session_id).toBe('session-fail');
    expect(manifest.summary.exit_code).toBe(7);
    expect(manifest.continue.step_id).toBe('normal');
    expect(manifest.continue.session_id).toBe('session-normal');
  });

  it('adopts top-level yy continue snapshots and persists them to the caller scope for yy cc', async () => {
    const { executablePath } = await installFakeTopLevelPersistingJuno(testDir, 'yy');
    await fs.ensureDir(path.join(testDir, '.juno_task'));
    await fs.writeJson(path.join(testDir, '.juno_task', 'config.json'), { envFilePath: '.env.yylo' });
    const workflowPath = path.join(testDir, 'top-level-session.json');
    const outDir = path.join(testDir, 'top-level-session-out');
    await fs.writeJson(workflowPath, {
      name: 'top-level-session',
      steps: [{ id: 'agent', command: [executablePath, 'pi', 'child'] }],
    });

    const result = runWorkflow([
      '--workflow',
      workflowPath,
      '--run-root',
      testDir,
      '--out-dir',
      outDir,
      '--print-output',
      'none',
    ]);

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('step 1 [agent]: session-child');
    expect(result.stdout).toContain('metadata_file: session_continuity.v2.json');
    expect(await fs.pathExists(path.join(testDir, '.env.yylo'))).toBe(false);
    const manifestPath = path.join(outDir, 'manifest.json');
    expect(await fs.pathExists(manifestPath)).toBe(true);
    const manifest = await fs.readJson(manifestPath);
    expect(manifest.steps[0].session_id).toBe('session-child');
    expect(manifest.continue.step_id).toBe('agent');
    const doctor = runWorkflow(['doctor', outDir]);
    expect(doctor.status).toBe(0);
    expect(doctor.stdout).toContain('Workflow doctor');
  });

  it('supports continue_from_step to persist a non-last agent session', async () => {
    const { executablePath } = await installFakeJunoExecutable(testDir, 'yy');
    await fs.ensureDir(path.join(testDir, '.juno_task'));
    await fs.writeJson(path.join(testDir, '.juno_task', 'config.json'), { envFilePath: '.env.yylo' });
    const workflowPath = path.join(testDir, 'continue-from-step.json');
    const outDir = path.join(testDir, 'continue-from-step-out');
    await fs.writeJson(workflowPath, {
      name: 'continue-from-step',
      continue_from_step: 'first',
      steps: [
        { id: 'first', command: [executablePath, 'pi', 'alpha'] },
        { id: 'second', command: [executablePath, 'pi', 'omega'] },
      ],
    });

    const result = runWorkflow([
      '--workflow',
      workflowPath,
      '--run-root',
      testDir,
      '--out-dir',
      outDir,
      '--print-output',
      'none',
      ], undefined, { YYLO_CONTINUE_SCOPE: 'workflow-continue-from-step' });

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('metadata_file: session_continuity.v2.json');
    expect(await fs.pathExists(path.join(testDir, '.env.yylo'))).toBe(false);
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.continue.step_id).toBe('first');
    expect(manifest.continue.session_id).toBe('session-alpha');
  });

  it('persists continue settings for option-style yy -s/--subagent commands', async () => {
    const { executablePath } = await installFakeJunoExecutable(testDir, 'yy');
    await fs.ensureDir(path.join(testDir, '.juno_task'));
    await fs.writeJson(path.join(testDir, '.juno_task', 'config.json'), { envFilePath: '.env.yylo' });
    const workflowPath = path.join(testDir, 'option-style-subagent.json');
    const outDir = path.join(testDir, 'option-style-subagent-out');
    await fs.writeJson(workflowPath, {
      name: 'option-style-subagent',
      continue_from_step: 'first',
      steps: [
        { id: 'first', command: [executablePath, '-s', 'pi', 'alpha'] },
        { id: 'second', command: [executablePath, '--subagent', 'pi', 'omega'] },
      ],
    });

    const result = runWorkflow(
      ['--workflow', workflowPath, '--run-root', testDir, '--out-dir', outDir, '--print-output', 'none'],
      undefined,
      { YYLO_CONTINUE_SCOPE: 'workflow-option-style-subagent' },
    );

    expect(result.status).toBe(0);
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.continue.step_id).toBe('first');
    expect(manifest.continue.session_id).toBe('session-alpha');
    expect(manifest.continue.scope_hash).toMatch(/^SCOPE_[A-F0-9]{16}$/);
    expect(result.stdout).toContain('metadata_file: session_continuity.v2.json');
    expect(await fs.pathExists(path.join(testDir, '.env.yylo'))).toBe(false);
  });

  it('supports continue_from_step summary override when summary is not the default last successful session', async () => {
    const { executablePath } = await installFakeJunoExecutable(testDir, 'yy');
    await fs.ensureDir(path.join(testDir, '.juno_task'));
    await fs.writeJson(path.join(testDir, '.juno_task', 'config.json'), { envFilePath: '.env.yylo' });
    const workflowPath = path.join(testDir, 'continue-from-summary.json');
    const outDir = path.join(testDir, 'continue-from-summary-out');
    await fs.writeJson(workflowPath, {
      name: 'continue-from-summary',
      continue_from_step: 'summary',
      steps: [{ id: 'normal', command: [executablePath, 'pi', 'normal'] }],
      summary: { command: [executablePath, 'pi', 'fail'] },
    });

    const result = runWorkflow(
      ['--workflow', workflowPath, '--run-root', testDir, '--out-dir', outDir, '--print-output', 'none'],
      undefined,
      { YYLO_CONTINUE_SCOPE: 'workflow-continue-from-summary' },
    );

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('handoff: summary [summary] persisted for yy cc');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.continue.step_id).toBe('summary');
    expect(manifest.continue.session_id).toBe('session-fail');
  });

  it('fails strict continue_from_step summary when summary has no session id', async () => {
    const workflowPath = path.join(testDir, 'continue-from-summary-missing-session.json');
    await fs.writeJson(workflowPath, {
      name: 'continue-from-summary-missing-session',
      continue_from_step: 'summary',
      steps: [{ id: 'plain', command: 'printf done' }],
      summary: { command: 'printf summary' },
    });

    const result = runWorkflow([
      '--workflow', workflowPath, '--print-output', 'none',
      '--run-root', testDir, '--out-dir', path.join(testDir, 'continue-from-summary-missing-out'),
    ]);

    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain("continue_from_step 'summary' did not match an executed Juno invocation with a session_id");
  });

  it('fails strict continue_from_step when the selected step has no session id', async () => {
    const { executablePath } = await installFakeJunoExecutable(testDir, 'yy');
    const workflowPath = path.join(testDir, 'continue-from-missing-session.json');
    await fs.writeJson(workflowPath, {
      name: 'continue-from-missing-session',
      continue_from_step: 'plain',
      steps: [
        { id: 'agent', command: [executablePath, 'pi', 'alpha'] },
        { id: 'plain', command: 'printf done' },
      ],
    });

    const result = runWorkflow([
      '--workflow', workflowPath, '--print-output', 'none',
      '--run-root', testDir, '--out-dir', path.join(testDir, 'continue-from-missing-out'),
    ]);

    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain("continue_from_step 'plain' selected step 2 [plain], but it did not produce a session_id");
  });

  it('validates print-output all once and preserves primary failure output', async () => {
    const workflowPath = path.join(testDir, 'print-all.json');
    const outDir = path.join(testDir, 'print-all-out');
    await fs.writeJson(workflowPath, {
      name: 'print-all',
      steps: [
        { id: 'first', command: "printf 'first-output'" },
        { id: 'failed', fail_workflow: true, command: "printf 'failure-output'; exit 7" },
      ],
    });

    const result = runWorkflow([
      '--workflow', workflowPath, '--out-dir', outDir,
      '--print-output', 'all', '--no-print-step-stdout',
    ]);

    expect(result.status).toBe(7);
    expect(result.stdout).toContain('first-output\nfailure-output');
    expect(result.stderr).not.toContain('unknown --print-output step: all');
    const invalid = runWorkflow([
      '--workflow', workflowPath, '--out-dir', path.join(testDir, 'invalid-print'),
      '--print-output', 'missing',
    ]);
    expect(invalid.status).not.toBe(0);
    expect(invalid.stderr).toContain('unknown --print-output step: missing');
    expect(await fs.pathExists(path.join(testDir, 'invalid-print'))).toBe(false);
  });

  it('auto-detects argv juno commands, reads capture JSON, and exposes session templates', async () => {
    const { executablePath } = await installFakeJunoExecutable(testDir, 'yy');
    const workflowPath = path.join(testDir, 'argv-capture.json');
    const outDir = path.join(testDir, 'argv-capture-out');
    await fs.writeJson(workflowPath, {
      name: 'argv-capture',
      steps: [
        { id: 'first', command: [executablePath, 'pi', 'alpha'] },
        { id: 'resume', command: "printf 'resume={{ steps.first.session_id }} result={{ steps.first.capture_result }} response={{ steps.first.response }}'" },
      ],
    });

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'resume']);

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('resume=session-alpha result=captured alpha response=captured alpha');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.steps[0].capture_enabled).toBe(true);
    expect(manifest.steps[0].session_id).toBe('session-alpha');
    expect(manifest.steps[0].capture_result).toBe('captured alpha');
    expect(await fs.pathExists(manifest.steps[0].capture_json)).toBe(true);
  });

  it('auto-detects shell-string juno commands and injects capture env only for that step', async () => {
    const { executablePath } = await installFakeJunoExecutable(testDir, 'yy');
    const workflowPath = path.join(testDir, 'string-capture.json');
    const outDir = path.join(testDir, 'string-capture-out');
    await fs.writeJson(workflowPath, {
      name: 'string-capture',
      steps: [
        { id: 'first', command: `${executablePath} pi beta` },
        { id: 'plain', command: 'printf "tool=${JUNO_TOOL_ID-unset} capture=${JUNO_SUBAGENT_CAPTURE_PATH-unset}"' },
      ],
    });

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'plain']);

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('tool=unset capture=unset');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.steps[0].capture_enabled).toBe(true);
    expect(manifest.steps[0].session_id).toBe('session-beta');
    expect(manifest.steps[1].capture_enabled).toBe(false);
  });

  it('honors capture_session false for juno commands', async () => {
    const { executablePath } = await installFakeJunoExecutable(testDir, 'yy');
    const workflowPath = path.join(testDir, 'capture-disabled.json');
    const outDir = path.join(testDir, 'capture-disabled-out');
    await fs.writeJson(workflowPath, {
      name: 'capture-disabled',
      steps: [{ id: 'first', capture_session: false, command: [executablePath, 'pi', 'gamma'] }],
    });

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'first']);

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('tool=unset capture=unset');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.steps[0].capture_enabled).toBe(false);
    expect(manifest.steps[0].capture_json).toBe('');
  });

  it('records invalid capture JSON as a warning without failing the workflow', async () => {
    const { executablePath } = await installFakeJunoExecutable(testDir, 'yy');
    const workflowPath = path.join(testDir, 'invalid-capture.json');
    const outDir = path.join(testDir, 'invalid-capture-out');
    await fs.writeJson(workflowPath, {
      name: 'invalid-capture',
      steps: [{ id: 'bad_capture', command: [executablePath, 'pi', 'invalid'] }],
    });

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--final-output', 'none']);

    expect(result.status).toBe(0);
    expect(result.stderr).toContain('warning: invalid capture JSON');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.status).toBe('success');
    expect(manifest.steps[0].capture_enabled).toBe(true);
    expect(manifest.steps[0].capture_warning).toContain('invalid capture JSON');
    expect(manifest.steps[0].session_id).toBe('');
  });

  it('reconciles candidate changed paths after writing an in-worktree receipt', async () => {
    const worktree = path.join(testDir, 'composition-worktree');
    const workflowPath = path.join(testDir, 'candidate-composition.json');
    const outDir = path.join(testDir, 'candidate-composition-out');
    await fs.ensureDir(worktree);
    for (const args of [
      ['init'], ['config', 'user.email', 'fixture@example.com'], ['config', 'user.name', 'Fixture'],
    ]) {
      expect(spawnSync('git', args, { cwd: worktree }).status).toBe(0);
    }
    await fs.writeFile(path.join(worktree, 'baseline.txt'), 'baseline\n');
    expect(spawnSync('git', ['add', 'baseline.txt'], { cwd: worktree }).status).toBe(0);
    expect(spawnSync('git', ['commit', '-m', 'baseline'], { cwd: worktree }).status).toBe(0);

    const manifestPath = path.join(testDir, 'composition-manifest.json');
    await fs.writeJson(manifestPath, { candidate_receipt_path: 'receipts/candidate.json' });
    const producer = [
      'import json, pathlib',
      "pathlib.Path('product.txt').write_text('product\\n')",
      "p=pathlib.Path('receipts/candidate.json')",
      'p.parent.mkdir(parents=True, exist_ok=True)',
      "p.write_text(json.dumps({'schema_version':'fixture.v1','changed_paths':['product.txt']}))",
    ].join('; ');
    const gate = [
      'import json, pathlib, subprocess',
      "p=pathlib.Path('receipts/candidate.json')",
      'payload=json.loads(p.read_text())',
      "tracked=subprocess.run(['git','diff','--name-only','--relative','HEAD','--'],check=True,text=True,capture_output=True).stdout.splitlines()",
      "untracked=subprocess.run(['git','ls-files','--others','--exclude-standard'],check=True,text=True,capture_output=True).stdout.splitlines()",
      "assert payload['changed_paths'] == sorted(set(tracked + untracked))",
      "assert 'receipts/candidate.json' in payload['changed_paths']",
    ].join('; ');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'candidate-composition',
      vars: { route_manifest: manifestPath },
      steps: [
        {
          id: 'compose_candidate', capture_session: false, fail_workflow: true,
          command: ['python3', '-c', producer],
          candidate_composition: {
            manifest_path: '{{ route_manifest }}',
            receipt_path_field: 'candidate_receipt_path',
            changed_paths_field: 'changed_paths',
          },
        },
        { id: 'verify_bounded_outputs', capture_session: false, fail_workflow: true, command: ['python3', '-c', gate] },
      ],
    });

    const result = runWorkflow([
      '--workflow', workflowPath, '--project-root', worktree,
      '--out-dir', outDir, '--print-output', 'none',
    ]);
    expect(result.status, `${result.stdout}\n${result.stderr}`).toBe(0);
    const candidate = await fs.readJson(path.join(worktree, 'receipts', 'candidate.json'));
    expect(candidate.changed_paths).toEqual(['product.txt', 'receipts/candidate.json']);
    const evidence = await fs.readJson(path.join(outDir, 'steps', 'compose_candidate', 'candidate_composition.json'));
    expect(evidence.receipt_included).toBe(true);
    expect(evidence.replaced_changed_paths).toEqual(['product.txt']);

    const resumed = runWorkflow([
      '--workflow', workflowPath, '--project-root', worktree,
      '--out-dir', outDir, '--from-step', 'verify_bounded_outputs', '--print-output', 'none',
    ]);
    expect(resumed.status, `${resumed.stdout}\n${resumed.stderr}`).toBe(0);
  });

  it('rejects incomplete candidate composition contracts during lint', async () => {
    const workflowPath = path.join(testDir, 'bad-candidate-composition.json');
    await fs.writeJson(workflowPath, {
      workflow_id: 'bad-candidate-composition',
      steps: [{
        id: 'compose', command: ['true'],
        candidate_composition: { manifest_path: 'manifest.json', receipt_path_field: 'candidate_receipt_path' },
      }],
    });
    const result = runWorkflow(['lint', '--workflow', workflowPath, '--json']);
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain('candidate_composition must contain exactly');
  });

  it('resumes only from immutable workflow and receipt evidence', async () => {
    const workflowPath = path.join(testDir, 'receipt-resume.json');
    const outDir = path.join(testDir, 'receipt-resume-out');
    const receiptPath = path.join(testDir, 'producer-receipt.json');
    const markerPath = path.join(testDir, 'mutation-count.txt');
    const producerCode = [
      'import json, os, pathlib',
      `marker = pathlib.Path(${JSON.stringify(markerPath)})`,
      "marker.write_text(marker.read_text() + 'x' if marker.exists() else 'x')",
      `pathlib.Path(${JSON.stringify(receiptPath)}).write_text(json.dumps({` +
        "'schema_version':'fixture.v1','producer_step_digest':os.environ['JUNO_WORKFLOW_STEP_DIGEST'],'outcome':'completed'}))",
    ].join('; ');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'receipt-resume',
      receipts: [
        {
          id: 'producer_receipt',
          producer: 'producer',
          path: receiptPath,
          schema_version: 'fixture.v1',
          required_fields: ['producer_step_digest', 'outcome'],
          expected_fields: { outcome: 'completed' },
        },
      ],
      terminal_gate: 'gate',
      steps: [
        { id: 'producer', command: ['python3', '-c', producerCode] },
        { id: 'gate', requires_receipts: ['producer_receipt'], command: ['true'] },
      ],
    });

    const first = runWorkflow([
      '--workflow', workflowPath,
      '--project-root', testDir,
      '--out-dir', outDir,
      '--print-output', 'none',
    ]);
    expect(first.status).toBe(0);
    const resumed = runWorkflow([
      '--workflow', workflowPath,
      '--project-root', testDir,
      '--out-dir', outDir,
      '--from-step', 'gate',
      '--print-output', 'none',
    ]);
    expect(resumed.status).toBe(0);
    expect(await fs.readFile(markerPath, 'utf8')).toBe('x');
    let manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.steps[0].status).toBe('reused_verified');
    expect((await fs.readJson(path.join(outDir, 'run_contract.json'))).attempts).toHaveLength(2);

    const receipt = await fs.readJson(receiptPath);
    receipt.note = 'tampered';
    await fs.writeJson(receiptPath, receipt);
    const rejected = runWorkflow([
      '--workflow', workflowPath,
      '--project-root', testDir,
      '--out-dir', outDir,
      '--from-step', 'gate',
      '--print-output', 'none',
    ]);
    expect(rejected.status).not.toBe(0);
    expect(rejected.stderr).toContain('artifact_sha256');
    manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.steps[0].status).toBe('reused_verified');
    expect(await fs.readFile(markerPath, 'utf8')).toBe('x');
  });

  it('selectively amends from a verified prefix without re-executing its producer', async () => {
    const workflowPath = path.join(testDir, 'selective-amendment.json');
    const parentOut = path.join(testDir, 'selective-parent');
    const amendedOut = path.join(testDir, 'selective-amended');
    const markerPath = path.join(testDir, 'producer-count.txt');
    const gateMarkerPath = path.join(testDir, 'gate-count.txt');
    const producerCode = [
      'import json, os, pathlib',
      `marker = pathlib.Path(${JSON.stringify(markerPath)})`,
      "marker.write_text(marker.read_text() + 'x' if marker.exists() else 'x')",
      "receipt = pathlib.Path(os.environ['JUNO_WORKFLOW_RECEIPT_PRODUCER_RECEIPT'])",
      "receipt.parent.mkdir(parents=True, exist_ok=True)",
      "receipt.write_text(json.dumps({'schema_version':'fixture.v1','producer_step_digest':os.environ['JUNO_WORKFLOW_STEP_DIGEST'],'outcome':'completed'}))",
    ].join('; ');
    const workflow = (receiptDir: string) => ({
      schema_version: 1,
      workflow_id: 'selective-amendment',
      amendment_mode: 'harness_only_validation',
      receipts: [{
        id: 'producer_receipt',
        producer: 'producer',
        path: `{{ out_dir }}/${receiptDir}/producer.json`,
        schema_version: 'fixture.v1',
        required_fields: ['producer_step_digest', 'outcome'],
        expected_fields: { outcome: 'completed' },
      }],
      terminal_gate: 'gate',
      steps: [
        { id: 'producer', command: ['python3', '-c', producerCode] },
        {
          id: 'gate',
          requires_receipts: ['producer_receipt'],
          command: ['python3', '-c', `from pathlib import Path; Path(${JSON.stringify(gateMarkerPath)}).write_text('gate')`],
        },
      ],
    });
    await fs.writeJson(workflowPath, workflow('candidates'));

    const first = runWorkflow([
      '--workflow', workflowPath, '--project-root', testDir,
      '--out-dir', parentOut, '--print-output', 'none',
    ]);
    expect(first.status).toBe(0);
    await fs.remove(gateMarkerPath);
    await fs.writeJson(workflowPath, workflow('reviews'));

    const amended = runWorkflow([
      '--workflow', workflowPath, '--project-root', testDir,
      '--out-dir', amendedOut, '--amends-run', parentOut,
      '--from-step', 'gate', '--print-output', 'none',
    ]);
    expect(amended.status).toBe(0);
    expect(amended.stdout).toContain('Revalidate/reuse: producer');
    expect(amended.stdout).toContain('Execute: gate');
    expect(await fs.readFile(markerPath, 'utf8')).toBe('x');
    expect(await fs.readFile(gateMarkerPath, 'utf8')).toBe('gate');
    expect(await fs.pathExists(path.join(amendedOut, 'reviews', 'producer.json'))).toBe(true);
    const manifest = await fs.readJson(path.join(amendedOut, 'manifest.json'));
    expect(manifest.steps[0].status).toBe('amendment_revalidated');
    expect(manifest.amendment_plan.reused_steps).toEqual(['producer']);
    const contract = await fs.readJson(path.join(amendedOut, 'run_contract.json'));
    expect(contract.completed_steps.producer.receipts.producer_receipt.lineage).toBe('amendment_revalidated');
    expect(contract.completed_steps.producer.receipts.producer_receipt.inherited_from).toContain('/candidates/producer.json');
  });

  it('rejects tampered amendment evidence before executing the requested suffix', async () => {
    const workflowPath = path.join(testDir, 'tampered-amendment.json');
    const parentOut = path.join(testDir, 'tampered-parent');
    const amendedOut = path.join(testDir, 'tampered-amended');
    const receiptPath = path.join(parentOut, 'receipt.json');
    const suffixMarker = path.join(testDir, 'suffix-ran.txt');
    const producerCode = [
      'import json, os, pathlib',
      "path = pathlib.Path(os.environ['JUNO_WORKFLOW_RECEIPT_EVIDENCE'])",
      "path.parent.mkdir(parents=True, exist_ok=True)",
      "path.write_text(json.dumps({'schema_version':'fixture.v1','producer_step_digest':os.environ['JUNO_WORKFLOW_STEP_DIGEST']}))",
    ].join('; ');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'tampered-amendment',
      amendment_mode: 'harness_only_validation',
      receipts: [{
        id: 'evidence', producer: 'producer', path: '{{ out_dir }}/receipt.json',
        schema_version: 'fixture.v1', required_fields: ['producer_step_digest'],
      }],
      steps: [
        { id: 'producer', command: ['python3', '-c', producerCode] },
        { id: 'suffix', requires_receipts: ['evidence'], command: ['bash', '-lc', `touch ${JSON.stringify(suffixMarker)}`] },
      ],
    });
    expect(runWorkflow(['--workflow', workflowPath, '--out-dir', parentOut, '--print-output', 'none']).status).toBe(0);
    await fs.remove(suffixMarker);
    const tampered = await fs.readJson(receiptPath);
    tampered.changed = true;
    await fs.writeJson(receiptPath, tampered);

    const rejected = runWorkflow([
      '--workflow', workflowPath, '--out-dir', amendedOut,
      '--amends-run', parentOut, '--from-step', 'suffix', '--print-output', 'none',
    ]);
    expect(rejected.status).not.toBe(0);
    expect(rejected.stderr).toContain('artifact_sha256');
    expect(await fs.pathExists(suffixMarker)).toBe(false);
  });

  it('rejects a tampered hash-bound parent manifest before selective amendment dispatch', async () => {
    const workflowPath = path.join(testDir, 'manifest-tamper.json');
    const parentOut = path.join(testDir, 'manifest-parent');
    const amendedOut = path.join(testDir, 'manifest-amended');
    const suffixMarker = path.join(testDir, 'manifest-suffix.txt');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'manifest-tamper',
      amendment_mode: 'harness_only_validation',
      steps: [
        { id: 'producer', command: ['bash', '-lc', 'printf trusted'] },
        { id: 'suffix', command: ['bash', '-lc', `touch ${JSON.stringify(suffixMarker)}`] },
      ],
    });
    expect(runWorkflow(['--workflow', workflowPath, '--out-dir', parentOut, '--print-output', 'none']).status).toBe(0);
    await fs.remove(suffixMarker);
    const parentContract = await fs.readJson(path.join(parentOut, 'run_contract.json'));
    const manifestPath = parentContract.attempts.at(-1).manifest;
    const manifest = await fs.readJson(manifestPath);
    manifest.steps[0].response = 'tampered';
    await fs.writeJson(manifestPath, manifest);

    const rejected = runWorkflow([
      '--workflow', workflowPath, '--out-dir', amendedOut,
      '--amends-run', parentOut, '--from-step', 'suffix', '--print-output', 'none',
    ]);
    expect(rejected.status).not.toBe(0);
    expect(rejected.stderr).toContain('manifest hash mismatch');
    expect(await fs.pathExists(suffixMarker)).toBe(false);
  });

  it('fails closed when the newest parent attempt manifest is missing', async () => {
    const workflowPath = path.join(testDir, 'missing-newest-manifest.json');
    const parentOut = path.join(testDir, 'missing-newest-parent');
    const amendedOut = path.join(testDir, 'missing-newest-amended');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'missing-newest-manifest',
      amendment_mode: 'harness_only_validation',
      steps: [
        { id: 'prefix', command: ['bash', '-lc', 'printf prefix'] },
        { id: 'suffix', command: ['bash', '-lc', 'printf suffix'] },
      ],
    });
    expect(runWorkflow(['--workflow', workflowPath, '--out-dir', parentOut, '--print-output', 'none']).status).toBe(0);
    const contractPath = path.join(parentOut, 'run_contract.json');
    const contract = await fs.readJson(contractPath);
    contract.attempts.push({
      attempt_id: 'newer', status: 'failed', semantic_status: 'failed',
      manifest: path.join(parentOut, 'attempts', 'newer', 'manifest.json'),
      manifest_sha256: '0'.repeat(64),
    });
    await fs.writeJson(contractPath, contract);

    const rejected = runWorkflow([
      '--workflow', workflowPath, '--out-dir', amendedOut,
      '--amends-run', parentOut, '--from-step', 'suffix', '--print-output', 'none',
    ]);
    expect(rejected.status).not.toBe(0);
    expect(rejected.stderr).toContain('newest workflow attempt manifest is missing');
  });

  it('binds the newest parent attempt ID to its hash-bound manifest', async () => {
    const workflowPath = path.join(testDir, 'attempt-identity.json');
    const parentOut = path.join(testDir, 'attempt-identity-parent');
    const amendedOut = path.join(testDir, 'attempt-identity-amended');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'attempt-identity',
      amendment_mode: 'harness_only_validation',
      steps: [
        { id: 'prefix', command: ['bash', '-lc', 'printf prefix'] },
        { id: 'suffix', command: ['bash', '-lc', 'printf suffix'] },
      ],
    });
    expect(runWorkflow(['--workflow', workflowPath, '--out-dir', parentOut, '--print-output', 'none']).status).toBe(0);
    const contractPath = path.join(parentOut, 'run_contract.json');
    const contract = await fs.readJson(contractPath);
    contract.attempts.at(-1).attempt_id = 'forged-attempt';
    await fs.writeJson(contractPath, contract);

    const rejected = runWorkflow([
      '--workflow', workflowPath, '--out-dir', amendedOut,
      '--amends-run', parentOut, '--from-step', 'suffix', '--print-output', 'none',
    ]);
    expect(rejected.status).not.toBe(0);
    expect(rejected.stderr).toContain('attempt identity does not match');
  });

  it('rejects cross-attempt mixing between manifest context and completed evidence', async () => {
    const workflowPath = path.join(testDir, 'attempt-mismatch.json');
    const parentOut = path.join(testDir, 'attempt-mismatch-parent');
    const amendedOut = path.join(testDir, 'attempt-mismatch-amended');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'attempt-mismatch',
      amendment_mode: 'harness_only_validation',
      steps: [
        { id: 'prefix', command: ['bash', '-lc', 'printf prefix'] },
        { id: 'suffix', command: ['bash', '-lc', 'printf suffix'] },
      ],
    });
    expect(runWorkflow(['--workflow', workflowPath, '--out-dir', parentOut, '--print-output', 'none']).status).toBe(0);
    const contractPath = path.join(parentOut, 'run_contract.json');
    const contract = await fs.readJson(contractPath);
    const attempt = contract.attempts.at(-1);
    const manifest = await fs.readJson(attempt.manifest);
    manifest.steps[0].status = 'reused_verified';
    manifest.steps[0].reused_from_attempt = 'different-attempt';
    await fs.writeJson(attempt.manifest, manifest);
    attempt.manifest_sha256 = createHash('sha256').update(await fs.readFile(attempt.manifest)).digest('hex');
    await fs.writeJson(contractPath, contract);

    const rejected = runWorkflow([
      '--workflow', workflowPath, '--out-dir', amendedOut,
      '--amends-run', parentOut, '--from-step', 'suffix', '--print-output', 'none',
    ]);
    expect(rejected.status).not.toBe(0);
    expect(rejected.stderr).toContain('completed evidence attempt mismatch');
  });

  it('rejects removed or weakened receipt contracts for skipped producers', async () => {
    const workflowPath = path.join(testDir, 'removed-prefix-receipt.json');
    const parentOut = path.join(testDir, 'removed-prefix-parent');
    const amendedOut = path.join(testDir, 'removed-prefix-amended');
    const producerCode = [
      'import json, os, pathlib',
      "path = pathlib.Path(os.environ['JUNO_WORKFLOW_RECEIPT_EVIDENCE'])",
      "path.parent.mkdir(parents=True, exist_ok=True)",
      "path.write_text(json.dumps({'schema_version':'fixture.v1','producer_step_digest':os.environ['JUNO_WORKFLOW_STEP_DIGEST']}))",
    ].join('; ');
    const steps = [
      { id: 'producer', command: ['python3', '-c', producerCode] },
      { id: 'suffix', command: ['bash', '-lc', 'printf suffix'] },
    ];
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'removed-prefix-receipt',
      amendment_mode: 'harness_only_validation',
      receipts: [{
        id: 'evidence', producer: 'producer', path: '{{ out_dir }}/evidence.json',
        schema_version: 'fixture.v1', required_fields: ['producer_step_digest'],
      }],
      steps,
    });
    expect(runWorkflow(['--workflow', workflowPath, '--out-dir', parentOut, '--print-output', 'none']).status).toBe(0);
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'removed-prefix-receipt',
      amendment_mode: 'harness_only_validation',
      steps,
    });

    const rejected = runWorkflow([
      '--workflow', workflowPath, '--out-dir', amendedOut,
      '--amends-run', parentOut, '--from-step', 'suffix', '--print-output', 'none',
    ]);
    expect(rejected.status).not.toBe(0);
    expect(rejected.stderr).toContain('producer receipt set changed');
  });

  it('rejects newly declared skipped-producer receipts without a parent hash anchor', async () => {
    const workflowPath = path.join(testDir, 'missing-anchor.json');
    const parentOut = path.join(testDir, 'missing-anchor-parent');
    const amendedOut = path.join(testDir, 'missing-anchor-amended');
    const suffixMarker = path.join(testDir, 'missing-anchor-suffix.txt');
    const baseWorkflow = {
      schema_version: 1,
      workflow_id: 'missing-anchor',
      amendment_mode: 'harness_only_validation',
      steps: [
        { id: 'producer', command: ['bash', '-lc', 'printf complete'] },
        { id: 'suffix', command: ['bash', '-lc', `touch ${JSON.stringify(suffixMarker)}`] },
      ],
    };
    await fs.writeJson(workflowPath, baseWorkflow);
    expect(runWorkflow(['--workflow', workflowPath, '--out-dir', parentOut, '--print-output', 'none']).status).toBe(0);
    await fs.remove(suffixMarker);
    await fs.writeJson(workflowPath, {
      ...baseWorkflow,
      receipts: [{
        id: 'new_evidence', producer: 'producer', path: '{{ out_dir }}/new.json',
        schema_version: 'fixture.v1', required_fields: ['producer_step_digest'],
      }],
    });

    const rejected = runWorkflow([
      '--workflow', workflowPath, '--out-dir', amendedOut,
      '--amends-run', parentOut, '--from-step', 'suffix', '--print-output', 'none',
    ]);
    expect(rejected.status).not.toBe(0);
    expect(rejected.stderr).toContain('producer receipt set changed');
    expect(await fs.pathExists(suffixMarker)).toBe(false);
  });

  it('reuses hash-bound dynamic prefix commands rendered from predecessor responses', async () => {
    const workflowPath = path.join(testDir, 'dynamic-prefix.json');
    const parentOut = path.join(testDir, 'dynamic-prefix-parent');
    const amendedOut = path.join(testDir, 'dynamic-prefix-amended');
    const secondMarker = path.join(testDir, 'dynamic-second.txt');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'dynamic-prefix',
      amendment_mode: 'harness_only_validation',
      steps: [
        { id: 'first', command: ['bash', '-lc', 'printf alpha'] },
        { id: 'second', command: ['bash', '-lc', `printf '{{ steps.first.response }}' > ${JSON.stringify(secondMarker)}`] },
        { id: 'suffix', command: ['bash', '-lc', 'printf suffix'] },
      ],
    });
    expect(runWorkflow(['--workflow', workflowPath, '--out-dir', parentOut, '--print-output', 'none']).status).toBe(0);
    expect(await fs.readFile(secondMarker, 'utf8')).toBe('alpha');
    await fs.remove(secondMarker);

    const amended = runWorkflow([
      '--workflow', workflowPath, '--out-dir', amendedOut,
      '--amends-run', parentOut, '--from-step', 'suffix', '--print-output', 'none',
    ]);
    expect(amended.status).toBe(0);
    expect(await fs.pathExists(secondMarker)).toBe(false);
    const manifest = await fs.readJson(path.join(amendedOut, 'manifest.json'));
    expect(manifest.steps.slice(0, 2).map((step: { status: string }) => step.status)).toEqual([
      'amendment_revalidated', 'amendment_revalidated',
    ]);
  });

  it('preserves the original completed producer attempt across amendment generations', async () => {
    const workflowPath = path.join(testDir, 'multi-generation-lineage.json');
    const parentOut = path.join(testDir, 'lineage-parent');
    const firstAmendmentOut = path.join(testDir, 'lineage-amendment-one');
    const secondAmendmentOut = path.join(testDir, 'lineage-amendment-two');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'multi-generation-lineage',
      amendment_mode: 'harness_only_validation',
      steps: [
        { id: 'prefix', command: ['bash', '-lc', 'printf prefix'] },
        { id: 'suffix', command: ['bash', '-lc', 'printf suffix'] },
      ],
    });
    expect(runWorkflow(['--workflow', workflowPath, '--out-dir', parentOut, '--print-output', 'none']).status).toBe(0);
    const originalContract = await fs.readJson(path.join(parentOut, 'run_contract.json'));
    const originalAttempt = originalContract.completed_steps.prefix.attempt_id;
    expect(runWorkflow([
      '--workflow', workflowPath, '--out-dir', firstAmendmentOut,
      '--amends-run', parentOut, '--from-step', 'suffix', '--print-output', 'none',
    ]).status).toBe(0);
    expect(runWorkflow([
      '--workflow', workflowPath, '--out-dir', secondAmendmentOut,
      '--amends-run', firstAmendmentOut, '--from-step', 'suffix', '--print-output', 'none',
    ]).status).toBe(0);
    const secondContract = await fs.readJson(path.join(secondAmendmentOut, 'run_contract.json'));
    expect(secondContract.completed_steps.prefix.attempt_id).toBe(originalAttempt);
  });

  it('keeps full amendments runnable when the parent has only a run contract', async () => {
    const workflowPath = path.join(testDir, 'full-amendment.json');
    const parentOut = path.join(testDir, 'contract-only-parent');
    const amendedOut = path.join(testDir, 'full-amendment-out');
    await fs.ensureDir(parentOut);
    await fs.writeJson(path.join(parentOut, 'run_contract.json'), {
      schema_version: 'juno_workflow_run_contract.v1',
      workflow_id: 'full-amendment',
      attempts: [],
    });
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'full-amendment',
      amendment_mode: 'harness_only_validation',
      steps: [{ id: 'only', command: ['bash', '-lc', 'printf amended'] }],
    });
    const result = runWorkflow([
      '--workflow', workflowPath, '--out-dir', amendedOut,
      '--amends-run', parentOut, '--print-output', 'none',
    ]);
    expect(result.status).toBe(0);
    expect((await fs.readJson(path.join(amendedOut, 'run_contract.json'))).amendment_of.manifest).toBeUndefined();
  });

  it('lints contradictory hardcoded receipt paths and points to the canonical receipt context', async () => {
    const workflowPath = path.join(testDir, 'receipt-path-conflict.json');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'receipt_path_conflict',
      receipts: [{
        id: 'verified', producer: 'producer',
        path: '{{ out_dir }}/candidates/verified.json',
        schema_version: 'fixture.v1', required_fields: ['producer_step_digest'],
      }],
      steps: [{ id: 'producer', command: "printf 'write {{ out_dir }}/reviews/verified.json'" }],
    });
    const result = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(result.status).not.toBe(0);
    expect(result.stdout).toContain('hardcodes receipt path');
    expect(result.stdout).toContain('{{ receipts.verified.path }}');
  });

  it('rejects receipt IDs that cannot map unambiguously to templates and environment variables', async () => {
    const workflowPath = path.join(testDir, 'ambiguous-receipt-id.json');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'ambiguous_receipt_id',
      receipts: [{
        id: 'review.pass', producer: 'producer', path: 'review.json',
        schema_version: 'fixture.v1', required_fields: ['producer_step_digest'],
      }],
      steps: [{ id: 'producer', command: 'true' }],
    });
    const result = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain('use lowercase letters, numbers, and underscores');
  });

  it('does not flag unrelated JSON outputs that merely share a receipt basename', async () => {
    const workflowPath = path.join(testDir, 'receipt-path-non-conflict.json');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'receipt_path_non_conflict',
      receipts: [{
        id: 'candidate_report', producer: 'producer',
        path: '{{ out_dir }}/candidates/report.json',
        schema_version: 'fixture.v1', required_fields: ['producer_step_digest'],
      }],
      steps: [{
        id: 'producer',
        command: "printf 'receipt={{receipts.candidate_report.path}} diagnostic={{ out_dir }}/reviews/report.json'",
      }],
    });
    const result = runWorkflow(['lint', '--workflow', workflowPath]);
    expect(result.status).toBe(0);
  });

  it('recovers a complete checkpoint without rerunning the successful prefix', async () => {
    const workflowPath = path.join(testDir, 'recover-complete.json');
    const outDir = path.join(testDir, 'recover-complete-out');
    const prefixMarker = path.join(testDir, 'prefix-count.txt');
    const suffixMarker = path.join(testDir, 'suffix-count.txt');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'recover-complete',
      steps: [
        { id: 'prefix', command: ['bash', '-lc', `printf x >> ${JSON.stringify(prefixMarker)}; printf prefix`] },
        { id: 'suffix', command: ['bash', '-lc', `printf y >> ${JSON.stringify(suffixMarker)}; printf suffix`] },
      ],
    });
    const interrupted = runWorkflow([
      '--workflow', workflowPath, '--project-root', testDir, '--out-dir', outDir, '--print-output', 'none',
    ], undefined, { JUNO_WORKFLOW_TEST_INTERRUPT_AT: 'checkpoint_before_terminal_manifest' });
    expect(interrupted.status).toBe(86);
    expect(await fs.readFile(prefixMarker, 'utf8')).toBe('x');
    expect(await fs.pathExists(suffixMarker)).toBe(false);
    expect(await fs.pathExists(path.join(outDir, 'manifest.json'))).toBe(false);

    const dry = runWorkflow(['recover-attempt', outDir, '--dry-run']);
    expect(dry.status).toBe(0);
    expect(JSON.parse(dry.stdout).verified_prefix_steps).toEqual(['prefix']);
    expect((await fs.readJson(path.join(outDir, 'run_contract.json'))).attempts).toHaveLength(0);
    const recovered = runWorkflow(['recover-attempt', outDir]);
    expect(recovered.status).toBe(0);
    const recovery = JSON.parse(recovered.stdout);
    expect(recovery.first_invalid_step).toBe('suffix');
    const contract = await fs.readJson(path.join(outDir, 'run_contract.json'));
    expect(contract.attempts.at(-1).status).toBe('interrupted');
    expect(contract.attempts.at(-1).recovery_reason).toBe('recovered_from_step_checkpoints');
    expect(await fs.pathExists(path.join(outDir, 'manifest.json'))).toBe(false);
    const doctor = runWorkflow(['doctor', '--json', outDir]);
    expect(doctor.status).toBe(0);
    expect(JSON.parse(doctor.stdout).status).toBe('ok');

    const resumed = runWorkflow([
      '--workflow', workflowPath, '--project-root', testDir, '--out-dir', outDir,
      '--from-step', 'suffix', '--print-output', 'none',
    ]);
    expect(resumed.status).toBe(0);
    expect(await fs.readFile(prefixMarker, 'utf8')).toBe('x');
    expect(await fs.readFile(suffixMarker, 'utf8')).toBe('y');
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.steps[0].status).toBe('reused_verified');
  });

  it.each([
    'command_success_before_artifacts',
    'artifacts_before_checkpoint',
  ])('never reuses incomplete evidence interrupted at %s', async (point) => {
    const workflowPath = path.join(testDir, `${point}.json`);
    const outDir = path.join(testDir, `${point}-out`);
    const markerPath = path.join(testDir, `${point}-count.txt`);
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: `incomplete-${point}`,
      steps: [{ id: 'write', command: ['bash', '-lc', `printf x >> ${JSON.stringify(markerPath)}`] }],
    });
    expect(runWorkflow([
      '--workflow', workflowPath, '--project-root', testDir, '--out-dir', outDir, '--print-output', 'none',
    ], undefined, { JUNO_WORKFLOW_TEST_INTERRUPT_AT: point }).status).toBe(86);
    const contract = await fs.readJson(path.join(outDir, 'run_contract.json'));
    expect(contract.completed_steps).toEqual({});
    const recovered = runWorkflow(['recover-attempt', outDir]);
    expect(recovered.status).toBe(0);
    expect(JSON.parse(recovered.stdout).verified_prefix_steps).toEqual([]);
  });

  it.each([
    'summary_before_attempt_manifest',
    'attempt_manifest_before_record',
    'attempt_record_before_root_manifest',
  ])('recovers or consumes hash-bound metadata across terminal write boundary %s', async (point) => {
    const workflowPath = path.join(testDir, `${point}.json`);
    const outDir = path.join(testDir, `${point}-out`);
    await fs.writeJson(workflowPath, {
      schema_version: 1, workflow_id: `terminal-${point}`,
      steps: [{ id: 'only', command: ['bash', '-lc', 'printf complete'] }],
    });
    expect(runWorkflow([
      '--workflow', workflowPath, '--project-root', testDir, '--out-dir', outDir, '--print-output', 'none',
    ], undefined, { JUNO_WORKFLOW_TEST_INTERRUPT_AT: point }).status).toBe(86);
    const contract = await fs.readJson(path.join(outDir, 'run_contract.json'));
    if (point === 'attempt_record_before_root_manifest') {
      expect(contract.attempts).toHaveLength(1);
      expect(runWorkflow(['doctor', outDir]).status).toBe(0);
      expect(runWorkflow(['recover-attempt', outDir]).status).not.toBe(0);
    } else {
      expect(contract.attempts).toHaveLength(0);
      expect(runWorkflow(['recover-attempt', outDir]).status).toBe(0);
    }
  });

  it('fails recovery closed for artifact, workflow, variable, frozen-input, and receipt drift', async () => {
    const workflowPath = path.join(testDir, 'recovery-drift.json');
    const frozenPath = path.join(testDir, 'frozen.txt');
    const receiptPath = path.join(testDir, 'recovery-receipt.json');
    const outDir = path.join(testDir, 'recovery-drift-out');
    await fs.writeFile(frozenPath, 'frozen');
    const producerCode = `import json,os,pathlib; pathlib.Path(${JSON.stringify(receiptPath)}).write_text(json.dumps({'schema_version':'fixture.v1','producer_step_digest':os.environ['JUNO_WORKFLOW_STEP_DIGEST']})); print('complete',end='')`;
    await fs.writeJson(workflowPath, {
      schema_version: 1, workflow_id: 'recovery-drift', vars: { mode: 'approved' },
      frozen_inputs: [{ id: 'input', path: frozenPath }],
      receipts: [{ id: 'evidence', producer: 'only', path: receiptPath, schema_version: 'fixture.v1', required_fields: ['producer_step_digest'] }],
      steps: [{ id: 'only', command: ['python3', '-c', producerCode] }],
    });
    expect(runWorkflow([
      '--workflow', workflowPath, '--project-root', testDir, '--out-dir', outDir,
      '--print-output', 'none',
    ], undefined, { JUNO_WORKFLOW_TEST_INTERRUPT_AT: 'checkpoint_before_terminal_manifest' }).status).toBe(86);
    const contract = await fs.readJson(path.join(outDir, 'run_contract.json'));
    await fs.writeFile(contract.completed_steps.only.artifacts.stdout.path, 'tampered');
    let rejected = runWorkflow(['recover-attempt', outDir]);
    expect(rejected.status).not.toBe(0);
    expect(rejected.stderr).toContain('hash mismatch');
    await fs.writeFile(contract.completed_steps.only.artifacts.stdout.path, 'complete');
    const receipt = await fs.readJson(receiptPath);
    receipt.tampered = true;
    await fs.writeJson(receiptPath, receipt);
    rejected = runWorkflow(['recover-attempt', outDir]);
    expect(rejected.status).not.toBe(0);
    expect(rejected.stderr).toContain('artifact_sha256');
    delete receipt.tampered;
    await fs.writeFile(receiptPath, JSON.stringify(receipt));
    await fs.writeFile(frozenPath, 'changed');
    rejected = runWorkflow(['recover-attempt', outDir]);
    expect(rejected.status).not.toBe(0);
    expect(rejected.stderr).toContain('frozen_input');
    await fs.writeFile(frozenPath, 'frozen');
    const workflow = await fs.readJson(workflowPath);
    workflow.vars.mode = 'drifted';
    await fs.writeJson(workflowPath, workflow);
    rejected = runWorkflow(['recover-attempt', outDir]);
    expect(rejected.status).not.toBe(0);
    expect(rejected.stderr).toContain('workflow source');
  });

  it('binds normalized workflow model selection and fails recovery on config policy drift', async () => {
    const { executablePath } = await installFakeJunoExecutable(testDir, 'yy');
    const configPath = path.join(testDir, '.juno_task', 'config.json');
    await fs.ensureDir(path.dirname(configPath));
    await fs.writeJson(configPath, { workflowModels: [':luna'] });
    const workflowPath = path.join(testDir, 'model-policy-recovery.json');
    const outDir = path.join(testDir, 'model-policy-recovery-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'model-policy-recovery',
      steps: [{ id: 'review', command: [executablePath, 'pi', '--model', ':luna', 'review'] }],
    });
    const interrupted = runWorkflow([
      '--workflow', workflowPath, '--project-root', testDir, '--out-dir', outDir, '--print-output', 'none',
    ], undefined, { JUNO_WORKFLOW_TEST_INTERRUPT_AT: 'checkpoint_before_terminal_manifest' });
    expect(interrupted.status).toBe(86);
    const contract = await fs.readJson(path.join(outDir, 'run_contract.json'));
    expect(contract.workflow_model_bindings.steps.review.normalized_selector).toBe(':luna');
    expect(contract.completed_steps.review.workflow_model_selection.normalized_selector).toBe(':luna');
    await fs.writeJson(configPath, { workflowModels: [':luna', ':sol'] });
    const recovered = runWorkflow(['recover-attempt', outDir, '--dry-run']);
    expect(recovered.status).not.toBe(0);
    expect(recovered.stderr).toContain('workflowModels policy drifted');
  });

  it('refuses recovery while a signalled caller leaves a step process active', async () => {
    const workflowPath = path.join(testDir, 'active-recovery.json');
    const outDir = path.join(testDir, 'active-recovery-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1, workflow_id: 'active-recovery',
      steps: [{ id: 'active', command: ['bash', '-lc', 'sleep 30'] }],
    });
    const child = spawn('python3', [templateScript, '--workflow', workflowPath, '--project-root', testDir,
      '--out-dir', outDir, '--print-output', 'none'], {
      cwd: workflowFixtureController,
      env: {
        ...process.env,
        JUNO_TASK_ROOT: workflowFixtureController,
        YYLO_SESSION_METADATA_DIRECTORY: path.join(workflowFixtureController!, '.test-metadata'),
      },
      stdio: 'ignore',
    });
    const activePath = path.join(outDir, 'active_step.json');
    for (let index = 0; index < 100 && !(await fs.pathExists(activePath)); index += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const active = await fs.readJson(activePath);
    child.kill('SIGTERM');
    await new Promise((resolve) => child.once('exit', resolve));
    const refused = runWorkflow(['recover-attempt', outDir]);
    expect(refused.status).not.toBe(0);
    expect(refused.stderr).toContain('active or its process state is ambiguous');
    try { process.kill(-active.process_group_id, 'SIGTERM'); } catch {}
  });

  it('fails ENOSPC capture persistence without creating a reusable checkpoint', async () => {
    const { executablePath } = await installFakeJunoExecutable(testDir, 'yy');
    const workflowPath = path.join(testDir, 'enospc.json');
    const outDir = path.join(testDir, 'enospc-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1, workflow_id: 'enospc',
      steps: [{ id: 'only', command: [executablePath, 'pi', 'capture'] }],
    });
    const result = runWorkflow([
      '--workflow', workflowPath, '--project-root', testDir, '--out-dir', outDir, '--print-output', 'none',
    ], undefined, {
      JUNO_WORKFLOW_TEST_INTERRUPT_AT: 'capture_before_checkpoint',
      JUNO_WORKFLOW_TEST_INTERRUPT_MODE: 'enospc',
    });
    expect(result.status).not.toBe(0);
    expect((await fs.readJson(path.join(outDir, 'run_contract.json'))).completed_steps).toEqual({});
  });

  it('separates semantic terminal failure from successful transport', async () => {
    const workflowPath = path.join(testDir, 'semantic-terminal.json');
    const outDir = path.join(testDir, 'semantic-terminal-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'semantic-terminal',
      terminal_gate: 'review',
      steps: [
        {
          id: 'review',
          expected_outcomes: ['PASS'],
          command: ['bash', '-lc', 'printf transport-success'],
        },
      ],
    });

    const result = runWorkflow(['--workflow', workflowPath, '--out-dir', outDir, '--print-output', 'none']);
    expect(result.status).toBe(1);
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.semantic_status).toBe('failed');
    expect(manifest.steps[0].transport_exit_code).toBe(0);
    expect(manifest.steps[0].exit_code).toBe(1);
    expect(manifest.steps[0].failure_reason).toContain('missing JUNO_WORKFLOW_OUTCOME footer');
  });

  it('keeps terminal-gated dry runs successful and labels them dry_run', async () => {
    const workflowPath = path.join(testDir, 'terminal-dry-run.json');
    const outDir = path.join(testDir, 'terminal-dry-run-out');
    await fs.writeJson(workflowPath, {
      schema_version: 1,
      workflow_id: 'terminal-dry-run',
      terminal_gate: 'gate',
      steps: [{ id: 'gate', expected_outcomes: ['PASS'], command: ['false'] }],
    });

    const result = runWorkflow([
      '--workflow', workflowPath,
      '--out-dir', outDir,
      '--dry-run',
      '--print-output', 'none',
    ]);
    expect(result.status).toBe(0);
    const manifest = await fs.readJson(path.join(outDir, 'manifest.json'));
    expect(manifest.semantic_status).toBe('dry_run');
    expect(manifest.status).toBe('success');
  });

  it('does not weaken advanced YAML contracts when the ambient interpreter omits site-packages', async () => {
    const workflowPath = path.join(testDir, 'advanced-without-pyyaml.yaml');
    await fs.writeFile(
      workflowPath,
      `schema_version: 1
workflow_id: advanced_without_pyyaml
receipts:
  - id: evidence
    producer: producer
    path: receipt.json
    schema_version: evidence.v1
steps:
  - id: producer
    command: true
`,
    );

    const result = spawnSync('python3', ['-S', templateScript, 'lint', '--workflow', workflowPath], {
      cwd: workflowFixtureController,
      encoding: 'utf8',
      env: {
        ...process.env,
        JUNO_TASK_ROOT: workflowFixtureController,
        YYLO_SESSION_METADATA_DIRECTORY: path.join(workflowFixtureController!, '.test-metadata'),
      },
    });
    expect(result.status).toBe(2);
    expect(result.stderr).toContain('required_fields must include producer_step_digest');
  });
});
