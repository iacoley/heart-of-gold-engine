"""Call-time GitHub token delivery (issue #30): helper, gh shim, env wiring."""
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import import_script

BIN = Path(__file__).parent.parent / "bin"
HELPER = BIN / "git-credential-karakos"
SHIM_DIR = BIN / "shims"
SECRET = "ghp_sekret123"


def run_helper(stdin, env_file, op="get"):
    env = {"PATH": os.environ["PATH"], "KARAKOS_ENV_FILE": str(env_file)}
    return subprocess.run([sys.executable, str(HELPER), op], input=stdin,
                          capture_output=True, text=True, env=env)


@pytest.fixture
def envfile(tmp_path):
    f = tmp_path / ".env"
    f.write_text(f"# c\nOTHER=1\nGITHUB_TOKEN={SECRET}\n")
    return f


def test_helper_prints_protocol_output(envfile):
    r = run_helper("protocol=https\nhost=github.com\n\n", envfile)
    assert r.returncode == 0
    assert r.stdout == f"username=x-access-token\npassword={SECRET}\n"


def test_helper_handles_quotes_and_export(tmp_path):
    f = tmp_path / ".env"
    f.write_text(f'export GITHUB_TOKEN="{SECRET}"\n')
    assert f"password={SECRET}\n" in run_helper("host=github.com\n", f).stdout


def test_helper_missing_token_no_output(tmp_path):
    f = tmp_path / ".env"
    f.write_text("OTHER=1\n")
    r = run_helper("host=github.com\n", f)
    assert r.returncode != 0 and r.stdout == ""
    r = run_helper("host=github.com\n", tmp_path / "nope")
    assert r.returncode != 0 and r.stdout == ""


def test_helper_ignores_other_hosts_and_store(envfile):
    r = run_helper("host=gitlab.com\n", envfile)
    assert r.returncode != 0 and r.stdout == ""
    r = run_helper("host=github.com\n", envfile, op="store")
    assert r.stdout == ""


def make_fake_gh(d):
    d.mkdir()
    gh = d / "gh"
    gh.write_text('#!/bin/sh\necho "TOKEN=${GH_TOKEN-unset} ARGS=$*"\n')
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return d


def run_shim(tmp_path, envfile, extra_path_first=True):
    fake = make_fake_gh(tmp_path / "real")
    env = {
        "PATH": f"{SHIM_DIR}:{fake}:/usr/bin:/bin",
        "KARAKOS_ENV_FILE": str(envfile),
    }
    return subprocess.run([str(SHIM_DIR / "gh"), "pr", "list"],
                          capture_output=True, text=True, env=env, timeout=10)


def test_gh_shim_sets_token_for_child_only_no_recursion(tmp_path, envfile):
    r = run_shim(tmp_path, envfile)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"TOKEN={SECRET} ARGS=pr list"
    assert "GH_TOKEN" not in os.environ


def test_gh_shim_without_token_runs_gh_unset(tmp_path):
    f = tmp_path / ".env"
    f.write_text("OTHER=1\n")
    r = run_shim(tmp_path, f)
    assert r.stdout.strip() == "TOKEN=unset ARGS=pr list"


def test_gh_shim_missing_real_gh_fails_without_looping(tmp_path, envfile):
    r = subprocess.run([str(SHIM_DIR / "gh")], capture_output=True, text=True,
                       env={"PATH": f"{SHIM_DIR}", "KARAKOS_ENV_FILE": str(envfile)},
                       timeout=10)
    assert r.returncode == 127 and SECRET not in r.stderr


def test_build_subprocess_env_wiring(monkeypatch):
    mod = import_script("agent-server")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    monkeypatch.setenv("GH_TOKEN", SECRET)
    env = mod.build_subprocess_env("amos")
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "credential.https://github.com.helper"
    assert env["GIT_CONFIG_VALUE_0"] == str(HELPER.resolve())
    assert env["PATH"].split(os.pathsep)[0] == str(SHIM_DIR.resolve())
    assert "GITHUB_TOKEN" not in env and "GH_TOKEN" not in env
    assert SECRET not in "".join(env.values())
