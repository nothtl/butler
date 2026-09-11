"""Concrete benchmark adapters.

Only the *adapted* adapters execute here; the official runners are marked
BLOCKED because their environments are not installed on this machine. We never
report an adapted result as an official benchmark result.
"""

from __future__ import annotations

from typing import Any

from .adapter import (ADAPTED, BLOCKED, FAIL, NOT_APPLICABLE, PASS,
                      BenchmarkAdapter, BenchmarkResult)


def _registry_actions(container: Any) -> set[str]:
    from butler.agent.actions import ACTIONS
    return set(ACTIONS)


# ---------------------------------------------------------------------------
# AgentBench FC (adapted)
# ---------------------------------------------------------------------------
class AgentBenchFCAdapter(BenchmarkAdapter):
    """Butler-adapted function-calling evaluation.

    Official AgentBench is NOT executed. This adapter maps an FC-style case to
    Butler's action registry and measures valid tool choice + argument
    acceptance. It is a Butler-adapted suite, not the official benchmark.
    """

    name = "AgentBench FC (Butler-adapted)"
    version = "adapted-1"
    runner = "tests/evals/benchmarks/run_benchmarks.py"

    CASES = [
        {"input": "what's my day", "action": "status"},
        {"input": "plan my week", "action": "plan_week"},
        {"input": "what is most urgent", "action": "urgency"},
        {"input": "show my tasks", "action": "tasks"},
        {"input": "search the web for CS188 news", "action": "web_search"},
        {"input": "remember that I prefer mornings", "action": "memory_learn"},
        {"input": "what do you remember", "action": "memory_query"},
        {"input": "check my courses", "action": "course_list"},
        {"input": "organize my downloads", "action": "organize"},
        {"input": "track CS188 homework", "action": "tracker_create"},
    ]

    def load_cases(self) -> list[dict[str, Any]]:
        return list(self.CASES)

    def build_context(self, case):
        return {}

    def invoke_agent(self, case, context):
        from butler.agent.interpret import DeterministicInterpreter
        from butler.config import Config
        from butler.core import Container
        import tempfile, os
        base = tempfile.mkdtemp(prefix="bench-fc-", dir="/tmp/opencode")
        cfg = Config()
        cfg.data_dir = os.path.join(base, "s"); os.makedirs(cfg.data_dir)
        cfg.config_path = os.path.join(base, "c.toml"); cfg.timezone = "UTC"
        cfg.roots = [os.path.join(base, "r")]; os.makedirs(cfg.roots[0])
        cfg.ensure_dirs()
        c = Container(cfg)
        req = DeterministicInterpreter(c).interpret(case["input"])
        return {"action": req.action.value,
                "valid_action": req.action.value in _registry_actions(c)}

    def evaluate(self, case, output):
        got = output.get("action")
        return {"input": case["input"], "expected": case["action"], "got": got,
                "correct": got == case["action"],
                "valid_action": bool(output.get("valid_action")),
                "error": output.get("error")}

    def summarize(self, results):
        n = len(results)
        correct = sum(1 for r in results if r["correct"])
        valid = sum(1 for r in results if r["valid_action"])
        return BenchmarkResult(
            name=self.name, status=ADAPTED,
            scope=f"{n} FC-style cases",
            score=f"tool_choice {correct}/{n} ({correct/n*100:.0f}%), "
                  f"valid_action {valid}/{n}",
            measures="valid tool choice, registry validity, argument acceptance",
            does_not_map="official AgentBench container tasks/environments",
            environment="local, offline",
            version=self.version, runner=self.runner,
            known_deviations="input phrasing is Butler-specific; no official "
                             "AgentBench harness was run",
            limitations="measures selection only, not multi-step task success")


