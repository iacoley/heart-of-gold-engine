"""
Tests for mcp/tools-server.py skill discovery (issues #83, #84).

#83: discover_skills() must actually find the shipped example skill, and
     the server that hosts it must be registered where Claude Code reads
     config from (covered by test_admin_mcp.py::test_mcp_config_at_repo_root).
#84: a skill directory with a SKILL.md but no tools.json (the frontmatter
     Agent Skills convention) must produce a startup diagnostic naming the
     file, instead of being silently invisible.
"""

import json

import pytest

from conftest import import_script, PACKAGE_ROOT


@pytest.fixture
def tools_server(monkeypatch, tmp_path):
    """Import tools-server.py with WORKSPACE_ROOT pointed at a scratch dir."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    return import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")


def test_shipped_hello_world_skill_is_discoverable():
    """The package's only shipped skill must live where discover_skills()
    actually looks — one level under skills/ (skills/<name>/tools.json).

    Regression for #83: it used to ship at skills/examples/hello-world/,
    two levels deep, invisible to the one-level scan.
    """
    tools_json = PACKAGE_ROOT / "skills" / "hello-world" / "tools.json"
    assert tools_json.exists(), (
        "expected skills/hello-world/tools.json — the example skill must "
        "sit one level under skills/ to match discover_skills()'s scan depth"
    )
    assert not (PACKAGE_ROOT / "skills" / "examples").exists(), (
        "skills/examples/ should be gone now that hello-world moved up a level"
    )


def test_discover_skills_finds_real_hello_world(tools_server, monkeypatch):
    """discover_skills() against the real repo layout must surface hello_world."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(PACKAGE_ROOT))
    mod = import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")
    tools = mod.discover_skills()
    names = [t["name"] for t in tools]
    assert "hello_world" in names


def test_discover_skills_loads_tools_json_skill(tools_server, tmp_path):
    """A normal skills/<name>/tools.json skill is discovered and dispatchable."""
    skill_dir = tmp_path / "skills" / "my-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "tools.json").write_text(json.dumps({
        "skill_name": "my-skill",
        "version": "1.0.0",
        "description": "test skill",
        "tools": [{
            "name": "my_tool",
            "description": "does a thing",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        }],
    }))

    tools = tools_server.discover_skills()
    assert [t["name"] for t in tools] == ["my_tool"]
    assert tools[0]["_skill_dir"] == str(skill_dir)


def test_frontmatter_only_skill_is_skipped_with_diagnostic(tools_server, tmp_path, capsys):
    """Issue #84: a SKILL.md with no tools.json must not vanish silently —
    stderr must name the specific file and say why it was skipped."""
    skill_dir = tmp_path / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: foo\ndescription: a frontmatter-style skill\n---\n\nBody text.\n"
    )

    tools = tools_server.discover_skills()

    assert tools == []
    err = capsys.readouterr().err
    assert str(skill_md) in err
    assert "tools.json" in err


def test_skill_dir_with_neither_file_is_silently_ignored(tools_server, tmp_path, capsys):
    """A directory under skills/ with no tools.json and no SKILL.md (e.g. an
    organizational folder like the old skills/examples/) isn't a skill at
    all, so it must not be flagged as one."""
    (tmp_path / "skills" / "not-a-skill").mkdir(parents=True)

    tools = tools_server.discover_skills()

    assert tools == []
    assert capsys.readouterr().err == ""


class TestPathTraversalValidation:
    """task-1788231060: the old check (bare `".." in value`) false-positived
    on ordinary prose twice in one night — no tool schema in this repo has
    ever set path_mode, so the intended opt-out was dead code and every
    string field on every tool got the path-traversal check whether or not
    it was ever meant to hold a path. Tightened to require ".." to actually
    be acting as a path segment (adjacent to a separator, or the whole
    value)."""

    SCHEMA = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    }

    def test_ordinary_prose_with_two_dots_is_not_flagged(self, tools_server):
        # The exact shape of the real 2026-09-01 false positives: a
        # filename's own dot butting up against a sentence-ending period,
        # and an informal ".." used where an ellipsis belongs.
        assert tools_server.validate_args(
            {"content": "see reply_gate.py.. still not sure"}, self.SCHEMA
        ) is None
        assert tools_server.validate_args(
            {"content": "closes task-1788231060.. finally"}, self.SCHEMA
        ) is None

    def test_real_traversal_segment_is_still_flagged(self, tools_server):
        error = tools_server.validate_args(
            {"content": "../../config/agents.json"}, self.SCHEMA
        )
        assert error is not None
        assert "path traversal" in error

    def test_bare_dotdot_segment_is_still_flagged(self, tools_server):
        error = tools_server.validate_args({"content": ".."}, self.SCHEMA)
        assert error is not None

    def test_traversal_inside_a_longer_path_is_still_flagged(self, tools_server):
        error = tools_server.validate_args(
            {"content": "data/foo/../../secrets.env"}, self.SCHEMA
        )
        assert error is not None

    def test_path_mode_absolute_still_opts_out(self, tools_server):
        schema = {
            "type": "object",
            "properties": {"content": {"type": "string", "path_mode": "absolute"}},
            "required": ["content"],
        }
        assert tools_server.validate_args(
            {"content": "../../config/agents.json"}, schema
        ) is None


