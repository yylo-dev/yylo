/**
 * Test command implementation for yylo CLI
 *
 * Comprehensive AI-powered testing framework with intelligent test generation,
 * execution, analysis, and reporting capabilities.
 */

import * as path from 'node:path';
import fs from 'fs-extra';
import chalk from 'chalk';
import { Command } from 'commander';

import { loadConfig } from '../../core/config.js';
import {
  createExecutionEngine,
  createExecutionRequest,
} from '../../core/engine.js';
import { createSessionManager } from '../../core/session.js';
import { cliLogger, engineLogger } from '../utils/advanced-logger.js';
import type { TestCommandOptions } from '../types.js';
import { ValidationError, ConfigurationError, RuntimeError } from '../types.js';
import type { JunoTaskConfig, SubagentType } from '../../types/index.js';
import type { ProgressEvent } from '../../types/execution.js';
import type { SessionManager } from '../../core/session.js';
import {
  runTestAffectedCommand,
  runTestDaemonCommand,
} from '../../test-daemon/cli.js';

// ============================================================================
// Test Framework Types
// ============================================================================

export type TestType = 'unit' | 'integration' | 'e2e' | 'performance' | 'all';
export type IntelligenceLevel = 'basic' | 'smart' | 'comprehensive';
export type ReportFormat = 'json' | 'html' | 'markdown' | 'console';
export type TestFramework = 'vitest' | 'jest' | 'mocha' | 'custom';

interface TestGenerationRequest {
  target: string[];
  type: TestType;
  intelligence: IntelligenceLevel;
  template?: string;
  framework: TestFramework;
  subagent: SubagentType;
  workingDirectory: string;
}

interface TestExecutionRequest {
  target?: string[];
  framework: TestFramework;
  coverage?: boolean | string;
  watch?: boolean;
  reporters?: string[];
  config?: string;
  workingDirectory: string;
}

interface TestAnalysisRequest {
  results: any;
  coverage?: any;
  quality: 'basic' | 'thorough' | 'exhaustive';
  suggestions: boolean;
  subagent: SubagentType;
}

interface TestReportRequest {
  analysis: any;
  format: ReportFormat;
  outputPath?: string;
  includeVisualizations: boolean;
}

interface TestExecutionResult {
  success: boolean;
  totalTests: number;
  passedTests: number;
  failedTests: number;
  skippedTests: number;
  coverage?: any;
  executionTime: number;
  framework: TestFramework;
}

// ============================================================================
// AI Subagent Specialization
// ============================================================================

const TEST_SUBAGENT_SPECIALIZATIONS: Record<SubagentType, {
  strengths: string[];
  bestFor: string[];
  capabilities: string[];
}> = {
  claude: {
    strengths: ['analytical', 'comprehensive', 'documentation'],
    bestFor: ['complex-systems', 'integration-tests', 'architecture-validation'],
    capabilities: ['semantic-analysis', 'dependency-mapping', 'contract-testing'],
  },
  cursor: {
    strengths: ['code-centric', 'debugging', 'optimization'],
    bestFor: ['unit-tests', 'component-tests', 'performance-tests'],
    capabilities: ['static-analysis', 'edge-cases', 'performance-profiling'],
  },
  codex: {
    strengths: ['versatile', 'general-purpose', 'quick-generation'],
    bestFor: ['basic-tests', 'boilerplate', 'simple-scenarios'],
    capabilities: ['template-filling', 'pattern-matching', 'basic-scenarios'],
  },
  gemini: {
    strengths: ['creative', 'alternative-approaches', 'edge-cases'],
    bestFor: ['edge-case-testing', 'security-tests', 'unusual-scenarios'],
    capabilities: ['unusual-scenarios', 'security-analysis', 'stress-testing'],
  },
  pi: {
    strengths: ['code-generation', 'task-execution', 'automation'],
    bestFor: ['automated-tasks', 'code-generation', 'scripting'],
    capabilities: ['code-generation', 'task-automation', 'script-execution'],
  },
};

// ============================================================================
// Progress Display for Test Operations
// ============================================================================

class TestProgressDisplay {
  private startTime: Date = new Date();
  public verbose: number;

  constructor(verbose: number = 0) {
    this.verbose = verbose;
  }

  start(operation: string): void {
    this.startTime = new Date();
    console.log(chalk.blue.bold(`\n🧪 ${operation}`));
  }

  onProgress(event: ProgressEvent): void {
    // If this is raw JSON output from shell backend (jq-style formatting)
    // Display it with colors and indentation like `claude.py | jq .`
    if (event.metadata?.rawJsonOutput) {
      try {
        // Parse and re-format with indentation
        const jsonObj = JSON.parse(event.content);
        const formattedJson = this.colorizeJson(jsonObj);
        const backend = event.backend ? chalk.cyan(`[${event.backend}]`) : '';
        const timestamp = event.timestamp.toLocaleTimeString();

        if (this.verbose) {
          console.log(`${chalk.gray(timestamp)} ${backend} ${formattedJson}`);
        } else {
          console.log(`${backend} ${formattedJson}`);
        }
        return;
      } catch (error) {
        // If JSON parsing fails, fall back to raw output
        const backend = event.backend ? `[${event.backend}]` : '';
        console.log(`${backend} ${event.content}`);
        return;
      }
    }

    if (this.verbose) {
      const timestamp = event.timestamp.toLocaleTimeString();
      const content =
        event.content.length > 100 ? event.content.substring(0, 100) + '...' : event.content;

      console.log(chalk.gray(`[${timestamp}] ${event.type}: ${content}`));
    } else {
      // Show meaningful progress for test operations
      const eventType = event.type as string;
      if (eventType === 'test_generation') {
        process.stdout.write(chalk.blue(`\r🤖 Generating tests: ${event.content}`));
      } else if (eventType === 'test_execution') {
        process.stdout.write(chalk.green(`\r▶️  Running: ${event.content}`));
      } else if (eventType === 'test_analysis') {
        process.stdout.write(chalk.yellow(`\r📊 Analyzing: ${event.content}`));
      } else if (eventType === 'test_reporting') {
        process.stdout.write(chalk.cyan(`\r📋 Reporting: ${event.content}`));
      }
    }
  }

  phaseComplete(phase: string, result?: string): void {
    const elapsed = this.getElapsedTime();
    const icon = this.getStatusIcon(true);
    console.log(`${icon} ${phase} complete ${chalk.gray(`(${elapsed})`)}`);

    if (result && this.verbose) {
      console.log(chalk.gray(`   ${result}`));
    }
  }

