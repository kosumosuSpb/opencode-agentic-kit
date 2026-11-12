const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const child = require('node:child_process')
const { pathToFileURL } = require('node:url')

async function main() {
  const base = path.resolve(__dirname, '..')
  const input = fs.readFileSync(path.join(base, 'plugin/index.ts'), 'utf8')
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
  process.env.AGENTIC_STATE_DIR = path.join(temp, 'state')
  process.env.AGENTIC_RUNNER = path.join(base, 'runner/agentic_gate.py')
  try {
    const pluginPackage = path.join(temp, 'node_modules/@opencode/plugin')
    fs.mkdirSync(pluginPackage, {recursive: true})
    fs.writeFileSync(path.join(pluginPackage, 'package.json'), JSON.stringify({type: 'module', exports: './index.js'}))
    fs.writeFileSync(path.join(pluginPackage, 'index.js'), 'export const Plugin = { define: value => value }\n')
    const pluginCopy = path.join(temp, 'index.ts')
    fs.writeFileSync(pluginCopy, input)
    const { default: definition } = await import(pathToFileURL(pluginCopy).href)
    assert.equal(definition.id, 'agentic.workflow.preview')
    const commands = new Map()
    const calls = []
    let contextHook
    let permissionHook
    const sessionState = new Map([
      ['s1', {id:'s1', directory:path.join(root, 'nested'), agent:'build', permissions:[]}],
      ['s2', {id:'s2', directory:secondRoot, agent:'build', permissions:[]}],
    ])
    const session = {
      async get({ sessionID }) { return sessionID === 's3' ? {id: sessionID} : sessionState.get(sessionID) },
      async hook(name, fn) { assert.equal(name, 'context'); contextHook = fn },
      async update({sessionID, permissions}) { sessionState.get(sessionID).permissions = permissions },
      async switchAgent(options) { sessionState.get(options.sessionID).agent = options.agent; calls.push(['agent', options.agent]) },
      async switchModel(options) { calls.push(['model', options.model]) },
      async prompt(options) { calls.push(['prompt', options.text]) },
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
      'git status --short > status.txt',
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
      ['git status > status.txt'],
      ['git status | wc -l'],
      ['bash', 'git status'],
      ['echo $(git status)'],
      ['git status --unknown'],
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
    assert.equal(commands.size, 6)
    await commands.get('task').execute({sessionID:'s1',prompt:{text:'new task-a'}})
    assert.ok(fs.statSync(path.join(root, '.agentic/tasks/task-a/drafts')).isDirectory(),
      'task-new must create the draft directory')
    const beforeDesign = calls.length
    await commands.get('design').execute({ sessionID:'s1', prompt: { text: 'Add an endpoint' }, delivery: {} })
    assert.equal(calls[beforeDesign][1], 'architect')
    assert.equal(calls[beforeDesign + 1][1].id, 'gpt-5.6-sol')
    assert.ok(calls[beforeDesign + 2][1].includes(root))
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
    fs.writeFileSync(path.join(root, '.agentic/tasks/task-a/drafts/spec.md'), 'Spec')
    fs.writeFileSync(path.join(root, '.agentic/tasks/task-a/drafts/plan.md'), 'Plan')
    const beforeApproval = calls.length
    await commands.get('approve').execute({sessionID:'s1'})
    assert.equal(calls.length, beforeApproval + 1, 'Approval must only add one synthetic message')
    assert.equal(calls.at(-1)[0], 'synthetic')
    assert.equal(calls.at(-1)[1].resume, false, 'Approval must not resume the model')
    const beforeImplement = calls.length
    await commands.get('implement').execute({sessionID:'s1',prompt:{text:'Implement approved work'},delivery:{}})
    assert.equal(calls[beforeImplement][0], 'agent')
    assert.equal(calls[beforeImplement][1], 'developer')
    assert.equal(calls.at(-1)[0], 'prompt', 'Explicit implement must prompt Developer')
    const afterImplement = sessionState.get('s1').permissions
    const activeDraftEffect = [...inherited, ...afterImplement].filter(rule => wildcard(rule.action, 'edit') && wildcard(rule.resource, '.agentic/tasks/task-a/drafts/spec.md')).at(-1)?.effect
    assert.equal(activeDraftEffect, 'deny', 'Leaving Architect must revoke draft editing')
    await commands.get('task').execute({sessionID:'s1',prompt:{text:'new task-b'}})
    assert.equal(calls.at(-1)[0], 'synthetic')
    assert.equal(calls.at(-1)[1].resume, false)
    await assert.rejects(() => commands.get('implement').execute({sessionID:'s1',prompt:{text:'x'},delivery:{}}), /NO_APPROVAL/)
    // Explicit git repo resolution prevents accidentally targeting a different checkout.
    assert.ok(!input.includes('opencode-agentic-lab'))
  } finally {
    if (oldStateDir === undefined) delete process.env.AGENTIC_STATE_DIR
    else process.env.AGENTIC_STATE_DIR = oldStateDir
    if (oldRunner === undefined) delete process.env.AGENTIC_RUNNER
    else process.env.AGENTIC_RUNNER = oldRunner
    fs.rmSync(temp, {recursive:true, force:true})
  }
  console.log('PASS plugin: approval pause, explicit implement, permission hook, guards and project roots')
}
main().catch(err => { console.error(err); process.exitCode = 1 })
