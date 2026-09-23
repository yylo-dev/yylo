/**
 * Simplified Init command implementation for yylo CLI
 *
 * Minimal flow: Project Root → Main Task → Editor Selection → Git Setup → Save → Workspace Mode
 * Removes all complex features: token counting, cost calculation, character limits, etc.
 */

import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import fs from 'fs-extra';
import chalk from 'chalk';
import { Command } from 'commander';
import { promptMultiline, promptInputOnce } from '../utils/multiline.js';
import { resolveMachineOutput, writeMachineResponse, MACHINE_RESPONSE_SCHEMA } from '../machine-output.js';
import { planSimpleInit, applySimpleInit, planSimpleConversion, applySimpleConversion, writeSimpleInitPlan, type SimpleInitPlan, type SimpleConversionPlan } from '../../utils/simple-init.js';

import { getDefaultHooks } from '../../templates/default-hooks.js';
import { hasSimpleWorkspaceHint } from '../../utils/controller-resolver.js';
import { getDefaultModelForSubagent } from '../../core/subagent-models.js';
import { createPersistedProjectConfigDefaults } from '../../core/config.js';
import type { InitCommandOptions } from '../types.js';
import type { SubagentType } from '../../types/index.js';
import { ValidationError } from '../types.js';
import { buildChildProcessEnvironment } from '../../core/child-process-environment.js';
import {
  establishFreshInitTopology,
  installedCliVersion,
  probeFreshInitTopology,
} from '../../utils/fresh-init-topology.js';

/** Simple key-value variables for template interpolation */
interface InitVariables {
  readonly [key: string]: string | number | boolean | null | undefined;
}

interface InitializationContext {
  mode?: 'simple' | 'advanced';
  targetDirectory: string;
  task: string;
  subagent: string;
  gitUrl?: string;
  targetBranch?: string;
  variables: InitVariables;
  force: boolean;
  interactive: boolean;
}

/**
 * Simplified Interactive TUI for project initialization
 * Minimal flow as requested by user:
 * Collect all answers before mode-specific writes. Workspace Mode is the final question.
 */
class SimpleInitTUI {
  // Simple single-line input helper is provided by utils

  /**
   * Simplified gather method implementing the minimal flow
   */
  async gather(options: InitCommandOptions = {}): Promise<InitializationContext> {
    console.log(chalk.blue.bold('\n🚀 YYLO Project Initialization\n'));

    // 1. Project Root
    console.log(chalk.yellow('📁 Step 1: Project Directory'));
    const targetDirectory = options.directory ? path.resolve(options.directory) : await this.promptForDirectory();

    // 2. Main Task (multi-line, NO character limits)
    console.log(chalk.yellow('\n📝 Step 2: Main Task'));
    const task = options.task || await this.promptForTask();

    // 3. Editor Selection (simplified menu)
    console.log(chalk.yellow('\n👨‍💻 Step 3: Select Coding Editor'));
    const editor = options.subagent || await this.promptForEditor();

    // 4. Git Setup (simple yes/no)
    console.log(chalk.yellow('\n🔗 Step 4: Git Setup'));
    console.log(chalk.gray('   Simple requires prior git init; repository URLs are Advanced-only.'));
    const gitUrl = options.gitUrl || await this.promptForGitSetup();

    // 5. Save confirmation (handle existing files)
    console.log(chalk.yellow('\n💾 Step 5: Save Project'));
    await this.confirmSave(targetDirectory);

    // This must remain the final question, including existing-file confirmation.
    console.log(chalk.yellow('\n⚙️  Step 6: Workspace Mode'));
    console.log(chalk.gray('   Simple: code, notes and Ledger in one Git checkout; shared files, no managed delivery.'));
    console.log(chalk.gray('   Advanced: separate controller, isolated task worktrees and managed merging.'));
    console.log(chalk.gray('   Simple adds config, local ignore rules, guidance and a receipt under .juno_task.'));
    console.log(chalk.gray('   Existing instructions are preserved; selecting a mode does not convert an existing project.'));
    const answer = options.mode || (await promptInputOnce('Workspace mode: 1) Simple (recommended)  2) Advanced', '1')).trim().toLowerCase() || '1';
    const mode = ['1', 'simple'].includes(answer) ? 'simple' : ['2', 'advanced'].includes(answer) ? 'advanced' : undefined;
    if (!mode) throw new ValidationError('Choose Simple (1) or Advanced (2).', []);
    if (mode === 'simple' && (gitUrl || options.targetBranch || options.force)) {
      throw new ValidationError('Simple does not support repository URLs, --target-branch or --force. Run git init explicitly first.', []);
    }

    // Create simple variables (no complex template system)
    const variables = this.createSimpleVariables(targetDirectory, task, editor, gitUrl);

    console.log(chalk.green('\n✅ Setup complete! Creating project...\n'));

    return {
      mode,
      targetDirectory,
      task,
      subagent: editor, // Use selected editor as subagent
      ...(gitUrl ? { gitUrl } : {}),
      variables,
      force: false,
      interactive: true,
    };
  }

  private async promptForDirectory(): Promise<string> {
    console.log(chalk.gray('   Enter the target directory for your project'));
    const answer = await promptInputOnce('Directory path', process.cwd());
    return path.resolve(answer || process.cwd());
  }

  private async promptForTask(): Promise<string> {
    const input = await promptMultiline({
      label: 'Describe what you want to build',
      hint: 'Finish with double Enter. Blank lines are kept.',
      prompt: '  ',
      minLength: 5,
    });

    if (!input || input.replace(/\s+/g, '').length < 5) {
      throw new ValidationError('Task description must be at least 5 characters', [
        'Provide a basic description of what you want to build',
      ]);
    }

    return input;
  }

  private async promptForEditor(): Promise<string> {
    console.log(chalk.gray('   Select your preferred AI subagent (enter number):'));
    console.log(chalk.gray('   1) Claude'));
    console.log(chalk.gray('   2) Codex'));
    console.log(chalk.gray('   3) Gemini'));
    console.log(chalk.gray('   4) Cursor'));
    console.log(chalk.gray('   5) Pi'));

    const answer = await promptInputOnce('Subagent choice', '1');
    const choice = parseInt(answer) || 1;

    switch (choice) {
      case 1:
        return 'claude';
      case 2:
        return 'codex';
      case 3:
        return 'gemini';
      case 4:
        return 'cursor';
      case 5:
        return 'pi';
      default:
        return 'claude';
    }
  }

  private async promptForGitSetup(): Promise<string | undefined> {
    console.log(chalk.gray('   Would you like to set up Git? (y/n):'));
    const answer = (await promptInputOnce('Git setup', 'y')).toLowerCase();

    if (answer === 'y' || answer === 'yes') {
      console.log(chalk.gray('   Enter Git repository URL (optional):'));
      const gitUrl = await promptInputOnce('Git URL', '');

      if (gitUrl && gitUrl.trim()) {
        return gitUrl.trim();
      }
    }

    return undefined;
  }

  private async confirmSave(targetDirectory: string): Promise<void> {
    // Check if .juno_task already exists
    const junoTaskPath = path.join(targetDirectory, '.juno_task');

    if (await fs.pathExists(junoTaskPath)) {
      console.log(chalk.yellow('   ⚠️  .juno_task directory already exists'));
      console.log(chalk.gray('   Would you like to:'));
      console.log(chalk.gray('   1) Override existing files'));
      console.log(chalk.gray('   2) Cancel'));

      const answer = await promptInputOnce('Choice', '2');
      const choice = parseInt(answer) || 2;

      if (choice !== 1) {
        console.log(chalk.blue('\n❌ Initialization cancelled'));
        process.exit(0);
      }
    }
  }

  /**
   * Simplified variable creation - no complex template system
   */
  private createSimpleVariables(
    targetDirectory: string,
    task: string,
    editor: string,
    gitUrl?: string,
  ): InitVariables {
    const projectName = path.basename(targetDirectory);
    const currentDate = new Date().toISOString().split('T')[0];
    const agentMd = editor === 'claude' ? 'CLAUDE.md' : 'AGENTS.md';

    return {
      PROJECT_NAME: projectName,
      TASK: task,
      EDITOR: editor,
      AGENTMD: agentMd,
      CURRENT_DATE: currentDate,
      VERSION: '1.0.0',
      AUTHOR: 'Development Team',
      DESCRIPTION: task.substring(0, 200) + (task.length > 200 ? '...' : ''),
      GIT_URL: gitUrl || '',
    };
  }
}