class TestTaskboardUpdate:
    """taskboard's 'update' action was advertised in the tool schema
    (description + inputSchema enum) but never implemented in
    handle_core_tool — any call fell through to the generic
    'Unknown tool or action: taskboard' error, indistinguishable from
    the tool itself being missing. Regression coverage for the fix."""

    def _add_task(self, tools_server, title="a task"):
        # taskboard's "add" doesn't mkdir data/ itself — it relies on the
        # real WORKSPACE_ROOT already having one. Match that expectation
        # here rather than papering over it in the tool.
        (tools_server.WORKSPACE / "data").mkdir(parents=True, exist_ok=True)
        result = tools_server.handle_core_tool("taskboard", {"action": "add", "title": title})
        return result["task"]["id"]

    def test_update_changes_status(self, tools_server):
        task_id = self._add_task(tools_server)

        result = tools_server.handle_core_tool(
            "taskboard", {"action": "update", "id": task_id, "status": "in_progress"}
        )

        assert result["task"]["status"] == "in_progress"
        assert "error" not in result

        listed = tools_server.handle_core_tool("taskboard", {"action": "list"})
        task = next(t for t in listed["tasks"] if t["id"] == task_id)
        assert task["status"] == "in_progress"

    def test_update_missing_id_is_an_error_not_a_crash(self, tools_server):
        result = tools_server.handle_core_tool(
            "taskboard", {"action": "update", "status": "in_progress"}
        )
        assert "error" in result

    def test_update_missing_status_is_an_error_not_a_crash(self, tools_server):
        task_id = self._add_task(tools_server)
        result = tools_server.handle_core_tool(
            "taskboard", {"action": "update", "id": task_id}
        )
        assert "error" in result

    def test_update_unknown_task_id_reports_not_found(self, tools_server):
        result = tools_server.handle_core_tool(
            "taskboard", {"action": "update", "id": "task-doesnotexist", "status": "done"}
        )
        assert "error" in result
        assert "task-doesnotexist" in result["error"]

    def test_unknown_taskboard_action_names_the_action_not_the_tool(self, tools_server):
        """The pre-fix fallback error ('Unknown tool or action: taskboard')
        read as if the whole tool was missing. It should name the bad
        action instead, scoped to the taskboard branch."""
        result = tools_server.handle_core_tool("taskboard", {"action": "bogus"})
        assert "error" in result
        assert "taskboard" not in result["error"] or "bogus" in result["error"]
        assert "bogus" in result["error"]


class TestTaskboardEmailIntakeMarkRead:
    """Completing a task created by read_marvin_folder.py's email-intake
    (source="email-intake", email_uid=<uid>) must flip that message's
    \\Seen flag via mark_email_read.py — added 2026-09-01 so 'make sure
    emails get addressed, not just noticed' has a closing half, not just
    the task-creation half."""

    def _add_task(self, tools_server, **extra):
        (tools_server.WORKSPACE / "data").mkdir(parents=True, exist_ok=True)
        result = tools_server.handle_core_tool("taskboard", {"action": "add", "title": "t"})
        task_id = result["task"]["id"]
        if extra:
            tasks_file = tools_server.WORKSPACE / "data" / "taskboard.json"
            data = json.loads(tasks_file.read_text())
            for task in data["tasks"]:
                if task["id"] == task_id:
                    task.update(extra)
            tasks_file.write_text(json.dumps(data))
        return task_id

    def _capture_subprocess(self, tools_server, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append({"cmd": cmd, "env": kwargs.get("env", {}), "cwd": kwargs.get("cwd")})
            class Result:
                returncode = 0
                stdout = "{}"
                stderr = ""
            return Result()

        monkeypatch.setattr(tools_server.subprocess, "run", fake_run)
        return calls

    def test_complete_on_email_intake_task_marks_it_read(self, tools_server, monkeypatch):
        calls = self._capture_subprocess(tools_server, monkeypatch)
        task_id = self._add_task(tools_server, source="email-intake", email_uid=42)

        result = tools_server.handle_core_tool("taskboard", {"action": "complete", "id": task_id})

        assert result["task"]["status"] == "done"
        assert len(calls) == 1
        assert calls[0]["cmd"][-1].endswith("mark_email_read.py")
        sent_args = json.loads(calls[0]["env"]["TOOL_ARGS"])
        assert sent_args == {"uid": 42, "read": True}

    def test_update_to_done_on_email_intake_task_marks_it_read(self, tools_server, monkeypatch):
        calls = self._capture_subprocess(tools_server, monkeypatch)
        task_id = self._add_task(tools_server, source="email-intake", email_uid=7)

        tools_server.handle_core_tool(
            "taskboard", {"action": "update", "id": task_id, "status": "done"}
        )

        assert len(calls) == 1
        assert json.loads(calls[0]["env"]["TOOL_ARGS"]) == {"uid": 7, "read": True}

    def test_re_updating_an_already_done_email_task_does_not_re_fire(self, tools_server, monkeypatch):
        """Guards against re-marking (and re-hitting the real IMAP call)
        every time an already-completed task gets touched again."""
        calls = self._capture_subprocess(tools_server, monkeypatch)
        task_id = self._add_task(tools_server, source="email-intake", email_uid=7, status="done")

        tools_server.handle_core_tool(
            "taskboard", {"action": "update", "id": task_id, "status": "done"}
        )

        assert calls == []

    def test_completing_a_non_email_task_does_not_touch_gmail(self, tools_server, monkeypatch):
        calls = self._capture_subprocess(tools_server, monkeypatch)
        task_id = self._add_task(tools_server)

        tools_server.handle_core_tool("taskboard", {"action": "complete", "id": task_id})

        assert calls == []

    def test_mark_read_failure_does_not_block_task_completion(self, tools_server, monkeypatch):
        def raising_run(*a, **k):
            raise OSError("no such process")

        monkeypatch.setattr(tools_server.subprocess, "run", raising_run)
        task_id = self._add_task(tools_server, source="email-intake", email_uid=1)

        result = tools_server.handle_core_tool("taskboard", {"action": "complete", "id": task_id})

        assert result["task"]["status"] == "done"
        assert "error" not in result
