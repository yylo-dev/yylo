import { afterEach, describe, expect, it } from 'vitest';
import fs from 'fs-extra';
import * as path from 'node:path';
import os from 'node:os';

import { ShellBackend } from '../backends/shell-backend.js';
import type { ToolCallRequest, ProgressEvent } from '../../types/execution.js';

const tempRoots: string[] = [];

const createStubClaudeService = async () => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-stdout-'));
  tempRoots.push(tempRoot);

  const servicesDir = path.join(tempRoot, 'services');
  await fs.ensureDir(servicesDir);

  const scriptPath = path.join(servicesDir, 'claude.py');
  const scriptContent = `#!/usr/bin/env python3
import json

print(json.dumps({"type": "assistant", "content": "thinking"}))
print(json.dumps({"type": "result", "result": "done", "usage": {"input_tokens": 1, "output_tokens": 2}}))
`;
  await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

  return { servicesDir, workingDir: tempRoot };
};

const createEnvironmentBoundaryService = async () => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-env-boundary-'));
  tempRoots.push(tempRoot);
  const servicesDir = path.join(tempRoot, 'services');
  await fs.ensureDir(servicesDir);
  await fs.writeFile(
    path.join(servicesDir, 'claude.py'),
    `#!/usr/bin/env python3
import json, os, sys
names = sorted(name for name in os.environ if name.startswith("YYLO_LAST_"))
resume = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else None
print(json.dumps({"type":"result","result":json.dumps({"continuity_names":names,"config":os.environ.get("BOUNDARY_CONFIG"),"root":os.environ.get("JUNO_TASK_ROOT"),"resume":resume})}))
`,
    { mode: 0o755 },
  );
  return { servicesDir, workingDir: tempRoot };
};

const createStubTextService = async () => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-text-'));
  tempRoots.push(tempRoot);

  const servicesDir = path.join(tempRoot, 'services');
  await fs.ensureDir(servicesDir);

  const scriptPath = path.join(servicesDir, 'codex.py');
  const scriptContent = `#!/usr/bin/env python3
lines = [
  "import Link from 'next/link';",
  "export interface HeaderProps {",
  "  onToggleSideMenu?: () => void;",
  "  sticky?: boolean;",
  "}",
  "export function Header() {",
  "  const enabled = true;",
  "\\tconst tabValue = enabled;",
  "    const nested = enabled;",
  "  return nested;",
  "\\t\\t",
  "    ",
  "}",
]

for line in lines:
  print(line)
`;
  await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

  return { servicesDir, workingDir: tempRoot };
};

const createStubGeminiService = async () => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-gemini-'));
  tempRoots.push(tempRoot);

  const servicesDir = path.join(tempRoot, 'services');
  await fs.ensureDir(servicesDir);

  const scriptPath = path.join(servicesDir, 'gemini.py');
  const scriptContent = `#!/usr/bin/env python3
import json
import os
import sys

payload = {
  "argv": sys.argv[1:],
  "output_format_env": os.environ.get("GEMINI_OUTPUT_FORMAT"),
  "python_unbuffered_env": os.environ.get("PYTHONUNBUFFERED")
}

print(json.dumps({"type": "result", "content": json.dumps(payload)}))
`;
  await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

  return { servicesDir, workingDir: tempRoot };
};

const createStubPromptTransportService = async (subagent: 'claude' | 'codex' | 'gemini' | 'pi') => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-prompt-transport-'));
  tempRoots.push(tempRoot);

  const servicesDir = path.join(tempRoot, 'services');
  await fs.ensureDir(servicesDir);

  const scriptPath = path.join(servicesDir, `${subagent}.py`);
  const scriptContent = `#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]
prompt_file = None
for flag in ("--prompt-file", "-pp"):
  if flag in argv:
    index = argv.index(flag)
    if index + 1 < len(argv):
      prompt_file = argv[index + 1]

prompt_file_content = None
if prompt_file:
  with open(prompt_file, "r", encoding="utf-8") as handle:
    prompt_file_content = handle.read()

payload = {
  "argv": argv,
  "env_instruction": os.environ.get("JUNO_INSTRUCTION"),
  "prompt_file": prompt_file,
  "prompt_file_content": prompt_file_content,
}
print(json.dumps({"type": "result", "result": json.dumps(payload), "content": json.dumps(payload)}))
`;
  await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

  return { servicesDir, workingDir: tempRoot };
};

const createStubPiLiveService = async () => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-live-'));
  tempRoots.push(tempRoot);

  const servicesDir = path.join(tempRoot, 'services');
  await fs.ensureDir(servicesDir);

  const scriptPath = path.join(servicesDir, 'pi.py');
  const scriptContent = `#!/usr/bin/env python3
import json
import time

print(json.dumps({"type": "assistant", "content": "live-start"}))
time.sleep(0.12)
print(json.dumps({"type": "result", "result": "live-done", "session_id": "pi-live-session"}))
`;
  await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

  return { servicesDir, workingDir: tempRoot };
};

const createStubPiLargeOutputService = async () => {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-large-output-'));
  tempRoots.push(tempRoot);

  const servicesDir = path.join(tempRoot, 'services');
  await fs.ensureDir(servicesDir);

  const scriptPath = path.join(servicesDir, 'pi.py');
  const scriptContent = `#!/usr/bin/env python3
import json
import sys

sys.stdout.write("x" * (256 * 1024))
sys.stdout.write("\\n")
print(json.dumps({"type": "result", "result": "large-output-done", "session_id": "pi-large-session"}))
`;
  await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

  return { servicesDir, workingDir: tempRoot };
};

afterEach(async () => {
  await Promise.all(tempRoots.map((dir) => fs.remove(dir)));
  tempRoots.length = 0;
});