  showResults(results: TestExecutionResult): void {
    const elapsed = this.getElapsedTime();

    console.log(chalk.green.bold(`\n✅ Test Execution Complete ${chalk.gray(`(${elapsed})`)}`));

    console.log(chalk.blue('\n📊 Test Results:'));
    console.log(chalk.white(`   Total Tests: ${results.totalTests}`));
    console.log(chalk.green(`   Passed: ${results.passedTests}`));

    if (results.failedTests > 0) {
      console.log(chalk.red(`   Failed: ${results.failedTests}`));
    }

    if (results.skippedTests > 0) {
      console.log(chalk.yellow(`   Skipped: ${results.skippedTests}`));
    }

    console.log(
      chalk.white(
        `   Success Rate: ${((results.passedTests / results.totalTests) * 100).toFixed(1)}%`,
      ),
    );
    console.log(chalk.white(`   Execution Time: ${results.executionTime}ms`));
    console.log(chalk.white(`   Framework: ${results.framework}`));

    if (results.coverage) {
      console.log(chalk.blue('\n📈 Coverage Summary:'));
      console.log(chalk.white(`   Lines: ${results.coverage.lines?.toFixed(1) || 'N/A'}%`));
      console.log(chalk.white(`   Functions: ${results.coverage.functions?.toFixed(1) || 'N/A'}%`));
      console.log(chalk.white(`   Branches: ${results.coverage.branches?.toFixed(1) || 'N/A'}%`));
      console.log(
        chalk.white(`   Statements: ${results.coverage.statements?.toFixed(1) || 'N/A'}%`),
      );
    }
  }

  showError(error: Error): void {
    console.log(chalk.red(`\n❌ Test operation failed: ${error.message}`));
  }

  /**
   * Colorize JSON object for pretty terminal output (jq-style)
   */
  private colorizeJson(obj: any): string {
    const json = JSON.stringify(obj, null, 2);

    // Apply colors to different JSON elements
    const colored = json
      // Keys (property names)
      .replace(/"([^"]+)":/g, (_match, key) => `${chalk.blue(`"${key}"`)}:`)
      // String values
      .replace(/: "([^"]*)"/g, (_match, value) => `: ${chalk.green(`"${value}"`)}`)
      // Numbers
      .replace(/: (\d+\.?\d*)/g, (_match, num) => `: ${chalk.yellow(num)}`)
      // Booleans and null
      .replace(/: (true|false|null)/g, (_match, val) => `: ${chalk.magenta(val)}`);

    return colored;
  }

  private getElapsedTime(): string {
    const elapsed = Date.now() - this.startTime.getTime();
    const seconds = Math.floor(elapsed / 1000);
    const minutes = Math.floor(seconds / 60);

    if (minutes > 0) {
      return `${minutes}m ${seconds % 60}s`;
    }
    return `${seconds}s`;
  }

  private getStatusIcon(success: boolean): string {
    return success ? chalk.green('✅') : chalk.red('❌');
  }
}

// ============================================================================
// Test Generation Engine
// ============================================================================

class TestGenerationEngine {
  private progressDisplay: TestProgressDisplay;
  private config: JunoTaskConfig;

  constructor(
    config: JunoTaskConfig,
    _sessionManager: SessionManager,
    progressDisplay: TestProgressDisplay,
  ) {
    this.config = config;
    this.progressDisplay = progressDisplay;
  }

  async generateTests(request: TestGenerationRequest): Promise<string[]> {
    this.progressDisplay.start('AI-Powered Test Generation');

    try {
      // Analyze target files/directories
      const analysis = await this.analyzeTarget(request);

      // Generate test scenarios based on intelligence level
      const scenarios = await this.generateScenarios(request, analysis);

      // Create test files using AI subagent
      const testFiles = await this.createTestFiles(request, scenarios);

      this.progressDisplay.phaseComplete(
        'Test generation',
        `Generated ${testFiles.length} test files`,
      );

      return testFiles;
    } catch (error) {
      this.progressDisplay.showError(error as Error);
      throw error;
    }
  }

  private async analyzeTarget(request: TestGenerationRequest): Promise<any> {
    const instruction = `
Analyze the following target for test generation:
- Target: ${request.target.join(', ')}
- Test Type: ${request.type}
- Intelligence Level: ${request.intelligence}
- Framework: ${request.framework}

Please provide:
1. Code structure analysis
2. Dependency mapping
3. Function/method identification
4. Testability assessment
5. Recommended test scenarios

Focus on ${
      request.intelligence === 'basic'
        ? 'basic functionality'
        : request.intelligence === 'smart'
          ? 'comprehensive scenarios'
          : 'exhaustive analysis'
    }.
`;

    const executionRequest = createExecutionRequest({
      instruction,
      subagent: request.subagent,
      workingDirectory: request.workingDirectory,
      maxIterations: 3,
    });

    // Execute with shell backend
    const engine = createExecutionEngine(this.config);

    engine.onProgress(async (event: ProgressEvent) => {
      this.progressDisplay.onProgress(event);
    });

    try {
      const result = await engine.execute(executionRequest);
      await engine.shutdown();

      return result;
    } catch (error) {
      await engine.shutdown();
      throw error;
    }
  }

  private async generateScenarios(request: TestGenerationRequest, analysis: any): Promise<any[]> {
    const scenarios = [];

    // Generate scenarios based on intelligence level
    if (request.intelligence === 'basic') {
      scenarios.push(...(await this.generateBasicScenarios(request, analysis)));
    } else if (request.intelligence === 'smart') {
      scenarios.push(...(await this.generateBasicScenarios(request, analysis)));
      scenarios.push(...(await this.generateSmartScenarios(request, analysis)));
    } else {
      scenarios.push(...(await this.generateBasicScenarios(request, analysis)));
      scenarios.push(...(await this.generateSmartScenarios(request, analysis)));
      scenarios.push(...(await this.generateComprehensiveScenarios(request, analysis)));
    }

    return scenarios;
  }

  private async generateBasicScenarios(
    _request: TestGenerationRequest,
    _analysis: any,
  ): Promise<any[]> {
    // Basic happy path and error scenarios
    return [
      { type: 'happy-path', description: 'Basic functionality test' },
      { type: 'error-handling', description: 'Error scenarios test' },
      { type: 'edge-cases', description: 'Boundary value tests' },
    ];
  }

