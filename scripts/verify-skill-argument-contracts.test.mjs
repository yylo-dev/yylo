import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { verifySkillArguments } from './verify-skill-argument-contracts.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const metadata = path.join(root, 'src/templates/skills/argument-contracts.json');
const source = path.resolve(root, '../yylo-skills');
function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'yylo-skill-contract-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.cpSync(path.join(source, 'skills'), path.join(dir, 'skills'), { recursive: true });
  fs.copyFileSync(metadata, path.join(dir, 'metadata.json'));
  return { dir, meta: path.join(dir, 'metadata.json') };
}
function changeMetadata(meta, change) {
  const data = JSON.parse(fs.readFileSync(meta, 'utf8')); change(data);
  fs.writeFileSync(meta, JSON.stringify(data));
}
test('real pinned eight-skill source includes benchmark invocation', () => {
  assert.equal(verifySkillArguments(source, metadata), 8);
});
test('missing source fails rather than skipping verification', t => {
  const { dir, meta } = fixture(t);
  assert.throws(() => verifySkillArguments(path.join(dir, 'absent'), meta), /Missing skills source/);
});
test('old seven-skill release gives exact missing identity', t => {
  const { dir, meta } = fixture(t);
  fs.rmSync(path.join(dir, 'skills/benchmark-yylo'), { recursive: true });
  assert.throws(() => verifySkillArguments(dir, meta), /skills source: missing benchmark-yylo/);
});
test('extra release skill and omitted metadata are both reported', t => {
  const { dir, meta } = fixture(t);
  fs.mkdirSync(path.join(dir, 'skills/unexpected-yylo'));
  changeMetadata(meta, data => delete data.skills['benchmark-yylo']);
  assert.throws(() => verifySkillArguments(dir, meta), error =>
    error.message.includes('invocation metadata: missing benchmark-yylo') && error.message.includes('skills source: unexpected unexpected-yylo'));
});
test('undeclared placeholders cannot hide behind otherwise correct counts', t => {
  const { dir, meta } = fixture(t);
  fs.appendFileSync(path.join(dir, 'skills/benchmark-yylo/SKILL.md'), '\n$3\n');
  assert.throws(() => verifySkillArguments(dir, meta), /benchmark-yylo placeholder \$3: expected undeclared, got 1/);
});
test('empty metadata, unsupported schema and wrong counts fail', t => {
  const { dir, meta } = fixture(t);
  changeMetadata(meta, data => { data.schemaVersion = 99; data.skills['benchmark-yylo'].placeholders = {}; });
  assert.throws(() => verifySkillArguments(dir, meta), /unsupported metadata schema: 99/);
  fs.copyFileSync(metadata, meta);
  changeMetadata(meta, data => { data.skills['benchmark-yylo'].placeholders.$ARGUMENTS = 2; });
  assert.throws(() => verifySkillArguments(dir, meta), /expected 2, got 1/);
});
test('unsafe skill links and missing skill files fail', t => {
  const { dir, meta } = fixture(t);
  const file = path.join(dir, 'skills/benchmark-yylo/SKILL.md');
  fs.unlinkSync(file); fs.symlinkSync(metadata, file);
  assert.throws(() => verifySkillArguments(dir, meta), /missing or unsafe SKILL.md/);
});