describe('ShellBackend structured output', () => {
  it('waits for timeout escalation to kill Pi child and grandchild before rejecting', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-process-group-'));
    tempRoots.push(tempRoot);
    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);
    const marker = path.join(tempRoot, 'late-mutation');
    const pids = path.join(tempRoot, 'descendant-pids');
    await fs.writeFile(path.join(servicesDir, 'pi.py'), `#!/usr/bin/env python3
import os, pathlib, signal, subprocess, sys, time
pids = pathlib.Path(${JSON.stringify(pids)})
grandchild_code = "import pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(4); pathlib.Path(%r).write_text('late')" % ${JSON.stringify(marker)}
child_code = "import os,pathlib,signal,subprocess,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); g=subprocess.Popen([sys.executable,'-c',%r]); pathlib.Path(%r).write_text(str(os.getpid())+' '+str(g.pid)); time.sleep(30)" % (grandchild_code, str(pids))
subprocess.Popen([sys.executable, '-c', child_code])
# Synchronize fixture startup: the service does not enter its simulated tool call
# until both adversarial descendant PIDs have been durably published.
while not pids.exists() or len(pids.read_text().split()) != 2: time.sleep(.01)
print('descendants-ready', flush=True)
time.sleep(30)
`, { mode: 0o755 });
    const backend = new ShellBackend();
    // Leave ample startup budget for loaded CI hosts. The fixture handshake above
    // guarantees that a normally reached timeout always has two descendants to kill.
    backend.configure({ workingDirectory: tempRoot, servicesPath: servicesDir, timeout: 3000 });
    await backend.initialize();
    await expect(backend.execute({
      toolName: 'pi_subagent', arguments: { instruction: 'wait', project_path: tempRoot },
      timeout: 1000, priority: 'normal', metadata: { sessionId: 'cancel-test', iterationNumber: 1 },
    })).rejects.toThrow('timed out');
    const descendantPids = (await fs.readFile(pids, 'utf8')).trim().split(/\s+/).map(Number);
    for (const pid of descendantPids) expect(() => process.kill(pid, 0)).toThrow();
    await new Promise((done) => setTimeout(done, 4200));
    expect(await fs.pathExists(marker)).toBe(false);
  });

  it.each([true, false])('forwards outer terminal capability to piped Pi output (TTY=%s)', async (tty) => {
    const { servicesDir, workingDir } = await createStubClaudeService();
    await fs.writeFile(path.join(servicesDir, 'pi.py'), `#!/usr/bin/env python3
import json, os, sys
print(json.dumps({"type":"result","result":json.dumps({"hint":os.environ.get("JUNO_PI_OUTPUT_TTY"),"tty":sys.stdout.isatty()})}))
`, { mode: 0o755 });
    const descriptor = Object.getOwnPropertyDescriptor(process.stdout, 'isTTY');
    const previousHint = process.env.JUNO_PI_OUTPUT_TTY;
    try {
      Object.defineProperty(process.stdout, 'isTTY', { value: tty, configurable: true });
      process.env.JUNO_PI_OUTPUT_TTY = tty ? '0' : '1';
      const backend = new ShellBackend();
      backend.configure({ workingDirectory: workingDir, servicesPath: servicesDir,
        enableJsonStreaming: true, outputRawJson: true });
      await backend.initialize();
      const result = await backend.execute({
        toolName: 'pi_subagent', arguments: { instruction: 'test', project_path: workingDir },
        timeout: 15000, priority: 'normal',
        metadata: { sessionId: 'tty-test', iterationNumber: 1 },
      });
      const payload = JSON.parse(JSON.parse(result.content).result);
      expect(payload).toEqual({ hint: tty ? '1' : '0', tty: false });
    } finally {
      if (descriptor) Object.defineProperty(process.stdout, 'isTTY', descriptor);
      else delete (process.stdout as { isTTY?: boolean }).isTTY;
      if (previousHint === undefined) delete process.env.JUNO_PI_OUTPUT_TTY;
      else process.env.JUNO_PI_OUTPUT_TTY = previousHint;
    }
  });

  it('emits JSON-parsable stdout even when capture file is absent', async () => {
    const { servicesDir, workingDir } = await createStubClaudeService();

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: workingDir,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
      outputRawJson: true,
    });
    await backend.initialize();

    const progressEvents: ProgressEvent[] = [];
    const request: ToolCallRequest = {
      toolName: 'claude_subagent',
      arguments: {
        instruction: 'Return stub data',
        project_path: workingDir,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
      progressCallback: async (event) => {
        progressEvents.push(event);
      },
    };

    const result = await backend.execute(request);

    const parsed = JSON.parse(result.content);
    expect(parsed.type).toBe('result');
    expect(parsed.is_error).toBe(false);
    expect(parsed.result).toContain('done');
    expect(parsed.sub_agent_response).toBeTruthy();

    const metadata = result.metadata as any;
    expect(metadata?.structuredOutput).toBe(true);
    expect(metadata?.rawOutput).toContain('"result": "done"');
  });

  it('uses the Claude service wrapper when Cursor has no dedicated service script', async () => {
    const { servicesDir, workingDir } = await createStubClaudeService();
    const backend = new ShellBackend();
    backend.configure({ workingDirectory: workingDir, servicesPath: servicesDir });
    await backend.initialize();

    const result = await backend.execute({
      toolName: 'cursor_subagent',
      arguments: { instruction: 'Return stub data', project_path: workingDir },
      timeout: 15000,
      priority: 'normal',
      metadata: { sessionId: 'cursor-test', iterationNumber: 1 },
    });

    expect(result.content).toContain('done');
  });

  it('filters service continuity while preserving config/routing and typed resume dispatch', async () => {
    const { servicesDir, workingDir } = await createEnvironmentBoundaryService();
    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: workingDir,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
      environment: {
        BOUNDARY_CONFIG: 'preserved',
        JUNO_TASK_ROOT: '/controller',
        YYLO_LAST_SESSION_ID_SCOPE_0123456789ABCDEF: 'historical',
        YYLO_LAST_EXECUTION_SETTINGS: 'legacy',
      },
    });
    await backend.initialize();

    const result = await backend.execute({
      toolName: 'claude_subagent',
      arguments: { project_path: workingDir, resume: 'current-session' },
      timeout: 15000,
      priority: 'normal',
      metadata: { sessionId: 'test-session', iterationNumber: 1 },
    });

    const payload = JSON.parse(JSON.parse(result.content).result);
    expect(payload).toEqual({
      continuity_names: [],
      config: 'preserved',
      root: '/controller',
      resume: 'current-session',
    });
  });

  it('preserves leading whitespace for text streaming outputs', async () => {
    const { servicesDir, workingDir } = await createStubTextService();

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: workingDir,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const progressEvents: ProgressEvent[] = [];
    const dispose = backend.onProgress(async (event) => {
      progressEvents.push(event);
    });

    const request: ToolCallRequest = {
      toolName: 'codex_subagent',
      arguments: {
        project_path: workingDir,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    await backend.execute(request);
    dispose();

    const thinkingLines = progressEvents
      .filter((event) => event.type === 'thinking')
      .map((event) => event.content);

    expect(thinkingLines).toContain('  onToggleSideMenu?: () => void;');
    expect(thinkingLines).toContain('  sticky?: boolean;');
    expect(thinkingLines).toContain('\tconst tabValue = enabled;');
    expect(thinkingLines).toContain('    const nested = enabled;');
    expect(thinkingLines).toContain('  return nested;');
    expect(thinkingLines).toContain('\t\t');
    expect(thinkingLines).toContain('    ');
  });

  it('forces stream-json output format when invoking gemini service scripts', async () => {
    const originalOutputFormat = process.env.GEMINI_OUTPUT_FORMAT;
    delete process.env.GEMINI_OUTPUT_FORMAT;

    try {
      const { servicesDir, workingDir } = await createStubGeminiService();

      const backend = new ShellBackend();
      backend.configure({
        workingDirectory: workingDir,
        servicesPath: servicesDir,
        enableJsonStreaming: true,
      });
      await backend.initialize();

      const request: ToolCallRequest = {
        toolName: 'gemini_subagent',
        arguments: {
          instruction: 'Show args',
          model: 'gemini-2.5-pro',
          project_path: workingDir,
        },
        timeout: 15000,
        priority: 'normal',
        metadata: {
          sessionId: 'test-session',
          iterationNumber: 1,
        },
      };

      const result = await backend.execute(request);
      const firstLine = result.content.trim().split('\n')[0];
      const parsed = JSON.parse(firstLine);
      const payload = JSON.parse(parsed.content);

      expect(payload.argv).toContain('--output-format');
      expect(payload.argv).toContain('stream-json');
      expect(payload.output_format_env).toBe('stream-json');
      expect(payload.python_unbuffered_env).toBe('1');
    } finally {
      if (originalOutputFormat !== undefined) {
        process.env.GEMINI_OUTPUT_FORMAT = originalOutputFormat;
      } else {
        delete process.env.GEMINI_OUTPUT_FORMAT;
      }
    }
  });

  it('keeps small Python subagent prompts in argv and JUNO_INSTRUCTION', async () => {
    const originalThreshold = process.env.JUNO_PROMPT_ARG_MAX_BYTES;
    process.env.JUNO_PROMPT_ARG_MAX_BYTES = '1024';

    try {
      const { servicesDir, workingDir } = await createStubPromptTransportService('claude');

      const backend = new ShellBackend();
      backend.configure({
        workingDirectory: workingDir,
        servicesPath: servicesDir,
        enableJsonStreaming: true,
      });
      await backend.initialize();

      const instruction = 'small prompt';
      const request: ToolCallRequest = {
        toolName: 'claude_subagent',
        arguments: {
          instruction,
          project_path: workingDir,
        },
        timeout: 15000,
        priority: 'normal',
        metadata: {
          sessionId: 'test-session',
          iterationNumber: 1,
        },
      };

      const result = await backend.execute(request);
      const parsed = JSON.parse(result.content);
      const payload = JSON.parse(parsed.result);

      expect(payload.argv).toContain('-p');
      expect(payload.argv).toContain(instruction);
      expect(payload.argv).not.toContain('--prompt-file');
      expect(payload.env_instruction).toBe(instruction);
      expect(payload.prompt_file).toBeNull();
    } finally {
      if (originalThreshold !== undefined) {
        process.env.JUNO_PROMPT_ARG_MAX_BYTES = originalThreshold;
      } else {
        delete process.env.JUNO_PROMPT_ARG_MAX_BYTES;
      }
    }
  });

  it('uses an internal prompt file and omits JUNO_INSTRUCTION for oversized Python subagent prompts', async () => {
    const originalThreshold = process.env.JUNO_PROMPT_ARG_MAX_BYTES;
    process.env.JUNO_PROMPT_ARG_MAX_BYTES = '8';

    try {
      const { servicesDir, workingDir } = await createStubPromptTransportService('pi');

      const backend = new ShellBackend();
      backend.configure({
        workingDirectory: workingDir,
        servicesPath: servicesDir,
        enableJsonStreaming: true,
      });
      await backend.initialize();

      const instruction = 'this prompt is definitely larger than eight bytes';
      const request: ToolCallRequest = {
        toolName: 'pi_subagent',
        arguments: {
          instruction,
          project_path: workingDir,
        },
        timeout: 15000,
        priority: 'normal',
        metadata: {
          sessionId: 'test-session',
          iterationNumber: 1,
        },
      };

      const result = await backend.execute(request);
      const parsed = JSON.parse(result.content);
      const payload = JSON.parse(parsed.result);

      expect(payload.argv).not.toContain('-p');
      expect(payload.argv).not.toContain(instruction);
      expect(payload.argv).toContain('--prompt-file');
      expect(payload.env_instruction).toBeNull();
      expect(payload.prompt_file).toContain(path.join(os.tmpdir(), 'yylo'));
      expect(payload.prompt_file_content).toBe(instruction);
      expect(await fs.pathExists(payload.prompt_file)).toBe(false);
    } finally {
      if (originalThreshold !== undefined) {
        process.env.JUNO_PROMPT_ARG_MAX_BYTES = originalThreshold;
      } else {
        delete process.env.JUNO_PROMPT_ARG_MAX_BYTES;
      }
    }
  });

  it('applies oversized prompt-file transport to claude, codex, gemini, and pi Python subagents', async () => {
    const originalThreshold = process.env.JUNO_PROMPT_ARG_MAX_BYTES;
    process.env.JUNO_PROMPT_ARG_MAX_BYTES = '4';

    try {
      for (const subagent of ['claude', 'codex', 'gemini', 'pi'] as const) {
        const { servicesDir, workingDir } = await createStubPromptTransportService(subagent);

        const backend = new ShellBackend();
        backend.configure({
          workingDirectory: workingDir,
          servicesPath: servicesDir,
          enableJsonStreaming: true,
        });
        await backend.initialize();

        const instruction = `${subagent} oversized prompt`;
        const request: ToolCallRequest = {
          toolName: `${subagent}_subagent`,
          arguments: {
            instruction,
            project_path: workingDir,
          },
          timeout: 15000,
          priority: 'normal',
          metadata: {
            sessionId: 'test-session',
            iterationNumber: 1,
          },
        };

        const result = await backend.execute(request);
        const firstLine = result.content.trim().split('\n')[0];
        const parsed = JSON.parse(firstLine);
        const payload = JSON.parse(parsed.result ?? parsed.content);

        expect(payload.argv).toContain('--prompt-file');
        expect(payload.argv).not.toContain('-p');
        expect(payload.argv).not.toContain(instruction);
        expect(payload.env_instruction).toBeNull();
        expect(payload.prompt_file_content).toBe(instruction);
      }
    } finally {
      if (originalThreshold !== undefined) {
        process.env.JUNO_PROMPT_ARG_MAX_BYTES = originalThreshold;
      } else {
        delete process.env.JUNO_PROMPT_ARG_MAX_BYTES;
      }
    }
  });

  it('cleans oversized prompt files when spawn throws before a child process exists', async () => {
    const originalThreshold = process.env.JUNO_PROMPT_ARG_MAX_BYTES;
    process.env.JUNO_PROMPT_ARG_MAX_BYTES = '4';

    try {
      const { servicesDir, workingDir } = await createStubPromptTransportService('claude');
      const beforeFiles = new Set(
        (await fs.readdir(path.join(os.tmpdir(), 'yylo')).catch(() => [] as string[])).filter(
          (name) => name.includes('claude_subagent') && name.endsWith('-prompt.md'),
        ),
      );

      const backend = new ShellBackend();
      backend.configure({
        workingDirectory: `${workingDir}\0invalid`,
        servicesPath: servicesDir,
        enableJsonStreaming: true,
      });
      await backend.initialize();

      const request: ToolCallRequest = {
        toolName: 'claude_subagent',
        arguments: {
          instruction: 'oversized prompt that must be cleaned after a synchronous spawn throw',
          project_path: workingDir,
        },
        timeout: 15000,
        priority: 'normal',
        metadata: {
          sessionId: 'test-session',
          iterationNumber: 1,
        },
      };

      await expect(backend.execute(request)).rejects.toThrow('Failed to execute script');

      const afterFiles = (
        await fs.readdir(path.join(os.tmpdir(), 'yylo')).catch(() => [] as string[])
      ).filter((name) => name.includes('claude_subagent') && name.endsWith('-prompt.md'));
      expect(afterFiles.filter((name) => !beforeFiles.has(name))).toEqual([]);
    } finally {
      if (originalThreshold !== undefined) {
        process.env.JUNO_PROMPT_ARG_MAX_BYTES = originalThreshold;
      } else {
        delete process.env.JUNO_PROMPT_ARG_MAX_BYTES;
      }
    }
  });

  it('does not enforce backend timeout for pi live sessions', async () => {
    const { servicesDir, workingDir } = await createStubPiLiveService();

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: workingDir,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
      timeout: 50,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: 'open live',
        project_path: workingDir,
        live: true,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);
    const parsed = JSON.parse(result.content);
    expect(parsed.is_error).toBe(false);
    expect(parsed.result).toBe('live-done');
    expect(parsed.session_id).toBe('pi-live-session');
  });

  it('builds structured error output for generic subagent (pi) failures', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-err-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    // Create a pi.py that outputs a session event to stdout then crashes with error on stderr
    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json, sys

print(json.dumps({"type": "session", "id": "test-session"}))
print("Error: No API key found for vercel-ai-gateway.", file=sys.stderr)
sys.exit(1)
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: 'test task',
        model: 'zai/glm-5',
        project_path: tempRoot,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    // Should be marked as failed
    expect(result.status).toBe('failed');

    // Should have structured error output
    const parsed = JSON.parse(result.content);
    expect(parsed.type).toBe('result');
    expect(parsed.subtype).toBe('error');
    expect(parsed.is_error).toBe(true);
    expect(parsed.error).toContain('No API key found');
    expect(parsed.exit_code).toBe(1);

    // Metadata should include structured output flag
    const metadata = result.metadata as any;
    expect(metadata?.structuredOutput).toBe(true);
  });

  it('streams stderr as progress events for generic subagents', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-stderr-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json, sys

print("Executing: pi --model glm-5 --provider zai", file=sys.stderr)
print(json.dumps({"type": "session", "id": "test"}))
print("Error: connection refused", file=sys.stderr)
sys.exit(1)
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const progressEvents: ProgressEvent[] = [];
    const dispose = backend.onProgress(async (event) => {
      progressEvents.push(event);
    });

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: 'test task',
        project_path: tempRoot,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    await backend.execute(request);
    dispose();

    // stderr messages should appear as progress events with source: 'stderr'
    const stderrEvents = progressEvents.filter((e) => e.metadata?.source === 'stderr');
    expect(stderrEvents.length).toBeGreaterThan(0);

    const stderrContent = stderrEvents.map((e) => e.content).join('\n');
    expect(stderrContent).toContain('Executing: pi --model glm-5 --provider zai');
    expect(stderrContent).toContain('Error: connection refused');
  });

  it('returns raw output for generic subagent success', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-ok-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json

print(json.dumps({"type": "session", "id": "test"}))
print(json.dumps({"type": "agent_end", "result": "task completed"}))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: 'test task',
        project_path: tempRoot,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    // Should be marked as completed
    expect(result.status).toBe('completed');

    // Pi subagent now returns structured output with result extracted
    const parsed = JSON.parse(result.content);
    expect(parsed.type).toBe('result');
    expect(parsed.subtype).toBe('success');
    expect(parsed.is_error).toBe(false);
    expect(parsed.result).toBe('task completed');
  });

  it('extracts structured output from codex capture file when available', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-codex-cap-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    // Create a codex.py that writes a capture file and outputs agent_message events
    const scriptPath = path.join(servicesDir, 'codex.py');
    const scriptContent = `#!/usr/bin/env python3
import json, os, sys

# Write capture file if JUNO_SUBAGENT_CAPTURE_PATH is set
capture_path = os.environ.get("JUNO_SUBAGENT_CAPTURE_PATH")

# Simulate streaming output
print(json.dumps({"type": "agent_reasoning", "msg": {"type": "agent_reasoning", "text": "thinking..."}}))
final_event = {"type": "agent_message", "msg": {"type": "agent_message", "message": "Here is the solution code."}}
print(json.dumps(final_event))

if capture_path:
    with open(capture_path, "w") as f:
        f.write(json.dumps(final_event))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
      outputRawJson: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'codex_subagent',
      arguments: {
        instruction: 'Write a hello world',
        project_path: tempRoot,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    const parsed = JSON.parse(result.content);
    expect(parsed.type).toBe('result');
    expect(parsed.is_error).toBe(false);
    expect(parsed.result).toBe('Here is the solution code.');
    expect(parsed.sub_agent_response).toBeTruthy();
    expect(parsed.sub_agent_response.msg.type).toBe('agent_message');

    const metadata = result.metadata as any;
    expect(metadata?.structuredOutput).toBe(true);
    expect(metadata?.subAgentResponse).toBeTruthy();
  });

  it('extracts result from codex item.completed event format (not legacy msg format)', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-codex-item-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    // Create a codex.py that writes item.completed format (new codex event schema)
    const scriptPath = path.join(servicesDir, 'codex.py');
    const scriptContent = `#!/usr/bin/env python3
import json, os, sys

capture_path = os.environ.get("JUNO_SUBAGENT_CAPTURE_PATH")

# Simulate codex NDJSON streaming with item.completed events
print(json.dumps({"type": "thread.started", "thread_id": "test-thread-id"}))
print(json.dumps({"type": "turn.started"}))
print(json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "Thinking about the task..."}}))
final_event = {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "Here is the final answer from codex."}}
print(json.dumps(final_event))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 50}}))