/**
 * Simplified Project Generator - basic file creation only
 * Uses direct template literals instead of a Handlebars template engine.
 */
class SimpleProjectGenerator {
  constructor(private context: InitializationContext) {}

  async generate(): Promise<void> {
    const { targetDirectory, variables, force } = this.context;
    const freshTopology = probeFreshInitTopology(targetDirectory);

    console.log(chalk.blue('📁 Creating project directory...'));

    // Ensure target directory exists
    await fs.ensureDir(targetDirectory);

    // Check if .juno_task already exists (unless force flag is set)
    const junoTaskDir = path.join(targetDirectory, '.juno_task');
    const junoTaskExists = await fs.pathExists(junoTaskDir);

    if (junoTaskExists && !force) {
      throw new ValidationError(
        'Project already initialized. Directory .juno_task already exists.',
        ['Use --force flag to overwrite existing files', 'Choose a different directory'],
      );
    }

    // Create .juno_task directory
    await fs.ensureDir(junoTaskDir);

    // Create project env file
    console.log(chalk.blue('🌱 Creating project environment file...'));
    await this.createProjectEnvFile(targetDirectory);

    // Create config.json with user's subagent choice and other settings
    console.log(chalk.blue('⚙️ Creating project configuration...'));
    await this.createConfigFile(junoTaskDir, targetDirectory);
    await fs.writeFile(path.join(targetDirectory, '.gitignore'), [
      '.agents/', '.claude/', '.env.yylo', '.juno_task/cache/', '.juno_task/locks/',
      '.juno_task/runtime/', '.juno_task/scripts/', '.pi/', '.venv_juno/',
      'AGENTS.md', 'CLAUDE.md', '',
    ].join('\n'));

    console.log(chalk.blue('📄 Creating production-ready project files...'));

    // Derive template variables
    const subagent = String(variables.EDITOR || 'claude');
    const task = String(variables.TASK || '');
    const gitUrl = String(variables.GIT_URL || 'Not specified');
    const currentDate = String(variables.CURRENT_DATE || new Date().toISOString().split('T')[0]);
    const venvPath = path.join(targetDirectory, '.venv_juno');
    const agentDocFile = String(
      variables.AGENTMD || (subagent === 'claude' ? 'CLAUDE.md' : 'AGENTS.md'),
    );

    // Write prompt.md
    await fs.writeFile(
      path.join(junoTaskDir, 'prompt.md'),
      generatePromptContent(subagent, agentDocFile),
    );

    // Write init.md
    await fs.writeFile(
      path.join(junoTaskDir, 'init.md'),
      generateInitContent(task, subagent, gitUrl),
    );

    // Write implement.md
    await fs.writeFile(
      path.join(junoTaskDir, 'implement.md'),
      generateImplementContent(currentDate, subagent),
    );

    // Write USER_FEEDBACK.md
    await fs.writeFile(path.join(junoTaskDir, 'USER_FEEDBACK.md'), generateUserFeedbackContent());

    // Write plan.md (empty by design)
    await fs.writeFile(path.join(junoTaskDir, 'plan.md'), '');

    // Create specs directory and files
    const specsDir = path.join(junoTaskDir, 'specs');
    await fs.ensureDir(specsDir);

    // Create specs/README.md
    const specsReadmeContent = `# Project Specifications

This directory contains detailed specifications for the project components.

## Specification Files

- \`requirements.md\` - Functional and non-functional requirements
- \`architecture.md\` - System architecture and design decisions
- Additional spec files will be added as needed

## File Naming Convention

- Use GenZ-style naming (descriptive, modern)
- Avoid conflicts with existing file names
- Use \`.md\` extension for all specification files
`;

    await fs.writeFile(path.join(specsDir, 'README.md'), specsReadmeContent);

    // Create specs/requirements.md
    const requirementsContent = `# Requirements Specification

## Functional Requirements

### Core Features
- **FR1**: ${variables.TASK}
- **FR2**: Automated testing and validation
- **FR3**: Git integration and version control

### User Stories
- **US1**: As a developer, I want to have clear task instructions so that I can implement the solution effectively
- **US2**: As a developer, I want to have automated workflows so that I can focus on implementation
- **US3**: As a developer, I want to have proper documentation so that others can understand the project

## Non-Functional Requirements

### Performance Requirements
- Response time: Fast execution for AI subagent interactions
- Throughput: Handle multiple parallel subagent operations
- Scalability: Scale to handle complex tasks with multiple components

### Quality Requirements
- Code quality: Clean, maintainable, and well-documented code
- Testing: Comprehensive test coverage for all implemented features
- Documentation: Clear documentation for all components and workflows

## Constraints

### Technical Constraints
- Platform: Node.js/TypeScript environment
- AI Subagents: Use ${variables.EDITOR} as primary subagent
- Version Control: Git-based workflow with automated commits

## Acceptance Criteria

### Definition of Done
- [ ] All functional requirements implemented
- [ ] Tests passing for all implemented features
- [ ] Documentation updated
- [ ] Code review completed

### Success Metrics
- Task completion: Main task successfully implemented
- Code quality: Clean, maintainable codebase
- Documentation: Complete and accurate documentation
`;

    await fs.writeFile(path.join(specsDir, 'requirements.md'), requirementsContent);

    // Create specs/architecture.md
    const architectureContent = `# Architecture Specification

## System Overview

This project uses AI-assisted development with yylo to achieve: ${variables.TASK}

## Architectural Decisions

### 1. AI-First Development
- Use ${variables.EDITOR} as primary AI subagent
- Parallel subagent processing for complex tasks
- Automated workflow orchestration

### 2. Template-Driven Development
- Production-ready templates for project initialization
- Comprehensive prompt templates for AI guidance
- Structured specification templates

### 3. Git-Integrated Workflow
- Automated commit generation
- Tag-based version management
- Branch management for features

## Technology Stack

- **Language**: TypeScript
- **Runtime**: Node.js
- **CLI**: yylo with AI subagent integration
- **Version Control**: Git
- **Documentation**: Markdown-based

## Component Architecture

### Core Components
1. **Task Management**: Task definition and execution tracking
2. **Specification Management**: Requirements and architecture documentation
3. **AI Integration**: Subagent orchestration and communication
4. **Version Control**: Automated Git workflow management

### Data Flow
1. Task definition → AI processing → Implementation
2. Specifications → Development → Testing → Documentation
3. Continuous feedback loop through USER_FEEDBACK.md

## Quality Attributes

### Performance
- Fast AI subagent response times
- Efficient parallel processing
- Minimal overhead for workflow automation

### Maintainability
- Clear separation of concerns
- Comprehensive documentation
- Standardized templates and workflows

### Scalability
- Support for complex multi-component projects
- Flexible AI subagent configuration
- Extensible template system

## Implementation Guidelines

### Code Organization
- Follow TypeScript best practices
- Use meaningful naming conventions
- Implement proper error handling
- Maintain comprehensive test coverage

### Documentation Standards
- Keep specifications up to date
- Document architectural decisions
- Provide clear usage examples
- Maintain change logs

### Quality Assurance
- Automated testing for all components
- Code review process
- Performance monitoring
- Security best practices
`;

    await fs.writeFile(path.join(specsDir, 'architecture.md'), architectureContent);

    // Write CLAUDE.md and AGENTS.md (single template, parameterised by doc type)
    await fs.writeFile(
      path.join(targetDirectory, 'CLAUDE.md'),
      generateAgentDocContent(
        'CLAUDE.md',
        subagent,
        task,
        targetDirectory,
        gitUrl,
        currentDate,
        venvPath,
      ),
    );
    await fs.writeFile(
      path.join(targetDirectory, 'AGENTS.md'),
      generateAgentDocContent(
        'AGENTS.md',
        subagent,
        task,
        targetDirectory,
        gitUrl,
        currentDate,
        venvPath,
      ),
    );

    // Create enhanced README.md in root
    const readmeContent = `# ${variables.PROJECT_NAME}

${variables.DESCRIPTION}

## Overview

This project uses yylo for AI-powered development with ${variables.EDITOR} as the primary AI subagent.

## Getting Started

### Prerequisites

- Node.js (v18 or higher)
- yylo CLI installed
- Git for version control

### Quick Start

\`\`\`bash
# Start task execution with production-ready AI instructions
yylo start

# Or use main command with preferred subagent
yylo -s ${variables.EDITOR}

# Provide feedback on the development process
yylo feedback
\`\`\`

## Project Structure

\`\`\`
.
├── .env.yylo              # Project env file auto-loaded by yylo
├── .juno_task/
│   ├── prompt.md          # Production-ready AI instructions
│   ├── init.md            # Task breakdown and constraints
│   ├── plan.md            # Dynamic planning and tracking
│   ├── implement.md       # Implementation guide and current tasks
│   ├── USER_FEEDBACK.md   # User feedback and issue tracking
│   ├── scripts/           # Utility scripts for project maintenance
│   │   ├── install_requirements.sh  # Install Python dependencies
│   │   └── clean_logs_folder.sh  # Archive old log files (3+ days)
│   └── specs/             # Comprehensive specifications
│       ├── README.md      # Specs overview and guide
│       ├── requirements.md # Detailed functional requirements
│       └── architecture.md # System architecture and design
├── CLAUDE.md              # Session documentation and learnings
├── AGENTS.md              # AI agent selection and performance tracking
└── README.md              # This file
\`\`\`

## AI-Powered Development

This project implements a sophisticated AI development workflow:

1. **Task Analysis**: AI studies existing codebase and requirements
2. **Specification Creation**: Detailed specs with parallel subagents
3. **Implementation**: AI-assisted development (up to 500 parallel agents)
4. **Testing**: Automated testing with dedicated subagents
5. **Documentation**: Continuous documentation updates
6. **Version Control**: Automated Git workflow with smart commits

## Key Features

- **Production-Ready Templates**: Comprehensive templates for AI guidance
- **Parallel Processing**: Up to 500 parallel subagents for analysis
- **Automated Workflows**: Git integration, tagging, and documentation
- **Quality Enforcement**: Strict requirements against placeholder implementations
- **User Feedback Integration**: Continuous feedback loop via USER_FEEDBACK.md
- **Session Management**: Detailed tracking of development sessions

## Configuration

The project uses \`${variables.EDITOR}\` as the primary AI subagent with these settings:
- **Parallel Agents**: Up to 500 for analysis, 1 for build/test
- **Quality Standards**: Full implementations required
- **Documentation**: Comprehensive and up-to-date
- **Version Control**: Automated Git workflow

${variables.GIT_URL ? `\n## Repository\n${variables.GIT_URL}` : ''}

## Development Workflow

1. **Review Task**: Check \`.juno_task/init.md\` for main task
2. **Check Plan**: Review \`.juno_task/plan.md\` for current priorities
3. **Track Implementation**: Follow \`.juno_task/implement.md\` for current implementation steps
4. **Provide Feedback**: Use \`yylo feedback\` for issues or suggestions
5. **Monitor Progress**: Track AI development through \`.juno_task/prompt.md\`

---

Created with yylo on ${variables.CURRENT_DATE}
${variables.EDITOR ? `using ${variables.EDITOR} as primary AI subagent` : ''}
`;

    await fs.writeFile(path.join(targetDirectory, 'README.md'), readmeContent);

    // Copy utility scripts from templates to .juno_task/scripts/
    console.log(chalk.blue('📦 Installing utility scripts...'));
    await this.copyScriptsFromTemplates(junoTaskDir);

    // Install portable lifecycle prompts/wiki and register their file-backed macros.
    // This is a required fresh-install contract: initialization must not succeed with missing macros.
    console.log(chalk.blue('🧭 Installing managed prompts and lifecycle guidance...'));
    const { ManagedProjectAssets } = await import('../../utils/managed-project-assets.js');
    await ManagedProjectAssets.update(targetDirectory, {
      silent: false,
      localization: {
        ...(this.context.targetBranch ? { targetBranch: this.context.targetBranch } : {}),
        ...(this.context.gitUrl ? { gitRemoteUrl: this.context.gitUrl } : {}),
      },
    });

    // Execute install_requirements.sh to install Python dependencies
    console.log(chalk.blue('🐍 Installing Python requirements...'));
    await this.executeInstallRequirements(junoTaskDir);

    // Set up Git repository if Git URL is provided. A fresh unborn repository
    // is committed exactly once by the topology bootstrap below.
    await this.setupGitRepository(freshTopology.eligible);

    const topology = freshTopology.eligible
      ? await establishFreshInitTopology(freshTopology, await installedCliVersion())
      : { configured: false, integrationOwner: null };
    if (topology.configured) {
      console.log(chalk.green('   ✓ Configured controller and protected integration owner'));
      console.log(chalk.dim(`   Integration owner: ${topology.integrationOwner}`));
    }

    console.log(chalk.green.bold('\n✅ Project initialization complete!'));
    this.printNextSteps(targetDirectory, String(variables.EDITOR || 'claude'));
  }

