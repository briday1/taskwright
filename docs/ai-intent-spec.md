# Taskunity Intent Contract

This file defines the agreed, model-resolved intent schema for Taskunity AI interactions.

## Goal
- Resolve user intent across multi-turn conversations using structured output.
- Avoid brittle phrase matching for apply/update actions.
- Provide deterministic app behavior from an explicit contract.

## Required JSON Contract
Return strict JSON only:

```json
{
  "intent": {
    "kind": "update_checklist|create_tasks|save_note|file_edit|clarify|advice",
    "confidence": 0.0,
    "mode": "add|replace"
  },
  "resolved_checklist_items": ["..."],
  "resolved_tasks": [
    {"title": "...", "summary": "...", "priority": "low|normal|high|critical"}
  ],
  "resolved_note": "",
  "needs_clarification": false,
  "clarification_question": ""
}
```

## Resolution Rules
- For deictic follow-ups (those/them/that/it/yes/do it), resolve references from recent conversation.
- Prefer the most complete prior proposal over clarification/option prompts.
- Exclude option labels, boilerplate, and questions from resolved checklists.
- In task context:
  - `update_checklist` should return concrete checklist items.
  - Set `mode=replace` when user asks rewrite/new checklist.
- In milestone context:
  - `create_tasks` should return task objects.

## Safety Rules
- Never fabricate IDs or fields outside Taskunity schema.
- If confidence is low, set `needs_clarification=true` with one concise question.
- Keep checklist items concise, actionable, and implementation-oriented.