if capture_path:
    with open(capture_path, "w") as f:
        f.write(json.dumps(final_event))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
      outputRawJson: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'codex_subagent',
      arguments: {
        instruction: 'Write a hello world',
        project_path: tempRoot,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    const parsed = JSON.parse(result.content);
    expect(parsed.type).toBe('result');
    expect(parsed.is_error).toBe(false);
    // The key assertion: result should be the agent_message text, NOT the entire NDJSON stream
    expect(parsed.result).toBe('Here is the final answer from codex.');
    expect(parsed.sub_agent_response).toBeTruthy();
    expect(parsed.sub_agent_response.item.type).toBe('agent_message');

    const metadata = result.metadata as any;
    expect(metadata?.structuredOutput).toBe(true);
    expect(metadata?.subAgentResponse).toBeTruthy();
  });

  it('falls back to last JSON event for codex when capture file is absent', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-codex-fb-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    // Create a codex.py that does NOT write a capture file
    const scriptPath = path.join(servicesDir, 'codex.py');
    const scriptContent = `#!/usr/bin/env python3
import json

print(json.dumps({"type": "agent_reasoning", "msg": {"type": "agent_reasoning", "text": "analyzing..."}}))
print(json.dumps({"type": "agent_message", "msg": {"type": "agent_message", "message": "Final answer from codex."}}))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
      outputRawJson: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'codex_subagent',
      arguments: {
        instruction: 'Explain something',
        project_path: tempRoot,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    const parsed = JSON.parse(result.content);
    expect(parsed.type).toBe('result');
    expect(parsed.is_error).toBe(false);
    expect(parsed.result).toBe('Final answer from codex.');

    const metadata = result.metadata as any;
    expect(metadata?.structuredOutput).toBe(true);
  });

  it('passes --cd flag to Pi subagent with project_path', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-cd-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    // Create a pi.py that echoes sys.argv as JSON so we can inspect arguments
    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json, sys

print(json.dumps({"type": "argv", "args": sys.argv[1:]}))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: 'test task',
        project_path: '/my/project/dir',
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    // Parse the argv from the stub script output
    const lines = result.content.trim().split('\n');
    const lastJsonLine = lines.filter((l) => l.startsWith('{')).pop();
    expect(lastJsonLine).toBeTruthy();
    const parsed = JSON.parse(lastJsonLine!);

    // Pi subagent should receive --cd /my/project/dir
    const cdIdx = parsed.args.indexOf('--cd');
    expect(cdIdx).toBeGreaterThanOrEqual(0);
    expect(parsed.args[cdIdx + 1]).toBe('/my/project/dir');
  });

  it('forwards --live flag to Pi subagent and consumes capture payload when provided', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-live-cap-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json, os, sys

print(json.dumps({"type": "argv", "args": sys.argv[1:]}))

capture_path = os.environ.get("JUNO_SUBAGENT_CAPTURE_PATH")
if capture_path:
    captured = {
      "type": "result",
      "subtype": "success",
      "is_error": False,
      "result": "captured live result",
      "session_id": "pi-live-session-123",
      "usage": {"cost": {"total": 0.000777}}
    }
    with open(capture_path, "w") as f:
        f.write(json.dumps(captured))

print(json.dumps({"type": "agent_end", "result": "stdout fallback result"}))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
      outputRawJson: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: 'test live task',
        project_path: tempRoot,
        live: true,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    const parsed = JSON.parse(result.content);
    expect(parsed.type).toBe('result');
    expect(parsed.result).toBe('captured live result');
    expect(parsed.session_id).toBe('pi-live-session-123');
    expect(parsed.total_cost_usd).toBeCloseTo(0.000777, 10);

    const rawOutput = (result.metadata as any)?.rawOutput ?? result.content;
    const argvLine = rawOutput
      .trim()
      .split('\n')
      .find((line: string) => line.includes('"type": "argv"'));
    expect(argvLine).toBeTruthy();
    const argvPayload = JSON.parse(argvLine!);

    expect(argvPayload.args).toContain('--live');
  });

  it('attaches Pi live mode to inherited stdio on TTY while preserving capture payload', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-live-tty-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json, os, sys

print("stdout-live-sentinel")

capture_path = os.environ.get("JUNO_SUBAGENT_CAPTURE_PATH")
if capture_path:
    captured = {
      "type": "result",
      "subtype": "success",
      "is_error": False,
      "result": "captured tty live result",
      "argv": sys.argv[1:]
    }
    with open(capture_path, "w") as f:
        f.write(json.dumps(captured))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
      outputRawJson: true,
    });
    await backend.initialize();

    const stdinStream = process.stdin as NodeJS.ReadStream & { isTTY?: boolean };
    const stdoutStream = process.stdout as NodeJS.WriteStream & { isTTY?: boolean };
    const originalStdinIsTTY = stdinStream.isTTY;
    const originalStdoutIsTTY = stdoutStream.isTTY;

    Object.defineProperty(stdinStream, 'isTTY', { value: true, configurable: true });
    Object.defineProperty(stdoutStream, 'isTTY', { value: true, configurable: true });

    try {
      const request: ToolCallRequest = {
        toolName: 'pi_subagent',
        arguments: {
          instruction: 'interactive live task',
          project_path: tempRoot,
          live: true,
        },
        timeout: 15000,
        priority: 'normal',
        metadata: {
          sessionId: 'test-session',
          iterationNumber: 1,
        },
      };

      const result = await backend.execute(request);

      const parsed = JSON.parse(result.content);
      expect(parsed.type).toBe('result');
      expect(parsed.result).toBe('captured tty live result');
      expect(parsed.sub_agent_response?.argv).toContain('--live');

      // In inherited-stdio live mode, script stdout is not captured into rawOutput.
      const rawOutput = (result.metadata as any)?.rawOutput ?? '';
      expect(rawOutput).not.toContain('stdout-live-sentinel');
    } finally {
      if (originalStdinIsTTY === undefined) {
        delete (stdinStream as Record<string, unknown>).isTTY;
      } else {
        Object.defineProperty(stdinStream, 'isTTY', {
          value: originalStdinIsTTY,
          configurable: true,
        });
      }

      if (originalStdoutIsTTY === undefined) {
        delete (stdoutStream as Record<string, unknown>).isTTY;
      } else {
        Object.defineProperty(stdoutStream, 'isTTY', {
          value: originalStdoutIsTTY,
          configurable: true,
        });
      }
    }
  });

  it('attaches Pi live mode to inherited stdio when stdout is TTY but stdin is redirected', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-live-stdin-redirect-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json, os, sys

print("stdout-live-stdin-redirect")

capture_path = os.environ.get("JUNO_SUBAGENT_CAPTURE_PATH")
if capture_path:
    captured = {
      "type": "result",
      "subtype": "success",
      "is_error": False,
      "result": "captured live stdin redirect result",
      "argv": sys.argv[1:]
    }
    with open(capture_path, "w") as f:
        f.write(json.dumps(captured))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
      outputRawJson: true,
    });
    await backend.initialize();

    const stdinStream = process.stdin as NodeJS.ReadStream & { isTTY?: boolean };
    const stdoutStream = process.stdout as NodeJS.WriteStream & { isTTY?: boolean };
    const originalStdinIsTTY = stdinStream.isTTY;
    const originalStdoutIsTTY = stdoutStream.isTTY;

    Object.defineProperty(stdinStream, 'isTTY', { value: false, configurable: true });
    Object.defineProperty(stdoutStream, 'isTTY', { value: true, configurable: true });

    try {
      const request: ToolCallRequest = {
        toolName: 'pi_subagent',
        arguments: {
          instruction: 'interactive live task from redirected stdin',
          project_path: tempRoot,
          live: true,
        },
        timeout: 15000,
        priority: 'normal',
        metadata: {
          sessionId: 'test-session',
          iterationNumber: 1,
        },
      };

      const result = await backend.execute(request);

      const parsed = JSON.parse(result.content);
      expect(parsed.type).toBe('result');
      expect(parsed.result).toBe('captured live stdin redirect result');
      expect(parsed.sub_agent_response?.argv).toContain('--live');

      const rawOutput = (result.metadata as any)?.rawOutput ?? '';
      expect(rawOutput).not.toContain('stdout-live-stdin-redirect');
    } finally {
      if (originalStdinIsTTY === undefined) {
        delete (stdinStream as Record<string, unknown>).isTTY;
      } else {
        Object.defineProperty(stdinStream, 'isTTY', {
          value: originalStdinIsTTY,
          configurable: true,
        });
      }

      if (originalStdoutIsTTY === undefined) {
        delete (stdoutStream as Record<string, unknown>).isTTY;
      } else {
        Object.defineProperty(stdoutStream, 'isTTY', {
          value: originalStdoutIsTTY,
          configurable: true,
        });
      }
    }
  });

  it('keeps Pi structured fallback stable when live capture file is absent', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-live-fallback-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json, sys

print(json.dumps({"type": "argv", "args": sys.argv[1:]}))
print(json.dumps({"type": "agent_end", "result": "live fallback result"}))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: 'live fallback task',
        project_path: tempRoot,
        live: true,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    expect(result.status).toBe('completed');
    const parsed = JSON.parse(result.content);
    expect(parsed.type).toBe('result');
    expect(parsed.subtype).toBe('success');
    expect(parsed.is_error).toBe(false);
    expect(parsed.result).toBe('live fallback result');
    expect(parsed.sub_agent_response?.type).toBe('agent_end');
  });

  it('keeps live Pi failure capture session_id for resume visibility', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-live-error-session-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json, os, sys

capture_path = os.environ.get("JUNO_SUBAGENT_CAPTURE_PATH")
if capture_path:
    payload = {
      "type": "result",
      "subtype": "error",
      "is_error": True,
      "result": "upstream provider failed",
      "error": "upstream provider failed",
      "session_id": "sess-live-failure-123"
    }
    with open(capture_path, "w") as f:
        f.write(json.dumps(payload))

sys.exit(1)
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: 'live failure with session id',
        project_path: tempRoot,
        live: true,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    expect(result.status).toBe('failed');
    const parsed = JSON.parse(result.content);
    expect(parsed.type).toBe('result');
    expect(parsed.subtype).toBe('error');
    expect(parsed.is_error).toBe(true);
    expect(parsed.session_id).toBe('sess-live-failure-123');
    expect(parsed.result).toContain('upstream provider failed');
  });

  it('preserves session_id when Pi capture only contains session snapshot before failure', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-live-session-snapshot-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json, os, sys

capture_path = os.environ.get("JUNO_SUBAGENT_CAPTURE_PATH")
if capture_path:
    payload = {
      "type": "result",
      "subtype": "session",
      "is_error": False,
      "session_id": "sess-live-snapshot-only"
    }
    with open(capture_path, "w") as f:
        f.write(json.dumps(payload))

print("provider crashed before agent_end", file=sys.stderr)
sys.exit(1)
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: 'live failure with snapshot-only session id',
        project_path: tempRoot,
        live: true,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    expect(result.status).toBe('failed');
    const parsed = JSON.parse(result.content);
    expect(parsed.type).toBe('result');
    expect(parsed.subtype).toBe('error');
    expect(parsed.is_error).toBe(true);
    expect(parsed.result).toContain('provider crashed before agent_end');
    expect(parsed.session_id).toBe('sess-live-snapshot-only');
  });

  it('treats snapshot-only Pi capture as failed even when process exits zero', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-live-snapshot-success-exit-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json, os, sys

capture_path = os.environ.get("JUNO_SUBAGENT_CAPTURE_PATH")
if capture_path:
    payload = {
      "type": "result",
      "subtype": "session",
      "is_error": False,
      "session_id": "sess-live-snapshot-zero-exit"
    }
    with open(capture_path, "w") as f:
        f.write(json.dumps(payload))

sys.exit(0)
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: 'live snapshot-only zero-exit repro',
        project_path: tempRoot,
        live: true,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    expect(result.status).toBe('failed');
    const parsed = JSON.parse(result.content);
    expect(parsed.type).toBe('result');
    expect(parsed.subtype).toBe('error');
    expect(parsed.is_error).toBe(true);
    expect(parsed.result).toContain('session snapshot only');
    expect(parsed.session_id).toBe('sess-live-snapshot-zero-exit');
  });

  it('keeps Pi live success payload structured when capture has empty result but includes session_id', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-live-empty-result-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json, os, sys

capture_path = os.environ.get("JUNO_SUBAGENT_CAPTURE_PATH")
if capture_path:
    payload = {
      "type": "result",
      "subtype": "success",
      "is_error": False,
      "result": "",
      "session_id": "sess-live-empty-result"
    }
    with open(capture_path, "w") as f:
        f.write(json.dumps(payload))

sys.exit(0)
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: 'live success with empty result',
        project_path: tempRoot,
        live: true,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    expect(result.status).toBe('completed');
    const parsed = JSON.parse(result.content);
    expect(parsed.type).toBe('result');
    expect(parsed.subtype).toBe('success');
    expect(parsed.is_error).toBe(false);
    expect(parsed.result).toBe('');
    expect(parsed.session_id).toBe('sess-live-empty-result');
  });

  it('keeps non-live Pi argv unchanged (no --live flag)', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-nonlive-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json, sys

print(json.dumps({"type": "argv", "args": sys.argv[1:]}))
print(json.dumps({"type": "agent_end", "result": "non-live result"}))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
      outputRawJson: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: 'non-live task',
        project_path: tempRoot,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    const rawOutput = (result.metadata as any)?.rawOutput ?? result.content;
    const argvLine = rawOutput
      .trim()
      .split('\n')
      .find((line: string) => line.includes('"type": "argv"'));
    expect(argvLine).toBeTruthy();
    const argvPayload = JSON.parse(argvLine!);

    expect(argvPayload.args).not.toContain('--live');
  });

  it('forwards --live-manual and omits -p for promptless Pi live continue sessions', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-live-manual-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json, sys

print(json.dumps({"type": "argv", "args": sys.argv[1:]}))
print(json.dumps({"type": "agent_end", "result": "live manual ready"}))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
      outputRawJson: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: '',
        project_path: tempRoot,
        resume: 'resume-live-001',
        live: true,
        liveInteractiveSession: true,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    const rawOutput = (result.metadata as any)?.rawOutput ?? result.content;
    const lines = rawOutput.trim().split('\n');
    const argvLine = lines.find((l: string) => l.includes('"argv"'));
    expect(argvLine).toBeTruthy();
    const parsed = JSON.parse(argvLine!);

    expect(parsed.args).toContain('--live');
    expect(parsed.args).toContain('--live-manual');
    expect(parsed.args).toContain('--resume');
    expect(parsed.args).not.toContain('-p');
  });

  it('passes --fork to Pi service for clone requests and preserves returned clone session_id', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-clone-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'pi.py');
    const scriptContent = `#!/usr/bin/env python3
import json, sys

print(json.dumps({"type": "argv", "args": sys.argv[1:]}))
print(json.dumps({"type": "agent_end", "result": "cloned", "session_id": "clone-session-002"}))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
      outputRawJson: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: 'explore branch',
        project_path: tempRoot,
        resume: 'source-session-001',
        cloneSession: true,
        cloneFromSession: 'source-session-001',
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    const rawOutput = (result.metadata as any)?.rawOutput ?? result.content;
    const lines = rawOutput.trim().split('\n');
    const argvLine = lines.find((l: string) => l.includes('"argv"'));
    expect(argvLine).toBeTruthy();
    const argvPayload = JSON.parse(argvLine!);

    const forkIdx = argvPayload.args.indexOf('--fork');
    expect(forkIdx).toBeGreaterThanOrEqual(0);
    expect(argvPayload.args[forkIdx + 1]).toBe('source-session-001');
    expect(argvPayload.args).not.toContain('--resume');

    const structured = JSON.parse(result.content as string);
    expect(structured.result).toBe('cloned');
    expect(structured.session_id).toBe('clone-session-002');
  });

  it('passes --continue flag to Claude subagent when continueConversation is set', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-claude-cont-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'claude.py');
    const scriptContent = `#!/usr/bin/env python3
import json, sys

print(json.dumps({"type": "argv", "args": sys.argv[1:]}))
print(json.dumps({"type": "result", "result": "done"}))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'claude_subagent',
      arguments: {
        instruction: 'test task',
        project_path: tempRoot,
        continueConversation: true,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    // Claude structured output wraps content; use rawOutput from metadata to inspect argv
    const rawOutput = (result.metadata as any)?.rawOutput ?? result.content;
    const lines = rawOutput.trim().split('\n');
    const argvLine = lines.find((l: string) => l.includes('"argv"'));
    expect(argvLine).toBeTruthy();
    const parsed = JSON.parse(argvLine!);

    expect(parsed.args).toContain('--continue');
  });

  it('passes --resume flag with session ID to Claude subagent', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-claude-resume-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'claude.py');
    const scriptContent = `#!/usr/bin/env python3
