/**
 * Setup-git command implementation for yylo CLI
 *
 * Provides Git repository configuration and setup with URL management,
 * branch setup, remote configuration, and integration with project templates.
 */

import chalk from 'chalk';
import { Command } from 'commander';
import * as readline from 'node:readline';

import { GitManager, GitUrlUtils, type GitRepositoryInfo } from '../../core/git.js';
import type { SetupGitOptions } from '../types.js';
import { ManagedProjectAssets } from '../../utils/managed-project-assets.js';

// Import environment detector for headless mode checks
import { isHeadlessEnvironment } from '../../utils/environment.js';

/**
 * Interactive confirmation prompt
 */
interface ConfirmationPrompt {
  question: string;
  defaultAnswer?: boolean;
}

/**
 * Interactive prompt utilities
 */
class InteractivePrompts {
  /**
   * Ask user for confirmation
   */
  static async confirm(prompt: ConfirmationPrompt): Promise<boolean> {
    if (isHeadlessEnvironment()) {
      return prompt.defaultAnswer ?? false;
    }

    const rl = readline.createInterface({
      input: process.stdin,
      output: process.stdout,
    });

    return new Promise((resolve) => {
      const defaultText =
        prompt.defaultAnswer !== undefined
          ? ` (${prompt.defaultAnswer ? 'Y/n' : 'y/N'})`
          : ' (y/n)';

      rl.question(`${prompt.question}${defaultText}: `, (answer) => {
        rl.close();

        if (!answer.trim()) {
          resolve(prompt.defaultAnswer ?? false);
          return;
        }

        const normalizedAnswer = answer.toLowerCase().trim();
        resolve(normalizedAnswer === 'y' || normalizedAnswer === 'yes');
      });
    });
  }

  /**
   * Ask user for text input
   */
  static async input(question: string, defaultValue?: string): Promise<string> {
    if (isHeadlessEnvironment()) {
      return defaultValue || '';
    }

    const rl = readline.createInterface({
      input: process.stdin,
      output: process.stdout,
    });

    return new Promise((resolve) => {
      const defaultText = defaultValue ? ` (${defaultValue})` : '';
      rl.question(`${question}${defaultText}: `, (answer) => {
        rl.close();
        resolve(answer.trim() || defaultValue || '');
      });
    });
  }
}

/**
 * Git configuration display formatter
 */
class GitDisplayFormatter {
  formatRepositoryInfo(info: GitRepositoryInfo): void {
    if (!info.isRepository) {
      console.log(chalk.yellow('📁 Not a Git repository'));
      return;
    }

    console.log(chalk.blue.bold('\n📋 Git Repository Information\n'));

    // Repository status
    console.log(chalk.white.bold('Repository Status:'));
    console.log(`   Initialized: ${chalk.green('Yes')}`);
    console.log(
      `   Current Branch: ${info.currentBranch ? chalk.cyan(info.currentBranch) : chalk.gray('(no branch)')}`,
    );
    console.log(`   Status: ${this.formatStatus(info.status)}`);
    console.log(`   Commits: ${chalk.cyan(info.commitCount.toString())}`);

    // Remotes
    console.log(chalk.white.bold('\nRemotes:'));
    if (Object.keys(info.remotes).length === 0) {
      console.log(chalk.gray('   No remotes configured'));
    } else {
      Object.entries(info.remotes).forEach(([name, url]) => {
        const validation = GitUrlUtils.parseRepositoryUrl(url);
        const provider = validation ? ` (${validation.provider})` : '';
        console.log(`   ${chalk.cyan(name)}: ${chalk.white(url)}${chalk.gray(provider)}`);
      });
    }

    // Last commit
    if (info.lastCommit) {
      console.log(chalk.white.bold('\nLast Commit:'));
      console.log(`   Hash: ${chalk.gray(info.lastCommit.hash)}`);
      console.log(`   Message: ${chalk.white(info.lastCommit.message)}`);
      console.log(
        `   Author: ${chalk.gray(info.lastCommit.author)} <${chalk.gray(info.lastCommit.email)}>`,
      );
      console.log(`   Date: ${chalk.gray(info.lastCommit.date.toLocaleString())}`);
      console.log(`   Files Changed: ${chalk.cyan(info.lastCommit.filesChanged.toString())}`);
    } else {
      console.log(chalk.white.bold('\nCommits:'));
      console.log(chalk.gray('   No commits yet'));
    }
  }

