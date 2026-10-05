"""
PHOENIX action-control state machine (orchestrator-owned safety).
Pure-python, no heavy imports, so it can be unit-tested without loading the LLM.

The single architectural invariant enforced here:
  PHOENIX may only execute an action that was generated for the current goal
  (generation_id) and the current observation (observation_id). Every executed
  visual action must be followed by a fresh observation before another visual
  action.

Rules enforced (from the MK3 implementation plan):
  R1  An action from generation N cannot execute in generation N+1.
  R2  An action generated from observation N cannot execute against obs N+1.
  R3  A stale action never reaches Needle.
  R4  A visual action requires a fresh observation before another visual action.
  R5  The executor (nidle) never decides whether an action is still appropriate.
  R6  The LLM cannot override freshness validation.
  R7  A failed action cannot silently become a successful step.
  R8  A goal change invalidates every pending action from the previous goal.
"""

import time
import uuid
import hashlib

# --------------------------------------------------------------------------
# Result codes
# --------------------------------------------------------------------------
VALID = "VALID"
STALE_GENERATION = "STALE_GENERATION"
STALE_OBSERVATION = "STALE_OBSERVATION"
STALE_TARGET = "STALE_TARGET"
DOUBLE_EXECUTION = "DOUBLE_EXECUTION"
EXECUTOR_BUSY = "EXECUTOR_BUSY"
INVALID_TARGET = "INVALID_TARGET"

# Execution outcome statuses
SUCCESS = "SUCCESS"
FAILURE = "FAILURE"
UNCERTAIN = "UNCERTAIN"
STALE = "STALE"
INTERRUPTED = "INTERRUPTED"

VISUAL_ACTIONS = {"click", "type", "drag", "scroll", "double", "press"}
DETERMINISTIC_ACTIONS = {"launch", "open", "press", "scroll"}


class ActionProposal:
    """Immutable proposal once the orchestrator binds an LLM output to an observation."""

    __slots__ = ("action_id", "generation_id", "observation_id", "action_type",
                 "target_id", "target_type", "target_text", "bounds",
                 "full_command", "created_at", "executed", "status")

    def __init__(self, action_id, generation_id, observation_id, action_type,
                 full_command, target_id=None, target_type=None, target_text=None,
                 bounds=None):
        self.action_id = action_id
        self.generation_id = generation_id
        self.observation_id = observation_id
        self.action_type = action_type
        self.target_id = target_id
        self.target_type = target_type
        self.target_text = target_text or ""
        self.bounds = bounds or (0.0, 0.0, 0.0, 0.0)
        self.full_command = full_command
        self.created_at = time.time()
        self.executed = False
        self.status = "pending"

    def bind_observation(self, observation_id):
        """Snapshot-identify a proposal. Only the orchestrator may call this,
        and only when constructing a NEW proposal from the LLM output."""
        self.observation_id = observation_id

    def describe(self):
        return (f"Action(id={self.action_id}, gen={self.generation_id}, "
                f"obs={self.observation_id}, type={self.action_type}, "
                f"target_id={self.target_id}, tgt_text={self.target_text!r}, "
                f"cmd={self.full_command!r})")


class GoalState:
    __slots__ = ("goal_text", "generation_id", "started_at", "context_hash")

    def __init__(self):
        self.goal_text = None
        self.generation_id = 0
        self.started_at = None
        self.context_hash = None

    def new_goal(self, goal_text):
        """Begin a new goal. Returns the new generation_id. Invalidates all
        pending actions from the previous goal (R8) by construction."""
        self.goal_text = goal_text
        self.generation_id += 1
        self.started_at = time.time()
        self.context_hash = hashlib.sha256((goal_text or "").encode()).hexdigest()[:16]
        return self.generation_id


class StepState:
    __slots__ = ("description", "status", "step_id")

    def __init__(self):
        self.description = None
        self.status = "idle"  # idle | active | done | failed
        self.step_id = 0

    def begin(self, description):
        self.step_id += 1
        self.description = description
        self.status = "active"

    def finish(self, ok=True):
        self.status = "done" if ok else "failed"


