import asyncio
from anvil_forge import Platform

async def main():
    platform = Platform.from_config("anvil-forge.yaml", app="ironclad")
    reply = await platform.run_agent("support_agent", "What accounts do I have?")
    print(reply.content, reply.status, reply.trace_id)

asyncio.run(main())
