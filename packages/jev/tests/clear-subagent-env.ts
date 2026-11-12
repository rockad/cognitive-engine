/**
 * Import for its side effect: tests that run the guards must not see the developer's own subagent
 * model variables (CLAUDE_CODE_SUBAGENT_MODEL and friends change what an agent call runs on, so a
 * value left in the shell would change what these tests expect). Tests that exercise the variables
 * set them themselves.
 */
for (const v of ['CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL']) delete process.env[v];
