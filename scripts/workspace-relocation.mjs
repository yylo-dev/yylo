#!/usr/bin/env node
import { createHash } from 'node:crypto';
import { existsSync, lstatSync, readFileSync, realpathSync, renameSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawnSync } from 'node:child_process';

const PLAN_SCHEMA = 'yylo_workspace_relocation_plan.v1';
const RECEIPT_SCHEMA = 'yylo_workspace_relocation_receipt.v1';
const sha = (value) => createHash('sha256').update(value).digest('hex');
const fileSha = (file) => sha(readFileSync(file));
const fail = (message) => { throw new Error(message); };
function git(root, args) { const r = spawnSync('git', ['-C', root, ...args], { encoding: 'utf8' }); if (r.status !== 0) fail(r.stderr.trim() || `git ${args.join(' ')} failed`); return r.stdout.trim(); }
function absolutePhysical(value, label, mustExist = true) {
  if (!path.isAbsolute(value) || path.normalize(value) === path.parse(value).root) fail(`${label} must be a non-root absolute path`);
  const normalized = path.normalize(value);
  if (mustExist) {
    if (!existsSync(normalized)) fail(`${label} does not exist`);
    if (lstatSync(normalized).isSymbolicLink() || realpathSync(normalized) !== normalized) fail(`${label} must be a physical non-symlink path`);
  }
  return normalized;
}
function parseMappings(values) {
  if (values.length === 0) fail('at least one --map OLD=NEW is required');
  return values.map((value) => { const split = value.indexOf('='); if (split < 1) fail('mapping must be OLD=NEW');
    const oldRoot = absolutePhysical(value.slice(0, split), 'old root', false); const newRoot = absolutePhysical(value.slice(split + 1), 'new root');
    if (oldRoot === newRoot) fail('old and new roots must differ'); return { old_root: oldRoot, new_root: newRoot }; });
}
function replacement(value, mappings) {
  if (typeof value !== 'string') return null;
  for (const item of mappings) if (value === item.old_root || value.startsWith(`${item.old_root}${path.sep}`)) return `${item.new_root}${value.slice(item.old_root.length)}`;
  return null;
}
function collect(value, mappings, pointer = '', result = []) {
  if (Array.isArray(value)) value.forEach((item, index) => collect(item, mappings, `${pointer}/${index}`, result));
  else if (value && typeof value === 'object') for (const [key, item] of Object.entries(value)) collect(item, mappings, `${pointer}/${key.replaceAll('~','~0').replaceAll('/','~1')}`, result);
  else { const next = replacement(value, mappings); if (next !== null) result.push({ pointer, old_value: value, new_value: next }); }
  return result;
}
function setPointer(root, pointer, expected, value) { const parts = pointer.slice(1).split('/').map((item) => item.replaceAll('~1','/').replaceAll('~0','~')); let owner = root; for (const part of parts.slice(0,-1)) owner = owner[part]; const key = parts.at(-1); if (owner[key] !== expected) fail(`stale relocation pointer ${pointer}`); owner[key] = value; }
function args(argv) { const out = { maps: [] }; for (let i=0;i<argv.length;i++) { const key=argv[i]; if (key==='--map') out.maps.push(argv[++i]); else if (key?.startsWith('--')) out[key.slice(2).replaceAll('-','_')] = argv[++i]; else if (!out.operation) out.operation=key; else fail(`unexpected argument ${key}`); } return out; }
function bindings(controller, stateFile) { return { controller: realpathSync(controller), repository_common_dir: git(controller, ['rev-parse','--path-format=absolute','--git-common-dir']), head: git(controller,['rev-parse','HEAD']), target_ref: git(controller,['symbolic-ref','HEAD']), state_sha256: fileSha(stateFile) }; }
function ensureClean(controller) { if (git(controller, ['status','--porcelain']) !== '') fail('repository is dirty'); }
function verifyObjects(controller, state) { const missing=[]; for (const [taskId, record] of Object.entries(state.tasks ?? {})) { if (!record || ['MERGED','WITHDRAWN'].includes(record.state)) continue; for (const field of ['base_sha','tip_sha','candidate_sha']) { const oid=record[field]; if (typeof oid==='string' && /^[0-9a-f]{40}$/.test(oid)) { const r=spawnSync('git',['-C',controller,'cat-file','-e',`${oid}^{commit}`]); if (r.status!==0) missing.push({task_id:taskId,field,oid}); } } } return missing; }
function liveProducers(state) { const live=[]; for (const [taskId,record] of Object.entries(state.tasks??{})) { const producer=record?.fencing?.producer; if(record?.fencing?.state==='ACTIVE'&&producer?.host===os.hostname()&&Number.isSafeInteger(producer.pid)){try{process.kill(producer.pid,0);live.push({task_id:taskId,pid:producer.pid});}catch(error){if(error?.code==='EPERM')live.push({task_id:taskId,pid:producer.pid});}} } return live; }
function writeExclusive(file, value) { if (existsSync(file)) fail(`refusing to overwrite ${file}`); writeFileSync(file, `${JSON.stringify(value,null,2)}\n`, { mode: 0o600, flag: 'wx' }); }
const options=args(process.argv.slice(2));
try {
  const controller=absolutePhysical(options.controller ?? process.cwd(),'controller'); const stateFile=path.join(controller,'.juno_task/state/tasks.json');
  if (!existsSync(stateFile)) fail('controller task state is missing');
  if (options.operation==='plan') { ensureClean(controller); const mappings=parseMappings(options.maps); const state=JSON.parse(readFileSync(stateFile,'utf8')); const changes=collect(state,mappings); const missing_objects=verifyObjects(controller,state);
    const live_producers=liveProducers(state); const outcome=missing_objects.length?'blocked_missing_objects':live_producers.length?'blocked_live_producer':'planned'; const core={schema_version:PLAN_SCHEMA,bindings:bindings(controller,stateFile),mappings,changes,missing_objects,live_producers,outcome}; const plan={...core,plan_sha256:sha(JSON.stringify(core))}; const output=path.resolve(options.output); absolutePhysical(path.dirname(output),'output parent'); writeExclusive(output,plan); console.log(JSON.stringify(plan));
  } else if (options.operation==='apply') { ensureClean(controller); const plan=JSON.parse(readFileSync(options.plan,'utf8')); const {plan_sha256,...core}=plan; if(plan.schema_version!==PLAN_SCHEMA||sha(JSON.stringify(core))!==plan_sha256) fail('relocation plan integrity mismatch'); if(plan.outcome!=='planned'||plan.missing_objects.length) fail('relocation plan is blocked by missing commit objects'); const before=bindings(controller,stateFile); if(JSON.stringify(before)!==JSON.stringify(plan.bindings)) fail('relocation plan is stale'); const state=JSON.parse(readFileSync(stateFile,'utf8')); for(const change of plan.changes)setPointer(state,change.pointer,change.old_value,change.new_value); const bytes=`${JSON.stringify(state,null,2)}\n`; const temp=`${stateFile}.${process.pid}.tmp`; writeFileSync(temp,bytes,{mode:0o600}); renameSync(temp,stateFile); const receiptCore={schema_version:RECEIPT_SCHEMA,plan_sha256,state_before_sha256:before.state_sha256,state_after_sha256:sha(bytes),changed_records:plan.changes.length,bindings:{...before,state_sha256:sha(bytes)}}; const receipt={...receiptCore,receipt_sha256:sha(JSON.stringify(receiptCore))}; writeExclusive(options.receipt,receipt); console.log(JSON.stringify(receipt));
  } else if(options.operation==='verify') { const receipt=JSON.parse(readFileSync(options.receipt,'utf8')); const {receipt_sha256,...core}=receipt; if(receipt.schema_version!==RECEIPT_SCHEMA||sha(JSON.stringify(core))!==receipt_sha256)fail('relocation receipt integrity mismatch'); if(fileSha(stateFile)!==receipt.state_after_sha256)fail('relocated state differs from receipt'); console.log(JSON.stringify({schema_version:RECEIPT_SCHEMA,outcome:'verified',receipt_sha256}));
  } else fail('operation must be plan, apply, or verify');
} catch(error) { console.error(`workspace-relocation: ${error.message}`); process.exitCode=2; }
