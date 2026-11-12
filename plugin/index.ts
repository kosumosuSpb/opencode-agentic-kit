import { Plugin } from "@opencode/plugin"
import { execFile } from "node:child_process"
import { promisify } from "node:util"
import { homedir } from "node:os"
import { join } from "node:path"

// v1.2: sequential task workflow with OpenCode-scanned shell resources.
const exec = promisify(execFile)
const dataHome = process.env.XDG_DATA_HOME || join(homedir(), ".local", "share")
const RUNNER = process.env.AGENTIC_RUNNER || join(dataHome, "opencode-agentic", "preview", "runner", "agentic_gate.py")
const WORKFLOW_ROLES = new Set(["architect", "developer", "reviewer"])
const DRAFT_DENY_RULES = [
  { action: "edit", resource: ".agentic/tasks/*/drafts/**", effect: "deny" },
  { action: "edit", resource: ".agentic/drafts/*", effect: "deny" },
]
const SAFE_SHELL = new Set([
  "pwd",
  "git status",
  "git status --short",
  "git status --porcelain",
  "git diff --stat",
  "git diff --no-ext-diff",
  "git diff --no-ext-diff --stat",
  "git diff --no-ext-diff --cached",
  "git diff --no-ext-diff --cached --stat",
  "git --no-pager log --oneline -n 20",
  "git log --oneline -5",
  "git branch --show-current",
  "git rev-parse --show-toplevel",
])

function isSafeShellResource(resource: string): boolean {
  // OpenCode 2.2.7 scans the shell AST and supplies one resource per command.
  // Deliberately do not split or normalize raw shell here: opaque syntax, wrappers,
  // substitutions, pipelines and redirections must remain ask.
  return SAFE_SHELL.has(resource)
}

