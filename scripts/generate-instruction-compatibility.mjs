#!/usr/bin/env node
// One authored policy; Python embeds it so installed scripts need no extra loader.
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const policy = JSON.parse(readFileSync(new URL('../src/templates/instruction-bundle-compatibility.json', import.meta.url), 'utf8'));
const begin = '# BEGIN GENERATED INSTRUCTION COMPATIBILITY POLICY';
const end = '# END GENERATED INSTRUCTION COMPATIBILITY POLICY';
const replacement = `${begin}\nINSTRUCTION_COMPATIBILITY = ${JSON.stringify(policy)}\n${end}`;
for (const relative of ['../src/templates/scripts/task_workspace.py', '../../.juno_task/scripts/task_workspace.py']) {
  const file = fileURLToPath(new URL(relative, import.meta.url));
  const source = readFileSync(file, 'utf8');
  const start = source.indexOf(begin);
  const finish = source.indexOf(end, start);
  if (start < 0 || finish < 0) throw new Error(`Missing generated policy boundary: ${file}`);
  const generated = source.slice(0, start) + replacement + source.slice(finish + end.length);
  if (process.argv.includes('--check')) {
    if (source !== generated) throw new Error(`Stale instruction compatibility binding: ${file}`);
  } else writeFileSync(file, generated);
}
