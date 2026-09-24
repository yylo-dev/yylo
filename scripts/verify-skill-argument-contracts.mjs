#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
// Independent CLI requirements. The release manifest gate will supersede this
// inventory; never infer requirements from the files being validated.
const expected = [
  'artifact-yylo', 'benchmark-yylo', 'ledger-tasks-yylo',
  'plan-ledger-tasks-yylo', 'ralph-loop-yylo', 'understand-project-yylo',
  'wiki-yylo', 'workflow-yylo',
];

export function verifySkillArguments(canonicalRoot, metadata) {
  const errors = [];
  const mismatch = (label, names) => {
    for (const name of expected.filter(name => !names.includes(name))) errors.push(`${label}: missing ${name}`);
    for (const name of names.filter(name => !expected.includes(name))) errors.push(`${label}: unexpected ${name}`);
  };
  let contract;
  try { contract = JSON.parse(fs.readFileSync(metadata, 'utf8')); }
  catch { throw new Error(`SKILLS_CONTRACT_MISMATCH\nMissing or invalid invocation metadata: ${metadata}`); }
  if (contract.schemaVersion !== 1) errors.push(`unsupported metadata schema: ${contract.schemaVersion}`);
  mismatch('invocation metadata', Object.keys(contract.skills ?? {}));
  let entries;
  try { entries = fs.readdirSync(path.join(canonicalRoot, 'skills'), { withFileTypes: true }); }
  catch { throw new Error(`SKILLS_CONTRACT_MISMATCH\nMissing skills source: ${canonicalRoot}`); }
  mismatch('skills source', entries.map(entry => entry.name));
  for (const slug of expected) {
    const entry = entries.find(entry => entry.name === slug);
    if (!entry) continue;
    if (!entry.isDirectory()) { errors.push(`${slug}: expected a real skill directory`); continue; }
    let text;
    try {
      const file = path.join(canonicalRoot, 'skills', slug, 'SKILL.md');
      if (!fs.lstatSync(file).isFile()) throw new Error('not a regular file');
      text = fs.readFileSync(file, 'utf8');
    } catch { errors.push(`${slug}: missing or unsafe SKILL.md`); continue; }
    if (!text.includes(`\nname: ${slug}\n`)) errors.push(`${slug}: canonical name mismatch`);
    const declared = contract.skills?.[slug]?.placeholders;
    if (!declared || typeof declared !== 'object' || Array.isArray(declared)
        || !Object.hasOwn(declared, '$ARGUMENTS')) {
      errors.push(`${slug}: incomplete placeholder metadata`); continue;
    }
    const actual = {};
    for (const [placeholder] of text.matchAll(/\$ARGUMENTS\b|\$[1-9][0-9]*/g)) actual[placeholder] = (actual[placeholder] ?? 0) + 1;
    for (const placeholder of new Set([...Object.keys(declared), ...Object.keys(actual)])) {
      const count = declared[placeholder];
      if (!Number.isInteger(count) || count < 1 || actual[placeholder] !== count) {
        errors.push(`${slug} placeholder ${placeholder}: expected ${count ?? 'undeclared'}, got ${actual[placeholder] ?? 0}`);
      }
    }
  }
  if (errors.length) throw new Error(`SKILLS_CONTRACT_MISMATCH\n${errors.sort().join('\n')}`);
  return expected.length;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const count = verifySkillArguments(path.resolve(root, '../yylo-skills'), path.join(root, 'src/templates/skills/argument-contracts.json'));
    console.log(`skill package boundary: invocation metadata validated for ${count} required identities`);
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
