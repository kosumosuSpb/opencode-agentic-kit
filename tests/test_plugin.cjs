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
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'agentic-preview-plugin-'))
  const root = path.join(temp, 'repo')
  fs.mkdirSync(root)
  fs.mkdirSync(path.join(root, 'nested'))
  child.execFileSync('git', ['init', '-q', root])
  const secondRoot = path.join(temp, 'second-repo')
  fs.mkdirSync(secondRoot)
  child.execFileSync('git', ['init', '-q', secondRoot])
  const oldStateDir = process.env.AGENTIC_STATE_DIR
  const oldRunner = process.env.AGENTIC_RUNNER
  const realRunner = path.join(base, 'runner/agentic_gate.py')
  const runnerShim = path.join(temp, 'runner-shim.py')
  fs.writeFileSync(runnerShim, [
    'from pathlib import Path',
    'import importlib.util, json, sys',
    `REAL = Path(${JSON.stringify(realRunner)})`,
    "spec = importlib.util.spec_from_file_location('real_agentic_gate', REAL)",
    'module = importlib.util.module_from_spec(spec)',
    'spec.loader.exec_module(module)',
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
    const cleanup = tuiDefinition.setup({
      data: { on(name, fn) { assert.equal(name, 'session.inbox.enqueued'); inboxListener = fn; return () => {} } },
      keymap: { layer(factory) { tuiLayer = factory(); return () => {} } },
      client: { session: { async command(options) {
        // Enforce the actual OpenCode 2.2.x command RPC contract.
        for (const key of ['sessionID', 'name', 'text']) {
          if (!Object.hasOwn(options, key)) throw new Error(`Missing key at [\"${key}\"]`)
        }
        tuiCalls.push(options)
      } } },
      location: { directory: root },
      ui: {
        router: { current() { return {type:'session', sessionID:'s1'} } },
        toast: { show(options) { toasts.push(options) } },
      },
    })
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
    assert.deepEqual(tuiLayer.commands.map(command => command.slash.name),
      ['design', 'task', 'approve', 'implement', 'review', 'finalize', 'accept'])
    assert.ok(tuiLayer.commands.every(command => command.slash.arguments === true))
    await tuiLayer.commands.find(command => command.slash.name === 'task').run('status')
    await tuiLayer.commands.find(command => command.slash.name === 'implement').run('UNIQUE_TUI_MARKER')
    assert.deepEqual(tuiCalls, [
      { sessionID: 's1', directory: root, name: 'task', text: 'status' },
      { sessionID: 's1', directory: root, name: 'implement', text: 'UNIQUE_TUI_MARKER' },
    ])
    const commands = new Map()
    const calls = []
    let contextHook
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
      async hook(name, fn) { assert.equal(name, 'context'); contextHook = fn },
      async update({sessionID, permissions}) { sessionState.get(sessionID).permissions = permissions },
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
      ['git diff --stat'],
      ['git log --oneline -5'],
      ['git status', 'git diff --stat'],
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
    const scoped = sessionState.get('s1').permissions
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
    await assert.rejects(() => contextHook({agent:'reviewer',sessionID:'s1'}), /NO_.*RECEIPT/)
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
    assert.ok(finalizeCalls.at(-1)[1].description.includes(`/accept ${reviewID}`))
    const beforeDuplicateFinalize = calls.length
    await commands.get('finalize').execute({sessionID:'s1',prompt:{text:'Retry'},delivery:{}})
    const duplicateFinalize = calls.slice(beforeDuplicateFinalize)
    assert.equal(duplicateFinalize.filter(call => call[0] === 'prompt').length, 0,
      'repeated finalize must not duplicate an active Reviewer')
    assert.ok(duplicateFinalize.at(-1)[1].description.includes('suppressed'))
    child.execFileSync('python3', [runnerShim, '--project', root, 'review-submit', reviewID, 'pass', 'none'])
    await commands.get('accept').execute({sessionID:'s1',prompt:{text:reviewID}})
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