class PerceptionState:
    """One snapshot per screen/DOM parse. observation_id strictly increases."""

    __slots__ = ("observation_id", "timestamp", "foreground_window", "dom",
                 "screenshot_hash", "score_threshold", "extra")

    def __init__(self):
        self.observation_id = 0
        self.timestamp = None
        self.foreground_window = None
        self.dom = []          # list of {'type':..., 'content':..., 'bbox':...}
        self.screenshot_hash = None
        self.score_threshold = None
        self.extra = {}

    def capture(self, dom, screenshot_hash=None, foreground_window=None,
                score_threshold=None):
        self.observation_id += 1
        self.dom = list(dom or [])
        self.timestamp = time.time()
        self.screenshot_hash = screenshot_hash
        self.foreground_window = foreground_window
        self.score_threshold = score_threshold
        return self.observation_id

    def element(self, element_id):
        if element_id is None:
            return None
        if not (isinstance(element_id, int) and 0 <= element_id < len(self.dom)):
            return None
        return self.dom[element_id]


class ExecutionState:
    """Single-at-a-time execution; guards double-execution and busy re-entry."""

    __slots__ = ("status", "action_id", "action", "executed_ids",
                 "last_execution_result", "verify_requested", "started_at")

    def __init__(self):
        self.status = "idle"          # idle | executing | verifying | completed
        self.action_id = None
        self.action = None            # last ActionProposal
        self.executed_ids = set()     # every action_id ever dispatched (R2 double-exec)
        self.last_execution_result = None
        self.verify_requested = False
        self.started_at = None

    def start(self, action):
        if self.status == "executing":
            return EXECUTOR_BUSY
        if action.action_id in self.executed_ids:
            return DOUBLE_EXECUTION
        self.status = "executing"
        self.action_id = action.action_id
        self.action = action
        self.started_at = time.time()
        self.verify_requested = False
        self.executed_ids.add(action.action_id)
        return VALID

    def mark_verify(self):
        if self.status == "executing":
            self.status = "verifying"

    def interrupt(self, reason="interrupted"):
        """A watchdog/plan abort ended the executing action before it completed.
        Marks the action INTERRUPTED so a later step can never claim it succeeded
        (R7) and the driver forces a fresh observation on the next dispatch."""
        if self.status in ("executing", "verifying"):
            self.status = "completed"
            if self.action is not None:
                self.action.status = INTERRUPTED
            self.last_execution_result = INTERRUPTED
        return INTERRUPTED

    def finish(self, result):
        self.status = "completed"
        self.action.status = result
        self.last_execution_result = result


