from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from supabase import create_client

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(BACKEND_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR / "scripts"))

from app.core.config import settings
from final_gate_scenarios import gate_ids, member_email_for
from run_final_internal_gate import (
    DEFAULT_EMAIL,
    DEFAULT_WORKSPACE_ID,
    delete_ephemeral_auth_user,
    ensure_ephemeral_auth_user,
    run_captured_process,
)


def _print_process(proc: subprocess.CompletedProcess[str], limit: int = 12000) -> None:
    if proc.stdout:
        print(proc.stdout[-limit:])
    if proc.stderr:
        print(proc.stderr[-limit:])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run only the Linka Final Gate frontend Playwright E2E."
    )
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE_ID)
    parser.add_argument("--frontend-url", default="http://127.0.0.1:3000")
    parser.add_argument("--keep-fixtures", action="store_true")
    args = parser.parse_args()

    print("=== Linka FRONTEND E2E FOCUSED GATE ===")

    if settings.is_production:
        print("[FAIL] Refusing to create test fixtures in production.")
        return 2
    if not settings.supabase_secret_key:
        print("[FAIL] SUPABASE_SECRET_KEY is required server-side.")
        return 2

    frontend_dir = PROJECT_DIR / "frontend"
    if not frontend_dir.exists():
        print("[FAIL] frontend directory is missing.")
        return 2

    npm_executable = (
        shutil.which("npm.cmd") if os.name == "nt" else shutil.which("npm")
    ) or shutil.which("npm")
    if npm_executable is None:
        print("[FAIL] npm executable not found in PATH.")
        return 2

    password = input("Supabase admin password: ")
    member_password = secrets.token_urlsafe(28) + "A9!"
    member_email = member_email_for(args.email)
    workspace_id = UUID(args.workspace_id)
    ids = gate_ids(workspace_id)
    auth_user_id: str | None = None
    succeeded = False

    try:
        admin_auth = create_client(
            settings.supabase_url,
            settings.supabase_publishable_key,
        ).auth.sign_in_with_password(
            {"email": args.email, "password": password}
        )
        if admin_auth.session is None:
            print("[FAIL] Admin Supabase login returned no session.")
            return 2
        print("[PASS] Admin Supabase login")

        auth_user_id = ensure_ephemeral_auth_user(member_email, member_password)
        print("[PASS] Ephemeral real Supabase member identity")

        seed = run_captured_process(
            [
                sys.executable,
                str(BACKEND_DIR / "scripts" / "seed_final_internal_gate.py"),
                "--workspace-id",
                str(workspace_id),
                "--member-auth-user-id",
                auth_user_id,
                "--member-email",
                member_email,
            ],
            cwd=BACKEND_DIR,
        )
        if seed.returncode != 0:
            _print_process(seed)
            print(f"[FAIL] Seed Final Gate fixtures — exit={seed.returncode}")
            return 2
        print("[PASS] Seed Final Gate fixtures only")

        normalization = run_captured_process(
            [
                sys.executable,
                str(
                    BACKEND_DIR
                    / "scripts"
                    / "normalize_staging_fixture_contacts.py"
                ),
                "--workspace-id",
                str(workspace_id),
            ],
            cwd=BACKEND_DIR,
        )
        _print_process(normalization)
        if normalization.returncode != 0:
            print(
                "[WARN] Could not normalize legacy full-staging contacts. "
                "This does not affect the Final Gate/E2E fixtures."
            )

        quality = run_captured_process(
            [
                sys.executable,
                str(BACKEND_DIR / "scripts" / "validate_final_gate_fixture_quality.py"),
                "--workspace-id",
                str(workspace_id),
            ],
            cwd=BACKEND_DIR,
        )
        _print_process(quality)
        if quality.returncode != 0:
            print(f"[FAIL] Fixture data quality — exit={quality.returncode}")
            return 2

        member_auth = create_client(
            settings.supabase_url,
            settings.supabase_publishable_key,
        ).auth.sign_in_with_password(
            {"email": member_email, "password": member_password}
        )
        if member_auth.session is None:
            print("[FAIL] Member Supabase login returned no session.")
            return 2
        print("[PASS] Real member Supabase login")

        env = os.environ.copy()
        env.update(
            {
                "TIA_E2E_BASE_URL": args.frontend_url,
                "TIA_E2E_ADMIN_EMAIL": args.email,
                "TIA_E2E_ADMIN_PASSWORD": password,
                "TIA_E2E_MEMBER_EMAIL": member_email,
                "TIA_E2E_MEMBER_PASSWORD": member_password,
                "TIA_E2E_PRIMARY_WORKSPACE_ID": str(workspace_id),
                "TIA_E2E_SECONDARY_WORKSPACE_ID": str(ids["secondary_workspace"]),
                "NO_COLOR": "1",
            }
        )

        proc = run_captured_process(
            [npm_executable, "run", "test:e2e"],
            cwd=frontend_dir,
            env=env,
        )
        _print_process(proc)

        if proc.returncode != 0:
            print(f"[FAIL] Frontend Playwright E2E — exit={proc.returncode}")
            print("Fixtures kept for inspection.")
            return 2

        print("[PASS] Frontend Playwright E2E")
        succeeded = True
        return 0

    except Exception as exc:
        print(f"[FAIL] Focused frontend gate — {type(exc).__name__}: {exc}")
        print("Fixtures kept for inspection.")
        return 2
    finally:
        if succeeded and not args.keep_fixtures and auth_user_id is not None:
            cleanup = run_captured_process(
                [
                    sys.executable,
                    str(BACKEND_DIR / "scripts" / "cleanup_final_internal_gate.py"),
                    "--workspace-id",
                    str(workspace_id),
                    "--member-email",
                    member_email,
                ],
                cwd=BACKEND_DIR,
            )
            if cleanup.returncode == 0:
                try:
                    delete_ephemeral_auth_user(auth_user_id)
                    print("[PASS] Focused Final Gate fixtures cleaned")
                except Exception as exc:
                    print(
                        "[WARN] DB fixtures cleaned but ephemeral Supabase user "
                        f"could not be deleted: {type(exc).__name__}"
                    )
            else:
                _print_process(cleanup)
                print("[WARN] Focused fixture cleanup failed; fixtures were kept.")
        elif not succeeded:
            print("Focused fixtures kept because the E2E gate failed.")


if __name__ == "__main__":
    exit_code = main()
    raise SystemExit(exit_code)
