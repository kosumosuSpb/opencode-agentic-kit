import { Plugin } from "@opencode/plugin/tui"

export default Plugin.define({
  id: "agentic.workflow.preview.tui",
  setup(ctx) {
    const shown = new Set<string>()
    const stopInbox = ctx.data.on("session.inbox.enqueued", (event: any) => {
      const item = event?.data?.item ?? event?.data?.inbox ?? event?.data
      if (item?.type !== "synthetic" || item?.payload?.metadata?.agenticKitTaskResult !== true) return
      if (typeof item.id === "string" && shown.has(item.id)) return
      if (typeof item.id === "string") shown.add(item.id)
      const message = String(item.payload.description || item.payload.text || "Task updated")
      ctx.ui.toast.show({ title: "Agentic Kit", message, variant: "success", duration: 8000 })
    })

    const workflowCommands = [
      ["design", "Draft specification and plan"],
      ["task", "Manage Agentic Kit tasks"],
      ["approve", "Approve current drafts"],
      ["implement", "Implement the approved task"],
      ["review", "Run independent review"],
      ["finalize", "Finalize the reviewed task"],
      ["accept", "Record human acceptance"],
    ] as const
    const stopCommands = ctx.keymap.layer(() => ({
      mode: "global",
      priority: 20,
      commands: workflowCommands.map(([name, title]) => ({
        id: `agentic.${name}`,
        title,
        group: "Agentic Kit",
        palette: true,
        run: async (input?: string) => {
          try {
            const route = ctx.ui.router.current()
            let sessionID: string
            if (route?.type === "session" && typeof route.sessionID === "string") {
              sessionID = route.sessionID
            } else if (route?.type === "home") {
              const session = await ctx.client.session.create({
                location: ctx.location ?? ctx.data.location.default(),
              })
              if (!session || typeof session.id !== "string" || session.id.trim() === "") {
                throw new Error("Unable to create an OpenCode session")
              }
              sessionID = session.id
              ctx.ui.router.navigate({ type: "session", sessionID })
            } else {
              ctx.ui.toast.show({
                title: "Agentic Kit",
                message: `/${name} requires an active session`,
                variant: "error",
                duration: 8000,
              })
              return
            }
            const result = await ctx.client.session.command({
              sessionID,
              directory: ctx.location?.directory,
              name,
              text: input ?? "",
            })
            if (result?.error) throw new Error(String(result.error?.message ?? result.error))
          } catch (error) {
            ctx.ui.toast.show({
              title: "Agentic Kit",
              message: `/${name} failed: ${error instanceof Error ? error.message : String(error)}`,
              variant: "error",
              duration: 8000,
            })
          }
        },
      })),
    }))

    return () => {
      stopCommands()
      stopInbox()
    }
  },
})
