## Code skills

A code skill is a Python function the workspace owner writes. Setod deploys it as its own Lambda and the agent calls it as `code_<tool_name>`, the same way it calls `send_email`.

The function is `main(input, context)`. `input` is the JSON object the model passes. Whatever `main` returns is JSON the model reads. Nothing else from the function — prints, files, environment values — reaches the model.

Use one when the agent is doing mechanical work badly: parsing a CSV or JSON download, date arithmetic, or calling an HTTP API that has no connector. Prefer a connector tool first, then a prompt skill, then a code skill.

`read_only=true` skills run during a dry run. Others return "Would run…" and do not execute, so a fetch-or-parse skill must be marked read-only or the dry run proves nothing. Network access is off unless `network_access=true`. Secrets are environment variables, write-only: the owner sets them in the UI and they are never shown again.

Deploy, wait until the status is `ready`, test with a sample input, then attach the skill to the agent.
