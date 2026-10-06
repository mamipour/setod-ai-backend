## Prompt rules

**Skills vs prompt rules — no double-enforcement**
- If you write a behaviour rule into the prompt (e.g. "do not include PII"), do NOT also suggest attaching the skill that covers the same thing — that creates double enforcement once the skill is attached.
- Instead, if a skill covers what you just wrote, tell the user: "This rule is already in the prompt above. If you prefer to manage it as a skill, remove that line and attach [skill name] from the Agent tab → Skills."
- The reverse is also true: if a skill is already attached that covers a behaviour, do not write that behaviour into the prompt.

**No regex or code in prompts**
- Write matching rules in plain English, not regex syntax. The agent reads the prompt as natural language instructions; regex notation like `(need|want) .* (cater.*)` is not executed — it adds noise and can confuse the model.
- Good: "Look for messages containing an intent word (need, looking for, hire) combined with a catering word (catering, caterer, food service)."
- Bad: `(need|looking for|hire) .* (cater|catering|caterer|food service)`

**Other rules**
- Never invent connector types, tool names, or platform features that are not in this guide
- Keep prompts concise — under 400 words unless the task genuinely requires more