  formatSetupResult(info: GitRepositoryInfo, url?: string): void {
    console.log(chalk.green.bold('\n✅ Git Setup Complete!\n'));

    console.log(chalk.blue('📋 Configuration Summary:'));
    console.log(`   Repository: ${chalk.green('Initialized')}`);
    console.log(`   Branch: ${chalk.cyan(info.currentBranch || 'main')}`);
    console.log(`   Status: ${this.formatStatus(info.status)}`);

    if (url) {
      const validation = GitUrlUtils.parseRepositoryUrl(url);
      const provider = validation ? ` (${validation.provider})` : '';
      console.log(`   Remote URL: ${chalk.white(url)}${chalk.gray(provider)}`);

      if (validation) {
        const webUrl = GitUrlUtils.getWebUrl(url);
        console.log(`   Web URL: ${chalk.blue(webUrl)}`);
      }
    }

    console.log(chalk.blue('\n💡 Next Steps:'));
    console.log('   1. Review and commit your changes:');
    console.log(chalk.gray('      git add .'));
    console.log(chalk.gray('      git commit -m "Initial commit"'));

    if (url) {
      console.log('   2. Push to remote repository:');
      console.log(chalk.gray(`      git push -u origin ${info.currentBranch || 'main'}`));
    }

    console.log('   3. Start working on your project:');
    console.log(chalk.gray('      yylo start'));
  }

  private formatStatus(status: GitRepositoryInfo['status']): string {
    const statusColors = {
      clean: chalk.green('Clean'),
      dirty: chalk.yellow('Uncommitted changes'),
      untracked: chalk.blue('Untracked files'),
      mixed: chalk.red('Mixed changes'),
      empty: chalk.gray('No commits'),
      'not-repository': chalk.red('Not a Git repository'),
    };

    return statusColors[status] || chalk.gray(status);
  }
}

/**
 * Interactive Git setup
 */
class GitSetupInteractive {
  constructor(
    private gitManager: GitManager,
    private formatter: GitDisplayFormatter,
  ) {}

  async setup(): Promise<void> {
    console.log(chalk.blue.bold('\n🔧 Git Repository Setup\n'));

    const info = await this.gitManager.getRepositoryInfo();

    if (!info.isRepository) {
      console.log(chalk.yellow('No Git repository found. Initializing...'));
      await this.gitManager.initRepository();
      console.log(chalk.green('✅ Git repository initialized'));
    }

    // Get current upstream configuration
    const upstreamConfig = await this.gitManager.getUpstreamConfig();

    // Prompt for upstream URL
    const url = await this.promptForUrl(upstreamConfig.url);

    if (url) {
      console.log(chalk.blue(`Setting up upstream: ${url}`));
      await this.gitManager.setupUpstream(url);
      await this.gitManager.updateJunoTaskConfig(url);
      await ManagedProjectAssets.update(process.cwd(), {
        silent: true, localization: { gitRemoteUrl: url },
      });
      console.log(chalk.green(`✅ Upstream URL configured: ${url}`));

      // Get updated info
      const updatedInfo = await this.gitManager.getRepositoryInfo();

      // Create initial commit if needed
      if (updatedInfo.commitCount === 0) {
        const shouldCommit = await InteractivePrompts.confirm({
          question: 'Create initial commit with current files?',
          defaultAnswer: true,
        });

        if (shouldCommit) {
          console.log(chalk.blue('Creating initial commit...'));
          await this.gitManager.createInitialCommit();
          console.log(chalk.green('✅ Initial commit created'));
        }
      }

      // Offer to push to upstream
      const shouldPush = await InteractivePrompts.confirm({
        question: 'Push to upstream repository now?',
        defaultAnswer: false,
      });

      if (shouldPush) {
        try {
          console.log(chalk.blue('Pushing to upstream...'));
          await this.gitManager.performInitialPush();
          console.log(chalk.green('✅ Successfully pushed to upstream'));
        } catch (error) {
          console.warn(chalk.yellow(`⚠️  Push failed: ${error}`));
          console.log(chalk.gray('You can push manually later with: git push -u origin main'));
        }
      }
    }

    // Display final results
    const finalInfo = await this.gitManager.getRepositoryInfo();
    this.formatter.formatSetupResult(finalInfo, url);
  }

