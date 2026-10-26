# Plugin Result Rendering

Data results use the existing `ToolResult.artifact` contract. The engine treats the payload as opaque, stores it with the conversation, and sends it as the SSE `data` field.

## Display Parameters

A plugin can declare these fields inside its artifact:

- `pack`: the owning plugin's directory name.
- `resultDisplay: 'card'`: render the structured result without a duplicate text summary.
- Omitted or unknown `resultDisplay`: retain the existing summary-plus-artifact behavior.

`summary` remains available for conversation history, tracing, and compatibility. The frontend hides it only when `resultDisplay` is `card` **and** the owning plugin has a renderer that accepts the result. Unknown plugins, unsupported payloads, errors, and clarification questions retain their normal fallback paths.

## Frontend Ownership

Place a default export in `frontend/src/plugins/<pack>/result.ts`:

```ts
export default {
  component: ResultCard,
  canRender: (message) => /* validate supported artifact shape */,
}
```

`resultRegistry.ts` discovers these modules by directory convention. It routes by the artifact's `pack`; it does not inspect business names or infer display policy from a plugin name. The plugin owns its component, field selection, business states, and validation. Shared components remain available for charts, evidence, and safe Markdown rendering.

The result component receives `artifact`, `summary`, manifest `labels`, `renderMarkdown`, `compact`, and `citedChunks`. The supplied Markdown renderer sanitizes HTML. Critical business values and references belong in the artifact, not only in transient formatting fields, so history restores the same result.

## Model Output

A plugin owns its prompt, structured business-output schema, and validation. Use the platform's existing `chat_json` capability for model-generated JSON, then validate business types, enums, required fields, and source references in the plugin. Invalid output is an error, not a successful empty card. Structured upstream API records do not need another model call.

Metrology enables card-only output for its answers, progress records, and business notices. Existing knowledge-graph and BI rendering remains compatible; each plugin may adopt this explicit display contract independently.
