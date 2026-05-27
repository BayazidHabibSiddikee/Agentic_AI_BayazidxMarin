import subprocess
import json
import re
import shutil
import os
import tempfile
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime


# ─────────────────────────────────────────────
#  Data Structures
# ─────────────────────────────────────────────

@dataclass
class Subtask:
    id: int
    description: str
    type: str          # "write" | "review" | "debug" | "general"
    depends_on: List[int] = field(default_factory=list)
    assigned_agent: str = ""
    status: str = "pending"   # pending | running | done | failed
    output: str = ""
    error: str = ""


@dataclass
class AgentResult:
    agent: str
    subtask_id: int
    output: str
    success: bool
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ─────────────────────────────────────────────
#  Agent invocation strategies (verified from --help)
#
#  claude      →  claude -p "<prompt>"
#  openclaude  →  openclaude -p "<prompt>"
#  opencode    →  opencode run "<prompt>"
#  gemini      →  gemini -p "<prompt>"
#  kiro-cli    →  kiro-cli chat  (prompt via stdin)
# ─────────────────────────────────────────────

AGENT_STRATEGIES: Dict[str, dict] = {
    "claude":     {"mode": "flag",   "args": ["-p"]},
    "openclaude": {"mode": "flag",   "args": ["-p"]},
    "opencode":   {"mode": "subarg", "args": ["run"]},
    "gemini":     {"mode": "flag",   "args": ["-p"]},
    "kiro-cli":   {"mode": "stdin",  "args": ["chat"]},
}

# Per-agent timeouts (seconds).
# opencode hangs on long prompts → short leash so fallback kicks in fast.
AGENT_TIMEOUTS: Dict[str, int] = {
    "claude":     60,
    "openclaude": 60,
    "opencode":   45,   # ← short: times out often, fall back to claude quickly
    "gemini":     60,
    "kiro-cli":   60,
}


# ─────────────────────────────────────────────
#  MasterAgent
# ─────────────────────────────────────────────

