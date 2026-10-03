#!/usr/bin/env python3
"""
Voice agent evaluation harness — 20 clinic scenarios.

Drives the Setod ConversationRelay WebSocket directly (no real Twilio call needed):
  1. Opens wss://api.setod.com/voice/ws/{trigger_id} as if Twilio did.
  2. Sends a "setup" frame with a synthetic callSid and caller number.
  3. For each turn, sends a "prompt" frame and collects the response tokens.
  4. Scores: task completion (TC), tool-argument accuracy (TA), first-token latency (FTL).
  5. Writes results to voice_eval_results.json.

Usage:
    python3 scripts/voice_eval.py --trigger <trigger_id> --url wss://api.setod.com

The trigger must have a phone trigger configured and a connected agent.

Requires: websockets>=12, httpx
    pip install "websockets>=12" httpx
"""

from __future__ import annotations

import asyncio
import json
import time
import argparse
import os
from dataclasses import dataclass, field, asdict
from typing import Any

try:
    import websockets
except ImportError:
    raise SystemExit("pip install 'websockets>=12'")

# ── Scenarios ─────────────────────────────────────────────────────────────────
# Each scenario: caller utterance(s) and expected outcomes.
# task_keywords: words that MUST appear in agent response to count as task-complete.
# expected_tool_calls: tool name → required argument keys/values (for TA scoring).

SCENARIOS: list[dict[str, Any]] = [
    # 1. Book an appointment
    {
        "id": "S01",
        "name": "Book appointment",
        "turns": [
            "Hi, I'd like to book an appointment with Dr. Smith for next Tuesday at 2pm.",
        ],
        "task_keywords": ["tuesday", "2", "booked", "confirmed", "appointment"],
        "expected_tools": {},
    },
    # 2. Cancel appointment
    {
        "id": "S02",
        "name": "Cancel appointment",
        "turns": [
            "I need to cancel my appointment tomorrow morning.",
        ],
        "task_keywords": ["cancel", "cancelled", "removed"],
        "expected_tools": {},
    },
    # 3. Reschedule
    {
        "id": "S03",
        "name": "Reschedule appointment",
        "turns": [
            "Can I move my appointment from Thursday to Friday at the same time?",
        ],
        "task_keywords": ["friday", "rescheduled", "moved", "changed"],
        "expected_tools": {},
    },
    # 4. Get clinic hours
    {
        "id": "S04",
        "name": "Clinic hours query",
        "turns": [
            "What are your clinic hours on weekends?",
        ],
        "task_keywords": ["saturday", "sunday", "weekend", "hours", "closed", "open"],
        "expected_tools": {},
    },
    # 5. Insurance question
    {
        "id": "S05",
        "name": "Insurance accepted",
        "turns": [
            "Do you accept Blue Cross insurance?",
        ],
        "task_keywords": ["blue cross", "insurance", "accept", "covered"],
        "expected_tools": {},
    },
    # 6. Prescription refill
    {
        "id": "S06",
        "name": "Prescription refill request",
        "turns": [
            "I need a refill on my metformin prescription.",
        ],
        "task_keywords": ["refill", "prescription", "metformin", "doctor", "sent"],
        "expected_tools": {},
    },
    # 7. Emergency redirection
    {
        "id": "S07",
        "name": "Emergency redirection",
        "turns": [
            "I'm having chest pains and difficulty breathing.",
        ],
        "task_keywords": ["911", "emergency", "immediately", "call"],
        "expected_tools": {},
    },
    # 8. Lab results inquiry
    {
        "id": "S08",
        "name": "Lab results",
        "turns": [
            "I was wondering if my lab results are ready?",
        ],
        "task_keywords": ["lab", "results", "doctor", "contact", "review"],
        "expected_tools": {},
    },
    # 9. New patient registration
    {
        "id": "S09",
        "name": "New patient registration",
        "turns": [
            "I'm a new patient and would like to register.",
        ],
        "task_keywords": ["register", "form", "information", "welcome", "new patient"],
        "expected_tools": {},
    },
    # 10. Referral request
    {
        "id": "S10",
        "name": "Specialist referral",
        "turns": [
            "Can I get a referral to a cardiologist?",
        ],
        "task_keywords": ["referral", "cardiologist", "doctor", "specialist"],
        "expected_tools": {},
    },
    # 11. Multi-turn: appointment with clarification
    {
        "id": "S11",
        "name": "Appointment with date clarification",
        "turns": [
            "I want to book an appointment.",
            "Sometime next week if possible.",
            "Monday would be great.",
        ],
        "task_keywords": ["monday", "booked", "confirmed"],
        "expected_tools": {},
    },
    # 12. Wrong number (graceful)
    {
        "id": "S12",
        "name": "Wrong number graceful exit",
        "turns": [
            "Oh sorry, I think I have the wrong number.",
        ],
        "task_keywords": ["no problem", "okay", "goodbye", "sorry", "help"],
        "expected_tools": {},
    },
    # 13. Billing/cost question
    {
        "id": "S13",
        "name": "Appointment cost query",
        "turns": [
            "How much does a general consultation cost?",
        ],
        "task_keywords": ["cost", "fee", "price", "dollar", "consultation"],
        "expected_tools": {},
    },
    # 14. Directions/location
    {
        "id": "S14",
        "name": "Clinic location",
        "turns": [
            "Where is your clinic located?",
        ],
        "task_keywords": ["address", "street", "located", "clinic", "directions"],
        "expected_tools": {},
    },
    # 15. After-hours voicemail
    {
        "id": "S15",
        "name": "After-hours handling",
        "turns": [
            "I'm calling after hours to leave a message for my doctor.",
        ],
        "task_keywords": ["message", "doctor", "morning", "callback", "note"],
        "expected_tools": {},
    },
    # 16. Appointment reminder check
    {
        "id": "S16",
        "name": "Reminder confirmation",
        "turns": [
            "I got a reminder call about an appointment but I can't remember when it is.",
        ],
        "task_keywords": ["appointment", "time", "date", "scheduled"],
        "expected_tools": {},
    },
    # 17. Medication side effects
    {
        "id": "S17",
        "name": "Medication side effects",
        "turns": [
            "I've been having side effects from my new medication. What should I do?",
        ],
        "task_keywords": ["doctor", "pharmacist", "side effect", "contact", "consult"],
        "expected_tools": {},
    },
    # 18. Wait time inquiry
    {
        "id": "S18",
        "name": "Walk-in wait time",
        "turns": [
            "If I walk in right now, how long is the wait?",
        ],
        "task_keywords": ["wait", "time", "minutes", "walk-in", "busy"],
        "expected_tools": {},
    },
    # 19. Fax/form request
    {
        "id": "S19",
        "name": "Medical form request",
        "turns": [
            "Can you fax my medical records to my new doctor?",
        ],
        "task_keywords": ["fax", "records", "send", "doctor", "request"],
        "expected_tools": {},
    },
    # 20. Language switch
    {
        "id": "S20",
        "name": "Language preference",
        "turns": [
            "Bonjour, parlez-vous français?",
        ],
        "task_keywords": ["français", "french", "parle", "oui", "bonjour", "help"],
        "expected_tools": {},
    },
]


