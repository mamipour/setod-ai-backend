## Diagnosing a run

When the user asks why the agent did or did not do something on a run — a missing link, a message that never arrived, the wrong items picked — read the run trace first (`read_run_trace` in the copilot, `setod_read_run` over MCP). The run list in your context shows only status and token count; the trace shows what actually happened.

Read the trace before forming a theory. It tells you which tool the agent called, the exact arguments it passed (an outbound message body appears here, so you can see precisely what was sent), and what each tool returned — so you can tell a prompt problem apart from a tool returning data that did not contain what the prompt asked for.

Never tell the user you cannot see the run's internal trace or the content of a message it sent. You can: read it.
