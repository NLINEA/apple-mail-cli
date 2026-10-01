#!/usr/bin/env python3
"""Temporary-fixture Foundation/CommonCrypto checks. Never opens Apple Mail."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "skills/apple-mail-cli/scripts"))
import send_guard

osascript = shutil.which("osascript")
if not osascript:
    raise SystemExit("This optional native protocol check requires macOS osascript.")
backend = (root / "skills/apple-mail-cli/scripts/mail_backend.js").read_text()
harness = r'''
function run() {
  ObjC.import("Foundation");
  var data = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
  var input = JSON.parse(ObjC.unwrap($.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding)));
  Application = function () { throw new Error("Real application access forbidden by this fixture"); };
  sendStateRoot = function () { return input.root; };
  var result = { text_hash: sha256Text(input.text), intent_hash: sha256Text(canonicalJson(sendIntent(input.payload))) };
  var changed = JSON.parse(JSON.stringify(input.payload));
  changed.body += "Changed";
  try { validateAndClaimSend(changed); result.drift_rejected = false; }
  catch (_) { result.drift_rejected = true; }
  try { result.claim = validateAndClaimSend(input.payload); result.accepted = true; }
  catch (error) { result.accepted = false; result.error = String(error.message); }
  try { validateAndClaimSend(input.payload); result.replay_rejected = false; }
  catch (_) { result.replay_rejected = true; }
  return JSON.stringify(result);
}
'''
checks = []
with tempfile.TemporaryDirectory(prefix="mailctl-native-send-protocol-") as temporary:
    owned = Path(temporary)
    script = owned / "native-fixture.js"
    script.write_text(backend + "\n" + harness)
    for case in ("exact", "snapshot_bytes", "missing_record", "permissions"):
        state = owned / case / "state"
        source = owned / (case + ".pdf")
        source.write_bytes(b"approved")
        evidence = [{"name": source.name, "size_bytes": 8, "sha256": hashlib.sha256(b"approved").hexdigest(), "path": str(source)}]
        payload = {"command": "send", "sender": "sender@example.test", "to": ["recipient@example.test"], "cc": [], "bcc": [], "subject": "香港 café", "body": "Final body\n\n"}
        intent = send_guard.canonical_intent(payload, evidence)
        digest = send_guard.intent_digest(intent)
        with mock.patch.object(send_guard, "state_root", return_value=state):
            attempt = send_guard.reserve_attempt(digest, evidence)
        payload.update(confirm_send=digest, attachments=attempt.snapshots, attachment_evidence=intent["attachments"])
        if case == "snapshot_bytes":
            snapshot = Path(attempt.snapshots[0])
            snapshot.chmod(0o600)
            snapshot.write_bytes(b"altered!")
            snapshot.chmod(0o400)
        if case == "missing_record":
            (attempt.directory / "attempt.json").unlink()
        if case == "permissions":
            state.chmod(0o755)
        text = "abc 香港 café"
        request = {"root": str(state), "payload": payload, "text": text}
        process = subprocess.run([osascript, "-l", "JavaScript", str(script)], input=json.dumps(request, ensure_ascii=False), text=True, encoding="utf-8", capture_output=True, timeout=15)
        if process.returncode:
            raise SystemExit("Synthetic native protocol fixture failed: " + process.stderr.strip())
        result = json.loads(process.stdout)
        assert result["text_hash"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert result["intent_hash"] == digest
        assert result["drift_rejected"] is True
        assert result["accepted"] is (case == "exact"), result
        assert result["replay_rejected"] is True
        checks.append({"case": case, "accepted": result["accepted"], "intent_matches_python": True, "drift_rejected": True, "replay_rejected": True})
print(json.dumps({"native_protocol_cases": len(checks), "checks": checks, "foundation_commoncrypto": True, "temporary_state_root_injected": True, "mail_loaded": False, "native_Mail_send_behavior_validated": False}))
