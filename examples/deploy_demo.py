"""Plan deployment without writing or contacting the service manager.

Calendar validation uses systemd-analyze, which does not need a user session.

    uv run python examples/deploy_demo.py

Uses only the library API, as a UI would.
"""

from pathlib import Path

import floe


def main():
    workflow = floe.load(Path(__file__).resolve().parent / "workflow.py")
    plan = floe.plan_install(floe.Deployment.for_workflow(workflow), floe.UnitStore(floe.user_unit_dir()))
    for change in plan.changes:
        print(f"# {change.name}: {change.action}\n{change.text}")


if __name__ == "__main__":
    main()