function isRemoteWrite(resource: string): boolean {
  // Removing quote/backslash concatenation catches forms such as g""it and g\it.
  // False positives only make a write-like command safer by denying it.
  const normalized = resource.replace(/["'\\]/g, "").toLowerCase()
  return /(?:^|[^a-z0-9_])(?:[^\s;|&()]*\/)?git(?:\s|$)[\s\S]*\bpush\b/.test(normalized)
    || /(?:^|[;|&()]|\s)(?:uv\s+publish|twine\s+upload|python\S*\s+-m\s+twine\s+upload|npm\s+publish|pnpm\s+publish|cargo\s+publish)(?:\s|$)/.test(normalized)
}

function configuredModel(role: "architect" | "developer" | "reviewer") {
  const defaults = {
    architect: "openai/gpt-5.6-sol",
    developer: "opencode/deepseek-v4.1-flash-free",
    reviewer: "openai/gpt-5.6-sol",
  }
  const name = `AGENTIC_${role.toUpperCase()}_MODEL`
  const raw = (process.env[name] || defaults[role]).trim()
  const slash = raw.indexOf("/")
  if (slash < 1 || slash === raw.length - 1) throw new Error(`Invalid ${name}: expected provider/model`)
  return { providerID: raw.slice(0, slash), id: raw.slice(slash + 1) }
}

function shellQuote(s: string): string {
  return "'" + s.replaceAll("'", "'\"'\"'") + "'"
}

function roleID(agent: any): string | undefined {
  return typeof agent === "string" ? agent : agent?.id
}

async function setDraftPermissionScope(ctx: any, sessionID: string, task?: any): Promise<void> {
  const session = await ctx.session.get({ sessionID })
  const current = Array.isArray(session?.permissions) ? session.permissions : []
  const rules = [
    ...DRAFT_DENY_RULES,
    ...(task ? [
      { action: "edit", resource: task.draft_spec, effect: "allow" },
      { action: "edit", resource: task.draft_plan, effect: "allow" },
    ] : []),
  ]
  const tail = current.slice(-rules.length)
  if (JSON.stringify(tail) === JSON.stringify(rules)) return
  await ctx.session.update({ sessionID, permissions: [...current, ...rules] })
}

async function currentProject(ctx: any, sessionID: string): Promise<string> {
  const session = await ctx.session.get({ sessionID })
  const directory = session?.directory ?? session?.location?.directory
  if (typeof directory !== "string" || !directory.startsWith("/")) {
    // Do not use ctx.location: global plugins can serve multiple sessions.
    throw new Error("PROJECT_UNRESOLVED: OpenCode session has no absolute directory")
  }
  try {
    const { stdout } = await exec("git", ["-C", directory, "rev-parse", "--show-toplevel"],
      { timeout: 10000, maxBuffer: 1024 * 1024 })
    const root = stdout.trim()
    if (!root.startsWith("/")) throw new Error("Invalid git root")
    return root
  } catch (error: any) {
    throw new Error(`NOT_GIT_REPOSITORY: ${String(error?.stderr || error?.message || error)}`)
  }
}

async function guard(project: string, ...args: string[]): Promise<string> {
  try {
    const { stdout } = await exec("python3", [RUNNER, "--project", project, ...args], {
      timeout: 1_800_000, maxBuffer: 4 * 1024 * 1024,
    })
    return stdout.trim()
  } catch (error: any) {
    throw new Error(`Agentic workflow blocked: ${String(error?.stderr || error?.message || error)}`)
  }
}

export default Plugin.define({
  id: "agentic.workflow.preview",
  async setup(ctx) {
    // Project configs can append later allow rules. Keep workflow Git/package writes guarded here too.
    await ctx.permission.hook("evaluate", (event: any) => {
      if (!WORKFLOW_ROLES.has(event.agent) || event.action !== "shell") return
      if (event.resources.some((resource: string) => isRemoteWrite(resource))) {
        event.effect = "deny"
        event.message = "Agentic workflow forbids push and package publication"
      } else if (event.effect === "deny") {
        return
      } else if (event.resources.length > 0 && event.resources.every(isSafeShellResource)) {
        event.effect = "allow"
        event.message = "OpenCode scanner confirmed an allowlisted read-only command chain"
      } else {
        event.effect = "ask"
        event.message = "This shell command requires a separate user decision"
      }
    })

    // A manual agent switch also checks the relevant precondition.
    await ctx.session.hook("context", async (event: any) => {
      const role = roleID(event.agent)
      if (role === "architect") {
        const project = await currentProject(ctx, event.sessionID)
        const task = JSON.parse(await guard(project, "task-ensure"))
        await setDraftPermissionScope(ctx, event.sessionID, task)
        return
      }
      await setDraftPermissionScope(ctx, event.sessionID)
      if (role !== "developer" && role !== "reviewer") return
      const project = await currentProject(ctx, event.sessionID)
      if (role === "developer") await guard(project, "check-approval")
      if (role === "reviewer") {
        try { await guard(project, "ready", "verify") }
        catch { await guard(project, "ready", "full") }
      }
    })

    await ctx.command.transform((editor) => {
      editor.add({
        name: "design",
        description: "Architect: design into unapproved draft files",
        execute: async ({ sessionID, prompt, delivery }) => {
          const project = await currentProject(ctx, sessionID)
          const task = JSON.parse(await guard(project, "task-ensure"))
          await setDraftPermissionScope(ctx, sessionID, task)
          await ctx.session.switchAgent({ sessionID, agent: "architect" })
          await ctx.session.switchModel({ sessionID, model: configuredModel("architect") })
          await ctx.session.prompt({ ...prompt, sessionID, delivery,
            text: `Проект: ${project}. Активная задача: ${task.task_id}. Проектирование задачи: ${prompt.text}\n\nСохрани черновики ТОЛЬКО в ${task.draft_spec} и ${task.draft_plan}. Укажи acceptance criteria и тест-план, статус DRAFT. Не меняй код. Не утверждай проект самостоятельно.` })
        },
      })
      editor.add({
        name: "task",
        description: "Task management: new <id>, use <id>, or status",
        execute: async ({ sessionID, prompt }) => {
          const project = await currentProject(ctx, sessionID)
          const words = String(prompt?.text || "status").trim().split(/\s+/)
          const operation = words[0] || "status"
          let result: string
          if (operation === "status" && words.length === 1) {
            result = await guard(project, "task-status")
          } else if (operation === "new" && words.length === 2) {
            result = await guard(project, "task-new", words[1])
          } else if (operation === "use" && words.length === 2) {
            result = await guard(project, "task-use", words[1])
          } else {
            throw new Error("TASK_USAGE: /task status | /task new <id> | /task use <id>")
          }
          if (operation === "new" || operation === "use") {
            const task = JSON.parse(await guard(project, "task-info"))
            const session = await ctx.session.get({ sessionID })
            await setDraftPermissionScope(ctx, sessionID,
              roleID(session?.agent) === "architect" && !task.completed_at ? task : undefined)
          }
          await ctx.session.synthetic({ sessionID, text: result, resume: false })
        },
      })
      editor.add({
        name: "approve",
        description: "Soft human approval of the active task draft",
        execute: async ({ sessionID }) => {
          const project = await currentProject(ctx, sessionID)
          // Soft approval only: same-user processes can execute the Runner directly.
          const result = await guard(project, "approve", "--human-confirm")
          await ctx.session.synthetic({ sessionID, text: `Soft approval recorded (not OS-enforced). ${result}`, resume: false })
        },
      })
      editor.add({
        name: "implement",
        description: "Developer: original ub-mode + TDD + fast gate",
        execute: async ({ sessionID, prompt, delivery }) => {
          const project = await currentProject(ctx, sessionID)
          await guard(project, "check-approval")
          await setDraftPermissionScope(ctx, sessionID)
          await ctx.session.switchAgent({ sessionID, agent: "developer" })
          await ctx.session.switchModel({ sessionID, model: configuredModel("developer") })
          await ctx.session.prompt({ ...prompt, sessionID, delivery,
            text: `Реализуй утверждённую спецификацию через оригинальный ub-mode. Сначала загрузи skill, затем TDD RED→GREEN→REFACTOR и запуски реальных тестов. После каждого блока fast-gate. В конце: python3 ${shellQuote(RUNNER)} --project ${shellQuote(project)} gate fast. Задача: ${prompt.text}. Не меняй approved specs/plans. Не создавай git commit и не выполняй push/deploy/publish.` })
        },
      })
      editor.add({
        name: "review",
        description: "Fresh verify gate before independent read-only review",
        execute: async ({ sessionID, prompt, delivery }) => {
          const project = await currentProject(ctx, sessionID)
          await guard(project, "gate", "verify")
          const readiness = await guard(project, "ready", "verify")
          await setDraftPermissionScope(ctx, sessionID)
          await ctx.session.switchAgent({ sessionID, agent: "reviewer" })
          await ctx.session.switchModel({ sessionID, model: configuredModel("reviewer") })
          await ctx.session.prompt({ ...prompt, sessionID, delivery,
            text: `Независимое ревью утверждённых критериев, безопасности и тестов. Runner ready verify для ${project}: ${readiness}. Receipt находится во внешнем state store; его отсутствие в .agentic не дефект. Gate не доказывает сам по себе TDD или каждый acceptance criterion. Приводи дефекты с путями и строками, явно разделяй доказанное и недоказанное. Не изменяй код и не создавай коммиты. Контекст: ${prompt.text}` })
        },
      })
      editor.add({
        name: "finalize",
        description: "Fresh full gate and final human acceptance recommendation",
        execute: async ({ sessionID, prompt, delivery }) => {
          const project = await currentProject(ctx, sessionID)
          await guard(project, "gate", "full")
          const readiness = await guard(project, "ready", "full")
          await setDraftPermissionScope(ctx, sessionID)
          await ctx.session.switchAgent({ sessionID, agent: "reviewer" })
          await ctx.session.switchModel({ sessionID, model: configuredModel("reviewer") })
          await ctx.session.prompt({ ...prompt, sessionID, delivery,
            text: `Runner ready full для ${project}: ${readiness}. Receipt находится во внешнем state store. Составь финальное заключение, отдельно укажи доказательства TDD, wheel и acceptance criteria, а также всё недоказанное. Только человек принимает работу. Не создавай коммиты и не выполняй push/deploy/publish. Контекст: ${prompt.text}` })
          await guard(project, "complete")
        },
      })
    })
  },
})
