/**
 * Main command implementation for yylo CLI
 *
 * Comprehensive main execution command with full specification compliance.
 * Handles direct subagent execution with support for:
 * - File and inline prompts
 * - Interactive input modes
 * - Environment variable integration
 * - Complete validation and error handling
 */

import * as path from 'node:path';
import * as childProcess from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'fs-extra';
import chalk from 'chalk';
import { Command } from 'commander';

import { loadConfig } from '../../core/config.js';
import {
  clearContinueScopeRunning,
  markContinueScopeRunning,
  resolveContinueScopeContext,
} from '../../core/continue-scope.js';
import { persistContinueScopeSnapshot } from '../../core/session-continuity-state.js';
import { getSessionMetadataDirectory, SessionContinuityStateError } from '../../core/session-continuity-state.js';
import { withSessionMetadataLock } from '../../core/session-metadata.js';
import {
  getConfiguredDefaultModelForSubagent,
  getDefaultModelForSubagent,
  isModelCompatibleWithSubagent,
} from '../../core/subagent-models.js';
import { createExecutionEngine, createExecutionRequest } from '../../core/engine.js';
import { getCurrentGitBranch } from '../../core/git.js';
import { logger, LogLevel } from '../utils/advanced-logger.js';
import { ConcurrentFeedbackCollector } from '../../utils/concurrent-feedback-collector.js';
import { hasSimpleWorkspaceHint, resolveController } from '../../utils/controller-resolver.js';
import { checkLedgerReadiness, LedgerDelegateError } from './ledger.js';
import { AgentStartupError, errorMessage, resolveAgentWorkspace } from '../../utils/agent-startup.js';
import { buildChildProcessEnvironment } from '../../core/child-process-environment.js';
import { writeTerminalProgress } from '../../utils/terminal-progress-writer.js';
import { checkpointControllerAfterFinalization } from '../../utils/controller-checkpoint.js';
import type { MainCommandOptions } from '../types.js';
import {
  areLifecycleHooksDisabled,
  ValidationError,
  ConfigurationError,
  RuntimeError,
} from '../types.js';
import type { SubagentType } from '../../types/index.js';
import type { ExecutionRequest, ExecutionResult } from '../../core/engine.js';
import { ExecutionStatus } from '../../core/engine.js';
import { buildJunoExecutionEnvelope, extractJunoExecutionResponse } from '../../core/execution-envelope.js';
import type { ProgressEvent } from '../../types/execution.js';
import {
  CONTINUE_SETTINGS_VERSION,
  prepareSessionBranchExecution,
  resolveSessionBranchNameForSummary,
  syncSessionBranchExecutionResult,
  type ContinueSettingsSnapshot,
} from '../session-branch-workflow.js';

/**
 * Normalize verbose option to numeric level.
 * Accepts number/boolean/string values because Commander can pass optional args as booleans/strings.
 */
function normalizeVerboseLevel(verbose: unknown, quiet: boolean | undefined): number {
  if (quiet) return 0;
  if (verbose === undefined || verbose === null) return 1;

  if (typeof verbose === 'number' && Number.isFinite(verbose)) {
    if (verbose <= 0) return 0;
    if (verbose >= 2) return 2;
    return 1;
  }

  if (typeof verbose === 'boolean') {
    return verbose ? 1 : 0;
  }

  const str = String(verbose).toLowerCase().trim();
  if (['false', 'no', '0'].includes(str)) return 0;
  if (['true', 'yes', '1'].includes(str)) return 1;

  const num = Number(str);
  if (!Number.isNaN(num)) {
    if (num <= 0) return 0;
    if (num >= 2) return 2;
    return 1;
  }

  return 1;
}

function resolveJunoCodeVersion(command: Command): string {
  let current: Command | null = command;
  while (current) {
    const configuredVersion = current.version();
    if (configuredVersion) return configuredVersion;
    current = current.parent;
  }
  throw new ConfigurationError('YYLO CLI version is not configured');
}

interface SessionHistoryEntry {
  id: string;
  status: string;
  initialMessage: string;
  initialMessageAt: string;
  lastMessageAt: string;
  completedAt: string;
  subagent: string;
  model: string;
  settings: Record<string, unknown>;
  totalCostUsd: number;
  turnCount: number;
  messageCount: number;
  iterations: number;
  durationMs: number;
  sessionIds: string[];
}

interface SessionHistoryDocument {
  version: number;
  sessions: SessionHistoryEntry[];
}

const SESSION_HISTORY_VERSION = 1;
const SESSION_HISTORY_FILE_NAME = 'session_history.json';

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function toNumber(value: unknown): number | null {
  if (isFiniteNumber(value)) return value;
  if (typeof value === 'string') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function parseIsoDate(value: unknown): Date | null {
  if (value instanceof Date && Number.isFinite(value.getTime())) {
    return value;
  }

  if (typeof value === 'string' || typeof value === 'number') {
    const parsed = new Date(value);
    if (Number.isFinite(parsed.getTime())) {
      return parsed;
    }
  }

  return null;
}

function toIsoString(value: Date): string {
  return value.toISOString();
}

function parseJsonObject(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'string') return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    if (parsed && typeof parsed === 'object') {
      return parsed as Record<string, unknown>;
    }
  } catch {
    return null;
  }
  return null;
}

