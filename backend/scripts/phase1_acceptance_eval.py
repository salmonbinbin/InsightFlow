"""独立临时库真实评测；仅包装现有 evaluator 记录逐调用证据，不修改业务。"""

import hashlib
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pgserver

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import run_classification_eval as evaluator  # noqa: E402

from app.services import classification  # noqa: E402
from app.services.llm_client import LLMClient  # noqa: E402

OUT = ROOT.parent / "work" / "phase1-acceptance"
calls = []
original_call = LLMClient.classify_batch


def observed(self, items, max_tokens=4096):
    start = time.monotonic()
    event = {
        "ids": [i["feedback_id"] for i in items],
        "started": datetime.now(timezone.utc).isoformat(),
    }
    try:
        result = original_call(self, items, max_tokens)
        event["usage"] = {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        }
        try:
            classification.validate_batch_output(result.payload, event["ids"])
            event["schema_and_ids_valid"] = True
            json.dumps(result.payload, allow_nan=False)
            event["strict_contract_valid"] = all(
                isinstance(row.get("pain_point"), str) for row in result.payload["results"]
            )
            event["raw_pain_point_types"] = [
                type(row.get("pain_point")).__name__ for row in result.payload["results"]
            ]
        except Exception as exc:
            event["schema_and_ids_valid"] = False
            event["validation_error_type"] = type(exc).__name__
        return result
    except Exception as exc:
        event["error_type"] = type(exc).__name__
        raise
    finally:
        event["latency_ms"] = round((time.monotonic() - start) * 1000)
        calls.append(event)


LLMClient.classify_batch = observed
real_server = pgserver.get_server
with tempfile.TemporaryDirectory(prefix="insightflow-real-eval-") as tmp:

    def isolated_server(*args, **kwargs):
        return real_server(Path(tmp) / "postgres", cleanup_mode="stop")

    pgserver.get_server = isolated_server
    evaluator.main()
report = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "calls": calls,
    "dataset_sha256": {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT.parent / "data").glob("*.csv")
    },
    "labels_status": "NOT human-reviewed; initial reference set",
    "intent_accuracy": None,
    "intent_limitation": "没有受控词表或人工判分标准，本轮未评估",
    "strict_contract_valid_calls": sum(c.get("strict_contract_valid", False) for c in calls),
    "schema_valid_calls": sum(c.get("schema_and_ids_valid", False) for c in calls),
    "total_calls": len(calls),
    "call_errors": sum("error_type" in c for c in calls),
    "protocol_errors": sum(c.get("error_type") == "LLMProtocolError" for c in calls),
    "actual_attempt_latency_ms": sum(c["latency_ms"] for c in calls),
}
(OUT / "real-eval-attempts.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(report, ensure_ascii=False, indent=2))
