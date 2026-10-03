// Runs inside the agent container, after the image has selected its runtime UID.
// Configuration is private to this process; session directories remain persistent.
// The real agent argv arrives as JSON in VIBEPOD_PROVIDER_COMMAND: this script is
// launched as `node <mounted path>` only, so image entrypoints that re-parse argv
// through `sh -c "$*"` cannot mangle quotes, braces, or prompt text.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawn } = require('node:child_process');

let temporary;
let child;
// Messages safe to print: they name files or directories, never their contents.
class UserError extends Error {}
function cleanup() {
  if (temporary) fs.rmSync(temporary, { recursive: true, force: true });
}
function objectFile(root, name) {
  const filename = path.join(root, name);
  if (!fs.existsSync(filename)) return {};
  const value = JSON.parse(fs.readFileSync(filename, 'utf8'));
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid config');
  return value;
}
function privateJson(name, value) {
  const target = path.join(temporary, name);
  fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
  fs.writeFileSync(target, JSON.stringify(value, null, 2), { mode: 0o600 });
}
function resourcePath(root, value) {
  if (typeof value !== 'string') return value;
  const prefix = /^[!+-]/.test(value) ? value[0] : '';
  const rest = prefix ? value.slice(1) : value;
  if (path.isAbsolute(rest) || rest.startsWith('~')) return value;
  return prefix + path.resolve(root, rest);
}
try {
  const plan = JSON.parse(process.env.VIBEPOD_PROVIDER_PLAN);
  if (!['pi', 'codex', 'opencode'].includes(plan.agent)) throw new Error('Invalid agent');
  const home = process.env.HOME;
  if (!home) throw new Error('Missing home');
  const isPi = plan.agent === 'pi';
  const isOpencode = plan.agent === 'opencode';
  // OpenCode merges config sources in order: global $XDG_CONFIG_HOME/opencode
  // files, OPENCODE_CONFIG, project .opencode dirs, then OPENCODE_CONFIG_DIR
  // last. The view therefore becomes OPENCODE_CONFIG_DIR (the winning source);
  // XDG_CONFIG_HOME stays untouched so every other tool keeps its config.
  const root = isPi
    ? (process.env.PI_CODING_AGENT_DIR || path.join(home, '.pi', 'agent'))
    : isOpencode
      ? (process.env.OPENCODE_CONFIG_DIR
        || path.join(process.env.XDG_CONFIG_HOME || path.join(home, '.config'), 'opencode'))
      : path.join(home, '.codex');
  const command = JSON.parse(process.env.VIBEPOD_PROVIDER_COMMAND || 'null');
  if (!Array.isArray(command) || !command.length || command.some(v => typeof v !== 'string')) {
    throw new Error('Missing command');
  }
  // Parse before creating persistent directories or launching the agent.
  if (isOpencode && fs.existsSync(path.join(root, 'opencode.jsonc'))) {
    throw new UserError('Temporary provider routing needs opencode.json; found opencode.jsonc in '
      + root + '. Convert it to opencode.json for provider launches.');
  }
  const auth = isOpencode ? null : objectFile(root, 'auth.json');
  const settings = isPi ? objectFile(root, 'settings.json') : null;
  const models = isPi ? objectFile(root, 'models.json') : null;
  const opencodeConfig = isOpencode ? objectFile(root, 'opencode.json') : null;
  if (isPi && models.providers !== undefined &&
      (!models.providers || typeof models.providers !== 'object' || Array.isArray(models.providers))) {
    throw new Error('Invalid provider map');
  }
  temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'vibepod-provider-'));
  fs.chmodSync(temporary, 0o700);
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  const privateFiles = new Set(isPi
    ? ['models.json', 'auth.json', 'settings.json', 'models-store.json']
    : isOpencode
      ? ['opencode.json']
      : ['config.toml', 'auth.json', 'models_cache.json']);
  const dirs = isPi
    ? ['sessions', 'extensions', 'skills', 'prompts', 'themes', 'npm', 'git']
    : isOpencode
      ? []
      : ['sessions', 'archived_sessions'];
  for (const dir of dirs) fs.mkdirSync(path.join(root, dir), { recursive: true, mode: 0o700 });
  const copyRecursively = (source, target) => {
    if (fs.statSync(source).isDirectory()) {
      fs.mkdirSync(target, { recursive: true, mode: 0o700 });
      for (const name of fs.readdirSync(source)) {
        copyRecursively(path.join(source, name), path.join(target, name));
      }
      return;
    }
    fs.copyFileSync(source, target);
    fs.chmodSync(target, 0o600);
  };
  for (const name of fs.readdirSync(root)) {
    // Native profile locks belong to other processes, not to this private view.
    if (name.endsWith('.lock') && privateFiles.has(name.slice(0, -5))) continue;
    const source = path.join(root, name), target = path.join(temporary, name);
    if (privateFiles.has(name)) {
      copyRecursively(source, target);
    } else {
      fs.symlinkSync(source, target);
    }
  }
  if (isPi) {
    models.providers = { ...(models.providers || {}), ...plan.providers };
    for (const [name, provider] of Object.entries(plan.providers)) {
      // Auth-file entries outrank models.json, so override only in this temporary copy.
      auth[name] = { type: 'api_key', key: provider.apiKey };
    }
    for (const field of ['extensions', 'skills', 'prompts', 'themes']) {
      if (Array.isArray(settings[field])) {
        settings[field] = settings[field].map(value => resourcePath(root, value));
      }
    }
    if (Array.isArray(settings.packages)) {
      const local = value => typeof value === 'string' && /^\.\.?\//.test(value)
        ? path.resolve(root, value) : value;
      settings.packages = settings.packages.map(pkg => typeof pkg === 'string'
        ? local(pkg) : { ...pkg, source: local(pkg.source) });
    }
    privateJson('models.json', models);
    privateJson('settings.json', settings);
    privateJson('auth.json', auth);
  }
  if (isOpencode) {
    // Verified against opencode source (provider/provider.ts, config/variable.ts):
    // npm package names per protocol, {env:VAR} substitution, model "provider/id".
    const npm = {
      'openai-chat': '@ai-sdk/openai-compatible',
      'openai-responses': '@ai-sdk/openai',
      'anthropic': '@ai-sdk/anthropic',
    };
    opencodeConfig.provider = opencodeConfig.provider
      && typeof opencodeConfig.provider === 'object'
      && !Array.isArray(opencodeConfig.provider) ? opencodeConfig.provider : {};
    const names = Object.keys(plan.providers);
    for (const name of names) {
      const entry = plan.providers[name];
      // @ai-sdk/anthropic treats baseURL as the full prefix (default
      // https://api.anthropic.com/v1); registered Anthropic URLs omit /v1.
      const baseURL = entry.protocol === 'anthropic'
        ? entry.baseUrl.replace(/\/+$/, '') + '/v1'
        : entry.baseUrl;
      const options = { baseURL };
      if (entry.apiKeyEnv) options.apiKey = '{env:' + entry.apiKeyEnv + '}';
      // Null prototype: a model literally named __proto__ must stay an own property.
      const models = Object.create(null);
      for (const model of entry.models) {
        // opencode model config: limit.{context,output} (both required) and reasoning.
        const settings = (entry.settings || {})[model] || {};
        const config = {};
        if (settings.contextWindow && settings.maxOutputTokens) {
          config.limit = { context: settings.contextWindow, output: settings.maxOutputTokens };
        }
        if (typeof settings.reasoning === 'boolean') config.reasoning = settings.reasoning;
        models[model] = config;
      }
      opencodeConfig.provider[name] = { npm: npm[entry.protocol], name, options, models };
    }
    if (names.length === 1) {
      const entry = plan.providers[names[0]];
      if (entry.defaultModel) opencodeConfig.model = names[0] + '/' + entry.defaultModel;
    }
    privateJson('opencode.json', opencodeConfig);
  }
  const env = { ...process.env };
  delete env.VIBEPOD_PROVIDER_PLAN;
  delete env.VIBEPOD_PROVIDER_COMMAND;
  if (isPi) env.PI_CODING_AGENT_DIR = temporary;
  else if (isOpencode) env.OPENCODE_CONFIG_DIR = temporary;
  else env.CODEX_HOME = temporary;
  child = spawn(command[0], command.slice(1), { stdio: 'inherit', env });
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    process.on(signal, () => { if (child) child.kill(signal); });
  }
  child.on('error', () => {
    cleanup();
    console.error('Could not start agent with temporary provider configuration.');
    process.exitCode = 1;
  });
  child.on('exit', (code, signal) => {
    cleanup();
    process.exitCode = code === null ? 128 + (os.constants.signals[signal] || 1) : code;
  });
} catch (error) {
  cleanup();
  // Never print parse errors: their messages can contain native credential values.
  console.error(error instanceof UserError
    ? error.message
    : 'Cannot prepare temporary provider configuration; check native profile files.');
  process.exitCode = 1;
}
