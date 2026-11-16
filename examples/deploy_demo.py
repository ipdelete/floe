"""Render the repository workflow's units without writing or calling systemd.

    uv run python examples/deploy_demo.py
"""

from pathlib import Path

from floe.deploy import Deployment


def main():
    deployment = Deployment(
        workflow="mydag",
        script=Path(__file__).resolve().parents[1] / "workflow.py",
        schedule=("hourly",),
    )
    for name, text in deployment.render_units().items():
        print(f"# {name}\n{text}")


if __name__ == "__main__":
    main()
