/**
 * Default hooks template configuration
 *
 * This module provides default hook commands that are shipped with yylo.
 * These hooks are automatically configured during project initialization (init command).
 *
 * Hook Types:
 * - START_RUN: Executes at the beginning of a run (before all iterations)
 * - START_ITERATION: Executes at the start of each iteration
 * - END_ITERATION: Executes at the end of each iteration
 * - END_RUN: Executes at the end of a run (after all iterations)
 *
 * To modify default hooks:
 * 1. Update the commands in this file
 * 2. Rebuild the project (npm run build)
 * 3. New installations will use the updated defaults
 *
 * @module templates/default-hooks
 */

import type { Hooks } from '../types/index';

/**
 * Default hooks configuration template
 *
 * All hook types are included so users can see available hooks without reading documentation.
 * Empty command arrays indicate hooks that are available but not configured by default.
 *
 * These hooks provide useful default behaviors:
 * - File size monitoring for CLAUDE.md, AGENTS.md, .juno_task/plan.md, .juno_task/tasks.md
 * - Alerts via juno-kanban when documentation/task files become too large
 *
 * Users can customize these hooks by editing .juno_task/config.json after initialization
 */
export const DEFAULT_HOOKS: Hooks = {
  // Executes once at the beginning of a run (before all iterations)
  // Use for: setup, environment checks, notifications, pre-run cleanup
  START_RUN: {
    // Workspace-owned readiness runs explicitly before dispatch. Installation
    // and package updates are owner-requested maintenance, never default hooks.
    commands: [],
  },

  // Executes at the start of each iteration
  // Use for: file monitoring, state checks, per-iteration setup
  START_ITERATION: {
    commands: [
      // Monitor CLAUDE.md file size
      'file="CLAUDE.md"; lines=$(wc -l < "$file" 2>/dev/null || echo 0); chars=$(wc -m < "$file" 2>/dev/null || echo 0); if [ "$lines" -gt 450 ] || [ "$chars" -gt 40000 ]; then juno-kanban create "[Critical] file $file is too large, keep it lean and useful for every run of the agent." --reject-duplicates; fi',

      // Monitor AGENTS.md file size
      'file="AGENTS.md"; lines=$(wc -l < "$file" 2>/dev/null || echo 0); chars=$(wc -m < "$file" 2>/dev/null || echo 0); if [ "$lines" -gt 450 ] || [ "$chars" -gt 40000 ]; then juno-kanban create "[Critical] file $file is too large, keep it lean and useful for every run of the agent." --reject-duplicates; fi',

      // Monitor .juno_task/plan.md file size
      'file=".juno_task/plan.md"; lines=$(wc -l < "$file" 2>/dev/null || echo 0); chars=$(wc -m < "$file" 2>/dev/null || echo 0); if [ "$lines" -gt 450 ] || [ "$chars" -gt 40000 ]; then juno-kanban create "[Critical] file $file is too large, keep it lean and useful for every run of the agent."  --reject-duplicates; fi',

      // Monitor .juno_task/tasks.md file size
      'file=".juno_task/tasks.md"; lines=$(wc -l < "$file" 2>/dev/null || echo 0); chars=$(wc -m < "$file" 2>/dev/null || echo 0); if [ "$lines" -gt 450 ] || [ "$chars" -gt 40000 ]; then juno-kanban create "[Critical] file $file is too large, keep it lean and useful for every run of the agent." --reject-duplicates; fi',
      './.juno_task/scripts/cleanup_feedback.sh',
    ],
  },

  // Executes at the end of each iteration
  // Use for: validation, logging, per-iteration cleanup, progress tracking
  END_ITERATION: {
    commands: [],
  },

  // Executes once at the end of a run (after all iterations complete)
  // Use for: final cleanup, notifications, reports, post-run actions
  END_RUN: {
    commands: [],
  },

  // Executes when stale iteration is detected in run_until_completion.sh
  // Use for: alerts, notifications, logging when agent is not making progress
  ON_STALE: {
    commands: [
      './.juno_task/scripts/kanban.sh create "Warning: You haven\'t done anything on the kanban in the past run. You need to process a task, or if you find it unsuitable or unresolvable, you need to archive the task" --status todo',
    ],
  },
};

/**
 * Get default hooks configuration
 *
 * Returns a copy of the default hooks to prevent mutation of the template.
 * This function can be used during initialization to populate the config.json file.
 *
 * @returns A copy of the default hooks configuration
 */
export function getDefaultHooks(): Hooks {
  return JSON.parse(JSON.stringify(DEFAULT_HOOKS));
}

/**
 * Get default hooks as formatted JSON string for config.json
 *
 * @param indent - Number of spaces for indentation (default: 2)
 * @returns Formatted JSON string of default hooks
 */
export function getDefaultHooksJson(indent: number = 2): string {
  return JSON.stringify(DEFAULT_HOOKS, null, indent);
}
