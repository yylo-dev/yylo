/**
 * Tests for juno-skill-preprocessor Pi extension.
 *
 * Tests the exported utility functions (substituteArgs, processShellDirectives,
 * parseCommandArgs, parseFrontmatter, findSkillFile) that power the extension.
 *
 * Why: The preprocessor is the bridge between Claude-style dynamic skills and Pi's
 * static skill expansion. Without it, Pi skills lose variable substitution and
 * shell directive execution — breaking write-once, deploy-everywhere skills.
 */
import { execSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Import the preprocessor functions directly from the template source
import junoSkillPreprocessor, {
  createDirectiveArgumentNamespace,
  expandSkillInvocation,
  findSkillFile,
  parseCommandArgs,
  parseFrontmatter,
  parseHeredocDeclarations,
  processShellDirectives,
  substituteArgs,
} from '../../templates/extensions/pi/juno-skill-preprocessor.js';

// ---------------------------------------------------------------------------
// substituteArgs
// ---------------------------------------------------------------------------
describe('substituteArgs', () => {
  it('should substitute $1 and $2 with positional args', () => {
    expect(substituteArgs('Hello $1, welcome to $2', ['Alice', 'Wonderland'])).toBe(
      'Hello Alice, welcome to Wonderland',
    );
  });

  it('should replace unmatched positional args with empty string', () => {
    expect(substituteArgs('$1 and $2 and $3', ['only-one'])).toBe('only-one and  and ');
  });

  it('should substitute $ARGUMENTS with all args joined', () => {
    expect(substituteArgs('Args: $ARGUMENTS', ['a', 'b', 'c'])).toBe('Args: a b c');
  });

  it('should substitute $@ with all args joined', () => {
    expect(substituteArgs('All: $@', ['x', 'y'])).toBe('All: x y');
  });

  it('should substitute ${@:N} — args from Nth position', () => {
    expect(substituteArgs('Rest: ${@:2}', ['a', 'b', 'c', 'd'])).toBe('Rest: b c d');
  });

  it('should substitute ${@:N:L} — L args from position N', () => {
    expect(substituteArgs('Slice: ${@:2:2}', ['a', 'b', 'c', 'd'])).toBe('Slice: b c');
  });

  it('should handle ${@:0} as ${@:1} (bash convention)', () => {
    expect(substituteArgs('${@:0}', ['a', 'b'])).toBe('a b');
  });

  it('should handle empty args', () => {
    expect(substituteArgs('$1 $ARGUMENTS', [])).toBe(' ');
  });

  it('should handle no placeholders', () => {
    expect(substituteArgs('plain text', ['arg'])).toBe('plain text');
  });

  it('should handle multiple occurrences of the same variable', () => {
    expect(substituteArgs('$1 then $1 again', ['hello'])).toBe('hello then hello again');
  });

  it('should not re-substitute values containing $ patterns', () => {
    // If $1 resolves to "$2", that "$2" should NOT be substituted again
    expect(substituteArgs('$1 $2', ['$2', 'world'])).toBe('$2 world');
  });
});

// ---------------------------------------------------------------------------
// parseCommandArgs
// ---------------------------------------------------------------------------
describe('parseCommandArgs', () => {
  it('should split simple space-separated args', () => {
    expect(parseCommandArgs('hello world')).toEqual(['hello', 'world']);
  });

  it('should handle double-quoted strings', () => {
    expect(parseCommandArgs('"hello world" foo')).toEqual(['hello world', 'foo']);
  });

  it('should handle single-quoted strings', () => {
    expect(parseCommandArgs("'hello world' bar")).toEqual(['hello world', 'bar']);
  });

  it('should handle escaped characters', () => {
    expect(parseCommandArgs('hello\\ world')).toEqual(['hello world']);
  });

  it('should handle empty input', () => {
    expect(parseCommandArgs('')).toEqual([]);
    expect(parseCommandArgs('   ')).toEqual([]);
  });

  it('should handle mixed quotes', () => {
    expect(parseCommandArgs('"one two" \'three four\' five')).toEqual([
      'one two',
      'three four',
      'five',
    ]);
  });

  it('should handle multiple spaces between args', () => {
    expect(parseCommandArgs('a   b    c')).toEqual(['a', 'b', 'c']);
  });

  it('treats all whitespace as boundaries while preserving quoted multiline values', () => {
    expect(parseCommandArgs('one\n"two\nlines"\tthree')).toEqual(['one', 'two\nlines', 'three']);
  });

  it('preserves empty quoted arguments and trailing backslashes', () => {
    expect(parseCommandArgs(`'' "" literal\\`)).toEqual(['', '', 'literal\\']);
  });
});

// ---------------------------------------------------------------------------
// parseFrontmatter
// ---------------------------------------------------------------------------
describe('parseFrontmatter', () => {
  it('should parse YAML frontmatter', () => {
    const content = '---\nname: test-skill\ndescription: A test\n---\nBody content here';
    const { frontmatter, body } = parseFrontmatter(content);
    expect(frontmatter.name).toBe('test-skill');
    expect(frontmatter.description).toBe('A test');
    expect(body).toBe('Body content here');
  });

  it('should parse boolean values', () => {
    const content = '---\nenable-shell-directives: true\ndisabled: false\n---\nBody';
    const { frontmatter } = parseFrontmatter(content);
    expect(frontmatter['enable-shell-directives']).toBe(true);
    expect(frontmatter['disabled']).toBe(false);
  });

  it('should handle content without frontmatter', () => {
    const content = 'Just plain body text';
    const { frontmatter, body } = parseFrontmatter(content);
    expect(frontmatter).toEqual({});
    expect(body).toBe('Just plain body text');
  });

  it('should handle empty body after frontmatter', () => {
    const content = '---\nname: empty\n---\n';
    const { frontmatter, body } = parseFrontmatter(content);
    expect(frontmatter.name).toBe('empty');
    expect(body).toBe('');
  });

  it('should handle multi-line body', () => {
    const content = '---\nname: multi\n---\nLine 1\nLine 2\nLine 3';
    const { body } = parseFrontmatter(content);
    expect(body).toBe('Line 1\nLine 2\nLine 3');
  });
});

// ---------------------------------------------------------------------------
// processShellDirectives
// ---------------------------------------------------------------------------
describe('processShellDirectives', () => {
  it('should execute simple commands', () => {
    const result = processShellDirectives('Output: !`echo hello`', process.cwd());
    expect(result).toBe('Output: hello');
  });

  it('should handle multiple directives', () => {
    const result = processShellDirectives('!`echo a` and !`echo b`', process.cwd());
    expect(result).toBe('a and b');
  });

  it('should handle failed commands gracefully', () => {
    const result = processShellDirectives('!`nonexistent_command_xyz_12345`', process.cwd());
    expect(result).toBe('[Error executing: nonexistent_command_xyz_12345]');
  });

  it('should not process text without shell directives', () => {
    const text = 'Plain text with `code blocks` and backticks';
    expect(processShellDirectives(text, process.cwd())).toBe(text);
  });

  it('should trim output', () => {
    const result = processShellDirectives('!`echo "  spaced  "`', process.cwd());
    expect(result).toBe('spaced');
  });

  it('should handle command with pipes', () => {
    const result = processShellDirectives('!`echo "hello world" | tr "h" "H"`', process.cwd());
    expect(result).toBe('Hello world');
  });

  it('should respect timeout', () => {
    // Use a very short timeout with a sleeping command
    const result = processShellDirectives('!`sleep 10`', process.cwd(), 100);
    expect(result).toBe('[Error executing: sleep 10]');
  });
});

// ---------------------------------------------------------------------------
// findSkillFile
// ---------------------------------------------------------------------------
describe('findSkillFile', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(process.env.TMPDIR || '/tmp', 'skill-test-'));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('should find skill in .pi/skills/{name}/SKILL.md', () => {
    const skillDir = path.join(tmpDir, '.pi', 'skills', 'test-skill');
    fs.mkdirSync(skillDir, { recursive: true });
    fs.writeFileSync(path.join(skillDir, 'SKILL.md'), '---\nname: test-skill\n---\nBody');

    const result = findSkillFile('test-skill', tmpDir);
    expect(result).toBe(path.join(skillDir, 'SKILL.md'));
  });

  it('should find skill in .claude/skills/{name}/SKILL.md', () => {
    const skillDir = path.join(tmpDir, '.claude', 'skills', 'my-skill');
    fs.mkdirSync(skillDir, { recursive: true });
    fs.writeFileSync(path.join(skillDir, 'SKILL.md'), '---\nname: my-skill\n---\nBody');

    const result = findSkillFile('my-skill', tmpDir);
    expect(result).toBe(path.join(skillDir, 'SKILL.md'));
  });

  it('should find .md file directly in skill dir', () => {
    const skillDir = path.join(tmpDir, '.pi', 'skills');
    fs.mkdirSync(skillDir, { recursive: true });
    fs.writeFileSync(path.join(skillDir, 'direct-skill.md'), '---\nname: direct-skill\n---\nBody');

    const result = findSkillFile('direct-skill', tmpDir);
    expect(result).toBe(path.join(skillDir, 'direct-skill.md'));
  });

  it('should prefer .pi/skills over .claude/skills', () => {
    // Create in both locations
    const piDir = path.join(tmpDir, '.pi', 'skills', 'dual-skill');
    const claudeDir = path.join(tmpDir, '.claude', 'skills', 'dual-skill');
    fs.mkdirSync(piDir, { recursive: true });
    fs.mkdirSync(claudeDir, { recursive: true });
    fs.writeFileSync(path.join(piDir, 'SKILL.md'), 'pi version');
    fs.writeFileSync(path.join(claudeDir, 'SKILL.md'), 'claude version');

    const result = findSkillFile('dual-skill', tmpDir);
    expect(result).toBe(path.join(piDir, 'SKILL.md'));
  });

  it('should return null for unknown skill', () => {
    expect(findSkillFile('nonexistent-skill', tmpDir)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Integration: full skill preprocessing flow
// ---------------------------------------------------------------------------
describe('skill preprocessing integration', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(process.env.TMPDIR || '/tmp', 'skill-integ-'));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('should substitute variables and process shell directives end-to-end', () => {
    // Create a skill with variables and shell directives
    const skillDir = path.join(tmpDir, '.pi', 'skills', 'test-skill');
    fs.mkdirSync(skillDir, { recursive: true });
    fs.writeFileSync(
      path.join(skillDir, 'SKILL.md'),
      [
        '---',
        'name: test-skill',
        'description: Test skill',
        'enable-shell-directives: true',
        '---',
        '',
        '## Task: $1',
        '',
        'Current dir: !`echo test-output`',
        '',
        'All args: $ARGUMENTS',
      ].join('\n'),
    );

    // Find the skill
    const skillPath = findSkillFile('test-skill', tmpDir);
    expect(skillPath).not.toBeNull();

    // Read and parse
    const content = fs.readFileSync(skillPath!, 'utf-8');
    const { frontmatter, body } = parseFrontmatter(content);

    // Substitute args
    const args = parseCommandArgs('"build the app" "no breaking changes"');
    let processed = substituteArgs(body.trim(), args);

    // Process shell directives
    if (frontmatter['enable-shell-directives'] === true) {
      processed = processShellDirectives(processed, tmpDir);
    }

    expect(processed).toContain('## Task: build the app');
    expect(processed).toContain('Current dir: test-output');
    expect(processed).toContain('All args: build the app no breaking changes');
  });

  it('should NOT process shell directives when frontmatter opt-in is missing', () => {
    const skillDir = path.join(tmpDir, '.pi', 'skills', 'no-shell');
    fs.mkdirSync(skillDir, { recursive: true });
    fs.writeFileSync(
      path.join(skillDir, 'SKILL.md'),
      [
        '---',
        'name: no-shell',
        'description: No shell directives',
        '---',
        '',
        '!`echo should-not-run`',
      ].join('\n'),
    );

    const content = fs.readFileSync(path.join(skillDir, 'SKILL.md'), 'utf-8');
    const { frontmatter, body } = parseFrontmatter(content);

    let processed = body.trim();

    // Shell directives should NOT be processed
    if (frontmatter['enable-shell-directives'] === true) {
      processed = processShellDirectives(processed, tmpDir);
    }

    expect(processed).toBe('!`echo should-not-run`');
  });

  it('should handle skills with no arguments gracefully', () => {
    const content = '---\nname: simple\n---\nNo variables here, just text.';
    const { body } = parseFrontmatter(content);
    const processed = substituteArgs(body.trim(), []);
    expect(processed).toBe('No variables here, just text.');
  });
});

describe('directive environment namespace', () => {
  it('retries inherited and authored collisions before selecting an absent namespace', () => {
    const tokens = ['inherited', 'authored', 'safe'];
    const inherited = { JUNO_SKILL_ARGUMENT_inherited_0: 'sentinel' };
    const command = 'printf %s "$JUNO_SKILL_ARGUMENT_authored_0"';
    const namespace = createDirectiveArgumentNamespace(command, inherited, () => tokens.shift()!);
    expect(namespace).toBe('JUNO_SKILL_ARGUMENT_safe_');
    expect(command).not.toContain(namespace);
    expect(Object.keys(inherited).some((key) => key.startsWith(namespace))).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Pi input-handler parity and consumption contract
// ---------------------------------------------------------------------------
describe('Pi input handler argument preservation', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(process.env.TMPDIR || '/tmp', 'skill-handler-'));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  function skill(name: string, body: string, shell = false): void {
    const dir = path.join(tmpDir, '.pi', 'skills', name);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(
      path.join(dir, 'SKILL.md'),
      `---\nname: ${name}\n${shell ? 'enable-shell-directives: true\n' : ''}---\n${body}`,
    );
  }

  it.each([
    ['$1', 'one "two words" three', 'one', '"two words" three'],
    ['$ARGUMENTS', 'one  "two words"', 'one  "two words"', ''],
    ['$@', `one\n@@no_code`, `one\n@@no_code`, ''],
    ['${@:2}', 'one two three', 'two three', 'one'],
    ['${@:2:1}', 'one "two words" three', 'two words', 'one three'],
    ['$1 / ${@:3:1}', 'one two three four', 'one / three', 'two four'],
  ])(
    'substitutes %s and appends only unconsumed raw arguments',
    (placeholder, raw, inBody, remaining) => {
      skill('consume', `Value: ${placeholder}`);
      const expanded = expandSkillInvocation(`/skill:consume ${raw}`, tmpDir)!;
      expect(expanded).toContain(`Value: ${inBody}`);
      const suffix = expanded.split('</skill>')[1] ?? '';
      expect(suffix).toBe(remaining ? `\n\n${remaining}` : '');
    },
  );

  it('honors intentional repeated and overlapping placeholders without a runtime append', () => {
    skill('explicit', '$1 again $1\nComplete: $ARGUMENTS');
    const expanded = expandSkillInvocation('/skill:explicit alpha beta', tmpDir)!;
    expect(expanded).toContain('alpha again alpha\nComplete: alpha beta');
    expect(expanded.endsWith('</skill>')).toBe(true);
  });

  it('preserves the multiline ypl payload after shortcut rewriting byte-for-byte', async () => {
    skill('ralph-loop-yylo', 'Instructions only');
    const heredoc = '## oD5g4o\nWhat is the root cause of 504\n@@no_code';
    const rewritten = `/skill:ralph-loop-yylo ${heredoc}`;
    expect(expandSkillInvocation(rewritten, tmpDir)!.split('</skill>\n\n')[1]).toBe(heredoc);

    let handler: ((event: { text: string }) => unknown) | undefined;
    const pi = {
      on: vi.fn((_event: string, callback: typeof handler) => {
        handler = callback;
      }),
    };
    const cwd = vi.spyOn(process, 'cwd').mockReturnValue(tmpDir);
    try {
      junoSkillPreprocessor(pi as never);
      expect(pi.on).toHaveBeenCalledWith('input', expect.any(Function));
      expect(handler!({ text: rewritten })).toEqual({
        action: 'transform',
        text: expandSkillInvocation(rewritten, tmpDir),
      });
    } finally {
      cwd.mockRestore();
    }
  });

  it('substitutes positional placeholders inside opted-in authored directives before execution', () => {
    skill('directive-position', 'Result: !`printf %s "$1"`', true);
    const expanded = expandSkillInvocation('/skill:directive-position hello tail', tmpDir)!;
    expect(expanded).toContain('Result: hello\n</skill>');
    expect(expanded.endsWith('</skill>\n\ntail')).toBe(true);
  });

  it('does not overwrite inherited predictable sentinels and keeps multiple placeholders distinct', () => {
    const inheritedName = 'JUNO_SKILL_ARGUMENT_0';
    const previous = process.env[inheritedName];
    process.env[inheritedName] = 'inherited-sentinel';
    skill(
      'namespace-collision',
      `Result: !\`printf '%s|%s|%s' "$${inheritedName}" "$1" "$2"\``,
      true,
    );
    try {
      const expanded = expandSkillInvocation(
        '/skill:namespace-collision "first value" "second value"',
        tmpDir,
      )!;
      expect(expanded).toContain('Result: inherited-sentinel|first value|second value');
      expect(expanded.endsWith('</skill>')).toBe(true);
      expect(process.env[inheritedName]).toBe('inherited-sentinel');
    } finally {
      if (previous === undefined) delete process.env[inheritedName];
      else process.env[inheritedName] = previous;
    }
  });

  it('passes hostile values through the environment in every supported authored shell context', () => {
    const marker = path.join(tmpDir, 'shell-context-marker');
    const contexts = {
      unquoted: 'Result: !`printf %s $1`',
      single: "Result: !`printf %s '$1'`",
      double: 'Result: !`printf %s "$1"`',
      heredoc: 'Result: !`cat <<EOF\n$1\nEOF`',
    };
    const values = [
      `$(touch ${marker})`,
      `\`touch ${marker}\``,
      'dollar-$HOME',
      'back\\slash',
      "apostrophe-'",
      `line one\n$(touch ${marker})\nline three`,
    ];

    for (const [context, body] of Object.entries(contexts)) {
      values.forEach((value, valueIndex) => {
        const name = `safe-${context}-${valueIndex}`;
        skill(name, body, true);
        const quotedArgument = `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
        const expanded = expandSkillInvocation(`/skill:${name} ${quotedArgument}`, tmpDir)!;
        expect(expanded, `${context}: ${JSON.stringify(value)}`).toContain(`Result: ${value}`);
        expect(expanded.endsWith('</skill>')).toBe(true);
        expect(fs.existsSync(marker)).toBe(false);
      });
    }
  });

  it('keeps command-substitution-looking bytes inert in the reported heredoc reproduction', () => {
    const marker = path.join(tmpDir, 'heredoc-marker');
    skill('heredoc-reproduction', 'Result: !`cat <<EOF\n$1\nEOF`', true);
    const value = `before\n$(touch ${marker})\nafter`;
    const expanded = expandSkillInvocation(`/skill:heredoc-reproduction "${value}"`, tmpDir)!;
    expect(expanded).toContain(`Result: ${value}`);
    expect(expanded.endsWith('</skill>')).toBe(true);
    expect(fs.existsSync(marker)).toBe(false);
  });

  it.each([
    ['quoted', "<<'EOF'", 'EOF'],
    ['double-quoted', '<<"EOF"', 'EOF'],
    ['escaped', '<<\\EOF', 'EOF'],
    ['partial-escaped', '<<E\\OF', 'EOF'],
    ['partial-double', '<<E"OF"', 'EOF'],
    ['partial-single', "<<'E'OF", 'EOF'],
    ['combined', String.raw`<<'E'\O"F"`, 'EOF'],
    ['empty-single', "<<''", ''],
    ['empty-double', '<<""', ''],
  ])(
    'fails safely before executing placeholder-bearing %s non-expanding heredocs',
    (_kind, declaration, terminator) => {
      expect(parseHeredocDeclarations(`cat ${declaration}`)).toEqual([
        { delimiter: terminator, stripTabs: false, expands: false },
      ]);
      const marker = path.join(tmpDir, `non-expanding-${_kind}-marker`);
      skill(
        `non-expanding-${_kind}`,
        `Before: !\`touch ${marker}\`\nResult: !\`cat ${declaration}\n$1\n${terminator}\``,
        true,
      );
      const expanded = expandSkillInvocation(
        `/skill:non-expanding-${_kind} "safe payload"`,
        tmpDir,
      )!;
      expect(expanded).toContain(
        '[Error: argument placeholders inside quoted or escaped-delimiter heredocs are unsupported; directive was not executed]',
      );
      expect(expanded.match(/safe payload/g)).toHaveLength(1);
      expect(fs.existsSync(marker)).toBe(false);
    },
  );

  it.each([
    ['left-shift', `printf '%s' $((1 << 2))-$1`, '4-hello'],
    ['nested', `printf '%s' $(((1 << 2) + (8 >> 1)))-$1`, '8-hello'],
    ['double-quoted', `printf '%s' "$((2 << 3))-$1"`, '16-hello'],
  ])('ignores arithmetic shift operators in %s arithmetic expansion', (_kind, command, output) => {
    skill(`arithmetic-${_kind}`, `Result: !\`${command}\``, true);
    const expanded = expandSkillInvocation(`/skill:arithmetic-${_kind} hello`, tmpDir)!;
    expect(expanded).toContain(`Result: ${output}`);
    expect(expanded.endsWith('</skill>')).toBe(true);
    expect(expanded).not.toContain('[Error:');
  });

  it('executes placeholder-free arithmetic and preserves raw arguments exactly once', () => {
    skill('arithmetic-no-placeholder', `Result: !\`printf '%s' $((1 << 2))\``, true);
    const expanded = expandSkillInvocation('/skill:arithmetic-no-placeholder hello', tmpDir)!;
    expect(expanded).toContain('Result: 4');
    expect(expanded.match(/hello/g)).toHaveLength(1);
    expect(expanded.endsWith('</skill>\n\nhello')).toBe(true);
    expect(expanded).not.toContain('[Error:');
  });

  it.each([
    ['benign', '7319', `$(( $1 << 2 ))`],
    ['hostile', '$(touch ARITHMETIC_HOSTILE_MARKER)', `"$(( $1 << 2 ))"`],
  ])(
    'refuses %s placeholders inside arithmetic before any directive executes',
    (_kind, value, expression) => {
      const preflightMarker = path.join(tmpDir, `arithmetic-${_kind}-preflight-marker`);
      const hostileMarker = path.join(tmpDir, 'arithmetic-hostile-marker');
      const raw = value.replace('ARITHMETIC_HOSTILE_MARKER', hostileMarker);
      skill(
        `arithmetic-placeholder-${_kind}`,
        `Before: !\`touch ${preflightMarker}\`\nResult: !\`printf '%s' ${expression}\``,
        true,
      );
      const expanded = expandSkillInvocation(
        `/skill:arithmetic-placeholder-${_kind} "${raw}"`,
        tmpDir,
      )!;
      expect(expanded).toContain(
        '[Error: argument placeholders inside shell arithmetic expansions are unsupported; directive was not executed]',
      );
      expect(
        expanded.match(new RegExp(raw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g')),
      ).toHaveLength(1);
      expect(fs.existsSync(preflightMarker)).toBe(false);
      expect(fs.existsSync(hostileMarker)).toBe(false);
    },
  );

  it('refuses an unterminated arithmetic placeholder before any directive executes', () => {
    const preflightMarker = path.join(tmpDir, 'open-arithmetic-preflight-marker');
    const hostileMarker = path.join(tmpDir, 'open-arithmetic-hostile-marker');
    const raw = `$(touch ${hostileMarker})`;
    skill(
      'open-arithmetic-placeholder',
      `Before: !\`touch ${preflightMarker}\`\nResult: !\`printf '%s' "$(( $1 << 2)"\``,
      true,
    );
    const expanded = expandSkillInvocation(`/skill:open-arithmetic-placeholder "${raw}"`, tmpDir)!;
    expect(expanded).toContain(
      '[Error: argument placeholders inside shell arithmetic expansions are unsupported; directive was not executed]',
    );
    expect(
      expanded.match(new RegExp(raw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g')),
    ).toHaveLength(1);
    expect(fs.existsSync(preflightMarker)).toBe(false);
    expect(fs.existsSync(hostileMarker)).toBe(false);
  });

  it.each([
    ['single-quoted', `printf '%s' 'lookalike <<E"OF": $1'`, 'lookalike <<E"OF": payload'],
    ['double-quoted', `printf '%s' "lookalike <<'EOF': $1"`, "lookalike <<'EOF': payload"],
    ['escaped', `printf '%s' \\<\\<EOF; printf '%s' "$1"`, '<<EOFpayload'],
    ['comment', `printf '%s' "$1" # <<E\\OF`, 'payload'],
  ])('ignores %s heredoc operator lookalikes', (_kind, command, output) => {
    skill(`lookalike-${_kind}`, `Result: !\`${command}\``, true);
    const expanded = expandSkillInvocation(`/skill:lookalike-${_kind} payload`, tmpDir)!;
    expect(expanded).toContain(`Result: ${output}`);
    expect(expanded).not.toContain('[Error:');
  });

  it.each([
    ['single-quote', `printf '%s' '$1`],
    ['double-quote', `printf '%s' "$1`],
    ['unterminated-heredoc', `cat <<EOF\n$1`],
  ])('refuses malformed %s source before any directive executes', (_kind, command) => {
    const marker = path.join(tmpDir, `malformed-${_kind}-marker`);
    skill(`malformed-${_kind}`, `Before: !\`touch ${marker}\`\nResult: !\`${command}\``, true);
    const expanded = expandSkillInvocation(`/skill:malformed-${_kind} "safe payload"`, tmpDir)!;
    expect(expanded).toContain(
      '[Error: malformed or unterminated shell quoting/heredoc; directive was not executed]',
    );
    expect(expanded.match(/safe payload/g)).toHaveLength(1);
    expect(fs.existsSync(marker)).toBe(false);
  });

  it('combines authored directives with positional/all placeholders safely through the registered handler', () => {
    const marker = path.join(tmpDir, 'argument-was-executed');
    skill(
      'directive-combined',
      'Position: !`printf %s "$1"`\nRequest: !`printf %s "$ARGUMENTS"`',
      true,
    );
    const raw = `"$(touch ${marker})" "two words" !\`touch ${marker}\``;
    const invocation = `/skill:directive-combined ${raw}`;
    const expected = expandSkillInvocation(invocation, tmpDir)!;
    expect(expected).toContain(`Position: $(touch ${marker})`);
    expect(expected).toContain(`Request: ${raw}`);
    expect(expected.endsWith('</skill>')).toBe(true);
    expect(fs.existsSync(marker)).toBe(false);

    let handler: ((event: { text: string }) => unknown) | undefined;
    const pi = {
      on: vi.fn((_event: string, callback: typeof handler) => {
        handler = callback;
      }),
    };
    const cwd = vi.spyOn(process, 'cwd').mockReturnValue(tmpDir);
    try {
      junoSkillPreprocessor(pi as never);
      expect(handler!({ text: invocation })).toEqual({ action: 'transform', text: expected });
      expect(fs.existsSync(marker)).toBe(false);
    } finally {
      cwd.mockRestore();
    }
  });

  it('never recognizes an argument-supplied directive after all-argument substitution', () => {
    const marker = path.join(tmpDir, 'injected-directive-ran');
    skill('directive-injection', 'Request: $ARGUMENTS', true);
    const raw = `literal !\`touch ${marker}\` $(touch ${marker})`;
    const expanded = expandSkillInvocation(`/skill:directive-injection ${raw}`, tmpDir)!;
    expect(expanded).toContain(`Request: ${raw}`);
    expect(expanded.endsWith('</skill>')).toBe(true);
    expect(fs.existsSync(marker)).toBe(false);
  });

  it('keeps shell directives literal unless opted in and never executes argument text', () => {
    skill('plain-shell', 'Directive: !`printf body`');
    const literal = '$(printf argument) !`echo injected` `echo nope` $HOME \\ path';
    const plain = expandSkillInvocation(`/skill:plain-shell ${literal}`, tmpDir)!;
    expect(plain).toContain('Directive: !`printf body`');
    expect(plain.split('</skill>\n\n')[1]).toBe(literal);

    skill('opted-shell', 'Directive: !`printf body`', true);
    const expanded = expandSkillInvocation(`/skill:opted-shell ${literal}`, tmpDir)!;
    expect(expanded).toContain('Directive: body\n</skill>');
    expect(expanded).toContain('!`echo injected`');
    expect(expanded.endsWith(literal)).toBe(true);
  });
});