  private async generateSmartScenarios(
    _request: TestGenerationRequest,
    _analysis: any,
  ): Promise<any[]> {
    // Integration and complex scenarios
    return [
      { type: 'integration', description: 'Component integration tests' },
      { type: 'performance', description: 'Performance benchmark tests' },
      { type: 'security', description: 'Basic security tests' },
    ];
  }

  private async generateComprehensiveScenarios(
    request: TestGenerationRequest,
    _analysis: any,
  ): Promise<any[]> {
    // Advanced scenarios based on subagent specialization
    const specialization = TEST_SUBAGENT_SPECIALIZATIONS[request.subagent];

    return specialization.capabilities.map((capability: string) => ({
      type: capability,
      description: `Advanced ${capability} tests`,
    }));
  }

  private async createTestFiles(
    request: TestGenerationRequest,
    scenarios: any[],
  ): Promise<string[]> {
    const template = await this.selectTemplate(request);
    const testFiles: string[] = [];

    for (const scenario of scenarios) {
      const testContent = await this.generateTestContent(request, scenario, template);
      const testFilePath = this.resolveTestFilePath(request, scenario);

      await fs.ensureDir(path.dirname(testFilePath));
      await fs.writeFile(testFilePath, testContent);
      testFiles.push(testFilePath);
    }

    return testFiles;
  }

  private async selectTemplate(request: TestGenerationRequest): Promise<string> {
    // Select appropriate template based on type and framework
    // In a real implementation, this would load from a templates directory
    // Template selection would use: request.template || `${request.type}-${request.framework}`
    return this.getDefaultTemplate(request.type, request.framework);
  }

  private getDefaultTemplate(type: TestType, framework: TestFramework): string {
    switch (`${type}-${framework}`) {
      case 'unit-vitest':
        return this.getVitestUnitTemplate();
      case 'integration-vitest':
        return this.getVitestIntegrationTemplate();
      case 'e2e-playwright':
        return this.getPlaywrightE2ETemplate();
      default:
        return this.getGenericTemplate();
    }
  }

  private getVitestUnitTemplate(): string {
    return `
import { describe, it, expect, beforeEach, afterEach } from 'vitest';

describe('{{functionName}}', () => {
  // Setup and teardown
  beforeEach(() => {
    // Test setup
  });

  afterEach(() => {
    // Test cleanup
  });

  // Generated test cases
  {{testCases}}
});
    `.trim();
  }

  private getVitestIntegrationTemplate(): string {
    return `
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { setupServer } from 'msw/node';
import { http, HttpResponse } from 'msw';

// Mock server setup
const server = setupServer(
  // Generated mock handlers
);

describe('{{featureName}} Integration', () => {
  beforeAll(() => server.listen());
  afterAll(() => server.close());

  // Generated integration tests
  {{testCases}}
});
    `.trim();
  }

  private getPlaywrightE2ETemplate(): string {
    return `
import { test, expect } from '@playwright/test';

test.describe('{{featureName}} E2E', () => {
  test('should complete user flow', async ({ page }) => {
    // Generated E2E test steps
    {{testSteps}}
  });
});
    `.trim();
  }

  private getGenericTemplate(): string {
    return `
// Generic test template
describe('{{testName}}', () => {
  it('should work', () => {
    // Test implementation
    expect(true).toBe(true);
  });
});
    `.trim();
  }

  private async generateTestContent(
    request: TestGenerationRequest,
    scenario: any,
    template: string,
  ): Promise<string> {
    const instruction = `
Generate test content for the following scenario:
- Type: ${request.type}
- Framework: ${request.framework}
- Scenario: ${scenario.description}
- Template: ${template}

Create comprehensive test cases that follow best practices for ${request.framework}.
Focus on ${request.intelligence} level testing with proper assertions, mocking, and edge cases.

Return only the test code that should be placed in the template.
`;

    void instruction;
    return template
      .replace('{{functionName}}', 'GeneratedFunction')
      .replace('{{testCases}}', this.generatePlaceholderTestCases(scenario))
      .replace('{{featureName}}', 'GeneratedFeature')
      .replace('{{testSteps}}', this.generatePlaceholderTestSteps(scenario))
      .replace('{{testName}}', 'GeneratedTest');
  }

  private generatePlaceholderTestCases(scenario: any): string {
    return `
  it('should handle ${scenario.type}', () => {
    // Generated test implementation for ${scenario.description}
    expect(true).toBe(true);
  });
    `.trim();
  }

  private generatePlaceholderTestSteps(scenario: any): string {
    return `
    // Generated E2E steps for ${scenario.description}
    await page.goto('/');
    expect(await page.title()).toContain('Test');
    `.trim();
  }

  private resolveTestFilePath(request: TestGenerationRequest, scenario: any): string {
    const target = request.target[0] || 'src';
    const testName = scenario.type.replace('-', '_');
    const extension = this.getTestFileExtension(request.framework);

    if (request.type === 'e2e') {
      return path.join(
        request.workingDirectory,
        'tests',
        'e2e',
        `${testName}.e2e.test${extension}`,
      );
    } else {
      return path.join(request.workingDirectory, target, `${testName}.test${extension}`);
    }
  }

  private getTestFileExtension(framework: TestFramework): string {
    switch (framework) {
      case 'vitest':
      case 'jest':
        return '.ts';
      case 'mocha':
        return '.js';
      default:
        return '.ts';
    }
  }
}

// ============================================================================
// Test Execution Engine
// ============================================================================

class TestExecutionEngine {
  private progressDisplay: TestProgressDisplay;

  constructor(progressDisplay: TestProgressDisplay) {
    this.progressDisplay = progressDisplay;
  }

  async executeTests(request: TestExecutionRequest): Promise<TestExecutionResult> {
    this.progressDisplay.start('Test Execution');

    try {
      const startTime = Date.now();

      // Determine test command based on framework
      const testCommand = this.buildTestCommand(request);

      // Execute tests
      const result = await this.runTestCommand(testCommand, request.workingDirectory);

      // Parse results
      const testResults = this.parseTestResults(result);

      // Collect coverage if requested
      const coverage = request.coverage ? await this.collectCoverage(request) : undefined;

      this.progressDisplay.showResults({
        ...testResults,
        coverage,
        executionTime: Date.now() - startTime,
        framework: request.framework,
      });

      return {
        ...testResults,
        coverage,
        executionTime: Date.now() - startTime,
        framework: request.framework,
      };
    } catch (error) {
      this.progressDisplay.showError(error as Error);
      throw error;
    }
  }

