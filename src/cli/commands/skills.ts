/** Explicit, versioned remote YYLO skill installation and local status. */
import { Command, Option } from 'commander';
import chalk from 'chalk';
import { SkillInstaller } from '../../utils/skill-installer.js';

export function createSkillsCommand(): Command {
  const command = new Command('skills')
    .description('Install and inspect YYLO agent skills')
    .addHelpText(
      'after',
      `
Examples:
  $ yylo skills install
  $ yylo skills install --version 2.0.4
  $ yylo skills update
  $ yylo skills list
  $ yylo skills status

Install and update are the only skills commands that access the network. They
retrieve the latest stable yylo-dev/yylo-skills release satisfying ${SkillInstaller.VERSION_RANGE}
and install all seven canonical skills to .agents/skills, .claude/skills, and
.pi/skills. Unchanged receipt-owned copies upgrade automatically during install
or update. Customized/unrecorded conflicts require explicit --force; unrelated
and customized legacy skills are preserved. CLI installation alone does not
install skills.
`,
    );

  const addRemoteCommand = (name: 'install' | 'update') => {
    command
      .command(name)
      .description(`${name === 'install' ? 'Install' : 'Update'} canonical YYLO skills from GitHub`)
      .option('-v, --version <semver>', `Exact stable release satisfying ${SkillInstaller.VERSION_RANGE} (for example 2.0.4)`)
      .addOption(new Option('--skill-version <semver>').hideHelp())
      .option('-f, --force', 'Replace differing YYLO-owned skill directories')
      .action(async (options: { version?: string; skillVersion?: string; force?: boolean }) => {
        try {
          const requestedVersion = options.version ?? options.skillVersion;
          const result = await SkillInstaller.installRemote(process.cwd(), {
            ...(requestedVersion ? { version: requestedVersion } : {}),
            ...(options.force === undefined ? {} : { force: options.force }),
            silent: true,
          });
          console.log(
            result.changed
              ? chalk.green(`✓ Installed YYLO skills ${result.version} via ${result.acquisition}`)
              : chalk.green(`✓ YYLO skills ${result.version} are already installed`),
          );
          for (const warning of result.warnings ?? []) console.warn(chalk.yellow(`⚠ ${warning}`));
        } catch (error) {
          console.error(chalk.red(`✗ Skill ${name} failed:`));
          console.error(chalk.red(error instanceof Error ? error.message : String(error)));
          process.exitCode = 1;
        }
      });
  };

  addRemoteCommand('install');
  addRemoteCommand('update');

  command
    .command('list')
    .alias('ls')
    .description('List canonical skills and local installation state (offline)')
    .action(async () => {
      const groups = await SkillInstaller.listSkillGroups(process.cwd());
      for (const group of groups) {
        console.log(chalk.blue.bold(`\n${group.name} skills -> ${group.destDir}/`));
        for (const file of group.files) {
          console.log(`  ${file.installed ? chalk.green('✓') : chalk.red('✗')} ${file.name}`);
        }
      }
    });

  command
    .command('status')
    .description('Show local YYLO skill installation status (offline)')
    .action(async () => {
      const projectDir = process.cwd();
      const record = await SkillInstaller.getInstallRecord(projectDir);
      const needsUpdate = await SkillInstaller.needsUpdate(projectDir);
      const guidance = await SkillInstaller.inspectGuidance(projectDir);
      console.log(chalk.blue('Skills Status:\n'));
      console.log(`  Release: ${record?.version ?? 'not recorded'}`);
      console.log(`  Required: ${SkillInstaller.VERSION_RANGE}`);
      console.log(`  Source: ${record?.repository ?? SkillInstaller.REPOSITORY}`);
      console.log(`  ${needsUpdate ? chalk.yellow('⚠ Install required') : chalk.green('✓ Installed')}`);
      for (const finding of guidance.findings) {
        console.log(chalk.yellow(`  ⚠ ${finding.reason}: ${finding.destination}`));
      }
      if (needsUpdate) console.log(chalk.dim('\n  Run: yylo skills install; unchanged managed copies upgrade without --force.'));
      if (!guidance.coherent) console.log(chalk.dim('  Review preserved legacy/customized files separately; installation does not grant cleanup authority.'));
    });

  return command;
}