import json, sys

print(json.dumps({"type": "argv", "args": sys.argv[1:]}))
print(json.dumps({"type": "result", "result": "resumed"}))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'claude_subagent',
      arguments: {
        instruction: 'test task',
        project_path: tempRoot,
        resume: 'session-abc-123',
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    const rawOutput = (result.metadata as any)?.rawOutput ?? result.content;
    const lines = rawOutput.trim().split('\n');
    const argvLine = lines.find((l: string) => l.includes('"argv"'));
    expect(argvLine).toBeTruthy();
    const parsed = JSON.parse(argvLine!);

    const resumeIdx = parsed.args.indexOf('--resume');
    expect(resumeIdx).toBeGreaterThanOrEqual(0);
    expect(parsed.args[resumeIdx + 1]).toBe('session-abc-123');
  });

  it('passes tool arguments to Claude subagent (--tools, --allowedTools, --disallowedTools)', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-claude-tools-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'claude.py');
    const scriptContent = `#!/usr/bin/env python3
import json, sys

print(json.dumps({"type": "argv", "args": sys.argv[1:]}))
print(json.dumps({"type": "result", "result": "ok"}))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'claude_subagent',
      arguments: {
        instruction: 'test task',
        project_path: tempRoot,
        tools: ['Bash', 'Edit'],
        allowedTools: ['Bash', 'Read', 'Write'],
        disallowedTools: ['NotebookEdit'],
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    const rawOutput = (result.metadata as any)?.rawOutput ?? result.content;
    const lines = rawOutput.trim().split('\n');
    const argvLine = lines.find((l: string) => l.includes('"argv"'));
    expect(argvLine).toBeTruthy();
    const parsed = JSON.parse(argvLine!);
    const args = parsed.args;

    // --tools should include Bash and Edit
    const toolsIdx = args.indexOf('--tools');
    expect(toolsIdx).toBeGreaterThanOrEqual(0);
    expect(args[toolsIdx + 1]).toBe('Bash');
    expect(args[toolsIdx + 2]).toBe('Edit');

    // --allowedTools should include Bash, Read, Write
    const allowedIdx = args.indexOf('--allowedTools');
    expect(allowedIdx).toBeGreaterThanOrEqual(0);

    // --disallowedTools should include NotebookEdit
    const disallowedIdx = args.indexOf('--disallowedTools');
    expect(disallowedIdx).toBeGreaterThanOrEqual(0);
    expect(args[disallowedIdx + 1]).toBe('NotebookEdit');
  });

  it('does not pass Pi-specific --cd flag to Claude subagent', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-claude-nocd-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'claude.py');
    const scriptContent = `#!/usr/bin/env python3
