// Offline contract/type and real React rendering tests. No install or web server.
// Use the existing dependency tree via MACRO_WEB_NODE_MODULES when this worktree
// has none. This does not mutate that dependency tree or execute app network I/O.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const root = path.resolve(__dirname, '../..');
const modules = process.env.MACRO_WEB_NODE_MODULES || path.join(root, 'web/node_modules');
const dependencies = createRequire(path.join(modules, '__contract_test__.cjs'));
const ts = dependencies('typescript');
const React = dependencies('react');
const { renderToStaticMarkup } = dependencies('react-dom/server');

function typecheck(source) {
  const filename = path.join(root, 'web/lib/__virtual_contract__.ts');
  const options = { strict: true, noEmit: true, target: ts.ScriptTarget.ES2020, skipLibCheck: true, types: [] };
  const host = ts.createCompilerHost(options);
  const original = host.getSourceFile.bind(host);
  host.getSourceFile = (file, languageVersion, ...args) => file === filename
    ? ts.createSourceFile(file, source, languageVersion, true)
    : original(file, languageVersion, ...args);
  const program = ts.createProgram([filename], options, host);
  return ts.getPreEmitDiagnostics(program).map(d => ts.flattenDiagnosticMessageText(d.messageText, '\n'));
}

test('authoritative day, capability, and coverage fields have the shared optional rolling contract', () => {
  const types = fs.readFileSync(path.join(root, 'web/lib/types.ts'), 'utf8');
  const cases = JSON.parse(fs.readFileSync(path.join(root, 'docs/agent-canvas/fixtures/day-readiness.json')));
  const payload = JSON.stringify(cases.at(-1).payload);
  const source = types + `\nconst wire = ${payload}; const day: DayPayload = wire;
    const readiness: boolean | undefined = day.has_targets;
    const zone: string | undefined = day.day_timing?.time_zone;
    const hour: number | undefined = day.day_timing?.rollover_hour;
    const current: string | undefined = day.day_timing?.effective_date;
    const protocol: string | undefined = day.canvas_protocol;
    const coverage: WorkoutStatsPayload['coverage'] = {muscle_groups: {}, workout_types: {}, untouched: [],
      muscle_coverage_complete: false, unclassified_days: ['2026-09-16'], note: 'Fixture note'};
    const complete: boolean | undefined = coverage.muscle_coverage_complete;
    const unknown: string[] | undefined = coverage.unclassified_days;
    const note: string | null | undefined = coverage.note;
    const legacy: WorkoutStatsPayload['coverage'] = {muscle_groups: {}, workout_types: {}, untouched: []};`;
  assert.deepEqual(typecheck(source), []);
});

test('the changed dashboard typechecks against the actual web contract', () => {
  const options = { strict: true, noEmit: true, target: ts.ScriptTarget.ES2020,
    jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.Bundler,
    skipLibCheck: true, esModuleInterop: true, baseUrl: path.join(root, 'web'), paths: { '@/*': ['./*'] },
    typeRoots: [path.join(modules, '@types')], types: ['node', 'react', 'react-dom'] };
  const host = ts.createCompilerHost(options);
  host.resolveModuleNames = (names, containingFile) => names.map(name =>
    ts.resolveModuleName(name, containingFile, options, host).resolvedModule ||
    ts.resolveModuleName(name, path.join(path.dirname(modules), '__dependency_resolution__.tsx'), options, host).resolvedModule);
  const program = ts.createProgram([path.join(root, 'web/components/WorkoutDashboard.tsx')], options, host);
  const diagnostics = ts.getPreEmitDiagnostics(program).map(d => ts.flattenDiagnosticMessageText(d.messageText, '\n'));
  assert.deepEqual(diagnostics, []);
});

function renderCoverage(coverage) {
  const source = fs.readFileSync(path.join(root, 'web/components/WorkoutDashboard.tsx'), 'utf8');
  const code = ts.transpileModule(source, { compilerOptions: {
    jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
    esModuleInterop: true,
  }}).outputText;
  const data = {
    today: { date: '2026-09-16', entries: 2, exercises: [] },
    week: { week_label: 'Fixture week', days_logged: 1, total_sets: 2, streak_weeks: 0 },
    coverage, prs: [], plan: { today: { type: 'Push', exercises: [] }, next: [] },
  };
  const module = { exports: {} };
  const requireFixture = name => name === 'swr'
    ? { __esModule: true, default: () => ({ data, error: null, isLoading: false, mutate: () => {} }) }
    : dependencies(name);
  new Function('require', 'module', 'exports', code)(requireFixture, module, module.exports);
  return renderToStaticMarkup(React.createElement(module.exports.default, { revision: 0 }));
}

const classified = { muscle_groups: { Push: 1, Pull: 0, Quads: 0, Hams: 0, Abs: 0 },
  workout_types: { Push: 1, Cardio: 1 }, untouched: ['Pull', 'Quads', 'Hams', 'Abs'] };

test('legacy and fully classified coverage retain useful bars and Not hit', () => {
  for (const coverage of [classified, { ...classified, muscle_coverage_complete: true, unclassified_days: [], note: null }]) {
    const html = renderCoverage(coverage);
    assert.match(html, /Not hit: Pull, Quads, Hams, Abs/);
    assert.match(html, /width:100%/);
  }
});

test('incomplete coverage shows classified bars and its note without claiming untouched muscles', () => {
  const html = renderCoverage({ ...classified, muscle_coverage_complete: false,
    unclassified_days: ['2026-09-16'], note: 'Counts show classified days only.' });
  assert.match(html, /Counts show classified days only\./);
  assert.match(html, /Classified coverage/);
  assert.match(html, /width:100%/);
  assert.doesNotMatch(html, /Not hit:/);
});
