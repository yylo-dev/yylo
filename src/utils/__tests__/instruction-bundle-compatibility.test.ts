import { execFileSync } from 'node:child_process';
import * as path from 'node:path';
import policy from '../../templates/instruction-bundle-compatibility.json';
import {
  instructionVersionCompatible, instructionDeclarationCompatible,
} from '../instruction-bundle-compatibility.js';

const cases: Array<[unknown, boolean]> = [
  ['1.0.0', true], ['1.1.0', true], ['1.2.0', true], ['1.999.42', true],
  ['2.0.0', false], ['0.1.0', false], ['01.0.0', false], ['1.01.0', false],
  ['1.0.01', false], ['1.0', false], ['1.0.0-rc.1', false], ['1.0.0+build', false],
  ['1.0.0\n', false], [' 1.0.0', false], ['1.0.0 ', false],
  [null, false], [true, false], [1, false], [[], false], [{}, false],
];

describe('single instruction compatibility contract', () => {
  it.each(cases)('classifies %j as %s', (value, expected) => {
    expect(instructionVersionCompatible(value)).toBe(expected);
  });

  it('agrees with shipped Python for every conformance case and generated policy', () => {
    const scripts = path.resolve('src/templates/scripts');
    const probe = `import sys,json\nsys.path.insert(0,sys.argv[1])\nimport task_workspace as t\nvalues=json.loads(sys.argv[2])\nprint(json.dumps({'policy':t.INSTRUCTION_COMPATIBILITY,'results':[t.instruction_version_compatible(v) for v in values]}))`;
    const result = JSON.parse(execFileSync('python3', ['-c', probe, scripts, JSON.stringify(cases.map(([v]) => v))], { encoding: 'utf8' }));
    expect(result.policy).toEqual(policy);
    expect(result.results).toEqual(cases.map(([, expected]) => expected));
  });

  it('admits only supported declaration shapes without expanding ownership', () => {
    const declaration = { schemaVersion: policy.declarationSchema, semanticVersion: '1.42.3' };
    const rows: Array<[unknown, unknown, boolean]> = [
      [1, null, true], [2, declaration, true], [3, declaration, false],
      [true, null, false], [2, { ...declaration, requiredCapabilities: ['write-anywhere'] }, false],
      [1, declaration, false], [2, { ...declaration, schemaVersion: 'unknown' }, false],
      [2, { ...declaration, semanticVersion: '2.0.0' }, false], [2, null, false],
    ];
    for (const [schema, value, expected] of rows) {
      expect(instructionDeclarationCompatible(schema, value)).toBe(expected);
    }
    const result = JSON.parse(execFileSync('python3', ['-c',
      `import sys,json\nsys.path.insert(0,sys.argv[1])\nimport task_workspace as t\nprint(json.dumps([t.instruction_declaration_compatible(s,d) for s,d,_ in json.loads(sys.argv[2])]))`,
      path.resolve('src/templates/scripts'), JSON.stringify(rows)], { encoding: 'utf8' }));
    expect(result).toEqual(rows.map(([, , expected]) => expected));
  });
});