import json, sys

print(json.dumps({"type": "argv", "args": sys.argv[1:]}))
print(json.dumps({"type": "result", "result": "done"}))
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'claude_subagent',
      arguments: {
        instruction: 'test task',
        project_path: '/some/path',
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    const rawOutput = (result.metadata as any)?.rawOutput ?? result.content;
    const lines = rawOutput.trim().split('\n');
    const argvLine = lines.find((l: string) => l.includes('"argv"'));
    expect(argvLine).toBeTruthy();
    const parsed = JSON.parse(argvLine!);

    // Claude should NOT receive --cd (Pi-only flag)
    expect(parsed.args).not.toContain('--cd');
  });

  it('handles codex structured error output on failure', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-codex-err-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    const scriptPath = path.join(servicesDir, 'codex.py');
    const scriptContent = `#!/usr/bin/env python3
import json, sys

print(json.dumps({"type": "agent_message", "msg": {"type": "agent_message", "message": "Something went wrong"}}))
print("Error: API key invalid", file=sys.stderr)
sys.exit(1)
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'codex_subagent',
      arguments: {
        instruction: 'test task',
        project_path: tempRoot,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    expect(result.status).toBe('failed');

    const parsed = JSON.parse(result.content);
    expect(parsed.type).toBe('result');
    expect(parsed.is_error).toBe(true);
    expect(parsed.exit_code).toBe(1);

    const metadata = result.metadata as any;
    expect(metadata?.structuredOutput).toBe(true);
  });

  it('strips messages and type from Pi sub_agent_response to reduce token usage', async () => {
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'juno-shell-pi-sanitize-'));
    tempRoots.push(tempRoot);

    const servicesDir = path.join(tempRoot, 'services');
    await fs.ensureDir(servicesDir);

    // Create a pi.py that outputs a result event with nested sub_agent_response containing messages
    const scriptPath = path.join(servicesDir, 'pi.py');
    const captureEvent = {
      type: 'result',
      subtype: 'success',
      is_error: false,
      result: 'Hello from Pi agent',
      usage: {
        input: 64,
        output: 137,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens: 201,
        cost: {
          input: 0.000128,
          output: 0.000274,
          cacheRead: 0,
          cacheWrite: 0,
          total: 0.000402,
        },
      },
      sub_agent_response: {
        type: 'agent_end',
        messages: [
          { role: 'user', content: [{ type: 'text', text: 'test prompt' }], timestamp: 123 },
          {
            role: 'assistant',
            content: [
              { type: 'thinking', thinking: 'long thinking content...' },
              { type: 'text', text: 'Hello from Pi agent' },
            ],
            api: 'openai-completions',
            provider: 'zai',
            model: 'glm-5',
            usage: { input: 64, output: 137 },
            stopReason: 'stop',
            timestamp: 456,
          },
        ],
      },
    };
    const scriptContent = `#!/usr/bin/env python3
import json, sys, os

capture_path = os.environ.get("JUNO_SUBAGENT_CAPTURE_PATH")
event = ${JSON.stringify(JSON.stringify(captureEvent))}
print(event)
if capture_path:
    with open(capture_path, "w") as f:
        f.write(event)
`;
    await fs.writeFile(scriptPath, scriptContent, { mode: 0o755 });

    const backend = new ShellBackend();
    backend.configure({
      workingDirectory: tempRoot,
      servicesPath: servicesDir,
      enableJsonStreaming: true,
    });
    await backend.initialize();

    const request: ToolCallRequest = {
      toolName: 'pi_subagent',
      arguments: {
        instruction: 'test task',
        model: 'zai/glm-5',
        project_path: tempRoot,
      },
      timeout: 15000,
      priority: 'normal',
      metadata: {
        sessionId: 'test-session',
        iterationNumber: 1,
      },
    };

    const result = await backend.execute(request);

    expect(result.status).toBe('completed');

    const parsed = JSON.parse(result.content);
    expect(parsed.type).toBe('result');
    expect(parsed.is_error).toBe(false);
    expect(parsed.result).toBe('Hello from Pi agent');
    expect(parsed.usage).toEqual(captureEvent.usage);
    expect(parsed.total_cost_usd).toBeCloseTo(0.000402, 10);

    // sub_agent_response should exist but without messages array
    expect(parsed.sub_agent_response).toBeDefined();
    expect(parsed.sub_agent_response.messages).toBeUndefined();

    // Inner sub_agent_response (from pi.py) should have messages and type stripped
    if (parsed.sub_agent_response.sub_agent_response) {
      expect(parsed.sub_agent_response.sub_agent_response.messages).toBeUndefined();
      expect(parsed.sub_agent_response.sub_agent_response.type).toBeUndefined();
    }
  });

  it('bounds retained Pi stdout so large provider logs do not grow the Node heap', async () => {
    const { servicesDir, workingDir } = await createStubPiLargeOutputService();
    const previousCaptureLimit = process.env.JUNO_SHELL_CAPTURE_LIMIT_BYTES;
    const previousStreamLimit = process.env.JUNO_SHELL_STREAM_BUFFER_LIMIT_BYTES;
    process.env.JUNO_SHELL_CAPTURE_LIMIT_BYTES = '8192';
    process.env.JUNO_SHELL_STREAM_BUFFER_LIMIT_BYTES = '8192';

    try {
      const backend = new ShellBackend();
      backend.configure({
        workingDirectory: workingDir,
        servicesPath: servicesDir,
        enableJsonStreaming: true,
      });
      await backend.initialize();

      const result = await backend.execute({
        toolName: 'pi_subagent',
        arguments: {
          instruction: 'emit large output',
          project_path: workingDir,
        },
        timeout: 15000,
        priority: 'normal',
        metadata: {
          sessionId: 'test-session',
          iterationNumber: 1,
        },
      });

      expect(result.status).toBe('completed');
      const parsed = JSON.parse(result.content);
      expect(parsed.result).toBe('large-output-done');
      expect(parsed.session_id).toBe('pi-large-session');

      const metadata = result.metadata as any;
      expect(Buffer.byteLength(metadata.rawOutput, 'utf8')).toBeLessThanOrEqual(8192);
      expect(metadata.rawOutput).toContain('large-output-done');
    } finally {
      if (previousCaptureLimit === undefined) delete process.env.JUNO_SHELL_CAPTURE_LIMIT_BYTES;
      else process.env.JUNO_SHELL_CAPTURE_LIMIT_BYTES = previousCaptureLimit;
      if (previousStreamLimit === undefined) delete process.env.JUNO_SHELL_STREAM_BUFFER_LIMIT_BYTES;
      else process.env.JUNO_SHELL_STREAM_BUFFER_LIMIT_BYTES = previousStreamLimit;
    }
  });
});