  private buildTestCommand(request: TestExecutionRequest): string[] {
    const baseCommand = this.getFrameworkCommand(request.framework);
    const command = [...baseCommand];

    // Add coverage flag
    if (request.coverage) {
      command.push('--coverage');
      if (typeof request.coverage === 'string') {
        command.push('--reporter=html', `--outputFile=${request.coverage}`);
      }
    }

    // Add watch mode
    if (request.watch) {
      command.push('--watch');
    }

    // Add custom config
    if (request.config) {
      command.push('--config', request.config);
    }

    // Add reporters
    if (request.reporters && request.reporters.length > 0) {
      request.reporters.forEach((reporter) => {
        command.push('--reporter', reporter);
      });
    }

    // Add target
    if (request.target && request.target.length > 0) {
      command.push(...request.target);
    } else {
      command.push('**/*.test.{ts,js}');
    }

    return command;
  }

  private getFrameworkCommand(framework: TestFramework): string[] {
    switch (framework) {
      case 'vitest':
        return ['npx', 'vitest', 'run'];
      case 'jest':
        return ['npx', 'jest', '--passWithNoTests'];
      case 'mocha':
        return ['npx', 'mocha'];
      default:
        return ['npm', 'test'];
    }
  }

  private async runTestCommand(command: string[], cwd: string): Promise<any> {
    const { execa } = await import('execa');
    const cmd = command[0] || 'npm';

    try {
      const result = await execa(cmd, command.slice(1), {
        cwd,
        stdio: this.progressDisplay.verbose ? 'inherit' as const : 'pipe' as const,
      });

      return {
        stdout: result.stdout,
        stderr: result.stderr,
        exitCode: result.exitCode,
      };
    } catch (error: any) {
      return {
        stdout: error.stdout || '',
        stderr: error.stderr || '',
        exitCode: error.exitCode || 1,
      };
    }
  }

  private parseTestResults(
    result: any,
  ): Omit<TestExecutionResult, 'coverage' | 'executionTime' | 'framework'> {
    // Parse test results from output
    // This is a simplified parser - real implementation would be more robust

    const stdout = result.stdout || '';
    const stderr = result.stderr || '';

    // Try to extract test counts from output
    const totalMatch = stdout.match(/(\d+)\s+test(s?)/i) || stderr.match(/(\d+)\s+test(s?)/i);
    const passedMatch =
      stdout.match(/(\d+)\s+passing|(\d+)\s+passed/i) ||
      stderr.match(/(\d+)\s+passing|(\d+)\s+passed/i);
    const failedMatch =
      stdout.match(/(\d+)\s+failing|(\d+)\s+failed/i) ||
      stderr.match(/(\d+)\s+failing|(\d+)\s+failed/i);
    const skippedMatch =
      stdout.match(/(\d+)\s+skipping|(\d+)\s+skipped/i) ||
      stderr.match(/(\d+)\s+skipping|(\d+)\s+skipped/i);

    const totalTests = totalMatch ? parseInt(totalMatch[1]) : 0;
    const passedTests = passedMatch ? parseInt(passedMatch[1] || passedMatch[2]) : 0;
    const failedTests = failedMatch ? parseInt(failedMatch[1] || failedMatch[2]) : 0;
    const skippedTests = skippedMatch ? parseInt(skippedMatch[1] || skippedMatch[2]) : 0;

    return {
      success: result.exitCode === 0,
      totalTests,
      passedTests,
      failedTests,
      skippedTests,
    };
  }

  private async collectCoverage(request: TestExecutionRequest): Promise<any> {
    const coverageFile = path.join(request.workingDirectory, 'coverage', 'coverage-summary.json');

    try {
      if (await fs.pathExists(coverageFile)) {
        const coverageData = await fs.readJson(coverageFile);
        return {
          lines: coverageData.total?.lines?.pct || 0,
          functions: coverageData.total?.functions?.pct || 0,
          branches: coverageData.total?.branches?.pct || 0,
          statements: coverageData.total?.statements?.pct || 0,
        };
      }
    } catch (error) {
      // Coverage file not found or invalid
    }

    return undefined;
  }
}

// ============================================================================
// Test Analysis Engine
// ============================================================================

class TestAnalysisEngine {
  private progressDisplay: TestProgressDisplay;

  constructor(
    _config: JunoTaskConfig,
    _sessionManager: SessionManager,
    progressDisplay: TestProgressDisplay,
  ) {
    this.progressDisplay = progressDisplay;
  }

  async analyzeTests(request: TestAnalysisRequest): Promise<any> {
    this.progressDisplay.start('Test Quality Analysis');

    try {
      // Calculate basic metrics
      const metrics = this.calculateMetrics(request.results, request.coverage);

      // Generate AI-powered insights if requested
      let insights: string[] = [];
      if (request.quality === 'thorough' || request.quality === 'exhaustive') {
        insights = await this.generateInsights(request, metrics);
      }

      // Generate suggestions if requested
      let suggestions: string[] = [];
      if (request.suggestions) {
        suggestions = await this.generateSuggestions(metrics, insights);
      }

      this.progressDisplay.phaseComplete('Test analysis', `Analyzed ${metrics.totalTests} tests`);

      return {
        metrics,
        insights,
        suggestions,
        quality: this.assessQuality(metrics),
        recommendations: this.generateRecommendations(metrics, insights),
      };
    } catch (error) {
      this.progressDisplay.showError(error as Error);
      throw error;
    }
  }

  private calculateMetrics(results: TestExecutionResult, coverage?: any): any {
    const passRate = results.totalTests > 0 ? (results.passedTests / results.totalTests) * 100 : 0;
    const failRate = results.totalTests > 0 ? (results.failedTests / results.totalTests) * 100 : 0;
    const skipRate = results.totalTests > 0 ? (results.skippedTests / results.totalTests) * 100 : 0;

    return {
      totalTests: results.totalTests,
      passedTests: results.passedTests,
      failedTests: results.failedTests,
      skippedTests: results.skippedTests,
      passRate,
      failRate,
      skipRate,
      executionTime: results.executionTime,
      framework: results.framework,
      coverage: coverage || {},
    };
  }

