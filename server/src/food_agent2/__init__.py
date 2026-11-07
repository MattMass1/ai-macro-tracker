"""The food-logging agent: one structured model call per turn, then plain code.

The model reads the message. Code does retrieval, portion math, the preview,
confirmation, the write, the readback and the receipt. Every question comes from
code and is asked once. Behind ``FOOD_AGENT2_ENABLED``
(``release_gates.food_agent2_allowed``).

Modules: ``contracts`` (what the model may return), ``interpret`` (the model call),
``match`` (identity comparison and hard guards), ``retrieval`` (the lookup ladder),
``sources`` (live providers), ``history`` (recall from the user's own log), ``turn``
(the coordinator).
"""
