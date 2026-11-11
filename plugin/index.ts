import { Plugin } from "@opencode/plugin"
import { execFile } from "node:child_process"
import { promisify } from "node:util"
import { homedir } from "node:os"
import { join } from "node:path"

// v1.2-preview: portable workflow, preserving v1.1's single-task approval protocol.
const exec = promisify(execFile)
const dataHome = process.env.XDG_DATA_HOME || join(homedir(), ".local", "share")
const RUNNER = process.env.AGENTIC_RUNNER || join(dataHome, "opencode-agentic", "preview", "runner", "agentic_gate.py")

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
    // A manual agent switch also checks the relevant precondition.
    await ctx.session.hook("context", async (event: any) => {
      const role = typeof event.agent === "string" ? event.agent : event.agent?.id
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
          await ctx.session.switchAgent({ sessionID, agent: "architect" })
          await ctx.session.switchModel({ sessionID, model: configuredModel("architect") })
          await ctx.session.prompt({ ...prompt, sessionID, delivery,
            text: `Проект: ${project}. Проектирование задачи: ${prompt.text}\n\nСохрани черновики ТОЛЬКО в .agentic/drafts/spec.md и .agentic/drafts/plan.md. Укажи acceptance criteria и тест-план, статус DRAFT. Не меняй код. Не утверждай проект самостоятельно.` })
        },
      })
      editor.add({
        name: "approve",
        description: "Soft human approval of current single-task draft (preview)",
        execute: async ({ sessionID }) => {
          const project = await currentProject(ctx, sessionID)
          // Soft approval only: same-user processes can execute the Runner directly.
          const result = await guard(project, "approve", "--human-confirm")
          await ctx.session.synthetic({ sessionID, text: `Soft approval recorded (not OS-enforced). ${result}` })
        },
      })
      editor.add({
        name: "implement",
        description: "Developer: original ub-mode + TDD + fast gate",
        execute: async ({ sessionID, prompt, delivery }) => {
          const project = await currentProject(ctx, sessionID)
          await guard(project, "check-approval")
          await ctx.session.switchAgent({ sessionID, agent: "developer" })
          await ctx.session.switchModel({ sessionID, model: configuredModel("developer") })
          await ctx.session.prompt({ ...prompt, sessionID, delivery,
            text: `Реализуй утверждённую спецификацию через оригинальный ub-mode. Сначала загрузи skill, затем TDD RED→GREEN→REFACTOR и запуски реальных тестов. После каждого блока fast-gate. В конце: python3 ${shellQuote(RUNNER)} --project ${shellQuote(project)} gate fast. Задача: ${prompt.text}. Не меняй approved specs/plans. Не выполняй push/deploy.` })
        },
      })
      editor.add({
        name: "review",
        description: "Fresh verify gate before independent read-only review",
        execute: async ({ sessionID, prompt, delivery }) => {
          const project = await currentProject(ctx, sessionID)
          await guard(project, "gate", "verify")
          await ctx.session.switchAgent({ sessionID, agent: "reviewer" })
          await ctx.session.switchModel({ sessionID, model: configuredModel("reviewer") })
          await ctx.session.prompt({ ...prompt, sessionID, delivery,
            text: `Независимое ревью утверждённых критериев, безопасности и тестов. Verify Gate для ${project} только что прошёл. Приводи дефекты с путями и строками. Не изменяй код. Контекст: ${prompt.text}` })
        },
      })
      editor.add({
        name: "finalize",
        description: "Fresh full gate and final human acceptance recommendation",
        execute: async ({ sessionID, prompt, delivery }) => {
          const project = await currentProject(ctx, sessionID)
          await guard(project, "gate", "full")
          await ctx.session.switchAgent({ sessionID, agent: "reviewer" })
          await ctx.session.switchModel({ sessionID, model: configuredModel("reviewer") })
          await ctx.session.prompt({ ...prompt, sessionID, delivery,
            text: `FULL Quality Gate для ${project} только что прошёл. Составь финальное заключение, ограничения и соответствие критериям. Только человек принимает работу. Не выполняй push/deploy. Контекст: ${prompt.text}` })
        },
      })
    })
  },
})
