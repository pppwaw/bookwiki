version: v1
---
Agent: {agent_name}
Prompt: {prompt_name}@{prompt_version}
Output schema: {output_model}

Agent instructions:
{agent_instructions}

Input JSON:
```json
{input_json}
```

Draft JSON:
```json
{draft_json}
```

Use the draft as a structural starting point, but improve the content according to the agent instructions.
Return only the final JSON object.
