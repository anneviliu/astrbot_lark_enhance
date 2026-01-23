# Lark Enhance Plugin Architecture

## 1. Project Overview

`lark_enhance` is an AstrBot plugin designed to deepen the integration with the Lark (Feishu) IM platform. It enhances the conversational experience by providing context that is otherwise missing from standard message events, such as real user nicknames, quoted message content, recent group chat history, and streaming card output.

### High-Level Architecture

The plugin functions as a **Star** (AstrBot plugin) and hooks into the event lifecycle:

1.  **Message Pre-processing (`on_message`)**:
    *   **User Resolution**: Intercepts incoming messages to resolve OpenIDs (e.g., in `@mentions` or sender fields) to real nicknames using the Lark API. This ensures the LLM knows who is talking.
    *   **History Recording**: Maintains a sliding window of recent group messages with **persistent storage** (`data/group_history.json`). This allows the bot to understand context across restarts.
    *   **Content Cleaning**: Sanitizes message content to remove internal JSON/Python object representations, ensuring the LLM receives clean text.
    *   **Quote Handling**: Detects if a user is replying to another message, fetches the original quoted content, and attaches it to the event context.
    *   **Group Info**: Fetches group name and description for context injection.

2.  **LLM Request Intervention (`on_llm_request`)**:
    *   **Context Injection**: Before sending a request to the LLM, the plugin injects:
        *   Group information (name, description).
        *   The content of the message being quoted (if any).
        *   The recent group chat history (from persistent storage).
    *   **Safety Cleaning**: Scrubs `tool_calls` and intermediate tool outputs from the conversation history to prevent compatibility issues with certain LLM providers (e.g., Gemini).

3.  **Result Decoration (`on_decorating_result`)**:
    *   **Content Cleaning**: Cleans LLM output that may contain serialized format (e.g., `[{'type': 'text', 'text': '...'}]`).
    *   **Mention Conversion**: Converts `@Name` patterns in LLM responses to actual Lark `At` components, enabling real @ mentions in group chats.

4.  **Streaming Card Output**:
    *   **Typewriter Effect**: When enabled, uses Lark message cards with real-time updates (via Patch API) to display LLM output progressively, creating a typewriter effect.
    *   **Implementation**: Uses monkey-patching of `LarkMessageEvent.send_streaming` to intercept streaming output.

5.  **Tools**:
    *   **`lark_emoji_reply`**: Exposes a tool allowing the LLM to react to messages with emojis (e.g., THUMBSUP, HEART). Limited to **one reaction per message** to prevent spam.

## 2. Build & Commands

This project is an AstrBot plugin and relies on the AstrBot runtime.

*   **Runtime**: Python 3.10+
*   **Dependencies**: Listed in `requirements.txt` (primary dependency: `lark-oapi`).
*   **Installation**:
    1.  Place the `lark_enhance` directory into `data/plugins/`.
    2.  Install dependencies: `pip install -r requirements.txt`.
    3.  Restart AstrBot.
*   **Update**: `git pull` in the plugin directory and restart AstrBot.

## 3. Code Style

*   **Standard**: PEP 8.
*   **Type Hints**: Fully typed code structure (`main.py`).
*   **Async/Await**: Extensive use of `asyncio` for non-blocking Lark API calls.
*   **Logging**: Uses `astrbot.core.logger` with the `[lark_enhance]` prefix for easy filtering.
*   **Error Handling**: API calls are wrapped in `try-except` blocks to ensure plugin failures do not crash the main bot process.
*   **Caching**: Uses TTL-based caching (5 minutes) for group members and group info to reduce API calls.
*   **Debouncing**: History saves are debounced (5 seconds) to reduce disk I/O.

## 4. Testing

*   **Manual Testing**:
    *   **Mentions**: @Mention the bot or other users to verify nickname resolution.
    *   **Mention Conversion**: Ask the bot to "@someone" and verify it creates a real Lark mention.
    *   **Quotes**: Reply to a message and check if the bot understands the context of the replied message.
    *   **History**: Chat in a group and ask the bot to summarize recent discussions.
    *   **Reactions**: Ask the bot to "give me a like" to test `lark_emoji_reply`.
    *   **Streaming Card**: Enable `enable_streaming_card` and verify typewriter effect in Lark.
*   **Debugging**: Enable debug logging in AstrBot to see detailed `[lark_enhance]` logs, including history recording and prompt injection payloads.

## 5. Security

*   **Data Privacy**:
    *   Group history is stored in `data/group_history.json` with minimal information (timestamp, sender name, content).
    *   Sensitive data (like user full names) is fetched via API and cached in memory (`self.user_cache`) to minimize API exposure.
*   **Access Control**:
    *   Gracefully handles `41050` (Permission Denied) errors from Lark API if the bot lacks contact reading permissions.
    *   Validates `event.get_platform_name() == "lark"` before executing platform-specific logic.

## 6. Configuration

Configuration is managed via `_conf_schema.json` and loaded into `self.config`.

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `enable_real_name` | bool | `true` | Resolve OpenIDs to real nicknames. |
| `enable_quoted_content` | bool | `true` | Fetch and inject quoted message content. |
| `enable_group_info` | bool | `true` | Inject group name/description into system prompt. |
| `enable_context_cleaner` | bool | `true` | Sanitize context to prevent LLM errors (Gemini compatibility). |
| `enable_streaming_card` | bool | `false` | Use streaming card messages for typewriter effect. Requires LLM streaming to be enabled. |
| `history_inject_count` | int | `20` | Number of recent group messages to keep and inject into prompt. Set to `0` to disable. |
| `bot_name` | string | `"助手"` | Bot's display name in group chat history records. |
| `enable_mention_convert` | bool | `true` | Convert `@Name` in LLM responses to real Lark mentions. |

## 7. Key Implementation Details

### Streaming Card (Typewriter Effect)

The streaming card feature uses:
1. **`LarkStreamingCard` class**: Manages card creation, updates, and finalization.
2. **Monkey-patching**: Replaces `LarkMessageEvent.send_streaming` at plugin initialization.
3. **Debounced updates**: Updates card every 0.3s or 5 characters to balance responsiveness and API limits.
4. **Patch API**: Uses Lark's `im.v1.message.patch` endpoint for real-time card content updates.

### History Persistence

- History is saved to `data/group_history.json` with debouncing (5-second interval).
- Loaded on plugin initialization, survives AstrBot restarts.
- Cleared when user sends `/reset` command.

### Caching Strategy

- **User cache**: In-memory, no TTL (nicknames rarely change).
- **Group members cache**: 5-minute TTL, used for @ mention conversion.
- **Group info cache**: 5-minute TTL, used for context injection.
