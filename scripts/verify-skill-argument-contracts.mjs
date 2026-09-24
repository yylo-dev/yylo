#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import semver from 'semver';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const requirementsPath = path.join(root, 'src/skills-requirements.json');

export function verifySkillArguments(canonicalRoot, metadata, requirementsFile = requirementsPath) {
  const errors = [];
  const load = (file, label) => {
    try { return JSON.parse(fs.readFileSync(file, 'utf8')); }
    catch { throw new Error(`SKILLS_CONTRACT_MISMATCH\nMissing or invalid ${label}: ${file}`); }
  };
  const requirements = load(requirementsFile, 'CLI requirements');
  if (requirements.schemaVersion !== 1 || requirements.additionalSkills !== 'reject'
      || !requirements.required || !Array.isArray(requirements.manifestSchemaVersions)
      || !Array.isArray(requirements.contractVersions)) {
    throw new Error('SKILLS_CONTRACT_MISMATCH\nUnsupported CLI requirements contract');
  }
  const expected = Object.keys(requirements.required).sort();
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
  const manifest = load(path.join(canonicalRoot, 'skills-manifest.json'), 'skills release manifest');
  if (manifest.packageId !== 'yylo-skills' || !requirements.manifestSchemaVersions.includes(manifest.schemaVersion)
      || manifest.additionalSkills !== 'reject') errors.push('unsupported skills release manifest contract');
  let version;
  try { version = fs.readFileSync(path.join(canonicalRoot, 'VERSION'), 'utf8').trim(); }
  catch { errors.push('missing skills VERSION'); }
  const range = load(path.join(root, 'package.json'), 'CLI package').yyloSkills.version;
  if (manifest.sourceVersion !== version || !semver.valid(version) || !semver.satisfies(version, range)) {
    errors.push(`skills version mismatch: selected ${version}, manifest ${manifest.sourceVersion}, CLI requires ${range}`);
  }
  mismatch('release manifest', Object.keys(manifest.skills ?? {}));
  for (const slug of expected) {
    const released = manifest.skills?.[slug];
    const invocation = contract.skills?.[slug];
    if (!released || !requirements.contractVersions.includes(released.contractVersion)) errors.push(`${slug}: unsupported or missing invocation contract version`);
    if (released?.semantics !== requirements.required[slug] || invocation?.semantics !== requirements.required[slug]) errors.push(`${slug}: invocation semantics mismatch`);
    const ordered = value => JSON.stringify(Object.entries(value ?? {}).sort(([a], [b]) => a.localeCompare(b)));
    if (ordered(released?.placeholders) !== ordered(invocation?.placeholders)) errors.push(`${slug}: release/invocation placeholder metadata mismatch`);
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
