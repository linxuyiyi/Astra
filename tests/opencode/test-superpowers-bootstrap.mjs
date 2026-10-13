import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import plugin, { SuperpowersPlugin } from '../../.opencode/plugins/superpowers.js';

const NEXUS_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const BOOTSTRAP_ENV_KEY = 'NEXUS_SUPERPOWERS_BOOTSTRAP';

// Run fn with the controlled bootstrap env var derived only from `env`, so a
// NEXUS_SUPERPOWERS_BOOTSTRAP inherited from the parent shell can never leak
// into an assertion. Always restore the exact prior environment afterwards.
const withBootstrapEnv = async (env, fn) => {
  const previous = { ...process.env };
  delete process.env[BOOTSTRAP_ENV_KEY];
  Object.assign(process.env, env);
  try {
    return await fn();
  } finally {
    for (const key of Object.keys(process.env)) delete process.env[key];
    Object.assign(process.env, previous);
  }
};

const hasBootstrapPart = (parts) =>
  parts.some((part) => part.type === 'text' && part.text.includes('EXTREMELY_IMPORTANT'));

// --- V1 (named export) -----------------------------------------------------

const makeV1Output = () => ({
  messages: [
    {
      info: { role: 'user', sessionID: 'ses_test' },
      parts: [{ type: 'text', text: '你好' }],
    },
  ],
});

const v1Injected = (directory, env = {}) =>
  withBootstrapEnv(env, async () => {
    const instance = await SuperpowersPlugin({ client: null, directory });
    const output = makeV1Output();
    await instance['experimental.chat.messages.transform']({}, output);
    return hasBootstrapPart(output.messages[0].parts);
  });

// --- V2 (default.setup) ----------------------------------------------------

const makeV2Context = (directory) => {
  const state = { contextHook: null, registeredSkills: [] };
  const ctx = {
    skill: {
      transform: async (callback) => {
        await callback({ add: (skill) => state.registeredSkills.push(skill) });
      },
    },
    session: {
      hook: async (name, callback) => {
        if (name === 'context') state.contextHook = callback;
      },
    },
  };
  if (directory !== undefined) ctx.directory = directory;
  return { ctx, state };
};

const v2Injected = (directory, env = {}) =>
  withBootstrapEnv(env, async () => {
    const { ctx, state } = makeV2Context(directory);
    await plugin.setup(ctx);
    assert.ok(state.contextHook, 'V2 setup must register the context hook');
    const event = {
      sessionID: 'ses_v2',
      messages: [{ role: 'user', content: [{ type: 'text', text: '你好' }] }],
    };
    await state.contextHook(event);
    return event.messages.some(
      (message) =>
        message.role === 'user' &&
        Array.isArray(message.content) &&
        hasBootstrapPart(message.content),
    );
  });

const v2RegisteredSkillCount = (directory) =>
  withBootstrapEnv({}, async () => {
    const { ctx, state } = makeV2Context(directory);
    await plugin.setup(ctx);
    return state.registeredSkills.length;
  });

// --- V1 assertions ---------------------------------------------------------

// Default: the bootstrap stays off so a normal task does not inherit the whole
// brainstorm → plan → subagent controller workflow. Skills remain registered
// and loadable through the native `skill` tool.
assert.equal(await v1Injected(NEXUS_ROOT), false, 'developer session must not get the bootstrap by default');
assert.equal(
  await v1Injected(NEXUS_ROOT, { [BOOTSTRAP_ENV_KEY]: '0' }),
  false,
  'an explicit non-1 value keeps the bootstrap off',
);

// Opt-in restores the upstream automatic skill-check workflow.
assert.equal(
  await v1Injected(NEXUS_ROOT, { [BOOTSTRAP_ENV_KEY]: '1' }),
  true,
  'NEXUS_SUPERPOWERS_BOOTSTRAP=1 should inject the bootstrap',
);

// Sessions outside the repository root never receive it, even when the switch is on.
assert.equal(
  await v1Injected(path.join(NEXUS_ROOT, 'backend'), { [BOOTSTRAP_ENV_KEY]: '1' }),
  false,
  'a non-root working directory must not receive the superpowers bootstrap',
);

// Isolation: a NEXUS_SUPERPOWERS_BOOTSTRAP inherited from the parent shell must
// not turn the default-off case on.
process.env[BOOTSTRAP_ENV_KEY] = '1';
try {
  assert.equal(
    await v1Injected(NEXUS_ROOT),
    false,
    'an inherited NEXUS_SUPERPOWERS_BOOTSTRAP must not leak into the default-off case',
  );
} finally {
  delete process.env[BOOTSTRAP_ENV_KEY];
}

// --- V2 assertions ---------------------------------------------------------

// V2 cannot confirm the working directory unless the host supplies
// ctx.directory, so injection is even stricter than V1: only a non-empty
// directory resolving to the repository root may inject.
assert.equal(await v2Injected(NEXUS_ROOT, {}), false, 'V2 developer session must not get the bootstrap by default');
assert.equal(
  await v2Injected(NEXUS_ROOT, { [BOOTSTRAP_ENV_KEY]: '1' }),
  true,
  'V2 with a repository-root directory injects when enabled',
);
assert.equal(
  await v2Injected(path.join(NEXUS_ROOT, 'backend'), { [BOOTSTRAP_ENV_KEY]: '1' }),
  false,
  'V2 with a non-root directory must not inject',
);
assert.equal(
  await v2Injected(undefined, { [BOOTSTRAP_ENV_KEY]: '1' }),
  false,
  'V2 without a ctx.directory must conservatively not inject',
);

// Skills stay registered for every V2 context, independent of the bootstrap gate.
assert.ok(await v2RegisteredSkillCount(NEXUS_ROOT) > 0, 'V2 registers skills at the repository root');
assert.ok(
  await v2RegisteredSkillCount(path.join(NEXUS_ROOT, 'backend')) > 0,
  'V2 registers skills outside the repository root',
);
assert.ok(await v2RegisteredSkillCount(undefined) > 0, 'V2 registers skills without a ctx.directory');

console.log('ok: superpowers bootstrap is opt-in, isolated from inherited env, and limited to the repository root');