  private async createProjectEnvFile(targetDirectory: string): Promise<void> {
    const envPath = path.join(targetDirectory, '.env.yylo');

    if (!(await fs.pathExists(envPath))) {
      await fs.writeFile(
        envPath,
        '# Pin juno-kanban task storage to this project when commands run from here.\nJUNO_TASK_ROOT=.\n',
      );
      console.log(chalk.green('   ✓ Created .env.yylo'));
    } else {
      console.log(chalk.gray('   ℹ️  .env.yylo already exists'));
    }
  }

  private async createConfigFile(junoTaskDir: string, targetDirectory: string): Promise<void> {
    const selectedSubagent = (this.context.subagent || 'claude') as SubagentType;
    const selectedModel = getDefaultModelForSubagent(selectedSubagent);
    const defaults = createPersistedProjectConfigDefaults(targetDirectory);
    const configContent = {
      ...defaults,
      defaultSubagent: selectedSubagent,
      defaultModel: selectedModel,
      defaultModels: {
        ...(defaults.defaultModels as Record<string, string>),
        [selectedSubagent]: selectedModel,
      },
      mainTask: this.context.task || 'Project initialization',
      verbose: 0,
      envFileCopied: true,
      hooks: getDefaultHooks(),
    };

    const configPath = path.join(junoTaskDir, 'config.json');
    await fs.writeFile(configPath, JSON.stringify(configContent, null, 2));

    console.log(
      chalk.green(
        `   ✓ Created .juno_task/config.json with ${this.context.subagent} as default subagent`,
      ),
    );
  }

  /**
   * Copy utility scripts from templates/scripts to .juno_task/scripts directory
   * This includes scripts like clean_logs_folder.sh for log management
   */
  private async copyScriptsFromTemplates(junoTaskDir: string): Promise<void> {
    try {
      // Create scripts directory in .juno_task
      const scriptsDir = path.join(junoTaskDir, 'scripts');
      await fs.ensureDir(scriptsDir);

      // Get the template scripts directory path
      // In development: src/cli/commands/init.ts -> src/templates/scripts
      // In production (dist): dist/bin/cli.mjs -> dist/templates/scripts
      const __filename = fileURLToPath(import.meta.url);
      const __dirname = path.dirname(__filename);

      // Determine the correct path based on whether we're in dist or src
      let templatesScriptsDir: string;

      if (__dirname.includes('/dist/bin') || __dirname.includes('\\dist\\bin')) {
        // Production: dist/bin -> dist/templates/scripts
        templatesScriptsDir = path.join(__dirname, '../templates/scripts');
      } else if (
        __dirname.includes('/src/cli/commands') ||
        __dirname.includes('\\src\\cli\\commands')
      ) {
        // Development: src/cli/commands -> src/templates/scripts
        templatesScriptsDir = path.join(__dirname, '../../templates/scripts');
      } else {
        // Fallback - try both
        templatesScriptsDir = path.join(__dirname, '../../templates/scripts');
      }

      // Check if template scripts directory exists
      if (!(await fs.pathExists(templatesScriptsDir))) {
        console.log(
          chalk.yellow('   ⚠️  Template scripts directory not found, skipping script installation'),
        );
        return;
      }

      // Read all files from template scripts directory
      const scriptFiles = await fs.readdir(templatesScriptsDir);

      if (scriptFiles.length === 0) {
        console.log(chalk.gray('   ℹ️  No template scripts found to install'));
        return;
      }

      // Copy each script file
      let copiedCount = 0;
      for (const scriptFile of scriptFiles) {
        const sourcePath = path.join(templatesScriptsDir, scriptFile);
        const destPath = path.join(scriptsDir, scriptFile);

        // Only copy files (not directories)
        const stats = await fs.stat(sourcePath);
        if (stats.isFile()) {
          const sourceContent = await fs.readFile(sourcePath, 'utf8');
          if (sourceContent.startsWith('# Retired')) continue;
          await fs.copy(sourcePath, destPath);

          // Set executable permissions (chmod +x) for .sh files
          if (scriptFile.endsWith('.sh')) {
            await fs.chmod(destPath, 0o755); // rwxr-xr-x
          }

          copiedCount++;
          console.log(chalk.green(`   ✓ Installed script: ${scriptFile}`));
        }
      }

      if (copiedCount > 0) {
        console.log(
          chalk.green(`   ✓ Installed ${copiedCount} utility script(s) in .juno_task/scripts/`),
        );
      }
    } catch (error) {
      console.log(chalk.yellow('   ⚠️  Failed to copy utility scripts'));
      console.log(
        chalk.gray(`   Error: ${error instanceof Error ? error.message : String(error)}`),
      );
      console.log(chalk.gray('   Scripts can be added manually later if needed'));
    }
  }

