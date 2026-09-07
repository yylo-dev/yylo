#!/usr/bin/env node
import { readFileSync } from 'node:fs';
import path from 'node:path';
const root=path.resolve(process.argv[2]??path.join(import.meta.dirname,'../..'));
const active=['.juno_task/config.json','.juno_task/config/task-workspace.json','.juno_task/plan.md','CLAUDE.md','juno-code/README.md','juno-code/docs/ledger-native-records-aggregate-acceptance.md','juno-code/src/templates/config/task-workspace.json'];
const patterns=[/(?:^|[\s`"'=:(])\/(?:Users|home)\/[A-Za-z0-9._-]+\//gu,/(?:^|[\s`"'=:(])\/private\/tmp\//gu,/\b[A-Za-z]:\\Users\\[^\\\s]+\\/gu];
const findings=[];
for(const relative of active){const text=readFileSync(path.join(root,relative),'utf8');for(const pattern of patterns){for(const match of text.matchAll(pattern))findings.push({path:relative,line:text.slice(0,match.index).split('\n').length,match:match[0].trim()});}}
const report={schema_version:'yylo_path_portability_report.v1',active_paths:active,excluded_classes:['immutable receipts and runtime logs','test fixtures and examples','lockfiles and generated output','redaction and security canaries'],findings};
console.log(JSON.stringify(report));if(findings.length)process.exitCode=1;