const LEADING_PROMPT_SHORTCUT_REGEX = /^%(?:\{([^\s{}]+)\}|([^\s%][^\s]*))(.*)$/s;
const LEADING_PROMPT_DELIMITER_MARKERS = new Set(['---', '***', '___']);
const LEADING_DIRECTIVE_LINE_REGEX = /^(?:%(?:\{[^\s{}]+\}|[^\s%][^\s]*)|\/skill:[^\s]+|\/[^\s]+|\$[^\s]+)/;
const KANBAN_TASK_REFERENCE_REGEX = /(?<!#)##\s*\{?([A-Za-z0-9]{6})\}?(?![A-Za-z0-9])/g;
const KANBAN_TASK_SCRIPT_RELATIVE_PATH = path.join('.juno_task', 'scripts', 'kanban.sh');
const KANBAN_HYDRATION_TOTAL_TIMEOUT_MS = 30000;
const KANBAN_GET_ATTEMPT_TIMEOUT_MS = 10000;
const KANBAN_GET_MAX_ATTEMPTS = 3;
const KANBAN_GET_RETRY_BASE_DELAY_MS = 100;

type KanbanTaskRecord = Record<string, unknown> & { id?: string };
type KanbanLookupFailureKind = 'timeout' | 'not_found' | 'error';

interface KanbanLookupFailure {
  kind: KanbanLookupFailureKind;
  detail: string;
}

interface KanbanLookupResult {
  tasks: KanbanTaskRecord[];
  failure?: KanbanLookupFailure;
}

interface KanbanHydrationResult {
  tasksById: Map<string, KanbanTaskRecord>;
  failuresById: Map<string, KanbanLookupFailure>;
  manualCommand: string;
}

function extractReferencedKanbanTaskIds(prompt: string): string[] {
  const taskIds: string[] = [];
  const seen = new Set<string>();

  for (const match of prompt.matchAll(KANBAN_TASK_REFERENCE_REGEX)) {
    const taskId = match[1];
    if (!taskId || seen.has(taskId)) {
      continue;
    }
    seen.add(taskId);
    taskIds.push(taskId);
  }

  return taskIds;
}

function normalizeKanbanTaskArray(payload: unknown): KanbanTaskRecord[] {
  if (Array.isArray(payload)) {
    return payload.filter((entry): entry is KanbanTaskRecord => Boolean(entry) && typeof entry === 'object');
  }

  if (payload && typeof payload === 'object') {
    return [payload as KanbanTaskRecord];
  }

  return [];
}

function isKanbanLookupTimeout(error: unknown): boolean {
  if (!error || typeof error !== 'object') {
    return false;
  }
  const candidate = error as { killed?: unknown; signal?: unknown; code?: unknown; message?: unknown };
  return (
    candidate.killed === true ||
    candidate.signal === 'SIGTERM' ||
    candidate.code === 'ETIMEDOUT' ||
    (typeof candidate.message === 'string' && /timed?\s*out/i.test(candidate.message))
  );
}

function lookupFailureDetail(error: unknown, stderr: string): string {
  const stderrDetail = stderr.trim();
  if (stderrDetail) {
    return stderrDetail;
  }
  return error instanceof Error && error.message.trim() ? error.message.trim() : 'unknown Kanban lookup error';
}

function retryDelay(attempt: number): Promise<void> {
  const jitterMs = Math.floor(Math.random() * KANBAN_GET_RETRY_BASE_DELAY_MS);
  return new Promise((resolve) => setTimeout(resolve, KANBAN_GET_RETRY_BASE_DELAY_MS * attempt + jitterMs));
}

async function runKanbanGetCommand(
  command: string,
  args: string[],
  workingDirectory: string,
  deadlineMs: number,
): Promise<KanbanLookupResult> {
  const controller = resolveController(workingDirectory, 'kanban');
  if (controller.role === 'simple') {
    command = await checkLedgerReadiness({ cwd: workingDirectory });
    args = ['--config', path.join(controller.path, '.juno_task/config.json'), ...args];
  }
  const execFile = promisify(childProcess.execFile);

  for (let attempt = 1; attempt <= KANBAN_GET_MAX_ATTEMPTS; attempt += 1) {
    const remainingMs = deadlineMs - Date.now();
    if (remainingMs <= 0) {
      return { tasks: [], failure: { kind: 'timeout', detail: 'total hydration deadline exhausted' } };
    }

    try {
      const result = await execFile(command, args, {
        cwd: workingDirectory,
        env: buildChildProcessEnvironment(process.env, {
          JUNO_TASK_ROOT: controller.path,
          JUNO_CONTROLLER_SOURCE: controller.source,
          JUNO_WORKSPACE_ROLE: controller.role,
        }),
        maxBuffer: 1024 * 1024,
        timeout: Math.min(KANBAN_GET_ATTEMPT_TIMEOUT_MS, remainingMs),
      });

      const stdout =
        typeof result === 'string' || Buffer.isBuffer(result)
          ? String(result)
          : String((result as { stdout?: unknown }).stdout ?? '');

      try {
        const parsed = JSON.parse(stdout) as unknown;
        return { tasks: normalizeKanbanTaskArray(parsed) };
      } catch (error) {
        return { tasks: [], failure: { kind: 'error', detail: `invalid Kanban JSON: ${lookupFailureDetail(error, '')}` } };
      }
    } catch (error) {
      const stderr = String((error as { stderr?: unknown } | null)?.stderr ?? '');
      const detail = lookupFailureDetail(error, stderr);
      if (/Task(?:\(s\))? not found:/i.test(detail)) {
        return { tasks: [], failure: { kind: 'not_found', detail } };
      }
      if (!isKanbanLookupTimeout(error)) {
        return { tasks: [], failure: { kind: 'error', detail } };
      }
      if (attempt < KANBAN_GET_MAX_ATTEMPTS && deadlineMs - Date.now() > KANBAN_GET_RETRY_BASE_DELAY_MS) {
        await retryDelay(attempt);
        continue;
      }
      return { tasks: [], failure: { kind: 'timeout', detail } };
    }
  }

  return { tasks: [], failure: { kind: 'timeout', detail: 'Kanban lookup attempts exhausted' } };
}

async function fetchKanbanTasksForCommand(
  command: string,
  taskIds: string[],
  workingDirectory: string,
  deadlineMs: number,
): Promise<{ tasksById: Map<string, KanbanTaskRecord>; failuresById: Map<string, KanbanLookupFailure> }> {
  const tasksById = new Map<string, KanbanTaskRecord>();
  const failuresById = new Map<string, KanbanLookupFailure>();
  if (taskIds.length === 0) {
    return { tasksById, failuresById };
  }

  const requestedTaskIds = new Set(taskIds);
  const addFetchedTasks = (fetchedTasks: KanbanTaskRecord[]): void => {
    for (const task of fetchedTasks) {
      const taskId = typeof task.id === 'string' ? task.id : undefined;
      if (taskId && requestedTaskIds.has(taskId)) {
        tasksById.set(taskId, task);
        failuresById.delete(taskId);
      }
    }
  };

  const batchResult = await runKanbanGetCommand(command, ['get', ...taskIds], workingDirectory, deadlineMs);
  addFetchedTasks(batchResult.tasks);
  const unresolvedTaskIds = taskIds.filter((taskId) => !tasksById.has(taskId));

  // An exhausted batch timeout is a shared operational failure. Starting another
  // subprocess per ID would amplify the same process storm and exceed the one
  // hydration deadline, so preserve the failure truth for every unresolved ID.
  if (batchResult.failure?.kind === 'timeout') {
    for (const taskId of unresolvedTaskIds) {
      failuresById.set(taskId, batchResult.failure);
    }
    return { tasksById, failuresById };
  }

  for (const taskId of unresolvedTaskIds) {
    if (Date.now() >= deadlineMs) {
      failuresById.set(taskId, { kind: 'timeout', detail: 'total hydration deadline exhausted' });
      continue;
    }
    const result = await runKanbanGetCommand(command, ['get', taskId], workingDirectory, deadlineMs);
    addFetchedTasks(result.tasks);
    if (!tasksById.has(taskId) && result.failure) {
      failuresById.set(taskId, result.failure);
    }
  }

  return { tasksById, failuresById };
}

async function fetchReferencedKanbanTasks(
  taskIds: string[],
  workingDirectory: string,
): Promise<KanbanHydrationResult> {
  const tasksById = new Map<string, KanbanTaskRecord>();
  const failuresById = new Map<string, KanbanLookupFailure>();
  const kanbanScriptPath = path.join(workingDirectory, KANBAN_TASK_SCRIPT_RELATIVE_PATH);
  const hasKanbanScript = await fs.pathExists(kanbanScriptPath);
  const simple = hasSimpleWorkspaceHint(workingDirectory);
  const command = simple ? 'yylo-ledger' : hasKanbanScript ? kanbanScriptPath : 'juno-kanban';
  const manualCommand = simple ? 'yy ledger' : hasKanbanScript ? './.juno_task/scripts/kanban.sh' : 'juno-kanban';

  if (taskIds.length === 0) {
    return { tasksById, failuresById, manualCommand };
  }

  const deadlineMs = Date.now() + KANBAN_HYDRATION_TOTAL_TIMEOUT_MS;
  const fetched = await fetchKanbanTasksForCommand(command, taskIds, workingDirectory, deadlineMs);
  return { ...fetched, manualCommand };
}

export async function expandKanbanTaskReferencesInPrompt(
  prompt: string,
  workingDirectory: string,
): Promise<string> {
  const referencedTaskIds = extractReferencedKanbanTaskIds(prompt);
  if (referencedTaskIds.length === 0) {
    return prompt;
  }

  const { tasksById, failuresById, manualCommand } = await fetchReferencedKanbanTasks(
    referencedTaskIds,
    workingDirectory,
  );

  for (const [taskId, failure] of failuresById.entries()) {
    if (failure.kind === 'not_found') {
      continue;
    }
    const reason = failure.kind === 'timeout' ? 'timed out' : 'failed';
    console.error(
      chalk.yellow(
        `Warning: Kanban task hydration ${reason} for ${taskId}. ` +
          `Automatic substitution was skipped; the agent was instructed to fetch the task manually.`,
      ),
    );
  }

  return prompt.replace(KANBAN_TASK_REFERENCE_REGEX, (fullMatch, taskId: string) => {
    const task = tasksById.get(taskId);
    if (task) {
      return `\n[kanban_task:${taskId}]\n${JSON.stringify(task, null, 2)}\n[/kanban_task]`;
    }

    const failure = failuresById.get(taskId);
    if (!failure || failure.kind === 'not_found') {
      return fullMatch;
    }

    const failureLabel = failure.kind === 'timeout' ? 'timed out' : 'failed';
    return (
      `\n[kanban_task_hydration_warning:${taskId}]\n` +
      `Automatic Kanban task hydration ${failureLabel}. Before acting on this task, manually run ` +
      `\`${manualCommand} get ${taskId}\` from the project root and use its canonical task payload.\n` +
      `Unresolved task reference: ${fullMatch}\n` +
      `[/kanban_task_hydration_warning]\n`
    );
  });
}

export function normalizeLeadingPromptDirectiveArtifacts(prompt: string): string {
  const lines = prompt.split(/\r?\n/);
  if (lines.length === 0) {
    return prompt;
  }

  let index = 0;
  while (index < lines.length) {
    const candidateLine = lines[index];
    if (candidateLine === undefined || candidateLine.trim() !== '') {
      break;
    }
    index += 1;
  }

  const firstMeaningfulRawLine = lines[index];
  if (firstMeaningfulRawLine === undefined) {
    return prompt;
  }

  const firstMeaningfulLine = firstMeaningfulRawLine.trim();
  if (!LEADING_PROMPT_DELIMITER_MARKERS.has(firstMeaningfulLine)) {
    return prompt;
  }

  let directiveIndex = index + 1;
  while (directiveIndex < lines.length) {
    const candidateLine = lines[directiveIndex];
    if (candidateLine === undefined || candidateLine.trim() !== '') {
      break;
    }
    directiveIndex += 1;
  }

  const directiveRawLine = lines[directiveIndex];
  if (directiveRawLine === undefined) {
    return prompt;
  }

  const directiveCandidate = directiveRawLine.trimStart();
  if (!LEADING_DIRECTIVE_LINE_REGEX.test(directiveCandidate)) {
    return prompt;
  }

  return lines.slice(directiveIndex).join('\n');
}

export function rewriteLeadingPromptShortcut(prompt: string, subagent: SubagentType): string {
  const match = prompt.match(LEADING_PROMPT_SHORTCUT_REGEX);
  if (!match) {
    return prompt;
  }

  const shortcut = match[1] ?? match[2];
  if (!shortcut) {
    return prompt;
  }

  const remaining = match[3] ?? '';

  switch (subagent) {
    case 'claude':
      return `/${shortcut}${remaining}`;
    case 'pi':
      return `/skill:${shortcut}${remaining}`;
    case 'codex':
      return `$${shortcut}${remaining}`;
    default:
      return prompt;
  }
}

function buildContinueSettingsSnapshot(request: ExecutionRequest): ContinueSettingsSnapshot {
  const snapshot: ContinueSettingsSnapshot = {
    version: CONTINUE_SETTINGS_VERSION,
    subagent: request.subagent,
  };

  if (request.model) snapshot.model = request.model;
  if (isFiniteNumber(request.maxIterations)) snapshot.maxIterations = request.maxIterations;
  if (request.thinking) snapshot.thinking = request.thinking;
  if (request.live) snapshot.live = true;
  if (request.agents) snapshot.agents = request.agents;
  if (request.tools) snapshot.tools = [...request.tools];
  if (request.allowedTools) snapshot.allowedTools = [...request.allowedTools];
  if (request.appendAllowedTools) snapshot.appendAllowedTools = [...request.appendAllowedTools];
  if (request.disallowedTools) snapshot.disallowedTools = [...request.disallowedTools];

  return snapshot;
}

async function persistContinueContext(
  result: ExecutionResult,
  config: { workingDirectory: string; envFilePath?: string },
  verboseLevel: number,
  options: MainCommandOptions = {},
): Promise<void> {
  try {
    const isNamedCloneRun = typeof options.cloneBranchName === 'string' && options.cloneBranchName.trim().length > 0;
    if (isNamedCloneRun) {
      // Named clones intentionally do not switch the active branch. The cloned session is
      // persisted by syncSessionBranchExecutionResult under its branch name; writing it
      // here into the shell-scoped env snapshot would make `.env.yylo` disagree with the
      // still-active branch and cause the next `yy cc` to fail the mismatch guard.
      return;
    }

    const sessionIds = extractSessionIds(result);
    const latestSessionId = sessionIds[sessionIds.length - 1];
    if (!latestSessionId) {
      return;
    }

    const settings = buildContinueSettingsSnapshot(result.request);
    const serializedSettings = JSON.stringify(settings);
    const continueScope = resolveContinueScopeContext(process.env, process.ppid, config.workingDirectory);

    await persistContinueScopeSnapshot({
      workingDirectory: config.workingDirectory,
      context: continueScope,
      sessionId: latestSessionId,
      serializedSettings,
    });

    if (verboseLevel >= 2) {
      console.error(
        chalk.gray(
          `   Continue scope snapshot persisted (${continueScope.scopeSource} -> ${continueScope.scopeHash})`,
        ),
      );
    }
  } catch (error) {
    if (verboseLevel >= 2) {
      console.error(chalk.yellow(`Warning: Failed to persist continue context: ${error}`));
    }
  }
}

function extractCostFromPayload(payload: Record<string, unknown>): number | null {
  for (const key of ['total_cost_usd', 'totalCostUsd', 'totalCostUSD'] as const) {
    const directCost = toNumber(payload[key]);
    if (directCost !== null) {
      return directCost;
    }
  }

  const usage = payload.usage;
  if (usage && typeof usage === 'object') {
    const usageCost = (usage as Record<string, unknown>).cost;
    if (usageCost && typeof usageCost === 'object') {
      const fallback = toNumber((usageCost as Record<string, unknown>).total);
      if (fallback !== null) {
        return fallback;
      }
    }
  }

  return null;
}

function extractSessionId(payload: Record<string, unknown>): string | null {
  const direct = payload.session_id;
  if (typeof direct === 'string' && direct.trim()) {
    return direct.trim();
  }

  const camel = payload.sessionId;
  if (typeof camel === 'string' && camel.trim()) {
    return camel.trim();
  }

  const subAgentResponse = payload.sub_agent_response;
  if (subAgentResponse && typeof subAgentResponse === 'object') {
    const nested = (subAgentResponse as Record<string, unknown>).session_id;
    if (typeof nested === 'string' && nested.trim()) {
      return nested.trim();
    }
  }

  return null;
}

function extractMessagesArray(payload: Record<string, unknown>): unknown[] | null {
  if (Array.isArray(payload.messages)) {
    return payload.messages;
  }

  const subAgentResponse = payload.sub_agent_response;
  if (subAgentResponse && typeof subAgentResponse === 'object') {
    const nestedMessages = (subAgentResponse as Record<string, unknown>).messages;
    if (Array.isArray(nestedMessages)) {
      return nestedMessages;
    }
  }

  return null;
}

function buildHistorySettings(request: ExecutionRequest): Record<string, unknown> {
  const settings: Record<string, unknown> = {
    maxIterations: request.maxIterations,
  };

  if (request.resume) settings.resume = request.resume;
  if (request.continueConversation) settings.continueConversation = true;
  if (request.thinking) settings.thinking = request.thinking;
  if (request.live) settings.live = true;
  if (request.agents) settings.agents = request.agents;
  if (request.tools) settings.tools = request.tools;
  if (request.allowedTools) settings.allowedTools = request.allowedTools;
  if (request.appendAllowedTools) settings.appendAllowedTools = request.appendAllowedTools;
  if (request.disallowedTools) settings.disallowedTools = request.disallowedTools;

  return settings;
}

function extractSessionIds(result: ExecutionResult): string[] {
  const sessionIds: string[] = [];
  const seen = new Set<string>();

  const addSessionId = (candidate: unknown): void => {
    if (typeof candidate !== 'string') return;
    const value = candidate.trim();
    if (!value || seen.has(value)) return;
    seen.add(value);
    sessionIds.push(value);
  };

  const addFromPayload = (payload: Record<string, unknown> | null | undefined): void => {
    if (!payload) return;
    addSessionId(extractSessionId(payload));
  };

  const iterations = Array.isArray(result.iterations) ? result.iterations : [];
  for (const iteration of iterations) {
    addFromPayload(parseJsonObject(iteration.toolResult?.content));

    const metadata = iteration.toolResult?.metadata as Record<string, unknown> | undefined;
    if (metadata && typeof metadata === 'object') {
      addSessionId(metadata.sessionId);
      addSessionId(metadata.session_id);
      addFromPayload(metadata.subAgentResponse as Record<string, unknown> | undefined);
    }
  }

  const progressEvents = Array.isArray(result.progressEvents) ? result.progressEvents : [];
  for (const event of progressEvents) {
    addSessionId(event?.metadata?.sessionId);
    addSessionId(event?.metadata?.session_id);
    addFromPayload(event?.metadata?.subAgentResponse as Record<string, unknown> | undefined);
    addFromPayload(event?.metadata?.parsedEvent as Record<string, unknown> | undefined);
  }

  return sessionIds;
}

function extractTurnAndMessageCounts(result: ExecutionResult): {
  turnCount: number;
  messageCount: number;
} {
  const iterations = Array.isArray(result.iterations) ? result.iterations : [];

  const turnCountsBySession = new Map<string, number>();
  const messageCountsBySession = new Map<string, number>();

  for (const [index, iteration] of iterations.entries()) {
    const payload = parseJsonObject(iteration.toolResult?.content);
    if (!payload) continue;

    const sessionKey = extractSessionId(payload) || `iteration-${index + 1}`;

    const numTurns = toNumber(payload.num_turns ?? payload.numTurns);
    if (numTurns !== null && numTurns >= 0) {
      const previous = turnCountsBySession.get(sessionKey) ?? 0;
      turnCountsBySession.set(sessionKey, Math.max(previous, numTurns));
    }

    const messages = extractMessagesArray(payload);
    if (messages) {
      const previous = messageCountsBySession.get(sessionKey) ?? 0;
      messageCountsBySession.set(sessionKey, Math.max(previous, messages.length));
    }
  }

  let turnCount = [...turnCountsBySession.values()].reduce((sum, value) => sum + value, 0);
  let messageCount = [...messageCountsBySession.values()].reduce((sum, value) => sum + value, 0);

  if (turnCount === 0 && iterations.length > 0) {
    turnCount = iterations.length;
  }

  if (messageCount === 0) {
    messageCount = turnCount > 0 ? turnCount * 2 : 0;
  }

  return { turnCount, messageCount };
}

function extractTotalCost(result: ExecutionResult): number {
  const iterations = Array.isArray(result.iterations) ? result.iterations : [];
  let totalCost = 0;

  for (const iteration of iterations) {
    const payload = parseJsonObject(iteration.toolResult?.content);
    if (!payload) continue;
    const cost = extractCostFromPayload(payload);
    if (cost !== null) {
      totalCost += cost;
    }
  }

  return totalCost;
}

function extractLastMessageTime(result: ExecutionResult, fallback: Date): Date {
  let latest = fallback;
  const progressEvents = Array.isArray(result.progressEvents) ? result.progressEvents : [];

  for (const event of progressEvents) {
    const eventDate = parseIsoDate(event?.timestamp);
    if (eventDate && eventDate.getTime() > latest.getTime()) {
      latest = eventDate;
    }
  }

  const iterations = Array.isArray(result.iterations) ? result.iterations : [];
  for (const iteration of iterations) {
    const payload = parseJsonObject(iteration.toolResult?.content);
    if (!payload) continue;

    const payloadDate = parseIsoDate(payload.datetime);
    if (payloadDate && payloadDate.getTime() > latest.getTime()) {
      latest = payloadDate;
    }
  }

  return latest;
}

function buildSessionHistoryEntry(result: ExecutionResult): SessionHistoryEntry {
  const now = new Date();
  const startTime = parseIsoDate((result as { startTime?: unknown }).startTime) || now;
  const endTime = parseIsoDate((result as { endTime?: unknown }).endTime) || startTime;
  const durationMs =
    toNumber((result as { duration?: unknown }).duration) ??
    Math.max(0, endTime.getTime() - startTime.getTime());

  const { turnCount, messageCount } = extractTurnAndMessageCounts(result);
  const sessionIds = extractSessionIds(result);

  return {
    id: result.request.requestId,
    status: result.status,
    initialMessage: result.request.instruction,
    initialMessageAt: toIsoString(startTime),
    lastMessageAt: toIsoString(extractLastMessageTime(result, endTime)),
    completedAt: toIsoString(endTime),
    subagent: result.request.subagent,
    model: result.request.model || 'auto',
    settings: buildHistorySettings(result.request),
    totalCostUsd: extractTotalCost(result),
    turnCount,
    messageCount,
    iterations: Array.isArray(result.iterations) ? result.iterations.length : 0,
    durationMs,
    sessionIds,
  };
}

async function readSessionHistoryDocument(
  historyPath: string,
  verboseLevel: number,
): Promise<SessionHistoryDocument> {
  const emptyDocument: SessionHistoryDocument = {
    version: SESSION_HISTORY_VERSION,
    sessions: [],
  };

  if (!(await fs.pathExists(historyPath))) {
    return emptyDocument;
  }

  const raw = await fs.readFile(historyPath, 'utf-8');
  try {
    const existing = JSON.parse(raw) as unknown;
    if (existing && typeof existing === 'object' && Array.isArray((existing as Record<string, unknown>).sessions)) {
      return {
        version: toNumber((existing as Record<string, unknown>).version) ?? SESSION_HISTORY_VERSION,
        sessions: (existing as Record<string, unknown>).sessions as SessionHistoryEntry[],
      };
    }
  } catch (error) {
    const backupPath = `${historyPath}.invalid-${new Date().toISOString().replace(/[:.]/g, '-')}`;
    await fs.writeFile(backupPath, raw, 'utf-8');
    if (verboseLevel >= 1) {
      console.error(
        chalk.yellow(
          `Warning: Repaired unreadable session history; original saved to ${backupPath}: ${error}`,
        ),
      );
    }
    return emptyDocument;
  }

  const backupPath = `${historyPath}.invalid-${new Date().toISOString().replace(/[:.]/g, '-')}`;
  await fs.writeFile(backupPath, raw, 'utf-8');
  if (verboseLevel >= 1) {
    console.error(
      chalk.yellow(
        `Warning: Repaired invalid session history shape; original saved to ${backupPath}`,
      ),
    );
  }
  return emptyDocument;
}

async function persistSessionHistory(result: ExecutionResult, verboseLevel: number): Promise<void> {
  try {
    if (
      !result?.request ||
      typeof result.request.workingDirectory !== 'string' ||
      !result.request.workingDirectory.trim()
    ) {
      if (verboseLevel >= 2) {
        console.error(chalk.yellow('Warning: Skipping session history persistence: missing working directory'));
      }
      return;
    }

    const historyPath = path.join(
      getSessionMetadataDirectory(result.request.workingDirectory),
      SESSION_HISTORY_FILE_NAME,
    );

    const metadataDirectory = path.dirname(historyPath);
    await withSessionMetadataLock(metadataDirectory, SESSION_HISTORY_FILE_NAME, async () => {
      const document = await readSessionHistoryDocument(historyPath, verboseLevel);
      document.sessions.unshift(buildSessionHistoryEntry(result));
      await fs.writeJson(historyPath, document, { spaces: 2 });
    });
  } catch (error) {
    if (verboseLevel >= 1) {
      console.error(chalk.yellow(`Warning: Failed to persist session history: ${error}`));
    }
  }
}

/**
 * Prompt input processor for handling various input types
 */
class PromptProcessor {
  constructor(private options: MainCommandOptions) {}

  async processPrompt(): Promise<string> {
    const { prompt, promptFile, interactivePrompt } = this.options;

    // Handle --interactive-prompt (TUI editor)
    if (interactivePrompt) {
      return await this.launchTUIPromptEditor(typeof prompt === 'string' ? prompt : undefined);
    }

    // Handle --prompt-file / -f flag (explicit file-based prompt)
    if (promptFile) {
      return await this.loadPromptFromFile(promptFile);
    }

    // Normalize prompt: Commander sets prompt=true when -p is used without an argument
    // (e.g. `yylo -p << 'EOF'` where heredoc redirects stdin)
    const promptText = typeof prompt === 'string' ? prompt : undefined;

    if (!promptText) {
      // Auto-detect piped stdin (heredoc, pipe, redirect) — no flag needed
      if (this.hasRedirectedStdin()) {
        return await this.readPipedStdin();
      }

      // Continue+Pi+live with no explicit prompt should open Pi TUI directly,
      // preserving the existing session and letting the operator type in-app.
      if (this.shouldOpenLiveContinueSessionWithoutPrompt()) {
        return '';
      }

      if (this.options.interactive) {
        return await this.collectInteractivePrompt();
      } else {
        // Try default prompt file: .juno_task/prompt.md
        const defaultPromptPath = path.join(process.cwd(), '.juno_task', 'prompt.md');
        if (await fs.pathExists(defaultPromptPath)) {
          console.error(
            chalk.blue(`📄 Using default prompt: ${chalk.cyan('.juno_task/prompt.md')}`),
          );
          return await this.loadPromptFromFile(defaultPromptPath);
        } else {
          throw new ValidationError('Prompt is required for execution', [
            "Provide prompt text: yylo claude 'your prompt here'",
            'Use file input: yylo claude prompt.txt',
            "Pipe via stdin: echo 'prompt' | yylo claude",
            'Use heredoc: yylo claude -p << \'EOF\'\\nyour prompt\\nEOF',
            'Shell safety: use single quotes (or -f/stdin) when prompt contains backticks or $()',
            'Use interactive mode: yylo claude --interactive',
            'Create default prompt file: .juno_task/prompt.md',
          ]);
        }
      }
    }

    // Check if prompt is a file path
    if (await this.isFilePath(promptText)) {
      return await this.loadPromptFromFile(promptText);
    }

    // Direct prompt text
    return promptText.trim();
  }

  private hasRedirectedStdin(): boolean {
    if (process.stdin.isTTY !== true) {
      return true;
    }

    try {
      const descriptor = fs.fstatSync(0);
      return descriptor.isFIFO() || descriptor.isFile() || descriptor.isSocket();
    } catch {
      return false;
    }
  }

  private shouldOpenLiveContinueSessionWithoutPrompt(): boolean {
    return (
      this.options.continueFromLatest === true &&
      this.options.subagent === 'pi' &&
      this.options.live === true &&
      !this.options.promptFile &&
      this.options.prompt !== true &&
      !this.options.interactive &&
      !this.options.interactivePrompt
    );
  }

  private async isFilePath(prompt: string): Promise<boolean> {
    // Check if it looks like a file path and exists
    if (prompt.includes('\n') || prompt.length > 500) {
      return false; // Too long or multiline to be a file path
    }

    try {
      const resolvedPath = path.resolve(prompt);
      return await fs.pathExists(resolvedPath);
    } catch {
      return false;
    }
  }

  private async loadPromptFromFile(filePath: string): Promise<string> {
    try {
      const resolvedPath = path.resolve(filePath);
      const content = await fs.readFile(resolvedPath, 'utf-8');

      if (!content.trim()) {
        throw new RuntimeError('Prompt file is empty', resolvedPath);
      }

      console.error(
        chalk.blue(
          `📄 Loaded prompt from: ${chalk.cyan(path.relative(process.cwd(), resolvedPath))}`,
        ),
      );
      return content.trim();
    } catch (error) {
      if (error instanceof RuntimeError) {
        throw error;
      }

      throw new RuntimeError(`Failed to read prompt file: ${error}`, filePath);
    }
  }

  private async launchTUIPromptEditor(_initialValue?: string): Promise<string> {
    // TUI system has been removed; redirect to readline-based interactive prompt
    console.error(chalk.yellow('Using interactive prompt mode...'));
    return await this.collectInteractivePrompt();
  }

  private async readPipedStdin(): Promise<string> {
    return new Promise((resolve, reject) => {
      let input = '';

      process.stdin.setEncoding('utf8');
      process.stdin.resume();

      process.stdin.on('data', (chunk) => {
        input += chunk;
      });

      process.stdin.on('end', () => {
        const trimmed = input.trim();
        if (!trimmed) {
          reject(
            new ValidationError('Empty stdin input', [
              'Provide prompt text via stdin',
              "Example: echo 'your prompt' | yylo claude",
              "Example: yylo claude << 'EOF'\\nyour prompt\\nEOF",
            ]),
          );
        } else {
          console.error(
            chalk.blue(`📥 Read prompt from stdin (${trimmed.length} chars)`),
          );
          resolve(trimmed);
        }
      });

      process.stdin.on('error', (error) => {
        reject(new RuntimeError(`Failed to read stdin: ${error}`, 'stdin'));
      });
    });
  }

  private async collectInteractivePrompt(): Promise<string> {
    console.error(chalk.blue.bold('\n✏️  Interactive Prompt Mode\n'));
    console.error(chalk.yellow('Enter your prompt (press Ctrl+D when finished):'));
    console.error(
      chalk.gray('You can type multiple lines. End with Ctrl+D (Unix) or Ctrl+Z (Windows).\n'),
    );

    return new Promise((resolve, reject) => {
      let input = '';

      process.stdin.setEncoding('utf8');
      process.stdin.resume();

      process.stdin.on('data', (chunk) => {
        input += chunk;
      });

      process.stdin.on('end', () => {
        const trimmed = input.trim();
        if (!trimmed) {
          reject(
            new ValidationError('Empty prompt provided', [
              'Provide meaningful prompt text',
              'Use --help for usage examples',
            ]),
          );
        } else {
          resolve(trimmed);
        }
      });

      process.stdin.on('error', (error) => {
        reject(new RuntimeError(`Failed to read interactive input: ${error}`, 'stdin'));
      });
    });
  }
}

/**
 * Module-level session ID tracker — updated during execution, read by SIGINT handler in cli.ts
 */
let _activeSessionId: string | null = null;

/**
 * Get the most recently known sub-agent session ID (for use by signal handlers).
 */
export function getActiveSessionId(): string | null {
  return _activeSessionId;
}

/**
 * Execution progress display for main command
 */
class MainProgressDisplay {
  private startTime: Date = new Date();
  private currentIteration: number = 0;
  private verboseLevel: number;
  private hasStreamedJsonOutput: boolean = false; // Track if we streamed JSON output via progress events
  private sessionIds: Map<number, string> = new Map(); // iteration# → sub-agent session_id
  private latestSessionId: string | null = null; // most recent session_id seen
  private lastResolvedInstructionByIteration: Map<number, string> = new Map();

  constructor(verboseLevel: number = 1, private readonly suppressResult = false) {
    this.verboseLevel = verboseLevel;
  }

  start(request: ExecutionRequest, gitBranch: string | null = null): void {
    this.startTime = new Date();

    // Level 0 (quiet): suppress all start info
    if (this.verboseLevel === 0) return;

    console.error(
      chalk.blue.bold(
        '\n🚀 Executing with ' +
          request.subagent.charAt(0).toUpperCase() +
          request.subagent.slice(1),
      ),
    );

    // Level 1+: always show model, max iterations (helping texts)
    if (request.model) {
      console.error(chalk.gray(`   Model: ${request.model}`));
    }
    console.error(
      chalk.gray(
        `   Max Iterations: ${request.maxIterations === -1 ? 'unlimited' : request.maxIterations}`,
      ),
    );
    if (gitBranch) {
      console.error(chalk.gray(`   Git Branch: ${gitBranch}`));
    }

    for (const [label, value] of this.getSelectedExecutionOptions(request)) {
      console.error(chalk.gray(`   ${label}: ${value}`));
    }

    // Level 2 only: Request ID, Working Directory (debug info)
    if (this.verboseLevel >= 2) {
      console.error(chalk.gray(`   Request ID: ${request.requestId}`));
      console.error(chalk.gray(`   Working Directory: ${request.workingDirectory}`));
    }

    const hasPromptSubstitutionSyntax = this.hasPromptCommandSubstitutionSyntax(request.instruction);
    const hasPromptMacroSyntax = this.hasPromptMacroSyntax(request.instruction);

    if (hasPromptSubstitutionSyntax || hasPromptMacroSyntax) {
      console.error(chalk.blue('\n📋 Task Template:'));
    } else {
      console.error(chalk.blue('\n📋 Task:'));
    }

    console.error(chalk.white(`   ${this.buildInstructionPreview(request.instruction)}`));

    if (hasPromptSubstitutionSyntax) {
      console.error(
        chalk.gray('   Prompt-time substitutions are resolved immediately before each subagent call.'),
      );
    }
    if (hasPromptMacroSyntax) {
      console.error(chalk.gray('   Prompt macros (@@key) are translated before the agent sees your prompt.'));
    }

    console.error('');
  }

  private getSelectedExecutionOptions(request: ExecutionRequest): Array<[string, string]> {
    const selected: Array<[string, string]> = [];

    if (request.thinking) {
      selected.push(['Thinking', request.thinking]);
    }
    if (request.live) {
      selected.push(['Live Mode', 'enabled']);
    }
    if (request.resume) {
      selected.push(['Resume Session', request.resume]);
    }
    if (request.continueConversation) {
      selected.push(['Continue Conversation', 'latest']);
    }
    if (request.agents) {
      selected.push(['Agents', this.truncateSummaryValue(request.agents)]);
    }

    const pushListOption = (label: string, values: readonly string[] | undefined): void => {
      if (!values || values.length === 0) return;
      selected.push([label, this.truncateSummaryValue(values.join(', '))]);
    };

    pushListOption('Tools', request.tools);
    pushListOption('Allowed Tools', request.allowedTools);
    pushListOption('Append Allowed Tools', request.appendAllowedTools);
    pushListOption('Disallowed Tools', request.disallowedTools);

    return selected;
  }

  private truncateSummaryValue(value: string, maxLength: number = 140): string {
    if (value.length <= maxLength) return value;
    return `${value.substring(0, maxLength - 3)}...`;
  }

  private buildInstructionPreview(instruction: string, maxLength: number = 200): string {
    if (instruction.length <= maxLength) {
      return instruction;
    }
    return `${instruction.substring(0, maxLength)}...`;
  }

  private hasPromptCommandSubstitutionSyntax(instruction: string): boolean {
    return instruction.includes("!'") || instruction.includes('!```');
  }

  private hasPromptMacroSyntax(instruction: string): boolean {
    return /(^|\s)@@[A-Za-z0-9_.:-]+(?=$|\s)/.test(instruction) || /\\@@[A-Za-z0-9_.:-]+/.test(instruction);
  }

  onInstructionResolved(
    iteration: number,
    resolvedInstruction: string,
    templateInstruction?: string,
    warnings?: Array<{ message?: string }>,
  ): void {
    const hasWarnings = Boolean(warnings && warnings.some((warning) => Boolean(warning?.message)));
    const previousInstruction = this.lastResolvedInstructionByIteration.get(iteration);
    if (previousInstruction === resolvedInstruction && !hasWarnings) {
      return;
    }

    this.lastResolvedInstructionByIteration.set(iteration, resolvedInstruction);

    if (this.verboseLevel === 0) {
      return;
    }

    const shouldPrintResolvedInstruction = !(
      templateInstruction !== undefined && resolvedInstruction === templateInstruction
    );

    if (shouldPrintResolvedInstruction) {
      const heading =
        iteration === 1
          ? '\n🧩 Resolved Task (iteration 1):'
          : `\n🧩 Resolved Task (iteration ${iteration}):`;

      console.error(chalk.blue(heading));
      console.error(chalk.white(`   ${this.buildInstructionPreview(resolvedInstruction)}`));
    }

    if (hasWarnings) {
      const warningHeader =
        iteration === 1
          ? '\n⚠ Prompt macro warnings (iteration 1):'
          : `\n⚠ Prompt macro warnings (iteration ${iteration}):`;
      console.error(chalk.yellow(warningHeader));
      for (const warning of warnings ?? []) {
        if (!warning?.message) continue;
        console.error(chalk.yellow(`   - ${warning.message}`));
      }
    }
  }

  onProgress(event: ProgressEvent): void {
    const timestamp = event.timestamp.toLocaleTimeString();

    // Capture sub-agent session_id from progress event metadata (claude/pi emit this)
    if (event.metadata?.sessionId && typeof event.metadata.sessionId === 'string') {
      this.latestSessionId = event.metadata.sessionId;
      if (this.currentIteration > 0) {
        this.sessionIds.set(this.currentIteration, event.metadata.sessionId);
      }
      // Update module-level tracker for SIGINT handler
      _activeSessionId = this.latestSessionId;
    }

    // If this is raw JSON output from shell backend (jq-style formatting)
    // OR if this is TEXT format streaming from shell backend (codex.py)
    // Mark that we're streaming output - this means we should NOT print the accumulated result later
    if (
      event.metadata?.rawJsonOutput ||
      (event.metadata?.format === 'text' && event.metadata?.raw === true)
    ) {
      this.hasStreamedJsonOutput = true;
    }

    // Level 0: suppress all streaming output (still track session IDs and hasStreamed)
    if (this.verboseLevel === 0) return;

    // If this is raw JSON output from shell backend (jq-style formatting)
    // Display it with colors and indentation like `claude.py | jq .`
    if (event.metadata?.rawJsonOutput) {
      try {
        // Parse and re-format with indentation
        const jsonObj = JSON.parse(event.content);
        const formattedJson = this.colorizeJson(jsonObj);
        const backend = event.backend ? chalk.cyan(`[${event.backend}]`) : '';

        if (this.verboseLevel >= 2) {
          // Level 2: Show pretty formatted JSON with timestamp and backend prefix on STDERR
          console.error(`${chalk.gray(timestamp)} ${backend} ${formattedJson}`);
        } else {
          // Level 1: Show JSON with backend prefix on STDERR
          console.error(`${backend} ${formattedJson}`);
        }
        return;
      } catch (error) {
        // If JSON parsing fails, fall back to raw output
        const backend = event.backend ? `[${event.backend}]` : '';
        console.error(`${chalk.gray(timestamp)} ${backend} ${event.content}`);
        return;
      }
    }

    // Try to parse content as JSON for jq-style formatting
    // This handles codex output which sends TEXT format but contains JSON
    try {
      const jsonObj = JSON.parse(event.content);
      const formattedJson = this.colorizeJson(jsonObj);
      // Show clean JSON output without prefixes (user wants clean output)
      console.error(formattedJson);
      return;
    } catch (error) {
      // Not JSON - show raw content without prefix (user wants clean output)
      console.error(event.content);
    }
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

  onIterationStart(iteration: number): void {
    this.currentIteration = iteration;
    if (this.verboseLevel === 0) return;
    const elapsed = this.getElapsedTime();
    console.error(chalk.yellow(`\n🔄 Iteration ${iteration} started (display elapsed: ${elapsed}; not provider runtime)`));
  }

  onIterationComplete(success: boolean, duration: number): void {
    if (this.verboseLevel === 0) return;
    const elapsed = this.getElapsedTime();
    const durationText = `${duration.toFixed(0)}ms`;
    if (success) {
      console.error(
        chalk.green(
          `✅ Iteration ${this.currentIteration} completed (${durationText}, total: ${elapsed})`,
        ),
      );
    } else {
      console.error(
        chalk.red(
          `❌ Iteration ${this.currentIteration} failed (${durationText}, total: ${elapsed})`,
        ),
      );
    }
  }

  private extractErrorMessage(candidate: unknown): string | null {
    if (!candidate) return null;

    if (candidate instanceof Error) {
      const message = candidate.message.trim();
      return message.length > 0 ? message : null;
    }

    if (typeof candidate === 'string') {
      const message = candidate.trim();
      return message.length > 0 ? message : null;
    }

    if (typeof candidate === 'object') {
      const maybeError = candidate as { message?: unknown; error?: unknown };

      if (typeof maybeError.message === 'string' && maybeError.message.trim().length > 0) {
        return maybeError.message.trim();
      }

      if (typeof maybeError.error === 'string' && maybeError.error.trim().length > 0) {
        return maybeError.error.trim();
      }
    }

    return null;
  }

  private getFailureReason(result: ExecutionResult): string | null {
    const lastIteration = result.iterations[result.iterations.length - 1];
    const candidates: unknown[] = [
      result.error,
      lastIteration?.error,
      lastIteration?.toolResult?.error,
    ];

    for (const candidate of candidates) {
      const message = this.extractErrorMessage(candidate);
      if (message) {
        return message;
      }
    }

    return null;
  }

  complete(
    result: ExecutionResult,
    branchName: string = 'main',
    gitBranch: string | null = null,
  ): void {
    const elapsed = this.getElapsedTime();
    const summaryBranchName = branchName.trim() || 'main';

    // Level 0 (quiet): only show final result on STDOUT, nothing else
    if (this.verboseLevel === 0) {
      if (this.suppressResult) return;
      const lastIteration = result.iterations[result.iterations.length - 1];
      // Quiet mode suppresses every progress event, so no streamed result has
      // actually reached stdout even when the backend emitted raw JSON events.
      if (lastIteration?.toolResult.content) {
        const displayContent = this.getDisplayResultContent(lastIteration.toolResult.content);
        if (displayContent.trim().length > 0) {
          console.log(displayContent);
        }
      }
      return;
    }

    // Send completion status to STDERR (progress messages)
    if (result.status === ExecutionStatus.COMPLETED) {
      console.error(chalk.green.bold(`\n✅ Execution completed successfully! (${elapsed})`));
    } else {
      console.error(chalk.red.bold(`\n❌ Execution failed (${elapsed})`));
      const failureReason = this.getFailureReason(result);
      if (failureReason) {
        console.error(chalk.red(`   Failure reason: ${failureReason}`));
      }
    }

    // Show final result heading on STDERR, actual result content on STDOUT
    // NOTE: If we streamed JSON output via progress events (hasStreamedJsonOutput=true),
    // skip printing the accumulated toolResult.content to avoid duplication
    const lastIteration = result.iterations[result.iterations.length - 1];
    const structuredOutput = (lastIteration?.toolResult.metadata as any)?.structuredOutput === true;
    const rawResultContent = lastIteration?.toolResult.content || '';
    const displayResultContent = rawResultContent
      ? this.getDisplayResultContent(rawResultContent)
      : '';
    const shouldPrintResult = Boolean(
      lastIteration &&
        displayResultContent &&
        (!this.hasStreamedJsonOutput || structuredOutput),
    );

    if (shouldPrintResult) {
      console.error(chalk.blue('\n📄 Result:'));
      // Final result goes to STDOUT for variable capture
      console.log(displayResultContent);
    }

    const iterationCosts = this.extractIterationCosts(result);
    const totalCostUsd = [...iterationCosts.values()].reduce((sum, cost) => sum + cost, 0);
    const completedAt = new Date();

    // Level 1+: show statistics (helping texts: iteration count, time, failures)
    if (this.verboseLevel >= 1) {
      const stats = result.statistics;
      console.error(chalk.blue('\n📊 Statistics:'));
      console.error(chalk.white(`   Total Iterations: ${stats.totalIterations}`));
      console.error(chalk.white(`   Successful: ${stats.successfulIterations}`));
      console.error(chalk.white(`   Failed: ${stats.failedIterations}`));
      console.error(
        chalk.white(
          `   Average Duration: ${this.formatAverageDuration(stats.averageIterationDuration)}`,
        ),
      );
      console.error(chalk.white(`   Tool Calls: ${stats.totalToolCalls}`));
      console.error(chalk.white(`   Branch: ${summaryBranchName}`));
      if (gitBranch) {
        console.error(chalk.white(`   Git Branch: ${gitBranch}`));
      }
      console.error(chalk.white(`   Completed At: ${this.formatHumanDateTime(completedAt)}`));

      if (iterationCosts.size > 0) {
        console.error(chalk.white(`   Total Cost: ${this.formatUsd(totalCostUsd)}`));
      }

      if (stats.rateLimitEncounters > 0) {
        console.error(chalk.yellow(`   Rate Limits: ${stats.rateLimitEncounters}`));
      }
    }

    // Show session IDs (always — useful for resuming sessions)
    this.extractSessionIdsFromResult(result);
    if (this.sessionIds.size > 0) {
      const sortedSessionEntries = [...this.sessionIds.entries()].sort((a, b) => a[0] - b[0]);
      console.error(chalk.blue('\n🔑 Session ID(s):'));
      if (sortedSessionEntries.length === 1) {
        const singleSessionEntry = sortedSessionEntries[0];
        if (singleSessionEntry) {
          const [iteration, sessionId] = singleSessionEntry;
          const cost = iterationCosts.get(iteration);
          if (cost !== undefined) {
            console.error(chalk.white(`   ${sessionId}    cost: ${this.formatUsd(cost)}`));
          } else {
            console.error(chalk.white(`   ${sessionId}`));
          }
        }
      } else {
        for (const [iteration, sessionId] of sortedSessionEntries) {
          const cost = iterationCosts.get(iteration);
          if (cost !== undefined) {
            console.error(
              chalk.white(`   Iteration ${iteration}: ${sessionId}    cost: ${this.formatUsd(cost)}`),
            );
          } else {
            console.error(chalk.white(`   Iteration ${iteration}: ${sessionId}`));
          }
        }
      }
    } else if (this.latestSessionId) {
      console.error(chalk.blue('\n🔑 Session ID:'));
      if (iterationCosts.size === 1) {
        const firstCost = iterationCosts.values().next().value;
        if (typeof firstCost === 'number') {
          console.error(
            chalk.white(`   ${this.latestSessionId}    cost: ${this.formatUsd(firstCost)}`),
          );
        } else {
          console.error(chalk.white(`   ${this.latestSessionId}`));
        }
      } else {
        console.error(chalk.white(`   ${this.latestSessionId}`));
      }
    } else {
      console.error(chalk.gray('\n🔑 Session ID: could not be extracted'));
    }
  }

  private getDisplayResultContent(content: string): string {
    if (this.verboseLevel >= 2) {
      return content;
    }

    try {
      const payload = JSON.parse(content) as Record<string, unknown>;
      if (payload?.type === 'result' && Object.prototype.hasOwnProperty.call(payload, 'result')) {
        const resultValue = payload.result;
        if (typeof resultValue === 'string') {
          return resultValue;
        }
        if (resultValue === null || resultValue === undefined) {
          return '';
        }
        return JSON.stringify(resultValue, null, 2);
      }
    } catch {
      // Non-JSON content should be shown as-is.
    }

    return content;
  }

  /**
   * Extract session IDs from iteration results' structured payloads
   */
  private extractSessionIdsFromResult(result: ExecutionResult): void {
    for (const [index, iteration] of result.iterations.entries()) {
      const iterationNumber =
        typeof iteration.iterationNumber === 'number' && Number.isFinite(iteration.iterationNumber)
          ? iteration.iterationNumber
          : index + 1;

      // Skip if we already have a session_id for this iteration (from progress events)
      if (this.sessionIds.has(iterationNumber)) continue;

      try {
        const payload = JSON.parse(iteration.toolResult.content);
        if (payload.session_id && typeof payload.session_id === 'string') {
          this.sessionIds.set(iterationNumber, payload.session_id);
          this.latestSessionId = payload.session_id;
          _activeSessionId = this.latestSessionId;
        }
      } catch {
        // Not JSON or no session_id — that's fine (e.g., codex)
      }
    }
  }

  private extractIterationCosts(result: ExecutionResult): Map<number, number> {
    const costs = new Map<number, number>();

    for (const [index, iteration] of result.iterations.entries()) {
      const iterationNumber =
        typeof iteration.iterationNumber === 'number' && Number.isFinite(iteration.iterationNumber)
          ? iteration.iterationNumber
          : index + 1;

      if (typeof iteration.toolResult?.content !== 'string' || !iteration.toolResult.content.trim()) {
        continue;
      }

      try {
        const payload = JSON.parse(iteration.toolResult.content) as Record<string, unknown>;
        const totalCostUsd = this.extractTotalCostUsd(payload);
        if (totalCostUsd !== null) {
          costs.set(iterationNumber, totalCostUsd);
        }
      } catch {
        // Non-JSON content has no structured usage/cost metadata
      }
    }

    return costs;
  }

  private extractTotalCostUsd(payload: Record<string, unknown>): number | null {
    for (const key of ['total_cost_usd', 'totalCostUsd', 'totalCostUSD'] as const) {
      const directValue = payload[key];
      if (typeof directValue === 'number' && Number.isFinite(directValue)) {
        return directValue;
      }

      if (typeof directValue === 'string') {
        const parsed = Number(directValue);
        if (Number.isFinite(parsed)) {
          return parsed;
        }
      }
    }

    const usage = payload.usage;
    if (usage && typeof usage === 'object') {
      const usageCost = (usage as Record<string, unknown>).cost;
      if (usageCost && typeof usageCost === 'object') {
        const total = (usageCost as Record<string, unknown>).total;
        if (typeof total === 'number' && Number.isFinite(total)) {
          return total;
        }

        if (typeof total === 'string') {
          const parsed = Number(total);
          if (Number.isFinite(parsed)) {
            return parsed;
          }
        }
      }
    }

    return null;
  }

  private formatAverageDuration(durationMs: number): string {
    if (!Number.isFinite(durationMs)) {
      return '0ms';
    }

    const safeDurationMs = Math.max(durationMs, 0);
    const units = [
      { label: 'h', divisor: 60 * 60 * 1000 },
      { label: 'm', divisor: 60 * 1000 },
      { label: 's', divisor: 1000 },
    ] as const;

    for (const unit of units) {
      if (safeDurationMs >= unit.divisor) {
        const value = safeDurationMs / unit.divisor;
        return `${this.formatDurationValue(value)}${unit.label}`;
      }
    }

    return `${Math.round(safeDurationMs)}ms`;
  }

  private formatDurationValue(value: number): string {
    return value.toFixed(2).replace(/\.?0+$/, '');
  }

  private formatHumanDateTime(date: Date): string {
    return new Intl.DateTimeFormat(undefined, {
      year: 'numeric',
      month: 'short',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: true,
      timeZoneName: 'short',
    }).format(date);
  }

  private formatUsd(amount: number): string {
    return `$${amount.toFixed(6)}`;
  }

  onError(error: Error): void {
    console.error(chalk.red(`\n❌ Execution error: ${error.message}`));
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
}

/**
 * Main command execution coordinator
 */
class MainExecutionCoordinator {
  private config: any;
  private progressDisplay: MainProgressDisplay;
  private feedbackCollector: ConcurrentFeedbackCollector | null = null;
  private enableFeedback: boolean = false;

  constructor(config: any, verboseLevel: number = 1, enableFeedback: boolean = false, suppressResult = false) {
    this.config = config;
    this.progressDisplay = new MainProgressDisplay(verboseLevel, suppressResult);
    this.enableFeedback = enableFeedback;

    // Initialize feedback collector if enabled
    if (this.enableFeedback) {
      this.feedbackCollector = new ConcurrentFeedbackCollector({
        command: 'yylo',
        commandArgs: ['feedback'],
        verbose: this.config.verbose,
        showHeader: true,
        progressInterval: 0, // Don't use built-in ticker, we have our own progress display
      });
    }
  }

  async execute(request: ExecutionRequest, branchName: string = 'main'): Promise<ExecutionResult> {
    // Log backend selection at level 2 (debug)
    if (this.config.verbose >= 2) {
      console.error(chalk.gray(`   Backend: Shell Scripts`));
    }

    // Create execution engine (backend is created internally)
    const engine = createExecutionEngine(this.config);

    // Set up progress callback
    engine.onProgress(async (event: any) => {
      // Route progress events to the progress display (always show progress)
      this.progressDisplay.onProgress(event);
    });

    // Set up event handlers
    engine.on('iteration:start', ({ iterationNumber }) => {
      this.progressDisplay.onIterationStart(iterationNumber);
    });

    engine.on('iteration:instruction-resolved', ({ iterationNumber, instruction, templateInstruction, warnings }) => {
      this.progressDisplay.onInstructionResolved(
        iterationNumber,
        typeof instruction === 'string' ? instruction : '',
        typeof templateInstruction === 'string' ? templateInstruction : undefined,
        Array.isArray(warnings) ? warnings : undefined,
      );
    });

    engine.on('iteration:complete', ({ iterationResult }) => {
      this.progressDisplay.onIterationComplete(iterationResult.success, iterationResult.duration);
    });

    engine.on('execution:error', ({ error }) => {
      if (!(error instanceof AgentStartupError)) this.progressDisplay.onError(error);
    });

    try {
      // Resolve once so the start and completion views describe the same execution context.
      const gitBranch = await getCurrentGitBranch(request.workingDirectory);

      // Start progress display
      this.progressDisplay.start(request, gitBranch);

      // Start feedback collector if enabled
      if (this.feedbackCollector) {
        writeTerminalProgress(
          chalk.gray('   Feedback collection: enabled (Type F+Enter to enter feedback mode)') +
            '\n',
        );
        this.feedbackCollector.start();
      }

      // Execute task
      const result = await engine.execute(request);

      // Complete progress display
      this.progressDisplay.complete(result, branchName, gitBranch);

      return result;
    } catch (error) {
      throw error;
    } finally {
      // Stop feedback collector if it was started
      if (this.feedbackCollector) {
        await this.feedbackCollector.stop();
      }

      // Cleanup
      try {
        await engine.shutdown();
      } catch (cleanupError) {
        console.warn(chalk.yellow(`Warning: Cleanup error: ${cleanupError}`));
      }
    }
  }
}

/**
 * Main command handler
 */
export async function mainCommandHandler(
  _args: string[],
  options: MainCommandOptions,
  _command: Command,
): Promise<void> {
  try {
    // Normalize verbose early; root CLI path can pass booleans/strings from Commander optional args.
    const effectiveVerbose = normalizeVerboseLevel(options.verbose, options.quiet);

    // Resolve --cwd to one absolute project route before configuration and
    // telemetry. Relative paths are relative to the invocation directory.
    const requestedWorkingDirectory = options.cwd
      ? path.resolve(process.cwd(), options.cwd)
      : process.cwd();

    if (options.cwd && hasSimpleWorkspaceHint(process.cwd())) {
      const origin = resolveController(process.cwd(), 'product-edit');
      const selected = resolveController(requestedWorkingDirectory, 'product-edit');
      if (selected.role !== 'simple' || selected.path !== origin.path) {
        throw new Error('Simple --cwd must remain inside the validated project root.');
      }
    }

    // Configuration ownership diagnostics precede dispatch/preflight; metadata sources are read-only.
    const config = await loadConfig({
      baseDir: requestedWorkingDirectory,
      ...(options.config !== undefined ? { configFile: options.config } : {}),
      cliConfig: {
        verbose: effectiveVerbose,
        quiet: options.quiet || false,
        logLevel: options.logLevel || 'info',
        ...(options.cwd || !hasSimpleWorkspaceHint(requestedWorkingDirectory)
          ? { workingDirectory: requestedWorkingDirectory } : {}),
        // Pass through onHourlyLimit if specified via CLI flag
        ...(options.onHourlyLimit
          ? { onHourlyLimit: options.onHourlyLimit as 'wait' | 'raise' }
          : {}),
      },
    });

    // Validate invocation authority before session setup or any controller-owned hook.
    resolveAgentWorkspace(requestedWorkingDirectory, undefined, 'diagnostic');
    if (config.controllerWorkspace?.mode === 'simple') {
      await checkLedgerReadiness({ cwd: config.workingDirectory });
    }
    await prepareSessionBranchExecution(options, config);

    // Set logger level based on effective verbose:
    //   0 (quiet): WARN — suppress INFO/DEBUG, only show warnings and errors
    //   1 (normal): INFO — show important INFO (e.g. quota limits), suppress DEBUG (hook execution details)
    //   2 (verbose): DEBUG — show everything including hook execution tracking
    if (effectiveVerbose >= 2) {
      logger.setLevel(LogLevel.DEBUG);
    } else if (effectiveVerbose === 0) {
      logger.setLevel(LogLevel.WARN);
    } else {
      logger.setLevel(LogLevel.INFO);
    }

    // Commander maps each negated spelling to its singular/plural option key.
    // Normalize both aliases here so every execution path has one skipHooks truth.
    if (areLifecycleHooksDisabled(options)) {
      config.skipHooks = true;
      if (effectiveVerbose >= 1) {
        console.error(chalk.gray('   Hooks: disabled (--no-hooks/--no-hook)'));
      }
    }

    // Resolve subagent: CLI flag > config.json > DEFAULT_CONFIG
    if (!options.subagent) {
      if (config.defaultSubagent) {
        options.subagent = config.defaultSubagent as SubagentType;
        if (effectiveVerbose >= 1) {
          console.error(chalk.gray(`   Subagent: ${config.defaultSubagent} (from config.json)`));
        }
      } else {
        options.subagent = 'claude' as SubagentType;
        if (effectiveVerbose >= 1) {
          console.error(chalk.gray(`   Subagent: claude (default)`));
        }
      }
    }

    // Validate subagent
    const validSubagents: SubagentType[] = ['claude', 'cursor', 'codex', 'gemini', 'pi'];
    if (!validSubagents.includes(options.subagent)) {
      throw new ValidationError(`Invalid subagent: ${options.subagent}`, [
        `Use one of: ${validSubagents.join(', ')}`,
        'Example: yylo claude "your prompt"',
        'Use --help for more information',
      ]);
    }

    // Validate --live usage (pi-only)
    if (options.live && options.subagent !== 'pi') {
      throw new ValidationError('--live is only supported with the pi subagent', [
        'Use: yylo pi --live -p "your prompt"',
        'Remove --live for non-pi subagents',
      ]);
    }

    if (options.cloneSession && options.subagent !== 'pi') {
      throw new ValidationError('Pi session cloning is only supported with the pi subagent', [
        'Use: yylo --resume <session-id> --clone "your prompt"',
        'Or pass --subagent pi when cloning from a continue scope',
      ]);
    }

    // Commander and project config are the authoritative request observations.
    // Attach only these allowlisted values; never infer them from raw argv or
    // prompt text. Immutable IDs, origin routing, and start timing stay fixed.
    const configuredModel = getConfiguredDefaultModelForSubagent(config, options.subagent);
    const requestedModel = options.model || configuredModel || null;
    const { startActiveInvocation } = await import('../../core/invocation-lifecycle.js');
    if (!await startActiveInvocation({
      service: options.subagent,
      requestedModel,
    })) return;

    // Process prompt
    const promptProcessor = new PromptProcessor(options);
    const rawInstruction = await promptProcessor.processPrompt();
    const normalizedInstruction = normalizeLeadingPromptDirectiveArtifacts(rawInstruction);
    const rewrittenInstruction = rewriteLeadingPromptShortcut(normalizedInstruction, options.subagent);
    const instruction = await expandKanbanTaskReferencesInPrompt(
      rewrittenInstruction,
      config.workingDirectory,
    );

    // Backend is always 'shell' (only backend type)
    const selectedBackend = 'shell' as const;

    // Check if --allowed-tools and --append-allowed-tools are used together (mutually exclusive)
    if (options.allowedTools && options.appendAllowedTools) {
      console.error(
        chalk.red(
          '\n❌ Error: --allowed-tools and --append-allowed-tools are mutually exclusive. Use one or the other.',
        ),
      );
      process.exit(1);
      return;
    }

    // Validate maxIterations - check for NaN (e.g., from parseInt('invalid'))
    // This must happen BEFORE the fallback logic, otherwise NaN || default = default (silent failure)
    if (options.maxIterations !== undefined && Number.isNaN(options.maxIterations)) {
      throw new ValidationError('Max iterations must be a valid number', [
        'Use -1 for unlimited iterations',
        'Use positive integers like 1, 5, or 10',
        'Example: -i 5',
      ]);
    }

    // Provider execution still resolves configured/built-in defaults normally.
    const resolvedModel = requestedModel || getDefaultModelForSubagent(options.subagent);

    const liveInteractiveSession =
      options.continueFromLatest === true &&
      options.subagent === 'pi' &&
      options.live === true &&
      instruction.length === 0 &&
      typeof options.resume === 'string' &&
      options.resume.trim().length > 0;

    // Create execution request
    // Pass both --tools and --allowed-tools as separate parameters
    // Use nullish coalescing (??) instead of || to properly handle 0 or NaN values
    const executionRequest = createExecutionRequest({
      instruction,
      subagent: options.subagent,
      backend: selectedBackend,
      workingDirectory: config.workingDirectory,
      maxIterations: options.maxIterations ?? config.defaultMaxIterations,
      model: resolvedModel,
      ...(options.executionEnvelope === true ? { sessionMetadata: {
        executionControllerDirectory: resolveController(config.workingDirectory, 'diagnostic', {
          ignoreEnvironmentAssertions: true,
          trustedResolver: true,
        }).path,
      } } : {}),
      ...(options.agents !== undefined ? { agents: options.agents } : {}),
      ...(options.tools !== undefined ? { tools: options.tools } : {}),
      ...(options.allowedTools !== undefined ? { allowedTools: options.allowedTools } : {}),
      ...(options.appendAllowedTools !== undefined ? { appendAllowedTools: options.appendAllowedTools } : {}),
      ...(options.disallowedTools !== undefined ? { disallowedTools: options.disallowedTools } : {}),
      ...(options.resume !== undefined ? { resume: options.resume } : {}),
      ...(options.cloneSession !== undefined ? { cloneSession: options.cloneSession } : {}),
      ...(options.cloneFromSession !== undefined ? { cloneFromSession: options.cloneFromSession } : {}),
      ...(options.continue !== undefined ? { continueConversation: options.continue } : {}),
      ...(options.thinking !== undefined ? { thinking: options.thinking } : {}),
      ...(options.live !== undefined ? { live: options.live } : {}),
      ...(liveInteractiveSession ? { liveInteractiveSession: true } : {}),
    });

    const executionBranchName = await resolveSessionBranchNameForSummary(options, config);

    // Execute
    const coordinator = new MainExecutionCoordinator(
      config,
      options.executionEnvelope === true ? 0 : effectiveVerbose,
      options.enableFeedback || false,
      options.executionEnvelope === true,
    );

    const continueScope = resolveContinueScopeContext(process.env, process.ppid, config.workingDirectory);
    let continueScopeMarkedRunning = false;
    let exitCode: number | null = null;
    let executionError: unknown;

    try {
      await markContinueScopeRunning(config.workingDirectory, continueScope);
      continueScopeMarkedRunning = true;

      const result = await coordinator.execute(executionRequest, executionBranchName);
      const { observeActiveInvocationProviderResult } = await import('../../core/invocation-lifecycle.js');
      observeActiveInvocationProviderResult(result);
      if (options.executionEnvelope === true) {
        process.stdout.write(`${JSON.stringify(buildJunoExecutionEnvelope(result, resolveJunoCodeVersion(_command)))}\n`);
        const evidenceFd = Number(process.env['YYLO_EXECUTION_EVIDENCE_FD']);
        if (Number.isInteger(evidenceFd) && evidenceFd >= 3) {
          fs.writeSync(evidenceFd, extractJunoExecutionResponse(result));
        }
      }

      await persistSessionHistory(result, effectiveVerbose);
      const sessionIds = extractSessionIds(result);
      await syncSessionBranchExecutionResult(result, config, options, sessionIds[sessionIds.length - 1]);
      await persistContinueContext(result, config, effectiveVerbose, options);

      // Set exit code based on result. Timeout remains non-zero while the
      // invocation lifecycle separately preserves its semantic classification.
      exitCode = result.status === ExecutionStatus.COMPLETED ? 0 : 1;
      if (result.status === ExecutionStatus.TIMEOUT) {
        const { markActiveInvocationTimeout } = await import('../../core/invocation-lifecycle.js');
        markActiveInvocationTimeout();
      }
    } catch (error) {
      executionError = error;
      exitCode = 1;
    } finally {
      if (continueScopeMarkedRunning) {
        try {
          await clearContinueScopeRunning(config.workingDirectory, continueScope);
        } catch (error) {
          if (effectiveVerbose >= 2) {
            console.error(chalk.yellow(`Warning: Failed to clear continue scope runtime marker: ${error}`));
          }
        }
      }
    }

    if (exitCode !== null) {
      // This is the true outer execution finalizer: history, session branches,
      // continuation state, and the running marker have already been persisted.
      if (config.controllerWorkspace?.mode !== 'simple') {
        await checkpointControllerAfterFinalization(config.workingDirectory, exitCode);
      }
      if (executionError !== undefined) throw executionError;
      process.exit(exitCode);
      return;
    }
  } catch (error) {
    if (error instanceof ValidationError) {
      console.error(chalk.red.bold('\n❌ Validation Error'));
      console.error(chalk.red(`   ${error.message}`));

      if (error.suggestions?.length) {
        console.error(chalk.yellow('\n💡 Suggestions:'));
        error.suggestions.forEach((suggestion) => {
          console.error(chalk.yellow(`   • ${suggestion}`));
        });
      }

      process.exit(1);
      return;
    } else if (error instanceof AgentStartupError) {
      console.error(error.message);
      // Finalize through the invocation boundary even if startup left handles open.
      process.exit(2);
      return;
    } else if (error instanceof LedgerDelegateError) {
      console.error(error.message);
      process.exitCode = error.exitCode;
      return;
    } else if (error instanceof ConfigurationError) {
      console.error(chalk.red.bold('\n❌ Configuration Error'));
      console.error(chalk.red(`   ${error.message}`));

      if (error.suggestions?.length) {
        console.error(chalk.yellow('\n💡 Suggestions:'));
        error.suggestions.forEach((suggestion) => {
          console.error(chalk.yellow(`   • ${suggestion}`));
        });
      }

      process.exit(2);
      return;
    } else if (error instanceof RuntimeError) {
      console.error(chalk.red.bold('\n❌ File System Error'));
      console.error(chalk.red(`   ${error.message}`));

      if (error.suggestions?.length) {
        console.error(chalk.yellow('\n💡 Suggestions:'));
        error.suggestions.forEach((suggestion) => {
          console.error(chalk.yellow(`   • ${suggestion}`));
        });
      }

      process.exit(5);
      return;
    } else if (error instanceof SessionContinuityStateError) {
      console.error(chalk.red.bold('\n❌ Branch Registry Error'));
      console.error(chalk.red(`   ${error.message}`));
      console.error(chalk.yellow('\n💡 Suggestions:'));
      console.error(chalk.yellow("   • Run ypl 'init' or yylo pi 'init' to create/refresh the main branch"));
      console.error(chalk.yellow('   • Inspect branches with: yylo branches'));
      process.exit(1);
      return;
    } else {
      // Unexpected error
      console.error(chalk.red.bold('\n❌ Unexpected Error'));
      console.error(chalk.red(`   ${errorMessage(error)}`));

      if (options.verbose) {
        console.error('\n📍 Stack Trace:');
        console.error(error);
      }

      process.exit(99);
      return;
    }
  }
}

// Export for testing
export { getDefaultModelForSubagent, isModelCompatibleWithSubagent };