  private async generateInsights(request: TestAnalysisRequest, metrics: any): Promise<string[]> {
    const instruction = `
Analyze the following test results and provide insights:
- Total Tests: ${metrics.totalTests}
- Pass Rate: ${metrics.passRate.toFixed(1)}%
- Coverage: ${JSON.stringify(metrics.coverage)}
- Quality Level: ${request.quality}

Please provide:
1. Test quality assessment
2. Coverage gaps identification
3. Performance observations
4. Maintenance concerns
5. Best practices compliance

Focus on actionable insights that can improve test quality and effectiveness.
`;

    void instruction;
    return [
      `Test pass rate of ${metrics.passRate.toFixed(1)}% is ${metrics.passRate >= 90 ? 'excellent' : metrics.passRate >= 75 ? 'good' : 'needs improvement'}`,
      `Coverage analysis shows ${metrics.coverage.lines?.toFixed(1) || 'N/A'}% line coverage`,
      `Test execution time of ${metrics.executionTime}ms is ${metrics.executionTime < 5000 ? 'optimal' : 'could be optimized'}`,
      'Consider adding more edge case tests for comprehensive coverage',
    ];
  }

  private async generateSuggestions(metrics: any, insights: string[]): Promise<string[]> {
    const suggestions = [];

    // Coverage suggestions
    if (metrics.coverage.lines < 80) {
      suggestions.push('Increase line coverage by adding tests for uncovered code paths');
    }

    if (metrics.coverage.branches < 75) {
      suggestions.push('Add branch coverage tests to handle all conditional logic');
    }

    // Pass rate suggestions
    if (metrics.passRate < 90) {
      suggestions.push('Investigate and fix failing tests to improve pass rate');
    }

    // Performance suggestions
    if (metrics.executionTime > 10000) {
      suggestions.push('Optimize test performance by using mocking and parallel execution');
    }

    // Add AI-generated suggestions
    suggestions.push(...insights.map((insight) => `🤖 AI Insight: ${insight}`));

    return suggestions;
  }

  private assessQuality(metrics: any): 'excellent' | 'good' | 'fair' | 'poor' {
    const score = this.calculateQualityScore(metrics);

    if (score >= 90) return 'excellent';
    if (score >= 75) return 'good';
    if (score >= 60) return 'fair';
    return 'poor';
  }

  private calculateQualityScore(metrics: any): number {
    let score = 0;

    // Pass rate (40% weight)
    score += (metrics.passRate / 100) * 40;

    // Coverage (30% weight)
    const avgCoverage =
      ((metrics.coverage.lines || 0) +
        (metrics.coverage.functions || 0) +
        (metrics.coverage.branches || 0) +
        (metrics.coverage.statements || 0)) /
      4;
    score += (avgCoverage / 100) * 30;

    // Performance (20% weight)
    const performanceScore = Math.max(0, 100 - metrics.executionTime / 1000);
    score += (performanceScore / 100) * 20;

    // Test count (10% weight)
    const testCountScore = Math.min(100, metrics.totalTests * 2);
    score += (testCountScore / 100) * 10;

    return Math.round(score);
  }

  private generateRecommendations(metrics: any, _insights: string[]): string[] {
    const recommendations = [];
    const quality = this.assessQuality(metrics);

    switch (quality) {
      case 'excellent':
        recommendations.push('Maintain current test quality and continue best practices');
        break;
      case 'good':
        recommendations.push('Focus on improving coverage and reducing test execution time');
        break;
      case 'fair':
        recommendations.push('Address failing tests and improve coverage significantly');
        break;
      case 'poor':
        recommendations.push('Comprehensive test refactoring needed - focus on fundamentals');
        break;
    }

    return recommendations;
  }
}

// ============================================================================
// Test Report Engine
// ============================================================================

class TestReportEngine {
  async generateReport(request: TestReportRequest): Promise<string> {
    const outputPath = request.outputPath || this.getDefaultOutputPath(request.format);

    switch (request.format) {
      case 'json':
        return await this.generateJSONReport(request.analysis, outputPath);
      case 'html':
        return await this.generateHTMLReport(
          request.analysis,
          outputPath,
          request.includeVisualizations,
        );
      case 'markdown':
        return await this.generateMarkdownReport(request.analysis, outputPath);
      case 'console':
        return await this.displayConsoleReport(request.analysis);
      default:
        throw new ValidationError(`Unsupported report format: ${request.format}`);
    }
  }

  private getDefaultOutputPath(format: ReportFormat): string {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    return path.join(process.cwd(), `test-report-${timestamp}.${format}`);
  }

  private async generateJSONReport(analysis: any, outputPath: string): Promise<string> {
    const report = {
      timestamp: new Date().toISOString(),
      summary: {
        totalTests: analysis.metrics.totalTests,
        passRate: analysis.metrics.passRate,
        quality: analysis.quality,
        coverage: analysis.metrics.coverage,
      },
      metrics: analysis.metrics,
      insights: analysis.insights,
      suggestions: analysis.suggestions,
      recommendations: analysis.recommendations,
    };

    await fs.ensureDir(path.dirname(outputPath));
    await fs.writeJson(outputPath, report, { spaces: 2 });

    return outputPath;
  }

  private async generateHTMLReport(
    analysis: any,
    outputPath: string,
    includeVisualizations: boolean,
  ): Promise<string> {
    const html = `
<!DOCTYPE html>
<html>
<head>
  <title>Test Report</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; }
    .header { background: #f5f5f5; padding: 20px; border-radius: 5px; }
    .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin: 20px 0; }
    .metric { background: #f9f9f9; padding: 15px; border-radius: 5px; text-align: center; }
    .insights { margin: 20px 0; }
    .suggestions { margin: 20px 0; }
    .quality-${analysis.quality} { color: ${analysis.quality === 'excellent' ? 'green' : analysis.quality === 'good' ? 'blue' : analysis.quality === 'fair' ? 'orange' : 'red'}; }
  </style>
</head>
<body>
  <div class="header">
    <h1>Test Report</h1>
    <p>Generated: ${new Date().toISOString()}</p>
    <p>Quality: <span class="quality-${analysis.quality}">${analysis.quality.toUpperCase()}</span></p>
  </div>

  <div class="metrics">
    <div class="metric">
      <h3>${analysis.metrics.totalTests}</h3>
      <p>Total Tests</p>
    </div>
    <div class="metric">
      <h3>${analysis.metrics.passRate.toFixed(1)}%</h3>
      <p>Pass Rate</p>
    </div>
    <div class="metric">
      <h3>${analysis.metrics.coverage.lines?.toFixed(1) || 'N/A'}%</h3>
      <p>Line Coverage</p>
    </div>
    <div class="metric">
      <h3>${analysis.metrics.executionTime}ms</h3>
      <p>Execution Time</p>
    </div>
  </div>

  ${includeVisualizations ? this.generateCharts(analysis) : ''}

  <div class="insights">
    <h2>AI Insights</h2>
    <ul>
      ${analysis.insights.map((insight: string) => `<li>${insight}</li>`).join('')}
    </ul>
  </div>

  <div class="suggestions">
    <h2>Suggestions</h2>
    <ul>
      ${analysis.suggestions.map((suggestion: string) => `<li>${suggestion}</li>`).join('')}
    </ul>
  </div>
</body>
</html>
    `.trim();

    await fs.ensureDir(path.dirname(outputPath));
    await fs.writeFile(outputPath, html);

    return outputPath;
  }

