"""Agent eval harness (issue #69): scenario-based diagnostic benchmarks.

Runs the real AgentRuntime + ToolExecutor against a live model endpoint
while the cluster is simulated from scenario fixtures. CI only smoke-tests
the harness itself with a scripted provider; live-model runs are local.
"""
