"""Test fixtures and mock stubs for Hermes in-memory provider tests."""

import json
import sys
import types

# Ensure stub modules exist when running on standard Python without Hermes base image.
if "agent" not in sys.modules:
    agent_mod = types.ModuleType("agent")
    memory_provider_mod = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        """Stub MemoryProvider base class."""
        pass

    memory_provider_mod.MemoryProvider = MemoryProvider
    agent_mod.memory_provider = memory_provider_mod
    sys.modules["agent"] = agent_mod
    sys.modules["agent.memory_provider"] = memory_provider_mod

if "plugins" not in sys.modules:
    plugins_mod = types.ModuleType("plugins")
    plugins_memory_mod = types.ModuleType("plugins.memory")
    plugins_memory_mod.load_memory_provider = lambda name: None
    plugins_mod.memory = plugins_memory_mod
    sys.modules["plugins"] = plugins_mod
    sys.modules["plugins.memory"] = plugins_memory_mod

if "tools" not in sys.modules:
    tools_mod = types.ModuleType("tools")
    tools_reg_mod = types.ModuleType("tools.registry")
    tools_reg_mod.tool_error = lambda msg, **kw: json.dumps({"error": msg, **kw})
    tools_mod.registry = tools_reg_mod
    sys.modules["tools"] = tools_mod
    sys.modules["tools.registry"] = tools_reg_mod