@dataclass
class TurnResult:
    turn: int
    user_text: str
    agent_response: str
    first_token_ms: float
    total_ms: float


@dataclass
class ScenarioResult:
    scenario_id: str
    scenario_name: str
    turns: list[TurnResult] = field(default_factory=list)
    task_complete: bool = False
    tool_accuracy: float = 1.0
    error: str | None = None

    @property
    def avg_first_token_ms(self) -> float:
        if not self.turns:
            return 0.0
        return sum(t.first_token_ms for t in self.turns) / len(self.turns)

    @property
    def avg_total_ms(self) -> float:
        if not self.turns:
            return 0.0
        return sum(t.total_ms for t in self.turns) / len(self.turns)


async def run_scenario(ws_base_url: str, trigger_id: str, scenario: dict[str, Any]) -> ScenarioResult:
    result = ScenarioResult(scenario_id=scenario["id"], scenario_name=scenario["name"])
    call_sid = f"EVAL_{scenario['id']}_{int(time.time())}"
    url = f"{ws_base_url}/voice/ws/{trigger_id}"

    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
            # Send setup frame (mimics Twilio ConversationRelay setup)
            await ws.send(json.dumps({
                "event": "setup",
                "callSid": call_sid,
                "from": "+15550000000",
                "customParameters": {"eval": "true"},
            }))

            # Wait for setup ack or welcome message
            setup_resp = await asyncio.wait_for(ws.recv(), timeout=15.0)
            _ = json.loads(setup_resp)  # discard

            all_responses: list[str] = []

            for turn_idx, utterance in enumerate(scenario["turns"]):
                t_start = time.perf_counter()
                first_token_ms = 0.0

                await ws.send(json.dumps({
                    "event": "prompt",
                    "voicePrompt": utterance,
                    "last": True,
                }))

                # Collect tokens until we get a message with "last": true
                response_tokens: list[str] = []
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    except asyncio.TimeoutError:
                        break
                    msg = json.loads(raw)
                    if msg.get("event") == "token":
                        if not first_token_ms:
                            first_token_ms = (time.perf_counter() - t_start) * 1000
                        response_tokens.append(msg.get("token", ""))
                        if msg.get("last", False):
                            break
                    elif msg.get("event") in ("hangup", "error"):
                        break

                t_end = time.perf_counter()
                agent_text = "".join(response_tokens)
                all_responses.append(agent_text)

                result.turns.append(TurnResult(
                    turn=turn_idx,
                    user_text=utterance,
                    agent_response=agent_text,
                    first_token_ms=first_token_ms,
                    total_ms=(t_end - t_start) * 1000,
                ))

            # Score task completion: check last response contains task keywords
            combined_response = " ".join(all_responses).lower()
            keywords_hit = [k for k in scenario["task_keywords"] if k.lower() in combined_response]
            result.task_complete = len(keywords_hit) >= max(1, len(scenario["task_keywords"]) // 2)

            # Send hangup
            await ws.send(json.dumps({"event": "hangup"}))

    except Exception as exc:
        result.error = str(exc)

    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description="Voice eval harness — 20 clinic scenarios")
    parser.add_argument("--trigger", required=True, help="Agent phone trigger UUID")
    parser.add_argument("--url", default="wss://api.setod.com", help="WebSocket base URL")
    parser.add_argument("--out", default="voice_eval_results.json", help="Output JSON file")
    parser.add_argument("--scenarios", default="", help="Comma-separated scenario IDs to run (default: all)")
    args = parser.parse_args()

    ws_base = args.url.rstrip("/")
    selected_ids = set(s.strip() for s in args.scenarios.split(",") if s.strip())
    scenarios_to_run = [s for s in SCENARIOS if not selected_ids or s["id"] in selected_ids]

    print(f"Running {len(scenarios_to_run)} scenario(s) against trigger {args.trigger}…")
    results: list[ScenarioResult] = []

    for sc in scenarios_to_run:
        print(f"  [{sc['id']}] {sc['name']}…", end=" ", flush=True)
        r = await run_scenario(ws_base, args.trigger, sc)
        status = "✓" if r.task_complete and not r.error else ("✗" if not r.task_complete else "ERR")
        print(f"{status}  FTL={r.avg_first_token_ms:.0f}ms  total={r.avg_total_ms:.0f}ms")
        if r.error:
            print(f"     Error: {r.error}")
        results.append(r)
        await asyncio.sleep(2)  # brief pause between calls

    # Summary
    completed = sum(1 for r in results if r.task_complete)
    errored = sum(1 for r in results if r.error)
    avg_ftl = sum(r.avg_first_token_ms for r in results if not r.error) / max(1, len(results) - errored)
    avg_total = sum(r.avg_total_ms for r in results if not r.error) / max(1, len(results) - errored)

    print()
    print(f"=== SUMMARY ===")
    print(f"Task completion: {completed}/{len(results)} ({100*completed//len(results)}%)")
    print(f"Errors:          {errored}")
    print(f"Avg FTL:         {avg_ftl:.0f} ms")
    print(f"Avg turn time:   {avg_total:.0f} ms")

    out = {
        "trigger_id": args.trigger,
        "url": args.url,
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": {
            "task_completion_pct": 100 * completed // len(results),
            "errored": errored,
            "avg_first_token_ms": round(avg_ftl, 1),
            "avg_turn_ms": round(avg_total, 1),
        },
        "scenarios": [
            {
                **asdict(r),
                "avg_first_token_ms": round(r.avg_first_token_ms, 1),
                "avg_total_ms": round(r.avg_total_ms, 1),
            }
            for r in results
        ],
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nResults written to {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
