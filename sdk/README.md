# garrison-sdk

[![PyPI](https://img.shields.io/pypi/v/garrison-sdk.svg)](https://pypi.org/project/garrison-sdk/) [![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/infoshikharpathak/garrison/blob/main/LICENSE)

Governed, traced AI agents on [Anvil](https://github.com/infoshikharpathak/anvil)
(tool governance) and [Agent Forge](https://github.com/infoshikharpathak/Agent-Forge)
(agents). Declare apps, tools, agents and flows once; every run is checked by
Anvil, executed by Agent Forge and traced end to end under one `trace_id`.

This is the SDK. The control plane (accounts, versioned prompts, app keys,
dashboards) is [`garrison-hq`](https://pypi.org/project/garrison-hq/).

## Install

```bash
pip install garrison-sdk                 # OpenAI-backed agents
pip install "garrison-sdk[anthropic]"    # + agents on Anthropic models
```

**Install `garrison-sdk`, import `garrison`:** the install name and the
import name differ. Python 3.11 or newer. It brings `garrison-anvil` (imported as `anvil`) and
`garrison-forge` (imported as `agent_forge`); for those two the install and
import names differ. Garrison was called Bastion before its first release;
see the [changelog](https://github.com/infoshikharpathak/garrison/blob/main/CHANGELOG.md)
for what changed.

## Quickstart

`garrison.yaml`:

```yaml
apps:
  ironclad:
    base_url: http://localhost:8765
    tools:
      get_accounts: {path: /accounts, method: GET, trust: trusted, description: List the user's bank accounts.,
                     parameters: {type: object, properties: {user_id: {type: string}}, required: [user_id]}}
agents:
  ironclad.support_agent:
    instructions: You are Ironclad's support agent for user_id "demo". Look up accounts before answering.
    prompt_version: v1
    tools: [get_accounts]
```

```python
import asyncio
from garrison import Platform

async def main():
    platform = Platform.from_config("garrison.yaml", app="ironclad")
    reply = await platform.run_agent("support_agent", "What accounts do I have?")
    print(reply.content, reply.status, reply.trace_id)

asyncio.run(main())
```

Set `OPENAI_API_KEY` first. The run's input, every tool call and the output
are checked by Anvil, and the full trace is appended to
`./garrison_traces.jsonl`. A runnable version with a stand-in bank API is in
[examples/quickstart](https://github.com/infoshikharpathak/garrison/tree/main/examples/quickstart).

## Remote mode

Run on the bundle a `garrison-hq` service publishes for the key's app; prompt
edits there reach the running app without a restart:

```python
platform = Platform.from_service(os.environ["GARRISON_URL"], api_key=os.environ["GARRISON_KEY"])
```

## More

Sessions, flows, grants and credentials, agent-to-agent calls
(`garrison.a2a`), the dashboard sink and the config reference are in the
[full README](https://github.com/infoshikharpathak/garrison#readme).

## License

Apache License 2.0. See [LICENSE](https://github.com/infoshikharpathak/garrison/blob/main/LICENSE).