# ---------------------------------------------------------------------------
# ToolBench (adapted)
# ---------------------------------------------------------------------------
class ToolBenchAdapter(BenchmarkAdapter):
    """Butler-adapted tool-selection evaluation over the action registry."""

    name = "ToolBench (Butler-adapted)"
    version = "adapted-1"
    runner = "tests/evals/benchmarks/run_benchmarks.py"

    CASES = [
        {"input": "show my trackers", "action": "tracker_list"},
        {"input": "why did you notify me", "action": "tracker_explain"},
        {"input": "find me two hours", "action": "find_best_slot"},
        {"input": "optimize my week", "action": "optimize_week"},
        {"input": "add chicken to my pantry", "action": "create_item"},
        {"input": "link this to CS188", "action": "link_items"},
        {"input": "what's in my pantry", "action": "knowledge_lookup"},
        {"input": "show my settings", "action": "settings_view"},
        {"input": "disable web", "action": "settings_update"},
        {"input": "forget my preference", "action": "memory_forget"},
    ]

    def load_cases(self):
        return list(self.CASES)

    def build_context(self, case):
        return {}

    def invoke_agent(self, case, context):
        from butler.agent.interpret import DeterministicInterpreter
        from butler.config import Config
        from butler.core import Container
        import tempfile, os
        base = tempfile.mkdtemp(prefix="bench-tb-", dir="/tmp/opencode")
        cfg = Config()
        cfg.data_dir = os.path.join(base, "s"); os.makedirs(cfg.data_dir)
        cfg.config_path = os.path.join(base, "c.toml"); cfg.timezone = "UTC"
        cfg.roots = [os.path.join(base, "r")]; os.makedirs(cfg.roots[0])
        cfg.ensure_dirs()
        c = Container(cfg)
        req = DeterministicInterpreter(c).interpret(case["input"])
        return {"action": req.action.value,
                "valid_action": req.action.value in _registry_actions(c)}

    def evaluate(self, case, output):
        return {"input": case["input"], "expected": case["action"],
                "got": output.get("action"),
                "correct": output.get("action") == case["action"],
                "valid_action": bool(output.get("valid_action")),
                "error": output.get("error")}

    def summarize(self, results):
        n = len(results)
        correct = sum(1 for r in results if r["correct"])
        valid = sum(1 for r in results if r["valid_action"])
        return BenchmarkResult(
            name=self.name, status=ADAPTED, scope=f"{n} tool-selection cases",
            score=f"tool_selection {correct}/{n} ({correct/n*100:.0f}%), "
                  f"valid_action {valid}/{n}",
            measures="intent -> tool selection, registry validity",
            does_not_map="official ToolBench API/tool corpus",
            environment="local, offline", version=self.version, runner=self.runner,
            known_deviations="Butler registry, not the official ToolBench tools",
            limitations="no argument-generation scoring against official tools")


# ---------------------------------------------------------------------------
# tau-bench (official: BLOCKED)
# ---------------------------------------------------------------------------
class TauBenchAdapter(BenchmarkAdapter):
    name = "tau-bench / tau2 (official)"
    version = "unknown (runner not installed)"
    runner = "official tau-bench runner (not available)"

    def load_cases(self):
        return []

    def build_context(self, case):
        return {}

    def invoke_agent(self, case, context):
        raise RuntimeError("official tau-bench environment is not installed")

    def evaluate(self, case, output):
        return {"error": "not run"}

    def summarize(self, results):
        return BenchmarkResult(
            name=self.name, status=BLOCKED,
            scope="multi-turn tool+policy tasks",
            measures="task success, tool correctness, policy compliance",
            does_not_map="requires the official tau-bench user simulator, "
                         "domain databases and runner",
            environment="not installed; no network/domain match",
            version=self.version, runner=self.runner,
            known_deviations="not executed",
            limitations="Butler maps user->Telegram/CLI, tools->action "
                        "registry, policy->SafetyPolicy, conversation->"
                        "InteractionStore; a compatible harness would be "
                        "required before any score is meaningful")


# ---------------------------------------------------------------------------
# GAIA (official: BLOCKED / NOT APPLICABLE here)
# ---------------------------------------------------------------------------
class GaiaAdapter(BenchmarkAdapter):
    name = "GAIA (official)"
    version = "unknown (runner not installed)"
    runner = "official GAIA runner (not available)"

    def load_cases(self):
        return []

    def build_context(self, case):
        return {}

    def invoke_agent(self, case, context):
        raise RuntimeError("official GAIA environment is not installed")

    def evaluate(self, case, output):
        return {"error": "not run"}

    def summarize(self, results):
        return BenchmarkResult(
            name=self.name, status=BLOCKED,
            scope="general assistant reasoning / research",
            measures="planning, web research, tool use, evidence synthesis",
            does_not_map="GAIA tasks often require arbitrary web browsing and "
                         "file tools with resource needs unsuitable for the Pi",
            environment="not installed; resource/domain mismatch",
            version=self.version, runner=self.runner,
            known_deviations="not executed",
            limitations="do not claim GAIA capability without the official "
                        "runner and a sandboxed environment")


ADAPTERS = (AgentBenchFCAdapter, ToolBenchAdapter, TauBenchAdapter,
            GaiaAdapter)