class MasterAgent:
    AGENT_POOL: List[str] = ["claude", "openclaude", "opencode", "gemini", "kiro-cli"]

    STAGE_ROLES = {
        "write":   "You are a senior software engineer. Write clean, well-documented code.",
        "review":  "You are a code reviewer. Identify bugs, style issues, and improvements.",
        "debug":   "You are a debugging expert. Fix errors and explain what was wrong.",
        "general": "You are a helpful assistant. Complete the task as best you can.",
    }

    def __init__(self, orchestrator: str = "opencode", verbose: bool = True):
        self.orchestrator = orchestrator
        self.verbose = verbose
        self._rr_index = 0
        self.history: List[AgentResult] = []

    # ── Logging ──────────────────────────────

    def log(self, msg: str, level: str = "INFO"):
        if self.verbose:
            tag = {"INFO": "[*]", "WARN": "[!]", "ERR": "[✗]", "OK": "[✓]"}.get(level, "[?]")
            print(f"{tag} {msg}")

    # ── Agent pool ────────────────────────────

    def available_agents(self) -> List[str]:
        return [a for a in self.AGENT_POOL if shutil.which(a) is not None]

    def next_agent(self) -> str:
        pool = self.available_agents()
        if not pool:
            raise RuntimeError(
                "No CLI agents found in PATH. Need at least one of: " +
                ", ".join(self.AGENT_POOL)
            )
        agent = pool[self._rr_index % len(pool)]
        self._rr_index += 1
        return agent

    # ── Low-level CLI call ────────────────────

    def _call_agent(self, agent: str, prompt: str) -> str:
        """
        Call one CLI agent with its correct invocation strategy and per-agent timeout.
        Returns output string on success, or "[ERROR] ..." / "[SKIP] ..." on failure.
        Never raises.
        """
        if shutil.which(agent) is None:
            return f"[SKIP] Agent '{agent}' not found in PATH."

        strategy = AGENT_STRATEGIES.get(agent, {"mode": "flag", "args": ["-p"]})
        timeout  = AGENT_TIMEOUTS.get(agent, 60)
        mode     = strategy["mode"]
        args     = strategy["args"]

        try:
            if mode in ("flag", "subarg"):
                # claude -p "prompt"  /  opencode run "prompt"
                cmd = [agent] + args + [prompt]
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout
                )
            elif mode == "stdin":
                # kiro-cli chat  (prompt on stdin)
                cmd = [agent] + args
                result = subprocess.run(
                    cmd, input=prompt,
                    capture_output=True, text=True, timeout=timeout
                )
            else:
                return f"[ERROR] Unknown strategy mode '{mode}' for agent '{agent}'."

            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()

            # ── Fix: empty final answer ──────────────────────────────────
            # Some agents (claude, openclaude) write the actual response to
            # stderr (progress/streaming lines) and stdout ends up empty.
            # Prefer stdout when non-empty; fall back to stderr.
            output = stdout if stdout else stderr

            if not output:
                return f"[ERROR] {agent}: empty response (exit {result.returncode})"

            # Non-zero exit but we still got text → treat as success (agent
            # may exit non-zero on warnings). Only error if truly empty.
            return output

        except subprocess.TimeoutExpired:
            return f"[ERROR] Agent '{agent}' timed out after {timeout}s."
        except Exception as e:
            return f"[ERROR] {agent}: {e}"

    # ── Fallback loop ─────────────────────────

    def _call_agent_with_fallback(self, preferred: str, prompt: str) -> tuple[str, str]:
        """
        Try preferred agent first. On any error, cycle through all available
        agents until one succeeds. Returns (agent_name_used, output).
        """
        pool = self.available_agents()
        candidates = [preferred] + [a for a in pool if a != preferred]

        for agent in candidates:
            self.log(f"Trying agent '{agent}' ...")
            out = self._call_agent(agent, prompt)

            if out.startswith("[ERROR]") or out.startswith("[SKIP]"):
                self.log(f"Agent '{agent}' failed: {out}", "WARN")
                continue

            self.log(f"Agent '{agent}' responded OK.", "OK")
            return agent, out

        return preferred, "[ERROR] All agents failed to respond."

    # ── Orchestrator: decompose task ──────────

    def decompose_task(self, task: str) -> List[Subtask]:
        self.log(f"Decomposing task with orchestrator '{self.orchestrator}' ...")

        decompose_prompt = (
            'Break this coding task into subtasks (write→review→debug order).\n'
            'Reply ONLY with a JSON array, no markdown, no preamble.\n'
            'Schema: [{"id":1,"description":"...","type":"write|review|debug|general","depends_on":[]}]\n\n'
            f'Task: {task}'
        )

        agent_used, raw = self._call_agent_with_fallback(self.orchestrator, decompose_prompt)
        self.log(f"Decompose response from '{agent_used}':\n{raw}")

        # Strip accidental markdown fences
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        # Extract JSON array even if buried in prose
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            raw = match.group(0)

        try:
            items = json.loads(raw)
            subtasks = [
                Subtask(
                    id=item["id"],
                    description=item["description"],
                    type=item.get("type", "general"),
                    depends_on=item.get("depends_on", []),
                )
                for item in items
            ]
            self.log(f"Decomposed into {len(subtasks)} subtask(s).", "OK")
            return subtasks
        except (json.JSONDecodeError, KeyError) as e:
            self.log(f"Could not parse subtasks ({e}). Using single-subtask fallback.", "WARN")
            return [Subtask(id=1, description=task, type="general")]

    # ── Execute one subtask ───────────────────

    def execute_subtask(self, subtask: Subtask, context: str = "") -> AgentResult:
        preferred = self.next_agent()
        subtask.assigned_agent = preferred
        subtask.status = "running"

        role = self.STAGE_ROLES.get(subtask.type, self.STAGE_ROLES["general"])
        prompt_parts = [role, f"\nSubtask: {subtask.description}"]
        if context:
            prompt_parts.append(f"\nContext / previous output:\n{context}")
        full_prompt = "\n".join(prompt_parts)

        self.log(f"Subtask {subtask.id} ({subtask.type}) → preferred agent '{preferred}'")
        agent_used, output = self._call_agent_with_fallback(preferred, full_prompt)

        success = not output.startswith("[ERROR]")
        subtask.status = "done" if success else "failed"
        subtask.output = output
        subtask.assigned_agent = agent_used
        if not success:
            subtask.error = output

        result = AgentResult(agent=agent_used, subtask_id=subtask.id,
                             output=output, success=success)
        self.history.append(result)
        return result

    # ── Main entry point ──────────────────────

    def execute_task(self, task: str) -> str:
        self.log(f"Starting task: {task}")
        pool = self.available_agents()
        if not pool:
            return "[ERROR] No CLI agents available. Install claude, opencode, or gemini."
        self.log(f"Available agents: {pool}")

        # Step 1: Decompose
        subtasks = self.decompose_task(task)

        # Step 2: Execute in dependency order, chaining outputs
        completed: Dict[int, str] = {}

        for subtask in subtasks:
            dep_context = ""
            for dep_id in subtask.depends_on:
                if dep_id in completed:
                    dep_context += f"\n--- Output of subtask {dep_id} ---\n{completed[dep_id]}\n"

            result = self.execute_subtask(subtask, context=dep_context)

            if result.success:
                completed[subtask.id] = result.output
                self.log(f"Subtask {subtask.id} done by '{result.agent}'.", "OK")
            else:
                self.log(f"Subtask {subtask.id} failed: {result.output}", "ERR")
                completed[subtask.id] = f"[FAILED] {result.output}"

        # Step 3: Synthesise final answer
        self.log("Synthesising final answer ...")
        synthesis_input = "\n\n".join(
            f"=== Subtask {st.id}: {st.description} ===\n"
            f"Agent: {st.assigned_agent}\n{completed.get(st.id, '[no output]')}"
            for st in subtasks
        )
        synthesis_prompt = (
            f"You are the final reviewer.\n"
            f"Original task: {task}\n\n"
            f"Outputs from all agents:\n\n{synthesis_input}\n\n"
            f"Produce a clean, final consolidated answer. Highlight any unresolved issues."
        )
        _, final = self._call_agent_with_fallback(self.orchestrator, synthesis_prompt)
        self.log("Task complete.", "OK")
        return final

    # ── Summary report ────────────────────────

    def print_summary(self):
        print("\n" + "═" * 60)
        print("  EXECUTION SUMMARY")
        print("═" * 60)
        for r in self.history:
            status = "✓" if r.success else "✗"
            print(f"  [{status}] Subtask {r.subtask_id:02d}  agent={r.agent:<12}  ts={r.timestamp}")
        print("═" * 60 + "\n")


# ─────────────────────────────────────────────
#  CLI entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
        "Write a Python function that merges two sorted lists. "
        "Review it for correctness and style, then debug any issues."
    )

    agent = MasterAgent(orchestrator="opencode", verbose=True)
    final_answer = agent.execute_task(task)

    agent.print_summary()

    print("\n" + "═" * 60)
    print("  FINAL ANSWER")
    print("═" * 60)
    print(final_answer)
