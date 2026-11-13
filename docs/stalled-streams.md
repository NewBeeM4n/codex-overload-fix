# Stalled streams / Issue #7

[Issue #7](https://github.com/NewBeeM4n/codex-overload-fix/issues/7) reports an apparently running turn that resumes after about 300 seconds, without an explicit capacity error. This symptom alone does not establish rate limiting or a deliberate change to block the relay. The official [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) documents a 300000 ms SSE idle timeout in Codex. That is consistent with the observed interval, but is not a diagnosis of that particular request.

## What this change does

The relay still retries explicit overload refusals before output. An idle connection, EOF, or transport failure after sending a POST is ambiguous: the upstream may already be processing it. The relay closes that connection without replaying the POST. Once response headers or output have been forwarded, reconnecting or resuming is the client's responsibility; closing the stream does not guarantee that every Codex version will resume automatically.

For a reproducibly stalled connection, try a shorter SSE idle timeout in a manually started relay:

~~~powershell
python relay/relay.py --port 8787 --proxy http://127.0.0.1:7897 --log relay.log --stream-idle-timeout 60
~~~

Use your actual HTTP proxy port; omit --proxy when no proxy is needed. Stop any existing relay on the selected port first. This is a manual launch option, not an installer setting. Do not edit your account credentials or chat database.

| Setting | Default | Scope |
| --- | --- | --- |
| --stream-idle-timeout | Inherits --timeout (600 seconds by default) | Maximum wait for upstream bytes in an uncompressed HTTP 200 SSE body, before and after forwarding starts |
| --commit-deadline | 120 seconds | Maximum time to buffer the response head before forwarding starts |
| --total-budget | 300 seconds | Overall budget before forwarding starts, including retries and backoff |

The earliest applicable deadline wins. The new option does not shorten connection establishment, response-header waits, JSON responses, or compressed responses. It resets on upstream byte activity, including SSE comments/heartbeats. It does not detect a semantic stall when the server keeps sending bytes. A shorter value can interrupt legitimate silent reasoning; keep the defaults unless the tradeoff is useful for your workload. This option does not bypass quota or service-side restrictions.

## Reading diagnostics

After forwarding begins, the relay now logs a summary such as:

~~~text
[12345] stream ended: upstream idle timeout; terminal=none; forwarded=1234B; elapsed=60.1s; not replayed
~~~

- upstream idle timeout: no upstream bytes arrived within the configured interval.
- eof: the upstream reader returned EOF. This alone does not mean the response completed.
- transport interrupted: a socket or HTTP framing error interrupted forwarding.
- client closed: the connection watcher detected client cancellation.
- terminal=response.completed: a complete SSE response.completed event was observed.
- terminal=none: no recognized terminal event was observed; this is not proof of overload.

The terminal field is an allowlisted event type, never event text, IDs, or error messages. Diagnostic inspection is bounded and may stop on an oversized partial event. Raw response bytes are still forwarded unchanged. Non-SSE and compressed bodies are not inspected; their terminal field remains none.

Before forwarding starts, the existing upstream status log now also identifies early EOF, idle timeout, the pre-output deadline, or client cancellation. The existing POST log records request sizes and model settings; it does not record request or response content. When reporting a recurrence, share only the few corresponding log lines after checking them for sensitive metadata, together with Codex version, platform, and whether a proxy was used.

## Validation and remaining uncertainty

Local HTTP fixtures cover silent setup, a stall after reasoning output, activity extending past the idle interval, truncated chunk framing, JSON passthrough, and no automatic replay of ambiguous requests. These establish the relay behavior. The original account/server condition in Issue #7 has not been reproduced, so this is a diagnostic and waiting-time mitigation, not a confirmed fix for its underlying cause.