  private generateCharts(analysis: any): string {
    // Simple chart visualization (in real implementation, would use charting library)
    return `
    <div class="charts">
      <h2>Visualizations</h2>
      <div style="background: #f0f0f0; padding: 20px; margin: 10px 0;">
        <h3>Test Results Distribution</h3>
        <div style="display: flex; height: 30px;">
          <div style="background: green; width: ${analysis.metrics.passRate}%;"></div>
          <div style="background: red; width: ${analysis.metrics.failRate}%;"></div>
          <div style="background: yellow; width: ${analysis.metrics.skipRate}%;"></div>
        </div>
      </div>
    </div>
    `;
  }

  private async generateMarkdownReport(analysis: any, outputPath: string): Promise<string> {
    const markdown = `
# Test Report

**Generated:** ${new Date().toISOString()}
**Quality:** ${analysis.quality.toUpperCase()}
**Framework:** ${analysis.metrics.framework}

## Summary

| Metric | Value |
|--------|-------|
| Total Tests | ${analysis.metrics.totalTests} |
| Pass Rate | ${analysis.metrics.passRate.toFixed(1)}% |
| Failed Tests | ${analysis.metrics.failedTests} |
| Skipped Tests | ${analysis.metrics.skippedTests} |
| Execution Time | ${analysis.metrics.executionTime}ms |

## Coverage

| Metric | Percentage |
|--------|------------|
| Lines | ${analysis.metrics.coverage.lines?.toFixed(1) || 'N/A'}% |
| Functions | ${analysis.metrics.coverage.functions?.toFixed(1) || 'N/A'}% |
| Branches | ${analysis.metrics.coverage.branches?.toFixed(1) || 'N/A'}% |
| Statements | ${analysis.metrics.coverage.statements?.toFixed(1) || 'N/A'}% |

## AI Insights

${analysis.insights.map((insight: string) => `- ${insight}`).join('\n')}

## Suggestions

${analysis.suggestions.map((suggestion: string) => `- ${suggestion}`).join('\n')}

## Recommendations

${analysis.recommendations.map((rec: string) => `- ${rec}`).join('\n')}
    `.trim();

    await fs.ensureDir(path.dirname(outputPath));
    await fs.writeFile(outputPath, markdown);

    return outputPath;
  }

  private async displayConsoleReport(analysis: any): Promise<string> {
    console.log(chalk.blue.bold('\n📊 Test Analysis Report'));
    console.log(chalk.gray(`Generated: ${new Date().toISOString()}`));
    console.log(chalk.gray(`Quality: ${analysis.quality.toUpperCase()}`));

    console.log(chalk.blue('\n📈 Metrics:'));
    console.log(chalk.white(`   Total Tests: ${analysis.metrics.totalTests}`));
    console.log(chalk.white(`   Pass Rate: ${analysis.metrics.passRate.toFixed(1)}%`));
    console.log(chalk.white(`   Failed: ${analysis.metrics.failedTests}`));
    console.log(chalk.white(`   Skipped: ${analysis.metrics.skippedTests}`));
    console.log(chalk.white(`   Execution Time: ${analysis.metrics.executionTime}ms`));

    if (analysis.metrics.coverage.lines) {
      console.log(chalk.blue('\n📋 Coverage:'));
      console.log(chalk.white(`   Lines: ${analysis.metrics.coverage.lines.toFixed(1)}%`));
      console.log(chalk.white(`   Functions: ${analysis.metrics.coverage.functions.toFixed(1)}%`));
      console.log(chalk.white(`   Branches: ${analysis.metrics.coverage.branches.toFixed(1)}%`));
      console.log(
        chalk.white(`   Statements: ${analysis.metrics.coverage.statements.toFixed(1)}%`),
      );
    }

    if (analysis.insights.length > 0) {
      console.log(chalk.blue('\n🤖 AI Insights:'));
      analysis.insights.forEach((insight: string) => {
        console.log(chalk.white(`   • ${insight}`));
      });
    }

    if (analysis.suggestions.length > 0) {
      console.log(chalk.blue('\n💡 Suggestions:'));
      analysis.suggestions.forEach((suggestion: string) => {
        console.log(chalk.white(`   • ${suggestion}`));
      });
    }

    if (analysis.recommendations.length > 0) {
      console.log(chalk.blue('\n🎯 Recommendations:'));
      analysis.recommendations.forEach((rec: string) => {
        console.log(chalk.white(`   • ${rec}`));
      });
    }

    return 'console';
  }
}

// ============================================================================
// Main Test Command Handler
// ============================================================================