  /**
   * Execute install_requirements.sh script to install Python dependencies
   * This runs automatically during init to install juno-kanban and other Python dependencies
   */
  private async executeInstallRequirements(junoTaskDir: string): Promise<void> {
    try {
      const scriptsDir = path.join(junoTaskDir, 'scripts');
      const installScript = path.join(scriptsDir, 'install_requirements.sh');

      // Check if install_requirements.sh exists
      if (!(await fs.pathExists(installScript))) {
        console.log(
          chalk.yellow(
            '   ⚠️  install_requirements.sh not found, skipping Python dependencies installation',
          ),
        );
        console.log(
          chalk.gray('   You can install dependencies manually: juno-kanban'),
        );
        return;
      }

      // Import child_process to execute the script
      const { execSync } = await import('child_process');

      // Execute the install_requirements.sh script
      try {
        // CRITICAL: Run the script from the initialized project root, not from .juno_task
        // and not necessarily from the caller's original cwd when --directory is used.
        // Also pin JUNO_TASK_ROOT for juno-kanban so fresh projects cannot inherit a
        // stale task root from the parent shell and write tasks/config elsewhere.
        const projectRoot = this.context.targetDirectory;
        const installEnvironment = buildChildProcessEnvironment(process.env, {
          JUNO_TASK_ROOT: projectRoot,
        });
        // init always provisions/selects the target project's runtime. An invoking
        // alias or unrelated activated project must not make install_requirements
        // treat its .venv_juno as the new controller's environment.
        delete installEnvironment.VIRTUAL_ENV;
        const output = execSync(installScript, {
          cwd: projectRoot,
          env: installEnvironment,
          encoding: 'utf8',
          stdio: 'pipe', // Capture output instead of inheriting
        });

        // Print the script output
        if (output && output.trim()) {
          console.log(output);
        }

        console.log(chalk.green('   ✓ Python requirements installation completed'));
      } catch (error: any) {
        // Script execution failed
        const errorOutput = error.stdout ? error.stdout.toString() : '';
        const errorMsg = error.stderr ? error.stderr.toString() : error.message;

        // Print any output the script produced before failing
        if (errorOutput && errorOutput.trim()) {
          console.log(errorOutput);
        }

        // Check if this is a "requirements already satisfied" scenario (exit code 0)
        if (error.status === 0) {
          console.log(chalk.green('   ✓ Python requirements installation completed'));
          return;
        }

        // Check if this is a "neither uv nor pip found" error
        if (errorMsg.includes('Neither') || errorMsg.includes('not found')) {
          console.log(chalk.yellow('   ⚠️  Python package manager not found'));
          console.log(
            chalk.gray('   Please install uv or pip manually to install Python dependencies'),
          );
          console.log(chalk.gray('   Required packages: juno-kanban'));
        } else {
          console.log(chalk.yellow('   ⚠️  Failed to install Python requirements'));
          console.log(chalk.gray(`   Error: ${errorMsg}`));
          console.log(
            chalk.gray(
              '   You can run the script manually later: .juno_task/scripts/install_requirements.sh',
            ),
          );
        }
      }
    } catch (error) {
      console.log(chalk.yellow('   ⚠️  Failed to execute install_requirements.sh'));
      console.log(
        chalk.gray(`   Error: ${error instanceof Error ? error.message : String(error)}`),
      );
      console.log(
        chalk.gray('   You can install dependencies manually: juno-kanban'),
      );
    }
  }

  private printNextSteps(targetDirectory: string, editor: string): void {
    console.log(chalk.blue('\n🎯 Next Steps:'));
    console.log(chalk.white(`   cd ${targetDirectory}`));
    console.log(chalk.white('   yylo start           # Start task execution'));
    console.log(chalk.white(`   yylo -s ${editor}       # Quick execution with ${editor}`));
    console.log(chalk.gray('\n💡 Tips:'));
    console.log(chalk.gray('   - Edit .juno_task/prompt.md to modify your main task'));
    console.log(chalk.gray('   - Use "yylo --help" to see all available commands'));
    console.log(chalk.gray('   - Run .juno_task/scripts/clean_logs_folder.sh to archive old logs'));
  }

