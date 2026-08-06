"""Autonomous /goal execution state machine (L6).

Modules:

- ``models.py``  — GoalState / phases / statuses / stop reasons
- ``store.py``   — atomic persistence (goal.json + goal-history/)
- ``policy.py``  — limits validation + hard-stop decisions
- ``engine.py``  — pure state transitions
- ``prompt.py``  — ACT-stage instruction builder for the agent
- ``runner.py``  — background thread orchestrating L2–L5
- ``commands.py`` — /goal CLI parsing + handlers
"""
