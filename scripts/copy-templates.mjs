#!/usr/bin/env node
import fs from 'fs-extra';
import path from 'node:path';

const sourceRoot = 'src/templates';
const destinationRoot = 'dist/templates';
const directories = ['scripts', 'maintenance', 'prompts', 'wiki', 'config', 'controller-agent', 'workflows'];
const retiredMarker = '# Retired';

for (const directory of directories) {
  const source = path.join(sourceRoot, directory);
  const destination = path.join(destinationRoot, directory);
  if (!fs.existsSync(source)) {
    throw new Error(`Missing managed template directory: ${source}`);
  }
  fs.copySync(source, destination, {
    recursive: true,
    filter: (candidate) => {
      if (!fs.statSync(candidate).isFile()) return true;
      return !fs.readFileSync(candidate, 'utf8').startsWith(retiredMarker);
    },
  });
}

fs.copyFileSync(
  path.join(sourceRoot, 'managed-assets.json'),
  path.join(destinationRoot, 'managed-assets.json'),
);
fs.removeSync(path.join(destinationRoot, 'scripts/logs'));
for (const filename of fs.readdirSync(path.join(destinationRoot, 'scripts'))) {
  if (filename.endsWith('.sh')) {
    fs.chmodSync(path.join(destinationRoot, 'scripts', filename), 0o755);
  }
}
