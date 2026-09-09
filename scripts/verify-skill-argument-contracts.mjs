#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const templates = path.join(root, 'src/templates/skills');
const expected = ['kanban-workflow', 'plan-kanban-tasks', 'ralph-loop', 'understand-project'];
const contract = JSON.parse(fs.readFileSync(path.join(templates, 'argument-contracts.json'), 'utf8'));
const names = Object.keys(contract.skills ?? contract).sort();
if (names.join('\0') !== expected.sort().join('\0')) {
  throw new Error(`skill argument metadata must name exactly the canonical four skills: ${names.join(', ')}`);
}
const payloads = [];
const walk = (dir) => {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const absolute = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(absolute);
    else if (entry.name === 'SKILL.md') payloads.push(path.relative(root, absolute));
  }
};
walk(templates);
if (payloads.length) throw new Error(`CLI package source contains canonical skill payloads:\n${payloads.join('\n')}`);
console.log('skill package boundary: metadata only; no bundled SKILL.md payloads');
