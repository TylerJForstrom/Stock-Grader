"""Expectation clocks for the evidence loop: cadence.py + workflow wiring.

Three contracts are pinned here:

1. The clock semantics — jitter-tolerant day-of-month expectations,
   bootstrap-guarded, previous-month self-gate mode.
2. The accounting artifact — written for EVERY profile every run (refused
   states included), append-per-run inside a month, refuse-not-clobber.
3. The cross-repo execution contract — Stock-Vault's shadow-arms workflow
   runs cadence.py as a bare uninstalled script, so the module must stay
   stdlib-only with no relative imports.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

from stock_grader import cadence, cli

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "monthly-forward-backtest.yml"


def _repo(
    tmp_path: Path,
    *,
    freezes: dict[str, list[str]] | None = None,
    accounting_months: list[str] | None = None,
) -> Path:
    """A minimal Stock-Grader layout: dated freezes + accounted months."""
    root = tmp_path / "repo"
    for profile, dates in (freezes or {}).items():
        pdir = root / "frozen_scores" / profile
        pdir.mkdir(parents=True, exist_ok=True)
        for day in dates:
            (pdir / f"{day}.parquet").write_bytes(b"")
    for month in accounting_months or []:
        mdir = root / "docs" / "forward" / month
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "accounting.json").write_text(
            json.dumps({"schema_version": "1.0", "month": month, "runs": []}),
            encoding="utf-8",
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------- clock logic


def test_expected_month_is_previous_before_grace_day_and_current_after() -> None:
    assert cadence.expected_month(dt.date(2026, 8, 3), 4) == "2026-07"
    assert cadence.expected_month(dt.date(2026, 8, 4), 4) == "2026-08"
    # January rolls back across the year boundary.
    assert cadence.expected_month(dt.date(2026, 1, 2), 4) == "2025-12"


def test_bootstrap_passes_with_notes_when_nothing_exists(tmp_path) -> None:
    root = _repo(tmp_path)
    ok, lines = cadence.check_cadence(root, dt.date(2026, 8, 20))
    assert ok
    assert sum("BOOTSTRAP" in line for line in lines) == 2


def test_freeze_clock_tolerates_jitter_then_demands_the_current_month(tmp_path) -> None:
    root = _repo(tmp_path, freezes={"all_weather": ["2026-07-01"]})
    # Day 3: the current month's freeze may not have run yet.
    ok, _ = cadence.check_cadence(root, dt.date(2026, 8, 3))
    assert ok
    # Day 4: the July freeze is now a missed cadence.
    ok, lines = cadence.check_cadence(root, dt.date(2026, 8, 4))
    assert not ok
    assert any(line.startswith("FAIL freeze") for line in lines)

    fresh = _repo(tmp_path / "b", freezes={"all_weather": ["2026-08-01"]})
    ok, _ = cadence.check_cadence(fresh, dt.date(2026, 8, 31))
    assert ok


def test_freeze_clock_reads_dated_filenames_not_catalog_files(tmp_path) -> None:
    """manifest.json and stray files must not register as freezes."""
    root = _repo(tmp_path, freezes={"all_weather": ["2026-07-01"]})
    profile_dir = root / "frozen_scores" / "all_weather"
    (profile_dir / "manifest.json").write_text("{}", encoding="utf-8")
    (profile_dir / "notes.parquet").write_bytes(b"")  # not a dated panel
    assert cadence.newest_freeze_month(root / "frozen_scores") == "2026-07"


def test_accounting_clock_grace_day_boundary(tmp_path) -> None:
    root = _repo(tmp_path, accounting_months=["2026-07"])
    # Day 7: last month's artifact still satisfies the clock.
    ok, _ = cadence.check_cadence(root, dt.date(2026, 8, 7))
    assert ok
    # Day 8: the current month's run (the 6th) is overdue.
    ok, lines = cadence.check_cadence(root, dt.date(2026, 8, 8))
    assert not ok
    assert any(line.startswith("FAIL accounting") for line in lines)


def test_pre_run_holds_the_accounting_clock_to_the_previous_month(tmp_path) -> None:
    """The self-gate must not demand the artifact the gated run itself writes."""
    root = _repo(tmp_path, accounting_months=["2026-07"])
    ok, _ = cadence.check_cadence(root, dt.date(2026, 8, 20), pre_run=True)
    assert ok
    # A missed month is still caught: July absent, gate red.
    bare = _repo(tmp_path / "b", accounting_months=["2026-06"])
    ok, lines = cadence.check_cadence(bare, dt.date(2026, 8, 6), pre_run=True)
    assert not ok
    assert any(line.startswith("FAIL accounting") for line in lines)


def test_month_dir_without_accounting_json_does_not_count(tmp_path) -> None:
    root = _repo(tmp_path, accounting_months=["2026-06"])
    reports_only = root / "docs" / "forward" / "2026-07"
    reports_only.mkdir(parents=True)
    (reports_only / "all_weather.md").write_text("report", encoding="utf-8")
    assert cadence.newest_accounting_month(root / "docs" / "forward") == "2026-06"


# ------------------------------------------------------- accounting artifact


def test_accounting_records_every_state_including_refusals(tmp_path) -> None:
    states = {
        "all_weather": "evaluated",
        "deep_value": "not_matured",
        "growth": "refused_build",
        "value": "refused_declare",
        "quality": "refused_backtest",
    }
    path = cadence.write_accounting(
        tmp_path,
        "2026-08",
        states,
        event="schedule",
        run_id="123",
        now=dt.datetime(2026, 8, 6, 2, 41, tzinfo=dt.UTC),
    )
    assert path == tmp_path / "2026-08" / "accounting.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == cadence.ACCOUNTING_SCHEMA_VERSION
    assert payload["month"] == "2026-08"
    (run,) = payload["runs"]
    assert run["profiles"] == states
    assert run["counts"] == {"evaluated": 1, "not_matured": 1, "refused": 3}
    assert run["event"] == "schedule"
    assert run["run_id"] == "123"
    assert run["run_utc"] == "2026-08-06T02:41:00Z"


def test_accounting_appends_runs_instead_of_erasing_them(tmp_path) -> None:
    cadence.write_accounting(tmp_path, "2026-08", {"all_weather": "not_matured"})
    cadence.write_accounting(tmp_path, "2026-08", {"all_weather": "evaluated"})
    payload = json.loads((tmp_path / "2026-08" / "accounting.json").read_text(encoding="utf-8"))
    assert [run["profiles"]["all_weather"] for run in payload["runs"]] == [
        "not_matured",
        "evaluated",
    ]


def test_accounting_refuses_rather_than_clobbers(tmp_path) -> None:
    month_dir = tmp_path / "2026-08"
    month_dir.mkdir()
    artifact = month_dir / "accounting.json"

    artifact.write_text("not json", encoding="utf-8")
    with pytest.raises(cadence.CadenceError, match="does not parse"):
        cadence.write_accounting(tmp_path, "2026-08", {"a": "evaluated"})

    artifact.write_text(
        json.dumps({"schema_version": "1.0", "month": "2026-07", "runs": []}),
        encoding="utf-8",
    )
    with pytest.raises(cadence.CadenceError, match="month"):
        cadence.write_accounting(tmp_path, "2026-08", {"a": "evaluated"})

    artifact.write_text(
        json.dumps({"schema_version": "9.9", "month": "2026-08", "runs": []}),
        encoding="utf-8",
    )
    with pytest.raises(cadence.CadenceError, match="schema_version"):
        cadence.write_accounting(tmp_path, "2026-08", {"a": "evaluated"})


def test_accounting_refuses_bad_inputs(tmp_path) -> None:
    with pytest.raises(cadence.CadenceError, match="YYYY-MM"):
        cadence.write_accounting(tmp_path, "2026-13", {"a": "evaluated"})
    with pytest.raises(cadence.CadenceError, match="empty loop"):
        cadence.write_accounting(tmp_path, "2026-08", {})
    with pytest.raises(cadence.CadenceError, match="unknown state"):
        cadence.write_accounting(tmp_path, "2026-08", {"a": "maybe_later"})


def test_states_file_parser_is_strict(tmp_path) -> None:
    good = tmp_path / "states.tsv"
    good.write_text(
        "# comment\nall_weather\tevaluated\ndeep_value\trefused_build\n",
        encoding="utf-8",
    )
    assert cadence.read_states_file(good) == {
        "all_weather": "evaluated",
        "deep_value": "refused_build",
    }

    for bad_body, message in [
        ("all_weather evaluated\n", "not '<profile>"),
        ("all_weather\twat\n", "unknown state"),
        ("a\tevaluated\na\tevaluated\n", "duplicate profile"),
    ]:
        bad = tmp_path / "bad.tsv"
        bad.write_text(bad_body, encoding="utf-8")
        with pytest.raises(cadence.CadenceError, match=message):
            cadence.read_states_file(bad)


# ---------------------------------------------------------------------- CLI


def test_check_cadence_cli_exit_codes(tmp_path) -> None:
    root = _repo(tmp_path, freezes={"all_weather": ["2026-07-01"]})
    passing = ["check-cadence", "--repo-root", str(root), "--as-of", "2026-08-03"]
    failing = ["check-cadence", "--repo-root", str(root), "--as-of", "2026-08-04"]
    assert cli.main(passing) == 0
    assert cli.main(failing) == 1
    assert cli.main(["check-cadence", "--repo-root", str(root), "--as-of", "nope"]) == 2


def test_forward_accounting_cli_writes_refused_states(tmp_path) -> None:
    states = tmp_path / "states.tsv"
    states.write_text("growth\trefused_build\nvalue\tnot_matured\n", encoding="utf-8")
    out = tmp_path / "forward"
    assert (
        cli.main(
            [
                "forward-accounting",
                "--month",
                "2026-08",
                "--states",
                str(states),
                "--out",
                str(out),
                "--event",
                "workflow_dispatch",
                "--run-id",
                "42",
            ]
        )
        == 0
    )
    payload = json.loads((out / "2026-08" / "accounting.json").read_text(encoding="utf-8"))
    (run,) = payload["runs"]
    assert run["profiles"] == {"growth": "refused_build", "value": "not_matured"}
    assert run["counts"]["refused"] == 1
    assert run["event"] == "workflow_dispatch"

    states.write_text("growth\twat\n", encoding="utf-8")
    assert (
        cli.main(
            ["forward-accounting", "--month", "2026-08", "--states", str(states), "--out", str(out)]
        )
        == 2
    )


# ------------------------------------------------- cross-repo script contract


def test_cadence_module_is_stdlib_only_with_no_relative_imports() -> None:
    """Stock-Vault runs this file directly from a bare checkout — a non-stdlib
    or relative import would break a sibling repo's CI, not this one's."""
    tree = ast.parse(
        (REPO_ROOT / "src" / "stock_grader" / "cadence.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in sys.stdlib_module_names, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import would break bare-script execution"
            assert node.module is not None
            assert node.module.split(".")[0] in sys.stdlib_module_names, node.module


def test_cadence_runs_as_a_bare_uninstalled_script(tmp_path) -> None:
    """The exact invocation shadow-arms.yml uses, against an empty checkout."""
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "src" / "stock_grader" / "cadence.py"),
            "check",
            "--repo-root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("BOOTSTRAP") == 2


# ------------------------------------------------------------ workflow wiring


def test_workflow_accounts_unconditionally_after_the_loop() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    build_step = workflow.split("- name: Build and evaluate every profile", 1)[1]
    build_step = build_step.split("- name: Commit archived panels", 1)[0]

    # Every branch of the loop records a state...
    for state in cadence.VALID_STATES:
        assert f"\\t{state}\\n" in build_step, state
    # ...into a file that is truncated at the top of every run...
    assert ': > "$STATES"' in build_step
    # ...and the accounting write sits after the loop, before the exit, so it
    # runs on nothing-matured months and on refused months alike.
    done = build_step.index("\n          done\n")
    accounting = build_step.index("stock-grader forward-accounting")
    exit_line = build_step.index("exit $failed")
    assert done < accounting < exit_line
    assert "--event" in build_step and "--run-id" in build_step
    # An accounting refusal is a red run, not a silent skip.
    assert "forward-accounting refused" in build_step


def test_workflow_self_gates_on_the_previous_month() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    gate = workflow.find("stock-grader check-cadence --repo-root . --pre-run")
    chain = workflow.find("- name: Refuse to append to a broken ledger chain")
    build = workflow.find("- name: Build and evaluate every profile")
    assert gate != -1 and chain != -1 and build != -1
    assert chain < gate < build

    # The gate must page (fail the job), never be swallowed.
    gate_step = workflow.split("- name: Cadence self-gate", 1)[1]
    gate_step = gate_step.split("      - name: ", 1)[0]
    assert "continue-on-error" not in gate_step

    # The build step self-heals past a red cadence gate but never past a
    # broken ledger chain.
    assert "id: chain" in workflow
    build_step = workflow.split("- name: Build and evaluate every profile", 1)[1]
    assert "if: ${{ !cancelled() && steps.chain.outcome == 'success' }}" in build_step


def test_workflow_keeps_its_action_sha_pins_and_commit_paths() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    checkout_pin = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
    python_pin = "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
    assert workflow.count(checkout_pin) == 3  # self + vault + foundry
    assert workflow.count(python_pin) == 1
    assert "uses: actions/checkout@v" not in workflow  # no pin regressions
    # The public commit step still publishes docs/forward (accounting included)
    # and never the gitignored build/ directory.
    assert "git add -A research_ledger.jsonl docs/forward" in workflow
    assert "git add -A build" not in workflow
