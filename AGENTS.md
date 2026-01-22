# Lark Enhance Plugin Architecture

## 1. Project Overview

`lark_enhance` is an AstrBot plugin designed to improve the integration with the Lark (Feishu) platform. It enriches the conversational context by resolving user information, handling quoted messages, maintaining group chat history, and providing platform-specific capabilities like emoji reactions.

### Core Architecture

The plugin operates as a `Star` extension within AstrBot, utilizing the event-driven architecture to intercept and modify message flows.

*   **Event Interception**: Listens for `LARK` platform events via `on_message` to:
    *   Preprocess message content (e.g., resolving OpenIDs to nicknames).
    *   **Local History Recording**: Automatically records group chat messages into an in-memory sliding window (`self.group_history`) to provide context for future interactions.
    *   **Content Cleaning**: Implements robust cleaning logic (`_clean_content`) to prevent JSON-formatted strings or internal message representations from polluting the conversation history, ensuring the LLM sees clean text.
*   **Prompt Injection**: Uses `on_llm_request` to inject additional context into the LLM prompt:
    *   **Quoted Content**: If the user replies to a message, the quoted content is fetched and injected.
    *   **Group Context**: Recent group chat history (from local memory) is injected to help the LLM understand the ongoing conversation, even if it wasn't directly mentioned.
    *   **Context Cleaning**: Cleans up `tool_calls` and `tool_result` in the conversation history to prevent LLM errors (e.g., Gemini's `thought_signature` issue) and ensures tool execution results are presented in a friendly format.
*   **Tool Registration**: Exposes `lark_emoji_reply` as a tool for the LLM to interact with Lark's reaction system.
    *   **Silent Execution**: The tool returns `None` upon success, preventing the LLM from generating redundant follow-up text (e.g., "I've added the emoji") while still recording the action in the history.
*   **Self-Message Recording**: Listens for `after_message_sent` events to record the bot's own responses into the group history, ensuring a bidirectional context.
*   **API Integration**: Directly accesses the `lark_oapi` client instance injected into `AstrMessageEvent` to perform API calls (fetching user info, message details).

## 2. Build & Commands

Since this is a plugin, it runs within the AstrBot environment.

### Development
*   **Location**: `data/plugins/lark_enhance/`
*   **Dependencies**: Defined in `requirements.txt`. Depends on `lark-oapi`.
*   **Reload**: Restart AstrBot to apply changes: `uv run main.py`.

### Testing
*   **Manual Testing**: 
    *   Send messages on Lark to trigger features (quote a message, @mention a user).
    *   Test group chat context by discussing a topic and then asking the bot about it.
    *   Test emoji reaction tool.
*   **Logs**: Check console output for `[lark_enhance]` prefix to verify behavior.

## 3. Code Style

Follows the AstrBot core project standards.

*   **Formatting**: PEP 8.
*   **Type Hints**: Strictly used for all methods.
*   **Async/Await**: Used for all I/O operations (Lark API calls).
*   **Logging**: Uses `astrbot.core.logger` with the `[lark_enhance]` prefix.

## 4. Testing

*   **Unit Tests**: Should mock `AstrMessageEvent` and `lark_oapi` client responses.
*   **Integration**: Requires a running Lark bot instance and correct event dispatching.
*   **Error Handling**: All API calls are wrapped in try/except blocks to prevent crashing the main bot process.

## 5. Security

*   **Data Privacy**:
    *   Caches user nicknames in memory (`self.user_cache`) to reduce API calls.
    *   **Group History**: Stores recent group messages in memory (`self.group_history`). This data is **ephemeral** (lost on restart) and is strictly used for context injection. It is NOT persisted to disk.
*   **Permissions**:
    *   Checks `event.get_platform_name() == "lark"` before execution.
    *   Handles `41050` (permission denied) gracefully when fetching user info.
*   **Token Access**: Uses the authenticated client provided by the event object; does not handle tokens directly.

## 6. Configuration

Configuration is managed via `_conf_schema.json` and accessible through the AstrBot dashboard or config files.

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `enable_real_name` | bool | `true` | Resolve OpenIDs to real nicknames. |
| `enable_quoted_content` | bool | `true` | Fetch and inject quoted message content. |
| `enable_group_info` | bool | `true` | Inject group name/desc (planned). |
| `enable_context_cleaner` | bool | `true` | Clean `tool_calls` to fix Gemini errors. |
| `enable_streaming_card` | bool | `false` | Use card messages for streaming (planned). |
| `history_inject_count` | int | `20` | Number of recent group messages to record locally and inject into context. Set to 0 to disable. |