  private async promptForUrl(currentUrl?: string): Promise<string | undefined> {
    console.log(chalk.yellow('🔗 Repository URL Setup:'));

    if (currentUrl) {
      console.log(`   Current: ${chalk.white(currentUrl)}`);
      const keepCurrent = await InteractivePrompts.confirm({
        question: 'Keep current URL?',
        defaultAnswer: true,
      });

      if (keepCurrent) {
        return currentUrl;
      }
    }

    console.log('   Enter Git repository URL (or press Enter to skip):');
    console.log(chalk.gray('   Examples:'));
    console.log(chalk.gray('     https://github.com/owner/repo.git'));
    console.log(chalk.gray('     git@github.com:owner/repo.git'));
    console.log();

    const newUrl = await InteractivePrompts.input('Git URL', '');

    if (!newUrl.trim()) {
      return undefined;
    }

    // Validate the URL
    const validation = GitManager.validateGitUrl(newUrl);
    if (!validation.valid) {
      console.log(chalk.red(`❌ Invalid URL: ${validation.error}`));
      console.log(chalk.yellow('Please use a valid Git repository URL'));
      return this.promptForUrl(); // Retry
    }

    if (validation.provider) {
      console.log(chalk.green(`✅ Valid ${validation.provider} repository URL`));
    }

    return newUrl;
  }
}

/**
 * Main setup-git command handler
 */
