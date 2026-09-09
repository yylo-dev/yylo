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
console.log('skill package boundary: remote installer metadata validated');
