"""Demo agent: proves the whole pipeline (schedule -> facts -> optional LLM text -> finding with evidence -> event).
Disable it in the config once real agents exist (agents.demo.enabled = false)."""
import os, shutil
from ..runner import Agent


class Demo(Agent):
    name = "demo"
    group = "test"
    title = "Demo agent"
    autonomy = "R"
    interval_s = 300

    async def run(self, ctx):
        ctx.working("reading", "load and disk of this container")
        load1, load5, _ = os.getloadavg()
        du = shutil.disk_usage("/")
        pct = round(du.used * 100 / du.total, 1)
        facts = f"load average {load1:.2f} (1 min), {load5:.2f} (5 min); root disk {pct}% used of {du.total // 2**30} GB"
        text = facts
        try:
            ctx.working("thinking", "asking the gateway for a one-line status")
            r = await ctx.llm([{"role": "system", "content": "You write one short plain sentence. No preamble."},
                               {"role": "user", "content": f"Describe this machine's status: {facts}"}], options={"num_predict": 60})
            text = r["text"]
        except Exception as e:                      # the finding is still useful without the model
            text = f"{facts} (no model summary: {e})"
        ctx.working("typing", "writing the finding")
        ctx.finding("demo:heartbeat", "info", "Demo agent heartbeat", text,
                    evidence=[{"type": "metric", "ref": "os.getloadavg / shutil.disk_usage", "excerpt": facts}], confidence=1.0)
        ctx.note(facts)
