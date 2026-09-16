#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const metadata = path.join(root, 'src/templates/skills/argument-contracts.json');
const canonicalRoot = path.resolve(root, '../yylo-skills');
const expected = [
  'artifact-yylo',
  'ledger-tasks-yylo',
  'plan-ledger-tasks-yylo',
  'ralph-loop-yylo',
  'understand-project-yylo',
  'wiki-yylo',
  'workflow-yylo',
];
const contract = JSON.parse(fs.readFileSync(metadata, 'utf8'));
const names = Object.keys(contract.skills ?? {}).sort();
if (names.join('\0') !== expected.join('\0')) {
  throw new Error(`skill argument metadata must name exactly the canonical seven skills: ${names.join(', ')}`);
}
if (fs.existsSync(path.join(canonicalRoot, 'skills'))) {
  for (const slug of expected) {
    const text = fs.readFileSync(path.join(canonicalRoot, 'skills', slug, 'SKILL.md'), 'utf8');
    if (!text.includes(`\nname: ${slug}\n`)) throw new Error(`canonical skill name mismatch: ${slug}`);
    const declared = contract.skills[slug].placeholders;
    for (const [placeholder, count] of Object.entries(declared)) {
      const actual = text.split(placeholder).length - 1;
      if (actual !== count) throw new Error(`${slug} placeholder ${placeholder}: expected ${count}, got ${actual}`);
    }
  }
}
console.log('skill package boundary: seven-skill invocation metadata validated');
