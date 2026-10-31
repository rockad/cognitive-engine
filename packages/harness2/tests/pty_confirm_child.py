"""The child of the pty test (test_stage1_approval_pty.py): asks for an approval exactly as `stage1 run` does, on the
process's real stdin, stdout and controlling terminal. Exit 0 = approved, 3 = typed wrong, 4 = refused (the reason is
printed). Not a test module."""
import os
import sys

from cognitive_harness2.stage1 import ApprovalRefused, confirm_on_terminal

try:
    ok = confirm_on_terminal("approve local-ollama stage1-pty", environ=os.environ)
except ApprovalRefused as e:
    print(f"REFUSED: {e}", flush=True)
    sys.exit(4)
print("APPROVED" if ok else "NOT-APPROVED", flush=True)
sys.exit(0 if ok else 3)
