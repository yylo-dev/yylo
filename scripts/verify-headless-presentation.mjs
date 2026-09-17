#!/usr/bin/env node
// Run after npm run build. Tests actual packaged bytes with no site-packages.
import { execFileSync } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const relative = 'templates/services';
const source = path.join(root, 'src', relative);
const built = path.join(root, 'dist', relative);
const manifest = JSON.parse(readFileSync(path.join(source, 'vendor/pygments.json'), 'utf8'));
for (const file of ['headless_presentation.py', `vendor/${manifest.file}`, 'vendor/PYGMENTS-LICENSE', 'vendor/pygments.json']) {
  const expected = readFileSync(path.join(source, file));
  const actual = readFileSync(path.join(built, file));
  if (!expected.equals(actual)) throw new Error(`Built presentation asset differs: ${file}`);
}
const digest = createHash('sha256').update(readFileSync(path.join(built, 'vendor', manifest.file))).digest('hex');
if (digest !== manifest.sha256) throw new Error('Bundled lexer checksum mismatch');
if (!existsSync(path.join(built, 'pi.py'))) throw new Error('Missing built Pi service');
execFileSync('python3', ['-S', '-c', `
import headless_presentation as p
from pi import PiService
for language, text in [('python', 'return 42'), ('typescript', 'const count: number = 42;')]:
    result = p.highlight(text, language)
    assert '\\x1b[' in result, language
    assert p.sanitize(result) == text
print('Packaged presentation and bundled lexers: OK (no site-packages)')
`], { cwd: built, stdio: 'inherit' });