export async function testCommandHandler(
  args: string[],
  options: TestCommandOptions,
  _command: Command,
): Promise<void> {
  const verbose = options.verbose ?? 0;
  try {
    console.log(chalk.blue.bold('🧪 YYLO - AI-Powered Testing Framework'));

    cliLogger.info('Starting test command', { options, args });

    // Load configuration
    const workingDir = process.cwd();
    const loadConfigOptions: Parameters<typeof loadConfig>[0] = {
      baseDir: workingDir,
      cliConfig: {
        verbose: options.verbose ?? 0,
        quiet: options.quiet || false,
        logLevel: options.logLevel || 'info',
        workingDirectory: workingDir,
      },
    };
    if (options.config) {
      loadConfigOptions.configFile = options.config;
    }
    const config = await loadConfig(loadConfigOptions);
    cliLogger.info('Configuration loaded successfully');

    // Create session manager and progress display
    const sessionManager = await createSessionManager(config);
    const progressDisplay = new TestProgressDisplay(options.verbose);

    // Create test session
    const session = await sessionManager.createSession({
      name: `Test Session ${new Date().toISOString()}`,
      subagent: options.subagent || config.defaultSubagent,
      config: config,
      tags: ['test', 'testing', 'framework'],
      metadata: {
        testType: options.type,
        intelligence: options.intelligence,
        generate: options.generate,
        run: options.run,
        analyze: options.analyze,
        report: options.report,
      },
    });

    let generatedFiles: string[] = [];
    let testResults: TestExecutionResult | undefined;
    let analysis: any;

    try {
      // Phase 1: Generate Tests (if requested)
      if (options.generate) {
        engineLogger.info('Starting test generation', {
          subagent: options.subagent || config.defaultSubagent,
          intelligence: options.intelligence,
          target: args,
        });

        const generationEngine = new TestGenerationEngine(config, sessionManager, progressDisplay);
        const generateRequest: TestGenerationRequest = {
          target: args.length > 0 ? args : ['src'],
          type: options.type || 'all',
          intelligence: options.intelligence || 'comprehensive',
          framework: (options.framework as TestFramework) || 'vitest',
          subagent: options.subagent || config.defaultSubagent,
          workingDirectory: config.workingDirectory,
        };
        if (options.template) {
          generateRequest.template = options.template;
        }
        generatedFiles = await generationEngine.generateTests(generateRequest);

        await sessionManager.addHistoryEntry(session.info.id, {
          type: 'system',
          content: `Generated ${generatedFiles.length} test files`,
          data: { files: generatedFiles },
          iteration: 1,
        });
      }

      // Phase 2: Execute Tests (if requested or if generated tests)
      if (options.run || options.generate) {
        engineLogger.info('Starting test execution', {
          framework: options.framework,
          coverage: options.coverage,
          watch: options.watch,
        });

        const executionEngine = new TestExecutionEngine(progressDisplay);
        const execRequest: TestExecutionRequest = {
          framework: (options.framework as TestFramework) || 'vitest',
          workingDirectory: config.workingDirectory,
        };
        if (args.length > 0) {
          execRequest.target = args;
        } else if (generatedFiles.length > 0) {
          execRequest.target = generatedFiles;
        }
        if (options.coverage !== undefined) {
          execRequest.coverage = options.coverage;
        }
        if (options.watch !== undefined) {
          execRequest.watch = options.watch;
        }
        if (options.reporters !== undefined) {
          execRequest.reporters = options.reporters;
        }
        if (options.config !== undefined) {
          execRequest.config = options.config;
        }
        testResults = await executionEngine.executeTests(execRequest);

        const passRate = testResults.totalTests > 0
          ? ((testResults.passedTests / testResults.totalTests) * 100).toFixed(1)
          : '0.0';
        await sessionManager.addHistoryEntry(session.info.id, {
          type: 'system',
          content: `Executed ${testResults.totalTests} tests with ${passRate}% pass rate`,
          data: testResults,
          iteration: 2,
        });
      }

      // Phase 3: Analyze Results (if requested or if tests were executed)
      if ((options.analyze || testResults) && testResults) {
        engineLogger.info('Starting test analysis', {
          quality: options.quality,
          suggestions: options.suggestions,
        });

        const analysisEngine = new TestAnalysisEngine(config, sessionManager, progressDisplay);
        analysis = await analysisEngine.analyzeTests({
          results: testResults,
          coverage: testResults.coverage,
          quality: options.quality || 'thorough',
          suggestions: options.suggestions !== false,
          subagent: options.subagent || config.defaultSubagent,
        });

        await sessionManager.addHistoryEntry(session.info.id, {
          type: 'system',
          content: `Analyzed test quality: ${analysis.quality}`,
          data: analysis,
          iteration: 3,
        });
      }

      // Phase 4: Generate Report (if requested or if analysis was performed)
      if ((options.report || analysis) && analysis) {
        engineLogger.info('Generating test report', {
          format: options.format,
          outputPath: options.report,
        });

        const reportEngine = new TestReportEngine();
        const reportRequest: TestReportRequest = {
          analysis,
          format: (options.format as ReportFormat) || 'markdown',
          includeVisualizations: true,
        };
        if (typeof options.report === 'string') {
          reportRequest.outputPath = options.report;
        }
        const reportPath = await reportEngine.generateReport(reportRequest);

        await sessionManager.addHistoryEntry(session.info.id, {
          type: 'system',
          content: `Generated ${options.format} report: ${reportPath}`,
          data: { reportPath, format: options.format },
          iteration: 4,
        });

        if (options.format !== 'console') {
          console.log(chalk.green(`\n📄 Report generated: ${reportPath}`));
        }
      }

      // Complete session
      await sessionManager.completeSession(session.info.id, {
        success: true,
        output: `Test operations completed successfully`,
        finalState: {
          generatedFiles: generatedFiles.length,
          testResults,
          analysis,
          sessionId: session.info.id,
        },
      });

      // Complete command timing
      cliLogger.info('Test command completed successfully');

      // Set exit code based on test results
      const exitCode = testResults && !testResults.success ? 1 : 0;
      process.exit(exitCode);
    } catch (error) {
      // Complete session with error
      await sessionManager.completeSession(session.info.id, {
        success: false,
        error: error instanceof Error ? error.message : String(error),
      });
      throw error;
    }
  } catch (error) {
    if (error instanceof ValidationError) {
      console.error(chalk.red.bold('\n❌ Validation Error'));
      console.error(chalk.red(`   ${error.message}`));

      if (error.suggestions?.length) {
        console.error(chalk.yellow('\n💡 Suggestions:'));
        error.suggestions.forEach((suggestion: string) => {
          console.error(chalk.yellow(`   • ${suggestion}`));
        });
      }

      process.exit(1);
    }

    if (error instanceof ConfigurationError) {
      console.error(chalk.red.bold('\n❌ Configuration Error'));
      console.error(chalk.red(`   ${error.message}`));

      if (error.suggestions?.length) {
        console.error(chalk.yellow('\n💡 Suggestions:'));
        error.suggestions.forEach((suggestion: string) => {
          console.error(chalk.yellow(`   • ${suggestion}`));
        });
      }

      process.exit(2);
    }

    if (error instanceof RuntimeError) {
      console.error(chalk.red.bold('\n❌ File System Error'));
      console.error(chalk.red(`   ${error.message}`));

      if (error.suggestions?.length) {
        console.error(chalk.yellow('\n💡 Suggestions:'));
        error.suggestions.forEach((suggestion: string) => {
          console.error(chalk.yellow(`   • ${suggestion}`));
        });
      }

      process.exit(5);
    }

    // Unexpected error
    console.error(chalk.red.bold('\n❌ Unexpected Error'));
    console.error(chalk.red(`   ${error}`));

    if (verbose) {
      console.error('\n📍 Stack Trace:');
      console.error(error);
    }

    process.exit(99);
  }
}

