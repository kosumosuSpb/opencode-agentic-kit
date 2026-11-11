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
    const session = {
      async get({ sessionID }) { return sessionID === 's3' ? {id: sessionID} : { id: sessionID, directory: sessionID === 's2' ? secondRoot : path.join(root, 'nested') } },
      async hook(name, fn) { assert.equal(name, 'context'); contextHook = fn },
      async switchAgent(options) { calls.push(['agent', options.agent]) },
      async switchModel(options) { calls.push(['model', options.model]) },
      async prompt(options) { calls.push(['prompt', options.text]) },
      async synthetic(options) { calls.push(['synthetic', options.text]) },
    }
    const ctx = { session, command: { async transform(fn) { fn({ add(c) { commands.set(c.name, c) } }) } } }
    await definition.setup(ctx)
    assert.equal(commands.size, 5)
    await commands.get('design').execute({ sessionID:'s1', prompt: { text: 'Add an endpoint' }, delivery: {} })
    assert.equal(calls[0][1], 'architect')
    assert.equal(calls[1][1].id, 'gpt-5.6-sol')
    assert.ok(calls[2][1].includes(root))
    await commands.get('design').execute({ sessionID:'s2', prompt: { text: 'Another project' }, delivery: {} })
    assert.ok(calls.at(-1)[1].includes(secondRoot))
    await assert.rejects(() => commands.get('design').execute({sessionID:'s3', prompt:{text:'x'},delivery:{}}), /PROJECT_UNRESOLVED/)
    const n = calls.length
    await assert.rejects(() => commands.get('implement').execute({sessionID:'s1',prompt:{text:'x'},delivery:{}}), /NO_APPROVAL/)
    assert.equal(calls.length, n, 'Blocked implement must not switch agent')
    await assert.rejects(() => contextHook({agent:'reviewer',sessionID:'s1'}), /NO_.*RECEIPT/)
    // Explicit git repo resolution prevents accidentally targeting a different checkout.
    assert.ok(!input.includes('opencode-agentic-lab'))
  } finally {
    if (oldStateDir === undefined) delete process.env.AGENTIC_STATE_DIR
    else process.env.AGENTIC_STATE_DIR = oldStateDir
    if (oldRunner === undefined) delete process.env.AGENTIC_RUNNER
    else process.env.AGENTIC_RUNNER = oldRunner
    fs.rmSync(temp, {recursive:true, force:true})
  }
  console.log('PASS plugin: 5 commands, model selection, nested project root, blocked implement/reviewer')
}
main().catch(err => { console.error(err); process.exitCode = 1 })