  /**
   * Initialize Git repository and set up remote if Git URL is provided
   */
  private async setupGitRepository(skipInitialCommit = false): Promise<void> {
    if (!this.context.gitUrl) {
      return; // No Git URL provided, skip Git setup
    }

    const { targetDirectory } = this.context;

    try {
      console.log(chalk.blue('🔧 Setting up Git repository...'));

      // Check if git is available
      const { execSync, spawnSync } = await import('child_process');

      try {
        execSync('git --version', { stdio: 'ignore' });
      } catch (error) {
        console.log(chalk.yellow('   ⚠️  Git not found, skipping repository setup'));
        console.log(chalk.gray('   Install Git to enable repository initialization'));
        return;
      }

      // Initialize git repository
      try {
        const requestedBranch = (this.context.targetBranch || process.env.YYLO_TARGET_BRANCH || 'main')
          .replace(/^refs\/heads\//, '');
        const branch = /^[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(requestedBranch) &&
          !requestedBranch.includes('..') ? requestedBranch : 'main';
        // Array-form spawn: the branch name never passes through a shell string.
        spawnSync('git', ['init', '-b', branch], { cwd: targetDirectory, stdio: 'ignore' });
        console.log(chalk.green('   ✓ Initialized Git repository'));
      } catch (error) {
        // Git repository might already exist, that's okay
        console.log(chalk.yellow('   ⚠️  Git repository already exists or initialization failed'));
      }

      // Add remote if URL is provided
      if (this.context.gitUrl) {
        try {
          // Check if remote already exists
          const remotes = execSync('git remote -v', {
            cwd: targetDirectory,
            encoding: 'utf8',
          });

          if (remotes.includes('origin')) {
            console.log(chalk.yellow('   ⚠️  Git remote "origin" already exists'));
          } else {
            // Add origin remote
            // Array-form spawn: the URL never passes through a shell string.
            spawnSync('git', ['remote', 'add', 'origin', this.context.gitUrl], {
              cwd: targetDirectory,
              stdio: 'ignore',
            });
            console.log(chalk.green(`   ✓ Added remote origin: ${this.context.gitUrl}`));
          }
        } catch (error) {
          console.log(chalk.yellow('   ⚠️  Failed to add Git remote'));
        }
      }

      // Create initial commit if repository has no commits
      try {
        let hasCommits = false;
        try {
          execSync('git rev-parse HEAD', {
            cwd: targetDirectory,
            stdio: 'ignore',
          });
          hasCommits = true;
        } catch {
          // HEAD doesn't exist - fresh repo with no commits
          hasCommits = false;
        }

        if (!hasCommits && !skipInitialCommit) {
          // Add all files and create initial commit
          execSync('git add .', { cwd: targetDirectory, stdio: 'ignore' });

          const commitMessage = `Initial commit: ${this.context.task || 'Project initialization'}\n\n🤖 Generated with yylo using ${this.context.subagent} subagent\n🎯 Main Task: ${this.context.task}\n\n🚀 Generated with [yylo](https://github.com/yylo-dev/yylo)\n\nCo-Authored-By: Claude <noreply@anthropic.com>`;

          // Array-form spawn: the message is passed as a single argv element.
          spawnSync('git', ['commit', '-m', commitMessage], {
            cwd: targetDirectory,
            stdio: 'ignore',
          });
          console.log(chalk.green('   ✓ Created initial commit'));
        } else {
          console.log(chalk.gray('   ℹ️  Repository already has commits'));
        }
      } catch (error) {
        console.log(chalk.yellow('   ⚠️  Failed to create initial commit'));
        console.log(chalk.gray('   You can commit manually later'));
      }
    } catch (error) {
      console.log(chalk.yellow('   ⚠️  Git setup failed'));
      console.log(chalk.gray(`   Error: ${error}`));
      console.log(chalk.gray('   You can set up Git manually later'));
    }
  }
}

/**
 * Headless initialization for automation (simplified)
 */
class SimpleHeadlessInit {
  constructor(private options: InitCommandOptions) {}

  async initialize(): Promise<InitializationContext> {
    const targetDirectory = path.resolve(this.options.directory || process.cwd());
    const task = this.options.task || 'Define your main task objective here';
    const gitUrl = this.options.gitUrl;

    // Use subagent from options or fallback to default
    const selectedSubagent = this.options.subagent || 'claude';

    // Create simple variables
    const variables = this.createSimpleVariables(targetDirectory, task, selectedSubagent, gitUrl);

    return {
      targetDirectory,
      task,
      subagent: selectedSubagent,
      ...(gitUrl ? { gitUrl } : {}),
      ...(this.options.targetBranch ? { targetBranch: this.options.targetBranch } : {}),
      variables,
      force: this.options.force || false,
      interactive: false,
    };
  }

  private createSimpleVariables(
    targetDirectory: string,
    task: string,
    editor: string,
    gitUrl?: string,
  ): InitVariables {
    const projectName = path.basename(targetDirectory);
    const currentDate = new Date().toISOString().split('T')[0];
    const agentMd = editor === 'claude' ? 'CLAUDE.md' : 'AGENTS.md';

    return {
      PROJECT_NAME: projectName,
      TASK: task,
      EDITOR: editor,
      AGENTMD: agentMd,
      CURRENT_DATE: currentDate,
      VERSION: '1.0.0',
      AUTHOR: 'Development Team',
      DESCRIPTION: task.substring(0, 200) + (task.length > 200 ? '...' : ''),
      GIT_URL: gitUrl || '',
    };
  }
}

/**
 * Main simplified init command handler
 */
export async function initCommandHandler(
  _args: any,
  options: InitCommandOptions,
  command: Command,
): Promise<void> {
  try {
    // Get global options from command's parent program
    const globalOptions = command.parent?.opts() || {};
    const allOptions = { ...options, ...globalOptions };

    console.log(chalk.blue.bold('🎯 YYLO - Simplified Initialization'));

    let context: InitializationContext;

    // Default to interactive mode if no task is provided
    const shouldUseInteractive =
      options.interactive ||
      (!options.task && !process.env.CI) ||
      process.env.FORCE_INTERACTIVE === '1';

    if (shouldUseInteractive) {
      // Interactive mode with simplified TUI
      console.log(chalk.yellow('🚀 Starting interactive setup...'));
      const tui = new SimpleInitTUI();
      context = await tui.gather(options);
    } else {
      // Headless mode
      const headless = new SimpleHeadlessInit(allOptions);
      context = await headless.initialize();
    }

    if ((context.mode || options.mode) === 'simple') {
      const plan = await planSimpleInit(context.targetDirectory, { task: context.task, subagent: context.subagent });
      const outcome = await applySimpleInit(plan);
      printSimpleOutcome(outcome, plan.root);
      return;
    }

    if (hasSimpleWorkspaceHint(context.targetDirectory)) {
      throw new ValidationError('Simple-to-Advanced conversion is not supported, including with --force. Preserve the existing workspace.', []);
    }

    // Generate Advanced project
    const generator = new SimpleProjectGenerator(context);
    await generator.generate();

    // Install service scripts automatically
    try {
      console.log(chalk.blue('\n📦 Installing service scripts...'));
      const { ServiceInstaller } = await import('../../utils/service-installer.js');
      await ServiceInstaller.install();
      console.log(chalk.green('✓ Service scripts installed successfully'));
      console.log(chalk.dim(`  Location: ${ServiceInstaller.getServicesDir()}`));
    } catch (serviceError) {
      // Don't fail initialization if service installation fails, just warn
      console.log(chalk.yellow('⚠️  Service installation skipped'));
      if (options.verbose) {
        console.log(
          chalk.gray(
            `  ${serviceError instanceof Error ? serviceError.message : String(serviceError)}`,
          ),
        );
      }
    }

    // Ensure the process exits cleanly after successful initialization to avoid
    // lingering interactive sessions waiting for manual quit keys.
    // This makes automated TUI runs finish without requiring 'q'.
    try {
      const { EXIT_CODES } = await import('../types.js');
      process.exit(EXIT_CODES.SUCCESS);
    } catch {
      // Fallback if import path changes; still attempt graceful exit
      process.exit(0);
    }
  } catch (error) {
    if (error instanceof ValidationError) {
      console.error(chalk.red.bold('\n❌ Initialization Failed'));
      console.error(chalk.red(`   ${error.message}`));

      if (error.suggestions?.length) {
        console.error(chalk.yellow('\n💡 Suggestions:'));
        error.suggestions.forEach((suggestion) => {
          console.error(chalk.yellow(`   • ${suggestion}`));
        });
      }

      process.exit(1);
    }

    // Unexpected error
    console.error(chalk.red.bold('\n❌ Unexpected Error'));
    console.error(chalk.red(`   ${error}`));

    if (options.verbose) {
      console.error('\n📍 Stack Trace:');
      console.error(error);
    }

    process.exit(99);
  }
}

function printSimpleOutcome(outcome: string, root: string): void {
  console.log(`${outcome === 'already-initialized' ? 'Already initialized' : 'Initialized'} Simple workspace: ${root}`);
  console.log('Read .juno_task/simple-agent-guidance.md alongside your project instructions; next: yy ledger, yy pi.');
  console.log('No dependencies installed; initialization does not certify agent readiness.');
}

/**
 * Configure the init command for Commander.js (simplified)
 */
export function configureInitCommand(program: Command): void {
  program
    .command('init')
    .description('Initialize new yylo project - supports both interactive and inline modes')
    .argument(
      '[description]',
      'Task description for inline mode (optional - triggers inline mode if provided)',
    )
    .option('-s, --subagent <name>', 'AI subagent to use (claude, codex, gemini, cursor, pi)')
    .option('-g, --git-repo <url>', 'Git repository URL')
    .option('-d, --directory <path>', 'Target directory (default: current directory)')
    .option('--mode <mode>', 'Workspace mode: simple or advanced (interactive choice; headless default: advanced)')
    .option('--from-advanced <controller>', 'Simple: convert a settled Advanced controller into a fresh --directory (preview by default)')
    .option('--dry-run', 'Fresh Simple: read-only preview instead of initializing')
    .option('--format <format>', 'Machine response format: json or ndjson (Simple automation)')
    .option('--raw', 'Emit compact machine output (machine mode only)')
    .option('--plan-file <path>', 'Simple: write a fresh preview plan outside the project')
    .option('--apply-plan <path>', 'Simple: apply an inspected, unchanged initialization plan')
    .option('-f, --force', 'Force overwrite existing files (never supported in Simple mode)')
    .option('-i, --interactive', 'Force interactive mode (even if description is provided)')
    .option('--git-url <url>', 'Git repository URL (alias for --git-repo)')
    .option('--target-branch <name>', 'Product default branch (env: YYLO_TARGET_BRANCH; default: main)')
    .option('-t, --task <description>', 'Task description (alias for positional description)')
    .action(async (description, options, command) => {
      if ((options.format || options.raw || command.parent?.opts().format) && (options.mode !== 'simple' || options.interactive)) {
        throw new ValidationError('Machine output requires noninteractive --mode simple initialization.', []);
      }
      if (options.mode !== undefined && !['simple', 'advanced'].includes(options.mode)) {
        throw new ValidationError('Workspace mode must be simple or advanced.', []);
      }
      if (options.dryRun || options.fromAdvanced || options.planFile || options.applyPlan || (options.mode === 'simple' && !options.interactive)) {
        try {
          if (options.mode !== 'simple') throw new Error('Plan/apply options require --mode simple. No conversion is performed.');
          if (options.dryRun && (options.applyPlan || options.planFile || options.fromAdvanced || options.interactive)) {
            throw new Error('--dry-run is only for fresh noninteractive Simple preview; omit --apply-plan, --plan-file, --from-advanced and --interactive.');
          }
          const format = options.format ?? command.parent?.opts().format;
          if (format !== undefined && !['json', 'ndjson'].includes(format)) throw new Error('Choose --format json or ndjson.');
          const machine = resolveMachineOutput(format ? ['--format', format, ...(options.raw ? ['--raw'] : [])] : []);
          const report = (data: unknown, human: () => void) => {
            if (machine) writeMachineResponse({ schema_version: MACHINE_RESPONSE_SCHEMA, command: { name: 'init', version: 1 }, status: 'success', projection: 'default', data, error: null }, machine);
            else human();
          };
          if (description || options.task || options.force || options.interactive || options.gitRepo || options.gitUrl || options.targetBranch || options.subagent) {
            throw new Error('Simple automation supports --directory, --dry-run, --from-advanced, --plan-file or --apply-plan only. No force, managed Git options or interactive setup in automation. Use --interactive --mode simple for guided fresh setup.');
          }
          if (options.planFile && options.applyPlan) throw new Error('Choose --plan-file or --apply-plan, not both.');
          if (options.fromAdvanced && options.applyPlan) throw new Error('--apply-plan already binds the source; do not also pass --from-advanced.');
          if (options.fromAdvanced && !options.directory) throw new Error('--from-advanced requires a fresh --directory. No in-place conversion.');
          if (options.applyPlan) {
            const stat = await fs.lstat(options.applyPlan);
            if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 4194304) throw new Error('Simple plan must be a regular file no larger than 4 MiB.');
            const plan = await fs.readJson(options.applyPlan) as SimpleInitPlan | SimpleConversionPlan;
            if (options.directory) {
              const directory = path.resolve(options.directory);
              const canonical = path.join(await fs.realpath(path.dirname(directory)), path.basename(directory));
              if (canonical !== plan.root) throw new Error('--directory does not match the inspected plan root.');
            }
            const outcome = plan.schema === 'yylo_simple_conversion_plan.v1' ? await applySimpleConversion(plan) : await applySimpleInit(plan);
            report({ outcome, root: plan.root }, () => printSimpleOutcome(outcome, plan.root));
          } else {
            const plan = options.fromAdvanced ? await planSimpleConversion(options.fromAdvanced, options.directory) : await planSimpleInit(options.directory || process.cwd());
            if (options.planFile) await writeSimpleInitPlan(options.planFile, plan);
            if (options.dryRun) {
              report(plan, () => {
                console.log(`Preview only; not initialized: ${plan.root}`);
                console.log((plan as SimpleInitPlan).outcome === 'already-initialized' ? 'Already initialized; no proposed writes.' : `Proposed files: ${Object.keys((plan as SimpleInitPlan).files).join(', ')}`);
                console.log('To initialize: yy init --mode simple --directory ' + JSON.stringify(plan.root));
              });
            } else if (options.planFile || options.fromAdvanced) {
              report(plan, () => console.log(JSON.stringify(plan, null, 2)));
            } else {
              const outcome = await applySimpleInit(plan as SimpleInitPlan);
              report({ outcome, root: plan.root }, () => printSimpleOutcome(outcome, plan.root));
            }
          }
        } catch (error) {
          throw new ValidationError(`Simple initialization refused: ${error instanceof Error ? error.message : String(error)}`, []);
        }
        return;
      }
      // Determine task description from multiple possible sources
      // Priority: positional argument > --task option > interactive mode
      const taskDescription = description || options.task;

      const initOptions: InitCommandOptions = {
        mode: options.mode,
        directory: options.directory,
        force: options.force,
        task: taskDescription,
        gitUrl: options.gitRepo || options.gitUrl,
        targetBranch: options.targetBranch,
        subagent: options.subagent,
        interactive: options.interactive,
        // Global options
        verbose: options.verbose,
        quiet: options.quiet,
        config: options.config,
        logFile: options.logFile,
        logLevel: options.logLevel,
      };

      await initCommandHandler([], initOptions, command);
    })
    .addHelpText(
      'after',
      `
Workspace modes:
  yy init                         # Final question: Simple (recommended) or Advanced
  yy init --interactive --mode simple   # Guided Simple setup, no mode question
  yy init "Build an API" --mode advanced # Explicit Advanced automation

Simple workspace automation (prior Git initialization required):
  yy init --mode simple                  # Initializes now, without confirmation
  yy init --mode simple --directory /project
  yy init --mode simple --dry-run        # Read-only preview; no files written

Optional saved-plan automation:
  yy init --mode simple --directory /project --plan-file /external/simple-plan.json
  yy init --mode simple --apply-plan /external/simple-plan.json

  Plain fresh Simple init now writes files. Preview automation must use --dry-run
  or --plan-file. Conversion remains preview-only until explicit --apply-plan.
  Preserves existing AGENTS.md, CLAUDE.md and .gitignore; generates supplemental
  .juno_task/simple-agent-guidance.md and a metadata-local ignore file.
  Fresh Simple setup has no force, Git initialization, commits, worktrees, or installation.
  Omitted-mode inline automation retains Advanced behavior; --mode advanced is explicit.
  Interrupted initialization is refused until explicitly inspected/recovered.
  Runtime readiness and agent activation are separate from initialization.

Advanced to Simple (fresh destination only; source workspaces remain unchanged):
  yy init --mode simple --from-advanced /controller --directory /new-project --plan-file /external/conversion.json
  yy init --mode simple --apply-plan /external/conversion.json
  Requires a registered metadata-only controller, settled tasks and clean worktrees.
  Copies selected product history and committed Ledger data; retains inactive old
  instructions/config. No automatic cutover, remote, staging, commits or cleanup.
  Review custom instructions/settings manually. Simple-to-Advanced is unsupported.

Modes:
  Interactive Mode (default):
    $ yylo init                                    # Opens interactive TUI
    $ yylo init --interactive                      # Force interactive mode

  Inline Mode (for automation):
    $ yylo init "Build a REST API"                 # Minimal inline mode
    $ yylo init "Build a REST API" --subagent claude --git-repo https://github.com/owner/repo
    $ yylo init "Build a REST API" --subagent codex --directory ./my-project

Examples:
  # Interactive mode (default)
  $ yylo init                                    # Initialize in current directory with TUI
  $ yylo init --directory my-project             # Initialize in ./my-project with TUI

  # Inline mode (automation-friendly)
  $ yylo init "Create a TypeScript library"      # Quick init with inline description
  $ yylo init "Build web app" --subagent claude  # Specify AI subagent
  $ yylo init "API server" --git-repo https://github.com/me/repo

Arguments & Options:
  [description]              Task description (optional - triggers inline mode)
  -s, --subagent <name>      AI subagent: claude, codex, gemini, cursor, pi (default: claude)
  -g, --git-repo <url>       Git repository URL
  -d, --directory <path>     Target directory (default: current directory)
  -f, --force                Force overwrite existing files
  -i, --interactive          Force interactive mode

Interactive Flow:
  1. Project Root → Specify target directory
  2. Main Task → Multi-line description (no character limits)
  3. Subagent Selection → Choose from Claude, Codex, Gemini, Cursor
  4. Git Setup → Simple yes/no for Git configuration
  5. Save → Handle existing files with override/cancel options
  6. Workspace Mode → Simple (recommended) or Advanced; final question before writes

Notes:
  - All inline mode arguments are optional
  - Defaults: directory=cwd, subagent=claude, no git repo
  - No prompt cost calculation or token counting
  - No character limits on task descriptions
    `,
    );
}

// ---------------------------------------------------------------------------
// Template content generators (replace Handlebars template engine)
// ---------------------------------------------------------------------------

function generateInitContent(task: string, subagent: string, gitUrl: string): string {
  return `# Main Task
${task}

### Task 1
First task is to study @.juno_task/plan.md  (it may be incorrect) and is to use up to 500 subagents to study existing project
and study what is needed to achieve the main task.
From that create/update a @.juno_task/plan.md  which is a bullet point list sorted in priority of the items which have yet to be implemeneted. Think extra hard.
Study @.juno_task/plan.md to determine starting point for research and keep it up to date with items considered complete/incomplete using subagents.

### Task 2
Second Task is to understand the task, create a spec for process to follow, plan to execute, scripts to create, virtual enviroment that we need, things that we need to be aware of, how to test the scripts and follow progress.
Think hard and plan/create spec for every step of this task
and for each part create a seperate .md file under @.juno_task/spec/*

## ULTIMATE Goal
We want to achieve the main Task with respect to the Constraints section

Part 1)
Consider missing steps and plan. If the step is missing then author the specification at @.juno_task/spec/FILENAME.md (do NOT assume that it does not exist, search before creating). The naming of the module should be GenZ named and not conflict with another module name. If you create a new step then document the plan to implement in @.juno_task/plan.md


Part 2) after completing the plan, and spec, create task for implementing each part on kanban './.juno_task/scripts/kanban.sh' You need to create a task for each step of implementation and testing. You need to go through the project, the spec and plan at the end to make sure you have covered all tasks on the kanban. We will later on implement tasks from kanban one by one.
After completing the proccess an implementer agent would start the job and go through kanban tasks one by one.


### Constraints
**Preferred Subagent**: ${subagent}
**Repository URL**: ${gitUrl}
`;
}

function generatePromptContent(subagent: string, agentDocFile: string): string {
  return `0a. study @.juno_task/implement.md.

0b.  When you discover a syntax, logic, UI, User Flow Error or bug. Immediately update  Kanban with your findings using a ${subagent} subagent. When the issue is resolved, update Kanban.

999. Important: When authoring documentation capture the why tests and the backing implementation is important.

9999. Important: We want single sources of truth, no migrations/adapters. If tests unrelated to your work fail then it's your job to resolve these tests as part of the increment of change.

999999. As soon as there are no build or test errors create a git tag. If there are no git tags start at 0.0.0 and increment patch by 1 for example 0.0.1 if 0.0.0 does not exist.

999999999. You may add extra logging if required to be able to debug the issues.

9999999999. ALWAYS KEEP Tasks up to date with your learnings using a ${subagent} subagent. Especially after wrapping up/finishing your turn.



99999999999. When you learn something new about how to run the app or examples make sure you update @${agentDocFile} using a ${subagent} subagent but keep it brief. For example if you run commands multiple times before learning the correct command then that file should be updated.

999999999999. IMPORTANT when you discover a bug resolve it using ${subagent} subagents even if it is unrelated to the current piece of work after documenting it in Tasks

9999999999999999999. Keep @${agentDocFile} up to date with information on how to build the app and your learnings to optimize the build/test loop using a ${subagent} subagent.

999999999999999999999. For any bugs you notice, it's important to resolve them or document them in Tasks to be resolved using a ${subagent} subagent.

99999999999999999999999. When authoring the missing features you may author multiple standard libraries at once using up to 1000 parallel subagents

99999999999999999999999999. When Tasks, ${agentDocFile} becomes large periodically clean out the items that are completed from the file using a ${subagent} subagent.
Large ${agentDocFile} reduce the performance.



9999999999999999999999999999. DO NOT IMPLEMENT PLACEHOLDER OR SIMPLE IMPLEMENTATIONS. WE WANT FULL IMPLEMENTATIONS. DO IT OR I WILL YELL AT YOU

9999999999999999999999999999999. SUPER IMPORTANT DO NOT IGNORE. DO NOT PLACE STATUS REPORT UPDATES INTO @${agentDocFile}

99999999999999999999999999999999. After reveiwing Feedback, if you find an open issue, you need to update previously handled issues status as well. If user reporting a bug, that earlier on reported on the Tasks or @${agentDocFile} as resolved. You should update it to reflect that the issue is not resolved.
it would be ok to include past reasoning and root causing to the open issue, You should mention. <PREVIOUS_AGENT_ATTEMP> Tag and describe the approach already taken, so the agent knows 1.the issue is still open,2. past approaches to resolve it, what it was, and know that it has failed.
Tasks , USER_FEEDBACK and @${agentDocFile} should repesent truth. User Open Issue is a high level of truth. so you need to reflect it on the files.
`;
}

function generateImplementContent(currentDate: string, subagent: string): string {
  return `---
description: Execute the implementation plan by processing and executing all tasks defined in Kanban
---

## User Input
\`\`\`text
A.
**ALWAYS check remaing tasks and user feedbacks. Integrate it into the plan,
this is the primary mechanism for user input and for you to track your progress.
\`./.juno_task/scripts/kanban.sh list --limit 5\`
return the most recent 5 Tasks and their status and potential agent response to them.

**Important** ./.juno_task/scripts/kanban.sh has already installed in your enviroment and you can execute it in your bash.

A-1.
read @.juno_task/USER_FEEDBACK.md user feedback on your current execution will be writeen here. And will guide you. If user wants to talk to you while you are working , he will write into this file. first think you do is to read it file.

B.
Based on Items in **./.juno_task/scripts/kanban.sh** reflect on @.juno_task/plan.md and keep it up-to-date.
0g. Entities and their status in **./.juno_task/scripts/kanban.sh** has higher priority and level of truth than other parts of the app.
If you see user report a bug that you earlier marked as resolved, you need to investigate the issue again.
./.juno_task/scripts/kanban.sh items has the higher level of truth. Always

0e. Status in ./.juno_task/scripts/kanban.sh could be backlog, todo, in_progress, done.
in_progress, todo, backlog. That is the priority of tasks in general sense, unless you find something with 10X magnitute of importance, or if you do it first it make other tasks easier or unnecessary.


0f. After reviwing Feedback, if you find an open issue, you need to update previously handled issues status as well. If user reporting a bug, that earlier on reported on the feedback/plan or Claude.md as resolved. You should update it to reflect that the issue is not resolved.
\`./.juno_task/scripts/kanban.sh mark todo --ID {Task_ID}\`

it would be ok to include past reasoning and root causing to the open issue, You should mention. <PREVIOUS_AGENT_ATTEMP> Tag and describe the approach already taken, so the agent knows
   1.the issue is still open,
   2. past approaches to resolve it, what it was, and know that it has failed.
\`./.juno_task/scripts/kanban.sh mark todo --ID {Task_ID} --response "<PREVIOUS_AGENT_ATTEMP>{what happend before ...}<PREVIOUS_AGENT_ATTEMP>" \`

   **Note** updating response will REPLACE response. So you need to include everything important from the past as well you can check the content of a task with
   \`./.juno_task/scripts/kanban.sh get {TASK_ID}\`



C. Using parallel subagents. You may use up to 500 parallel subagents for all operations but only 1 subagent for build/tests.

D. Choose the most important 1 things, ( Based on Open Issue  and Also Tasks ), Think hard about what is the most important Task.

E. update status of most important task on ./.juno_task/scripts/kanban.sh.
(if the task is not on ./.juno_task/scripts/kanban.sh, create it ! Kanban is our source of truth)
\`./.juno_task/scripts/kanban.sh mark in_progress --ID {Task_ID}\`


F. Implement the most important 1 thing following the outline.

\`\`\`

You **MUST** consider the user input before proceeding (if not empty).

## Outline

1. Run \`./.juno_task/scripts/kanban.sh list\` from repo root and check current project status.

2. Load and analyze the implementation context:
   - **REQUIRED**: Read Kanban for the complete task list and execution plan
   - **REQUIRED**: Read plan.md for tech stack, architecture, and file structure
   - **IF EXISTS**: Read data-model.md for entities and relationships
   - **IF EXISTS**: Read contracts/ for API specifications and test requirements
   - **IF EXISTS**: Read research.md for technical decisions and constraints
   - **IF EXISTS**: Read quickstart.md for integration scenarios

3. **Project Setup Verification**:
   - **REQUIRED**: Create/verify ignore files based on actual project setup:

   **Detection & Creation Logic**:
   - Check if the following command succeeds to determine if the repository is a git repo (create/verify .gitignore if so):

     \`\`\`sh
     git rev-parse --git-dir 2>/dev/null
     \`\`\`
   - Check if Dockerfile* exists or Docker in plan.md → create/verify .dockerignore
   - Check if .eslintrc* or eslint.config.* exists → create/verify .eslintignore
   - Check if .prettierrc* exists → create/verify .prettierignore
   - Check if .npmrc or package.json exists → create/verify .npmignore (if publishing)
   - Check if terraform files (*.tf) exist → create/verify .terraformignore
   - Check if .helmignore needed (helm charts present) → create/verify .helmignore

   **If ignore file already exists**: Verify it contains essential patterns, append missing critical patterns only
   **If ignore file missing**: Create with full pattern set for detected technology

   **Common Patterns by Technology** (from plan.md tech stack):
   - **Node.js/JavaScript**: \`node_modules/\`, \`dist/\`, \`build/\`, \`*.log\`, \`.env*\`
   - **Python**: \`__pycache__/\`, \`*.pyc\`, \`.venv/\`, \`venv/\`, \`dist/\`, \`*.egg-info/\`
   - **Java**: \`target/\`, \`*.class\`, \`*.jar\`, \`.gradle/\`, \`build/\`
   - **C#/.NET**: \`bin/\`, \`obj/\`, \`*.user\`, \`*.suo\`, \`packages/\`
   - **Go**: \`*.exe\`, \`*.test\`, \`vendor/\`, \`*.out\`
   - **Universal**: \`.DS_Store\`, \`Thumbs.db\`, \`*.tmp\`, \`*.swp\`, \`.vscode/\`, \`.idea/\`

   **Tool-Specific Patterns**:
   - **Docker**: \`node_modules/\`, \`.git/\`, \`Dockerfile*\`, \`.dockerignore\`, \`*.log*\`, \`.env*\`, \`coverage/\`
   - **ESLint**: \`node_modules/\`, \`dist/\`, \`build/\`, \`coverage/\`, \`*.min.js\`
   - **Prettier**: \`node_modules/\`, \`dist/\`, \`build/\`, \`coverage/\`, \`package-lock.json\`, \`yarn.lock\`, \`pnpm-lock.yaml\`
   - **Terraform**: \`.terraform/\`, \`*.tfstate*\`, \`*.tfvars\`, \`.terraform.lock.hcl\`

5. Parse Kanban structure and extract:
   - **Task phases**: Setup, Tests, Core, Integration, Polish
   - **Task dependencies**: Sequential vs parallel execution rules
   - **Task details**: ID, description, file paths, parallel markers [P]
   - **Execution flow**: Order and dependency requirements

6. Execute implementation following the task plan:
   - **Phase-by-phase execution**: Complete each phase before moving to the next
   - **Respect dependencies**: Run sequential tasks in order, parallel tasks [P] can run together
   - **Follow TDD approach**: Execute test tasks before their corresponding implementation tasks
   - **File-based coordination**: Tasks affecting the same files must run sequentially
   - **Validation checkpoints**: Verify each phase completion before proceeding

7. Implementation execution rules:
   - **Setup first**: Initialize project structure, dependencies, configuration
   - **Tests before code**: If you need to write tests for contracts, entities, and integration scenarios
   - **Core development**: Implement models, services, CLI commands, endpoints
   - **Integration work**: Database connections, middleware, logging, external services
   - **Polish and validation**: Unit tests, performance optimization, documentation

8. Progress tracking and error handling:
   - Report progress after each completed task
   - Halt execution if any non-parallel task fails
   - For parallel tasks [P], continue with successful tasks, report failed ones
   - Provide clear error messages with context for debugging
   - Suggest next steps if implementation cannot proceed
   - **IMPORTANT** For completed tasks, make sure to mark the task off as [X] in the tasks file.
   - **IMPORTANT** Keep ./.juno_task/scripts/kanban.sh up-to-date
   When the issue is resolved always update ./.juno_task/scripts/kanban.sh
   \`./.juno_task/scripts/kanban.sh --status {status} --ID {task_id} --response "{key actions you take, and how you did test it}"\`

9. Completion validation:
   - Verify all required tasks are completed
   - Check that implemented features match the original specification
   - Validate that tests pass and coverage meets requirements
   - Confirm the implementation follows the technical plan
   - Report final status with summary of completed work
   - When the issue is resolved always update ./.juno_task/scripts/kanban.sh
   \` ./.juno_task/scripts/kanban.sh --mark done --ID {task_id} --response "{key actions you take, and how you did test it}" \`

10. Git

   When the tests pass update ./.juno_task/scripts/kanban.sh, then add changed code with "git add -A" via bash then do a "git commit" with a message that describes the changes you made to the code. After the commit do a "git push" to push the changes to the remote repository.
   Use commit message as a backlog of what has achieved. So later on we would know exactly what we achieved in each commit.
   Update the task in ./.juno_task/scripts/kanban.sh with the commit hash so later on we could map each task to a specific git commit
   \`./.juno_task/scripts/kanban.sh update {task_id} --commit {commit_hash}\`



Note: This command assumes a complete task breakdown exists in Kanban. If tasks are incomplete or missing, suggest running \`/tasks\` first to regenerate the task list.


---
*Last updated: ${currentDate}*
*Primary subagent: ${subagent}*`;
}

function generateUserFeedbackContent(): string {
  return `## User Feedback
`;
}

/**
 * Single template for both CLAUDE.md and AGENTS.md.
 * The two files were 95% identical — consolidated into one source of truth.
 */
function generateAgentDocContent(
  docType: 'CLAUDE.md' | 'AGENTS.md',
  subagent: string,
  task: string,
  projectPath: string,
  gitUrl: string,
  currentDate: string,
  venvPath: string,
): string {
  const title =
    docType === 'CLAUDE.md'
      ? '# Claude Code Session Documentation'
      : '# AGENTS.md Session Documentation';

  return `${title}

## Current Project Configuration

**Selected Coding Agent:** ${subagent}
**Main Task:** ${task}
**Project Path:** ${projectPath}
**Git Repository:** ${gitUrl}
**Configuration Date:** ${currentDate}

## Kanban Task Management

For comprehensive kanban usage (all commands, dependency management, best practices), use the \`ledger-tasks-yylo\` skill.

\`\`\`bash
# List tasks
./.juno_task/scripts/kanban.sh list --limit 5 --sort asc
./.juno_task/scripts/kanban.sh list --status [backlog|todo|in_progress|done] --sort asc

# Task operations
./.juno_task/scripts/kanban.sh get {TASK_ID}
./.juno_task/scripts/kanban.sh mark [in_progress|done|todo] --id {TASK_ID} --response "message"
./.juno_task/scripts/kanban.sh update {TASK_ID} --commit {COMMIT_HASH}
\`\`\`

When a task on kanban, has related_tasks key, you need to get the task to understand the complete picture of tasks related to the current current task, you can get all the context through
\`./.juno_task/scripts/kanban.sh get {TASK_ID}\`

When creating a task, relevant to another task, you can add the following format anywhere in the body of the task : \`[task_id]{Ref_TASK_ID}[/task_id]\` , using ref task id, help kanban organize dependecies between tasks better.

Important: You need to get maximum 3 tasks done in one go.

## Agent-Specific Instructions

### ${subagent} Configuration
- **Recommended Model:** Latest available model for ${subagent}
- **Interaction Style:** Professional and detail-oriented
- **Code Quality:** Focus on production-ready, well-documented code
- **Testing:** Comprehensive unit and integration tests required

## Build & Test Commands

**Environment Setup:**
\`\`\`bash
# Activate virtual environment (if applicable)
source ${venvPath}/bin/activate

# Navigate to project
cd ${projectPath}
\`\`\`

**Testing:**
\`\`\`bash
# Run tests
python -m pytest tests/ -v

# Run with coverage
python -m pytest tests/ --cov=src --cov-report=term-missing
\`\`\`

**Development Notes:**
- Keep this file updated with important learnings and optimizations
- Document any environment-specific setup requirements
- Record successful command patterns for future reference

## Session History

| Date | Agent | Task Summary | Status |
|------|-------|--------------|---------|
| ${currentDate} | ${subagent} | Project initialization | ✅ Completed |

## Agent Performance Notes

### ${subagent} Observations:
- Initial setup: Successful
- Code quality: To be evaluated
- Test coverage: To be assessed
- Documentation: To be reviewed

*Note: Update this section with actual performance observations during development*`;
}
