"""The agent bridge: ACP asks, ACP discussions and their transports.

Import the module you use (for example ``ai_agent_bridge.monitor_client`` or
``ai_agent_bridge._cli``). The package itself loads nothing, so importing a
transport or the Monitor client never initializes the CLI. Fleet Comms is the
only message store; the legacy broker, inbox and channel stores are removed
(#515).
"""