class Orchestrator:
    """Owns goal/step/perception/execution state and validates every action.

    The LLM only proposes; this class decides whether the proposal is allowed.
    """

    def __init__(self):
        self.goal = GoalState()
        self.step = StepState()
        self.perception = PerceptionState()
        self.execution = ExecutionState()
        self._action_seq = 0

    def new_user_intent(self, goal_text):
        """User gave a fresh instruction. New goal -> new generation_id (R8),
        pending actions aborted. Returns the generation_id."""
        gen = self.goal.new_goal(goal_text)
        self.step.status = "idle"
        self.execution.status = "idle"
        self.execution.action_id = None
        self.execution.verify_requested = False
        self.execution.last_execution_result = None
        return gen

    def observe(self, dom, screenshot_hash=None, foreground_window=None,
                score_threshold=None):
        """Fresh observation. Every visual action MUST be preceded by a call to
        this (R4)."""
        obs_id = self.perception.capture(dom, screenshot_hash, foreground_window,
                                         score_threshold)
        return obs_id

    def propose(self, action_type, full_command, target_id=None,
                target_type=None, target_text=None, bounds=None):
        """Bind an LLM-proposed action to the CURRENT generation+observation.
        This is the only place observation_id is attached; the LLM cannot pass
        its own ids (R6)."""
        self._action_seq += 1
        return ActionProposal(
            action_id=self._action_seq,
            generation_id=self.goal.generation_id,
            observation_id=self.perception.observation_id,
            action_type=action_type,
            full_command=full_command,
            target_id=target_id,
            target_type=target_type,
            target_text=target_text,
            bounds=bounds,
        )

    # ---------------- validation ----------------
    def validate(self, action):
        """Returns VALID or a rejection reason. Runs immediately before dispatch
        to Needle; the executor never re-validates (R5) and the LLM can't override
        whether it passes (R6)."""
        if action.generation_id != self.goal.generation_id:
            return STALE_GENERATION
        if action.action_type in VISUAL_ACTIONS and action.observation_id != self.perception.observation_id:
            return STALE_OBSERVATION
        if action.action_id in self.execution.executed_ids:
            return DOUBLE_EXECUTION
        if self.execution.status == "executing":
            return EXECUTOR_BUSY
        if action.action_type in VISUAL_ACTIONS and action.target_id is not None:
            el = self.perception.element(action.target_id)
            if el is None:
                return INVALID_TARGET
        return VALID

    def target_is_valid(self, action, exact=False):
        """Strong target validation: id exists AND type AND approximate bounds AND
        semantic label agree (guard against id reuse after DOM shuffle)."""
        el = self.perception.element(action.target_id)
        if el is None:
            return False
        # type
        if action.target_type and action.target_type != "any":
            if (el.get("type") or "").strip().lower() != action.target_type.strip().lower():
                return False
        # semantic label (substring match; the label is what the model saw)
        if action.target_text:
            if action.target_text not in (el.get("content") or ""):
                return False
        # approximate bounds (normalized coords; allow the DOM to drift a little)
        if action.bounds and len(action.bounds) == 4 and len(el.get("bbox") or []) == 4:
            ex = [float(x) for x in el["bbox"]]
            ax = [float(x) for x in action.bounds]
            drift = max(abs(ex[i] - ax[i]) for i in range(4))
            if drift > 0.08 and exact:
                return False
        return True

    def needs_fresh_observation(self, action, last_visual_obs_id=None):
        """R4: a visual action must run against a strictly newer observation than
        the last one a visual action ran against."""
        if action.action_type not in VISUAL_ACTIONS:
            return False
        if last_visual_obs_id is None:
            return False
        return action.observation_id <= last_visual_obs_id

    def must_reevaluate(self):
        """Stale actions are never sent to Needle; they force a re-observation."""
        return self.execution.action is not None and \
            self.execution.action.status in (STALE, STALE_GENERATION,
                                             STALE_OBSERVATION, STALE_TARGET,
                                             INTERRUPTED)

    def is_deterministic(self, action_type):
        """Plan Phase 2.1: macro-chaining is ONLY allowed for deterministic
        actions (e.g. launch). Visual actions default to single-step."""
        return action_type in DETERMINISTIC_ACTIONS and action_type not in VISUAL_ACTIONS

    def verify_deterministic(self, action, expected_change):
        """Level-1 deterministic verification: did the intended state change
        actually occur? Returns SUCCESS / FAILURE / UNCERTAIN.

        expected_change: dict with keys like 'window_changed', 'screen_changed',
        'dom_gained', 'dom_lost' populated by the caller from before/after
        perception snapshots."""
        if not expected_change:
            return UNCERTAIN
        changed = (expected_change.get("window_changed") or
                   expected_change.get("screen_changed") or
                   expected_change.get("dom_gained") or
                   expected_change.get("dom_lost"))
        if changed:
            return SUCCESS
        # action reported success locally but the world didn't change -> at least
        # it isn't a proven success; the system may decide to repeat, but it can
        # NEVER mark it as a succeeded step silently (R7).
        return UNCERTAIN