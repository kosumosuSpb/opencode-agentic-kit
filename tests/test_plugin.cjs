const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const child = require('node:child_process')
const { pathToFileURL } = require('node:url')

async function main() {
  const base = path.resolve(__dirname, '..')
  const input = fs.readFileSync(path.join(base, 'plugin/index.ts'), 'utf8')
  const tuiInput = fs.readFileSync(path.join(base, 'plugin/tui.ts'), 'utf8')
  assert.match(input, /process\.env\.AGENTIC_RUNNER \|\| join\(dataHome, "opencode-agentic", "runner", "agentic_gate\.py"\)/)
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'agentic-preview-plugin-'))
  const root = path.join(temp, 'repo')
  fs.mkdirSync(root)
  fs.mkdirSync(path.join(root, 'nested'))
  child.execFileSync('git', ['init', '-q', root])
  const secondRoot = path.join(temp, 'second-repo')
  fs.mkdirSync(secondRoot)
  child.execFileSync('git', ['init', '-q', secondRoot])
  const diffRoot = path.join(temp, 'diff-repo')
  const diffHome = path.join(temp, 'diff-home')
  fs.mkdirSync(diffRoot)
  fs.mkdirSync(diffHome)
  const diffEnv = {...process.env, HOME:diffHome, GIT_CONFIG_NOSYSTEM:'1'}
  delete diffEnv.GIT_PAGER
  delete diffEnv.PAGER
  child.execFileSync('git', ['init', '-q', diffRoot], {env:diffEnv})
  fs.writeFileSync(path.join(diffRoot, 'tracked.txt'), 'before\n')
  child.execFileSync('git', ['add', 'tracked.txt'], {cwd:diffRoot, env:diffEnv})
  child.execFileSync('git', [
    '-c', 'user.name=Agentic Test', '-c', 'user.email=agentic@example.invalid',
    'commit', '-qm', 'baseline',
  ], {cwd:diffRoot, env:diffEnv})
  fs.writeFileSync(path.join(diffRoot, 'tracked.txt'), 'after\n')
  const pagerMarker = path.join(temp, 'pager-called')
  const fsmonitorMarker = path.join(temp, 'fsmonitor-called')
  const pagerScript = path.join(temp, 'pager.sh')
  const fsmonitorScript = path.join(temp, 'fsmonitor.sh')
  fs.writeFileSync(pagerScript, `#!/bin/sh\n: > '${pagerMarker}'\ncat\n`)
  fs.writeFileSync(fsmonitorScript, `#!/bin/sh\n: > '${fsmonitorMarker}'\nprintf 'token\\n'\n`)
  fs.chmodSync(pagerScript, 0o700)
  fs.chmodSync(fsmonitorScript, 0o700)
  child.execFileSync('git', ['config', '--global', 'core.pager', pagerScript], {env:diffEnv})
  child.execFileSync('git', ['config', '--global', 'core.fsmonitor', fsmonitorScript], {env:diffEnv})
  child.execFileSync('script', [
    '-qefc', 'git --paginate diff --no-ext-diff --no-textconv -- tracked.txt', '/dev/null',
  ], {cwd:diffRoot, env:diffEnv, stdio:'ignore'})
  assert.equal(fs.existsSync(pagerMarker), true, 'control command must invoke configured pager')
  fs.rmSync(pagerMarker)
  if (fs.existsSync(fsmonitorMarker)) fs.rmSync(fsmonitorMarker)
  child.execFileSync('git', [
    'diff', '--no-ext-diff', '--no-textconv', '--', 'tracked.txt',
  ], {cwd:diffRoot, env:diffEnv})
  assert.equal(fs.existsSync(fsmonitorMarker), true,
    'control command must invoke configured fsmonitor hook')
  fs.rmSync(fsmonitorMarker)
  child.execFileSync('git', [
    '-c', 'core.fsmonitor=false', '--no-pager', 'diff',
    '--no-ext-diff', '--no-textconv', '--', 'tracked.txt',
  ], {cwd:diffRoot, env:diffEnv})
  assert.equal(fs.existsSync(pagerMarker), false, 'safe diff must suppress configured pager')
  assert.equal(fs.existsSync(fsmonitorMarker), false, 'safe diff must suppress fsmonitor hook')
  const oldStateDir = process.env.AGENTIC_STATE_DIR
  const oldRunner = process.env.AGENTIC_RUNNER
  const realRunner = path.join(base, 'runner/agentic/cli.py')
  const runnerShim = path.join(temp, 'runner-shim.py')
  fs.writeFileSync(runnerShim, [
    'from pathlib import Path',
    'import json, sys',
    `REAL = Path(${JSON.stringify(realRunner)})`,
    'sys.path.insert(0, str(REAL.parent.parent))',
    'from agentic import cli as module',
    "if 'gate' in sys.argv and sys.argv[-2] == 'gate' and sys.argv[-1] in {'verify', 'full'}:",
    "    level = sys.argv[-1]",
    "    project = Path(sys.argv[sys.argv.index('--project') + 1]).resolve()",
    '    module.validate_git_root(project)',
    '    task_id = module.require_open_task(project)',
    '    baseline = module.validate_baseline(project)',
    '    receipt = module.receipt_path(project, level)',
    '    module._write_json_atomic(receipt, {',
    "        'task_id': task_id, 'level': level, 'fingerprint': module.fingerprint(project),",
    "        'baseline': baseline, 'commands': [], 'wheel_verified': False,",
    "        'passed_at': '2026-11-13T01:00:00+00:00',",
    '    })',
    "    print(f'=== QUALITY GATE {level.upper()}: PASS (test shim) ===')",
    '    raise SystemExit(0)',
    'raise SystemExit(module.main())',
    '',
  ].join('\n'))
  process.env.AGENTIC_STATE_DIR = path.join(temp, 'state')
  process.env.AGENTIC_RUNNER = runnerShim
  try {
    const pluginPackage = path.join(temp, 'node_modules/@opencode/plugin')
    fs.mkdirSync(pluginPackage, {recursive: true})
    fs.writeFileSync(path.join(pluginPackage, 'package.json'), JSON.stringify({
      type: 'module', exports: {'.': './index.js', './tui': './tui.js'},
    }))
    fs.writeFileSync(path.join(pluginPackage, 'index.js'), 'export const Plugin = { define: value => value }\n')
    fs.writeFileSync(path.join(pluginPackage, 'tui.js'), 'export const Plugin = { define: value => value }\n')
    const pluginCopy = path.join(temp, 'index.ts')
    fs.writeFileSync(pluginCopy, input)
    const { default: definition } = await import(pathToFileURL(pluginCopy).href)
    assert.equal(definition.id, 'agentic.workflow.preview')
    const tuiCopy = path.join(temp, 'tui.ts')
    fs.writeFileSync(tuiCopy, tuiInput)
    const { default: tuiDefinition } = await import(pathToFileURL(tuiCopy).href)
    assert.equal(tuiDefinition.id, 'agentic.workflow.preview.tui')
    let inboxListener
    const toasts = []
    let tuiLayer
    const tuiCalls = []
    const createdSessions = []
    const navigations = []
    let defaultLocationCalls = 0
    let createSessionError
    let createdSessionID = 'new-session-1'
    let activeRoute = {type:'session', sessionID:'s1'}
    const tuiContext = {
      data: {
        location: { default() { defaultLocationCalls += 1; return {directory: root} } },
        on(name, fn) { assert.equal(name, 'session.inbox.enqueued'); inboxListener = fn; return () => {} },
      },
      keymap: { layer(factory) { tuiLayer = factory(); return () => {} } },
      client: { session: {
        async create(options) {
          createdSessions.push(options)
          if (createSessionError) throw createSessionError
          return {id: createdSessionID}
        },
        async command(options) {
        // Enforce the actual OpenCode 2.2.x command RPC contract.
        for (const key of ['sessionID', 'name', 'text']) {
          if (!Object.hasOwn(options, key)) throw new Error(`Missing key at [\"${key}\"]`)
        }
        tuiCalls.push(options)
      } } },
      location: { directory: root },
      ui: {
        router: {
          current() { return activeRoute },
          navigate(route) { navigations.push(route); activeRoute = route },
        },
        toast: { show(options) { toasts.push(options) } },
      },
    }
    const cleanup = tuiDefinition.setup(tuiContext)
    assert.equal(typeof cleanup, 'function')
    inboxListener({data:{id:'ordinary',type:'synthetic',payload:{text:'hidden',metadata:{}}}})
    assert.equal(toasts.length, 0)
    const notice = {data:{id:'task-result',type:'synthetic',payload:{
      text:'TASK ACTIVE: task-a', description:'TASK ACTIVE: task-a',
      metadata:{agenticKitTaskResult:true},
    }}}
    inboxListener(notice)
    inboxListener(notice)
    assert.equal(toasts.length, 1)
    assert.equal(toasts[0].title, 'Agentic Kit')
    assert.ok(toasts[0].message.includes('task-a'))
    assert.equal(tuiLayer.mode, 'global')
    assert.deepEqual(tuiLayer.commands.map(command => command.id),
      ['agentic.design', 'agentic.task', 'agentic.approve', 'agentic.implement',
        'agentic.review', 'agentic.finalize', 'agentic.accept'])
    assert.ok(tuiLayer.commands.every(command => command.palette === true))
    assert.ok(tuiLayer.commands.every(command => command.slash === undefined))
    await tuiLayer.commands.find(command => command.id === 'agentic.task').run('status')
    await tuiLayer.commands.find(command => command.id === 'agentic.implement').run('UNIQUE_TUI_MARKER')
    assert.deepEqual(tuiCalls, [
      { sessionID: 's1', directory: root, name: 'task', text: 'status' },
      { sessionID: 's1', directory: root, name: 'implement', text: 'UNIQUE_TUI_MARKER' },
    ])
    assert.deepEqual(createdSessions, [], 'Existing session must be reused')
    activeRoute = {type: 'home'}
    await tuiLayer.commands.find(command => command.id === 'agentic.task').run('status')
    assert.deepEqual(createdSessions, [{location: {directory: root}}],
      'Kit must create a session at the home screen without a model prompt')
    assert.deepEqual(navigations, [{type: 'session', sessionID: 'new-session-1'}])
    assert.deepEqual(tuiCalls[2],
      {sessionID: 'new-session-1', directory: root, name: 'task', text: 'status'})
    assert.ok(!toasts.some(toast => toast.message?.includes('requires an active session')))
    activeRoute = {type: 'home'}
    tuiContext.location = undefined
    createdSessionID = 'default-location-session'
    await tuiLayer.commands.find(command => command.id === 'agentic.task').run('fallback')
    assert.equal(defaultLocationCalls, 1)
    assert.deepEqual(createdSessions.at(-1), {location: {directory: root}})
    assert.deepEqual(navigations.at(-1),
      {type: 'session', sessionID: 'default-location-session'})
    assert.deepEqual(tuiCalls.at(-1),
      {sessionID: 'default-location-session', directory: undefined, name: 'task', text: 'fallback'})

    activeRoute = {type: 'home'}
    createSessionError = new Error('session creation rejected')
    const commandsBeforeRejectedCreate = tuiCalls.length
    const navigationsBeforeRejectedCreate = navigations.length
    const toastsBeforeRejectedCreate = toasts.length
    await tuiLayer.commands.find(command => command.id === 'agentic.task').run('rejected')
    createSessionError = undefined
    assert.equal(tuiCalls.length, commandsBeforeRejectedCreate)
    assert.equal(navigations.length, navigationsBeforeRejectedCreate)
    assert.equal(toasts.length, toastsBeforeRejectedCreate + 1)
    assert.equal(toasts.at(-1).variant, 'error')
    assert.match(toasts.at(-1).message, /session creation rejected/)

    for (const invalidSessionID of ['', '   ']) {
      activeRoute = {type: 'home'}
      createdSessionID = invalidSessionID
      const commandsBeforeInvalidID = tuiCalls.length
      const navigationsBeforeInvalidID = navigations.length
      const toastsBeforeInvalidID = toasts.length
      await tuiLayer.commands.find(command => command.id === 'agentic.task').run('invalid')
      assert.equal(tuiCalls.length, commandsBeforeInvalidID,
        `Command must not run with session ID ${JSON.stringify(invalidSessionID)}`)
      assert.equal(navigations.length, navigationsBeforeInvalidID,
        `Router must not navigate with session ID ${JSON.stringify(invalidSessionID)}`)
      assert.equal(toasts.length, toastsBeforeInvalidID + 1)
      assert.equal(toasts.at(-1).variant, 'error')
      assert.match(toasts.at(-1).message, /Unable to create an OpenCode session/)
    }
    const commands = new Map()
    const calls = []
    const sessionUpdates = []
    let contextHook
    let promptHook
    let permissionHook
    const sessionState = new Map([
      ['s1', {id:'s1', directory:path.join(root, 'nested'), agent:'build', permissions:[
        {action:'edit',resource:'*',effect:'deny'},
        {action:'edit',resource:'tests/*',effect:'allow'},
        {action:'edit',resource:'*',effect:'deny'},
        {action:'shell',resource:'*',effect:'deny'},
      ]}],
      ['s2', {id:'s2', directory:secondRoot, agent:'build', permissions:[]}],
    ])
    const session = {
      async get({ sessionID }) { return sessionID === 's3' ? {id: sessionID} : sessionState.get(sessionID) },
      async hook(name, fn) {
        if (name === 'context') contextHook = fn
        else if (name === 'prompt') promptHook = fn
        else assert.fail(`Unexpected session hook: ${name}`)
      },
      async update({sessionID, permissions, metadata}) {
        const state = sessionState.get(sessionID)
        if (permissions !== undefined) state.permissions = permissions
        if (metadata !== undefined) state.metadata = metadata
        sessionUpdates.push({sessionID, permissions, metadata})
      },
      async switchAgent(options) { sessionState.get(options.sessionID).agent = options.agent; calls.push(['agent', options.agent]) },
      async switchModel(options) { calls.push(['model', options.model]) },
      async prompt(options) { calls.push(['prompt', options.text, options]) },
      async synthetic(options) { calls.push(['synthetic', options]) },
    }
    const ctx = {
      session,
      permission: { async hook(name, fn) { assert.equal(name, 'evaluate'); permissionHook = fn } },
      command: { async transform(fn) { fn({ add(c) { commands.set(c.name, c) } }) } },
    }
    await definition.setup(ctx)
    assert.equal(contextHook, undefined,
      'Provider context dispatch must not enforce workflow task preconditions')
    assert.equal(typeof promptHook, 'function')
    await promptHook({sessionID:'s1',prompt:{text:'ordinary build session'}})
    assert.equal(fs.existsSync(path.join(temp, 'state', 'projects')), false,
      'An ordinary session prompt must not create Agentic task state')
    sessionState.get('s1').agent = 'developer'
    await assert.rejects(
      () => promptHook({sessionID:'s1',prompt:{text:'manual developer prompt'}}),
      /NO_ACTIVE_TASK/,
      'Direct workflow-role prompts must retain task guards',
    )
    sessionState.get('s1').agent = 'build'
    const runRolePrompt = async (sessionID, agent) => {
      sessionState.get(sessionID).agent = agent
      return promptHook({sessionID,prompt:{text:`manual ${agent} prompt`}})
    }
    for (const resource of [
      'git commit -am change',
      'git -C . commit -am change',
      'git -c alias.x=commit x',
      'command git commit -am change',
      "bash -c 'git commit -am change'",
      'g""it commit -am change',
      'ls -la',
      'find .agentic -maxdepth 3 -type f',
      'python3 --version',
      'python3 -m pytest --version',
      'python3 -c "import setuptools; print(setuptools.__version__)"',
    ]) {
      const request = {agent:'developer',action:'shell',resources:[resource],effect:'allow'}
      await permissionHook(request)
      assert.equal(request.effect, 'ask', `${resource} must not inherit a broad allow`)
    }
    for (const resource of [
      'git push origin main',
      'git -C . push origin main',
      'command git push origin main',
      "bash -c 'git push origin main'",
      'g""it push origin main',
      'python3 -m twine upload dist/*',
      'uv publish dist/pkg.whl',
      'npm publish',
      'pnpm publish',
      'cargo publish',
      'kubectl apply -f deployment.yaml',
      'helm upgrade api ./chart',
      'terraform apply plan.tfplan',
      'docker push registry.example/app:latest',
      'make deploy',
      'npm run deploy',
      'ansible-playbook production.yml',
    ]) {
      const request = {agent:'developer',action:'shell',resources:[resource],effect:'allow'}
      await permissionHook(request)
      assert.equal(request.effect, 'deny', `${resource} must be denied`)
    }
    const status = {agent:'developer',action:'shell',resources:['git status --short'],effect:'allow'}
    await permissionHook(status)
    assert.equal(status.effect, 'allow')
    for (const resources of [
      ['git -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv --stat'],
      ['git log --oneline -5'],
      ['git status', 'git -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv --stat'],
      ['git status --short', 'git log --oneline -5'],
    ]) {
      const request = {agent:'developer',action:'shell',resources,effect:'ask'}
      await permissionHook(request)
      assert.equal(request.effect, 'allow', `${resources.join(' && ')} should be auto-allowed`)
    }
    for (const resources of [
      ['git status && git diff --stat'],
      ['git status', 'rm -rf target'],
      ['git diff --output=result.txt'],
      ['git status | wc -l'],
      ['bash', 'git status'],
      ['echo $(git status)'],
      ['git status --unknown'],
      ['ls -la', 'echo ---', 'find .agentic -maxdepth 3 -type f'],
    ]) {
      const request = {agent:'developer',action:'shell',resources,effect:'allow'}
      await permissionHook(request)
      assert.equal(request.effect, 'ask', `${resources.join(' / ')} must require a decision`)
    }
    const mixed = {agent:'developer',action:'shell',resources:['git status --short','rm -rf target'],effect:'allow'}
    await permissionHook(mixed)
    assert.equal(mixed.effect, 'ask')
    const configuredDeny = {agent:'developer',action:'shell',resources:['git commit -am x'],effect:'deny'}
    await permissionHook(configuredDeny)
    assert.equal(configuredDeny.effect, 'deny', 'Hook must not weaken an existing deny')
    const safeConfiguredDeny = {agent:'developer',action:'shell',resources:['git status'],effect:'deny'}
    await permissionHook(safeConfiguredDeny)
    assert.equal(safeConfiguredDeny.effect, 'deny', 'Safe allowlist must not override an existing deny')
    const build = {agent:'build',action:'shell',resources:['git commit -am x'],effect:'allow'}
    await permissionHook(build)
    assert.equal(build.effect, 'allow', 'Hook must stay scoped to workflow roles')
    for (const resource of [
      'touch arithmetic.py',
      'cat > arithmetic.py',
      'printf x >> arithmetic.py',
      'echo x | tee arithmetic.py',
      'cp template.py arithmetic.py',
      'mv temp.py arithmetic.py',
      'sed -i s/x/y/ arithmetic.py',
    ]) {
      const request = {agent:'developer',action:'shell',resources:[resource],effect:'ask'}
      await permissionHook(request)
      assert.equal(request.effect, 'deny', `${resource} must not bypass write/edit permissions`)
    }
    const redirected = {agent:'developer',action:'shell',resources:['git status > status.txt'],effect:'ask'}
    await permissionHook(redirected)
    assert.equal(redirected.effect, 'deny')
    const runnerPrefix = `python3 '${process.env.AGENTIC_RUNNER}' --project '${root}'`
    for (const resource of [
      `${runnerPrefix} tdd --cycle arithmetic --stage red --expect "missing function" -- python -m pytest -q tests/test_arithmetic.py`,
      `${runnerPrefix} tdd --cycle arithmetic --stage red --scope regression --expect "full suite contains the new failure" -- python -m pytest -q tests`,
      `${runnerPrefix} tdd --cycle arithmetic --stage green -- python -m pytest -q tests/test_arithmetic.py`,
      `${runnerPrefix} baseline`,
      `${runnerPrefix} gate fast`,
      `${runnerPrefix} evidence`,
    ]) {
      const request = {sessionID:'s1',agent:'developer',action:'shell',resources:[resource],effect:'allow'}
      await permissionHook(request)
      assert.equal(request.effect, 'allow', `${resource} must be allowed as a scoped Runner call`)
    }
    for (const expectation of [
      "'expected value with spaces'",
      "'expected > actual'",
      "'left < right'",
      "'quoted \"value\"'",
      "'can'\"'\"'t parse'",
      "'literal ; touch result.txt'",
    ]) {
      const resource = `${runnerPrefix} tdd --cycle quoted --stage red --expect ${expectation} -- python -m pytest -q tests/test_quoted.py`
      const request = {sessionID:'s1',agent:'developer',action:'shell',resources:[resource],effect:'ask'}
      await permissionHook(request)
      assert.equal(request.effect, 'allow', `${expectation} must remain one inert argv value`)
    }
    for (const resource of [
      `${runnerPrefix} tdd --cycle quoted --stage red --expect expected > result.txt -- python -m pytest`,
      `${runnerPrefix} tdd --cycle quoted --stage red --expect "$(touch result.txt)" -- python -m pytest`,
      `${runnerPrefix} tdd --cycle quoted --stage red --expect 'safe' -- python -c 'print(1)'`,
      `${runnerPrefix} tdd --cycle quoted --stage red --expect 'safe' -- python -m pytest\ngit status`,
    ]) {
      const request = {sessionID:'s1',agent:'developer',action:'shell',resources:[resource],effect:'ask'}
      await permissionHook(request)
      assert.notEqual(request.effect, 'allow', `${resource} must not bypass shell/Runner validation`)
    }
    const safePathspecDiffs = [
      'git -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv -- src/app.py',
      'git -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv -- tests/test_app.py',
      "git -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv -- 'src/file with spaces.py'",
      'git -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv --cached -- tests/test_app.py',
      'git -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv --stat -- src/app.py',
      'git -c core.fsmonitor=false --no-pager diff --no-textconv --cached --no-ext-diff -- tests/test_app.py',
    ]
    for (const agent of ['developer', 'reviewer']) for (const resource of safePathspecDiffs) {
      const request = {sessionID:'s1',agent,action:'shell',resources:[resource],effect:'ask'}
      await permissionHook(request)
      assert.equal(request.effect, 'allow', `${resource} must be allowed for ${agent}`)
    }
    for (const resource of [
      'git diff --output=result.txt -- src/app.py',
      'git diff --ext-diff -- src/app.py',
      'git diff --textconv -- src/app.py',
      'git diff --no-ext-diff --no-textconv --output=result.txt -- src/app.py',
      'git diff --no-ext-diff --no-textconv --ext-diff -- src/app.py',
      'git diff --no-ext-diff --no-textconv --textconv -- src/app.py',
      'git -c diff.external=touch diff --no-ext-diff --no-textconv -- src/app.py',
      'git --no-pager diff --no-ext-diff --no-textconv -- src/app.py',
      'git -c core.fsmonitor=false diff --no-ext-diff --no-textconv -- src/app.py',
      'git -c core.fsmonitor=true --no-pager diff --no-ext-diff --no-textconv -- src/app.py',
      'git -c core.fsmonitor=false -c core.fsmonitor=true --no-pager diff --no-ext-diff --no-textconv -- src/app.py',
      'git -c core.fsmonitor=false --no-pager --paginate diff --no-ext-diff --no-textconv -- src/app.py',
      'git -c core.fsmonitor=false --no-pager diff --paginate --no-ext-diff --no-textconv -- src/app.py',
      'git diff --no-ext-diff -- src/app.py',
      'git diff --no-textconv -- src/app.py',
      'git diff -- src/app.py > result.txt',
      'git diff -- src/app.py; git status',
      'git diff -- src/app.py && touch result.txt',
      'git diff -- $(touch result.txt)',
      'git diff -- src/app.py | cat',
      'git diff -- src/app.py\ngit status',
    ]) {
      for (const agent of ['developer', 'reviewer']) {
        const request = {sessionID:'s1',agent,action:'shell',resources:[resource],effect:'ask'}
        await permissionHook(request)
        assert.notEqual(request.effect, 'allow', `${resource} must not be auto-allowed for ${agent}`)
      }
    }
    for (const resource of [
      `${runnerPrefix} evidence`,
      `${runnerPrefix} ready verify`,
      `${runnerPrefix} ready full`,
      `${runnerPrefix} review-context verify`,
      `${runnerPrefix} review-context full`,
      `${runnerPrefix} review-submit 0123456789abcdef0123456789abcdef pass none`,
      `${runnerPrefix} review-submit 0123456789abcdef0123456789abcdef reject major:src/app.py:42:wrong-result`,
    ]) {
      const request = {sessionID:'s1',agent:'reviewer',action:'shell',resources:[resource],effect:'ask'}
      await permissionHook(request)
      assert.equal(request.effect, 'allow', `${resource} must be a scoped read-only Reviewer call`)
    }
    const reviewerTest = {sessionID:'s1',agent:'reviewer',action:'shell',
      resources:['PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v'],effect:'allow'}
    await permissionHook(reviewerTest)
    assert.equal(reviewerTest.effect, 'ask', 'Reviewer must not receive arbitrary Python execution')
    for (const resource of [
      `python3 -c 'print(1)'`,
      `${runnerPrefix} gate full`,
      `${runnerPrefix} --help`,
      `${runnerPrefix} gate fast > result.txt`,
      `python3 '${process.env.AGENTIC_RUNNER}' --project '${secondRoot}' gate fast`,
    ]) {
      const request = {sessionID:'s1',agent:'developer',action:'shell',resources:[resource],effect:'allow'}
      await permissionHook(request)
      assert.notEqual(request.effect, 'allow', `${resource} must not receive Runner auto-allow`)
    }
    assert.equal(commands.size, 7)
    await commands.get('task').execute({sessionID:'s1',prompt:{text:'new task-a'}})
    assert.equal(calls.at(-1)[0], 'synthetic')
    assert.equal(calls.at(-1)[1].resume, false)
    assert.equal(calls.at(-1)[1].metadata.agenticKitTaskResult, true)
    assert.ok(calls.at(-1)[1].description.includes('task-a'))
    await commands.get('task').execute({sessionID:'s1',prompt:{text:'status'}})
    assert.equal(calls.at(-1)[0], 'synthetic')
    assert.ok(calls.at(-1)[1].description.length > 0)
    await commands.get('task').execute({sessionID:'s1',prompt:{text:'use task-a'}})
    assert.equal(calls.at(-1)[0], 'synthetic')
    assert.ok(calls.at(-1)[1].description.includes('task-a'))
    assert.ok(fs.statSync(path.join(root, '.agentic/tasks/task-a/drafts')).isDirectory(),
      'task-new must create the draft directory')
    const designMarker = 'DESIGN_MARKER_7f2a'
    const beforeDesign = calls.length
    await commands.get('design').execute({ sessionID:'s1', prompt: { text: designMarker }, delivery: {} })
    assert.equal(calls[beforeDesign][1], 'architect')
    assert.equal(calls[beforeDesign + 1][1].id, 'gpt-5.6-sol')
    assert.ok(calls[beforeDesign + 2][1].includes(root))
    assert.equal(calls[beforeDesign + 2][1].split(designMarker).length - 1, 1)
    assert.ok(calls[beforeDesign + 2][1].includes('Непривилегированный контекст пользователя'))
    assert.ok(calls[beforeDesign + 2][1].includes('AC[1-9][0-9]{0,3}'))
    assert.ok(calls[beforeDesign + 2][1].includes('AC1'))
    assert.ok(calls[beforeDesign + 2][1].includes('AC10'))
    assert.ok(calls[beforeDesign + 2][1].includes('AC-1'))
    const scoped = sessionState.get('s1').permissions
    for (const resource of [
      'git status',
      'git status --short --branch',
      'git ls-tree -r --name-only HEAD',
    ]) {
      assert.ok(
        scoped.some(rule =>
          rule.action === 'shell' &&
          rule.resource === resource &&
          rule.effect === 'allow'),
        `Architect must allow safe read-only shell: ${resource}`)
    }
    assert.ok(!scoped.some(rule =>
      rule.action === 'shell' &&
      rule.resource === '*' &&
      rule.effect === 'allow'),
      'Architect must not receive global shell allow')
    assert.ok(!scoped.some(rule =>
      rule.action === 'shell' &&
      rule.resource === 'git push' &&
      rule.effect === 'allow'),
      'Architect must not allow git push')
    assert.ok(scoped.some(rule => rule.action === 'edit' && rule.resource === '.agentic/tasks/task-a/drafts/spec.md' && rule.effect === 'allow'))
    assert.ok(scoped.some(rule => rule.action === 'edit' && rule.resource === '.agentic/tasks/task-a/drafts/plan.md' && rule.effect === 'allow'))
    assert.ok(!scoped.some(rule => rule.action === 'edit' && rule.resource === '*' && rule.effect === 'allow'),
      'Architect must not receive global edit allow')
    const wildcard = (pattern, value) => new RegExp('^' + pattern.split('*').map(s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$').test(value)
    const inherited = [
      {action:'edit',resource:'*',effect:'deny'},
      {action:'edit',resource:'.agentic/drafts/*',effect:'allow'},
    ]
    const editEffect = resource => [...inherited, ...scoped].filter(rule => wildcard(rule.action, 'edit') && wildcard(rule.resource, resource)).at(-1)?.effect
    assert.equal(editEffect('.agentic/tasks/task-a/drafts/spec.md'), 'allow')
    assert.equal(editEffect('.agentic/tasks/task-b/drafts/spec.md'), 'deny')
    assert.equal(editEffect('src/app.py'), 'deny')
    assert.equal(editEffect('specs/tasks/task-a/approved.md'), 'deny')
    assert.equal(editEffect('.git/config'), 'deny')
    await commands.get('design').execute({ sessionID:'s2', prompt: { text: 'Another project' }, delivery: {} })
    assert.ok(calls.at(-1)[1].includes(secondRoot))
    assert.ok(fs.statSync(path.join(secondRoot, '.agentic/tasks/task-1/drafts')).isDirectory(),
      'design must create the implicit task draft directory')
    await assert.rejects(() => commands.get('design').execute({sessionID:'s3', prompt:{text:'x'},delivery:{}}), /PROJECT_UNRESOLVED/)
    const n = calls.length
    await assert.rejects(() => commands.get('implement').execute({sessionID:'s1',prompt:{text:'x'},delivery:{}}), /NO_APPROVAL/)
    assert.equal(calls.length, n, 'Blocked implement must not switch agent')
    await assert.rejects(() => runRolePrompt('s1', 'reviewer'), /NO_.*RECEIPT/)
    const reviewerScoped = sessionState.get('s1').permissions
    assert.ok(reviewerScoped.some(rule => rule.action === 'edit' && rule.resource === '*' && rule.effect === 'deny'))
    assert.ok(reviewerScoped.some(rule => rule.action === 'external_directory' && rule.resource === '*' && rule.effect === 'deny'))
    assert.ok(reviewerScoped.some(rule => rule.action === 'shell' && rule.resource === 'git status --short' && rule.effect === 'allow'))
    assert.ok(reviewerScoped.some(rule => rule.action === 'shell' && rule.resource.endsWith('review-context verify') && rule.effect === 'allow'))
    fs.writeFileSync(path.join(root, '.agentic/tasks/task-a/drafts/spec.md'), 'Spec')
    fs.writeFileSync(path.join(root, '.agentic/tasks/task-a/drafts/plan.md'), 'Plan')
    const beforeApproval = calls.length
    await commands.get('approve').execute({sessionID:'s1'})
    assert.equal(calls.length, beforeApproval + 1, 'Approval must only add one synthetic message')
    assert.equal(calls.at(-1)[0], 'synthetic')
    assert.equal(calls.at(-1)[1].resume, false, 'Approval must not resume the model')
    assert.equal(calls.at(-1)[1].metadata.agenticKitTaskResult, true)
    fs.writeFileSync(path.join(root, '.agentic/tasks/task-a/drafts/spec.md'), 'Revised spec')
    fs.writeFileSync(path.join(root, '.agentic/tasks/task-a/drafts/plan.md'), 'Revised plan')
    await commands.get('approve').execute({sessionID:'s1',prompt:{text:'replace'}})
    assert.equal(calls.at(-1)[0], 'synthetic')
    assert.equal(calls.at(-1)[1].resume, false)
    assert.ok(calls.at(-1)[1].description.includes('REPLACED'))
    assert.ok(calls.at(-1)[1].description.includes('archived'))
    const beforeImplement = calls.length
    const implementMarker = 'IMPLEMENT_MARKER_a91c'
    await commands.get('implement').execute({sessionID:'s1',prompt:{text:implementMarker},delivery:{}})
    assert.equal(calls[beforeImplement][0], 'agent')
    assert.equal(calls[beforeImplement][1], 'developer')
    assert.equal(calls.at(-1)[0], 'prompt', 'Explicit implement must prompt Developer')
    const afterImplement = sessionState.get('s1').permissions
    const activeDraftEffect = [...inherited, ...afterImplement].filter(rule => wildcard(rule.action, 'edit') && wildcard(rule.resource, '.agentic/tasks/task-a/drafts/spec.md')).at(-1)?.effect
    assert.equal(activeDraftEffect, 'deny', 'Leaving Architect must revoke draft editing')
    const developerEffect = (action, resource) => [...inherited, ...afterImplement]
      .filter(rule => wildcard(rule.action, action) && wildcard(rule.resource, resource)).at(-1)?.effect
    for (const editable of ['arithmetic.py', 'src/arithmetic.py', 'tests/test_arithmetic.py', 'pyproject.toml']) {
      assert.equal(developerEffect('edit', editable), 'allow', `${editable} must override inherited root deny`)
    }
    for (const protectedPath of [
      '.git/config',
      'specs/approved.md',
      'plans/approved.md',
      'specs/tasks/task-a/approved.md',
      'plans/tasks/task-a/approved.md',
      '.agentic/tasks/task-a/drafts/spec.md',
    ]) {
      assert.equal(developerEffect('edit', protectedPath), 'deny', protectedPath)
    }
    assert.equal(developerEffect('external_directory', '/tmp/other-project/file.py'), 'deny')
    assert.equal(developerEffect('shell', `${runnerPrefix} gate fast`), 'allow')
    assert.equal(developerEffect('shell', `${runnerPrefix} baseline`), 'allow')
    assert.equal(developerEffect('shell', `python3 arbitrary.py`), 'deny')
    assert.ok(calls.at(-1)[1].includes('RED feature:'))
    assert.ok(calls.at(-1)[1].includes('FAST GATE:'))
    assert.ok(calls.at(-1)[1].includes('"task_id": "task-a"'))
    assert.ok(calls.at(-1)[1].includes('specs/tasks/task-a/approved.md'))
    assert.ok(calls.at(-1)[1].includes('"verified": true'))
    assert.ok(calls.at(-1)[1].includes('"pytest_probe"'))
    assert.ok(calls.at(-1)[1].includes('Для исследования проекта используй только встроенные read, glob и grep'))

    const unmanagedPermissions = [
      {action:'edit',resource:'*',effect:'deny'},
      {action:'edit',resource:'user-owned/**',effect:'allow'},
      {action:'shell',resource:'*',effect:'deny'},
      {action:'shell',resource:'user-owned-status',effect:'allow'},
    ]
    const addPermissionSession = sessionID => sessionState.set(sessionID, {
      id: sessionID,
      directory: root,
      agent: 'build',
      metadata: {userOwned: 'preserve-me'},
      permissions: structuredClone(unmanagedPermissions),
    })
    const effect = (permissions, action, resource) => permissions
      .filter(rule => wildcard(rule.action, action) && wildcard(rule.resource, resource))
      .at(-1)?.effect ?? 'ask'
    const assertOutsideWorkflow = sessionID => {
      const state = sessionState.get(sessionID)
      assert.deepEqual(state.permissions, unmanagedPermissions,
        `${sessionID}: switching to Build must restore only non-Kit permissions`)
      assert.equal(state.metadata.userOwned, 'preserve-me')
      assert.equal(effect(state.permissions, 'edit', 'src/app.py'), 'deny')
      assert.equal(effect(state.permissions, 'edit', 'user-owned/file.txt'), 'allow')
      assert.equal(effect(state.permissions, 'shell', `${runnerPrefix} gate fast`), 'deny')
      assert.equal(effect(state.permissions, 'shell', 'user-owned-status'), 'allow')
      assert.equal(effect(state.permissions, 'edit', '.agentic/tasks/task-a/drafts/spec.md'), 'deny')
    }

    addPermissionSession('permissions-developer-build')
    await runRolePrompt('permissions-developer-build', 'developer')
    assert.equal(effect(sessionState.get('permissions-developer-build').permissions,
      'edit', 'src/app.py'), 'allow')
    assert.equal(effect(sessionState.get('permissions-developer-build').permissions,
      'shell', 'git -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv -- src/app.py'), 'allow')
    assert.notEqual(effect(sessionState.get('permissions-developer-build').permissions,
      'shell', 'arbitrary shell command'), 'allow', 'Developer must not receive shell=*')
    await runRolePrompt('permissions-developer-build', 'build')
    assertOutsideWorkflow('permissions-developer-build')
    assert.equal(effect(sessionState.get('permissions-developer-build').permissions,
      'shell', 'git -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv -- src/app.py'), 'deny')

    addPermissionSession('permissions-architect-build')
    await runRolePrompt('permissions-architect-build', 'architect')
    assert.equal(effect(sessionState.get('permissions-architect-build').permissions,
      'edit', '.agentic/tasks/task-a/drafts/spec.md'), 'allow')
    await runRolePrompt('permissions-architect-build', 'build')
    assertOutsideWorkflow('permissions-architect-build')

    addPermissionSession('permissions-reviewer-build')
    await assert.rejects(
      () => runRolePrompt('permissions-reviewer-build', 'reviewer'),
      /NO_.*RECEIPT/,
    )
    assert.equal(effect(sessionState.get('permissions-reviewer-build').permissions,
      'shell', `${runnerPrefix} review-context verify`), 'allow')
    await runRolePrompt('permissions-reviewer-build', 'build')
    assertOutsideWorkflow('permissions-reviewer-build')

    addPermissionSession('permissions-role-chain')
    await runRolePrompt('permissions-role-chain', 'developer')
    await assert.rejects(
      () => runRolePrompt('permissions-role-chain', 'reviewer'),
      /NO_.*RECEIPT/,
    )
    assert.equal(effect(sessionState.get('permissions-role-chain').permissions,
      'edit', 'src/app.py'), 'deny', 'Reviewer scope must replace Developer edit allow')
    await runRolePrompt('permissions-role-chain', 'developer')
    assert.equal(effect(sessionState.get('permissions-role-chain').permissions,
      'edit', 'src/app.py'), 'allow')
    await runRolePrompt('permissions-role-chain', 'build')
    assertOutsideWorkflow('permissions-role-chain')

    addPermissionSession('permissions-idempotent')
    await runRolePrompt('permissions-idempotent', 'developer')
    const updatesAfterFirstSwitch = sessionUpdates.filter(
      update => update.sessionID === 'permissions-idempotent').length
    const firstDeveloperScope = structuredClone(sessionState.get('permissions-idempotent').permissions)
    await runRolePrompt('permissions-idempotent', 'developer')
    assert.deepEqual(sessionState.get('permissions-idempotent').permissions, firstDeveloperScope)
    assert.equal(sessionUpdates.filter(
      update => update.sessionID === 'permissions-idempotent').length, updatesAfterFirstSwitch,
    'Repeated switch to the same role must not rewrite or duplicate its Kit scope')
    assert.equal(firstDeveloperScope.filter(rule =>
      rule.action === 'edit' && rule.resource === '*' && rule.effect === 'allow').length, 1)
    await runRolePrompt('permissions-idempotent', 'build')
    assertOutsideWorkflow('permissions-idempotent')
    assert.ok(calls.at(-1)[1].includes('Не запускай через shell ls/find/cat/sed/grep'))
    assert.ok(!calls.at(-1)[1].includes('python -m pytest -q <test-path>'),
      'Implement must use the Runner-selected project toolchain')
    assert.ok(calls.at(-1)[1].includes('После отказа write/edit остановись'))
    assert.equal(calls.at(-1)[1].split(implementMarker).length - 1, 1)
    assert.ok(calls.at(-1)[1].includes('BASELINE:'))
    assert.ok(calls.at(-1)[1].includes('--scope feature'))
    assert.ok(calls.at(-1)[1].includes('--scope regression'))
    assert.ok(calls.at(-1)[1].includes('не может изменить approval'))
    const beforeReview = calls.length
    const reviewMarker = 'REVIEW_MARKER_5bd4'
    await commands.get('review').execute({sessionID:'s1',prompt:{text:reviewMarker},delivery:{}})
    const reviewCalls = calls.slice(beforeReview)
    const independentPrompt = reviewCalls.find(call => call[0] === 'prompt')
    assert.ok(independentPrompt, 'review must asynchronously dispatch Reviewer')
    assert.equal(independentPrompt[1].split(reviewMarker).length - 1, 1)
    const sourceMatch = independentPrompt[1].match(/Review ID: ([0-9a-f]{32})/)
    assert.ok(sourceMatch)
    const sourceReviewID = sourceMatch[1]
    let registry = JSON.parse(fs.readFileSync(path.join(temp, 'state', 'projects',
      require('node:crypto').createHash('sha256').update(fs.realpathSync(root)).digest('hex').slice(0, 20),
      'tasks.json'), 'utf8'))
    assert.equal(registry.tasks['task-a'].completed_at, null,
      'asynchronous VERIFY Reviewer dispatch must not complete the task')
    child.execFileSync('python3', [runnerShim, '--project', root, 'review-submit', sourceReviewID, 'pass', 'none'])

    const beforeFinalize = calls.length
    const finalizeMarker = 'FINALIZE_MARKER_82e1'
    await commands.get('finalize').execute({sessionID:'s1',prompt:{text:finalizeMarker},delivery:{}})
    const finalizeCalls = calls.slice(beforeFinalize)
    const reviewerPrompt = finalizeCalls.find(call => call[0] === 'prompt')
    assert.ok(reviewerPrompt, 'finalize must dispatch Reviewer')
    assert.equal(reviewerPrompt[1].split(finalizeMarker).length - 1, 1)
    const reviewMatch = reviewerPrompt[1].match(/Review ID: ([0-9a-f]{32})/)
    assert.ok(reviewMatch)
    const reviewID = reviewMatch[1]
    assert.ok(reviewerPrompt[1].includes('Режим: reuse-independent-review'))
    assert.ok(reviewerPrompt[1].includes(`сохранённый независимый review ${sourceReviewID}`))
    assert.ok(reviewerPrompt[1].includes('Не повторяй полное чтение'))
    registry = JSON.parse(fs.readFileSync(path.join(temp, 'state', 'projects',
      require('node:crypto').createHash('sha256').update(fs.realpathSync(root)).digest('hex').slice(0, 20),
      'tasks.json'), 'utf8'))
    assert.equal(registry.tasks['task-a'].completed_at, null,
      'asynchronous Reviewer dispatch must not complete the task')
    assert.ok(finalizeCalls.at(-1)[1].description.includes('/accept'))
    const beforeDuplicateFinalize = calls.length
    await commands.get('finalize').execute({sessionID:'s1',prompt:{text:'Retry'},delivery:{}})
    const duplicateFinalize = calls.slice(beforeDuplicateFinalize)
    assert.equal(duplicateFinalize.filter(call => call[0] === 'prompt').length, 0,
      'repeated finalize must not duplicate an active Reviewer')
    assert.ok(duplicateFinalize.at(-1)[1].description.includes('suppressed'))
    child.execFileSync('python3', [runnerShim, '--project', root, 'review-submit', reviewID, 'pass', 'none'])
    await commands.get('accept').execute({sessionID:'s1',prompt:{text:''}})
    registry = JSON.parse(fs.readFileSync(path.join(temp, 'state', 'projects',
      require('node:crypto').createHash('sha256').update(fs.realpathSync(root)).digest('hex').slice(0, 20),
      'tasks.json'), 'utf8'))
    assert.ok(registry.tasks['task-a'].completed_at)
    assert.equal(registry.tasks['task-a'].acceptance.review_id, reviewID)
    const completedAt = registry.tasks['task-a'].completed_at
    await commands.get('accept').execute({sessionID:'s1',prompt:{text:reviewID}})
    registry = JSON.parse(fs.readFileSync(path.join(temp, 'state', 'projects',
      require('node:crypto').createHash('sha256').update(fs.realpathSync(root)).digest('hex').slice(0, 20),
      'tasks.json'), 'utf8'))
    assert.equal(registry.tasks['task-a'].completed_at, completedAt,
      'repeated acceptance must be idempotent')
    assert.ok(!input.includes('await guard(project, "complete")'))
    await commands.get('task').execute({sessionID:'s1',prompt:{text:'new task-b'}})
    assert.equal(calls.at(-1)[0], 'synthetic')
    assert.equal(calls.at(-1)[1].resume, false)
    await assert.rejects(() => commands.get('implement').execute({sessionID:'s1',prompt:{text:'x'},delivery:{}}), /NO_APPROVAL/)
    // Explicit git repo resolution prevents accidentally targeting a different checkout.
    assert.ok(!input.includes('opencode-agentic-lab'))
    assert.ok(input.includes('JSON.stringify(review, null, 2)'))
    assert.ok(input.includes('Не запускай через shell ls/find/cat/sed/grep'))
  } finally {
    if (oldStateDir === undefined) delete process.env.AGENTIC_STATE_DIR
    else process.env.AGENTIC_STATE_DIR = oldStateDir
    if (oldRunner === undefined) delete process.env.AGENTIC_RUNNER
    else process.env.AGENTIC_RUNNER = oldRunner
    fs.rmSync(temp, {recursive:true, force:true})
  }
  console.log('PASS plugin: async review, explicit acceptance, permissions, guards and project roots')
}
main().catch(err => { console.error(err); process.exitCode = 1 })