export async function setupGitCommandHandler(
  args: string[],
  options: SetupGitOptions,
  _command: Command,
): Promise<void> {
  try {
    const workingDirectory = process.cwd();
    const gitManager = new GitManager(workingDirectory);
    const formatter = new GitDisplayFormatter();

    if (options.show) {
      // Show current configuration
      const info = await gitManager.getRepositoryInfo();
      const upstreamConfig = await gitManager.getUpstreamConfig();

      formatter.formatRepositoryInfo(info);

      if (upstreamConfig.isConfigured) {
        console.log(chalk.blue('\n🔗 Upstream Configuration:'));
        console.log(`   Remote: ${chalk.cyan(upstreamConfig.remote)}`);
        console.log(`   URL: ${chalk.white(upstreamConfig.url)}`);
        console.log(`   Branch: ${chalk.cyan(upstreamConfig.branch)}`);

        const webUrl = GitUrlUtils.getWebUrl(upstreamConfig.url!);
        console.log(`   Web URL: ${chalk.blue(webUrl)}`);
      } else {
        console.log(chalk.yellow('\n🔗 No upstream repository configured'));
        console.log(chalk.gray('   Use: yylo setup-git <url> to configure'));
      }

      return;
    }

    if (options.remove) {
      // Remove upstream URL
      const info = await gitManager.getRepositoryInfo();

      if (!info.isRepository) {
        console.log(chalk.yellow('❌ Not a Git repository'));
        console.log(chalk.gray('   Initialize with: yylo setup-git --init'));
        return;
      }

      const upstreamConfig = await gitManager.getUpstreamConfig();
      if (!upstreamConfig.isConfigured) {
        console.log(chalk.yellow('❌ No upstream remote configured'));
        return;
      }

      const confirmRemoval = await InteractivePrompts.confirm({
        question: `Remove upstream remote '${upstreamConfig.remote}' (${upstreamConfig.url})?`,
        defaultAnswer: false,
      });

      if (confirmRemoval) {
        const removed = await gitManager.removeUpstream();
        if (removed) {
          console.log(chalk.green('✅ Upstream remote removed'));
        } else {
          console.log(chalk.yellow('⚠️  No upstream remote to remove'));
        }
      } else {
        console.log(chalk.gray('Operation cancelled'));
      }

      return;
    }

    // Handle URL argument
    const url = args[0];

    if (url) {
      // Direct URL setup
      const validation = GitManager.validateGitUrl(url);
      if (!validation.valid) {
        const { ValidationError } = await import('../types.js');
        throw new ValidationError(`Invalid Git URL: ${validation.error}`, [
          'Use HTTPS format: https://github.com/owner/repo.git',
          'Use SSH format: git@github.com:owner/repo.git',
          'Supported providers: GitHub, GitLab, Bitbucket',
        ]);
      }

      const info = await gitManager.getRepositoryInfo();

      if (!info.isRepository) {
        console.log(chalk.blue('🔧 Initializing Git repository...'));
        await gitManager.initRepository();
        console.log(chalk.green('✅ Git repository initialized'));
      }

      console.log(chalk.blue(`🔗 Setting up upstream: ${url}`));
      if (validation.provider) {
        console.log(chalk.gray(`   Provider: ${validation.provider}`));
      }

      await gitManager.setupUpstream(url);
      await gitManager.updateJunoTaskConfig(url);
      await ManagedProjectAssets.update(workingDirectory, {
        silent: true, localization: { gitRemoteUrl: url },
      });

      console.log(chalk.green(`✅ Upstream URL configured: ${url}`));

      // Get updated info
      const updatedInfo = await gitManager.getRepositoryInfo();

      // Create initial commit if repository is empty
      if (updatedInfo.commitCount === 0) {
        console.log(chalk.blue('📝 Creating initial commit...'));
        await gitManager.createInitialCommit();
        console.log(chalk.green('✅ Initial commit created'));
      }

      // Ask about initial push
      const shouldPush = await InteractivePrompts.confirm({
        question: 'Push to upstream repository now?',
        defaultAnswer: true,
      });

      if (shouldPush) {
        try {
          console.log(chalk.blue('📤 Pushing to upstream...'));
          await gitManager.performInitialPush();
          console.log(chalk.green('✅ Successfully pushed to upstream'));
        } catch (error) {
          console.warn(chalk.yellow(`⚠️  Push failed: ${error}`));
          console.log(chalk.gray('You can push manually later with: git push -u origin main'));
        }
      }

      // Display final configuration
      const finalInfo = await gitManager.getRepositoryInfo();
      formatter.formatSetupResult(finalInfo, url);
    } else {
      // Interactive setup
      const interactive = new GitSetupInteractive(gitManager, formatter);
      await interactive.setup();
    }
  } catch (error) {
    const { ValidationError, RuntimeError } = await import('../types.js');

    if (error instanceof ValidationError || error instanceof RuntimeError) {
      console.error(chalk.red.bold('\n❌ Git Setup Failed'));
      console.error(chalk.red(`   ${error.message}`));

      if ((error as any).suggestions?.length) {
        console.error(chalk.yellow('\n💡 Suggestions:'));
        (error as any).suggestions.forEach((suggestion: string) => {
          console.error(chalk.yellow(`   • ${suggestion}`));
        });
      }

      process.exit(1);
    }

    // Unexpected error
    console.error(chalk.red.bold('\n❌ Unexpected Error'));
    console.error(chalk.red(`   ${error instanceof Error ? error.message : String(error)}`));

    if (options.verbose && error instanceof Error) {
      console.error('\n📍 Stack Trace:');
      console.error(error.stack);
    }

    process.exit(99);
  }
}

/**
 * Configure the setup-git command for Commander.js
 */
export function configureSetupGitCommand(program: Command): void {
  program
    .command('setup-git')
    .description('Configure Git repository and upstream URL')
    .argument('[url]', 'Git repository URL')
    .option('-s, --show', 'Show current Git configuration')
    .option('-r, --remove', 'Remove upstream URL configuration')
    .action(async (url, options, command) => {
      const args = url ? [url] : [];
      await setupGitCommandHandler(args, options, command);
    })
    .addHelpText(
      'after',
      `
Examples:
  $ yylo setup-git                                    # Interactive setup
  $ yylo setup-git https://github.com/owner/repo     # Set specific URL
  $ yylo setup-git --show                            # Show current config
  $ yylo setup-git --remove                          # Remove upstream URL

Git URL Examples:
  https://github.com/owner/repo.git                       # GitHub HTTPS
  https://gitlab.com/owner/repo.git                       # GitLab HTTPS
  https://bitbucket.org/owner/repo.git                    # Bitbucket HTTPS

Environment Variables:
  JUNO_TASK_GIT_URL         Default Git repository URL

Notes:
  - Automatically initializes Git repository if needed
  - Sets up 'main' as default branch
  - Updates .juno_task/init.md with Git URL
  - Creates initial commit if repository is empty
  - Use HTTPS URLs for better compatibility
    `,
    );
}