/**
 * Configure the test command for Commander.js
 */
export function configureTestCommand(program: Command): void {
  program
    .command('test')
    .description('AI-powered testing framework for intelligent test generation and execution')
    .argument('[target...]', 'Test target (files, directories, or patterns)')
    .option('-t, --type <type>', 'Test type to generate/run', 'all')
    .option('-s, --subagent <name>', 'AI subagent for test generation', 'claude')
    .option('-i, --intelligence <level>', 'AI intelligence level', 'comprehensive')
    .option('-g, --generate', 'Generate tests using AI')
    .option('-r, --run', 'Execute tests')
    .option('--coverage [file]', 'Generate coverage report')
    .option('--analyze', 'Analyze test quality and coverage')
    .option('--quality <level>', 'Analysis quality level', 'thorough')
    .option('--suggestions', 'Generate improvement suggestions', true)
    .option('--report [file]', 'Generate test report')
    .option('--format <format>', 'Report format', 'markdown')
    .option('--template <name>', 'Test template to use')
    .option('--framework <name>', 'Testing framework', 'vitest')
    .option('--watch', 'Watch mode for continuous testing')
    .option('--reporters <items>', 'Test reporters (comma-separated)')
    // Advisory test-daemon options (ignored by the AI test framework path).
    .option('--json', 'Emit machine-readable daemon output')
    .option('--daemon', 'Use the warm advisory daemon for affected runs', true)
    .option('--no-daemon', 'Skip the warm daemon and run the cold path directly')
    .option('--changed-base <ref>', 'Base ref for affected-test selection (default HEAD)')
    .option('--daemon-force', 'Stop any stale daemon owner before starting')
    .option('--daemon-idle-timeout-ms <ms>', 'Daemon idle shutdown bound')
    .option('--daemon-max-requests <n>', 'Daemon request ceiling')
    .option('--daemon-timeout-ms <ms>', 'Per-run timeout for warm affected runs')
    .allowUnknownOption(true)
    .action(async (target, options, command) => {
      // Advisory test-daemon surface (Wave 2 of PDR 7djT8N): bounded
      // subcommands routed before the variadic target interpretation.
      if (target[0] === 'daemon') {
        await runTestDaemonCommand(target.slice(1), {
          json: options.json === true || Boolean(process.env.YYLO_TEST_DAEMON_JSON),
          force: options.daemonForce === true,
          idleTimeoutMs: options.daemonIdleTimeoutMs,
          maxRequests: options.daemonMaxRequests,
        });
        return;
      }
      if (target[0] === 'affected') {
        await runTestAffectedCommand(target.slice(1), {
          json: options.json === true || Boolean(process.env.YYLO_TEST_DAEMON_JSON),
          changedBase: options.changedBase,
          timeoutMs: options.daemonTimeoutMs,
          useDaemon: options.daemon !== false,
        });
        return;
      }
      const testOptions: TestCommandOptions = {
        type: options.type,
        subagent: options.subagent,
        intelligence: options.intelligence,
        generate: options.generate,
        run: options.run,
        coverage: options.coverage,
        analyze: options.analyze,
        quality: options.quality,
        suggestions: options.suggestions,
        report: options.report,
        format: options.format,
        template: options.template,
        framework: options.framework,
        watch: options.watch,
        reporters: options.reporters
          ? options.reporters.split(',').map((r: string) => r.trim())
          : undefined,
        // Global options
        verbose: options.verbose,
        quiet: options.quiet,
        config: options.config,
        logFile: options.logFile,
        logLevel: options.logLevel,
      };

      await testCommandHandler(target, testOptions, command);
    })
    .addHelpText(
      'after',
      `
Examples:
  $ yylo test --generate                          # Generate tests for current project
  $ yylo test --run                              # Run existing tests
  $ yylo test daemon start                       # Start the advisory warm test daemon
  $ yylo test daemon status                      # Bounded daemon status (<500ms)
  $ yylo test daemon stop                        # Graceful daemon stop
  $ yylo test affected                           # Warm affected-test edit loop (cold fallback)
  $ yylo test affected --no-daemon               # Force the cold path for this run
  $ yylo test --generate --run                   # Generate and run tests
  $ yylo test src/utils.ts --generate            # Generate tests for specific file
  $ yylo test --type unit --intelligence smart   # Generate smart unit tests
  $ yylo test --subagent cursor --generate       # Use Cursor for test generation
  $ yylo test --run --coverage                   # Run tests with coverage
  $ yylo test --analyze --quality thorough       # Analyze test quality thoroughly
  $ yylo test --report --format html             # Generate HTML report
  $ yylo test --framework jest --generate        # Generate Jest tests
  $ yylo test --template api-integration         # Use specific template
  $ yylo test --watch                            # Run tests in watch mode

Test Types:
  unit         Unit tests for individual functions/classes
  integration  Integration tests for component interactions
  e2e          End-to-end tests for complete user flows
  performance  Performance and load testing
  all          All test types (default)

Intelligence Levels:
  basic         Simple test generation with basic scenarios
  smart         Comprehensive generation with edge cases
  comprehensive Advanced generation with AI-powered insights

Frameworks:
  vitest        Modern Vitest framework (default)
  jest          Jest testing framework
  mocha         Mocha testing framework
  custom        Custom framework configuration

Report Formats:
  console       Display report in console (default)
  markdown      Generate Markdown report
  json          Generate JSON report
  html          Generate HTML report with visualizations

AI Subagent Specializations:
  claude        Best for: complex systems, integration tests, architecture validation
  cursor        Best for: unit tests, component tests, performance optimization
  codex         Best for: basic tests, quick generation, simple scenarios
  gemini        Best for: edge cases, security tests, unusual scenarios

Notes:
  - AI-powered test generation requires a configured subagent
  - Generated tests follow best practices for the selected framework
  - Coverage analysis supports multiple reporting formats
  - Quality analysis provides actionable insights for improvement
  - Reports can be generated in multiple formats for different audiences
  - Session tracking maintains complete test operation history
  - Performance metrics help optimize test execution time
    `,
    );
}
